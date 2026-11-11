"""A QTextBrowser that never fetches a resource an email asked for.

QTextDocument loads every ``<img src>`` (and ``background=``) it meets, and
falls back to opening the URL as a local file. A hostile email quoted into a
preview can therefore point at ``file://attacker/share/x.png`` and make
Windows connect to the attacker and leak the login hash, with no click at
all. This viewer answers every resource request itself: ``data:`` images
decode as normal, files the app wrote are served, and everything else gets
a blank 1×1 image so the document never reaches its file fallback.
"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QTextBrowser

from .. import safe_paths


def _blank() -> QImage:
    img = QImage(1, 1, QImage.Format.Format_ARGB32)
    img.fill(0)
    return img


class SafeTextBrowser(QTextBrowser):
    """Read-only HTML view for untrusted mail content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)

    def loadResource(self, type_: int, url: QUrl):  # noqa: N802 (Qt API)
        scheme = url.scheme().lower()
        if scheme == "data":
            # None = "not handled here"; QTextDocument decodes data: itself.
            return None
        real = safe_paths.safe_app_file(url.toString())
        if real:
            img = QImage(real)
            if not img.isNull():
                return img
        # Anything else (file://, UNC, http, unknown): answer with a blank so
        # the document does not try to open it as a local file.
        return _blank()
