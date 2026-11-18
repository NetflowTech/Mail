"""All the cryptography the backend does, in one place.

  • Master key: 32 random bytes that encrypt every stored mailbox password
    (AES-256-GCM). On Windows the key file is itself encrypted with DPAPI,
    machine scope, so a copy of the data folder is useless on another machine.
  • Sealing a password for one PC: each PC enrolls with an X25519 public key.
    A password is sent as an ephemeral-X25519 + HKDF-SHA256 + AES-256-GCM box
    that only that PC's private key opens; the PC id, mailbox and version are
    bound in as associated data, so a box can't be replayed to another PC or
    mailbox.
  • Device tokens: random 32 bytes; only their SHA-256 is stored.
  • Admin passwords: scrypt.
  • A self-signed TLS certificate; PCs pin its SHA-256 fingerprint.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (X25519PrivateKey,
                                                              X25519PublicKey)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.x509.oid import NameOID

GRANT_SALT = b"amail-grant-v1"
SETUP_PREFIX = "AMAIL1."


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def b64d(s: str) -> bytes:
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ----- Windows DPAPI (machine scope) -----------------------------------------

def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    CRYPTPROTECT_LOCAL_MACHINE = 0x4
    flags = CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE
    # Both functions take (in, description, entropy, reserved, prompt, flags,
    # out); the description is an input for Protect and an output for
    # Unprotect, and unused here either way.
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(blob_in), None, None, None, None, flags,
            ctypes.byref(blob_out))
    if not ok:
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _read_protected(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw.startswith(b"DPAPI:"):
        return _dpapi(raw[6:], protect=False)
    if raw.startswith(b"RAW:"):
        return raw[4:]
    raise ValueError(f"{path} is not an AMail key file")


def _write_protected(path: Path, key: bytes) -> None:
    _write_private(path, b"DPAPI:" + _dpapi(key, protect=True)
                   if sys.platform == "win32" else b"RAW:" + key)


def load_or_create_master_key(path: Path) -> bytes:
    """The 32-byte key that encrypts stored mailbox passwords."""
    if path.exists():
        return _read_protected(path)
    key = secrets.token_bytes(32)
    save_master_key(path, key)
    return key


def save_master_key(path: Path, key: bytes) -> None:
    _write_protected(path, key)


def load_or_create_signing_key(path: Path) -> Ed25519PrivateKey:
    """The server's Ed25519 key. It signs the time in every check-in answer,
    so a PC can't fake "the server answered recently" by editing a setting."""
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(_read_protected(path))
    key = Ed25519PrivateKey.generate()
    _write_protected(path, key.private_bytes(serialization.Encoding.Raw,
                                             serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption()))
    return key


def signing_public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw,
                                         serialization.PublicFormat.Raw)


def lease_message(device_id: str, t: int) -> bytes:
    return f"amail-lease-v1|{device_id}|{int(t)}".encode("utf-8")


def sign_lease(key: Ed25519PrivateKey, device_id: str, t: float) -> dict:
    t = int(t)
    return {"t": t, "sig": b64e(key.sign(lease_message(device_id, t)))}


def load_or_create_random(path: Path, n: int = 32) -> bytes:
    if path.exists():
        return path.read_bytes()
    v = secrets.token_bytes(n)
    _write_private(path, v)
    return v


# ----- stored mailbox passwords -------------------------------------------------

def encrypt_stored(master: bytes, plaintext: str, aad: str) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(master).encrypt(nonce, plaintext.encode("utf-8"),
                                          aad.encode("utf-8"))


def decrypt_stored(master: bytes, blob: bytes, aad: str) -> str:
    return AESGCM(master).decrypt(blob[:12], blob[12:],
                                  aad.encode("utf-8")).decode("utf-8")


# ----- sealing a password for one PC ---------------------------------------------

def server_tag(imap_host: str, imap_port: int, smtp_host: str,
               smtp_port: int) -> str:
    """The servers a password may be used with, as bound into its seal."""
    return (f"{imap_host.strip().lower()}:{int(imap_port)}|"
            f"{smtp_host.strip().lower()}:{int(smtp_port)}")


def grant_aad(device_id: str, email: str, version: int, servers: str) -> bytes:
    # The servers are part of what the seal authenticates: the PC can only
    # open the password while its account points at exactly these servers, so
    # editing the server in a local file breaks the login instead of sending
    # the password somewhere else.
    return f"v2|{device_id}|{email.lower()}|{version}|{servers}".encode("utf-8")


def _grant_key(shared: bytes, device_id: str, email: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=GRANT_SALT,
                info=f"{device_id}|{email.lower()}".encode("utf-8")).derive(shared)


def seal_for_device(device_public: bytes, device_id: str, email: str,
                    version: int, secret: str, servers: str) -> dict:
    """Encrypt *secret* so only the PC holding the private half of
    *device_public* can read it, and only for these *servers*."""
    eph = X25519PrivateKey.generate()
    shared = eph.exchange(X25519PublicKey.from_public_bytes(device_public))
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_grant_key(shared, device_id, email)).encrypt(
        nonce, secret.encode("utf-8"),
        grant_aad(device_id, email, version, servers))
    epk = eph.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    return {"v": 2, "epk": b64e(epk), "n": b64e(nonce), "ct": b64e(ct)}


def open_sealed(device_private: X25519PrivateKey, device_id: str, email: str,
                version: int, box: dict, servers: str) -> str:
    """The PC side of seal_for_device (used by tests; the app has its own copy)."""
    shared = device_private.exchange(
        X25519PublicKey.from_public_bytes(b64d(box["epk"])))
    return AESGCM(_grant_key(shared, device_id, email)).decrypt(
        b64d(box["n"]), b64d(box["ct"]),
        grant_aad(device_id, email, version, servers)).decode("utf-8")


def check_code(device_public: bytes) -> str:
    """A short code derived from a PC's public key. AMail shows it on the PC
    and the panel shows it next to the request: matching codes mean the
    request really came from the PC in front of you."""
    digest = hashlib.sha256(b"amail-device-check|" + device_public).digest()
    raw = base64.b32encode(digest).decode("ascii")[:8]
    return f"{raw[:4]}-{raw[4:]}"


def valid_public_key(raw: bytes) -> bool:
    try:
        X25519PublicKey.from_public_bytes(raw)
        return len(raw) == 32
    except Exception:
        return False


# ----- tokens and passwords ---------------------------------------------------------

def new_token() -> str:
    return b64e(secrets.token_bytes(32))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_enroll_code() -> str:
    # 4 groups of 4 from an unambiguous alphabet: easy to read out over a call.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


_SCRYPT = dict(n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${b64e(salt)}${b64e(dk)}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, dk = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=b64d(salt), **_SCRYPT)
        return hmac.compare_digest(got, b64d(dk))
    except Exception:
        return False


# ----- TLS certificate ----------------------------------------------------------------

def ensure_certificate(cert_path: Path, key_path: Path,
                       extra_names: list[str] | None = None) -> None:
    """A self-signed certificate for both servers, made once. PCs don't rely
    on its names or dates: they pin its fingerprint."""
    if cert_path.exists() and key_path.exists():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname() or "amail-admin"
    names: list[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost")]
    for n in extra_names or []:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            names.append(x509.DNSName(n))
    names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AMail Admin")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(days=1))
            .not_valid_after(now + _dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .sign(key, hashes.SHA256()))
    _write_private(key_path, key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def certificate_fingerprint(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return cert.fingerprint(hashes.SHA256()).hex()


# ----- setup code ------------------------------------------------------------------------

def make_setup_code(url: str, fingerprint: str, enroll_code: str,
                    signing_pub: bytes) -> str:
    """One string the admin pastes into AMail: where the server is, how to
    recognise it (certificate and signing key), and the code that lets a new
    PC register."""
    payload = json.dumps({"u": url, "f": fingerprint, "c": enroll_code,
                          "s": b64e(signing_pub)},
                         separators=(",", ":")).encode("utf-8")
    return SETUP_PREFIX + b64e(payload)


# ----- two-step sign-in (TOTP, RFC 6238) ---------------------------------------------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def totp_at(secret: str, step: int) -> str:
    key = base64.b32decode(secret)
    mac = hmac.new(key, step.to_bytes(8, "big"), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (int.from_bytes(mac[off:off + 4], "big") & 0x7FFFFFFF) % 1_000_000
    return f"{n:06d}"


def totp_verify(secret: str, code: str, last_step: int,
                now: float | None = None) -> int | None:
    """The time step *code* belongs to (one step of clock drift either way),
    or None. A step at or before *last_step* is refused: a code works once."""
    import time as _time
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    now_step = int((_time.time() if now is None else now) // 30)
    for step in (now_step - 1, now_step, now_step + 1):
        if step > last_step and hmac.compare_digest(totp_at(secret, step), code):
            return step
    return None


def totp_uri(secret: str, username: str, issuer: str = "AMail Admin") -> str:
    from urllib.parse import quote
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}"
            f"?secret={secret}&issuer={quote(issuer)}&digits=6&period=30")
