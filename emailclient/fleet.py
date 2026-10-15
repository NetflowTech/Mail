"""Client for cross-device fleet management (fleet.php on amail.app).

Mirrors approval.py: stdlib urllib + QSettings, no third-party deps, every call
fails soft (network/None results never raise into the UI). Tenant is the license
key; the per-install device id and admin credentials are reused from approval.py
so the admin device stays the admin.

Concepts:
  - heartbeat(): this device POSTs a snapshot of its accounts + statuses and
    receives the license-wide blocklist plus any pending pause/resume commands.
  - admin_list()/admin_command()/unblock(): admin-only fleet control.
  - the blocklist is cached locally (QSettings) so account-add and worker-start
    can refuse a blocked email even between heartbeats / while offline.
"""
from __future__ import annotations

import json
import logging
import socket
import urllib.parse
import urllib.request

from PySide6.QtCore import QSettings

from . import approval, nethttp

FLEET_URL = "https://amail.app/amail/fleet.php"
_BLOCKLIST_KEY = "fleet_blocklist"
_CAN_EXPORT_KEY = "fleet_can_export"

log = logging.getLogger(__name__)


def _qs() -> QSettings:
    return QSettings("AMail", "ui")


def _key() -> str:
    return str(_qs().value("lic_key") or "")


def _device() -> str:
    # Reuse approval.py's device id (same QSettings key) so all endpoints agree.
    return approval._device()


def _device_name() -> str:
    try:
        return socket.gethostname() or "PC"
    except Exception:
        return "PC"


def _local_ip() -> str:
    """This machine's LAN/private IP (the address other devices on the same
    network would reach it at). No packets are actually sent — connecting a UDP
    socket just makes the OS pick the right outbound interface."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# -- low-level HTTP (mirrors approval._post; creds/data always go in the body,
#    never the URL/query string, so they never leak into server access logs) ---

_last_error = ""     # human-readable detail of the most recent _post failure


def last_error() -> str:
    """Why the last server call failed (timeout, DNS, TLS, HTTP status…).

    Support cases like "it just says 'could not reach the server' on this PC"
    are unanswerable without this — the underlying exception used to be dropped
    into a debug log nobody had enabled."""
    return _last_error


def _post(params: dict, fields: dict, timeout: int = 12) -> dict | None:
    """LOCAL ONLY: no server communication."""
    return None


# -- device side -------------------------------------------------------------

def heartbeat(accounts: list[dict], expires: str = "") -> dict | None:
    """LOCAL ONLY: no server communication."""
    return None


def ack(ids: list[str]) -> bool:
    """LOCAL ONLY: no server communication."""
    return True


# -- admin side (creds reused from approval.py) ------------------------------

# CUSTOMER EDITION: the fleet ADMIN operations below are removed. Only the
# client side (heartbeat / blocklist obey / license usage) remains — that is what
# lets the server enforce the account limit. These stubs keep the same names and
# return values so the rest of the app is unaffected; the admin UI is hidden.

def admin_list() -> dict | None:
    return None


def admin_command(target_device: str, email: str, action: str) -> bool:
    return False


def unblock(email: str) -> bool:
    return False


def email_devices(email: str, timeout: int = 6) -> dict | None:
    return None


def export_accounts() -> tuple[list[dict] | None, str]:
    return None, "forbidden"


def set_can_export(flag: bool) -> None:
    _qs().setValue(_CAN_EXPORT_KEY, bool(flag))


def can_export() -> bool:
    """Cached from the last heartbeat: does this device host an export-permitted
    email? Used to reveal the Export button in the License tab between heartbeats."""
    return str(_qs().value(_CAN_EXPORT_KEY) or "").lower() in ("true", "1")


# -- local blocklist cache (QSettings, lowercased) ---------------------------

def set_blocklist(emails: list[str]) -> None:
    _qs().setValue(_BLOCKLIST_KEY, json.dumps(sorted({str(e).lower() for e in emails})))


def blocked_emails() -> set[str]:
    try:
        return {str(e).lower()
                for e in json.loads(str(_qs().value(_BLOCKLIST_KEY) or "[]"))}
    except Exception:
        return set()


def is_blocked(email: str) -> bool:
    return str(email).lower() in blocked_emails()
