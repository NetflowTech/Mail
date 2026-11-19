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
import json
import hmac
import imaplib
import ipaddress
import re
import secrets
import smtplib
import socket
import ssl
import time
from datetime import datetime, timedelta

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
PAGE = 100                     # mailboxes per page
GRID_MAX = 12                  # more mailboxes than this: the overview lists PCs
PICK_MAX = 200                 # mailboxes offered at once on a PC's page
MAILHOST_KEYS = ("imap_host", "imap_port", "smtp_host", "smtp_port")


def parse_import(text: str) -> tuple[list[dict], list[str]]:
    """Lines of ``email:password`` or ``email,password,display name``.
    Returns (entries, bad lines)."""
    entries, bad = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = ""
        if "," in line.split(":", 1)[0] or (":" not in line and "," in line):
            parts = [p.strip() for p in line.split(",")]
            email, password = parts[0], parts[1] if len(parts) > 1 else ""
            name = ", ".join(parts[2:]).strip()[:120]
        else:
            email, _, password = line.partition(":")
            email, password = email.strip(), password.strip()
        email = email.lower()
        if not _EMAIL.match(email) or not password:
            bad.append(raw.strip()[:80])
            continue
        entries.append({"email": email, "password": password, "display_name": name})
    return entries, bad
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,39}$")
STALE_AFTER = 3 * 86400        # an approved PC unseen this long is flagged

# What the activity page says for each recorded action.
ACTION_LABELS = {
    "login": "Signed in",
    "login_failed": "Wrong username or password",
    "code_failed": "Wrong authenticator code",
    "two_step_enabled": "Set up two-step sign-in",
    "enrolled": "A PC asked to join",
    "enroll_refused": "A PC tried to join with a wrong join code",
    "enroll_refused_full": "A PC was turned away: too many waiting for approval",
    "device_approved": "Approved a PC",
    "approve_code_mismatch": "Typed a check code that didn't match",
    "device_blocked": "Blocked a PC",
    "device_renamed": "Renamed a PC",
    "device_edited": "Changed a PC's name or note",
    "device_deleted": "Removed a PC",
    "signed_in": "Put a mailbox on a PC",
    "signed_out": "Took a mailbox off a PC",
    "takeover_requested": "Asked a PC to hand over an account",
    "takeover_cancelled": "Withdrew a hand-over request",
    "takeover_ignored": "Left an account on a PC personal",
    "takeover_done": "A PC handed over an account",
    "takeover_declined": "A user declined to hand over an account",
    "mailbox_added": "Added a mailbox",
    "mailboxes_imported": "Added mailboxes from a list",
    "signed_out_all": "Took every mailbox off a PC",
    "copied_mailboxes": "Copied a PC's mailboxes to another PC",
    "mailbox_changed": "Changed a mailbox",
    "mailbox_deleted": "Deleted a mailbox",
    "server_address_changed": "Changed the server address",
    "sign_in_apps_changed": "Changed the Google/Microsoft app registration",
    "enroll_code_rotated": "Made a new join code",
    "enrollment_opened": "Let new PCs join",
    "enrollment_closed": "Stopped new PCs joining",
    "admin_password_changed": "Changed their password",
    "admin_added": "Added an admin",
    "admin_removed": "Removed an admin",
    "admin_two_step_reset": "Reset an admin's two-step sign-in",
    "admin_set": "Set an admin's password from the server console",
    "master_key_exported": "Exported the master key from the server console",
}


def action_label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.startswith("mailbox_signed_in_with_"):
        return "Signed a mailbox in with " + action.rsplit("_", 1)[1].capitalize()
    return action.replace("_", " ").capitalize()


def when(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%a %d %b %Y, %H:%M")


def event_text(kind: str, detail) -> str:
    """One plain sentence for something a user did."""
    try:
        d = json.loads(detail) if isinstance(detail, str) else (detail or {})
    except ValueError:
        d = {}
    subj = f"\u201c{d['subject']}\u201d" if d.get("subject") else "an email"
    who = d.get("who", "")
    name = d.get("name", "")
    frm = f" from {who}" if who else ""
    to = f" to {who}" if who else ""
    return {
        "opened": f"Opened {subj}{frm}",
        "sent": f"Sent {subj}{to}",
        "replied": f"Replied with {subj}{to}",
        "deleted": f"Moved {subj}{frm} to Trash",
        "deleted_forever": f"Deleted {subj}{frm} permanently",
        "spam": f"Marked {subj}{frm} as spam",
        "restored": f"Restored {subj}{frm} to the Inbox",
        "marked_read": f"Marked {subj} as read",
        "marked_unread": f"Marked {subj} as unread",
        "attachment_opened": f"Opened attachment {name} from {subj}",
        "attachment_saved": f"Saved attachment {name} from {subj}",
        "printed": f"Printed {subj}",
        "signed_in": "Mailbox signed in on this PC",
        "signed_out": "Mailbox signed out of this PC",
        "app_start": "Started AMail",
        "app_quit": "Closed AMail",
    }.get(kind, kind.replace("_", " ").capitalize())


def who_label(actor: str) -> str:
    if actor.startswith("admin:"):
        return actor[6:]
    return {"device": "A PC", "panel": "Someone at the sign-in page",
            "console": "The server console"}.get(actor, actor)


def limit_key(addr: str | None = None) -> str:
    """What a login limit counts against. An IPv4 address as is; an IPv6
    address as its /64, since a machine on a LAN can give itself any
    address in its /64 at will."""
    addr = request.remote_addr if addr is None else addr
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return addr or "?"
    if ip.version == 6:
        return str(ipaddress.ip_network((ip, 64), strict=False))
    return str(ip)


def form_int(name: str, source=None, default: int = 0) -> int:
    """An integer field from the form (or *source*); anything else is a
    400, not a 500."""
    raw = (request.form if source is None else source).get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort(400)


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
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 7200:
        return "1 hour ago"
    if s < 86400:
        return f"{s // 3600} hours ago"
    if s < 172800:
        return "1 day ago"
    return f"{s // 86400} days ago"


def _checked_address(host: str) -> tuple[str | None, str]:
    """(error, address). The connection test may reach any mail server,
    private ones included, but not this machine or link-local / metadata
    addresses. The address checked here is the one the test then connects
    to: resolving the name a second time would let a DNS name that answers
    differently each time pass the check and then point at 127.0.0.1."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as e:
        return f"Can't find {host}: {e}", ""
    chosen = ""
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            return f"{host} points at an address the test won't connect to.", ""
        chosen = chosen or str(ip)
    if not chosen:
        return f"Can't find {host}.", ""
    return None, chosen


class _PinnedIMAP(imaplib.IMAP4_SSL):
    """IMAP over TLS to a checked address, with the certificate still
    verified against the host name."""

    def __init__(self, host, port, address, ssl_context, timeout):
        self._pinned = address
        super().__init__(host, port, ssl_context=ssl_context, timeout=timeout)

    def _create_socket(self, timeout):
        sock = socket.create_connection((self._pinned, self.port), timeout)
        return self.ssl_context.wrap_socket(sock, server_hostname=self.host)


class _PinnedSMTP(smtplib.SMTP):
    def __init__(self, host, port, address, timeout):
        self._pinned = address
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        return socket.create_connection((self._pinned, port), timeout)


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    def __init__(self, host, port, address, context, timeout):
        self._pinned = address
        super().__init__(host, port, context=context, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        sock = socket.create_connection((self._pinned, port), timeout)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def test_mailbox(email: str, password: str, imap_host: str, imap_port: int,
                 smtp_host: str, smtp_port: int, timeout: int = 15) -> str | None:
    """Log in to IMAP and SMTP exactly the way AMail will (IMAP over TLS on
    any port; SMTP over TLS on 465, STARTTLS otherwise; certificates
    verified). None if both work, else a short reason."""
    addresses = {}
    for h in {imap_host, smtp_host}:
        bad, addresses[h] = _checked_address(h)
        if bad:
            return bad
    # The same trusted-authority list AMail uses (certifi), so the check
    # agrees with the app on every system (python.org's macOS Python, for
    # one, has no trust store of its own).
    import certifi
    ctx = ssl.create_default_context(cafile=certifi.where())
    try:
        c = _PinnedIMAP(imap_host, imap_port, addresses[imap_host], ctx, timeout)
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
            s = _PinnedSMTPSSL(smtp_host, smtp_port, addresses[smtp_host], ctx, timeout)
        else:
            s = _PinnedSMTP(smtp_host, smtp_port, addresses[smtp_host], timeout)
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
    app.jinja_env.filters["when"] = when
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
                    st, (f"Can't sign in: {r.get('error') or st}", "bad"))
            else:
                state = (("Signing in…", "warn") if online
                         else ("Waiting for PC to come online", "muted"))
            rows.append({"mailbox": m, "email": email, "label": state[0],
                         "tone": state[1], "granted": True,
                         "error": (r or {}).get("error") or ""})
        for email, r in report.items():
            if r.get("managed") and email not in granted:
                rows.append({"mailbox": store.mailbox_by_email(email),
                             "email": email, "granted": False,
                             "label": ("Signing out…" if online
                                       else "Signs out when the PC is next online"),
                             "tone": "warn"})
        personal = sorted(e for e, r in report.items() if not r.get("managed"))
        takeovers = {t["email"]: t for t in store.takeovers_for(dev["id"])}
        personal_rows = [{"email": e, "kind": report[e].get("kind") or "password",
                          "takeover": takeovers.get(e)} for e in personal]
        state, label, tag = {
            "pending": ("pending", "Waiting for approval", "warn"),
            "revoked": ("revoked", "Blocked", "bad"),
        }.get(dev["status"], ("online", "Online", "ok") if online
              else ("offline", "Offline", "muted"))
        stale = (dev["status"] == "approved" and not online and granted
                 and time.time() - (dev["last_seen"] or 0) > STALE_AFTER)
        return {"d": dev, "online": online, "accounts": rows,
                "by_email": {r["email"]: r for r in rows},
                "personal": personal, "personal_rows": personal_rows,
                "state": state, "state_label": label,
                "tag": tag, "stale": stale}

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
            # Limited per address (an IPv6 /64, so a LAN machine can't dodge
            # it by changing its address) plus a ceiling for everyone
            # together. Not per username: that would let anyone on the
            # network lock the admin out by typing wrong passwords.
            if not (limiter.allow(f"login:{limit_key()}", 10, 900)
                    and limiter.allow("login:all", 60, 900)):
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
            if not (limiter.allow(f"code:{limit_key()}", 10, 900)
                    and limiter.allow("code:all", 60, 900)):
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
        q = (request.args.get("q") or "").strip()[:80]
        found = []
        if q:
            by_mb = store.grants_by_mailbox()
            found = [{"m": m, "devices": by_mb.get(m["id"], [])}
                     for m in store.mailboxes(q, 50)]
        attention = []
        for v in views:
            d = v["d"]
            if d["status"] == "pending":
                attention.append({"kind": "pending", "v": v})
            elif d["status"] == "approved":
                for a in v["accounts"]:
                    if a["tone"] == "bad":
                        why = f" ({a['error']})" if a.get("error") else ""
                        attention.append({
                            "kind": "error", "v": v,
                            "text": f"{d['name']} can't sign in to {a['email']}"
                                    f"{why}. Usually the password was changed at "
                                    "the mail host: update it on the mailbox's "
                                    "page."})
                if v["stale"]:
                    attention.append({
                        "kind": "stale", "v": v,
                        "text": f"{d['name']} was last seen "
                                f"{ago(d['last_seen'])} and still has mailboxes "
                                "on it. Is it switched off, or disconnected?"})
                fresh = [p["email"] for p in v["personal_rows"]
                         if p["kind"] == "password" and p["takeover"] is None]
                if fresh:
                    attention.append({
                        "kind": "personal", "v": v,
                        "text": f"{d['name']} has {len(fresh)} account(s) its "
                                f"user added that you don't manage: "
                                f"{', '.join(fresh)}. Take them over, or leave "
                                "them personal."})
                for p in v["personal_rows"]:
                    t = p["takeover"]
                    if t is not None and t["status"] == "declined":
                        attention.append({
                            "kind": "declined", "v": v,
                            "text": f"The person using {d['name']} declined to "
                                    f"hand over {p['email']}"
                                    + (f" ({t['note']})" if t["note"] else "")
                                    + ". Ask them, then try again or leave it "
                                    "personal."})
        setup = {"url": bool(store.get_setting("public_url")),
                 "mailbox": bool(store.mailboxes()),
                 "pc": any(v["d"]["status"] == "approved" for v in views)}
        setup["show"] = not all(setup.values())
        total = store.mailbox_count()
        return render_template("dashboard.html", views=views,
                               attention=attention, setup=setup,
                               mailboxes=store.mailboxes() if total <= GRID_MAX else [],
                               grid=total <= GRID_MAX, total=total, q=q, found=found)

    @app.get("/pcs")
    def pcs_page():
        return render_template("pcs.html",
                               views=[device_view(d) for d in store.devices()])

    @app.get("/devices/<device_id>")
    def device_page(device_id):
        dev = store.device(device_id) or abort(404)
        view = device_view(dev)
        granted = {r["email"] for r in view["accounts"] if r["granted"]}
        q = (request.args.get("q") or "").strip()[:80]
        pool = [m for m in store.mailboxes(q) if m["email"].lower() not in granted]
        available, available_total = pool[:PICK_MAX], len(pool)
        others = [d for d in store.devices()
                  if d["status"] == "approved" and d["id"] != dev["id"]
                  and store.granted_mailboxes(d["id"])]
        recent = [{"at": e["at"], "email": e["email"],
                   "text": event_text(e["kind"], e["detail"])}
                  for e in store.events(device_id=device_id, limit=15)]
        return render_template("device.html", v=view, available=available,
                               available_total=available_total, q=q,
                               others=others, recent=recent)

    @app.get("/users")
    def users_page():
        device_id = (request.args.get("pc") or "").strip()
        email = (request.args.get("mailbox") or "").strip().lower()
        day = (request.args.get("day") or "").strip()
        since = until = None
        if day:
            try:
                start = datetime.strptime(day, "%Y-%m-%d")
                since, until = start.timestamp(), (start + timedelta(days=1)).timestamp()
            except ValueError:
                day = ""
        rows = [{"at": e["at"], "pc": e["device_name"], "pc_id": e["device_id"],
                 "email": e["email"], "text": event_text(e["kind"], e["detail"])}
                for e in store.events(device_id or None, email or None,
                                      since, until, limit=500)]
        return render_template("users.html", rows=rows, devices=store.devices(),
                               mailboxes=store.mailboxes(), pc=device_id,
                               mailbox=email, day=day, days=cfg.event_days)

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
            "{name} blocked. Its company mailboxes are removed within a minute "
            "of it being online.")

    @app.post("/devices/<device_id>/edit")
    def device_edit(device_id):
        name = (request.form.get("name") or "").strip()[:80]
        note = (request.form.get("note") or "").strip()[:120]
        if not name:
            flash("Enter a name for the PC.", "bad")
            return redirect(url_for("device_page", device_id=device_id))

        def go(dev):
            store.rename_device(dev["id"], name, note)
            store.audit(actor(), "device_edited",
                        f"{dev['name']} -> {name}" + (f" ({note})" if note else ""))
        return _device_action(device_id, go, "Saved.")

    @app.post("/devices/<device_id>/delete")
    def device_delete(device_id):
        dev = store.device(device_id) or abort(404)
        if dev["status"] == "approved":
            flash("Block the PC first, and let it come online so its company "
                  "mailboxes are removed. Then remove it.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        store.delete_device(device_id)
        store.audit(actor(), "device_deleted", f"{dev['name']} ({dev['id']})")
        flash(f"{dev['name']} removed.", "ok")
        return redirect(url_for("dashboard"))

    @app.post("/devices/<device_id>/takeover")
    def device_takeover(device_id):
        dev = store.device(device_id) or abort(404)
        if dev["status"] != "approved":
            flash("Approve the PC first.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        rows = device_view(dev)["personal_rows"]
        if request.form.get("all"):
            wanted = {p["email"] for p in rows if not (
                p["takeover"] and p["takeover"]["status"] in ("pending", "ignored"))}
        else:
            wanted = {(request.form.get("email") or "").strip().lower()}
        asked = []
        for p in rows:
            if p["email"] in wanted and p["kind"] == "password":
                store.mark_takeover(dev["id"], p["email"], "pending")
                asked.append(p["email"])
        if not asked:
            flash("Nothing to take over: that account isn't on the PC, or it "
                  "signs in with Google or Microsoft (add those under "
                  "Mailboxes instead).", "bad")
        else:
            store.audit(actor(), "takeover_requested",
                        f"{', '.join(asked)} on {dev['name']}")
            flash(f"The person using {dev['name']} is asked to allow it within "
                  "a minute (when AMail is open). The result shows here.", "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/takeover/cancel")
    def device_takeover_cancel(device_id):
        dev = store.device(device_id) or abort(404)
        email = (request.form.get("email") or "").strip().lower()
        store.clear_takeover(dev["id"], email)
        store.audit(actor(), "takeover_cancelled", f"{email} on {dev['name']}")
        flash("Withdrawn.", "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/takeover/ignore")
    def device_takeover_ignore(device_id):
        dev = store.device(device_id) or abort(404)
        email = (request.form.get("email") or "").strip().lower()
        store.mark_takeover(dev["id"], email, "ignored")
        store.audit(actor(), "takeover_ignored", f"{email} on {dev['name']}")
        flash(f"{email} stays the user's own account on {dev['name']}.", "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/grant")
    def device_grant(device_id):
        dev = store.device(device_id) or abort(404)
        if dev["status"] != "approved":
            flash("Approve the PC before putting mailboxes on it.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        ids = []
        for raw in request.form.getlist("mailbox_id"):
            try:
                ids.append(int(raw))
            except ValueError:
                pass
        boxes = [store.mailbox(i) for i in ids]
        boxes = [m for m in boxes if m is not None]
        if not boxes:
            flash("Tick at least one mailbox.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        n = store.grant_many(dev["id"], [m["id"] for m in boxes])
        names = ", ".join(m["email"] for m in boxes[:5]) + (" …" if len(boxes) > 5 else "")
        store.audit(actor(), "signed_in", f"{names} on {dev['name']}"
                    + (f" ({len(boxes)} mailboxes)" if len(boxes) > 1 else ""))
        what = boxes[0]["email"] if len(boxes) == 1 else f"{n} mailbox(es)"
        flash(f"{what} will be signed in on {dev['name']} within a minute, as "
              "soon as the PC is online.", "ok")
        if request.form.get("next") == "mailbox":
            return redirect(url_for("mailbox_page", mailbox_id=boxes[0]["id"]))
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/ungrant-all")
    def device_ungrant_all(device_id):
        dev = store.device(device_id) or abort(404)
        n = store.revoke_all(dev["id"])
        store.audit(actor(), "signed_out_all", f"{n} mailboxes on {dev['name']}")
        flash(f"{n} mailbox(es) will be signed out of {dev['name']} within a "
              "minute, as soon as the PC is online.", "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/copy")
    def device_copy(device_id):
        dev = store.device(device_id) or abort(404)
        src = store.device(request.form.get("source") or "") or abort(404)
        if dev["status"] != "approved":
            flash("Approve the PC before putting mailboxes on it.", "bad")
            return redirect(url_for("device_page", device_id=device_id))
        n = store.grant_many(dev["id"], [m["id"] for m in
                                         store.granted_mailboxes(src["id"])])
        store.audit(actor(), "copied_mailboxes",
                    f"{n} from {src['name']} to {dev['name']}")
        flash(f"{n} mailbox(es) from {src['name']} will be signed in on "
              f"{dev['name']} within a minute, as soon as the PC is online.", "ok")
        return redirect(url_for("device_page", device_id=device_id))

    @app.post("/devices/<device_id>/ungrant")
    def device_ungrant(device_id):
        dev = store.device(device_id) or abort(404)
        mb = store.mailbox(form_int("mailbox_id")) or abort(404)
        store.revoke(dev["id"], mb["id"])
        store.audit(actor(), "signed_out", f"{mb['email']} on {dev['name']}")
        flash(f"{mb['email']} will be signed out of {dev['name']} within a "
              "minute, as soon as the PC is online.", "ok")
        if request.form.get("next") == "mailbox":
            return redirect(url_for("mailbox_page", mailbox_id=mb["id"]))
        return redirect(url_for("device_page", device_id=device_id))

    # ----- mailboxes -----

    def mailhost_defaults() -> dict:
        """Server settings remembered from the last import or add, so the
        next mailbox needs only its address and password."""
        d = {k: store.get_setting(f"mailhost_{k}") for k in MAILHOST_KEYS}
        d["imap_port"] = d["imap_port"] or 993
        d["smtp_port"] = d["smtp_port"] or 465
        return d

    def remember_mailhost(f: dict) -> None:
        for k in MAILHOST_KEYS:
            store.set_setting(f"mailhost_{k}", str(f[k]))

    def mailbox_listing(form: dict | None = None, status: int = 200):
        q = (request.args.get("q") or "").strip()[:80]
        only = request.args.get("only") == "none"
        try:
            page = max(1, form_int("page", request.args, 1))
        except ValueError:
            page = 1
        total = store.mailbox_count(q, only)
        pages = max(1, (total + PAGE - 1) // PAGE)
        page = min(page, pages)
        by_mb = store.grants_by_mailbox()
        rows = [{"m": m, "devices": by_mb.get(m["id"], [])}
                for m in store.mailboxes(q, PAGE, (page - 1) * PAGE, only)]
        return render_template(
            "mailboxes.html", rows=rows, form=form or mailhost_defaults(),
            host=mailhost_defaults(), q=q, only=only, page=page, pages=pages,
            total=total, all_count=store.mailbox_count(),
            none_count=store.mailbox_count(unassigned=True)), status

    @app.get("/mailboxes")
    def mailboxes():
        return mailbox_listing()

    @app.post("/mailboxes/import")
    def mailbox_import():
        f = {k: (request.form.get(k) or "").strip() for k in MAILHOST_KEYS}
        for k in ("imap_host", "smtp_host"):
            if not _HOST.match(f[k]):
                flash("Enter the IMAP and SMTP server names for these "
                      "mailboxes.", "bad")
                return redirect(url_for("mailboxes"))
        for k in ("imap_port", "smtp_port"):
            if not f[k].isdigit() or not 0 < int(f[k]) < 65536:
                flash("Ports must be numbers between 1 and 65535.", "bad")
                return redirect(url_for("mailboxes"))
            f[k] = int(f[k])
        entries, bad = parse_import(request.form.get("lines") or "")
        if not entries:
            flash("No usable lines. Use one mailbox per line: "
                  "email:password (or email,password,display name).", "bad")
            return redirect(url_for("mailboxes"))
        new = [e for e in entries if not store.mailbox_by_email(e["email"])]
        existing = len(entries) - len(new)
        if request.form.get("test") and new:
            e = new[0]
            err = mailbox_tester(e["email"], e["password"], f["imap_host"],
                                 f["imap_port"], f["smtp_host"], f["smtp_port"])
            if err:
                flash(f"Nothing imported. The first mailbox ({e['email']}) "
                      f"couldn't sign in with these settings: {err}", "bad")
                return redirect(url_for("mailboxes"))
        for e in new:
            mid = store.add_mailbox(e["email"], e["display_name"], f["imap_host"],
                                    f["imap_port"], f["smtp_host"], f["smtp_port"],
                                    b"pending")
            store.update_mailbox(mid, display_name=e["display_name"],
                                 imap_host=f["imap_host"], imap_port=f["imap_port"],
                                 smtp_host=f["smtp_host"], smtp_port=f["smtp_port"],
                                 password_enc=crypto.encrypt_stored(
                                     master_key, e["password"], f"mailbox:{mid}"))
        remember_mailhost(f)
        store.audit(actor(), "mailboxes_imported",
                    f"{len(new)} added, {existing} already there, {len(bad)} bad lines")
        msg = f"{len(new)} mailbox(es) added."
        if existing:
            msg += f" {existing} were already in the list and were left as they are."
        if bad:
            msg += (f" {len(bad)} line(s) could not be read: "
                    + "; ".join(bad[:5]) + (" …" if len(bad) > 5 else ""))
        flash(msg, "ok" if new else "bad")
        return redirect(url_for("mailboxes"))

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
            return mailbox_listing(form=f, status=400)
        remember_mailhost(f)
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
        flash(f"{f['email']} added. Now put it on a PC: pick one below.", "ok")
        return redirect(url_for("mailbox_page", mailbox_id=mid))

    # ----- Google / Microsoft sign-in (no mailbox password) -----

    @app.post("/mailboxes/oauth/start")
    def mailbox_oauth_start():
        provider = request.form.get("provider") or ""
        email = (request.form.get("email") or "").strip().lower()
        name = (request.form.get("display_name") or "").strip()[:120]
        renew = form_int("mailbox_id")
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
        flash("Saved. PCs with this mailbox pick up the change within a minute, "
              "as soon as they're online.", "ok")
        return redirect(url_for("mailbox_page", mailbox_id=mailbox_id))

    @app.post("/mailboxes/<int:mailbox_id>/delete")
    def mailbox_delete(mailbox_id):
        m = store.mailbox(mailbox_id) or abort(404)
        store.delete_mailbox(mailbox_id)
        store.audit(actor(), "mailbox_deleted", m["email"])
        flash(f"{m['email']} deleted. Every PC that had it is signed out within "
              "a minute, as soon as it's online.", "ok")
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
            flash("New join code made. The old setup code no longer works for "
                  "new PCs.", "ok")
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

    # ----- admins -----

    def own_password_ok() -> bool:
        row = store.get_admin(request.admin)
        return bool(row) and crypto.check_password(
            request.form.get("confirm") or "", row["pw_hash"])

    @app.get("/admins")
    def admins_page():
        return render_template("admins.html", admins=store.list_admins(),
                               me=request.admin)

    @app.post("/admins/add")
    def admin_add():
        name = (request.form.get("username") or "").strip()
        pw = request.form.get("password") or ""
        if not _USERNAME.match(name):
            flash("The username needs 2 to 40 letters, digits, dots, dashes or "
                  "underscores.", "bad")
        elif store.get_admin(name):
            flash(f"{name} is already an admin.", "bad")
        elif len(pw) < 12:
            flash("Use at least 12 characters for their password.", "bad")
        elif not own_password_ok():
            flash("Your own password is wrong. Type it to confirm adding an "
                  "admin.", "bad")
        else:
            store.set_admin(name, crypto.hash_password(pw))
            store.audit(actor(), "admin_added", name)
            flash(f"{name} added. They set up two-step sign-in the first time "
                  "they sign in.", "ok")
        return redirect(url_for("admins_page"))

    @app.post("/admins/remove")
    def admin_remove():
        name = (request.form.get("username") or "").strip()
        row = store.get_admin(name) if name else None
        if row is None:
            flash("That admin doesn't exist.", "bad")
        elif row["username"].lower() == (request.admin or "").lower():
            flash("You can't remove yourself. Ask another admin to.", "bad")
        elif store.admin_count() <= 1:
            flash("There has to be at least one admin.", "bad")
        elif not own_password_ok():
            flash("Your own password is wrong. Type it to confirm removing an "
                  "admin.", "bad")
        else:
            store.delete_admin(row["username"])
            store.audit(actor(), "admin_removed", row["username"])
            flash(f"{row['username']} removed and signed out.", "ok")
        return redirect(url_for("admins_page"))

    @app.post("/admins/reset-two-step")
    def admin_reset_two_step():
        name = (request.form.get("username") or "").strip()
        row = store.get_admin(name) if name else None
        if row is None:
            flash("That admin doesn't exist.", "bad")
        elif not own_password_ok():
            flash("Your own password is wrong. Type it to confirm the reset.",
                  "bad")
        else:
            store.set_totp(row["username"], "", 0)
            store.end_sessions(row["username"])
            store.audit(actor(), "admin_two_step_reset", row["username"])
            flash(f"{row['username']} sets up two-step sign-in again at their "
                  "next sign-in. Their open sessions were ended.", "ok")
            if row["username"].lower() == (request.admin or "").lower():
                session.clear()
                return redirect(url_for("login"))
        return redirect(url_for("admins_page"))

    @app.get("/audit")
    def audit_page():
        rows = [{"at": r["at"], "who": who_label(r["actor"]),
                 "what": action_label(r["action"]), "detail": r["detail"]}
                for r in store.audit_log()]
        return render_template("audit.html", rows=rows)

    return app
