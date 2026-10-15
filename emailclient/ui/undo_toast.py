"""Gmail-style "Undo Send" toast.

A small frameless bar pinned to the bottom-centre of the parent window right
after an email is queued. It counts down the Undo-Send window; clicking Undo
recalls the queued email(s) before the outbox worker sends them. When the
countdown elapses (or the toast is replaced) it quietly disappears and the mail
goes out.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from . import theme


class UndoSendToast(QFrame):
    undo_clicked = Signal()   # user hit Undo within the window

    def __init__(self, seconds: int, parent=None):
        super().__init__(parent)
        self._remaining = max(1, int(seconds))
        self.setObjectName("undoToast")
        self.setStyleSheet(f"""
            QFrame#undoToast {{
                background: {theme.TEXT};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
            QLabel {{ color: white; background: transparent; font-size: 13px; }}
            QPushButton {{
                color: {theme.GOLD}; background: transparent; border: none;
                font-weight: 700; font-size: 13px; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: white; }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 8, 10, 8)
        lay.setSpacing(12)
        self._label = QLabel()
        lay.addWidget(self._label)
        self._btn = QPushButton("Undo")
        self._btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn.clicked.connect(self._on_undo)
        lay.addWidget(self._btn)
        close = QPushButton("✕")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.finish)
        lay.addWidget(close)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._refresh()

    # -- lifecycle ----------------------------------------------------------
    def start(self):
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start()

    def _tick(self):
        self._remaining -= 1
        if self._remaining <= 0:
            self.finish()          # window elapsed → email sends, toast goes away
        else:
            self._refresh()

    def _refresh(self):
        self._label.setText(f"Sending email in {self._remaining}s")

    def _on_undo(self):
        self.undo_clicked.emit()
        self.finish()

    def finish(self):
        self._timer.stop()
        self.hide()
        self.deleteLater()

    # -- placement ----------------------------------------------------------
    def _reposition(self):
        self.adjustSize()
        p = self.parent()
        if p is None:
            return
        pw, ph = p.width(), p.height()
        w = self.width()
        self.move((pw - w) // 2, ph - self.height() - 28)

    def showEvent(self, e):
        self._reposition()
        super().showEvent(e)
