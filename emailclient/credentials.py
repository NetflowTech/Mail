from __future__ import annotations

import keyring

from .config import APP_NAME


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
