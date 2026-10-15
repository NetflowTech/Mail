"""Turn a stored message into a real .eml file, and back again.

This is what "copy an email and paste it into another email" needs: Outlook,
Gmail and every other client understand an attached message/rfc822 file, and
double-clicking it opens the mail. We produce exactly that file, so the copy
travels to any client — and we can open the ones other people send us.
"""
from __future__ import annotations

import json
import logging
import os
import re
from email.message import EmailMessage as MimeMessage
from email.utils import format_datetime, parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path

from .models import EmailMessage

log = logging.getLogger(__name__)

EML_SUFFIX = ".eml"
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_MAX_NAME = 64


def is_eml(path: str) -> bool:
    """True for a file we can open as an email."""
    return str(path or "").lower().endswith((EML_SUFFIX, ".msg.eml"))


def safe_filename(subject: str) -> str:
    """A filesystem-safe .eml name built from the subject."""
    name = _UNSAFE_NAME.sub("_", (subject or "").strip()) or "message"
    name = name.strip(". ")[:_MAX_NAME] or "message"
    return f"{name}{EML_SUFFIX}"


def _attachment_items(attachments_json: str | None) -> list[dict]:
    try:
        data = json.loads(attachments_json or "[]")
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, dict):          # {"files": [...], "inline": [...]}
        items: list[dict] = []
        for path in data.get("files", []) or []:
            items.append({"path": str(path)})
        for img in data.get("inline", []) or []:
            if isinstance(img, dict):
                items.append({"path": str(img.get("path", "")),
                              "cid": str(img.get("cid", ""))})
        return items
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, str):
                out.append({"path": item})
        return out
    return []


def build_eml(msg: EmailMessage) -> bytes:
    """Serialise a stored message to RFC 822 bytes.

    Body, headers and attachments are carried over; inline images keep their
    Content-IDs so the pasted copy still renders its signature images.
    """
    mime = MimeMessage()
    mime["From"] = msg.sender or ""
    mime["To"] = msg.recipients or ""
    if msg.cc:
        mime["Cc"] = msg.cc
    mime["Subject"] = msg.subject or ""
    try:
        dt = datetime.fromisoformat((msg.date or "").strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    mime["Date"] = format_datetime(dt)
    if getattr(msg, "message_id", ""):
        mime["Message-ID"] = msg.message_id

    mime.set_content(msg.body_text or "")
    if msg.body_html:
        mime.add_alternative(msg.body_html, subtype="html")

    for item in _attachment_items(msg.attachments):
        path = item.get("path", "")
        if not path or not os.path.isfile(path):
            continue                    # cache cleared — skip, never fail
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            log.warning("eml: cannot read attachment %s", path)
            continue
        cid = str(item.get("cid", ""))
        name = os.path.basename(path)
        kwargs = {"maintype": "application", "subtype": "octet-stream",
                  "filename": name}
        if cid:
            kwargs["cid"] = f"<{cid}>" if not cid.startswith("<") else cid
        mime.add_attachment(data, **kwargs)
    return mime.as_bytes()


def write_eml(msg: EmailMessage, directory: Path | str) -> str:
    """Write *msg* as an .eml inside *directory*; returns the file path.

    A name collision just gets a counter — copying two mails with the same
    subject must not overwrite the first one.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    base = safe_filename(msg.subject)
    path = directory / base
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists():
        path = directory / f"{stem} ({n}){suffix}"
        n += 1
    path.write_bytes(build_eml(msg))
    return str(path)


def read_eml(path: str, account_id: int = 0, folder: str = "INBOX",
             attachment_dir: Path | None = None) -> EmailMessage | None:
    """Parse an .eml file into a message we can display. None if unreadable."""
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        log.warning("eml: cannot read %s: %s", path, exc)
        return None
    from .mailparse import parse_message
    try:
        return parse_message(raw, account_id, folder, uid=0,
                             attachment_dir=attachment_dir)
    except Exception:  # noqa: BLE001
        log.exception("eml: could not parse %s", path)
        return None


def eml_paths_from_mime(mime) -> list[str]:
    """The .eml files carried by a QMimeData (clipboard or drag), if any.

    Outlook and Explorer put the file on the clipboard as a URL; that is what
    we look for, so a paste works whether the copy came from us or from
    another mail client.
    """
    out: list[str] = []
    if mime is None or not mime.hasUrls():
        return out
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if is_eml(path) and os.path.isfile(path):
            out.append(path)
    return out
