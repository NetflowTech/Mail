"""Notification Center: card-styled history of past notifications."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QWidget)

from ..db import Database
from . import theme

ROLE_MESSAGE_ID = Qt.ItemDataRole.UserRole
ROLE_NOTIF_ID = Qt.ItemDataRole.UserRole + 1

_BODY_MAX = 60  # elide notification bodies beyond this many chars


def _format_when(created_at: str | None) -> str:
    """ISO UTC timestamp -> local '06 Jun · 18:00' (raw fallback on parse error)."""
    if not created_at:
        return ""
    try:
        dt = datetime.fromisoformat(created_at).astimezone()
        return dt.strftime("%d %b · %H:%M")
    except ValueError:
        return created_at[:16].replace("T", " ")


def _elide(text: str, limit: int = _BODY_MAX) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


class _NotifCard(QFrame):
    """One notification: accent dot + title/body column + timestamp."""

    def __init__(self, title: str, body: str, when: str, is_read: bool = False):
        super().__init__()
        self.setObjectName("notifCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        # Unread → solid gold dot; read → hollow/transparent (keeps alignment).
        dot = QLabel("●")
        dot.setStyleSheet(
            ("color: transparent;" if is_read else f"color: {theme.GOLD};")
            + " font-size: 9px; border: none;")
        dot.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)
        title_lbl = QLabel(title or "")
        tf = QFont()
        tf.setPixelSize(13)
        # Read notifications are lighter weight + dimmed so unread ones stand out.
        tf.setWeight(QFont.Weight.Normal if is_read else QFont.Weight.DemiBold)
        title_lbl.setFont(tf)
        title_lbl.setStyleSheet(
            f"border: none; color: {theme.TEXT_DIM if is_read else theme.TEXT};")
        col.addWidget(title_lbl)

        body_lbl = QLabel(_elide(body))
        body_lbl.setStyleSheet(
            f"color: {theme.TEXT_DIM}; font-size: 12px; border: none;")
        col.addWidget(body_lbl)

        when_lbl = QLabel(when)
        when_lbl.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px; border: none;")
        when_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        col.addWidget(when_lbl)

        outer.addLayout(col, 1)


class NotifPopup(QDialog):
    """Notification history popup: styled cards, newest first.

    Clicking a card with a real message id emits open_message(id).
    """

    open_message = Signal(int)

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Notifications")
        self.resize(400, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # -- header: title + Clear all --
        header = QWidget()
        header.setObjectName("notifHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 10, 10, 10)
        title = QLabel("🔔 Notifications")
        hf = QFont()
        hf.setPixelSize(14)
        hf.setWeight(QFont.Weight.DemiBold)
        title.setFont(hf)
        hl.addWidget(title)
        hl.addStretch()
        self.read_all_btn = QPushButton("Mark all read")
        self.read_all_btn.setObjectName("notifClear")
        self.read_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.read_all_btn.clicked.connect(self._mark_all_read)
        hl.addWidget(self.read_all_btn)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.setObjectName("notifClear")
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self._clear_all)
        hl.addWidget(self.clear_btn)
        layout.addWidget(header)

        # -- card list --
        self.listw = QListWidget()
        self.listw.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.listw.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.listw.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.listw.setSpacing(4)
        self.listw.setStyleSheet(
            f"QListWidget {{ background: {theme.PANEL}; padding: 6px; }}"
            "QListWidget::item { background: transparent; }")
        self.listw.itemClicked.connect(self._on_item_activated)
        layout.addWidget(self.listw, 1)

        # -- empty state --
        self.empty_lbl = QLabel("No notifications yet")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_lbl.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 13px;"
            f" background: {theme.PANEL};")
        layout.addWidget(self.empty_lbl, 1)
        self.empty_lbl.hide()

    def refresh(self):
        self.listw.clear()
        rows = self.db.list_notifications()
        empty = not rows
        self.empty_lbl.setVisible(empty)
        self.listw.setVisible(not empty)
        self.clear_btn.setEnabled(not empty)
        unread = sum(1 for r in rows if not r["is_read"])
        self.read_all_btn.setEnabled(unread > 0)
        self.read_all_btn.setText(
            f"Mark all read ({unread})" if unread else "Mark all read")
        for r in rows:
            card = _NotifCard(r["title"], r["body"],
                              _format_when(r["created_at"]),
                              is_read=bool(r["is_read"]))
            item = QListWidgetItem()
            item.setData(ROLE_MESSAGE_ID, r["message_id"])
            item.setData(ROLE_NOTIF_ID, r["id"])
            item.setSizeHint(card.sizeHint())
            self.listw.addItem(item)
            self.listw.setItemWidget(item, card)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def _clear_all(self):
        self.db.clear_notifications()
        self.refresh()

    def _mark_all_read(self):
        self.db.mark_all_notifications_read()
        self.refresh()

    def _on_item_activated(self, item):
        nid = item.data(ROLE_NOTIF_ID)
        if nid is not None:
            self.db.mark_notification_read(nid)
        mid = item.data(ROLE_MESSAGE_ID)
        if mid is not None and mid > 0:
            self.open_message.emit(mid)
            self.close()
        else:
            self.refresh()   # non-email notice: just reflect the read state
