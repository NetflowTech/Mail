"""The API AMail PCs talk to (the port that may be reachable from outside).

POST /api/v1/enroll   {code, name, os, public_key}
                      -> {device_id, token, state, interval, lease}
POST /api/v1/checkin  Bearer token; {app_version, accounts:[...]}
                      -> {state, interval, managed:[...], lease, new_token?}

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
import logging
import secrets
import time

from flask import Flask, jsonify, request

from . import crypto
from .ratelimit import RateLimiter
from .store import Store

log = logging.getLogger(__name__)

MAX_NAME = 80
MAX_ACCOUNTS_REPORTED = 200
MAX_PENDING = 50                    # waiting PCs; more are refused
PENDING_EXPIRY = 7 * 86400          # a request nobody approves goes away
TOKEN_ROTATE_AFTER = 30 * 86400     # PCs get a fresh token this often


def _clip(v, n: int) -> str:
    return str(v or "")[:n]


def create_device_app(store: Store, master_key: bytes, checkin_interval: int,
                      signing_key, limiter: RateLimiter | None = None,
                      trusted_proxies: tuple = (), oauth_clients=None) -> Flask:
    """*oauth_clients*: provider -> {"id", "secret"}, read per request (the
    admin can change them in Settings)."""
    from . import oauth as _oauth
    oauth_clients = oauth_clients or (lambda: _oauth.DEFAULT_CLIENTS)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
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
                     "managed": bool(a.get("managed"))}
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
        if th == device["token_hash"] and device["prev_token_hash"]:
            store.retire_prev_token(device["id"])
        elif (th == device["token_hash"]
              and time.time() - (device["token_issued_at"] or 0) > TOKEN_ROTATE_AFTER):
            new = crypto.new_token()
            store.rotate_token(device["id"], crypto.token_hash(new))
            out["new_token"] = new

        state = device["status"]
        if state != "approved":
            return jsonify(state=state, managed=[], **out)

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
