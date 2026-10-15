"""Export/import the whole account setup so a user can move every logged-in
account to another machine with one file — no re-adding one by one.

The file bundles each account's settings + group + its password and OAuth
tokens (which normally live only in the OS keychain), plus the Microsoft token
cache. Emails are NOT included — they re-sync from the servers after import.

Because it contains every password, the file is encrypted with a user
passphrase. To stay dependency-free (the ``cryptography`` package ships an
unsigned Rust DLL that some endpoint-security suites — e.g. ESET — block), the
encryption is built entirely on the Python standard library: keys are derived
with PBKDF2-HMAC-SHA256 (``hashlib``), and the payload is sealed with an
encrypt-then-MAC scheme using an HMAC-SHA256 keystream plus an HMAC-SHA256
authentication tag.
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

# OSMACCT2 = stdlib crypto (no cryptography/Rust dependency). Older OSMACCT1
# Fernet files are no longer readable.
MAGIC = b"OSMACCT2"
_PBKDF2_ITERS = 200_000
FILE_SUFFIX = ".osmacct"


def _derive(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """Return (enc_key, mac_key), each 32 bytes, from the passphrase + salt."""
    material = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt,
                                   _PBKDF2_ITERS, dklen=64)
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


def _encrypt(passphrase: str, salt: bytes, plaintext: bytes) -> bytes:
    enc_key, mac_key = _derive(passphrase, salt)
    nonce = os.urandom(16)
    ks = _keystream(enc_key, nonce, len(plaintext))
    ciphertext = bytes(p ^ k for p, k in zip(plaintext, ks))
    tag = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    return nonce + tag + ciphertext


def _decrypt(passphrase: str, salt: bytes, body: bytes) -> bytes:
    nonce, tag, ciphertext = body[:16], body[16:48], body[48:]
    enc_key, mac_key = _derive(passphrase, salt)
    expected = hmac.new(mac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("Wrong password (or the file is corrupted).")
    ks = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(c ^ k for c, k in zip(ciphertext, ks))


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
    salt = os.urandom(16)
    return MAGIC + b"\n" + salt + _encrypt(passphrase, salt, raw)


def import_accounts(db, blob: bytes, passphrase: str, max_add: int | None = None):
    """Decrypt *blob* and recreate accounts/groups/credentials.

    Returns (added: list[Account], skipped: list[str email]). Accounts whose
    email already exists are skipped. When *max_add* is given (the license's
    remaining slots), at most that many NEW accounts are imported and the rest
    are skipped — import must not let a license exceed its account limit.
    Raises ValueError on bad file / wrong passphrase.
    """
    if not blob.startswith(MAGIC + b"\n"):
        raise ValueError("This is not an AMail account backup file.")
    body = blob[len(MAGIC) + 1:]
    salt, sealed = body[:16], body[16:]
    raw = _decrypt(passphrase, salt, sealed)
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
