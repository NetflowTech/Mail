from __future__ import annotations

import re

import keyring

from .config import APP_NAME

_APP_PASSWORD = re.compile(r"[A-Za-z]{4}(?:\s+[A-Za-z]{4}){3}")
_SPACES_ONLY_PROVIDERS = ("gmail.com", "googlemail.com", "google.com")


def clean_password(text: str, host: str = "") -> str:
    """The password as the user meant it.

    App passwords (Google, Yahoo, ...) are shown as four groups of four
    letters, and the spaces between them are display only; copied from a web
    page they also arrive as non-breaking spaces, which IMAP login can't send.
    Those lose all their spaces, as does anything for a Google server (Google
    only accepts app passwords there). Any other password is kept as typed,
    spaces included: only the line breaks and tabs a copy-paste adds at the
    ends are removed, and non-breaking spaces become normal spaces."""
    text = text or ""
    host = (host or "").lower()
    if _APP_PASSWORD.fullmatch(text.strip()) or any(
            host.endswith(d) for d in _SPACES_ONLY_PROVIDERS):
        return "".join(text.split())
    return text.replace("\u00a0", " ").strip("\r\n\t")


def set_password(email: str, password: str) -> None:
    # A mailbox the organization manages has its password set by the admin
    # panel only; nothing on this PC may replace it.
    from . import org
    if org.is_managed(email):
        return
    keyring.set_password(APP_NAME, email, password)


def get_password(email: str) -> str | None:
    """A password the user saved on this PC. Managed mailboxes have none here:
    their password is only handed out for a login, by password_for_account."""
    from . import org
    if org.is_managed(email):
        return None
    return keyring.get_password(APP_NAME, email)


def password_for_account(account, imap_host: str | None = None,
                         imap_port: int | None = None) -> str | None:
    """The password to log *account* in with. For a managed mailbox it opens
    only while the account's servers are exactly the ones the administrator
    set: a server changed by editing local files gets nothing.
    *imap_host*/*imap_port* override the account's (connection test)."""
    from . import org
    if org.is_managed(account.email):
        servers = org.server_tag(imap_host or account.imap_host,
                                 imap_port or account.imap_port,
                                 account.smtp_host, account.smtp_port)
        return org.password_for(account.email, servers)
    return keyring.get_password(APP_NAME, account.email)


def delete_stored_password(email: str) -> None:
    """Remove a password this PC stored itself (e.g. one the user typed for an
    account the organization now manages)."""
    try:
        keyring.delete_password(APP_NAME, email)
    except Exception:
        pass


def forget_account(email: str) -> None:
    """Remove everything this PC keeps to sign *email* in: its password and
    its Google or Microsoft sign-in. Called once the account is removed, so
    nothing usable stays in the system keychain. Never raises."""
    delete_stored_password(email)
    try:
        from . import google_oauth
        google_oauth.forget(email)
    except Exception:
        pass
    try:
        from . import oauth
        oauth.forget(email)
    except Exception:
        pass
