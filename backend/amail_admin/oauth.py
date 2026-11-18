"""Google / Microsoft sign-in for managed mailboxes (no mailbox password).

The admin signs the mailbox in once from the panel. Google and Microsoft only
let desktop mail apps return to a "loopback" address on the computer doing the
sign-in, so the flow is:

  1. the panel makes a sign-in link (PKCE, a fresh state value),
  2. the admin approves in their browser, which then tries to open
     http://127.0.0.1:<port>/?code=... on their own computer and shows
     "can't connect",
  3. the admin pastes that address back into the panel, which exchanges the
     code for tokens here on the server.

The refresh token is what PCs receive (sealed, like a password); AMail turns
it into short-lived access tokens itself. Defaults are the public desktop
clients AMail itself uses (Thunderbird's); an organization can register its
own and set them in Settings.
"""
from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import secrets
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

GOOGLE = "google"
MICROSOFT = "microsoft"

DEFAULT_CLIENTS = {
    GOOGLE: {"id": "406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com",
             "secret": "kSmqreRr0qwBWJgbf5Y-PjSU"},
    MICROSOFT: {"id": "9e5f94bc-e8a4-4e73-b8be-63364c29d753", "secret": ""},
}

PROVIDERS = {
    GOOGLE: {
        "label": "Google",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "revoke": "https://oauth2.googleapis.com/revoke",
        "scope": "https://mail.google.com/",
        "redirect_host": "127.0.0.1",
        "servers": ("imap.gmail.com", 993, "smtp.gmail.com", 465),
        "app_provider": "gmail",
    },
    MICROSOFT: {
        "label": "Microsoft",
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "revoke": "",
        "scope": ("https://outlook.office365.com/IMAP.AccessAsUser.All "
                  "https://outlook.office365.com/SMTP.Send offline_access"),
        "redirect_host": "localhost",
        "servers": ("outlook.office365.com", 993, "smtp.office365.com", 587),
        "app_provider": "outlook",
    },
}


class OAuthError(Exception):
    pass


def _ctx() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def start(provider: str, email: str, client: dict) -> dict:
    """A sign-in link plus what finish() needs to check the answer."""
    p = PROVIDERS[provider]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    state = secrets.token_urlsafe(24)
    port = secrets.randbelow(20000) + 40000
    redirect = f"http://{p['redirect_host']}:{port}/"
    params = {"client_id": client["id"], "redirect_uri": redirect,
              "response_type": "code", "scope": p["scope"], "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256",
              "login_hint": email}
    if provider == GOOGLE:
        params.update(access_type="offline", prompt="consent")
    else:
        params.update(prompt="select_account")
    return {"provider": provider, "email": email, "state": state,
            "verifier": verifier, "redirect": redirect,
            "url": p["auth"] + "?" + urllib.parse.urlencode(params)}


def code_from_pasted(pending: dict, pasted: str) -> str:
    """The authorization code in the address the browser landed on."""
    pasted = (pasted or "").strip()
    try:
        u = urllib.parse.urlparse(pasted)
        q = urllib.parse.parse_qs(u.query)
    except ValueError:
        raise OAuthError("That isn't the address from the browser.")
    if "error" in q:
        raise OAuthError(f"Sign-in was cancelled or refused: {q['error'][0]}")
    expected = urllib.parse.urlparse(pending["redirect"])
    if (u.hostname, u.port) != (expected.hostname, expected.port):
        raise OAuthError("That address is from a different sign-in. Start again.")
    if q.get("state", [""])[0] != pending["state"]:
        raise OAuthError("That address doesn't belong to this sign-in. Start again.")
    code = q.get("code", [""])[0]
    if not code:
        raise OAuthError("No sign-in code in that address. Copy the whole address.")
    return code


def _post(url: str, fields: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ctx()) as r:
            return json.loads(r.read(64 * 1024) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read(64 * 1024) or b"{}")
        except ValueError:
            body = {}
        raise OAuthError(body.get("error_description") or body.get("error")
                         or f"HTTP {e.code}")
    except (OSError, ValueError) as e:
        raise OAuthError(str(e))


def exchange(pending: dict, code: str, client: dict) -> dict:
    """Trade the code for tokens: {"refresh_token", "access_token"}."""
    p = PROVIDERS[pending["provider"]]
    fields = {"grant_type": "authorization_code", "code": code,
              "redirect_uri": pending["redirect"], "client_id": client["id"],
              "code_verifier": pending["verifier"]}
    if client.get("secret"):
        fields["client_secret"] = client["secret"]
    if pending["provider"] == MICROSOFT:
        fields["scope"] = p["scope"]
    tok = _post(p["token"], fields)
    if not tok.get("refresh_token") or not tok.get("access_token"):
        raise OAuthError("The sign-in didn't return a lasting token. Try again "
                         "(and allow offline access).")
    return tok


def check_mailbox(provider: str, email: str, access_token: str) -> None:
    """Log in to the mailbox with the new token: proves it belongs to *email*
    and can read mail. Raises OAuthError otherwise."""
    host, port = PROVIDERS[provider]["servers"][:2]
    auth = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    try:
        c = imaplib.IMAP4_SSL(host, port, ssl_context=_ctx(), timeout=20)
        try:
            c.authenticate("XOAUTH2", lambda _: auth.encode())
        finally:
            try:
                c.logout()
            except Exception:
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        raise OAuthError(f"Signed in, but {email}'s mailbox refused the token "
                         f"({e}). Was the right account chosen?")


def revoke(provider: str, refresh_token: str) -> None:
    """Best effort: make Google forget an old token (Microsoft has no
    per-token revoke for desktop apps; change the account password there)."""
    url = PROVIDERS[provider]["revoke"]
    if not url:
        return
    try:
        _post(url, {"token": refresh_token})
    except OAuthError:
        pass
