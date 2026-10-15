"""In-app feedback / complaint reporting.

The user writes what went wrong and attaches screenshots; this posts the lot to
feedback.php, where it shows up in the web admin panel. Identity (version,
device, license key) rides along so a report can be matched to a machine
without the user having to describe their setup.

Everything travels in the request body — never the query string — so nothing
lands in the server's access logs. See crashlog.py, which uses the same shape.
"""
from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid

from . import nethttp

FEEDBACK_URL = "https://amail.app/amail/feedback.php"

KINDS = (("issue", "Something is broken"),
         ("idea", "Idea / request"),
         ("question", "Question"),
         ("other", "Other"))

MAX_SHOTS = 5
MAX_SHOT_BYTES = 6 * 1024 * 1024
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _context() -> dict:
    """Version / device / key, best effort — a report goes out regardless."""
    try:
        from .crashlog import context
        return context()
    except Exception:
        return {"version": "?", "platform": "?", "device": "", "name": "",
                "key": ""}


def _multipart(fields: dict, files: list[tuple[str, str]]) -> tuple[bytes, str]:
    """Encode *fields* and *files* [(field, path)] as multipart/form-data."""
    boundary = "----AMail" + uuid.uuid4().hex
    sep = ("--" + boundary).encode()
    body = bytearray()
    for name, value in fields.items():
        body += sep + b"\r\n"
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += str(value).encode("utf-8") + b"\r\n"
    for name, path in files:
        filename = os.path.basename(path)
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        body += sep + b"\r\n"
        body += (f'Content-Disposition: form-data; name="{name}";'
                 f' filename="{filename}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode()
        body += data + b"\r\n"
    body += sep + b"--\r\n"
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def validate_shot(path: str) -> str:
    """Empty string if *path* is a usable screenshot, else why it is not."""
    if not os.path.isfile(path):
        return "That file is gone."
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
        return "Only PNG, JPG, GIF or WebP images can be attached."
    try:
        if os.path.getsize(path) > MAX_SHOT_BYTES:
            return "That image is larger than 6 MB."
    except OSError:
        return "That file could not be read."
    return ""


def send(message: str, shots: list[str] | None = None, kind: str = "issue",
         contact: str = "", timeout: int = 30) -> tuple[bool, str]:
    """LOCAL ONLY: no server communication."""
    return False, "Feedback reporting is disabled in this build."
