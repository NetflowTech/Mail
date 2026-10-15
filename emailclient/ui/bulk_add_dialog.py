"""Bulk account add: paste many `email:apppassword` lines, one provider for all."""
from __future__ import annotations

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QPlainTextEdit)

from ..config import PROVIDERS
from .add_account_dialog import SYNC_DEPTH_OPTIONS


def parse_bulk_lines(text: str) -> tuple[list[dict], list[str]]:
    """Parse `email:apppassword` lines (split on FIRST colon).

    Returns (entries, bad_lines). entries: [{"email":..., "password":...}].
    """
    entries, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        email, sep, password = line.partition(":")
        email = email.strip()
        password = password.strip()
        if not sep or "@" not in email or not password:
            bad.append(line)
            continue
        entries.append({"email": email, "password": password})
    return entries, bad


class BulkAddDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Bulk Add Accounts")
        self.resize(440, 420)
        self._entries: list[dict] = []
        form = QFormLayout(self)

        self.provider = QComboBox()
        self.provider.addItems(["gmail", "zoho", "yahoo", "custom"])

        # Optional common display name for every account added in this batch.
        # Blank = each account is named after its email (the part before @).
        self.display_name = QLineEdit()
        self.display_name.setPlaceholderText("(optional) same name for all — blank = from email")

        self.sync_depth = QComboBox()
        for label, value in SYNC_DEPTH_OPTIONS:
            self.sync_depth.addItem(label, value)

        # Group selector: assign all bulk-added accounts to one group, or make
        # a new group on the spot. data is the gid (int), None, or "__new__".
        self.group_box = QComboBox()
        self._last_group_gid = None
        self._reload_groups()
        self.group_box.activated.connect(self._on_group_activated)

        port_validator = QIntValidator(1, 65535)
        self.imap_host = QLineEdit()
        self.imap_host.setPlaceholderText("e.g. mail.yourdomain.com")
        self.imap_port = QLineEdit()
        self.imap_port.setPlaceholderText("993")
        self.imap_port.setValidator(port_validator)
        self.imap_port.setText("993")
        self.smtp_host = QLineEdit()
        self.smtp_host.setPlaceholderText("e.g. mail.yourdomain.com")
        self.smtp_port = QLineEdit()
        self.smtp_port.setPlaceholderText("465")
        self.smtp_port.setValidator(port_validator)
        self.smtp_port.setText("465")

        self.lines = QPlainTextEdit()
        self.lines.setPlaceholderText("email:apppassword\nemail2:apppassword2")

        guide = QLabel("One account per line — email:apppassword")
        guide.setObjectName("guide")
        guide.setWordWrap(True)

        form.addRow("Provider", self.provider)
        form.addRow("Display name", self.display_name)
        form.addRow("How far back to sync emails", self.sync_depth)
        form.addRow("Add to group", self.group_box)
        form.addRow("IMAP host", self.imap_host)
        form.addRow("IMAP port", self.imap_port)
        form.addRow("SMTP host", self.smtp_host)
        form.addRow("SMTP port", self.smtp_port)
        form.addRow(self.lines)
        form.addRow(guide)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        # IMAP/SMTP are always shown (prefilled from the provider preset) so the
        # mail server can be overridden even for a known provider like zoho.
        self.provider.currentTextChanged.connect(self._on_provider_changed)
        self._fill_from_preset(self.provider.currentText())

    # ------------------------------------------------------------------ helpers

    def _reload_groups(self, select_gid=None) -> None:
        """Rebuild the group dropdown: (No group), each group, + Create new."""
        self.group_box.blockSignals(True)
        self.group_box.clear()
        self.group_box.addItem("(No group)", None)
        for g in self.db.list_groups():
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

    def _on_group_activated(self, index: int) -> None:
        if self.group_box.itemData(index) != "__new__":
            self._last_group_gid = self.group_box.itemData(index)
            return
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        name = name.strip() if ok else ""
        if name:
            gid = self.db.add_group(name)
            self._reload_groups(select_gid=gid)
        else:  # cancelled / empty — revert to the previous choice
            self._reload_groups(select_gid=self._last_group_gid)

    def _fill_from_preset(self, provider: str) -> None:
        """Prefill the IMAP/SMTP fields from the provider's preset (empty for
        'custom' so the user types their own server)."""
        preset = PROVIDERS.get(provider, {})
        self.imap_host.setText(str(preset.get("imap_host", "")))
        self.imap_port.setText(str(preset.get("imap_port", 993)))
        self.smtp_host.setText(str(preset.get("smtp_host", "")))
        self.smtp_port.setText(str(preset.get("smtp_port", 465)))

    def _on_provider_changed(self, provider: str) -> None:
        self._fill_from_preset(provider)

    # ---------------------------------------------------------------- validation

    def _validate_and_accept(self) -> None:
        entries, bad = parse_bulk_lines(self.lines.toPlainText())
        if not entries:
            QMessageBox.warning(self, "Bulk Add",
                                "No valid lines found — write "
                                "email:apppassword on each line.")
            return
        # IMAP/SMTP host/port are editable for every provider now, so validate
        # them for all (not just 'custom').
        if not self.imap_host.text().strip() or not self.smtp_host.text().strip():
            QMessageBox.warning(self, "Bulk Add",
                                "IMAP and SMTP host are required.")
            return
        for port_field, name in ((self.imap_port, "IMAP"), (self.smtp_port, "SMTP")):
            try:
                p = int(port_field.text())
                if not (1 <= p <= 65535):
                    raise ValueError
            except (ValueError, TypeError):
                QMessageBox.warning(self, "Bulk Add",
                                    f"{name} port must be between 1 and 65535.")
                return
        if bad:
            QMessageBox.warning(self, "Bulk Add",
                                "These lines are invalid (email:apppassword "
                                "format required):\n" + "\n".join(bad[:10]))
            return
        self._entries = entries
        self.accept()

    # ------------------------------------------------------------------ results

    def entries(self) -> list[dict]:
        """Validated [{"email":..., "password":...}] from the OK'd dialog."""
        return self._entries

    def group_id(self):
        """Chosen group id for all entries, or None for no group."""
        data = self.group_box.currentData()
        return data if isinstance(data, int) else None

    def chosen_display_name(self) -> str:
        """Optional common display name for every account (blank = from email)."""
        return self.display_name.text().strip()

    def provider_preset(self) -> dict:
        """Provider name + connection + sync depth for all entries. IMAP/SMTP come
        from the (editable, possibly overridden) fields for every provider."""
        p = self.provider.currentText()
        preset = PROVIDERS[p]
        return {"provider": p,
                "imap_host": self.imap_host.text().strip(),
                "imap_port": int(self.imap_port.text()),
                "smtp_host": self.smtp_host.text().strip(),
                "smtp_port": int(self.smtp_port.text()),
                "color": preset["color"],
                "sync_depth": self.sync_depth.currentData() or "50"}
