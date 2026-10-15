"""License activation / status dialog."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout)


class LicenseDialog(QDialog):
    """Enter / view the OroStarMail license.

    dismissable=False is the forced-activation mode at startup: the Cancel
    button is hidden — closing the dialog without activating makes exec()
    return 0 and the caller quits the app politely.
    """

    def __init__(self, manager, parent=None, dismissable=True):
        super().__init__(parent)
        self._manager = manager
        self.setWindowTitle("OroStarMail License")
        self.setModal(True)
        self.resize(420, 240)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QLabel("🔑 OroStarMail License")
        f = header.font()
        f.setPointSize(f.pointSize() + 3)
        f.setWeight(QFont.Weight.DemiBold)
        header.setFont(f)
        header.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(header)

        self.status_label = QLabel()
        self.status_label.setObjectName("guide")
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.status_label)
        self._refresh_status()

        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("XXXXX-XXXXX-XXXXX-XXXXX")
        self.key_edit.returnPressed.connect(self._activate)
        layout.addWidget(self.key_edit)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        if dismissable:
            cancel_btn = QPushButton("Cancel")
            cancel_btn.clicked.connect(self.reject)
            btn_row.addWidget(cancel_btn)
        self.activate_btn = QPushButton("Activate")
        self.activate_btn.setObjectName("primary")
        self.activate_btn.setDefault(True)
        self.activate_btn.clicked.connect(self._activate)
        btn_row.addWidget(self.activate_btn)
        layout.addLayout(btn_row)

    def _refresh_status(self):
        st = self._manager.status()
        if st["state"] in ("active", "unverified"):
            expires = st["expires_at"].strftime("%Y-%m-%d")
            if st.get("used_total") is not None:
                self.status_label.setText(
                    f"Active — {st['used_total']}/{st['limit']} accounts "
                    f"used (all devices), expires {expires} "
                    f"({st['days_left']} days left)")
            else:
                self.status_label.setText(
                    f"Active — {st['limit']} accounts, expires {expires} "
                    f"({st['days_left']} days left)")
        elif st["state"] == "expired":
            self.status_label.setText(
                "Your license has expired. Enter a new license key.")
        elif st["state"] == "revoked":
            self.status_label.setText(
                "Your license was revoked. Enter a new license key.")
        else:
            self.status_label.setText(
                "Enter your license key to activate OroStarMail.")

    def _activate(self):
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "License", "Please enter a license key.")
            return
        ok, msg = self._manager.activate(key)
        if not ok:
            QMessageBox.warning(self, "License", msg)
            return
        QMessageBox.information(self, "License", msg)
        self.accept()
