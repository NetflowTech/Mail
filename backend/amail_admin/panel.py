"""The admin panel: office network only, admin login with two-step sign-in,
no JavaScript.

Protections:
  • answers only the configured admin networks (private ranges by default),
  • scrypt-hashed passwords plus a mandatory authenticator code (TOTP); each
    code works once; attempts are rate-limited per address,
  • sessions live on the server: signing out, or a password change, ends them;
    30 minutes idle or 8 hours in total ends them too,
  • session cookie: HttpOnly, Secure, SameSite=Strict, holding only a random
    session id,
  • a CSRF token on every form,
  • a strict Content-Security-Policy (no scripts at all), no framing, no caching,
  • mailbox passwords are write-only in the panel, and a saved password is
    never tested against a server typed in afterwards. (Anyone who controls
    the panel can still point a mailbox at another server with a new
    password, which is why the panel gets all of the above.)
"""
from __future__ import annotations

import hashlib
import hmac
import imaplib
import ipaddress
import re
import secrets
import smtplib
import socket
import ssl
import time
from datetime import timedelta

from flask import (Flask, abort, flash, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup

from . import crypto
from . import oauth
from .config import ONLINE_FACTOR, Config
from .ratelimit import RateLimiter
from .store import Store

_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,253}\.[^@\s]{2,}$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")

SESSION_IDLE = 30 * 60
SESSION_MAX = 8 * 3600
PRE_AUTH_MAX = 5 * 60          # password accepted, code not yet entered
PUBLIC_ENDPOINTS = {"login", "login_code", "static", "certificate"}


def ip_in(addr: str, networks) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in networks if n.version == ip.version)


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, int(time.time() - ts))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _refuse_local_target(host: str) -> str | None:
    """The connection test may reach any mail server, private ones included,
    but not this machine or link-local / metadata addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"Can't find {host}: {e}"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return f"{host} points at an address the test won't connect to."
    return None


def test_mailbox(email: str, password: str, imap_host: str, imap_port: int,
                 smtp_host: str, smtp_port: int, timeout: int = 15) -> str | None:
    """Log in to IMAP and SMTP exactly the way AMail will (IMAP over TLS on
    any port; SMTP over TLS on 465, STARTTLS otherwise; certificates
    verified). None if both work, else a short reason."""
    for h in {imap_host, smtp_host}:
        bad = _refuse_local_target(h)
        if bad:
            return bad
    # The same trusted-authority list AMail uses (certifi), so the check
    # agrees with the app on every system (python.org's macOS Python, for
    # one, has no trust store of its own).
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        c = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx,
                              timeout=timeout)
        try:
            c.login(email, password)
        finally:
            try:
                c.logout()
            except Exception:
                pass
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as e:
        return f"IMAP: {e}"[:200]
    try:
        if smtp_port == 465:
            s = smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=timeout)
        else:
            s = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
            s.starttls(context=ctx)
        try:
            s.login(email, password)
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except (smtplib.SMTPException, OSError, ssl.SSLError) as e:
        return f"SMTP: {e}"[:200]
    return None


def _qr_svg(text: str) -> Markup:
    import segno
    # Generated here from our own data, so marking it safe is sound.
    return Markup(segno.make(text, error="m").svg_inline(scale=5, dark="#000",
                                                         light="#fff"))


def oauth_clients(store: Store) -> dict:
    """The Google / Microsoft app registrations in use (Settings), defaulting
    to the public desktop clients AMail itself uses."""
    out = {}
    for prov, default in oauth.DEFAULT_CLIENTS.items():
        cid = store.get_setting(f"oauth_{prov}_id")
        out[prov] = ({"id": cid, "secret": store.get_setting(f"oauth_{prov}_secret")}
                     if cid else dict(default))
    return out


def create_panel_app(cfg: Config, store: Store, master_key: bytes,
                     session_key: bytes, signing_pub: bytes,
                     limiter: RateLimiter | None = None,
                     mailbox_tester=test_mailbox, oauth_api=oauth) -> Flask:
    app = Flask(__name__)
    app.secret_key = session_key
    app.config.update(
        SESSION_COOKIE_NAME="amail_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(seconds=SESSION_MAX),
        MAX_CONTENT_LENGTH=256 * 1024,
    )
    app.jinja_env.filters["ago"] = ago
    limiter = limiter or RateLimiter()
    online_window = cfg.checkin_interval * ONLINE_FACTOR

    def sid_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def current_user() -> str | None:
        """The signed-in admin, from a live server-side session."""
        token = session.get("sid")
        if not token:
            return None
        row = store.session(sid_hash(token))
        now = time.time()
        if (row is None or now - row["last_seen"] > SESSION_IDLE
                or now - row["created_at"] > SESSION_MAX):
            if row is not None:
                store.end_session(row["id"])
            return None
        return row["username"]

    # ----- request guards -----

    @app.before_request
    def _guard():
        if not ip_in(request.remote_addr or "", cfg.admin_networks):
            abort(403)
        user = current_user()
        request.admin = user
        if request.endpoint not in PUBLIC_ENDPOINTS:
            if not user:
                session.pop("sid", None)
                return redirect(url_for("login"))
            store.touch_session(sid_hash(session["sid"]))
        if request.method == "POST":
            token = request.form.get("csrf", "")
            if not session.get("csrf") or not hmac.compare_digest(
                    token.encode(), session["csrf"].encode()):
                abort(400)

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; "
            "img-src 'self'; frame-ancestors 'none'; form-action 'self'; "
            "base-uri 'none'")
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        user = getattr(request, "admin", None)
        alerts = []
        if user:
            day = time.time() - 86400
            failed = (store.audit_count("login_failed", day)
                      + store.audit_count("code_failed", day))
            if failed >= 5:
                alerts.append(f"{failed} failed admin sign-in attempts in the "
                              "last 24 hours. See Activity.")
            refused = store.audit_count("enroll_refused", day)
            if refused >= 5:
                alerts.append(f"{refused} PCs tried to join with a wrong code in "
                              "the last 24 hours. Someone may be guessing.")
            if store.audit_count("enroll_refused_full", day):
                alerts.append("PCs were turned away because too many are waiting "
                              "for approval. Your setup code may have leaked: "
                              "make a new enrollment code in Settings.")
        return {"csrf": session["csrf"], "user": user, "alerts": alerts,
                "pending": store.pending_count() if user else 0}

    def actor() -> str:
        return f"admin:{request.admin or '?'}"

    # ----- helpers -----

    def is_online(dev) -> bool:
        return bool(dev["last_seen"]) and time.time() - dev["last_seen"] < online_window

    def device_view(dev) -> dict:
        """A device plus the state of every account on it."""
        import json
        report = {a["email"]: a for a in json.loads(dev["report"] or "[]")}
        online = is_online(dev)
        rows = []
        granted = set()
        for m in store.granted_mailboxes(dev["id"]):
            email = m["email"].lower()
            granted.add(email)
            r = report.get(email)
            if r and r.get("managed"):
                st = r.get("status")
                state = {"running": ("Signed in", "ok"),
                         "paused": ("Paused on PC", "warn")}.get(
                    st, (f"Error: {r.get('error') or st}", "bad"))
            else:
                state = (("Signing in…", "warn") if online
                         else ("Waiting for PC to come online", "muted"))
            rows.append({"mailbox": m, "email": email, "label": state[0],
                         "tone": state[1], "granted": True})
        for email, r in report.items():
            if r.get("managed") and email not in granted:
                rows.append({"mailbox": store.mailbox_by_email(email),
                             "email": email, "granted": False,
                             "label": ("Signing out…" if online
                                       else "Signs out when the PC is next online"),
                             "tone": "warn"})
        personal = sorted(e for e, r in report.items() if not r.get("managed"))
        return {"d": dev, "online": online, "accounts": rows,
                "personal": personal,
                "check_code": crypto.check_code(dev["public_key"])}

    def mailbox_form() -> tuple[dict, str | None]:
        f = {k: (request.form.get(k) or "").strip() for k in
             ("email", "display_name", "imap_host", "imap_port", "smtp_host",
              "smtp_port")}
        f["password"] = request.form.get("password") or ""
        f["email"] = f["email"].lower()
        if not _EMAIL.match(f["email"]):
            return f, "Enter a valid email address."
        for k in ("imap_host", "smtp_host"):
            if not _HOST.match(f[k]):
                return f, "Enter a valid server name (letters, digits, dots, dashes)."
        for k in ("imap_port", "smtp_port"):
            if not f[k].isdigit() or not 0 < int(f[k]) < 65536:
                return f, "Ports must be numbers between 1 and 65535."
            f[k] = int(f[k])
        if len(f["display_name"]) > 120:
            return f, "Display name is too long."
        return f, None

    def start_session(username: str) -> None:
        token = secrets.token_urlsafe(32)
        session.clear()
        session.permanent = True
        session["sid"] = token
        session["csrf"] = secrets.token_urlsafe(32)
        store.prune_sessions(SESSION_IDLE, SESSION_MAX)
        store.add_session(sid_hash(token), username)

    # ----- sign in: password, then authenticator code -----

    @app.get("/certificate")
    def certificate():
        """The panel's own certificate, to install as trusted on admin PCs so
        the browser stops warning (a warning trains people to click through)."""
        return send_file(cfg.cert_path, mimetype="application/x-x509-ca-cert",
                         as_attachment=True, download_name="amail-admin.crt")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ip = request.remote_addr or ""
            # Limited per address only: a per-username limit would let anyone
            # on the network lock the admin out by typing wrong passwords.
            if not limiter.allow(f"login:{ip}", 10, 900):
                flash("Too many attempts from this computer. Wait 15 minutes.",
                      "bad")
                return render_template("login.html"), 429
            user = (request.form.get("username") or "").strip()
            row = store.get_admin(user) if user else None
            ok = crypto.check_password(request.form.get("password") or "",
                                       row["pw_hash"] if row else
                                       "scrypt$AAAAAAAAAAAAAAAAAAAAAA$AAAA")
            if row is not None and ok:
                session.clear()
                session["pre"] = {"user": row["username"], "t": time.time()}
                session["csrf"] = secrets.token_urlsafe(32)
                if not row["totp_secret"]:
                    session["totp_new"] = crypto.new_totp_secret()
                return redirect(url_for("login_code"))
            store.audit("panel", "login_failed", f"user={user[:60]} ip={ip}")
            flash("Wrong username or password.", "bad")
        return render_template("login.html")

    @app.route("/login/code", methods=["GET", "POST"])
    def login_code():
        pre = session.get("pre")
        if not pre or time.time() - pre.get("t", 0) > PRE_AUTH_MAX:
            session.clear()
            return redirect(url_for("login"))
        row = store.get_admin(pre["user"])
        if row is None:
            session.clear()
            return redirect(url_for("login"))
        enrolling = not row["totp_secret"]
        secret = session.get("totp_new") if enrolling else row["totp_secret"]
        if not secret:
            session.clear()
            return redirect(url_for("login"))
        if request.method == "POST":
            ip = request.remote_addr or ""
            if not limiter.allow(f"code:{ip}", 10, 900):
                flash("Too many attempts. Wait 15 minutes.", "bad")
                session.clear()
                return redirect(url_for("login"))
            step = crypto.totp_verify(secret, request.form.get("code") or "",
                                      row["totp_last_step"])
            accepted = False
            if step is not None:
                if enrolling:
                    store.set_totp(row["username"], secret, step)
                    store.audit(f"admin:{row['username']}", "two_step_enabled")
                    accepted = True
                else:
                    accepted = store.use_totp_step(row["username"], step)
            if accepted:
                start_session(row["username"])
                store.audit(f"admin:{row['username']}", "login", f"ip={ip}")
                return redirect(url_for("dashboard"))
            store.audit("panel", "code_failed", f"user={row['username']} ip={ip}")
            flash("That code isn't right. Use the current code from your "
                  "authenticator app.", "bad")
        setup = None
        if enrolling:
            setup = {"secret": secret,
                     "qr": _qr_svg(crypto.totp_uri(secret, row["username"]))}
        return render_template("login_code.html", setup=setup)

    @app.post("/logout")
    def logout():
        token = session.get("sid")
        if token:
            store.end_session(sid_hash(token))
        session.clear()
        return redirect(url_for("login"))

    # ----- dashboard / devices -----

    @app.get("/")
    def dashboard():
        views = [device_view(d) for d in store.devices()]
        return render_template("dashboard.html", views=views,
                               mailboxes=store.mailboxes())

    @app.get("/devices/<device_id>")
    def device_page(device_id):
        dev = store.device(device_id) or abort(404)
        view = device_view(dev)
        granted = {r["email"] for r in view["accounts"] if r["granted"]}
        available = [m for m in store.mailboxes()
                     if m["email"].lower() not in granted]
        return render_template("device.html", v=view, available=available)

    def _device_action(device_id, fn, message):
        dev = store.device(device_id) or abort(404)
        fn(dev)
        flash(message.format(name=dev["name"]), "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/approve")
    def device_approve(device_id):
        dev = store.device(device_id) or abort(404)
        given = "".join(ch for ch in (request.form.get("check_code") or "").upper()
                        if ch.isalnum())
        expected = crypto.check_code(dev["public_key"]).replace("-", "")
        if not hmac.compare_digest(given.encode(), expected.encode()):
            store.audit(actor(), "approve_code_mismatch", f"{dev['name']} ({dev['id']})")
            flash("The check code doesn't match. Read it from the PC itself "
                  "(AMail → Settings → Organization). If it really doesn't "
                  "match, this request didn't come from that PC: block it.", "bad")
            return redirect(url_for("device_page", device_id=device_id))

        def go(dev):
            store.set_device_status(dev["id"], "approved")
            store.audit(actor(), "device_approved", f"{dev['name']} ({dev['id']})")
        return _device_action(device_id, go, "{name} approved.")

    @app.post("/devices/<device_id>/block")
    def device_block(device_id):
        def go(dev):
            store.set_device_status(dev["id"], "revoked")
            store.audit(actor(), "device_blocked", f"{dev['name']} ({dev['id']})")
        return _device_action(
            device_id, go,
            "{name} blocked. Every managed account is removed from it at its "
            "next check-in.")

    @app.post("/devices/<device_id>/rename")
    def device_rename(device_id):
        name = (request.form.get("name") or "").strip()[:80]
        if not name:
            flash("Enter a name.", "bad")
            return redirect(url_for("device_page", device_id=device_id))

        def go(dev):
            store.rename_device(dev["id"], name)
            store.audit(actor(), "device_renamed", f"{dev['name']} -> {name}")
        return _device_action(device_id, go, "Renamed.")

    @app.post("/devices/<device_id>/delete")
    def device_delete(device_id):
        dev = store.device(device_id) or abort(404)
        if dev["status"] == "approved":
            flash("Block the PC first, and let it check in so its managed "
                  "accounts are removed. Then delete it.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        store.delete_device(device_id)
        store.audit(actor(), "device_deleted", f"{dev['name']} ({dev['id']})")
        flash(f"{dev['name']} deleted.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/devices/<device_id>/grant")
    def device_grant(device_id):
        dev = store.device(device_id) or abort(404)
        if dev["status"] != "approved":
            flash("Approve the PC before signing it in to accounts.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        mb = store.mailbox(int(request.form.get("mailbox_id") or 0)) or abort(404)
        store.grant(dev["id"], mb["id"])
        store.audit(actor(), "signed_in", f"{mb['email']} on {dev['name']}")
        flash(f"{mb['email']} will be signed in on {dev['name']} at its next "
              "check-in.", "ok")
        if request.form.get("next") == "mailbox":
            return redirect(url_for("mailbox_page", mailbox_id=mb["id"]))
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/ungrant")
    def device_ungrant(device_id):
        dev = store.device(device_id) or abort(404)
        mb = store.mailbox(int(request.form.get("mailbox_id") or 0)) or abort(404)
        store.revoke(dev["id"], mb["id"])
        store.audit(actor(), "signed_out", f"{mb['email']} on {dev['name']}")
        flash(f"{mb['email']} will be signed out of {dev['name']} at its next "
              "check-in.", "ok")
        if request.form.get("next") == "mailbox":
            return redirect(url_for("mailbox_page", mailbox_id=mb["id"]))
        return redirect(url_for("device_page", device_id=device_id))

    # ----- mailboxes -----

    def mailbox_rows():
        return [{"m": m, "devices": store.devices_for_mailbox(m["id"])}
                for m in store.mailboxes()]

    @app.get("/mailboxes")
    def mailboxes():
        return render_template("mailboxes.html", rows=mailbox_rows(), form={})

    @app.post("/mailboxes/add")
    def mailbox_add():
        f, err = mailbox_form()
        if not err and not f["password"]:
            err = "Enter the mailbox password."
        if not err and store.mailbox_by_email(f["email"]):
            err = f"{f['email']} is already in the list."
        if not err and request.form.get("test"):
            err = mailbox_tester(f["email"], f["password"], f["imap_host"],
                                 f["imap_port"], f["smtp_host"], f["smtp_port"])
            if err:
                err = f"Couldn't sign in with these settings. {err}"
        if err:
            flash(err, "bad")
            f.pop("password", None)
            return render_template("mailboxes.html", rows=mailbox_rows(), form=f), 400
        # Insert first to get the id the ciphertext is bound to.
        mid = store.add_mailbox(f["email"], f["display_name"], f["imap_host"],
                                f["imap_port"], f["smtp_host"], f["smtp_port"],
                                b"pending")
        store.update_mailbox(mid, display_name=f["display_name"],
                             imap_host=f["imap_host"], imap_port=f["imap_port"],
                             smtp_host=f["smtp_host"], smtp_port=f["smtp_port"],
                             password_enc=crypto.encrypt_stored(
                                 master_key, f["password"], f"mailbox:{mid}"))
        store.audit(actor(), "mailbox_added", f["email"])
        flash(f"{f['email']} added. Sign it in on a PC from here or the PC's page.",
              "ok")
        return redirect(url_for("mailbox_page", mailbox_id=mid))

    # ----- Google / Microsoft sign-in (no mailbox password) -----

    @app.post("/mailboxes/oauth/start")
    def mailbox_oauth_start():
        provider = request.form.get("provider") or ""
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("display_name") or "").strip()[:120]
        renew = int(request.form.get("mailbox_id") or 0)
        if provider not in oauth.PROVIDERS:
            abort(400)
        if renew:
            m = store.mailbox(renew) or abort(404)
            email, name = m["email"].lower(), m["display_name"]
        elif not _EMAIL.match(email):
            flash("Enter a valid email address.", "bad")
            return redirect(url_for("mailboxes"))
        elif store.mailbox_by_email(email):
            flash(f"{email} is already in the list. Use Renew sign-in on its "
                  "page instead.", "bad")
            return redirect(url_for("mailboxes"))
        pending = oauth_api.start(provider, email, oauth_clients(store)[provider])
        pending.update(name=name, mailbox_id=renew, t=time.time())
        session["oauth"] = pending
        return redirect(url_for("mailbox_oauth"))

    @app.get("/mailboxes/oauth")
    def mailbox_oauth():
        pending = session.get("oauth")
        if not pending or time.time() - pending["t"] > 900:
            session.pop("oauth", None)
            flash("That sign-in expired. Start again.", "bad")
            return redirect(url_for("mailboxes"))
        return render_template("mailbox_oauth.html", p=pending,
                               label=oauth.PROVIDERS[pending["provider"]]["label"])

    @app.post("/mailboxes/oauth/finish")
    def mailbox_oauth_finish():
        pending = session.get("oauth")
        if not pending or time.time() - pending["t"] > 900:
            session.pop("oauth", None)
            flash("That sign-in expired. Start again.", "bad")
            return redirect(url_for("mailboxes"))
        provider, email = pending["provider"], pending["email"]
        client = oauth_clients(store)[provider]
        try:
            code = oauth_api.code_from_pasted(pending, request.form.get("pasted"))
            tok = oauth_api.exchange(pending, code, client)
            oauth_api.check_mailbox(provider, email, tok["access_token"])
        except oauth.OAuthError as e:
            flash(str(e), "bad")
            return redirect(url_for("mailbox_oauth"))
        session.pop("oauth", None)
        imap_h, imap_p, smtp_h, smtp_p = oauth.PROVIDERS[provider]["servers"]
        mid = pending.get("mailbox_id") or 0
        old = store.mailbox(mid) if mid else None
        if old is None:
            if store.mailbox_by_email(email):
                flash(f"{email} was added meanwhile.", "bad")
                return redirect(url_for("mailboxes"))
            mid = store.add_mailbox(email, pending.get("name") or "", imap_h,
                                    imap_p, smtp_h, smtp_p, b"pending", provider)
        store.update_mailbox(mid, display_name=pending.get("name") or "",
                             imap_host=imap_h, imap_port=imap_p,
                             smtp_host=smtp_h, smtp_port=smtp_p,
                             password_enc=crypto.encrypt_stored(
                                 master_key, tok["refresh_token"], f"mailbox:{mid}"),
                             auth=provider)
        # The registration that issued this token is the one PCs must use.
        import json as _json
        store.set_setting(f"mbclient:{mid}", _json.dumps(client))
        if old is not None and old["auth"] == provider:
            # The old token stops working everywhere, including any copy taken
            # off a PC. PCs still signed in get the new one at check-in.
            try:
                oauth_api.revoke(provider, crypto.decrypt_stored(
                    master_key, old["password_enc"], f"mailbox:{mid}"))
            except Exception:
                pass
        store.audit(actor(), "mailbox_signed_in_with_" + provider,
                    email + (" (renewed)" if old is not None else ""))
        flash(f"{email} is signed in with {oauth.PROVIDERS[provider]['label']}. "
              + ("PCs signed in to it get the new sign-in at their next check-in."
                 if old is not None else "Now sign it in on a PC."), "ok")
        return redirect(url_for("mailbox_page", mailbox_id=mid))

    @app.get("/mailboxes/<int:mailbox_id>")
    def mailbox_page(mailbox_id):
        m = store.mailbox(mailbox_id) or abort(404)
        on = store.devices_for_mailbox(mailbox_id)
        on_ids = {d["id"] for d in on}
        others = [d for d in store.devices()
                  if d["status"] == "approved" and d["id"] not in on_ids]
        return render_template("mailbox.html", m=m, on=on, others=others,
                               online=is_online)

    @app.post("/mailboxes/<int:mailbox_id>/edit")
    def mailbox_edit(mailbox_id):
        m = store.mailbox(mailbox_id) or abort(404)
        if m["auth"] != "password":
            abort(400)          # Google / Microsoft mailboxes: renew instead
        f, err = mailbox_form()
        if not err and f["email"] != m["email"].lower():
            err = ("The email address can't be changed. Add it as a new "
                   "mailbox instead.")
        servers_changed = not err and (
            f["imap_host"].lower(), f["imap_port"], f["smtp_host"].lower(),
            f["smtp_port"]) != (m["imap_host"].lower(), m["imap_port"],
                                m["smtp_host"].lower(), m["smtp_port"])
        if not err and servers_changed and not f["password"]:
            # The saved password only ever goes to the servers it was saved
            # for: changing a server needs it typed again.
            err = ("Changing a server needs the password typed again, so the "
                   "saved one is never sent to a new server.")
        if not err and request.form.get("test"):
            pw = f["password"] or crypto.decrypt_stored(
                master_key, m["password_enc"], f"mailbox:{mailbox_id}")
            err = mailbox_tester(f["email"], pw, f["imap_host"], f["imap_port"],
                                 f["smtp_host"], f["smtp_port"])
            if err:
                err = f"Couldn't sign in with these settings. {err}"
        if err:
            flash(err, "bad")
            return redirect(url_for("mailbox_page", mailbox_id=mailbox_id))
        enc = (crypto.encrypt_stored(master_key, f["password"],
                                     f"mailbox:{mailbox_id}")
               if f["password"] else None)
        store.update_mailbox(mailbox_id, display_name=f["display_name"],
                             imap_host=f["imap_host"], imap_port=f["imap_port"],
                             smtp_host=f["smtp_host"], smtp_port=f["smtp_port"],
                             password_enc=enc)
        store.audit(actor(), "mailbox_changed",
                    m["email"] + (" (servers)" if servers_changed else "")
                    + (" (new password)" if enc else ""))
        flash("Saved. PCs signed in to this mailbox pick it up at their next "
              "check-in.", "ok")
        return redirect(url_for("mailbox_page", mailbox_id=mailbox_id))

    @app.post("/mailboxes/<int:mailbox_id>/delete")
    def mailbox_delete(mailbox_id):
        m = store.mailbox(mailbox_id) or abort(404)
        store.delete_mailbox(mailbox_id)
        store.audit(actor(), "mailbox_deleted", m["email"])
        flash(f"{m['email']} deleted. Every PC signed in to it is signed out at "
              "its next check-in.", "ok")
        return redirect(url_for("mailboxes"))

    # ----- settings / audit -----

    def setup_code() -> str:
        url = store.get_setting("public_url")
        fp = (crypto.certificate_fingerprint(cfg.cert_path)
              if store.get_setting("pin_cert", "1") == "1" else "")
        return crypto.make_setup_code(url, fp, store.get_setting("enroll_code"),
                                      signing_pub)

    @app.get("/settings")
    def settings_page():
        return render_template(
            "settings.html", code=setup_code(),
            public_url=store.get_setting("public_url"),
            pin=store.get_setting("pin_cert", "1") == "1",
            enroll_open=store.get_setting("enroll_open", "1") == "1",
            enroll_code=store.get_setting("enroll_code"),
            fingerprint=crypto.certificate_fingerprint(cfg.cert_path),
            oauth_ids={p: store.get_setting(f"oauth_{p}_id")
                       for p in oauth.PROVIDERS})

    @app.post("/settings/server")
    def settings_server():
        url = (request.form.get("public_url") or "").strip().rstrip("/")
        if not re.match(r"^https://[A-Za-z0-9.-]+(:\d{1,5})?$", url):
            flash("The address must look like https://name-or-ip:8443", "bad")
            return redirect(url_for("settings_page"))
        store.set_setting("public_url", url)
        store.set_setting("pin_cert", "1" if request.form.get("pin") else "0")
        store.audit(actor(), "server_address_changed", url)
        flash("Saved. Use the new setup code for PCs you add from now on.", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/settings/oauth")
    def settings_oauth():
        for prov in oauth.PROVIDERS:
            cid = (request.form.get(f"{prov}_id") or "").strip()[:200]
            secret = (request.form.get(f"{prov}_secret") or "").strip()[:200]
            store.set_setting(f"oauth_{prov}_id", cid)
            if secret or not cid:
                store.set_setting(f"oauth_{prov}_secret", secret)
        store.audit(actor(), "sign_in_apps_changed")
        flash("Saved. Mailboxes signed in before keep their old registration "
              "until you renew their sign-in.", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/settings/enrollment")
    def settings_enrollment():
        if request.form.get("rotate"):
            store.set_setting("enroll_code", crypto.new_enroll_code())
            store.audit(actor(), "enroll_code_rotated")
            flash("New enrollment code made. The old one no longer works.", "ok")
        else:
            on = "1" if request.form.get("open") else "0"
            store.set_setting("enroll_open", on)
            store.audit(actor(), "enrollment_" + ("opened" if on == "1" else "closed"))
            flash("Saved.", "ok")
        return redirect(url_for("settings_page"))

    @app.post("/settings/password")
    def settings_password():
        row = store.get_admin(request.admin)
        if not row or not crypto.check_password(
                request.form.get("current") or "", row["pw_hash"]):
            flash("Current password is wrong.", "bad")
            return redirect(url_for("settings_page"))
        new = request.form.get("new") or ""
        if len(new) < 12:
            flash("Use at least 12 characters.", "bad")
            return redirect(url_for("settings_page"))
        store.set_admin(row["username"], crypto.hash_password(new))   # ends sessions
        store.audit(actor(), "admin_password_changed")
        session.clear()
        flash("Password changed. Sign in again (every other session was ended "
              "too).", "ok")
        return redirect(url_for("login"))

    @app.get("/audit")
    def audit_page():
        return render_template("audit.html", rows=store.audit_log())

    return app
