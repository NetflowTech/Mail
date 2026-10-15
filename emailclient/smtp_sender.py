"""SMTP sending utilities.

- build_mime: construct an EmailMessage ready to send.
- send: connect to SMTP and transmit.
- parse_rcpts: split comma/semicolon-separated address strings.
"""
from __future__ import annotations

import logging
import mimetypes
import smtplib
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from datetime import datetime, timezone
from os.path import basename

from . import credentials, google_oauth, nethttp, oauth

log = logging.getLogger(__name__)


def parse_rcpts(*fields: str) -> list[str]:
    """Split comma/semicolon-separated address strings, strip, drop empties."""
    result = []
    for field in fields:
        if not field:
            continue
        for part in field.replace(";", ",").split(","):
            addr = part.strip()
            if addr:
                result.append(addr)
    return result


def build_mime(account, to_addrs: str, cc: str, subject: str,
               body_text: str, attachments: list[str],
               body_html: str = "",
               inline_images: list[tuple[str, str]] | None = None) -> EmailMessage:
    """Build an EmailMessage from its parts.

    body_text is treated as the final message body (caller is responsible for
    any signature content; this function does NOT append account.signature).
    When body_html is non-empty, it is added as a multipart/alternative HTML
    part alongside the plain text.

    inline_images is a list of (cid, path) tuples; each becomes an image/png
    part related to the HTML part, referenced from the HTML via cid: URLs.
    Only used when body_html is non-empty.

    Missing attachment / inline-image files are skipped with a log.warning.
    """
    msg = EmailMessage()
    msg["From"] = f"{account.name} <{account.email}>"
    msg["To"] = to_addrs
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    # Stamp our own Message-ID. Without one, the provider's SMTP server and its
    # IMAP APPEND each mint a different id for the SAME email, so the Sent
    # folder ended up holding two server copies plus our local copy — three
    # rows for one sent mail, with nothing to tie them together.
    domain = account.email.rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
        if inline_images:
            html_part = msg.get_payload()[-1]
            for cid, path in inline_images:
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    log.warning(
                        "smtp_sender: inline image not found, skipping: %s", path)
                    continue
                html_part.add_related(data, maintype="image", subtype="png",
                                      cid=f"<{cid}>")

    for path in attachments:
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            log.warning("smtp_sender: attachment not found, skipping: %s", path)
            continue
        mime_type, _ = mimetypes.guess_type(path)
        if mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(data, maintype=maintype, subtype=subtype,
                           filename=basename(path))

    return msg


def send(account, msg: EmailMessage, all_rcpts: list[str]) -> None:
    """Connect to SMTP and send *msg* to *all_rcpts*.

    Port 465 → SMTP_SSL; anything else → SMTP + STARTTLS.
    Outlook provider → XOAUTH2 (token from oauth.get_token).
    Gmail with a stored Google login → XOAUTH2 (google_oauth.get_token).
    Others (incl. app-password gmail) → plain login with stored password.
    """
    host = account.smtp_host
    port = account.smtp_port

    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30,
                                context=nethttp.ssl_context())
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=nethttp.ssl_context())
        smtp.ehlo()

    try:
        if account.provider == "outlook":
            token = oauth.get_token(account.email)
            if token is None:
                raise RuntimeError("microsoft re-login required")
            authstr = f"user={account.email}\x01auth=Bearer {token}\x01\x01"
            smtp.auth("XOAUTH2", lambda challenge=None: authstr)
        elif account.provider == "gmail" and google_oauth.has_login(account.email):
            token = google_oauth.get_token(account.email)
            if token is None:
                raise RuntimeError("google re-login required")
            authstr = f"user={account.email}\x01auth=Bearer {token}\x01\x01"
            smtp.auth("XOAUTH2", lambda challenge=None: authstr)
        else:
            password = credentials.get_password(account.email) or ""
            smtp.login(account.email, password)

        smtp.send_message(msg, from_addr=account.email, to_addrs=all_rcpts)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
