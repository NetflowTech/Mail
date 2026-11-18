"""Export/import the whole account setup so a user can move every logged-in
account to another machine with one file — no re-adding one by one.

The file bundles each account's settings + group + its password and OAuth
tokens (which normally live only in the OS keychain), plus the Microsoft token
cache. Emails are NOT included — they re-sync from the servers after import.

Because it contains every password, the file is encrypted with a user
passphrase: the key is derived with PBKDF2-HMAC-SHA256 and the payload is
sealed with AES-256-GCM (``cryptography``, already bundled for Microsoft
sign-in). Files from earlier versions, sealed with the previous standard-
library scheme (HMAC-SHA256 keystream, encrypt-then-MAC), still import.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct

import keyring

from . import credentials, fleet, google_oauth, oauth
from .config import APP_NAME
from .models import Account

# OSMACCT4 = AES-256-GCM, PBKDF2 round count stored in the file.
# OSMACCT3 (stdlib keystream + HMAC, round count stored) and OSMACCT2 (same,
# fixed 200,000 rounds) still import. OSMACCT1 Fernet files no longer do.
MAGIC = b"OSMACCT4"
_V3_MAGIC = b"OSMACCT3"
_LEGACY_MAGIC = b"OSMACCT2"
_LEGACY_ITERS = 200_000
# OWASP's current minimum for PBKDF2-HMAC-SHA256.
_PBKDF2_ITERS = 600_000
# A file naming an absurd count must not pin the CPU on import.
_MAX_ITERS = 10_000_000
FILE_SUFFIX = ".osmacct"


def _derive(passphrase: str, salt: bytes,
            iters: int = _PBKDF2_ITERS) -> tuple[bytes, bytes]:
    """Return (enc_key, mac_key), each 32 bytes, from the passphrase + salt."""
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                                   iters, dklen=64)
    return material[:32], material[32:]


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    """HMAC-SHA256 counter-mode keystream of at least *n* bytes."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        block = hmac.new(enc_key, nonce + struct.pack(">Q", counter),
                         hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:n])


def _encrypt(passphrase: str, salt: bytes, plaintext: bytes,
             iters: int = _PBKDF2_ITERS) -> bytes:
    enc_key, mac_key = _derive(passphrase, salt, iters)
    nonce = os.urandom(16)
    ks = _keystream(enc_key, nonce, len(plaintext))
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, ks))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + tag + ciphertext


def _decrypt(passphrase: str, salt: bytes, body: bytes,
             iters: int = _PBKDF2_ITERS) -> bytes:
    nonce, tag, ciphertext = body[:16], body[16:48], body[48:]
    enc_key, mac_key = _derive(passphrase, salt, iters)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Wrong password (or the file is corrupted).")
    ks = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(c ^ k for c, k in zip(ciphertext, ks))


def _aesgcm(key: bytes):
    """AES-256-GCM, with a clear message if the library can't be loaded (for
    example when security software blocks its DLL)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "AMail couldn't load its encryption component, so the accounts "
            "file can't be written or read on this PC. If security software "
            "blocked part of AMail, allow AMail's install folder and try "
            f"again. ({type(exc).__name__}: {exc})") from exc
    return AESGCM(key)


def _gcm_key(passphrase: str, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                               iters, dklen=32)


def seal(passphrase: str, raw: bytes) -> bytes:
    """Wrap *raw* in the current file format: magic, round count, salt, then
    a 12-byte nonce and the AES-256-GCM ciphertext+tag. The header is
    authenticated too, so the round count or salt can't be altered."""
    salt = os.urandom(16)
    header = MAGIC + b"\n" + struct.pack(">I", _PBKDF2_ITERS) + salt
    nonce = os.urandom(12)
    aes = _aesgcm(_gcm_key(passphrase, salt, _PBKDF2_ITERS))
    return header + nonce + aes.encrypt(nonce, raw, header)


def unseal(blob: bytes, passphrase: str) -> bytes:
    """Inverse of :func:`seal`; also reads the older OSMACCT3/OSMACCT2 files.
    Raises ValueError for a foreign file or a wrong passphrase."""
    if blob.startswith(MAGIC + b"\n"):
        body = blob[len(MAGIC) + 1:]
        if len(body) < 4 + 16 + 12 + 16:
            raise ValueError("This account backup file is truncated.")
        (iters,) = struct.unpack(">I", body[:4])
        if not 1 <= iters <= _MAX_ITERS:
            raise ValueError("This account backup file is corrupted.")
        salt, nonce, sealed = body[4:20], body[20:32], body[32:]
        header = blob[:len(MAGIC) + 1 + 20]
        aes = _aesgcm(_gcm_key(passphrase, salt, iters))
        from cryptography.exceptions import InvalidTag
        try:
            return aes.decrypt(nonce, sealed, header)
        except InvalidTag:
            raise ValueError("Wrong password (or the file is corrupted).") from None
    if blob.startswith(_V3_MAGIC + b"\n"):
        body = blob[len(_V3_MAGIC) + 1:]
        if len(body) < 4 + 16 + 48:
            raise ValueError("This account backup file is truncated.")
        (iters,) = struct.unpack(">I", body[:4])
        if not 1 <= iters <= _MAX_ITERS:
            raise ValueError("This account backup file is corrupted.")
        salt, sealed = body[4:20], body[20:]
    elif blob.startswith(_LEGACY_MAGIC + b"\n"):
        body = blob[len(_LEGACY_MAGIC) + 1:]
        iters, salt, sealed = _LEGACY_ITERS, body[:16], body[16:]
    else:
        raise ValueError("This is not an AMail account backup file.")
    return _decrypt(passphrase, salt, sealed, iters)


def export_accounts(db, passphrase: str) -> bytes:
    """Return an encrypted blob of all accounts/groups/credentials."""
    groups = {g["id"]: g for g in db.list_groups()}
    accts = []
    for a in db.list_accounts():
        g = groups.get(a.group_id)
        accts.append({
            "name": a.name, "email": a.email, "provider": a.provider,
            "imap_host": a.imap_host, "imap_port": a.imap_port,
            "smtp_host": a.smtp_host, "smtp_port": a.smtp_port,
            "color": a.color, "signature": a.signature,
            "signature_html": a.signature_html, "sync_depth": a.sync_depth,
            "attachments_only": a.attachments_only,
            "notify_enabled": a.notify_enabled,
            "group_name": g["name"] if g else None,
            "group_icon": (g["icon"] if g else "📁"),
            "password": credentials.get_password(a.email) or "",
            "google_refresh": keyring.get_password(
                APP_NAME, google_oauth._refresh_key(a.email)),
        })
    payload = {
        "version": 1,
        "accounts": accts,
        "msal_cache": keyring.get_password(APP_NAME, oauth._CACHE_KEY),
    }
    raw = json.dumps(payload).encode("utf-8")
    return seal(passphrase, raw)


def import_accounts(db, blob: bytes, passphrase: str, max_add: int | None = None):
    """Decrypt *blob* and recreate accounts/groups/credentials.

    Returns (added: list[Account], skipped: list[str email]). Accounts whose
    email already exists are skipped. When *max_add* is given (the license's
    remaining slots), at most that many NEW accounts are imported and the rest
    are skipped — import must not let a license exceed its account limit.
    Raises ValueError on bad file / wrong passphrase.
    """
    raw = unseal(blob, passphrase)
    payload = json.loads(raw.decode("utf-8"))

    if payload.get("msal_cache"):
        keyring.set_password(APP_NAME, oauth._CACHE_KEY, payload["msal_cache"])

    existing = {a.email.lower() for a in db.list_accounts()}
    groups = {g["name"]: g["id"] for g in db.list_groups()}
    added, skipped = [], []
    for e in payload.get("accounts", []):
        if e["email"].lower() in existing:
            skipped.append(e["email"])
            continue
        if fleet.is_blocked(e["email"]):
            # Removed license-wide by the admin — never re-add via import.
            skipped.append(e["email"])
            continue
        if max_add is not None and len(added) >= max_add:
            # License limit reached — skip the remaining accounts.
            skipped.append(e["email"])
            continue
        gid = None
        gname = e.get("group_name")
        if gname:
            if gname not in groups:
                groups[gname] = db.add_group(gname, icon=e.get("group_icon") or "📁")
            gid = groups[gname]
        acc = Account(
            id=None, name=e["name"], email=e["email"], provider=e["provider"],
            imap_host=e["imap_host"], imap_port=int(e["imap_port"]),
            smtp_host=e["smtp_host"], smtp_port=int(e["smtp_port"]),
            color=e["color"], sync_depth=e.get("sync_depth") or "50")
        acc.id = db.add_account(acc)
        if gid is not None:
            db.set_account_group(acc.id, gid)
            acc.group_id = gid
        db.update_account(acc.id,
                          signature=e.get("signature") or "",
                          signature_html=e.get("signature_html") or "",
                          attachments_only=bool(e.get("attachments_only")),
                          notify_enabled=bool(e.get("notify_enabled", True)))
        if e.get("password"):
            credentials.set_password(e["email"], e["password"])
        if e.get("google_refresh"):
            keyring.set_password(APP_NAME,
                                 google_oauth._refresh_key(e["email"]),
                                 e["google_refresh"])
        existing.add(e["email"].lower())
        added.append(acc)
    return added, skipped
