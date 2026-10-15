"""Tests for emailclient.fleet — local blocklist and stub verification."""
from __future__ import annotations

import json

import pytest
from unittest.mock import patch


class TestFleetStubs:
    """Verify that all network-facing fleet calls are properly disabled."""

    def test_post_returns_none(self):
        from emailclient import fleet
        result = fleet._post({}, {})
        assert result is None

    def test_heartbeat_returns_none(self):
        from emailclient import fleet
        result = fleet.heartbeat([])
        assert result is None

    def test_ack_returns_true(self):
        from emailclient import fleet
        result = fleet.ack(["id1", "id2"])
        assert result is True

    def test_admin_list_returns_none(self):
        from emailclient import fleet
        assert fleet.admin_list() is None

    def test_admin_command_returns_false(self):
        from emailclient import fleet
        assert fleet.admin_command("dev", "email@x.com", "pause") is False

    def test_unblock_returns_false(self):
        from emailclient import fleet
        assert fleet.unblock("someone@x.com") is False

    def test_email_devices_returns_none(self):
        from emailclient import fleet
        assert fleet.email_devices("x@x.com") is None

    def test_export_accounts_returns_forbidden(self):
        from emailclient import fleet
        data, msg = fleet.export_accounts()
        assert data is None
        assert msg == "forbidden"


class TestBlocklist:
    """set_blocklist / blocked_emails / is_blocked round-trip via QSettings."""

    def _fresh_qs(self):
        from PySide6.QtCore import QSettings
        # Use a temp in-memory settings object so tests don't pollute real config
        return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                         "AMail-test", "fleet-test")

    def _patch_qs(self, monkeypatch):
        """Make fleet._qs() return a fresh isolated QSettings."""
        qs = self._fresh_qs()
        # Clear any residue from a previous run
        qs.remove("fleet_blocklist")
        qs.remove("fleet_can_export")
        import emailclient.fleet as fleet_mod
        monkeypatch.setattr(fleet_mod, "_qs", lambda: qs)
        return qs

    def test_empty_blocklist(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        assert fleet.blocked_emails() == set()

    def test_set_and_read_blocklist(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["A@Example.COM", "b@example.com"])
        bl = fleet.blocked_emails()
        assert "a@example.com" in bl
        assert "b@example.com" in bl

    def test_is_blocked_true(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["blocked@corp.com"])
        assert fleet.is_blocked("blocked@corp.com") is True

    def test_is_blocked_case_insensitive(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["Blocked@Corp.com"])
        assert fleet.is_blocked("BLOCKED@CORP.COM") is True

    def test_is_blocked_false(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["other@corp.com"])
        assert fleet.is_blocked("safe@corp.com") is False

    def test_overwrite_blocklist(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["old@corp.com"])
        fleet.set_blocklist(["new@corp.com"])
        assert fleet.is_blocked("old@corp.com") is False
        assert fleet.is_blocked("new@corp.com") is True

    def test_empty_list_clears_blocklist(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["someone@corp.com"])
        fleet.set_blocklist([])
        assert fleet.blocked_emails() == set()

    def test_duplicates_deduplicated(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_blocklist(["a@x.com", "A@x.com", "a@X.com"])
        assert len(fleet.blocked_emails()) == 1

    def test_corrupt_blocklist_returns_empty(self, monkeypatch):
        from emailclient import fleet
        qs = self._patch_qs(monkeypatch)
        qs.setValue("fleet_blocklist", "NOT VALID JSON{{{{")
        assert fleet.blocked_emails() == set()


class TestCanExport:
    def _patch_qs(self, monkeypatch):
        from PySide6.QtCore import QSettings
        qs = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                       "AMail-test", "fleet-test2")
        qs.remove("fleet_can_export")
        import emailclient.fleet as fleet_mod
        monkeypatch.setattr(fleet_mod, "_qs", lambda: qs)
        return qs

    def test_default_false(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        assert fleet.can_export() is False

    def test_set_true(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_can_export(True)
        assert fleet.can_export() is True

    def test_set_false(self, monkeypatch):
        from emailclient import fleet
        self._patch_qs(monkeypatch)
        fleet.set_can_export(True)
        fleet.set_can_export(False)
        assert fleet.can_export() is False
