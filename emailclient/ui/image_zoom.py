"""Full-size image viewer for inline email images.

The reader runs with JavaScript disabled (email HTML is untrusted), so images
can't be made zoomable in-page. Instead each body image is wrapped in a link the
reader intercepts, and clicking it opens this native dialog — fit-to-window by
default, with zoom in/out and actual-size, plus Ctrl+wheel to zoom.
"""
from __future__ import annotations

import base64
import urllib.parse

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout)

from . import theme


def pixmap_from_src(src: str) -> QPixmap:
    """Load a QPixmap from an <img src> — a data: URI, file path, or file:// URL.
    Remote (http) images aren't fetched (the reader blocks them too)."""
    pm = QPixmap()
    try:
        if src.startswith("data:"):
            head, _, payload = src.partition(",")
            if ";base64" in head:
                pm.loadFromData(base64.b64decode(payload))
            else:
                pm.loadFromData(urllib.parse.unquote_to_bytes(payload))
        elif src.startswith("file://"):
            from PySide6.QtCore import QUrl
            pm = QPixmap(QUrl(src).toLocalFile())
        elif not src.startswith(("http://", "https://")):
            pm = QPixmap(src)
    except Exception:
        pass
    return pm


class ImageZoomDialog(QDialog):
    """Show *pixmap* with fit/actual/zoom controls."""

    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Image")
        self.resize(900, 680)
        self._orig = pixmap
        self._scale = 1.0
        self._fit = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._label = QLabel()
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setWidget(self._label)
        self._scroll.setStyleSheet(f"background:{theme.PANEL};border:none;")
        lay.addWidget(self._scroll, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 8)
        bar.setSpacing(8)
        bar.addStretch(1)
        for text, slot in (("Fit", self._do_fit), ("100%", self._do_actual),
                           ("−", self._zoom_out), ("+", self._zoom_in)):
            b = QPushButton(text)
            b.setFixedWidth(52 if len(text) > 1 else 36)
            b.clicked.connect(slot)
            bar.addWidget(b)
        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        bar.addWidget(close)
        bar.addStretch(1)
        lay.addLayout(bar)

        self._apply()

    # -- rendering -----------------------------------------------------------
    def _apply(self):
        if self._orig.isNull():
            self._label.setText("Couldn't load this image.")
            return
        if self._fit:
            area = self._scroll.viewport().size()
            pm = self._orig.scaled(
                max(area.width() - 4, 1), max(area.height() - 4, 1),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
        else:
            w = max(int(self._orig.width() * self._scale), 1)
            pm = self._orig.scaledToWidth(
                w, Qt.TransformationMode.SmoothTransformation)
        self._label.setPixmap(pm)

    def _do_fit(self):
        self._fit = True
        self._apply()

    def _do_actual(self):
        self._fit = False
        self._scale = 1.0
        self._apply()

    def _zoom_in(self):
        self._fit = False
        self._scale = min(self._scale * 1.25, 8.0)
        self._apply()

    def _zoom_out(self):
        self._fit = False
        self._scale = max(self._scale / 1.25, 0.1)
        self._apply()

    # -- interaction ---------------------------------------------------------
    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            (self._zoom_in if e.angleDelta().y() > 0 else self._zoom_out)()
            e.accept()
        else:
            super().wheelEvent(e)

    def resizeEvent(self, e):
        if self._fit:
            self._apply()
        super().resizeEvent(e)
