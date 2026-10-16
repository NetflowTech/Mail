"""The one-click 'Update available' dialog with download progress."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar,
                               QPushButton, QVBoxLayout)

from . import theme


class UpdateDialog(QDialog):
    def __init__(self, manifest: dict, parent=None):
        super().__init__(parent)
        ver = str(manifest.get("version", "?"))
        self.setWindowTitle(f"Update available — {ver}")
        self.setMinimumWidth(380)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 16)
        lay.setSpacing(10)

        title = QLabel(f"AMail {ver} is available")
        f = title.font()
        f.setPointSize(15)
        f.setBold(True)
        title.setFont(f)
        lay.addWidget(title)

        notes = QLabel(str(manifest.get("notes", "")).strip() or "Bug fixes and improvements.")
        notes.setWordWrap(True)
        notes.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(notes)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        row = QHBoxLayout()
        row.addStretch(1)
        self.later_btn: QPushButton | None = None
        if not manifest.get("mandatory"):
            self.later_btn = QPushButton("Later")
            self.later_btn.clicked.connect(self.reject)
            row.addWidget(self.later_btn)
        self.install_btn = QPushButton("Install now")
        self.install_btn.setObjectName("primary")
        self.install_btn.clicked.connect(self.accept)
        row.addWidget(self.install_btn)
        lay.addLayout(row)

    def set_progress(self, ratio: float | None) -> None:
        """Show a download bar; call from the UI thread. ratio=None → indeterminate."""
        self.progress.setVisible(True)
        self.install_btn.setEnabled(False)
        if ratio is not None:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(ratio * 100))
        else:
            self.progress.setRange(0, 0)  # indeterminate
