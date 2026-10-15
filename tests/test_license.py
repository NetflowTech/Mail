"""Tests for emailclient.license — stub verification (customer edition)."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone


class TestLicenseStubs:
    """Customer edition: all network calls are stubbed, license always active."""

    def test_fetch_licenses_returns_empty(self):
        from emailclient.license import fetch_licenses
        assert fetch_licenses() == {}

    def test_fetch_starts_returns_empty(self):
        from emailclient.license import fetch_starts
        assert fetch_starts() == {}

    def test_is_ymd_valid(self):
        from emailclient.license import _is_ymd
        assert _is_ymd("2024-01-15") is True

    def test_is_ymd_invalid(self):
        from emailclient.license import _is_ymd
        assert _is_ymd("15-01-2024") is False
        assert _is_ymd("not-a-date") is False
        assert _is_ymd("") is False
        assert _is_ymd(None) is False

    def test_is_ymd_valid_leap(self):
        from emailclient.license import _is_ymd
        assert _is_ymd("2024-02-29") is True

    def test_is_ymd_invalid_leap(self):
        from emailclient.license import _is_ymd
        assert _is_ymd("2023-02-29") is False


class TestLicenseManager:
    """LicenseManager always reports active with unlimited slots."""

    def _manager(self):
        from emailclient.license import LicenseManager
        from PySide6.QtCore import QSettings
        qs = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                       "AMail-test", "lic-test")
        return LicenseManager(qs=qs)

    def test_activate_returns_true(self):
        mgr = self._manager()
        ok, msg = mgr.activate("SOME-KEY")
        assert ok is True

    def test_status_state_active(self):
        mgr = self._manager()
        s = mgr.status()
        assert s["state"] == "active"

    def test_status_key_is_owned(self):
        mgr = self._manager()
        s = mgr.status()
        assert s["key"] == "OWNED"

    def test_status_limit_unlimited(self):
        mgr = self._manager()
        s = mgr.status()
        assert s["limit"] > 10000

    def test_status_expires_far_future(self):
        mgr = self._manager()
        s = mgr.status()
        assert s["days_left"] > 1000

    def test_status_has_timestamps(self):
        mgr = self._manager()
        s = mgr.status()
        assert isinstance(s["activated_at"], datetime)
        assert isinstance(s["expires_at"], datetime)

    def test_verify_silent_returns_active(self):
        mgr = self._manager()
        assert mgr.verify_silent() == "active"

    def test_remaining_slots_unlimited(self):
        mgr = self._manager()
        assert mgr.remaining_slots(50) > 10000

    def test_can_add_account_always_true(self):
        mgr = self._manager()
        ok, reason = mgr.can_add_account(0)
        assert ok is True
        assert reason == ""

    def test_can_add_account_with_many_accounts(self):
        mgr = self._manager()
        ok, _ = mgr.can_add_account(9999)
        assert ok is True

    def test_report_usage_returns_none(self):
        mgr = self._manager()
        assert mgr.report_usage(5) is None

    def test_fetch_usage_returns_none(self):
        mgr = self._manager()
        assert mgr.fetch_usage() is None

    def test_device_id_stable(self):
        mgr = self._manager()
        d1 = mgr.device_id
        d2 = mgr.device_id
        assert d1 == d2
        assert len(d1) > 0

    def test_last_usage_error_empty(self):
        mgr = self._manager()
        assert mgr.last_usage_error() == ""
