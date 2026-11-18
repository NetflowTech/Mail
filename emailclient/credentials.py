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
    keyring.set_password(APP_NAME, email, password)


def get_password(email: str) -> str | None:
    return keyring.get_password(APP_NAME, email)


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
