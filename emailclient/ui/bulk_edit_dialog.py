"""Bulk edit selected accounts: change display name / IMAP / SMTP / password
for many accounts at once. Only the fields whose checkbox is ticked are applied;
everything else is left untouched on each selected account."""
from __future__ import annotations

from PySide6.QtGui import QIntValidator
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox, QWidget)


class BulkEditAccountsDialog(QDialog):
    def __init__(self, db, account_ids, parent=None):
        super().__init__(parent)
        self.db = db
        self.account_ids = list(account_ids)
        self._changes: dict = {}
        self.setWindowTitle("Bulk Edit Accounts")
        self.resize(480, 320)
        form = QFormLayout(self)

        head = QLabel(f"Editing {len(self.account_ids)} selected account(s). "
                      "Tick a field to apply it to every one of them.")
        head.setObjectName("guide")
        head.setWordWrap(True)
        form.addRow(head)

        port_validator = QIntValidator(1, 65535)

        # --- display name ---
        self.name_cb = QCheckBox("Set display name")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("same name for all selected accounts")
        form.addRow(self.name_cb, self.name_edit)

        # --- IMAP server ---
        self.imap_cb = QCheckBox("Set IMAP server")
        self.imap_host = QLineEdit()
        self.imap_host.setPlaceholderText("imap.example.com")
        self.imap_port = QLineEdit()
        self.imap_port.setPlaceholderText("993")
        self.imap_port.setValidator(port_validator)
        self.imap_port.setFixedWidth(80)
        form.addRow(self.imap_cb, self._host_port_row(self.imap_host, self.imap_port))

        # --- SMTP server ---
        self.smtp_cb = QCheckBox("Set SMTP server")
        self.smtp_host = QLineEdit()
        self.smtp_host.setPlaceholderText("smtp.example.com")
        self.smtp_port = QLineEdit()
        self.smtp_port.setPlaceholderText("465")
        self.smtp_port.setValidator(port_validator)
        self.smtp_port.setFixedWidth(80)
        form.addRow(self.smtp_cb, self._host_port_row(self.smtp_host, self.smtp_port))

        # --- password ---
        self.pw_cb = QCheckBox("Set password")
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_edit.setPlaceholderText("same app password for all selected accounts")
        form.addRow(self.pw_cb, self.pw_edit)

        warn = QLabel("Changing IMAP/SMTP or the password reconnects every "
                      "selected account.")
        warn.setObjectName("guide")
        warn.setWordWrap(True)
        form.addRow(warn)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

        # Fields are disabled until their checkbox is ticked (opt-in per field).
        for cb, widgets in ((self.name_cb, (self.name_edit,)),
                            (self.imap_cb, (self.imap_host, self.imap_port)),
                            (self.smtp_cb, (self.smtp_host, self.smtp_port)),
                            (self.pw_cb, (self.pw_edit,))):
            for w in widgets:
                w.setEnabled(False)
            cb.toggled.connect(
                lambda on, ws=widgets: [w.setEnabled(on) for w in ws])

    # ------------------------------------------------------------------ helpers

    def _host_port_row(self, host_edit: QLineEdit, port_edit: QLineEdit) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(host_edit, 1)
        row.addWidget(QLabel("Port"))
        row.addWidget(port_edit)
        wrap = QWidget()
        wrap.setLayout(row)
        return wrap

    def _port(self, field: QLineEdit, label: str):
        try:
            p = int(field.text())
            if 1 <= p <= 65535:
                return p
        except (ValueError, TypeError):
            pass
        QMessageBox.warning(self, "Bulk Edit",
                            f"{label} port must be between 1 and 65535.")
        return None

    # ---------------------------------------------------------------- validation

    def _validate_and_accept(self) -> None:
        changes: dict = {}
        if self.name_cb.isChecked():
            name = self.name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "Bulk Edit",
                                    "Enter a display name, or untick that field.")
                return
            changes["name"] = name
        if self.imap_cb.isChecked():
            host = self.imap_host.text().strip()
            if not host:
                QMessageBox.warning(self, "Bulk Edit",
                                    "Enter an IMAP host, or untick that field.")
                return
            port = self._port(self.imap_port, "IMAP")
            if port is None:
                return
            changes["imap_host"] = host
            changes["imap_port"] = port
        if self.smtp_cb.isChecked():
            host = self.smtp_host.text().strip()
            if not host:
                QMessageBox.warning(self, "Bulk Edit",
                                    "Enter an SMTP host, or untick that field.")
                return
            port = self._port(self.smtp_port, "SMTP")
            if port is None:
                return
            changes["smtp_host"] = host
            changes["smtp_port"] = port
        if self.pw_cb.isChecked():
            # Strip whitespace (copy-pasted app passwords carry stray/nbsp spaces).
            pw = "".join(self.pw_edit.text().split())
            if not pw:
                QMessageBox.warning(self, "Bulk Edit",
                                    "Enter a password, or untick that field.")
                return
            changes["password"] = pw
        if not changes:
            QMessageBox.warning(self, "Bulk Edit",
                                "Tick at least one field to change.")
            return
        self._changes = changes
        self.accept()

    def changes(self) -> dict:
        """{name?, imap_host?, imap_port?, smtp_host?, smtp_port?, password?}."""
        return self._changes
