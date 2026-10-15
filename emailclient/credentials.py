from __future__ import annotations

import keyring

from .config import APP_NAME


def set_password(email: str, password: str) -> None:
    keyring.set_password(APP_NAME, email, password)


def get_password(email: str) -> str | None:
    return keyring.get_password(APP_NAME, email)
