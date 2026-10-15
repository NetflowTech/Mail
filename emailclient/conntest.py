"""Check that an account's server settings actually work, before saving them.

Finding out that a host, port or password is wrong by watching the sidebar dot
turn red minutes later is a poor way to set up an account. These helpers do the
same login the sync worker and the sender do, and report what happened in
words the user can act on.

Pure logic (no Qt) so it can be unit-tested and run off the GUI thread.
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
import socket
import ssl

from imapclient import IMAPClient

from . import credentials, google_oauth, nethttp, oauth

log = logging.getLogger(__name__)

TIMEOUT = 20


def _explain(exc: Exception) -> str:
    """Turn a connection failure into something a user can act on."""
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if isinstance(exc, socket.gaierror) or "name or service not known" in lowered \
            or "nodename nor servname" in lowered:
        return "Server name not found — check the server address."
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in lowered:
        return ("No answer from the server — check the port, or whether a "
                "firewall is blocking it.")
    if isinstance(exc, ssl.SSLError) or "ssl" in lowered or "certificate" in lowered:
        return f"Secure connection failed — {text}"
    if isinstance(exc, ConnectionRefusedError) or "refused" in lowered:
        return "Connection refused — the port is probably wrong."
    if "auth" in lowered or "login" in lowered or "credential" in lowered \
            or "password" in lowered:
        return f"Login rejected — {text}"
    return text


def _oauth_token(account) -> str | None:
    """The XOAUTH2 token for a browser-signed-in account, else None."""
    if account.provider == "outlook":
        return oauth.get_token(account.email)
    if account.provider == "gmail" and google_oauth.has_login(account.email):
        return google_oauth.get_token(account.email)
    return None


def _credentials(account, password: str | None):
    """(token, password) to try. Resolved BEFORE opening a socket — with
    nothing to log in with, connecting first would just stall on a timeout."""
    token = _oauth_token(account)
    if token:
        return token, None
    return None, (password or credentials.get_password(account.email) or "")


def check_imap(account, host: str, port: int,
               password: str | None = None) -> tuple[bool, str]:
    """Log in to IMAP exactly like the sync worker does. (ok, message)."""
    token, pw = _credentials(account, password)
    if not token and not pw:
        return False, "No password saved for this account yet."
    client = None
    try:
        client = IMAPClient(host, port=port, ssl=True,
                            ssl_context=nethttp.ssl_context(),
                            timeout=TIMEOUT)
        if token:
            client.oauth2_login(account.email, token)
        else:
            client.login(account.email, pw)
        client.select_folder("INBOX", readonly=True)
        return True, "IMAP login OK — inbox reachable."
    except imaplib.IMAP4.error as exc:
        return False, _explain(exc)
    except Exception as exc:  # noqa: BLE001
        return False, _explain(exc)
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:  # noqa: BLE001
                pass


def check_smtp(account, host: str, port: int,
               password: str | None = None) -> tuple[bool, str]:
    """Log in to SMTP exactly like the sender does — no mail is sent."""
    token, pw = _credentials(account, password)
    if not token and not pw:
        return False, "No password saved for this account yet."
    smtp = None
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT,
                                    context=nethttp.ssl_context())
            smtp.ehlo()
        else:
            smtp = smtplib.SMTP(host, port, timeout=TIMEOUT)
            smtp.ehlo()
            smtp.starttls(context=nethttp.ssl_context())
            smtp.ehlo()
        if token:
            authstr = f"user={account.email}\x01auth=Bearer {token}\x01\x01"
            smtp.auth("XOAUTH2", lambda challenge=None: authstr)
        else:
            smtp.login(account.email, pw)
        return True, "SMTP login OK — sending will work."
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"Login rejected — {exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) else exc.smtp_error}"
    except Exception as exc:  # noqa: BLE001
        return False, _explain(exc)
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass


def check_account(account, imap_host: str, imap_port: int,
                  smtp_host: str, smtp_port: int,
                  password: str | None = None) -> dict:
    """Run both checks. Returns {"imap": (ok, msg), "smtp": (ok, msg)}."""
    return {"imap": check_imap(account, imap_host, imap_port, password),
            "smtp": check_smtp(account, smtp_host, smtp_port, password)}
