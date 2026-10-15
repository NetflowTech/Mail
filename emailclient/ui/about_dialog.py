"""About AMail — company and contact information."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPixmap
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout

from .. import __version__
from ..paths import asset_dir
from . import theme


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About AMail")
        self.setFixedSize(380, 460)

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.setContentsMargins(28, 24, 28, 20)

        logo = QLabel()
        pm = QPixmap(str(asset_dir() / "AMail-logo-Exo2.svg"))
        if not pm.isNull():
            logo.setPixmap(pm.scaled(
                110, 110, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)

        name = QLabel("AMail")
        f = name.font()
        f.setPointSize(20)
        f.setWeight(QFont.Weight.Bold)
        name.setFont(f)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setStyleSheet(f"color: {theme.GOLD_DEEP};")
        lay.addWidget(name)

        ver = QLabel(f"Version {__version__}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        lay.addWidget(ver)

        tagline = QLabel("All your email accounts. One professional inbox.")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tagline.setWordWrap(True)
        tagline.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(tagline)
        lay.addSpacing(10)

        company = QLabel(
            "Developed by <b>AMail</b><br><br>"
            f"<a style='color:{theme.GOLD_DEEP}' "
            "href='mailto:info@amail.app'>info@amail.app</a><br>"
            f"<a style='color:{theme.GOLD_DEEP}' "
            "href='https://amail.app'>amail.app</a>")
        company.setAlignment(Qt.AlignmentFlag.AlignCenter)
        company.setOpenExternalLinks(True)
        company.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(company)

        lay.addStretch(1)
        copyright_ = QLabel("© 2026 AMail. All rights reserved.")
        copyright_.setAlignment(Qt.AlignmentFlag.AlignCenter)
        copyright_.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        lay.addWidget(copyright_)

        check = QPushButton("Check for updates")
        check.clicked.connect(self._check_updates)
        lay.addWidget(check, 0, Qt.AlignmentFlag.AlignCenter)

        ok = QPushButton("OK")
        ok.setObjectName("primary")
        ok.clicked.connect(self.accept)
        lay.addWidget(ok, 0, Qt.AlignmentFlag.AlignCenter)

    def _check_updates(self):
        from PySide6.QtWidgets import QMessageBox

        from .. import __version__, updater
        from .update_flow import run_update_flow
        manifest = updater.check_only(__version__)
        if manifest is None:
            QMessageBox.information(self, "Up to date",
                                    "You're running the latest version.")
            return
        # Close this dialog (and its nested exec() loop) before launching the
        # flow. If run_update_flow() later calls QApplication.quit() while
        # our exec() loop is still active, quit() is swallowed and the app
        # never exits. Parent the flow to our own parent (the main window),
        # which runs on the top-level, non-nested event loop.
        parent = self.parent()
        self.accept()
        run_update_flow(parent, manifest)
