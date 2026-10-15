from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPalette
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFrame, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QTabWidget, QVBoxLayout, QWidget)

from .. import account_transfer, approval, backup, config, feedback, fleet
from ..license import LicenseManager
from ..settings import (SOUNDS, AppearanceSettings, BackupSettings, NotifSettings,
                        ReaderSettings, SendSettings, play_sound)
from . import theme
from .attachment_chips import AttachmentArea
from .license_dialog import LicenseDialog


@contextmanager
def _busy():
    """Show a wait cursor for the duration of a slow (network/disk) action."""
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()

BACKUP_FREQ_OPTIONS = [("Only at app start", "startup"),
                       ("Every 6 hours", "6h"),
                       ("Daily", "daily")]

# Where the in-app new-email popup appears on screen (value = QSettings key).
POPUP_POSITION_OPTIONS = [("Top right (default)", "top-right"),
                          ("Top left", "top-left"),
                          ("Top center", "top-center"),
                          ("Bottom right", "bottom-right"),
                          ("Bottom left", "bottom-left"),
                          ("Bottom center", "bottom-center")]

# How long a popup card stays before it disappears on its own (auto-dismiss).
AUTO_DISMISS_OPTIONS = [("After 5 seconds", 5),
                        ("After 10 seconds", 10),
                        ("After 15 seconds", 15),
                        ("After 30 seconds", 30),
                        ("After 1 minute", 60),
                        ("After 2 minutes", 120),
                        ("After 5 minutes", 300)]

# How long to wait before re-showing a dismissed-but-still-unread card.
REMINDER_OPTIONS = [("Off — don't remind me again", 0),
                    ("After 1 minute", 1),
                    ("After 2 minutes", 2),
                    ("After 5 minutes", 5),
                    ("After 10 minutes", 10),
                    ("After 15 minutes", 15),
                    ("After 30 minutes", 30),
                    ("After 1 hour", 60)]



class _ThemePreview(QFrame):
    """A row of swatches showing what a palette actually looks like.

    Reads the palette directly rather than the live globals, so it can preview
    a theme the user has not applied yet.
    """

    _SLOTS = (("BG", "surface"), ("PANEL", "panel"), ("TEXT", "text"),
              ("TEXT_DIM", "muted"), ("GOLD", "accent"), ("GOLD_DEEP", "action"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("themePreview")
        self.setFixedHeight(58)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)
        self._chips = []
        for _slot, caption in self._SLOTS:
            col = QVBoxLayout()
            col.setSpacing(3)
            chip = QFrame()
            chip.setFixedSize(38, 20)
            label = QLabel(caption)
            label.setObjectName("guide")
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            col.addWidget(chip)
            col.addWidget(label)
            lay.addLayout(col)
            self._chips.append(chip)
        lay.addStretch(1)

    def set_theme(self, name: str) -> None:
        palette = theme.resolved_palette(theme.effective_theme(name or ""))
        self.setStyleSheet(
            f"QFrame#themePreview {{ background: {palette['BG']};"
            f" border: 1px solid {palette['BORDER']}; border-radius: 8px; }}"
            f" QFrame#themePreview QLabel {{ color: {palette['TEXT_DIM']};"
            " font-size: 10px; background: transparent; }")
        for chip, (slot, _caption) in zip(self._chips, self._SLOTS):
            chip.setStyleSheet(
                f"background: {palette[slot]};"
                f" border: 1px solid {palette['BORDER']}; border-radius: 5px;")


class SettingsDialog(QDialog):
    """App-wide settings, grouped into tabs: Notifications, Appearance,
    Backup, License. Each tab scrolls so its helper text always reads fully."""

    test_requested = Signal()  # fire a delayed test toast (visibility check)
    appearance_changed = Signal()  # theme or density changed — repaint the app
    _fleet_listed = Signal(object)  # background fleet.admin_list() → render
    _lock_delete_listed = Signal(bool)  # background get_settings() → checkbox
    _usage_refreshed = Signal()  # background fetch_usage() → refresh license label
    _usage_checked = Signal(bool)  # manual refresh done (True = server answered)

    def __init__(self, parent=None, license_manager=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(660, 620)
        self._settings = NotifSettings()
        self._license = (license_manager if license_manager is not None
                         else LicenseManager())
        self._backup_settings = BackupSettings()
        self._send_settings = SendSettings()
        self._reader_settings = ReaderSettings()
        self._appearance_settings = AppearanceSettings()
        # Guards for the background fleet refresh: stop touching widgets once the
        # dialog is closing, and don't run two refreshes at once.
        self._dialog_closing = False
        self._refresh_in_progress = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        # Pages/scroll areas need an explicit light background — otherwise
        # unstyled container widgets fall through to the OS dark palette.
        self.setStyleSheet(
            f"QScrollArea#settingsScroll {{ background: {theme.BG}; border: none; }}"
            f" QWidget#settingsPage {{ background: {theme.BG}; }}"
            f" QTabWidget::pane {{ background: {theme.BG}; }}")

        self.tabs = QTabWidget()
        # All four tabs fit on one row at this width — never show the ugly
        # ◀ ▶ scroll arrows; let the tab bar share the width evenly instead.
        self.tabs.setUsesScrollButtons(False)
        self.tabs.tabBar().setExpanding(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.addTab(self._scrollable(self._build_notifications_tab()),
                         "🔔 Notifications")
        self.tabs.addTab(self._scrollable(self._build_appearance_tab()),
                         "🎨 Appearance")
        self.tabs.addTab(self._scrollable(self._build_backup_tab()),
                         "💾 Backup")
        self.tabs.addTab(self._scrollable(self._build_license_tab()),
                         "🔑 License")
        self.tabs.addTab(self._scrollable(self._build_feedback_tab()),
                         "💬 Feedback")
        # Security tab is HIDDEN by default (admin-only). Reveal it with a
        # secret gesture: 5 clicks on the License tab within 5 seconds.
        self._security_shown = False
        self._license_clicks: list[float] = []
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        layout.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save_btn.setObjectName("primary")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_states()

    # -- tab builders --
    def _build_notifications_tab(self) -> QWidget:
        page, lay = self._new_page()

        self.enabled_cb = QCheckBox("Notifications on")
        self.enabled_cb.setChecked(self._settings.enabled)
        self.enabled_cb.toggled.connect(self._update_states)
        lay.addWidget(self.enabled_cb)

        self.sound_cb = QCheckBox("Play a sound with notifications")
        self.sound_cb.setChecked(self._settings.sound_enabled)
        self.sound_cb.toggled.connect(self._update_states)
        lay.addWidget(self.sound_cb)

        sound_row = QHBoxLayout()
        sound_row.setSpacing(8)
        self.sound_combo = QComboBox()
        self.sound_combo.addItems(SOUNDS)
        if self._settings.sound in SOUNDS:
            self.sound_combo.setCurrentText(self._settings.sound)
        sound_row.addWidget(self.sound_combo, 1)
        self.preview_btn = QPushButton("▶ Preview")
        self.preview_btn.clicked.connect(
            lambda: play_sound(self.sound_combo.currentText()))
        sound_row.addWidget(self.preview_btn)
        lay.addLayout(sound_row)

        lay.addWidget(self._guide("These settings apply immediately — new-email "
                                  "cards and sounds follow them."))

        # -- Popup location --
        lay.addWidget(self._heading("📍 Popup location"))
        pos_row = QHBoxLayout()
        pos_row.addWidget(QLabel("Show new-email popups at:"))
        self.popup_pos_combo = QComboBox()
        for label, value in POPUP_POSITION_OPTIONS:
            self.popup_pos_combo.addItem(label, value)
        idx = self.popup_pos_combo.findData(self._settings.popup_position)
        self.popup_pos_combo.setCurrentIndex(idx if idx >= 0 else 0)
        pos_row.addWidget(self.popup_pos_combo, 1)
        lay.addLayout(pos_row)
        lay.addWidget(self._guide(
            "Choose which corner (or edge) of the screen the new-email popup "
            "stack appears in. Takes effect the moment you Save."))

        # -- Auto-dismiss --
        lay.addWidget(self._heading("⏳ Auto-hide popups"))
        self.auto_dismiss_cb = QCheckBox(
            "Automatically hide a popup after a while")
        self.auto_dismiss_cb.setChecked(self._settings.auto_dismiss_enabled)
        self.auto_dismiss_cb.toggled.connect(self._update_states)
        lay.addWidget(self.auto_dismiss_cb)
        ad_row = QHBoxLayout()
        ad_row.addWidget(QLabel("Hide it:"))
        self.auto_dismiss_combo = QComboBox()
        for label, value in AUTO_DISMISS_OPTIONS:
            self.auto_dismiss_combo.addItem(label, value)
        idx = self.auto_dismiss_combo.findData(self._settings.auto_dismiss_seconds)
        self.auto_dismiss_combo.setCurrentIndex(idx if idx >= 0 else 1)
        ad_row.addWidget(self.auto_dismiss_combo, 1)
        lay.addLayout(ad_row)
        lay.addWidget(self._guide(
            "When on, a new-email popup disappears on its own after this time — "
            "you don't have to click ✕. If the email is still unread, your "
            "“Unread reminders” setting below still brings the card back later "
            "(set reminders to “Off” if you want it gone for good)."))

        lay.addWidget(self._heading("⏰ Unread reminders"))
        self.reminder_combo = QComboBox()
        for label, value in REMINDER_OPTIONS:
            self.reminder_combo.addItem(label, value)
        idx = self.reminder_combo.findData(self._settings.reminder_minutes)
        self.reminder_combo.setCurrentIndex(idx if idx >= 0 else 1)
        lay.addWidget(self.reminder_combo)
        lay.addWidget(self._guide(
            "When you dismiss a new-email card with ✕ and that email is still "
            "unread, AMail shows the card again after this much time so "
            "you don't miss it. Choose “Off” to dismiss cards for good."))

        test_btn = QPushButton("🔔 Test notification (in 3 sec)")
        test_btn.setToolTip("Click, then switch to Chrome/another app — "
                            "the card should appear there too")
        test_btn.clicked.connect(self.test_requested)
        lay.addWidget(test_btn)

        lay.addWidget(self._guide("Connection problems show a warning card; "
                                  "transient drops auto-reconnect."))
        lay.addStretch()
        return page

    def _build_appearance_tab(self) -> QWidget:
        page, lay = self._new_page()
        lay.addWidget(self._heading("🎨 Theme"))
        self.theme_combo = QComboBox()
        self.theme_combo.addItem(f"{theme.SYSTEM_THEME} (follow light / dark)",
                                 theme.SYSTEM_THEME)
        for name in theme.PALETTES:
            suffix = " — dark" if theme.PALETTES[name].get("IS_DARK") else ""
            self.theme_combo.addItem(f"{name}{suffix}", name)
        chosen = theme.current_theme()
        idx = self.theme_combo.findData(chosen)
        self.theme_combo.setCurrentIndex(max(0, idx))
        lay.addWidget(self.theme_combo)

        # Live swatch strip: seeing the palette beats reading its name.
        self.theme_preview = _ThemePreview()
        self.theme_preview.set_theme(chosen)
        lay.addWidget(self.theme_preview)
        self.theme_combo.currentIndexChanged.connect(
            lambda _i: self.theme_preview.set_theme(self.theme_combo.currentData()))
        lay.addWidget(self._guide(
            "Dark themes darken the app only — emails keep their white sheet, "
            "because that is how they were written. Some windows "
            "(compose/reader) update fully after being reopened."))

        lay.addWidget(self._heading("↕ Density"))
        density_row = QHBoxLayout()
        density_row.addWidget(QLabel("Message rows:"))
        self.density_combo = QComboBox()
        for name in theme.DENSITIES:
            self.density_combo.addItem(name, name)
        d_idx = self.density_combo.findData(theme.current_density())
        self.density_combo.setCurrentIndex(max(0, d_idx))
        density_row.addWidget(self.density_combo)
        density_row.addStretch(1)
        lay.addLayout(density_row)
        lay.addWidget(self._guide(
            "How much room each message row gets. Nothing is hidden at "
            "Compact — the rows just sit closer together."))

        lay.addWidget(self._heading("↩ Undo Send"))
        undo_row = QHBoxLayout()
        undo_row.addWidget(QLabel("Undo window:"))
        self.undo_combo = QComboBox()
        # value (seconds) → label
        self._undo_choices = [(0, "Off (send immediately)"), (5, "5 seconds"),
                              (10, "10 seconds"), (15, "15 seconds"),
                              (30, "30 seconds"), (60, "60 seconds")]
        for _s, lbl in self._undo_choices:
            self.undo_combo.addItem(lbl)
        cur = self._send_settings.undo_send_seconds
        idx = next((i for i, (s, _l) in enumerate(self._undo_choices) if s == cur), 3)
        self.undo_combo.setCurrentIndex(idx)
        undo_row.addWidget(self.undo_combo, 1)
        lay.addLayout(undo_row)
        lay.addWidget(self._guide(
            "After you hit Send, the email waits this long in the Outbox so you "
            "can Recall it with the Undo toast before it actually goes out."))

        lay.addWidget(self._heading("🖼 Remote images"))
        self.remote_img_cb = QCheckBox("Load remote images in emails")
        self.remote_img_cb.setChecked(self._reader_settings.load_remote_images)
        lay.addWidget(self.remote_img_cb)
        lay.addWidget(self._guide(
            "On: hosted images (logos, banners) display. Off: they're blocked, "
            "which stops tracking pixels from telling senders you opened the "
            "email or revealing your IP. Applies to the next email you open."))

        lay.addWidget(self._heading("📇 Account list"))
        self.show_email_cb = QCheckBox("Show email address instead of account name")
        self.show_email_cb.setChecked(self._appearance_settings.show_email)
        lay.addWidget(self.show_email_cb)
        lay.addWidget(self._guide(
            "On: the sidebar lists each account by its email address (e.g. "
            "support@amail.app). Off: it shows the friendly account name "
            "(e.g. AMail). Applies as soon as you Save."))
        lay.addStretch()
        return page

    def _build_backup_tab(self) -> QWidget:
        page, lay = self._new_page()
        lay.addWidget(self._heading("💾 Backup"))

        self.backup_freq_combo = QComboBox()
        for label, value in BACKUP_FREQ_OPTIONS:
            self.backup_freq_combo.addItem(label, value)
        idx = self.backup_freq_combo.findData(self._backup_settings.frequency)
        if idx >= 0:
            self.backup_freq_combo.setCurrentIndex(idx)
        lay.addWidget(self.backup_freq_combo)

        self.last_backup_label = QLabel(
            f"Last backup: {backup.last_backup_time() or 'never'}")
        lay.addWidget(self.last_backup_label)

        self.backup_folder_label = QLabel()
        self.backup_folder_label.setObjectName("guide")
        self.backup_folder_label.setWordWrap(True)
        self._update_backup_folder_label()
        lay.addWidget(self.backup_folder_label)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(8)
        choose_btn = QPushButton("📁 Choose backup folder…")
        choose_btn.clicked.connect(self._choose_backup_folder)
        folder_row.addWidget(choose_btn)
        open_folder_btn = QPushButton("📂 Open backup folder")
        open_folder_btn.clicked.connect(self._open_backup_folder)
        folder_row.addWidget(open_folder_btn)
        lay.addLayout(folder_row)

        backup_row = QHBoxLayout()
        backup_row.setSpacing(8)
        backup_now_btn = QPushButton("💾 Backup Now")
        backup_now_btn.clicked.connect(self._backup_now)
        backup_row.addWidget(backup_now_btn)
        lay.addLayout(backup_row)

        restore_btn = QPushButton("♻️ Restore from backup…")
        restore_btn.clicked.connect(self._restore_backup)
        lay.addWidget(restore_btn)

        restore_file_btn = QPushButton("📂 Restore from file…")
        restore_file_btn.setToolTip(
            "Pick a backup (.db) file from anywhere — e.g. one copied from "
            "another computer or a USB drive.")
        restore_file_btn.clicked.connect(self._restore_from_file)
        lay.addWidget(restore_file_btn)

        lay.addWidget(self._guide(
            "A backup contains the emails and settings of ALL accounts together "
            "(the mail.db file). Passwords live in the Mac Keychain — they are "
            "never included in backups."))

        lay.addWidget(self._heading("🔑 Move accounts to another PC"))
        lay.addWidget(self._guide(
            "Export all your logged-in accounts (with their passwords, groups "
            "and settings) into one password-protected file. On another "
            "computer, just Import that file — every account comes back, no "
            "re-adding one by one. Emails re-download automatically."))
        acct_row = QHBoxLayout()
        acct_row.setSpacing(8)
        export_btn = QPushButton("📤 Export accounts…")
        export_btn.clicked.connect(self._export_accounts)
        import_btn = QPushButton("📥 Import accounts…")
        import_btn.clicked.connect(self._import_accounts)
        acct_row.addWidget(export_btn)
        acct_row.addWidget(import_btn)
        lay.addLayout(acct_row)

        lay.addStretch()
        return page

    def _on_tab_clicked(self, index: int):
        """Secret gesture: 5 clicks on the License tab within 5 s reveal the
        hidden Security tab (kept hidden so normal users never see it)."""
        if self._security_shown:
            return
        if "License" not in self.tabs.tabText(index):
            self._license_clicks = []
            return
        now = time.monotonic()
        self._license_clicks = [t for t in self._license_clicks if now - t <= 5.0]
        self._license_clicks.append(now)
        if len(self._license_clicks) >= 5:
            self._reveal_security_tab()

    def _reveal_security_tab(self):
        self._security_shown = True
        lic_idx = next((i for i in range(self.tabs.count())
                        if "License" in self.tabs.tabText(i)), self.tabs.count())
        page = self._scrollable(self._build_security_tab())
        self.tabs.insertTab(lic_idx, page, "🔒 Security")
        self.tabs.setCurrentIndex(lic_idx)

    def _build_security_tab(self) -> QWidget:
        page, lay = self._new_page()
        lay.addWidget(self._heading("🔒 Admin send-approval"))
        self._sec_note = self._guide(
            "An admin can require approval before certain accounts send mail. "
            "Mark accounts below; emails from them wait in the admin's "
            "“Waiting for Approval” list (across all devices on this license) "
            "until the admin approves.")
        lay.addWidget(self._sec_note)

        # --- login area (shown when not logged in) ---
        self._admin_user_edit = QLineEdit()
        self._admin_user_edit.setPlaceholderText("Admin username")
        self._admin_pass_edit = QLineEdit()
        self._admin_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._admin_pass_edit.setPlaceholderText("Admin password")
        login_btn = QPushButton("Log in as admin")
        login_btn.clicked.connect(self._admin_login)
        login_lay = QVBoxLayout()
        login_lay.setSpacing(6)
        login_lay.setContentsMargins(0, 0, 0, 0)
        login_lay.addWidget(self._admin_user_edit)
        login_lay.addWidget(self._admin_pass_edit)
        login_lay.addWidget(login_btn)
        self._login_wrap = QWidget()
        self._login_wrap.setLayout(login_lay)
        lay.addWidget(self._login_wrap)

        # --- logged-in area (admin) ---
        self._admin_status_lbl = QLabel()
        f = self._admin_status_lbl.font()
        f.setWeight(QFont.Weight.DemiBold)
        self._admin_status_lbl.setFont(f)
        self._sec_search = QLineEdit()
        self._sec_search.setPlaceholderText("🔍 Search accounts…")
        self._sec_search.setClearButtonEnabled(True)
        self._sec_search.textChanged.connect(self._filter_accounts)
        all_row = QHBoxLayout()
        all_row.setContentsMargins(0, 0, 0, 0)
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(lambda: self._set_all_checked(True))
        sel_none = QPushButton("Select none")
        sel_none.clicked.connect(lambda: self._set_all_checked(False))
        all_row.addWidget(sel_all)
        all_row.addWidget(sel_none)
        all_row.addStretch(1)
        self._all_row_wrap = QWidget()
        self._all_row_wrap.setLayout(all_row)

        self._accounts_box = QWidget()
        self._accounts_lay = QVBoxLayout(self._accounts_box)
        self._accounts_lay.setContentsMargins(0, 0, 0, 0)
        self._accounts_lay.setSpacing(4)
        save_btn = QPushButton("💾 Save approval list")
        save_btn.clicked.connect(self._save_approval_accounts)
        logout_btn = QPushButton("Log out of admin")
        logout_btn.clicked.connect(self._admin_logout)

        # Policy: lock email deletion for everyone on this license. When on,
        # non-admin users can't delete any email (incoming or otherwise).
        self._lock_delete_cb = QCheckBox(
            "🔒 Lock email deletion — users on this license can't delete emails")
        self._lock_delete_cb.toggled.connect(self._on_lock_delete_toggled)

        in_lay = QVBoxLayout()
        in_lay.setSpacing(8)
        in_lay.setContentsMargins(0, 0, 0, 0)
        in_lay.addWidget(self._admin_status_lbl)
        in_lay.addWidget(self._guide("Require approval to send from:"))
        in_lay.addWidget(self._sec_search)
        in_lay.addWidget(self._all_row_wrap)
        in_lay.addWidget(self._accounts_box)
        in_lay.addWidget(save_btn)
        in_lay.addWidget(self._lock_delete_cb)
        self._devices_section = self._build_devices_section()
        in_lay.addWidget(self._devices_section)
        in_lay.addWidget(logout_btn)
        self._loggedin_wrap = QWidget()
        self._loggedin_wrap.setLayout(in_lay)
        lay.addWidget(self._loggedin_wrap)

        lay.addStretch()
        self._acct_checks: dict[str, QCheckBox] = {}
        self._fleet_listed.connect(self._render_devices)
        self._lock_delete_listed.connect(self._apply_lock_delete_state)
        self._refresh_security_state()
        return page

    # -- Devices on this license (cross-device admin) -----------------------
    def _build_devices_section(self) -> QWidget:
        box = QGroupBox("🖥 Devices on this license")
        outer = QVBoxLayout(box)
        outer.setSpacing(8)
        outer.addWidget(self._guide(
            "Every PC signed in with this license and the accounts running on "
            "it. Remove drops an account everywhere and blocks it from being "
            "re-added; Pause/Resume controls a single device."))
        self._devices_refresh_btn = QPushButton("🔄 Refresh")
        self._devices_refresh_btn.clicked.connect(self._refresh_devices_list)
        outer.addWidget(self._devices_refresh_btn)

        self._devices_status = QLabel("")
        self._devices_status.setObjectName("guide")
        outer.addWidget(self._devices_status)

        self._devices_box = QWidget()
        self._devices_lay = QVBoxLayout(self._devices_box)
        self._devices_lay.setContentsMargins(0, 0, 0, 0)
        self._devices_lay.setSpacing(4)
        outer.addWidget(self._devices_box)

        block_lbl = QLabel("⛔ Blocklist (removed accounts)")
        bf = block_lbl.font(); bf.setWeight(QFont.Weight.DemiBold)
        block_lbl.setFont(bf)
        outer.addWidget(block_lbl)
        self._blocklist_box = QWidget()
        self._blocklist_lay = QVBoxLayout(self._blocklist_box)
        self._blocklist_lay.setContentsMargins(0, 0, 0, 0)
        self._blocklist_lay.setSpacing(4)
        outer.addWidget(self._blocklist_box)
        return box

    def done(self, result):
        # Called by accept()/reject()/close — stop any in-flight refresh from
        # touching this (about-to-be-deleted) dialog's widgets.
        self._dialog_closing = True
        try:
            self._fleet_listed.disconnect(self._render_devices)
        except (RuntimeError, TypeError):
            pass
        super().done(result)

    def _refresh_devices_list(self):
        """Fetch the fleet on a background thread; render via _fleet_listed."""
        if self._dialog_closing or self._refresh_in_progress:
            return
        self._refresh_in_progress = True
        self._devices_refresh_btn.setEnabled(False)
        self._devices_status.setText("Loading…")

        def work():
            data = fleet.admin_list()
            if self._dialog_closing:
                return
            try:
                self._fleet_listed.emit(data if data is not None else {})
            except RuntimeError:
                pass   # dialog torn down between the check and the emit

        threading.Thread(target=work, daemon=True).start()

    def _clear_layout(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _ago(self, last_seen) -> str:
        try:
            secs = int(time.time()) - int(last_seen)
        except (TypeError, ValueError):
            return ""
        if secs < 90:
            return "just now"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        return f"{hours}h ago" if hours < 24 else f"{hours // 24}d ago"

    _STATUS_PILL = {"running": "🟢 running", "paused": "🟡 paused",
                    "reconnecting": "🟡 reconnecting", "error": "🔴 error",
                    "blocked": "⛔ blocked"}

    def _render_devices(self, data):
        if self._dialog_closing:
            return
        self._refresh_in_progress = False
        self._fleet_data = data
        try:
            self._devices_refresh_btn.setEnabled(True)
            self._populate_account_checkboxes(data)
            self._clear_layout(self._devices_lay)
            self._clear_layout(self._blocklist_lay)
        except RuntimeError:
            return   # widgets already destroyed (dialog closing)
        if not data:
            self._devices_status.setText(
                "No devices reported yet (or the fleet server is unreachable).")
            return
        devices = data.get("devices", {}) or {}
        blocklist = data.get("blocklist", []) or []
        total = sum(len(d.get("accounts", []) or []) for d in devices.values())
        self._devices_status.setText(
            f"{len(devices)} device(s), {total} account(s) running on this license.")

        # Stable order: most-recently-seen device first.
        for dev_id, info in sorted(
                devices.items(),
                key=lambda kv: kv[1].get("last_seen", 0), reverse=True):
            name = info.get("name", "PC")
            header = QLabel(f"💻 {name}   ·   {self._ago(info.get('last_seen'))}")
            hf = header.font(); hf.setWeight(QFont.Weight.DemiBold)
            header.setFont(hf)
            header.setStyleSheet(f"color: {theme.GOLD_DEEP};")
            self._devices_lay.addWidget(header)
            meta = self._device_meta_line(info)
            if meta:
                self._devices_lay.addWidget(self._guide(meta))
            accounts = info.get("accounts", []) or []
            if not accounts:
                self._devices_lay.addWidget(self._guide("   (no accounts)"))
            for acc in accounts:
                self._devices_lay.addWidget(self._device_account_row(dev_id, acc))

        if not blocklist:
            self._blocklist_lay.addWidget(self._guide("Nothing blocked."))
        for email in sorted(blocklist):
            self._blocklist_lay.addWidget(self._blocklist_row(email))

    def _device_meta_line(self, info: dict) -> str:
        """One-line 'where is this PC' summary: public IP + geo + local IP."""
        pub = str(info.get("public_ip", "") or "")
        loc = str(info.get("local_ip", "") or "")
        geo = info.get("geo") or {}
        place = ", ".join(p for p in (geo.get("city"), geo.get("region"),
                                      geo.get("country")) if p)
        parts = []
        if pub:
            parts.append(f"🌐 {pub}" + (f" · {place}" if place else ""))
        elif place:
            parts.append(f"🌐 {place}")
        if geo.get("isp"):
            parts.append(geo["isp"])
        if loc:
            parts.append(f"🏠 {loc}")
        return "   " + "   ·   ".join(parts) if parts else ""

    def _device_account_row(self, dev_id: str, acc: dict) -> QWidget:
        email = str(acc.get("email", ""))
        status = str(acc.get("status", "running"))
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(6)
        label = QLabel(f"{acc.get('name', '')}  <{email}>")
        label.setToolTip(email)
        row.addWidget(label, 1)
        pill = QLabel(self._STATUS_PILL.get(status, status))
        row.addWidget(pill)
        if status == "paused":
            resume = QPushButton("Resume"); resume.setObjectName("chip")
            resume.clicked.connect(
                lambda _=False, d=dev_id, e=email: self._fleet_cmd(d, e, "resume"))
            row.addWidget(resume)
        else:
            pause = QPushButton("Pause"); pause.setObjectName("chip")
            pause.clicked.connect(
                lambda _=False, d=dev_id, e=email: self._fleet_cmd(d, e, "pause"))
            row.addWidget(pause)
        remove = QPushButton("Remove"); remove.setObjectName("chip")
        remove.clicked.connect(
            lambda _=False, d=dev_id, e=email: self._fleet_remove(d, e))
        row.addWidget(remove)
        return w

    def _blocklist_row(self, email: str) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(12, 0, 0, 0)
        row.addWidget(QLabel(f"⛔ {email}"), 1)
        unblock = QPushButton("Unblock"); unblock.setObjectName("chip")
        unblock.clicked.connect(lambda _=False, e=email: self._fleet_unblock(e))
        row.addWidget(unblock)
        return w

    def _fleet_cmd(self, dev_id: str, email: str, action: str):
        threading.Thread(
            target=lambda: fleet.admin_command(dev_id, email, action),
            daemon=True).start()
        QTimer.singleShot(900, self._refresh_devices_list)

    def _fleet_remove(self, dev_id: str, email: str):
        if QMessageBox.question(
                self, "Remove account",
                f"Remove {email} from this license?\n\nIt will be deleted from "
                "the device it runs on and blocked from being re-added until you "
                "unblock it.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._fleet_cmd(dev_id, email, "remove")

    def _fleet_unblock(self, email: str):
        threading.Thread(target=lambda: fleet.unblock(email), daemon=True).start()
        QTimer.singleShot(900, self._refresh_devices_list)

    def _refresh_security_state(self):
        has_key = bool(approval._key())
        admin = approval.is_admin()
        self._login_wrap.setVisible(has_key and not admin)
        self._loggedin_wrap.setVisible(has_key and admin)
        if not has_key:
            self._sec_note.setText(
                "Activate a license first — approval uses your license key to "
                "link the admin and the other devices.")
            return
        if admin:
            creds = approval.admin_creds()
            self._admin_status_lbl.setText(f"✓ Logged in as admin ({creds[0]})")
            self._populate_account_checkboxes()
            self._refresh_devices_list()
            self._load_lock_delete_state()

    def _load_lock_delete_state(self):
        """Fetch the delete-lock setting in the background and reflect it in the
        checkbox (without firing the toggle handler)."""
        def work():
            settings = approval.get_settings()
            if not settings or self._dialog_closing:
                return
            try:
                self._lock_delete_listed.emit(bool(settings.get("lock_delete")))
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _apply_lock_delete_state(self, checked: bool):
        self._lock_delete_cb.blockSignals(True)
        self._lock_delete_cb.setChecked(checked)
        self._lock_delete_cb.blockSignals(False)

    def _on_lock_delete_toggled(self, checked: bool):
        if not approval.set_lock_delete(checked):
            self._apply_lock_delete_state(not checked)
            self._handle_admin_save_failure()
            return
        approval.refresh_settings_cache()   # apply on this device immediately

    def _handle_admin_save_failure(self):
        """An admin POST failed. Tell apart a stale admin session (the admin
        password changed on the server, so this device's saved creds no longer
        work) from a real network problem — and for the former, drop the dead
        session and bring back the login form instead of a dead-end warning."""
        with _busy():
            state = approval.admin_session_state()
        if state == "invalid":
            approval.clear_admin()
            self._refresh_security_state()   # reveals the login form again
            QMessageBox.warning(
                self, "Admin session expired",
                "Your admin session is no longer valid — the admin password "
                "was likely changed on the server. Please log in again as "
                "admin to manage these settings.")
        else:
            QMessageBox.warning(
                self, "Setting",
                "Could not save the setting — the approval server is "
                "unreachable. Check your internet connection and try again.")

    def _group_header(self, title: str, checks: list) -> QWidget:
        """A group label with 'All' / 'None' buttons scoped to that group."""
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 6, 0, 0)
        lbl = QLabel(title)
        f = lbl.font()
        f.setWeight(QFont.Weight.DemiBold)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color: {theme.GOLD_DEEP};")
        row.addWidget(lbl, 1)
        all_b = QPushButton("All")
        all_b.setObjectName("chip")
        all_b.clicked.connect(lambda: [c.setChecked(True) for c in checks])
        none_b = QPushButton("None")
        none_b.setObjectName("chip")
        none_b.clicked.connect(lambda: [c.setChecked(False) for c in checks])
        row.addWidget(all_b)
        row.addWidget(none_b)
        return w

    def _populate_account_checkboxes(self, data=None):
        """Build the approval picker from the *fleet* snapshot (every account on
        every device of this license), grouped by folder, deduped by email — so
        an admin on any PC can require approval for any employee's account, even
        ones that live on a different machine. ``data`` is the fleet.admin_list()
        result; None means it hasn't loaded yet."""
        self._clear_layout(self._accounts_lay)
        self._acct_checks = {}
        self._acct_rows = []   # (checkbox, search_text, header_widget)
        if data is None:
            data = getattr(self, "_fleet_data", None)
        devices = (data or {}).get("devices", {}) or {}
        if not devices:
            self._accounts_lay.addWidget(self._guide(
                "Loading accounts from the fleet… open AMail on the "
                "employees' PCs (signed in with this license) and their accounts "
                "will appear here to select."))
            return
        required = set(approval.fetch_accounts() or approval.cached_accounts())
        # Aggregate every account across all devices, deduped by email. Keep the
        # first non-empty name/group we see for each.
        agg: dict = {}   # email -> {"name", "group"}
        for info in devices.values():
            for acc in info.get("accounts", []) or []:
                em = str(acc.get("email", "")).strip().lower()
                if not em:
                    continue
                cur = agg.setdefault(em, {"name": "", "group": ""})
                if not cur["name"] and acc.get("name"):
                    cur["name"] = str(acc.get("name"))
                if not cur["group"] and acc.get("group"):
                    cur["group"] = str(acc.get("group"))
        if not agg:
            self._accounts_lay.addWidget(self._guide(
                "No accounts reported by any device yet."))
            return
        # bucket by group name (ungrouped last), each group alphabetical
        buckets: dict = {}
        for em, meta in agg.items():
            buckets.setdefault(meta["group"], []).append((em, meta))
        named = sorted((g for g in buckets if g), key=str.lower)
        order = named + ([""] if "" in buckets else [])
        for gname in order:
            checks = []
            title = f"📁 {gname}" if gname else "📁 Ungrouped"
            header = self._group_header(title, checks)
            self._accounts_lay.addWidget(header)
            for em, meta in sorted(buckets[gname], key=lambda t: t[0]):
                label = f"{meta['name']}   <{em}>" if meta["name"] else f"<{em}>"
                cb = QCheckBox(label)
                cb.setChecked(em in required)
                self._accounts_lay.addWidget(cb)
                self._acct_checks[em] = cb
                checks.append(cb)
                self._acct_rows.append(
                    (cb, f"{meta['name']} {em}".lower(), header))

    def _filter_accounts(self, text: str):
        q = text.strip().lower()
        rows = getattr(self, "_acct_rows", [])
        visible_per_header: dict = {}
        for cb, hay, header in rows:
            show = q in hay if q else True
            cb.setVisible(show)
            visible_per_header[header] = visible_per_header.get(header, False) or show
        for header, any_visible in visible_per_header.items():
            header.setVisible(any_visible)

    def _set_all_checked(self, checked: bool):
        for cb in getattr(self, "_acct_checks", {}).values():
            cb.setChecked(checked)

    def _admin_login(self):
        u = self._admin_user_edit.text().strip()
        p = self._admin_pass_edit.text()
        if not u or not p:
            QMessageBox.warning(self, "Admin login",
                                "Enter both the admin username and password.")
            return
        with _busy():
            ok = approval.verify_admin(u, p)
        if ok:
            approval.save_admin(u, p)
            self._admin_pass_edit.clear()
            self._refresh_security_state()
            QMessageBox.information(self, "Admin login",
                                    "Logged in. This device is now the admin "
                                    "console — the “Waiting for Approval” list "
                                    "will appear in the sidebar.")
        else:
            QMessageBox.warning(self, "Admin login",
                                "Wrong admin username/password, or the approval "
                                "server is unreachable.")

    def _admin_logout(self):
        approval.clear_admin()
        self._refresh_security_state()

    def _save_approval_accounts(self):
        creds = approval.admin_creds()
        if not creds:
            return
        emails = [e for e, cb in self._acct_checks.items() if cb.isChecked()]
        with _busy():
            saved = approval.mark_accounts(emails, creds[0], creds[1])
            if saved:
                approval.refresh_accounts_cache()
        if saved:
            QMessageBox.information(
                self, "Approval list",
                f"Saved — {len(emails)} account(s) now require approval to send.")
        else:
            QMessageBox.warning(self, "Approval list",
                                "Could not save (server unreachable?).")

    def _build_license_tab(self) -> QWidget:
        page, lay = self._new_page()
        lay.addWidget(self._heading("🔑 License"))

        self.license_label = QLabel()
        self.license_label.setWordWrap(True)
        self._refresh_license_label()
        lay.addWidget(self.license_label)

        # The label shows the cached cross-device total, which can lag behind the
        # server (other PCs add/remove accounts without this device re-reporting).
        # Pull the live total once in the background and re-render when it lands.
        self._usage_refreshed.connect(self._refresh_license_label)
        threading.Thread(target=self._refresh_usage_bg, daemon=True).start()

        # That background pull is best-effort and silent, so the numbers can
        # still look stale (offline at open, another PC just added accounts,
        # this device's own count never reported). Give the user a way to ask
        # for the live figures and see whether it actually worked.
        self.usage_hint = self._guide("")
        self.usage_hint.hide()
        lay.addWidget(self.usage_hint)
        self.usage_refresh_btn = QPushButton("⟳ Refresh usage")
        self.usage_refresh_btn.setToolTip(
            "Report this PC's accounts and read the live used/remaining "
            "totals for this license")
        self.usage_refresh_btn.clicked.connect(self._refresh_usage_clicked)
        self._usage_checked.connect(self._on_usage_checked)
        lay.addWidget(self.usage_refresh_btn)

        manage_btn = QPushButton("Manage license…")
        manage_btn.clicked.connect(self._manage_license)
        lay.addWidget(manage_btn)

        about_btn = QPushButton("ℹ️ About AMail…")
        about_btn.clicked.connect(self._show_about)
        lay.addWidget(about_btn)

        # Export unique accounts — only for emails the admin granted the export
        # permission (from the web panel). The flag is cached from the last fleet
        # heartbeat; the server re-checks it when the export actually runs.
        if fleet.can_export():
            lay.addWidget(self._heading("📇 Export unique accounts"))
            lay.addWidget(self._guide(
                "Download the full list of unique accounts on this license — each "
                "with the PC(s) it is installed on — as a spreadsheet (opens in "
                "Excel)."))
            export_unique_btn = QPushButton("📊 Export unique accounts (Excel)…")
            export_unique_btn.clicked.connect(self._export_unique_accounts)
            lay.addWidget(export_unique_btn)

        lay.addStretch()
        return page

    def _export_unique_accounts(self):
        """Fetch the license-wide unique-account list (with PCs) and save it as a
        CSV that opens cleanly in Excel. Server enforces the export permission."""
        from PySide6.QtWidgets import QFileDialog
        import csv
        from datetime import datetime, timezone
        with _busy():
            rows, reason = fleet.export_accounts()
        if rows is None:
            if reason == "forbidden":
                # The permission is per-device: this PC must be signed in with an
                # email the admin granted export rights to. Clear the stale cached
                # flag so the button hides until a heartbeat re-confirms access.
                fleet.set_can_export(False)
                QMessageBox.warning(
                    self, "Export unique accounts",
                    "This PC isn't signed in with an account that has export "
                    "permission.\n\nExport only works on the computer where the "
                    "permitted email (the one granted in the web panel) is logged "
                    "in. Sign in with that account here, or run the export on that "
                    "PC.")
            elif reason == "no_license":
                QMessageBox.warning(
                    self, "Export unique accounts",
                    "No license is activated on this device.")
            elif reason.startswith("server:"):
                # The server answered — the request itself was rejected. Saying
                # "check your internet connection" here sends people chasing the
                # wrong problem.
                QMessageBox.warning(
                    self, "Export unique accounts",
                    "The server rejected this request "
                    f"(error: {reason.split(':', 1)[1]}).\n\n"
                    "This usually means the license key or device id stored on "
                    "this PC is not valid. Re-activate the license under "
                    "“Manage license…” and try again.")
            else:
                detail = fleet.last_error()
                QMessageBox.warning(
                    self, "Export unique accounts",
                    "Could not reach the server (amail.app).\n\n"
                    "Check the internet connection, and whether a firewall, "
                    "antivirus or company proxy on this PC is blocking HTTPS "
                    "to amail.app."
                    + (f"\n\nDetails: {detail}" if detail else ""))
            return
        if not rows:
            QMessageBox.information(
                self, "Export unique accounts",
                "No accounts are currently reported on this license.")
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save unique accounts",
            f"amail-unique-accounts-{stamp}.csv", "CSV / Excel (*.csv)")
        if not path:
            return
        try:
            with _busy(), open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(["#", "Account", "Display name", "Group", "Status",
                            "Installed on PC(s)", "# PCs", "Local IP(s)",
                            "Public IP(s)"])
                for i, a in enumerate(rows, 1):
                    devs = a.get("devices", []) or []
                    names = [str(d.get("name", "") or "PC") for d in devs]
                    locals_ = [d.get("local_ip", "") for d in devs if d.get("local_ip")]
                    publics = [d.get("public_ip", "") for d in devs if d.get("public_ip")]
                    w.writerow([
                        i, a.get("email", ""), a.get("name", ""), a.get("group", ""),
                        a.get("status", ""), "; ".join(names), len(devs),
                        "; ".join(dict.fromkeys(locals_)),
                        "; ".join(dict.fromkeys(publics)),
                    ])
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Accounts exported",
            f"{len(rows)} unique account(s) saved to:\n{path}")

    # -- small widget helpers --

    # -- feedback ---------------------------------------------------------
    def _build_feedback_tab(self) -> QWidget:
        page, lay = self._new_page()
        lay.addWidget(self._heading("💬 Report a problem or an idea"))
        lay.addWidget(self._guide(
            "Tell us what happened and attach a screenshot — it goes straight "
            "to the AMail team with your app version and device name, so "
            "you don't have to describe your setup."))

        kind_row = QHBoxLayout()
        kind_row.addWidget(QLabel("This is:"))
        self.fb_kind = QComboBox()
        for value, label in feedback.KINDS:
            self.fb_kind.addItem(label, value)
        kind_row.addWidget(self.fb_kind, 1)
        lay.addLayout(kind_row)

        self.fb_text = QPlainTextEdit()
        self.fb_text.setPlaceholderText(
            "What were you doing, and what went wrong?")
        self.fb_text.setMinimumHeight(140)
        lay.addWidget(self.fb_text)

        contact_row = QHBoxLayout()
        contact_row.addWidget(QLabel("Reply to (optional):"))
        self.fb_contact = QLineEdit()
        self.fb_contact.setPlaceholderText("email or phone, if you want an answer")
        contact_row.addWidget(self.fb_contact, 1)
        lay.addLayout(contact_row)

        lay.addWidget(self._heading("🖼 Screenshots"))
        shot_row = QHBoxLayout()
        self.fb_attach_btn = QPushButton("Attach screenshot…")
        self.fb_attach_btn.clicked.connect(self._fb_attach)
        self.fb_paste_btn = QPushButton("Paste from clipboard")
        self.fb_paste_btn.setToolTip(
            "Take a screenshot with your usual shortcut, then paste it here")
        self.fb_paste_btn.clicked.connect(self._fb_paste)
        shot_row.addWidget(self.fb_attach_btn)
        shot_row.addWidget(self.fb_paste_btn)
        shot_row.addStretch(1)
        lay.addLayout(shot_row)

        # Same chips the composer uses: click one to preview it before sending.
        self.fb_shots: list[str] = []
        self.fb_shot_area = AttachmentArea()
        self.fb_shot_area.file_removed.connect(self._fb_remove_shot)
        self.fb_shot_area.file_opened.connect(self._fb_preview_shot)
        lay.addWidget(self.fb_shot_area)

        send_row = QHBoxLayout()
        self.fb_status = QLabel("")
        self.fb_status.setObjectName("guide")
        self.fb_status.setWordWrap(True)
        self.fb_send_btn = QPushButton("Send report")
        self.fb_send_btn.setObjectName("primary")
        self.fb_send_btn.clicked.connect(self._fb_send)
        send_row.addWidget(self.fb_status, 1)
        send_row.addWidget(self.fb_send_btn)
        lay.addLayout(send_row)
        lay.addStretch()
        return page

    def _fb_refresh_shots(self) -> None:
        self.fb_shot_area.set_files(self.fb_shots)
        left = feedback.MAX_SHOTS - len(self.fb_shots)
        for btn in (self.fb_attach_btn, self.fb_paste_btn):
            btn.setEnabled(left > 0)

    def _fb_add_shot(self, path: str) -> None:
        if len(self.fb_shots) >= feedback.MAX_SHOTS:
            self._fb_note(f"Up to {feedback.MAX_SHOTS} screenshots per report.")
            return
        problem = feedback.validate_shot(path)
        if problem:
            self._fb_note(problem)
            return
        if path not in self.fb_shots:
            self.fb_shots.append(path)
            self._fb_refresh_shots()
            self._fb_note("")

    def _fb_attach(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Attach screenshot", "",
            "Images (*.png *.jpg *.jpeg *.gif *.webp)")
        for path in paths:
            self._fb_add_shot(path)

    def _fb_paste(self) -> None:
        """Screenshot shortcuts put the image on the clipboard, not on disk, so
        write it out to a temp PNG and attach that."""
        image = QApplication.clipboard().image()
        if image.isNull():
            self._fb_note("There is no image on the clipboard.")
            return
        import tempfile
        fd, path = tempfile.mkstemp(prefix="amail_shot_", suffix=".png")
        os.close(fd)
        if not image.save(path, "PNG"):
            self._fb_note("That clipboard image could not be saved.")
            return
        self._fb_add_shot(path)

    def _fb_remove_shot(self, path: str) -> None:
        if path in self.fb_shots:
            self.fb_shots.remove(path)
        self._fb_refresh_shots()

    def _fb_preview_shot(self, path: str) -> None:
        from .attachment_preview import AttachmentPreviewDialog
        if os.path.isfile(path):
            AttachmentPreviewDialog(path, self, allow_download=False).exec()

    def _fb_note(self, text: str, ok: bool = False) -> None:
        self.fb_status.setText(text)
        colour = theme.SUCCESS if ok else theme.DANGER
        self.fb_status.setStyleSheet(f"color: {colour};" if text else "")

    def _fb_send(self) -> None:
        message = self.fb_text.toPlainText().strip()
        if not message:
            self._fb_note("Write what happened first.")
            self.fb_text.setFocus()
            return
        self.fb_send_btn.setEnabled(False)
        self._fb_note("Sending…")
        QApplication.processEvents()
        ok, error = feedback.send(message, self.fb_shots,
                                  kind=self.fb_kind.currentData(),
                                  contact=self.fb_contact.text())
        self.fb_send_btn.setEnabled(True)
        if ok:
            self.fb_text.clear()
            self.fb_shots = []
            self._fb_refresh_shots()
            self._fb_note("Thanks — your report reached us.", ok=True)
        else:
            self._fb_note(error)


    def _new_page(self):
        page = QWidget()
        page.setObjectName("settingsPage")
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 16, 16, 16)
        return page, lay

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        sa = QScrollArea()
        sa.setObjectName("settingsScroll")
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setWidget(inner)
        # Paint the viewport through its palette, NOT a stylesheet. A
        # selector-less setStyleSheet("background: ...") means "* { ... }" for
        # this widget AND every descendant, and a widget stylesheet outranks the
        # application one — which silently repainted every button inside a tab
        # (a #primary button ended up white-on-white).
        pal = sa.viewport().palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(theme.BG))
        pal.setColor(QPalette.ColorRole.Base, QColor(theme.BG))
        sa.viewport().setPalette(pal)
        sa.viewport().setAutoFillBackground(True)
        return sa

    def _heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        f = lbl.font()
        f.setWeight(QFont.Weight.DemiBold)
        lbl.setFont(f)
        return lbl

    def _guide(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("guide")
        lbl.setWordWrap(True)
        return lbl

    # -- behaviour --
    def _update_states(self):
        notif_on = self.enabled_cb.isChecked()
        self.sound_cb.setEnabled(notif_on)
        sound_on = notif_on and self.sound_cb.isChecked()
        self.sound_combo.setEnabled(sound_on)
        self.preview_btn.setEnabled(sound_on)
        self.reminder_combo.setEnabled(notif_on)
        self.popup_pos_combo.setEnabled(notif_on)
        self.auto_dismiss_cb.setEnabled(notif_on)
        self.auto_dismiss_combo.setEnabled(notif_on
                                           and self.auto_dismiss_cb.isChecked())

    def _refresh_usage_bg(self):
        """Fetch the live cross-device account total (caches it in QSettings) and
        signal the UI to re-render the label. Runs off the UI thread; all failures
        stay silent — the label keeps showing the last cached value."""
        try:
            if self._license.fetch_usage() is not None:
                self._usage_refreshed.emit()
        except Exception:  # noqa: BLE001
            pass

    def _refresh_usage_clicked(self):
        """Refresh usage on demand: push this PC's account count, then read the
        license-wide total back. Reporting first is what fixes the common stale
        case — a device whose own accounts were never counted on the server."""
        self.usage_refresh_btn.setEnabled(False)
        self.usage_refresh_btn.setText("⟳ Checking…")
        self.usage_hint.hide()
        db = getattr(self.parent(), "db", None)
        try:
            count = len(db.list_accounts()) if db is not None else None
        except Exception:  # noqa: BLE001
            count = None
        threading.Thread(target=self._refresh_usage_manual, args=(count,),
                         daemon=True).start()

    def _refresh_usage_manual(self, count: int | None):
        """Off the UI thread: talk to the usage endpoint, then report back."""
        ok = False
        try:
            total = (self._license.report_usage(count) if count is not None
                     else self._license.fetch_usage())
            ok = total is not None
        except Exception:  # noqa: BLE001
            ok = False
        try:
            self._usage_checked.emit(ok)
        except RuntimeError:
            pass   # dialog closed while the request was in flight

    def _on_usage_checked(self, ok: bool):
        self.usage_refresh_btn.setEnabled(True)
        self.usage_refresh_btn.setText("⟳ Refresh usage")
        if ok:
            self._refresh_license_label()
            self.usage_hint.setText("✓ Updated just now from the server.")
        else:
            detail = ""
            try:
                detail = self._license.last_usage_error()
            except Exception:  # noqa: BLE001
                pass
            self.usage_hint.setText(
                "⚠ Could not reach the license server — the numbers above are "
                "the last known ones. Check whether a firewall or company "
                "proxy on this PC blocks HTTPS to amail.app."
                + (f"\nDetails: {detail}" if detail else ""))
        self.usage_hint.show()

    def _refresh_license_label(self):
        st = self._license.status()
        if st["state"] in ("active", "unverified"):
            expires = st["expires_at"].strftime("%Y-%m-%d")
            if st.get("used_total") is not None:
                # Spell out what is left as well — "how many can I still add?"
                # is the number users actually care about (the web panel's
                # REMAINING column).
                left = max(int(st["limit"]) - int(st["used_total"]), 0)
                text = (f"Active — {st['used_total']}/{st['limit']} accounts "
                        f"used (all devices), {left} left, expires {expires} "
                        f"({st['days_left']} days left)")
            else:
                text = (f"Active — {st['limit']} accounts, expires {expires} "
                        f"({st['days_left']} days left)")
        elif st["state"] == "expired":
            text = "License expired — renew to keep adding accounts."
        elif st["state"] == "revoked":
            text = "License revoked — contact support."
        else:
            text = "No license activated."
        self.license_label.setText(text)

    def _manage_license(self):
        LicenseDialog(self._license, self).exec()
        self._refresh_license_label()

    def _show_about(self):
        from .about_dialog import AboutDialog
        AboutDialog(self).exec()

    def _backup_now(self):
        with _busy():
            path = backup.run_backup()
        self.last_backup_label.setText(
            f"Last backup: {backup.last_backup_time() or 'never'}")
        if path is not None:
            QMessageBox.information(self, "Backup", f"Backup created: {path}")
        else:
            QMessageBox.warning(self, "Backup", "Backup could not be created.")

    def _update_backup_folder_label(self):
        self.backup_folder_label.setText(
            f"Backup folder: {self._backup_settings.folder}")

    def _open_backup_folder(self):
        from pathlib import Path
        folder = Path(self._backup_settings.folder)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _choose_backup_folder(self):
        from PySide6.QtWidgets import QFileDialog
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose backup folder", self._backup_settings.folder)
        if chosen:
            self._backup_settings.folder = chosen
            self._update_backup_folder_label()
            QMessageBox.information(
                self, "Backup folder",
                f"Backups will now be saved to:\n{chosen}")

    def _export_accounts(self):
        from PySide6.QtWidgets import QFileDialog, QLineEdit
        db = getattr(self.parent(), "db", None)
        if db is None or not db.list_accounts():
            QMessageBox.information(self, "Export accounts",
                                   "No accounts to export yet.")
            return
        pw, ok = QInputDialog.getText(
            self, "Set a password",
            "Set a password to protect this file (you'll need it to import):",
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        pw = pw.strip()
        if len(pw) < 4:
            QMessageBox.warning(self, "Export accounts",
                                "Please use a password of at least 4 characters.")
            return
        confirm, ok = QInputDialog.getText(
            self, "Confirm password", "Type the same password again:",
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        if confirm.strip() != pw:
            QMessageBox.warning(self, "Export accounts",
                                "The two passwords don't match.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save accounts backup",
            f"amail-accounts{account_transfer.FILE_SUFFIX}",
            f"AMail accounts (*{account_transfer.FILE_SUFFIX})")
        if not path:
            return
        try:
            with _busy():
                blob = account_transfer.export_accounts(db, pw)
                with open(path, "wb") as fh:
                    fh.write(blob)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(
            self, "Accounts exported",
            f"{len(db.list_accounts())} account(s) saved to:\n{path}\n\n"
            "Keep this file and its password safe.")

    def _import_accounts(self):
        from PySide6.QtWidgets import QFileDialog, QLineEdit
        parent = self.parent()
        db = getattr(parent, "db", None)
        if db is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open accounts backup", "",
            f"AMail accounts (*{account_transfer.FILE_SUFFIX});;All files (*)")
        if not path:
            return
        pw, ok = QInputDialog.getText(
            self, "Enter password",
            "Enter the password for this accounts file:",
            QLineEdit.EchoMode.Password)
        if not ok:
            return
        # Import must respect the license account limit (cross-device aware).
        lic = getattr(parent, "_license", None)
        max_add = lic.remaining_slots(len(db.list_accounts())) if lic else None
        try:
            with _busy():
                with open(path, "rb") as fh:
                    blob = fh.read()
                added, skipped = account_transfer.import_accounts(
                    db, blob, pw.strip(), max_add=max_add)
        except ValueError as exc:
            QMessageBox.warning(self, "Import accounts", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        if hasattr(parent, "start_imported_accounts"):
            parent.start_imported_accounts(added)
        msg = f"✅ {len(added)} account(s) imported and now syncing."
        if max_add is not None and len(added) >= max_add and skipped:
            msg += ("\n\n⚠ License limit reached — some accounts were skipped. "
                    "Upgrade your license to add more.")
        if skipped:
            msg += (f"\n\n{len(skipped)} skipped (already added or over the "
                    "license limit):\n" + "\n".join(skipped[:10]))
        QMessageBox.information(self, "Import accounts", msg)

    def _restore_backup(self):
        """Pick a backup (each labelled with the date its data goes up to),
        confirm, then hand off to the main window to restore + restart."""
        backups = backup.list_backups()
        if not backups:
            QMessageBox.information(self, "Restore",
                                    "No backups found yet. Use “Backup Now” "
                                    "first.")
            return
        labels = [f"{b['label']}  —  contains all data up to this time"
                  for b in backups]
        choice, ok = QInputDialog.getItem(
            self, "Restore from backup",
            "Pick a backup to restore — each shows the date its data goes "
            "up to:", labels, 0, False)
        if not ok:
            return
        chosen = backups[labels.index(choice)]
        self._confirm_and_restore(chosen["path"], f"from {chosen['label']}")

    def _restore_from_file(self):
        """Restore from a backup (.db) file the user picks from anywhere —
        e.g. one copied off another computer or a USB drive."""
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a backup file to restore", self._backup_settings.folder,
            "Backup database (*.db);;All files (*)")
        if not path:
            return
        if not backup.is_valid_backup(path):
            QMessageBox.warning(
                self, "Restore from file",
                "That file is not a valid AMail backup. Pick a backup "
                "“.db” file (created by Backup Now or on another computer).")
            return
        self._confirm_and_restore(path, "from the chosen file")

    def _confirm_and_restore(self, path, source_desc: str):
        """Shared confirm + hand-off to the main window (which restores and
        restarts). *source_desc* describes where the backup came from."""
        confirm = QMessageBox.warning(
            self, "Restore this backup?",
            f"This will REPLACE all current emails and settings with the "
            f"backup {source_desc}.\n\nYour current data is saved as "
            f"a fresh backup first, so this can be undone. AMail will "
            f"restart to load the restored data.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        parent = self.parent()
        self.accept()  # close settings before the app restarts
        if hasattr(parent, "restore_from_backup"):
            parent.restore_from_backup(path)

    def _save(self):
        self._backup_settings.frequency = self.backup_freq_combo.currentData()
        self._settings.enabled = self.enabled_cb.isChecked()
        self._settings.sound_enabled = self.sound_cb.isChecked()
        self._settings.sound = self.sound_combo.currentText()
        self._settings.reminder_minutes = self.reminder_combo.currentData()
        self._settings.popup_position = self.popup_pos_combo.currentData()
        self._settings.auto_dismiss_enabled = self.auto_dismiss_cb.isChecked()
        self._settings.auto_dismiss_seconds = self.auto_dismiss_combo.currentData()
        self._send_settings.undo_send_seconds = \
            self._undo_choices[self.undo_combo.currentIndex()][0]
        self._reader_settings.load_remote_images = self.remote_img_cb.isChecked()
        self._appearance_settings.show_email = self.show_email_cb.isChecked()
        chosen = self.theme_combo.currentData()
        density = self.density_combo.currentData()
        restyle = chosen != theme.current_theme()
        if restyle:
            theme.set_theme(chosen)  # persist + mutate theme globals
        if density != theme.current_density():
            theme.set_density(density)
            restyle = True
        if restyle:
            app = QApplication.instance()
            if app is not None:
                app.setStyleSheet(theme.build_qss())  # live-apply chrome
            self.appearance_changed.emit()
        self.accept()
