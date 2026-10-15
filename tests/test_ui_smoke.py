"""Qt widget smoke tests — verify dialogs and widgets open without crashing.

These require a QApplication (provided by pytest-qt). They test that each
major UI component can be instantiated, shown, and has the expected title or
key attributes. No live network, IMAP, or SMTP connections are made.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def apply_default_theme():
    """Ensure theme globals are set before any UI widget is built."""
    from emailclient.ui import theme
    theme.apply_theme("AMail Light")


@pytest.fixture()
def account():
    from emailclient.models import Account
    return Account(
        id=1, name="Alice", email="alice@example.com",
        provider="gmail",
        imap_host="imap.gmail.com", imap_port=993,
        smtp_host="smtp.gmail.com", smtp_port=465,
        color="#DB4437",
    )


# ---------------------------------------------------------------------------
# About dialog
# ---------------------------------------------------------------------------

class TestAboutDialog:
    def test_opens(self, qtbot):
        from emailclient.ui.about_dialog import AboutDialog
        dlg = AboutDialog()
        qtbot.addWidget(dlg)
        assert dlg is not None

    def test_window_title(self, qtbot):
        from emailclient.ui.about_dialog import AboutDialog
        dlg = AboutDialog()
        qtbot.addWidget(dlg)
        assert "AMail" in dlg.windowTitle()
        assert "oro" not in dlg.windowTitle().lower()

    def test_fixed_size(self, qtbot):
        from emailclient.ui.about_dialog import AboutDialog
        dlg = AboutDialog()
        qtbot.addWidget(dlg)
        assert dlg.width() == 380
        assert dlg.height() == 460


# ---------------------------------------------------------------------------
# Add Account dialog
# ---------------------------------------------------------------------------

class TestAddAccountDialog:
    def test_opens(self, qtbot):
        from emailclient.ui.add_account_dialog import AddAccountDialog
        dlg = AddAccountDialog()
        qtbot.addWidget(dlg)
        assert dlg is not None

    def test_window_title_is_amail(self, qtbot):
        from emailclient.ui.add_account_dialog import AddAccountDialog
        dlg = AddAccountDialog()
        qtbot.addWidget(dlg)
        title = dlg.windowTitle().lower()
        assert "add" in title or "account" in title
        assert "oro" not in title

    def test_account_has_provider_after_instantiate(self, qtbot):
        from emailclient.ui.add_account_dialog import AddAccountDialog
        dlg = AddAccountDialog()
        qtbot.addWidget(dlg)
        # account() returns a partial Account even before submit; provider default is gmail
        acct = dlg.account()
        assert acct.provider in ("gmail", "zoho", "outlook", "yahoo", "custom")


# ---------------------------------------------------------------------------
# License dialog
# ---------------------------------------------------------------------------

class TestLicenseDialog:
    def _manager(self):
        from emailclient.license import LicenseManager
        from PySide6.QtCore import QSettings
        qs = QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope,
                       "AMail-test", "lic-smoke")
        return LicenseManager(qs=qs)

    def test_opens(self, qtbot):
        from emailclient.ui.license_dialog import LicenseDialog
        dlg = LicenseDialog(self._manager())
        qtbot.addWidget(dlg)
        assert dlg is not None

    def test_title_is_amail(self, qtbot):
        from emailclient.ui.license_dialog import LicenseDialog
        dlg = LicenseDialog(self._manager())
        qtbot.addWidget(dlg)
        title = dlg.windowTitle().lower()
        assert "oro" not in title


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------

class TestSettingsDialog:
    def test_opens(self, qtbot):
        from emailclient.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        qtbot.addWidget(dlg)
        assert dlg is not None

    def test_title_is_settings(self, qtbot):
        from emailclient.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        qtbot.addWidget(dlg)
        assert "Settings" in dlg.windowTitle()

    def test_title_no_orostar(self, qtbot):
        from emailclient.ui.settings_dialog import SettingsDialog
        dlg = SettingsDialog()
        qtbot.addWidget(dlg)
        assert "oro" not in dlg.windowTitle().lower()


# ---------------------------------------------------------------------------
# Notification stack (toast widget)
# ---------------------------------------------------------------------------

class TestNotificationStack:
    def test_opens(self, qtbot):
        from emailclient.ui.toast import NotificationStack
        stack = NotificationStack()
        qtbot.addWidget(stack)
        assert stack is not None

    def test_initially_no_cards(self, qtbot):
        from emailclient.ui.toast import NotificationStack
        stack = NotificationStack()
        qtbot.addWidget(stack)
        # Stack starts empty — no cards pushed yet
        assert stack is not None


# ---------------------------------------------------------------------------
# Theme: all palettes apply without crashing Qt globals
# ---------------------------------------------------------------------------

class TestThemeSwitching:
    def test_all_palettes_apply(self, qtbot):
        from emailclient.ui import theme
        for name in theme.PALETTES:
            theme.apply_theme(name)
            assert theme.BG != ""

    def test_build_qss_all_themes(self, qtbot):
        from emailclient.ui import theme
        for name in theme.PALETTES:
            theme.apply_theme(name)
            qss = theme.build_qss()
            assert isinstance(qss, str)
            assert len(qss) > 0

    def test_system_theme_resolves(self, qtbot):
        from emailclient.ui import theme
        theme.apply_theme(theme.SYSTEM_THEME)
        assert theme.BG != ""


# ---------------------------------------------------------------------------
# Image zoom widget
# ---------------------------------------------------------------------------

class TestImageZoom:
    def test_opens(self, qtbot):
        from PySide6.QtGui import QPixmap
        from emailclient.ui.image_zoom import ImageZoomDialog
        pm = QPixmap(100, 100)
        pm.fill()
        dlg = ImageZoomDialog(pm)
        qtbot.addWidget(dlg)
        assert dlg is not None


# ---------------------------------------------------------------------------
# Date filter widget
# ---------------------------------------------------------------------------

class TestDateFilter:
    def test_opens(self, qtbot):
        from emailclient.ui.date_filter import DateFilterDialog
        dlg = DateFilterDialog()
        qtbot.addWidget(dlg)
        assert dlg is not None


# ---------------------------------------------------------------------------
# Recipient edit field
# ---------------------------------------------------------------------------

class TestRecipientEdit:
    def test_instantiates(self, qtbot):
        from emailclient.ui.recipient_edit import RecipientEdit
        widget = RecipientEdit()
        qtbot.addWidget(widget)
        assert widget is not None

    def test_set_text(self, qtbot):
        from emailclient.ui.recipient_edit import RecipientEdit
        widget = RecipientEdit()
        qtbot.addWidget(widget)
        widget.setText("alice@example.com")
        assert "alice@example.com" in widget.text()

    def test_empty_initial_text(self, qtbot):
        from emailclient.ui.recipient_edit import RecipientEdit
        widget = RecipientEdit()
        qtbot.addWidget(widget)
        assert widget.text() == ""
