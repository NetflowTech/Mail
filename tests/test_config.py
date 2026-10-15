"""Tests for emailclient.config — app constants and provider presets."""
from __future__ import annotations

import pytest

from emailclient.config import APP_NAME, PROVIDERS, data_dir, db_path


class TestConfig:
    def test_app_name(self):
        assert APP_NAME == "AMail"

    def test_app_name_no_oro(self):
        assert "oro" not in APP_NAME.lower()
        assert "Oro" not in APP_NAME

    def test_providers_exist(self):
        for key in ("gmail", "zoho", "outlook", "yahoo", "custom"):
            assert key in PROVIDERS

    def test_provider_has_required_keys(self):
        required = {"imap_host", "imap_port", "smtp_host", "smtp_port", "color"}
        for name, cfg in PROVIDERS.items():
            assert required <= set(cfg.keys()), f"Provider {name!r} missing keys"

    def test_gmail_ports(self):
        assert PROVIDERS["gmail"]["imap_port"] == 993
        assert PROVIDERS["gmail"]["smtp_port"] == 465

    def test_outlook_smtp_starttls_port(self):
        # Outlook uses STARTTLS on 587, not SSL on 465
        assert PROVIDERS["outlook"]["smtp_port"] == 587

    def test_imap_port_993_for_all(self):
        for name, cfg in PROVIDERS.items():
            assert cfg["imap_port"] == 993, f"{name} imap_port should be 993"

    def test_colors_are_hex(self):
        for name, cfg in PROVIDERS.items():
            color = cfg["color"]
            assert color.startswith("#"), f"{name} color {color!r} not hex"
            assert len(color) in (4, 7), f"{name} color {color!r} wrong length"

    def test_data_dir_exists_after_call(self):
        d = data_dir()
        assert d.exists()
        assert d.is_dir()

    def test_data_dir_name(self):
        d = data_dir()
        assert d.name == ".amail"

    def test_db_path_inside_data_dir(self):
        db = db_path()
        assert db.parent == data_dir()
        assert db.name == "mail.db"

    def test_no_orostar_in_provider_hosts(self):
        for name, cfg in PROVIDERS.items():
            for key in ("imap_host", "smtp_host"):
                assert "orostar" not in cfg[key].lower(), (
                    f"{name}.{key} still contains 'orostar'"
                )
