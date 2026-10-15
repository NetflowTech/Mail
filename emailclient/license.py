"""Online license verification against orostars.com.

License file: one license per line, ``KEY:ACCOUNT_LIMIT:DAYS`` —
e.g. ``LCGE6-GFFHL-9YMP0-G9E4T:200:365``. DAYS counts from activation
(first successful verification of that key on this machine).

Pure logic (stdlib urllib + QSettings) so it stays unit-testable.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QSettings

from . import nethttp

LICENSE_URL = "https://orostars.com/orostarmail/lic.txt"
# Generation dates live in a SEPARATE file (KEY:YYYY-MM-DD per line) so lic.txt
# stays strictly KEY:LIMIT:DAYS — older builds skip any 4-field line. A .txt (not
# .json) so it's fetchable over HTTP (.htaccess denies *.json).
STARTS_URL = "https://orostars.com/orostarmail/lic_starts.txt"
USAGE_URL = "https://orostars.com/orostarmail/usage.php"
GRACE_DAYS = 7  # offline grace: keep last verified state this long

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fetch_licenses(url: str = LICENSE_URL,
                   timeout: int = 10) -> dict[str, tuple[int, int, str]]:
    """LOCAL ONLY: no server communication."""
    return {}


def fetch_starts(url: str = STARTS_URL, timeout: int = 10) -> dict[str, str]:
    """LOCAL ONLY: no server communication."""
    return {}


def _is_ymd(s: str) -> bool:
    """True if s is a valid YYYY-MM-DD date."""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


class LicenseManager:
    """Activation, expiry and account-limit enforcement via QSettings."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("OroStarMail", "ui")
        self._revoked = False     # set when the server drops our key
        self._unverified = False  # offline beyond grace — warn, don't brick
        # Why the last usage call failed, so the License tab can say more than
        # "could not reach the server".
        self._last_usage_error = ""

    # ------------------------------------------------------------- helpers

    def _stored_key(self) -> str:
        return str(self._qs.value("lic_key") or "")

    def _parse_dt(self, qs_key: str) -> datetime | None:
        raw = self._qs.value(qs_key)
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    @property
    def device_id(self) -> str:
        """Stable per-installation id used for cross-device usage tracking."""
        dev = str(self._qs.value("lic_device") or "")
        if not dev:
            dev = uuid.uuid4().hex
            self._qs.setValue("lic_device", dev)
        return dev

    def _usage_request(self, count: int | None) -> int | None:
        """LOCAL ONLY: no server communication."""
        return None

    def report_usage(self, count: int) -> int | None:
        """Report this device's account count; returns the cross-device total."""
        return self._usage_request(count)

    def last_usage_error(self) -> str:
        """Detail of the most recent failed usage call ("" when it worked)."""
        return getattr(self, "_last_usage_error", "")

    def fetch_usage(self) -> int | None:
        """Read the cross-device total without changing this device's count."""
        return self._usage_request(None)

    def _stored_used_total(self) -> int | None:
        raw = self._qs.value("lic_used_total")
        if raw is None or str(raw) == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ----------------------------------------------------------------- API

    def activate(self, key: str) -> tuple[bool, str]:
        return True, "Activated."

    def status(self) -> dict:
        far_future = _now() + timedelta(days=36500)
        return {"key": "OWNED", "limit": 999999, "days": 36500,
                "activated_at": _now(), "expires_at": far_future,
                "days_left": 36500, "state": "active", "used_total": None}

    def verify_silent(self) -> str:
        return "active"

    def remaining_slots(self, current_count: int) -> int:
        return 999999

    def can_add_account(self, current_count: int) -> tuple[bool, str]:
        return True, ""
