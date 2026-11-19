"""This PC's link to the organization's AMail admin backend.

The admin signs this PC in to (and out of) mailboxes from the admin panel. The
PC never learns those passwords in a form a user can see:

  • Joining: the admin's setup code gives the server address, the server's
    certificate fingerprint, its signing key and an enrollment code. The PC
    makes an X25519 key pair, keeps the private half in the OS keychain, and
    registers the public half. The admin approves the PC in the panel after
    comparing the check code AMail shows here.
  • Check-in (every ~30 s): the PC reports its accounts' status and gets back
    the full list of managed mailboxes it should hold. A password arrives
    sealed to this PC's key and to that mailbox's servers, and is stored
    sealed. It is opened in memory only when the mail code logs in, using the
    servers the account actually points at, so a server changed by editing
    local files can't open it (credentials.password_for_account).
  • Every answer carries the server's time, signed. Managed mailboxes stop
    working when the last signed time is over 7 days old (a sign-out can't
    reach a PC that never checks in); editing a local setting can't fake it.
  • Every request goes over HTTPS to a server whose certificate matches the
    pinned fingerprint (or, if the admin turned pinning off, a normally
    trusted certificate). The device token is never sent anywhere else.

Network calls are blocking: call them off the GUI thread. Nothing here raises
into the UI except enroll(), whose errors are meant for the user.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import logging
import os
import platform
import socket
import ssl
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SETUP_PREFIX = "AMAIL1."
GRANT_SALT = b"amail-grant-v1"
TIMEOUT = 15

# Organization servers this build will join, by certificate fingerprint
# (SHA-256 hex, as the admin panel's Settings page shows it). Empty: any
# server, with the user confirming its address. Put your server's fingerprint
# here before building a release, and a setup code for any other server is
# refused, so nobody can talk a user into joining a fake organization.
PINNED_SERVER_FINGERPRINTS: tuple[str, ...] = ()

_K_URL = "org_url"
_K_FP = "org_fp"
_K_SIGNPUB = "org_signpub"      # the server's Ed25519 key that signs its time
_K_DEVICE = "org_device_id"
_K_STATE = "org_state"          # pending | approved | revoked | "" (not joined)
_K_MANAGED = "org_managed"      # {email: {"version", "box", "cfg"}}
_K_LEASE = "org_lease"          # {"t": server time, "sig": signature}
_K_DECLINED = "org_declined"    # personal accounts the user kept personal
_K_HANDED = "org_handed"        # accounts handed over, awaiting the server's answer
_K_UNKNOWN = "org_unknown"      # {"t": first "unknown device" answer, "n": how many}
_S_TOKEN = "org:token"
_S_KEY = "org:key"

# A PC that hasn't heard from the server for this long stops using its managed
# mailboxes until it does.
LEASE_SECONDS = 7 * 24 * 3600
# A PC clock this far behind the server's last signed time is wrong, not just
# drifting. (Check-in time is in minutes; a few minutes of drift is normal.)
CLOCK_TOLERANCE = 24 * 3600
# "This PC was deleted" is acted on (every managed mailbox removed) only when
# the server itself has said so this many times over at least this long. A
# single 401, which a proxy or tunnel can produce, is a failed check-in.
UNKNOWN_CONFIRM_COUNT = 3
UNKNOWN_CONFIRM_SECONDS = 10 * 60


class OrgError(Exception):
    """A problem the user should see (bad code, server unreachable…)."""


# ----- storage (patched in tests) ---------------------------------------------

def _qs():
    from PySide6.QtCore import QSettings
    return QSettings("AMail", "ui")


def _get(key: str) -> str:
    return str(_qs().value(key) or "")


def _set(key: str, value: str) -> None:
    _qs().setValue(key, value)


def _secret_get(name: str) -> str | None:
    import keyring
    from .config import APP_NAME
    return keyring.get_password(APP_NAME, name)


def _secret_set(name: str, value: str | None) -> None:
    import keyring
    from .config import APP_NAME
    if value is None:
        try:
            keyring.delete_password(APP_NAME, name)
        except Exception:
            pass
    else:
        keyring.set_password(APP_NAME, name, value)


_lock = threading.RLock()


# ----- encoding helpers ---------------------------------------------------------

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def server_tag(imap_host: str, imap_port, smtp_host: str, smtp_port) -> str:
    """The servers a managed password may be used with (must match the
    backend's crypto.server_tag)."""
    return (f"{str(imap_host).strip().lower()}:{int(imap_port)}|"
            f"{str(smtp_host).strip().lower()}:{int(smtp_port)}")


# ----- state --------------------------------------------------------------------

def is_joined() -> bool:
    return bool(_get(_K_DEVICE)) and bool(_secret_get(_S_TOKEN))


def state() -> str:
    return _get(_K_STATE) if is_joined() else ""


def server_url() -> str:
    return _get(_K_URL)


def parse_setup_code(code: str) -> dict:
    code = "".join((code or "").split())
    if not code.startswith(SETUP_PREFIX):
        raise OrgError("That isn't an AMail setup code. Copy it again from "
                       "the admin panel's Settings page.")
    try:
        data = json.loads(_b64d(code[len(SETUP_PREFIX):]))
        url, fp, enroll = str(data["u"]), str(data.get("f") or ""), str(data["c"])
        signpub = _b64d(str(data["s"]))
    except Exception:
        raise OrgError("The setup code is incomplete. Copy all of it again.")
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname:
        raise OrgError("The setup code has no valid server address.")
    if fp and (len(fp) != 64 or any(c not in "0123456789abcdef" for c in fp)):
        raise OrgError("The setup code is damaged. Copy it again.")
    if len(signpub) != 32:
        raise OrgError("The setup code is damaged. Copy it again.")
    if PINNED_SERVER_FINGERPRINTS and fp not in PINNED_SERVER_FINGERPRINTS:
        raise OrgError("This setup code is for a server this copy of AMail "
                       "isn't allowed to join. Ask your administrator.")
    return {"url": url.rstrip("/"), "fp": fp, "code": enroll,
            "signpub": _b64e(signpub)}


# ----- HTTPS with a pinned certificate -----------------------------------------------

def _post(url: str, fp: str, path: str, body: dict,
          token: str | None = None) -> tuple[int, dict]:
    """POST JSON over HTTPS. With a fingerprint, the server must present exactly
    that certificate, checked before anything (the token) is sent."""
    p = urlparse(url)
    if p.scheme != "https":
        raise OrgError("The server address must start with https://")
    if fp:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE        # replaced by the pin below
    else:
        ctx = ssl.create_default_context()
    conn = http.client.HTTPSConnection(p.hostname, p.port or 443,
                                       timeout=TIMEOUT, context=ctx)
    try:
        conn.connect()
        if fp:
            der = conn.sock.getpeercert(binary_form=True) or b""
            got = hashlib.sha256(der).hexdigest()
            if not hmac.compare_digest(got, fp):
                raise OrgError("The server's certificate doesn't match the setup "
                               "code. Ask your administrator.")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=json.dumps(body).encode("utf-8"),
                     headers=headers)
        resp = conn.getresponse()
        raw = resp.read(256 * 1024)
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return resp.status, data if isinstance(data, dict) else {}
    finally:
        conn.close()


# ----- the signed lease ---------------------------------------------------------------

def _lease_ok(lease, device_id: str, signpub_b64: str) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        t = int(lease["t"])
        Ed25519PublicKey.from_public_bytes(_b64d(signpub_b64)).verify(
            _b64d(lease["sig"]), f"amail-lease-v1|{device_id}|{t}".encode("utf-8"))
        return True
    except Exception:
        return False


def _store_lease(lease) -> bool:
    """Keep *lease* if the server really signed it and it's newer."""
    if not _lease_ok(lease, _get(_K_DEVICE), _get(_K_SIGNPUB)):
        return False
    if int(lease["t"]) >= _lease_time():
        _set(_K_LEASE, json.dumps({"t": int(lease["t"]), "sig": lease["sig"]}))
    return True


def _lease_time() -> int:
    """The last server time this PC holds a valid signature for (0 if none).
    Checked on every read, so an edited setting counts for nothing."""
    try:
        lease = json.loads(_get(_K_LEASE) or "{}")
    except ValueError:
        return 0
    if not lease or not _lease_ok(lease, _get(_K_DEVICE), _get(_K_SIGNPUB)):
        return 0
    return int(lease["t"])


def lease_seconds() -> int:
    """The limit. AMAIL_ORG_LEASE_SECONDS can shorten it for testing, never
    lengthen it."""
    try:
        v = int(os.environ.get("AMAIL_ORG_LEASE_SECONDS", "") or LEASE_SECONDS)
    except ValueError:
        v = LEASE_SECONDS
    return max(60, min(v, LEASE_SECONDS))


def last_contact() -> float:
    return float(_lease_time())


def lease_expired(now: float | None = None) -> bool:
    """True when managed mailboxes must not be used: this PC is joined but its
    last signed server time is older than the limit (or missing, or forged).
    A clock set back to well before that time counts as expired too."""
    if not is_joined():
        return False
    last = _lease_time()
    now = time.time() if now is None else now
    return (last <= 0 or now < last - CLOCK_TOLERANCE
            or now - last > lease_seconds())


# ----- joining / leaving --------------------------------------------------------------

def _device_name() -> str:
    try:
        return socket.gethostname() or "PC"
    except Exception:
        return "PC"


def check_code() -> str:
    """The code the admin compares in the panel before approving this PC
    (must match the backend's crypto.check_code)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    priv_b64 = _secret_get(_S_KEY)
    if not priv_b64:
        return ""
    pub = X25519PrivateKey.from_private_bytes(_b64d(priv_b64)).public_key(
    ).public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    raw = base64.b32encode(hashlib.sha256(b"amail-device-check|" + pub)
                           .digest()).decode("ascii")[:8]
    return f"{raw[:4]}-{raw[4:]}"


def enroll(setup_code: str) -> str:
    """Join the organization. Returns the new state ('pending' until the admin
    approves). Raises OrgError with a message for the user."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    info = parse_setup_code(setup_code)
    key = X25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    priv = key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    try:
        status, data = _post(info["url"], info["fp"], "/api/v1/enroll", {
            "code": info["code"], "name": _device_name(),
            "os": f"{platform.system()} {platform.release()}".strip(),
            "public_key": _b64e(pub)})
    except OrgError:
        raise
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise OrgError(f"Couldn't reach the server ({e}). Check the PC is "
                       "online and the server is running.")
    if status == 403:
        raise OrgError("The server refused the setup code. Ask your "
                       "administrator for a new one.")
    if status == 429:
        raise OrgError("Too many attempts. Wait an hour and try again.")
    if status == 503:
        raise OrgError("The server isn't accepting new PCs right now. Ask your "
                       "administrator.")
    if status != 200 or not data.get("device_id") or not data.get("token"):
        raise OrgError(f"The server answered unexpectedly (HTTP {status}).")
    device_id = str(data["device_id"])
    if not _lease_ok(data.get("lease") or {}, device_id, info["signpub"]):
        raise OrgError("The server's answer wasn't signed with the key in the "
                       "setup code. Ask your administrator.")
    with _lock:
        leave()                       # drop any earlier membership first
        _secret_set(_S_KEY, _b64e(priv))
        _secret_set(_S_TOKEN, str(data["token"]))
        _set(_K_URL, info["url"])
        _set(_K_FP, info["fp"])
        _set(_K_SIGNPUB, info["signpub"])
        _set(_K_DEVICE, device_id)
        _set(_K_STATE, str(data.get("state") or "pending"))
        _store_lease(data["lease"])
    return state()


def leave() -> None:
    """Forget the organization: token, keys and every sealed password."""
    _set(_K_HANDED, "")
    _set(_K_UNKNOWN, "")
    with _lock:
        for e in list(_managed()):
            _drop_tokens(e)
        _secret_set(_S_TOKEN, None)
        _secret_set(_S_KEY, None)
        for k in (_K_URL, _K_FP, _K_SIGNPUB, _K_DEVICE, _K_STATE, _K_MANAGED,
                  _K_LEASE, _K_DECLINED):
            _set(k, "")


# ----- managed passwords -------------------------------------------------------------------

def _managed() -> dict:
    try:
        d = json.loads(_get(_K_MANAGED) or "{}")
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _save_managed(d: dict) -> None:
    _set(_K_MANAGED, json.dumps(d))


def managed_emails() -> set[str]:
    return set(_managed())


def is_managed(email: str) -> bool:
    return (email or "").lower() in _managed()


def held_version(email: str) -> int:
    return int(_managed().get((email or "").lower(), {}).get("version") or 0)


def store_grant(email: str, version: int, box: dict, cfg: dict) -> None:
    email = email.lower()
    with _lock:
        _drop_tokens(email)
        d = _managed()
        d[email] = {"version": int(version), "box": box, "cfg": cfg}
        _save_managed(d)


def forget(email: str) -> None:
    email = (email or "").lower()
    with _lock:
        _drop_tokens(email)
        d = _managed()
        d.pop(email, None)
        _save_managed(d)


def declined() -> set[str]:
    try:
        return set(json.loads(_get(_K_DECLINED) or "[]"))
    except ValueError:
        return set()


def set_declined(email: str, on: bool) -> None:
    d = declined()
    (d.add if on else d.discard)((email or "").lower())
    _set(_K_DECLINED, json.dumps(sorted(d)))


def handed() -> set[str]:
    """Accounts the user agreed to hand over to the organization. Their
    password was sent to the server; the managed copy arrives with a later
    answer, and is then applied without asking again."""
    try:
        return set(json.loads(_get(_K_HANDED) or "[]"))
    except ValueError:
        return set()


def set_handed(email: str, on: bool) -> None:
    d = handed()
    (d.add if on else d.discard)((email or "").lower())
    _set(_K_HANDED, json.dumps(sorted(d)))


def _open_box(device_id: str, email: str, version: int, box: dict,
              servers: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    priv_b64 = _secret_get(_S_KEY)
    if not priv_b64:
        raise OrgError("This PC's organization key is missing.")
    priv = X25519PrivateKey.from_private_bytes(_b64d(priv_b64))
    shared = priv.exchange(X25519PublicKey.from_public_bytes(_b64d(box["epk"])))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=GRANT_SALT,
               info=f"{device_id}|{email}".encode("utf-8")).derive(shared)
    aad = f"v2|{device_id}|{email}|{version}|{servers}".encode("utf-8")
    return AESGCM(key).decrypt(_b64d(box["n"]), _b64d(box["ct"]), aad).decode("utf-8")


def password_for(email: str, servers: str) -> str | None:
    """The managed password for *email*, opened in memory, for an account
    pointing at *servers* (see server_tag). None if this mailbox isn't
    managed, the 7-day limit has run out, or the servers aren't the ones the
    administrator set. Nothing is cached: it's opened per login."""
    email = (email or "").lower()
    with _lock:
        entry = _managed().get(email)
        if not entry or lease_expired():
            return None
        try:
            return _open_box(_get(_K_DEVICE), email,
                             int(entry.get("version") or 0), entry["box"], servers)
        except Exception:
            log.warning("org: the sealed password for %s didn't open for these "
                        "servers", email)
            return None


# ----- Google / Microsoft managed mailboxes ----------------------------------------

APP_PROVIDER = {"google": "gmail", "microsoft": "outlook", "password": "custom"}
# Password mailboxes at these hosts get AMail's provider for them, so the
# mail code uses the right folder names and sending rules (a Gmail app
# password is still a Gmail mailbox). "outlook" is never chosen here: in
# AMail it means Microsoft sign-in, which a password mailbox doesn't have.
_HOST_PROVIDER = (("gmail.com", "gmail"), ("googlemail.com", "gmail"),
                  ("yahoo.com", "yahoo"), ("zoho.com", "zoho"),
                  ("zoho.eu", "zoho"), ("zoho.in", "zoho"),
                  ("zohomail.com", "zoho"))


def app_provider(auth: str, imap_host: str = "") -> str:
    """AMail's provider name for a managed mailbox: decides which sign-in
    code and which folder names the mail code uses."""
    if auth != "password":
        return APP_PROVIDER[auth]
    host = (imap_host or "").lower().strip(".")
    for domain, provider in _HOST_PROVIDER:
        if host == domain or host.endswith("." + domain):
            return provider
    return "custom"
_TOKEN_URL = {
    "google": "https://oauth2.googleapis.com/token",
    "microsoft": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}
_MS_SCOPE = ("https://outlook.office365.com/IMAP.AccessAsUser.All "
             "https://outlook.office365.com/SMTP.Send offline_access")
# (email, version, servers) -> (access token, expires at). Access tokens only
# (an hour long), so mail doesn't refresh on every reconnect.
_access_cache: dict[tuple, tuple[str, float]] = {}


def auth_kind(email: str) -> str:
    """password | google | microsoft for a managed mailbox ("" if not managed)."""
    entry = _managed().get((email or "").lower())
    if not entry:
        return ""
    return str((entry.get("cfg") or {}).get("auth") or "password")


def _rotated_name(email: str, version: int) -> str:
    return f"org:rt:{email}:{version}"


def oauth_access_token(account) -> str | None:
    """An access token for a managed Google / Microsoft mailbox, from the
    sealed refresh token (opened only for the account's own servers, like a
    password). None if not possible: the caller treats it as a sign-in error."""
    email = (account.email or "").lower()
    entry = _managed().get(email)
    if not entry or lease_expired():
        return None
    cfg = entry.get("cfg") or {}
    kind = cfg.get("auth")
    if kind not in _TOKEN_URL:
        return None
    version = int(entry.get("version") or 0)
    servers = server_tag(account.imap_host, account.imap_port,
                         account.smtp_host, account.smtp_port)
    key = (email, version, servers)
    hit = _access_cache.get(key)
    if hit and hit[1] > time.time() + 60:
        return hit[0]
    try:
        sealed_rt = _open_box(_get(_K_DEVICE), email, version, entry["box"], servers)
    except Exception:
        log.warning("org: the sealed sign-in for %s didn't open for these servers",
                    email)
        return None
    # Microsoft hands out a new refresh token with each use; the newest one
    # is kept (for this version only) so the sign-in never runs out.
    rt = _secret_get(_rotated_name(email, version)) or sealed_rt
    client = cfg.get("oauth_client") or {}
    fields = {"grant_type": "refresh_token", "refresh_token": rt,
              "client_id": client.get("id", "")}
    if client.get("secret"):
        fields["client_secret"] = client["secret"]
    if kind == "microsoft":
        fields["scope"] = _MS_SCOPE
    payload = _token_request(_TOKEN_URL[kind], fields)
    if payload is None:
        return None     # the token service wasn't reached: nothing changes
    if payload.get("error") in _DEAD_TOKEN_ERRORS and rt != sealed_rt:
        # The kept one is really dead (not a blip): fall back to the one the
        # admin issued, and forget the dead one.
        _secret_set(_rotated_name(email, version), None)
        fields["refresh_token"] = sealed_rt
        payload = _token_request(_TOKEN_URL[kind], fields)
    if not payload or not payload.get("access_token"):
        return None
    if payload.get("refresh_token") and payload["refresh_token"] != sealed_rt:
        _secret_set(_rotated_name(email, version), payload["refresh_token"])
    token = payload["access_token"]
    _access_cache[key] = (token, time.time() + int(payload.get("expires_in") or 3600))
    return token


# The token service's answers that mean a refresh token is gone for good
# (revoked, expired, or for another client). Anything else is a passing
# problem: a timeout, a 5xx, a captive portal's page.
_DEAD_TOKEN_ERRORS = ("invalid_grant",)


def _token_request(url: str, fields: dict) -> dict | None:
    """The token service's answer. A refusal comes back as {"error": ...};
    None means it couldn't be reached or didn't answer properly, so nothing
    should be concluded about the refresh token."""
    import urllib.error
    import urllib.parse
    import urllib.request
    from . import nethttp
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type":
                                          "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20,
                                    context=nethttp.ssl_context()) as r:
            data = json.loads(r.read(64 * 1024) or b"{}")
            return data if isinstance(data, dict) else None
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            log.info("org: token service error HTTP %s", e.code)
            return None
        try:
            err = json.loads(e.read(4096) or b"{}").get("error")
        except (ValueError, AttributeError):
            err = None
        log.warning("org: token refresh refused: %s", err or e.code)
        return {"error": str(err or e.code)}
    except (OSError, ValueError) as e:
        log.info("org: token refresh failed: %s", e)
        return None


def _drop_tokens(email: str) -> None:
    email = (email or "").lower()
    for k in [k for k in _access_cache if k[0] == email]:
        _access_cache.pop(k, None)
    entry = _managed().get(email)
    if entry:
        _secret_set(_rotated_name(email, int(entry.get("version") or 0)), None)


def validate_box(email: str, version: int, box: dict, servers: str) -> bool:
    """True if this PC can open *box* for *servers* (checked before accepting
    a grant)."""
    try:
        _open_box(_get(_K_DEVICE), email.lower(), int(version), box, servers)
        return True
    except Exception:
        return False


# ----- check-in ------------------------------------------------------------------------------

def checkin(accounts: list[dict], app_version: str, handover: list | None = None,
            declined: list | None = None, events: list | None = None) -> dict | None:
    """Report this PC's accounts; returns the server's answer, or None if the
    server couldn't be reached or its answer wasn't properly signed.
    {"state": "unknown"} means the server no longer knows this PC (it was
    deleted in the panel)."""
    if not is_joined():
        return None
    token = _secret_get(_S_TOKEN) or ""
    body = {"app_version": app_version, "accounts": accounts}
    if handover:
        body["handover"] = handover
    if declined:
        body["declined"] = declined
    if events:
        body["events"] = events
    try:
        status, data = _post(_get(_K_URL), _get(_K_FP), "/api/v1/checkin",
                             body, token=token)
    except Exception as e:
        log.info("org: check-in failed: %s", e)
        return None
    if status == 401:
        return _unknown_answer(data)
    if status != 200 or data.get("state") not in ("pending", "approved", "revoked"):
        log.info("org: unexpected check-in answer HTTP %s", status)
        return None
    if not _store_lease(data.get("lease") or {}):
        log.warning("org: check-in answer not signed by the organization's key")
        return None
    _set(_K_UNKNOWN, "")
    if data.get("new_token"):
        _secret_set(_S_TOKEN, str(data["new_token"]))
    _set(_K_STATE, data["state"])
    return data


def _unknown_answer(data: dict) -> dict | None:
    """HTTP 401 on a check-in. The server says that when it no longer knows
    this PC (deleted in the panel), but a reverse proxy, tunnel or sign-in
    layer in front of it says it too, and acting on those would wipe every
    managed mailbox and its mail. So: only the server's own wording counts,
    and only once it has been repeated for a while (a proxy hiccup never
    lasts that long; the lease covers the case where it does). Until then
    it's just a failed check-in."""
    if data.get("error") != "unknown device":
        log.info("org: HTTP 401 without the server's answer; treated as unreachable")
        return None
    now = time.time()
    try:
        seen = json.loads(_get(_K_UNKNOWN) or "{}")
        first, n = float(seen["t"]), int(seen["n"])
    except (ValueError, TypeError, KeyError):
        first, n = now, 0
    n += 1
    if n >= UNKNOWN_CONFIRM_COUNT and now - first >= UNKNOWN_CONFIRM_SECONDS:
        return {"state": "unknown", "managed": []}
    _set(_K_UNKNOWN, json.dumps({"t": first, "n": n}))
    log.warning("org: the server doesn't know this PC (%d of %d, confirming)",
                n, UNKNOWN_CONFIRM_COUNT)
    return None
