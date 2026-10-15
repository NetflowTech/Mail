"""CUSTOMER EDITION — send-approval / admin client, neutered.

The full build talks to an admin approval + fleet backend. This customer build
ships NONE of that: there is no approval queue, no admin login, and — most
importantly — it NEVER uploads an email account's password to any server.

Every public name the rest of the app imports is preserved so nothing breaks,
but the server/admin logic and endpoints are gone. The admin-only functions are
inert no-ops, `is_admin()` is always False (so the admin UI stays hidden), and
`cached_accounts()` is always empty (so outgoing mail is always sent directly,
never routed through an approval queue).
"""
from __future__ import annotations

import uuid

from PySide6.QtCore import QSettings

from . import google_oauth


# -- local identity helpers (no network) -------------------------------------

def _qs() -> QSettings:
    return QSettings("OroStarMail", "ui")


def _key() -> str:
    return str(_qs().value("lic_key") or "")


def _device() -> str:
    qs = _qs()
    dev = str(qs.value("lic_device") or "")
    if not dev:
        dev = uuid.uuid4().hex
        qs.setValue("lic_device", dev)
    return dev


def _device_name() -> str:
    try:
        import socket
        return socket.gethostname() or ""
    except Exception:
        return ""


def _post(*args, **kwargs):
    """CUSTOMER EDITION: no server communication."""
    return None


def is_password_account(account) -> bool:
    """True if this account sends with a stored password (not OAuth)."""
    if account.provider == "outlook":
        return False
    if account.provider == "gmail" and google_oauth.has_login(account.email):
        return False
    return True


# -- admin session: disabled (this build is never an admin) ------------------

def save_admin(*args, **kwargs) -> None:
    return


def clear_admin(*args, **kwargs) -> None:
    return


def admin_creds(*args, **kwargs):
    return None


def is_admin(*args, **kwargs) -> bool:
    return False


def admin_session_state(*args, **kwargs) -> str:
    return "none"


def verify_admin(*args, **kwargs) -> bool:
    return False


# -- approval queue: disabled (mail is always sent directly) -----------------

def fetch_accounts(*args, **kwargs) -> list[str]:
    return []


def cached_accounts(*args, **kwargs) -> list[str]:
    return []


def refresh_accounts_cache(*args, **kwargs) -> list[str]:
    return []


def get_accounts(*args, **kwargs) -> list[str]:
    return []


def mark_accounts(*args, **kwargs) -> bool:
    return False


def submit(*args, **kwargs):
    """CUSTOMER EDITION: no approval queue — callers fall back to direct send."""
    return None


def approve(*args, **kwargs) -> bool:
    return False


def reject(*args, **kwargs) -> bool:
    return False


def pending(*args, **kwargs) -> list:
    return []


def status(*args, **kwargs) -> list:
    return []


def sender_sent(*args, **kwargs) -> bool:
    return False


def mark_sent(*args, **kwargs) -> bool:
    return False


def fetch_attachment(*args, **kwargs):
    return None


def fetch_audit_log(*args, **kwargs) -> list:
    return []


# -- server settings: disabled (nothing is configured server-side here) ------

def get_settings(*args, **kwargs) -> dict:
    return {}


def cached_settings(*args, **kwargs) -> dict:
    return {}


def refresh_settings_cache(*args, **kwargs) -> dict:
    return {}


def set_cloud_send(*args, **kwargs) -> bool:
    return False


def set_lock_delete(*args, **kwargs) -> bool:
    return False


def set_send_from_sender(*args, **kwargs) -> bool:
    return False


def delete_locked(*args, **kwargs) -> bool:
    return False


# -- credentials: NEVER uploaded in the customer build -----------------------

def save_creds(*args, **kwargs) -> bool:
    """CUSTOMER EDITION: disabled. An email account's password is NEVER sent to
    any server by this build."""
    return False


def remove_creds(*args, **kwargs) -> bool:
    return False
