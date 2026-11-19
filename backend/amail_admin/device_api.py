"""The API AMail PCs talk to (the port that may be reachable from outside).

POST /api/v1/enroll   {code, name, os, public_key}
                      -> {device_id, token, state, interval, lease}
POST /api/v1/checkin  Bearer token; {app_version, accounts:[...],
                                     handover:[...]?, declined:[...]?,
                                     events:[...]?}
                      -> {state, interval, managed:[...], lease, new_token?,
                          collect:[...]}

`collect` names accounts the user added on the PC that the admin wants to
take over. AMail asks the user; on a yes it sends the account's saved
password and servers in `handover` (the server stores the password encrypted
and the account becomes a company mailbox signed in on that PC), on a no it
sends `declined`. A handover is only accepted for an account the admin asked
for, so a PC can't push mailboxes into the panel on its own.

The check-in answer is declarative: `managed` is the full list of mailboxes
this PC should have right now. The PC adds what's new, updates what changed
and removes what's gone, so a PC that was offline for a week converges on its
first check-in back. A password is only included when the PC's copy is out of
date, sealed so that only this PC can open it, and only for the mailbox's own
servers. Every answer carries `lease`: the server's time, signed, which the PC
needs to keep using its managed mailboxes (see the app's 7-day limit).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import re
import secrets
import time

from flask import Flask, jsonify, request

from . import crypto
from .ratelimit import RateLimiter
from .store import Store

log = logging.getLogger(__name__)

MAX_NAME = 80
MAX_HANDOVER = 100                  # per check-in; the PC queues the rest
MAX_EVENTS = 500                    # per check-in
EVENT_DETAIL_KEYS = ("subject", "who", "name", "folder")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
MAX_ACCOUNTS_REPORTED = 200
MAX_PENDING = 50                    # waiting PCs; more are refused
PENDING_EXPIRY = 7 * 86400          # a request nobody approves goes away
TOKEN_ROTATE_AFTER = 30 * 86400     # PCs get a fresh token this often
TOKEN_RETRY_AFTER = 10 * 60         # ...and again if the fresh one wasn't taken up


def _clip(v, n: int) -> str:
    return str(v or "")[:n]


def create_device_app(store: Store, master_key: bytes, checkin_interval: int,
                      signing_key, limiter: RateLimiter | None = None,
                      trusted_proxies: tuple = (), oauth_clients=None,
                      event_retention: float = 90 * 86400) -> Flask:
    """*oauth_clients*: provider -> {"id", "secret"}, read per request (the
    admin can change them in Settings). *event_retention*: seconds the record
    of what users did is kept."""
    last_prune = [0.0]
    from . import oauth as _oauth
    oauth_clients = oauth_clients or (lambda: _oauth.DEFAULT_CLIENTS)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
    limiter = limiter or RateLimiter()

    def client_ip() -> str:
        """The caller's address. A forwarded address is believed only when the
        request came from a configured tunnel / proxy."""
        peer = request.remote_addr or ""
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if any(peer_ip in net for net in trusted_proxies):
            fwd = (request.headers.get("CF-Connecting-IP")
                   or request.headers.get("X-Forwarded-For", "").split(",")[-1])
            fwd = fwd.strip()
            try:
                return str(ipaddress.ip_address(fwd))
            except ValueError:
                pass
        return peer

    def lease_for(device_id: str) -> dict:
        return crypto.sign_lease(signing_key, device_id, time.time())

    @app.after_request
    def _headers(resp):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.errorhandler(404)
    @app.errorhandler(405)
    def _not_found(_e):
        return jsonify(error="not found"), 404

    @app.post("/api/v1/enroll")
    def enroll():
        ip = client_ip()
        if not limiter.allow(f"enroll:{ip}", limit=10, per_seconds=3600):
            return jsonify(error="too many attempts"), 429
        body = request.get_json(silent=True) or {}
        expected = store.get_setting("enroll_code")
        given = str(body.get("code") or "").strip().upper()
        if (not expected or store.get_setting("enroll_open", "1") != "1"
                or not hmac.compare_digest(given.encode(), expected.encode())):
            store.audit("device", "enroll_refused", f"ip={ip}")
            return jsonify(error="invalid enrollment code"), 403
        try:
            public_key = crypto.b64d(str(body.get("public_key") or ""))
        except Exception:
            public_key = b""
        if not crypto.valid_public_key(public_key):
            return jsonify(error="invalid public key"), 400
        # Requests nobody approved within a week go; a flood of new ones (a
        # leaked setup code) stops at a limit instead of filling the panel.
        store.prune_pending(time.time() - PENDING_EXPIRY)
        if store.pending_count() >= MAX_PENDING:
            store.audit("device", "enroll_refused_full", f"ip={ip}")
            return jsonify(error="too many PCs waiting for approval"), 503
        name = _clip(body.get("name"), MAX_NAME).strip() or "PC"
        device_id = secrets.token_hex(16)
        token = crypto.new_token()
        store.add_device(device_id, name, _clip(body.get("os"), MAX_NAME),
                         public_key, crypto.token_hash(token), ip)
        store.audit("device", "enrolled",
                    f"{name} ({device_id}) from {ip}, check code "
                    f"{crypto.check_code(public_key)}")
        return jsonify(device_id=device_id, token=token, state="pending",
                       interval=checkin_interval, lease=lease_for(device_id))

    def store_events(device, body: dict) -> None:
        """What the PC's user did with company mailboxes since last time.
        Only for mailboxes this server knows (the PC filters personal ones
        out; this makes sure), and only a few words each."""
        raw = body.get("events") or []
        if not isinstance(raw, list) or not raw:
            return
        known = {m["email"].lower() for m in store.mailboxes()}
        now = time.time()
        rows = []
        for ev in raw[:MAX_EVENTS]:
            if not isinstance(ev, dict):
                continue
            email = _clip(ev.get("email"), 254).strip().lower()
            kind = _clip(ev.get("kind"), 32).strip()
            if not kind or (email and email not in known) \
                    or (not email and not kind.startswith("app_")):
                continue
            try:
                at = float(ev.get("t"))
            except (TypeError, ValueError):
                at = now
            at = min(max(at, now - event_retention), now + 300)
            detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
            detail = {k: _clip(detail.get(k), 200) for k in EVENT_DETAIL_KEYS
                      if detail.get(k)}
            rows.append((at, email, kind, json.dumps(detail)))
        if rows:
            store.add_events(device["id"], rows)
        if now - last_prune[0] > 3600:
            last_prune[0] = now
            store.prune_events(now - event_retention)

    def handle_takeovers(device, body: dict) -> None:
        """Accounts the user agreed to hand over, and ones they declined."""
        pending = {t["email"] for t in store.takeovers_for(device["id"], "pending")}
        for h in (body.get("handover") or [])[:MAX_HANDOVER]:
            if not isinstance(h, dict):
                continue
            email = _clip(h.get("email"), 254).strip().lower()
            if email not in pending:
                continue            # only what the admin asked for
            existing = store.mailbox_by_email(email)
            if existing is not None:
                # Already a company mailbox (handed over from another PC, or
                # added by the admin): the PC's copy of the password isn't
                # needed, just sign this PC in to it.
                store.grant(device["id"], existing["id"])
                store.mark_takeover(device["id"], email, "done",
                                    "already a company mailbox")
                store.audit("device", "takeover_done",
                            f"{email} on {device['name']} (existing mailbox)")
                continue
            password = str(h.get("password") or "")[:512]
            imap_host = _clip(h.get("imap_host"), 253).strip()
            smtp_host = _clip(h.get("smtp_host"), 253).strip()
            try:
                imap_port, smtp_port = int(h.get("imap_port")), int(h.get("smtp_port"))
            except (TypeError, ValueError):
                imap_port = smtp_port = 0
            if (not password or not _HOST.match(imap_host) or not _HOST.match(smtp_host)
                    or not 0 < imap_port < 65536 or not 0 < smtp_port < 65536):
                store.mark_takeover(device["id"], email, "failed",
                                    "the PC sent unusable settings")
                continue
            name = _clip(h.get("display_name"), 120).strip()
            mid = store.add_mailbox(email, name, imap_host, imap_port, smtp_host,
                                    smtp_port, b"pending")
            store.update_mailbox(mid, display_name=name, imap_host=imap_host,
                                 imap_port=imap_port, smtp_host=smtp_host,
                                 smtp_port=smtp_port,
                                 password_enc=crypto.encrypt_stored(
                                     master_key, password, f"mailbox:{mid}"))
            store.grant(device["id"], mid)
            store.mark_takeover(device["id"], email, "done")
            store.audit("device", "takeover_done", f"{email} from {device['name']}")
        for d in (body.get("declined") or [])[:MAX_HANDOVER]:
            if not isinstance(d, dict):
                continue
            email = _clip(d.get("email"), 254).strip().lower()
            if email in pending:
                store.mark_takeover(device["id"], email, "declined",
                                    _clip(d.get("reason"), 120))
                store.audit("device", "takeover_declined",
                            f"{email} on {device['name']}")

    @app.post("/api/v1/checkin")
    def checkin():
        ip = client_ip()
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        th = crypto.token_hash(token) if token else ""
        device = store.device_by_token(th) if token else None
        if device is None:
            if not limiter.allow(f"badtoken:{ip}", limit=30, per_seconds=600):
                return jsonify(error="too many attempts"), 429
            return jsonify(error="unknown device"), 401
        # A PC checks in every ~30 s; far more than that is a stuck loop or a
        # stolen token being hammered.
        if not limiter.allow(f"device:{device['id']}", limit=20, per_seconds=60):
            return jsonify(error="too many check-ins"), 429

        body = request.get_json(silent=True) or {}
        reported = []
        held_versions: dict[str, int] = {}
        for a in (body.get("accounts") or [])[:MAX_ACCOUNTS_REPORTED]:
            if not isinstance(a, dict):
                continue
            email = _clip(a.get("email"), 254).strip().lower()
            if not email:
                continue
            entry = {"email": email,
                     "status": _clip(a.get("status"), 20),
                     "error": _clip(a.get("error"), 200),
                     "managed": bool(a.get("managed")),
                     "kind": _clip(a.get("kind"), 12) or "password"}
            reported.append(entry)
            if entry["managed"]:
                try:
                    held_versions[email] = int(a.get("version") or 0)
                except (TypeError, ValueError):
                    held_versions[email] = 0
        store.record_checkin(device["id"], ip, _clip(body.get("app_version"), 40),
                             reported)

        out: dict = {"interval": checkin_interval, "lease": lease_for(device["id"])}
        # Token rotation: the PC used its new token, so the old one can go; or
        # the current one is old, so issue a replacement (the old stays valid
        # until the PC has used the new one).
        issued = device["token_issued_at"] or 0
        if th == device["token_hash"] and device["prev_token_hash"]:
            store.retire_prev_token(device["id"])
        elif th == device["token_hash"] and time.time() - issued > TOKEN_ROTATE_AFTER:
            new = crypto.new_token()
            store.rotate_token(device["id"], crypto.token_hash(new))
            out["new_token"] = new
        elif th == device["prev_token_hash"] and time.time() - issued > TOKEN_RETRY_AFTER:
            # The PC is still on the old token: the answer carrying the new
            # one never got stored. Issue another (the old one stays valid
            # until the PC has used a new one), rather than leaving the old
            # token valid for ever and rotation stuck.
            new = crypto.new_token()
            store.reissue_token(device["id"], crypto.token_hash(new))
            out["new_token"] = new

        state = device["status"]
        if state != "approved":
            return jsonify(state=state, managed=[], **out)

        handle_takeovers(device, body)
        store_events(device, body)
        out["collect"] = sorted(t["email"] for t in
                                store.takeovers_for(device["id"], "pending"))

        managed = []
        for m in store.granted_mailboxes(device["id"]):
            email = m["email"].lower()
            item = {"email": email, "name": m["display_name"],
                    "imap_host": m["imap_host"], "imap_port": m["imap_port"],
                    "smtp_host": m["smtp_host"], "smtp_port": m["smtp_port"],
                    "version": m["version"], "auth": m["auth"]}
            if m["auth"] != "password":
                # The PC turns the refresh token into access tokens itself, with
                # the same app registration that issued it.
                import json as _json
                saved = store.get_setting(f"mbclient:{m['id']}")
                item["oauth_client"] = (_json.loads(saved) if saved
                                        else oauth_clients().get(m["auth"], {}))
            if held_versions.get(email) != m["version"]:
                password = crypto.decrypt_stored(master_key, m["password_enc"],
                                                 f"mailbox:{m['id']}")
                item["secret"] = crypto.seal_for_device(
                    device["public_key"], device["id"], email, m["version"],
                    password, crypto.server_tag(m["imap_host"], m["imap_port"],
                                                m["smtp_host"], m["smtp_port"]))
            managed.append(item)
        return jsonify(state="approved", managed=managed, **out)

    return app
