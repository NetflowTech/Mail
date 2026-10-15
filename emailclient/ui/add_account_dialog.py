"""Step-by-step Add Account wizard.

Step 1: pick a provider from a grid of big cards.
Step 2: enter the account details (email / password / sync depth, plus
IMAP/SMTP hosts for "Other").

The public API is unchanged from the old single-page dialog so callers keep
working: ``AddAccountDialog(parent)``, ``.exec()``, ``.account()`` and the
``.password`` QLineEdit attribute (read by MainWindow.add_account).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFormLayout,
                               QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from ..config import PROVIDERS
from ..models import Account

GUIDE = {
    "gmail": "Gmail: Google Account → Security → turn on 2-Step Verification,\n"
             "then create a new password under 'App passwords' and paste it here.",
    "zoho": "Zoho: Accounts → Security → App Passwords → Generate new password.",
    "yahoo": "Yahoo: Account Security → 'Generate app password' to create a password.",
    "outlook": "Outlook/Hotmail/Office365: no password needed — clicking Add opens your browser;"
               " sign in to Microsoft there and click Allow.",
    "custom": "Webmail/cPanel/any IMAP: copy the IMAP/SMTP settings from your hosting's "
              "Email section (usually mail.yourdomain.com, IMAP port 993, SMTP 465).",
}

GMAIL_GOOGLE_GUIDE = ("Gmail: no password needed — clicking Add opens your browser;"
                      " sign in to Google there and click Allow.")

SYNC_DEPTH_OPTIONS = [
    ("Auto — fast start, full sync in background (recommended)", "auto"),
    ("Last 50 (fast)", "50"),
    ("Last 7 days", "7d"),
    ("Last 30 days", "30d"),
    ("Last 90 days", "90d"),
    ("Last 1 year", "365d"),
    ("All emails (may be slow)", "all"),
]

# (provider key, card title, card caption)
_PROVIDER_CARDS = [
    ("gmail", "Gmail", "Sign in with Google (browser) — or app password"),
    ("outlook", "Outlook / Hotmail", "Sign in with Microsoft browser login"),
    ("yahoo", "Yahoo", "Sign in with Yahoo app password"),
    ("zoho", "Zoho", "Sign in with Zoho app password"),
    ("custom", "Other (any IMAP email)",
     "Webmail, cPanel, custom domain — works with any provider"),
]

_TITLES = {
    "gmail": "Gmail", "outlook": "Outlook / Hotmail", "yahoo": "Yahoo",
    "zoho": "Zoho", "custom": "Other (IMAP)",
}

_APP_PW_PROVIDERS = ("gmail", "yahoo", "zoho")

_APP_PW_VENDOR = {"gmail": "Google", "yahoo": "Yahoo", "zoho": "Zoho"}


class _ProviderCard(QPushButton):
    """Big selectable card: bold title + dim caption underneath."""

    def __init__(self, title: str, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("providerCard")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(220, 76)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)
        title_lbl = QLabel(title)
        f = title_lbl.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        title_lbl.setFont(f)
        cap_lbl = QLabel(caption)
        cap_lbl.setObjectName("guide")
        cap_lbl.setWordWrap(True)
        for lbl in (title_lbl, cap_lbl):
            lbl.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lay.addWidget(lbl)


class AddAccountDialog(QDialog):
    def __init__(self, parent=None, db=None):
        super().__init__(parent)
        self.setWindowTitle("Add Account")
        self._provider = "gmail"
        self._db = db
        self._last_group_gid = None

        root = QVBoxLayout(self)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)

        self.stack.addWidget(self._build_provider_page())
        self.stack.addWidget(self._build_details_page())

        # --- navigation row -------------------------------------------------
        self.back_btn = QPushButton("← Back")
        self.back_btn.clicked.connect(self._go_back)
        self.next_btn = QPushButton("Next →")
        self.next_btn.setEnabled(False)
        self.next_btn.clicked.connect(self._go_next)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.add_btn = QPushButton("Add account")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self._validate_and_accept)

        nav = QHBoxLayout()
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(cancel_btn)
        nav.addWidget(self.next_btn)
        nav.addWidget(self.add_btn)
        root.addLayout(nav)

        self._sync_nav()
        self._apply_provider("gmail", refresh_only=True)

    # ------------------------------------------------------------ step 1 page

    def _build_provider_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setSpacing(10)
        header = QLabel("Choose your email provider")
        f = header.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 3)
        header.setFont(f)
        lay.addWidget(header)

        grid = QGridLayout()
        grid.setSpacing(10)
        self._card_group = QButtonGroup(self)
        self._card_group.setExclusive(True)
        self._cards: dict[str, _ProviderCard] = {}
        for i, (key, title, caption) in enumerate(_PROVIDER_CARDS):
            card = _ProviderCard(title, caption)
            card.clicked.connect(
                lambda _=False, k=key: self._on_card_clicked(k))
            self._card_group.addButton(card)
            self._cards[key] = card
            grid.addWidget(card, i // 2, i % 2)
        # Bulk entry: closes the wizard and opens the bulk-add dialog
        self.bulk_requested = False
        bulk_card = _ProviderCard(
            "⊕ Bulk add (many accounts)",
            "Paste a list — email:password per line, all added at once")
        bulk_card.clicked.connect(self._on_bulk_clicked)
        grid.addWidget(bulk_card, len(_PROVIDER_CARDS) // 2,
                       len(_PROVIDER_CARDS) % 2)
        lay.addLayout(grid)
        lay.addStretch(1)
        return page

    def _reload_groups(self, select_gid=None):
        """Rebuild the group dropdown: (No group), each group, + Create new."""
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItem("(No group)", None)
        if self._db is not None:
            for g in self._db.list_groups():
                self.group_box.addItem(f"{g['icon']} {g['name']}", g["id"])
        self.group_box.addItem("➕ Create new group…", "__new__")
        idx = 0
        if select_gid is not None:
            found = self.group_box.findData(select_gid)
            if found >= 0:
                idx = found
        self.group_box.setCurrentIndex(idx)
        self.group_box.blockSignals(False)
        self._last_group_gid = self.group_box.currentData()

    def _on_group_activated(self, index):
        if self.group_box.itemData(index) != "__new__":
            self._last_group_gid = self.group_box.itemData(index)
            return
        if self._db is None:
            self._reload_groups(select_gid=self._last_group_gid)
            return
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        name = name.strip() if ok else ""
        if name:
            gid = self._db.add_group(name)
            self._reload_groups(select_gid=gid)
        else:  # cancelled / empty — revert to the previous choice
            self._reload_groups(select_gid=self._last_group_gid)

    def group_id(self):
        """Chosen group id, or None for no group."""
        data = self.group_box.currentData()
        return data if isinstance(data, int) else None

    def _on_bulk_clicked(self):
        self.bulk_requested = True
        self.reject()  # caller checks the flag and opens the bulk dialog

    def _on_card_clicked(self, provider: str):
        self._apply_provider(provider)
        self.stack.setCurrentIndex(1)
        self._sync_nav()
        self.email.setFocus()

    # ------------------------------------------------------------ step 2 page

    def _build_details_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setSpacing(8)

        self._details_header = QLabel("Account details")
        f = self._details_header.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 3)
        self._details_header.setFont(f)
        outer.addWidget(self._details_header)

        form = QFormLayout()
        self._form = form

        self.email = QLineEdit()
        self.email.setPlaceholderText("address@gmail.com")

        # Gmail login-method choice (browser OAuth vs app password)
        self._google_radio = QRadioButton(
            "Sign in with Google in browser (recommended)")
        self._google_radio.setChecked(True)
        self._app_pw_radio = QRadioButton("Use an app password")
        self._login_group = QButtonGroup(self)
        self._login_group.addButton(self._google_radio)
        self._login_group.addButton(self._app_pw_radio)
        login_lay = QVBoxLayout()
        login_lay.setSpacing(2)
        login_lay.addWidget(self._google_radio)
        login_lay.addWidget(self._app_pw_radio)
        self._login_choice_wrap = QWidget()
        self._login_choice_wrap.setLayout(login_lay)
        self._login_label = QLabel("Login method")
        self._google_radio.toggled.connect(self._on_login_method_changed)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("App password")
        self._pw_label = QLabel("App password")
        # Eye toggle: let the user reveal what they typed (default hidden).
        self._pw_show = QPushButton("👁")
        self._pw_show.setCheckable(True)
        self._pw_show.setFixedWidth(40)
        self._pw_show.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pw_show.setToolTip("Show / hide password")
        self._pw_show.toggled.connect(
            lambda on: self.password.setEchoMode(
                QLineEdit.EchoMode.Normal if on
                else QLineEdit.EchoMode.Password))
        _pw_row = QHBoxLayout()
        _pw_row.setContentsMargins(0, 0, 0, 0)
        _pw_row.addWidget(self.password, 1)
        _pw_row.addWidget(self._pw_show)
        self._pw_wrap = QWidget()
        self._pw_wrap.setLayout(_pw_row)

        self._pw_caption = QLabel("")
        self._pw_caption.setObjectName("guide")
        self._pw_caption.setWordWrap(True)
        self._pw_hint = QLabel("<a href='#'>How to create one</a>")
        self._pw_hint.setObjectName("guide")
        self._pw_hint.linkActivated.connect(self._show_app_pw_help)
        pw_help_row = QVBoxLayout()
        pw_help_row.setSpacing(0)
        pw_help_row.addWidget(self._pw_caption)
        pw_help_row.addWidget(self._pw_hint)
        self._pw_help_wrap = QWidget()
        self._pw_help_wrap.setLayout(pw_help_row)

        self.name = QLineEdit()
        self.name.setPlaceholderText("Optional — e.g. Work Gmail 1")

        self.sync_depth = QComboBox()
        for label, value in SYNC_DEPTH_OPTIONS:
            self.sync_depth.addItem(label, value)

        # Group selector: assign this account to a group, or make a new one.
        self.group_box = QComboBox()
        self._reload_groups()
        self.group_box.activated.connect(self._on_group_activated)

        # Custom IMAP/SMTP fields
        port_validator = QIntValidator(1, 65535, self)
        self.imap_host = QLineEdit()
        self.imap_host.setPlaceholderText("e.g. mail.yourdomain.com")
        self.imap_port = QLineEdit()
        self.imap_port.setPlaceholderText("993")
        self.imap_port.setValidator(port_validator)
        self.smtp_host = QLineEdit()
        self.smtp_host.setPlaceholderText("e.g. mail.yourdomain.com")
        self.smtp_port = QLineEdit()
        self.smtp_port.setPlaceholderText("465")
        self.smtp_port.setValidator(port_validator)

        self.guide = QLabel(GUIDE["gmail"])
        self.guide.setObjectName("guide")
        self.guide.setWordWrap(True)

        form.addRow("Email", self.email)
        form.addRow(self._login_label, self._login_choice_wrap)
        form.addRow(self._pw_label, self._pw_wrap)
        form.addRow("", self._pw_help_wrap)
        form.addRow("Display name", self.name)
        form.addRow("How far back to sync", self.sync_depth)
        form.addRow("Add to group", self.group_box)
        form.addRow("IMAP host", self.imap_host)
        form.addRow("IMAP port", self.imap_port)
        form.addRow("SMTP host", self.smtp_host)
        form.addRow("SMTP port", self.smtp_port)
        form.addRow(self.guide)

        outer.addLayout(form)
        outer.addStretch(1)
        return page

    # ----------------------------------------------------------------- helpers

    def _sync_nav(self):
        on_details = self.stack.currentIndex() == 1
        self.back_btn.setVisible(on_details)
        self.next_btn.setVisible(not on_details)
        self.next_btn.setEnabled(self._card_group.checkedButton() is not None)
        self.add_btn.setVisible(on_details)

    def _go_back(self):
        self.stack.setCurrentIndex(0)
        self._sync_nav()

    def _go_next(self):
        if self._card_group.checkedButton() is None:
            return
        self.stack.setCurrentIndex(1)
        self._sync_nav()
        self.email.setFocus()

    def _show_app_pw_help(self, _link=""):
        QMessageBox.information(
            self, "App password",
            GUIDE.get(self._provider, GUIDE["custom"]))

    def _set_custom_fields_visible(self, visible: bool) -> None:
        for widget in (self.imap_host, self.imap_port,
                       self.smtp_host, self.smtp_port):
            widget.setVisible(visible)
            label = self._form.labelForField(widget)
            if label:
                label.setVisible(visible)

    def _set_password_visible(self, visible: bool) -> None:
        self._pw_wrap.setVisible(visible)
        self._pw_label.setVisible(visible)
        self._pw_help_wrap.setVisible(visible)

    @property
    def use_google_login(self) -> bool:
        """True when the user picked browser sign-in (only meaningful for gmail)."""
        return self._provider == "gmail" and self._google_radio.isChecked()

    def _on_login_method_changed(self, _checked: bool = False) -> None:
        if self._provider != "gmail":
            return
        self._set_password_visible(not self.use_google_login)
        self.guide.setText(GMAIL_GOOGLE_GUIDE if self.use_google_login
                           else GUIDE["gmail"])

    def _apply_provider(self, provider: str, refresh_only: bool = False) -> None:
        self._provider = provider
        if not refresh_only and provider in self._cards:
            self._cards[provider].setChecked(True)
        title = _TITLES.get(provider, provider)
        self._details_header.setText(f"Account details — {title}")
        self.guide.setText(GUIDE.get(provider, ""))
        is_custom = (provider == "custom")
        self._set_custom_fields_visible(is_custom)
        is_gmail = (provider == "gmail")
        self._login_choice_wrap.setVisible(is_gmail)
        self._login_label.setVisible(is_gmail)
        if is_gmail:
            self._on_login_method_changed()
        else:
            self._set_password_visible(provider != "outlook")

        if provider in _APP_PW_PROVIDERS:
            vendor = _APP_PW_VENDOR[provider]
            self._pw_label.setText("App password")
            self.password.setPlaceholderText("App password")
            self._pw_caption.setText(
                f"{vendor} requires an app password"
                " (your regular password won't work).")
            self._pw_hint.setVisible(True)
        elif provider == "custom":
            self._pw_label.setText("Password")
            self.password.setPlaceholderText("Password")
            self._pw_caption.setText(
                "Use your normal mailbox password (the one you set in "
                "Hostinger / cPanel webmail). No app password needed.")
            self._pw_hint.setVisible(False)

        # Pre-fill port fields from preset
        preset = PROVIDERS.get(provider, {})
        self.imap_port.setText(str(preset.get("imap_port", 993)))
        self.smtp_port.setText(str(preset.get("smtp_port", 465)))

    # ---------------------------------------------------------------- validation

    def _validate_and_accept(self) -> None:
        email = self.email.text().strip()
        password = self.password.text()
        provider = self._provider

        if "@" not in email:
            QMessageBox.warning(self, "Add Account",
                                "The email must contain '@'. Enter a valid address.")
            return

        # Already added on THIS computer? Tell the user clearly (case-insensitive)
        # instead of failing later on the DB unique constraint.
        if self._db is not None:
            for a in self._db.list_accounts():
                if a.email.lower() == email.lower():
                    QMessageBox.warning(
                        self, "Already added",
                        f"{email} is already added on this PC (this system).\n\n"
                        "The same email can't be added twice on one computer.")
                    return

        if not password and provider != "outlook" and not self.use_google_login:
            QMessageBox.warning(self, "Add Account",
                                "App password cannot be empty."
                                if provider in _APP_PW_PROVIDERS
                                else "Password cannot be empty.")
            return

        if provider == "custom":
            if not self.imap_host.text().strip():
                QMessageBox.warning(self, "Add Account",
                                    "IMAP host is required for a custom provider.")
                return
            if not self.smtp_host.text().strip():
                QMessageBox.warning(self, "Add Account",
                                    "SMTP host is required for a custom provider.")
                return
            try:
                imap_p = int(self.imap_port.text())
                if not (1 <= imap_p <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                QMessageBox.warning(self, "Add Account",
                                    "IMAP port must be between 1 and 65535.")
                return
            try:
                smtp_p = int(self.smtp_port.text())
                if not (1 <= smtp_p <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                QMessageBox.warning(self, "Add Account",
                                    "SMTP port must be between 1 and 65535.")
                return

        self.accept()

    # ------------------------------------------------------------------ account

    def account(self) -> Account:
        p = self._provider
        preset = PROVIDERS[p]
        # New accounts default to the theme accent (empty color) so the whole
        # app stays visually consistent; users can set a per-account color later
        # in Account Settings.
        color = ""
        if p == "custom":
            imap_host = self.imap_host.text().strip()
            smtp_host = self.smtp_host.text().strip()
            imap_port = int(self.imap_port.text())
            smtp_port = int(self.smtp_port.text())
        else:
            imap_host = preset["imap_host"]
            imap_port = preset["imap_port"]
            smtp_host = preset["smtp_host"]
            smtp_port = preset["smtp_port"]
        email = self.email.text().strip()
        default_name = email.split("@")[0] if "@" in email else email
        return Account(id=None,
                       name=self.name.text().strip() or default_name,
                       email=email,
                       provider=p,
                       imap_host=imap_host,
                       imap_port=imap_port,
                       smtp_host=smtp_host,
                       smtp_port=smtp_port,
                       color=color,
                       sync_depth=self.sync_depth.currentData() or "50")
