"""Google OAuth 2.0 (browser sign-in) for Gmail — stdlib only.

Loopback flow: open the system browser on Google's consent screen, catch the
redirect on a local 127.0.0.1 HTTP server, exchange the code for tokens.
The refresh token is stored in the OS keyring; access tokens are cached
in memory with their expiry.

Like our Microsoft login (oauth.py), this uses Thunderbird's public OAuth
client — the de-facto standard credential for open-source IMAP clients
(Google does not consider the "secret" of an installed app confidential;
Thunderbird ships these same values in its public source tree).
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import keyring

from .config import APP_NAME
from . import nethttp

log = logging.getLogger(__name__)

CLIENT_ID = "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com"
CLIENT_SECRET = "kSmqreRr0qwBWJgbf5Y-PjSU"
SCOPE = "https://mail.google.com/"

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
LOGIN_TIMEOUT_SECONDS = 180
_EXPIRY_MARGIN_SECONDS = 60   # refresh a bit before the real expiry

# email -> (access_token, expires_at). In-memory only: avoids one token
# refresh per IMAP reconnect. time.time()-based, app code — fine.
_token_cache: dict[str, tuple[str, float]] = {}

_DONE_HTML = (b"<!doctype html><html><body style='font-family:sans-serif;"
              b"text-align:center;padding-top:80px'>"
              b"<h2>Login complete &#10003;</h2>"
              b"<p>You can close this tab and return to OroStarMail.</p>"
              b"</body></html>")


def _refresh_key(email: str) -> str:
    return f"google-refresh:{email}"


def _auth_url(email: str, port: int, state: str) -> str:
    """Build the Google consent-screen URL (pure function, unit-tested)."""
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": f"http://127.0.0.1:{port}",
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": email,
        "state": state,
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _post_token(fields: dict) -> dict:
    """POST a form to Google's token endpoint; return the JSON dict.

    Google returns errors (e.g. invalid_grant) as HTTP 4xx with a JSON body —
    parse that body instead of raising so callers can inspect the error key.
    """
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=nethttp.ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            return {"error": f"HTTP {exc.code}", "error_description": str(exc)}


def _cache_access(email: str, payload: dict) -> str:
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    _token_cache[email] = (token,
                           time.time() + expires_in - _EXPIRY_MARGIN_SECONDS)
    return token


def _exchange_code(email: str, code: str, redirect_uri: str) -> str:
    """Exchange an authorization code; store refresh token; return access token."""
    payload = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri,
    })
    if "access_token" not in payload:
        raise RuntimeError(payload.get("error_description")
                           or payload.get("error", "Google login failed"))
    refresh = payload.get("refresh_token")
    if refresh:
        keyring.set_password(APP_NAME, _refresh_key(email), refresh)
    return _cache_access(email, payload)


def login_interactive(email: str) -> str:
    """Open the browser for Google login; return access token.

    Serves exactly one loopback request in the calling thread, with a
    socket-timeout loop bounded at LOGIN_TIMEOUT_SECONDS.
    """
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            query = urllib.parse.urlparse(self.path).query
            result.update({k: v[0] for k, v in
                           urllib.parse.parse_qs(query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML)

        def log_message(self, *args):  # silence default stderr logging
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.timeout = 1   # handle_request() wakes every second to check the deadline
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    try:
        webbrowser.open(_auth_url(email, port, state))
        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        while not result:
            if time.time() > deadline:
                raise RuntimeError("Google login timed out")
            server.handle_request()
    finally:
        server.server_close()

    if "error" in result:
        raise RuntimeError(result.get("error_description", result["error"]))
    if result.get("state") != state:
        raise RuntimeError("Google login failed: state mismatch")
    code = result.get("code")
    if not code:
        raise RuntimeError("Google login failed: no authorization code returned")
    return _exchange_code(email, code, redirect_uri)


def get_token(email: str) -> str | None:
    """Access token via cached value or refresh token; None if re-login needed."""
    cached = _token_cache.get(email)
    if cached and cached[1] > time.time():
        return cached[0]
    refresh = keyring.get_password(APP_NAME, _refresh_key(email))
    if not refresh:
        return None
    payload = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if payload.get("error") == "invalid_grant":
        # Refresh token revoked/expired — clear it so has_login() goes False.
        log.warning("google refresh token invalid for %s, clearing", email)
        try:
            keyring.delete_password(APP_NAME, _refresh_key(email))
        except Exception:
            pass
        _token_cache.pop(email, None)
        return None
    if "access_token" not in payload:
        log.error("google token refresh failed for %s: %s", email,
                  payload.get("error_description") or payload.get("error"))
        return None
    return _cache_access(email, payload)


def has_login(email: str) -> bool:
    """True if a Google refresh token is stored for this address."""
    return keyring.get_password(APP_NAME, _refresh_key(email)) is not None
