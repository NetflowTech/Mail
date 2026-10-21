from __future__ import annotations

import email
import email.policy
import json
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .models import EmailMessage


# --- Denial-of-service guards --------------------------------------------------
# The MIME parser runs on every message during sync, straight off the wire and
# before anything is displayed, so a crafted email can hang it, exhaust memory,
# or crash the sync worker with zero interaction from the user. These bounds are
# far above any legitimate email but far below the crafted payloads, and any
# message that trips one is replaced with a safe placeholder instead of parsed.
MAX_MESSAGE_BYTES = 50 * 1024 * 1024          # whole message (Gmail's own ceiling)
MAX_HEADER_BYTES = 256 * 1024                 # size of the header section
MAX_HEADER_PARAMS = 512                       # ';' in headers -> quadratic param parse
MAX_ENCODED_WORDS = 2048                      # '=?' encoded-words -> memory blow-up
MAX_TYPE_MARKERS = 1024                       # multipart/message nesting & part count
MAX_PARTS = 1024                              # attachment parts we will enumerate
MAX_TOTAL_ATTACHMENT_BYTES = 100 * 1024 * 1024  # cumulative decoded attachment bytes


def _structural_reject(raw: bytes) -> str | None:
    """Reason to refuse `raw` without parsing it, or None if it looks sane.

    Cheap O(n) byte scans that bound the three fuzzed failure modes before the
    expensive stdlib parser ever runs: oversized input, a header with tens of
    thousands of parameters (quadratic hang), tens of thousands of encoded-words
    (memory amplification), and deeply nested / part-flooded structures.
    """
    if len(raw) > MAX_MESSAGE_BYTES:
        return "oversized"
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        sep = raw.find(b"\n\n")
    header = raw if sep == -1 else raw[:sep]
    if len(header) > MAX_HEADER_BYTES:
        return "oversized headers"
    if header.count(b";") > MAX_HEADER_PARAMS:
        return "too many header parameters"
    if header.count(b"=?") > MAX_ENCODED_WORDS:
        return "too many encoded-words"
    lowered = raw.lower()
    if (lowered.count(b"multipart/") + lowered.count(b"message/rfc822")
            > MAX_TYPE_MARKERS):
        return "too deeply nested"
    return None


def _placeholder(account_id: int, folder: str, uid: int,
                 reason: str) -> EmailMessage:
    """A safe stand-in for a message we refused to parse.

    It carries the real folder/uid so the sync watermark still advances and the
    hostile message is never re-fetched and re-processed on reconnect.
    """
    return EmailMessage(
        account_id=account_id, folder=folder, uid=uid,
        subject="(unreadable message)", sender="", recipients="", cc="",
        date=datetime.now(timezone.utc).isoformat(),
        body_text=f"This message could not be displayed safely ({reason}) "
                  "and was skipped.",
        body_html="", attachments="", message_id="")


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
    """Parse raw RFC822 bytes into an EmailMessage. Never raises on bad input.

    Hostile input (oversized, parameter/encoded-word floods, deep nesting) is
    refused up front or caught, and replaced with a safe placeholder, so a
    single crafted email can never hang the parser, exhaust memory, or crash
    the caller. See the DoS guards above.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")

    reason = _structural_reject(raw)
    if reason is not None:
        return _placeholder(account_id, folder, uid, reason)

    try:
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        return _build_message(msg, account_id, folder, uid, attachment_dir)
    except Exception:
        # RecursionError on deep nesting, or anything else the stdlib throws on
        # malformed MIME: never let it propagate out of here.
        return _placeholder(account_id, folder, uid, "unreadable")


def _build_message(msg, account_id: int, folder: str, uid: int,
                   attachment_dir: Path | None) -> EmailMessage:
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
                if len(parts) >= MAX_PARTS:
                    break
                if (id(part) not in seen
                        and part.get_content_maintype() == "image"
                        and part.get("Content-ID")):
                    parts.append(part)
                    seen.add(id(part))
                    forced_inline.add(id(part))
        except Exception:
            pass
        # Cap how many parts we enumerate: a message with tens of thousands of
        # parts would otherwise churn CPU and fill the disk one write at a time.
        parts = parts[:MAX_PARTS]
        total_bytes = 0
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
                total_bytes += len(content)
                if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
                    # Stop before decoding gigabytes out of a crafted message.
                    break
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
