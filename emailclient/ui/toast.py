"""Persistent in-app notification window.

A frameless always-on-top stack at the screen's top-right. Each new email
gets a card that STAYS until the user clicks it (opens the email) or
dismisses it with ✕. No OS notification permissions involved.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ..settings import NotifSettings
from . import theme

CARD_W = 360
MAX_HEIGHT = 540


class _Card(QFrame):
    clicked = Signal(int)     # message id
    dismissed = Signal(object)  # self

    def __init__(self, msg_id: int, title: str, subject: str, color: str):
        super().__init__()
        self.msg_id = msg_id
        self.setObjectName("toastCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 10, 8, 10)

        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 10px;"
                          "background: transparent;")
        lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        t = QLabel(title)
        f = QFont()
        f.setWeight(QFont.Weight.DemiBold)
        t.setFont(f)
        t.setWordWrap(True)
        t.setStyleSheet("background: transparent;")
        s = QLabel(subject)
        s.setWordWrap(True)
        s.setStyleSheet(f"color: {theme.TEXT_DIM}; background: transparent;")
        text.addWidget(t)
        text.addWidget(s)
        lay.addLayout(text, 1)

        from .editor_icons import icon as _icon
        x = QPushButton()
        x.setIcon(_icon("close", theme.TEXT_DIM))
        x.setObjectName("toastClose")
        x.setFixedSize(24, 24)
        x.setToolTip("Dismiss — reminds you in 1 minute if still unread")
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.clicked.connect(lambda: self.dismissed.emit(self))
        lay.addWidget(x, 0, Qt.AlignmentFlag.AlignTop)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.msg_id)
        super().mousePressEvent(event)


class NotificationStack(QWidget):
    """Floating card stack; emits message_clicked(msg_id) on card click."""

    message_clicked = Signal(int)
    card_dismissed = Signal(int)   # user pressed ✕ (msg_id > 0 only)
    mark_all_read = Signal(list)   # "Mark all read" pressed: list[msg_id > 0]

    def __init__(self):
        # NOTE: no Qt.Tool — Tool windows vanish when the app loses focus on
        # macOS; this stack must stay visible over other apps (VS Code etc.).
        super().__init__(None,
                         Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedWidth(CARD_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        self._header = QLabel("  📬 OroStarMail — New emails — click to open")
        header_row.addWidget(self._header, 1)
        self._close_all = QPushButton("Close all")
        self._close_all.setObjectName("toastCloseAll")
        self._close_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_all.setToolTip("Dismiss every card at once (same as pressing ✕ on each)")
        self._close_all.clicked.connect(self._on_close_all)
        header_row.addWidget(self._close_all, 0, Qt.AlignmentFlag.AlignVCenter)
        self._mark_all = QPushButton("Mark all read")
        self._mark_all.setObjectName("toastMarkAll")
        self._mark_all.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mark_all.setToolTip("Mark every notified email as read and clear this list")
        self._mark_all.clicked.connect(self._on_mark_all)
        header_row.addWidget(self._mark_all, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header_row)
        self.restyle()

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self._cards_lay = QVBoxLayout(inner)
        self._cards_lay.setContentsMargins(8, 4, 8, 8)
        self._cards_lay.setSpacing(6)
        self._cards_lay.addStretch(1)
        self._scroll.setWidget(inner)
        outer.addWidget(self._scroll)
        self._cards: list[_Card] = []

    # -- public API --
    def restyle(self):
        """Re-apply the (possibly switched) theme. Existing cards keep their
        inline label colors until recreated — acceptable for a toast."""
        self.setStyleSheet(f"""
            QWidget {{ background: {theme.PANEL}; }}
            QFrame#toastCard {{
                background: {theme.BG};
                border: 1px solid {theme.GOLD};
                border-radius: 10px;
            }}
            QFrame#toastCard:hover {{ background: {theme.GOLD_HOVER}; }}
            QPushButton#toastClose {{
                border: none; background: transparent;
                border-radius: 12px;
            }}
            QPushButton#toastClose:hover {{ background: {theme.PANEL_DARK}; }}
        """)
        self._header.setStyleSheet(f"color: {theme.GOLD_DEEP}; font-weight: 600;"
                                   f"padding: 6px 4px;")
        self._mark_all.setStyleSheet(f"""
            QPushButton#toastMarkAll {{
                color: {theme.GOLD_DEEP};
                background: transparent;
                border: 1px solid {theme.GOLD};
                border-radius: 8px;
                padding: 3px 10px;
                margin: 4px 8px 4px 0;
                font-weight: 600;
            }}
            QPushButton#toastMarkAll:hover {{ background: {theme.GOLD_HOVER}; }}
        """)
        self._close_all.setStyleSheet(f"""
            QPushButton#toastCloseAll {{
                color: {theme.TEXT_DIM};
                background: transparent;
                border: 1px solid {theme.BORDER};
                border-radius: 8px;
                padding: 3px 10px;
                margin: 4px 6px 4px 0;
                font-weight: 600;
            }}
            QPushButton#toastCloseAll:hover {{ background: {theme.PANEL_DARK}; }}
        """)

    def push(self, msg_id: int, title: str, subject: str, color: str):
        card = _Card(msg_id, title, subject, color)
        card.clicked.connect(self._on_card_clicked)
        card.dismissed.connect(self._on_card_dismissed)
        self._cards_lay.insertWidget(0, card)
        self._cards.append(card)
        self._arm_auto_dismiss(card)
        # A sync burst pushes many cards in one go. Repositioning, showing and
        # re-pinning the window on EVERY push made that O(n^2) on the GUI
        # thread — long enough for Windows to paint "Not Responding". Do it
        # once, after the burst; every card still appears exactly as before.
        self._schedule_show()

    def _schedule_show(self) -> None:
        if getattr(self, "_show_pending", False):
            return
        self._show_pending = True
        QTimer.singleShot(0, self._flush_show)

    def _flush_show(self) -> None:
        self._show_pending = False
        if not self._cards:
            return
        self._reposition()
        self.show()
        self._pin_to_all_spaces()

    def _arm_auto_dismiss(self, card: "_Card"):
        """If the user enabled auto-dismiss, schedule this card to disappear
        after the chosen delay. Auto-dismiss behaves exactly like pressing ✕,
        so the unread-reminder setting still applies (the card comes back later
        unless reminders are Off)."""
        ns = NotifSettings()
        if not ns.auto_dismiss_enabled:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._auto_dismiss(card))
        timer.start(ns.auto_dismiss_seconds * 1000)
        card._auto_timer = timer

    def _auto_dismiss(self, card: "_Card"):
        # Card may already be gone (clicked/dismissed/opened elsewhere).
        if card in self._cards:
            self._on_card_dismissed(card)

    def reshow_if_pending(self):
        """Re-show pending cards (e.g. when the app regains focus)."""
        if self._cards:
            self._reposition()
            self.show()
            self._pin_to_all_spaces()

    def _pin_to_all_spaces(self):
        """Bring the stack to the front over other apps, without stealing focus."""
        self.raise_()
        if sys.platform == "darwin":
            try:
                import objc  # pyobjc
                from ctypes import c_void_p
                view = objc.objc_object(c_void_p=int(self.winId()))
                win = view.window()
                # CanJoinAllSpaces (1<<0) | FullScreenAuxiliary (1<<8)
                win.setCollectionBehavior_((1 << 0) | (1 << 8))
                win.setLevel_(25)  # NSStatusWindowLevel: above normal app windows
                win.orderFrontRegardless()  # front even while another app is active
            except Exception:
                pass  # pyobjc missing: stays-on-top hint still applies
        elif sys.platform == "win32":
            # Force the popup above the currently-focused app (e.g. the browser)
            # without activating/stealing focus, so new-mail cards are visible
            # even while the user is working in another window.
            try:
                import ctypes
                hwnd = int(self.winId())
                HWND_TOPMOST = -1
                SWP = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
                ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP)
            except Exception:
                pass

    def remove_for_message(self, msg_id: int):
        """Email opened by any path: clear its pending notification."""
        for card in [c for c in self._cards if c.msg_id == msg_id]:
            self._remove_card(card)

    # -- internals --
    def _on_mark_all(self):
        """Mark every notified email read and clear the stack (no reminders)."""
        ids = [c.msg_id for c in self._cards if c.msg_id and c.msg_id > 0]
        for card in list(self._cards):
            self._remove_card(card)
        if ids:
            self.mark_all_read.emit(ids)

    def _on_close_all(self):
        """Dismiss every card at once — same semantics as pressing ✕ on each,
        so the user's unread-reminder setting still applies."""
        for card in list(self._cards):
            self._on_card_dismissed(card)

    def _on_card_dismissed(self, card: "_Card"):
        """✕ pressed: remove the card and tell the app (unread reminders)."""
        msg_id = card.msg_id
        self._remove_card(card)
        if msg_id and msg_id > 0:
            self.card_dismissed.emit(msg_id)

    def _on_card_clicked(self, msg_id: int):
        self.message_clicked.emit(msg_id)
        self.remove_for_message(msg_id)

    def _remove_card(self, card: "_Card"):
        if card in self._cards:
            self._cards.remove(card)
            timer = getattr(card, "_auto_timer", None)
            if timer is not None:
                timer.stop()
            card.setParent(None)
            card.deleteLater()
        if not self._cards:
            self.hide()
        else:
            self._reposition()

    def _reposition(self):
        n = len(self._cards)
        h = min(MAX_HEIGHT, 40 + n * 86)
        self.setFixedHeight(h)
        screen = QGuiApplication.primaryScreen().availableGeometry()
        pos = NotifSettings().popup_position
        margin = 14
        # Horizontal placement
        if pos.endswith("-left"):
            x = screen.left() + margin
        elif pos.endswith("-center"):
            x = screen.left() + (screen.width() - CARD_W) // 2
        else:  # -right (default)
            x = screen.right() - CARD_W - margin
        # Vertical placement
        if pos.startswith("bottom"):
            y = screen.bottom() - h - margin
        else:  # top (default)
            y = screen.top() + margin
        self.move(x, y)

    def apply_settings(self):
        """Re-read notification settings (e.g. after the user saves them) and
        move the stack to the newly chosen position. Auto-dismiss changes take
        effect for the next card pushed."""
        if self._cards:
            self._reposition()
