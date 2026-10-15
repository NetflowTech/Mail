from __future__ import annotations

import email
import email.policy
import json
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import EmailMessage


def _sanitize_filename(name: str, fallback: str) -> str:
    """Keep only a safe basename: strip path separators and NULs."""
    name = (name or "").replace("\x00", "")
    # Take the last path component for both separator styles.
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in (".", ".."):
        return fallback
    return name



def _nested_email_bytes(part, content) -> bytes | None:
    """Serialise an attached message/rfc822 part back to raw .eml bytes.

    Some servers (Zoho among them) send the attached mail base64-encoded. Python
    then hands back a message object with NO headers whose payload is still the
    encoded blob, and get_payload(decode=True) gives None — so the transfer
    encoding has to be undone here. Without this the saved .eml was a wall of
    base64 that reopened as an empty "(no subject)" message.

    Quoted-printable and 7bit parts arrive properly parsed, so they take the
    plain serialisation path below; "no headers" is what tells the two apart.
    """
    if hasattr(content, "keys") and not list(content.keys()):
        cte = (part.get("Content-Transfer-Encoding") or "").strip().lower()
        payload = content.get_payload()
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if hasattr(payload, "get_payload"):
            payload = payload.get_payload()
        if isinstance(payload, str) and payload.strip():
            try:
                if cte == "base64":
                    import base64
                    return base64.b64decode(payload, validate=False)
                if cte == "quoted-printable":
                    import quopri
                    return quopri.decodestring(payload.encode("utf-8", "replace"))
                return payload.encode("utf-8", "replace")
            except Exception:
                pass  # fall through to the plain serialisation below
    for attr in ("as_bytes", "as_string"):
        fn = getattr(content, attr, None)
        if fn is None:
            continue
        try:
            raw = fn()
        except Exception:
            continue
        if isinstance(raw, str):
            raw = raw.encode("utf-8", "replace")
        if isinstance(raw, bytes) and raw:
            return raw
    return None


def _nested_email_name(part) -> str:
    """Filename for an attached email that carried none.

    The subject is what the user recognises, and the .eml suffix is what makes
    the reader open it AS an email instead of as a text blob.
    """
    subject = ""
    try:
        inner = part.get_payload(0) if part.is_multipart() else None
        subject = str((inner or part).get("Subject") or "")
    except Exception:
        subject = ""
    subject = subject.strip()
    from .eml import safe_filename
    return safe_filename(subject or "Attached message")


def parse_message(raw: bytes, account_id: int, folder: str, uid: int,
                  attachment_dir: Path | None = None) -> EmailMessage:
    """Parse raw RFC822 bytes into an EmailMessage. Never raises on bad input."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    msg = email.message_from_bytes(raw, policy=email.policy.default)

    subject = (msg.get("Subject") or "").strip() or "(no subject)"
    sender = str(msg.get("From", ""))
    recipients = str(msg.get("To", ""))
    cc = str(msg.get("Cc", ""))
    # Identity of the mail itself — the only reliable way to tell that the
    # local sent copy and the server's copy (or two server copies) are one and
    # the same email. See Database.save_message.
    message_id = str(msg.get("Message-ID", "") or "").strip()

    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Normalize to UTC so the unified inbox sorts correctly across
        # accounts/timezones (the DB orders lexicographically on this column).
        date_iso = dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        date_iso = datetime.now(timezone.utc).isoformat()

    body_text, body_html = "", ""
    try:
        plain = msg.get_body(preferencelist=("plain",))
        if plain:
            body_text = plain.get_content()
        html = msg.get_body(preferencelist=("html",))
        if html:
            body_html = html.get_content()
    except Exception:
        pass  # malformed MIME: keep whatever we extracted

    attachments = ""
    if attachment_dir is not None:
        items = []
        try:
            parts = list(msg.iter_attachments())
        except Exception:
            parts = []
        seen = {id(p) for p in parts}
        forced_inline = set()  # parts we KNOW are inline images (added below)
        # Inline cid-referenced images often lack a Content-Disposition:
        # attachment header, so iter_attachments() misses them. Walk all
        # parts and pick up image/* parts carrying a Content-ID.
        try:
            for part in msg.walk():
                if (id(part) not in seen
                        and part.get_content_maintype() == "image"
                        and part.get("Content-ID")):
                    parts.append(part)
                    seen.add(id(part))
                    forced_inline.add(id(part))
        except Exception:
            pass
        for i, part in enumerate(parts):
            try:
                filename = part.get_filename() or f"attachment-{i}"
                content = part.get_content()
                if isinstance(content, str):
                    content = content.encode("utf-8")
                elif not isinstance(content, bytes):
                    # message/rfc822: get_content() hands back a PARSED message,
                    # not bytes. Serialise it back to .eml — skipping it used to
                    # drop an attached email from the Sent copy entirely.
                    content = _nested_email_bytes(part, content)
                    if content is None:
                        continue
                    if not part.get_filename():
                        filename = _nested_email_name(part)
                cid = (part.get("Content-ID") or "").strip().strip("<>")
                disp = (part.get_content_disposition() or "").lower()
                # A part is INLINE (embedded in the body, no chip) only if it is
                # actually referenced from the HTML via cid:, or explicitly
                # Content-Disposition: inline. A Content-ID alone does NOT make it
                # inline — some servers stamp one onto real attachments, which
                # used to hide PDFs/spreadsheets from the attachment list.
                inline = (id(part) in forced_inline
                          or (bool(cid) and cid in (body_html or ""))
                          or (disp == "inline" and cid
                              and cid in (body_html or "")))
                safe = _sanitize_filename(filename, f"attachment-{i}")
                dest_dir = attachment_dir / uuid.uuid4().hex
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / safe
                dest.write_bytes(content)
                items.append({"name": filename, "path": str(dest),
                              "size": len(content), "cid": cid,
                              "inline": bool(inline)})
            except Exception:
                continue  # one bad part must not lose the message
        if items:
            attachments = json.dumps(items)

    return EmailMessage(account_id=account_id, folder=folder, uid=uid,
                        subject=subject, sender=sender, recipients=recipients,
                        cc=cc, date=date_iso, body_text=body_text,
                        body_html=body_html, attachments=attachments,
                        message_id=message_id)
