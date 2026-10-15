"""Outlook-style recipient field: typed addresses become removable chips, and
the autocomplete dropdown shows an avatar + name + email card.

Drop-in for the old QLineEdit-based field — it keeps the same public surface
(`text()`, `setText()`, `setFocus()`, `setPlaceholderText()`, `set_addresses()`)
so the compose window needs no changes: `text()` still returns a comma-joined
recipient string and `setText()` still accepts one.

The pure helpers current_token / replace_token stay at module level so the token
logic remains unit-testable without constructing widgets.
"""
from __future__ import annotations

from email.utils import formataddr, getaddresses, parseaddr

from PySide6.QtCore import (QMimeData, QPoint, QRect, QSize, QStringListModel,
                            Qt, Signal)
from PySide6.QtGui import QColor, QDrag, QFont, QPainter
from PySide6.QtWidgets import (QApplication, QCompleter, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSizePolicy,
                               QStyledItemDelegate, QWidget)

from . import editor_icons, theme
from .attachment_chips import FlowLayout

_EDITOR_MIN_W = 120  # keep this much room for typing before wrapping to a new row

_MIN_PREFIX = 2  # don't pop up on a single typed character

#: Our own clipboard type for a dragged recipient. Having one means a chip can
#: only be dropped where it makes sense — a field will not swallow arbitrary
#: text someone happens to drag over it.
RECIPIENT_MIME = "application/x-orostarmail-recipient"


def recipient_mime(display: str) -> QMimeData:
    """What travels while a recipient chip is being dragged.

    Plain text goes along for the ride so the address can also be dropped into
    something outside this app.
    """
    mime = QMimeData()
    mime.setData(RECIPIENT_MIME, display.encode("utf-8"))
    mime.setText(display)
    return mime


def current_token(text: str) -> str:
    """The recipient token being typed: text after the last comma, lstripped."""
    return (text or "").rsplit(",", 1)[-1].lstrip()


def replace_token(text: str, completion: str) -> str:
    """Replace the in-progress token with *completion* and append ', '.

    "a@b.com, ki" + "king@x.com" -> "a@b.com, king@x.com, "
    """
    text = text or ""
    if "," in text:
        head = text.rsplit(",", 1)[0].rstrip()
        return f"{head}, {completion}, "
    return f"{completion}, "


def split_recipients(text: str) -> list[str]:
    """Split a recipient string into individual "Name <email>" / "email" display
    strings, tolerant of commas inside display names."""
    out: list[str] = []
    for name, addr in getaddresses([text or ""]):
        if not addr and not name:
            continue
        disp = formataddr((name, addr)) if name else (addr or name)
        if disp:
            out.append(disp)
    return out


def _name_email(display: str) -> tuple[str, str]:
    name, addr = parseaddr(display or "")
    return name, (addr or display or "")


class _RecipientFlow(FlowLayout):
    """Flow layout that STRETCHES the last item (the inline editor) to fill the
    remaining width of its row — so you keep typing the next address right after
    the last chip on the same line, and it only wraps when the row is full."""

    def _do_layout(self, rect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = eff.x(), eff.y()
        line_h = 0
        n = len(self._items)
        for i, item in enumerate(self._items):
            hint = item.sizeHint()
            if i == n - 1:  # the editor: fill the rest of this row
                remaining = eff.right() - x + 1
                if remaining < _EDITOR_MIN_W and line_h > 0:
                    x = eff.x()
                    y += line_h + self._spacing
                    line_h = 0
                    remaining = eff.right() - x + 1
                if not test_only:
                    item.setGeometry(QRect(QPoint(x, y),
                                           QSize(max(_EDITOR_MIN_W, remaining),
                                                 hint.height())))
                line_h = max(line_h, hint.height())
                break
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y += line_h + self._spacing
                next_x = x + hint.width() + self._spacing
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def _avatar_color(seed: str) -> QColor:
    """Stable pastel-ish avatar colour derived from the address."""
    palette = [theme.GOLD, "#4E9F6E", "#5B8DEF", "#C86B98", "#E0955B",
               "#7C8CF8", "#4FB0AE"]
    h = sum(ord(c) for c in (seed or "?"))
    return QColor(palette[h % len(palette)])


class _RecipientDelegate(QStyledItemDelegate):
    """Draws each autocomplete row as an avatar circle + bold name + grey email."""

    def sizeHint(self, option, index):
        return QSize(320, 52)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        # Row background (hover/selected highlight).
        from PySide6.QtWidgets import QStyle
        hot = bool(option.state & QStyle.StateFlag.State_Selected
                   or option.state & QStyle.StateFlag.State_MouseOver)
        if hot:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.GOLD_SOFT))
            painter.drawRoundedRect(rect.adjusted(4, 3, -4, -3), 8, 8)

        display = index.data() or ""
        name, email = _name_email(display)
        title = name or email
        initial = next((ch.upper() for ch in title if ch.isalnum()), "?")

        # Avatar circle.
        av = QRect(rect.left() + 12, rect.center().y() - 15, 30, 30)
        painter.setBrush(_avatar_color(email))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(av)
        painter.setPen(QColor("white"))
        f = QFont(); f.setPointSize(11); f.setBold(True)
        painter.setFont(f)
        painter.drawText(av, Qt.AlignmentFlag.AlignCenter, initial)

        text_left = av.right() + 12
        tw = rect.right() - text_left - 12
        if name:
            painter.setPen(QColor(theme.TEXT))
            nf = QFont(); nf.setPointSize(10); nf.setBold(True)
            painter.setFont(nf)
            nr = QRect(text_left, rect.top() + 8, tw, 18)
            painter.drawText(nr, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 name, Qt.TextElideMode.ElideRight, tw))
            painter.setPen(QColor(theme.TEXT_DIM))
            ef = QFont(); ef.setPointSize(9)
            painter.setFont(ef)
            er = QRect(text_left, rect.top() + 26, tw, 16)
            painter.drawText(er, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 email, Qt.TextElideMode.ElideRight, tw))
        else:
            painter.setPen(QColor(theme.TEXT))
            nf = QFont(); nf.setPointSize(10)
            painter.setFont(nf)
            nr = QRect(text_left, rect.top(), tw, rect.height())
            painter.drawText(nr, Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(
                                 email, Qt.TextElideMode.ElideRight, tw))
        painter.restore()


class RecipientDropButton(QPushButton):
    """The small "Cc" / "Bcc" toggle, which also accepts a dragged recipient.

    Cc and Bcc are hidden until the user asks for them, and a hidden widget
    receives no drops — so without this, moving a recipient to Cc would still
    mean revealing the row first and only then dragging. Dropping on the
    button does both.
    """

    recipientDropped = Signal(str)

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _carries_recipient(event) -> bool:
        return event.mimeData().hasFormat(RECIPIENT_MIME)

    def dragEnterEvent(self, event):
        if self._carries_recipient(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._carries_recipient(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if not self._carries_recipient(event):
            event.ignore()
            return
        display = bytes(event.mimeData().data(RECIPIENT_MIME)).decode("utf-8")
        self.recipientDropped.emit(display)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()


class _Chip(QFrame):
    """A removable recipient pill: 'Name <email>  ✕'.

    Dragging one moves the recipient to another field. The chip only leaves
    this field if the drop was actually taken somewhere, so a drag abandoned
    over empty space loses nothing.
    """
    removed = Signal(object)
    #: Emitted with this chip's value once another field has taken it.
    moved_away = Signal(str)

    def __init__(self, display: str, parent=None):
        super().__init__(parent)
        self.value = display
        self._press_at: QPoint | None = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setObjectName("recipChip")
        name, email = _name_email(display)
        label_text = name or email
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 2, 5, 2)
        lay.setSpacing(5)
        lbl = QLabel(label_text)
        lbl.setToolTip(display)
        lbl.setStyleSheet("background: transparent; border: none;")
        lay.addWidget(lbl)
        x = QPushButton()
        x.setObjectName("recipChipX")
        x.setIcon(editor_icons.icon("close", theme.GOLD_DEEP))
        x.setIconSize(QSize(9, 9))
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setFixedSize(17, 17)
        x.setToolTip("Remove")
        x.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(x)
        self.setStyleSheet(f"""
            QFrame#recipChip {{
                background: {theme.GOLD_SOFT};
                border: 1px solid {theme.GOLD};
                border-radius: 11px;
            }}
            QFrame#recipChip QLabel {{ color: {theme.GOLD_DEEP}; font-size: 12px; }}
            QPushButton#recipChipX {{
                border: none; background: transparent; border-radius: 8px;
            }}
            QPushButton#recipChipX:hover {{ background: {theme.GOLD}; }}
        """)

    # ----- dragging -------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_at = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._press_at is None:
            return super().mouseMoveEvent(e)
        moved = (e.position().toPoint() - self._press_at).manhattanLength()
        if moved < QApplication.startDragDistance():
            return super().mouseMoveEvent(e)
        self._press_at = None

        drag = QDrag(self)
        drag.setMimeData(recipient_mime(self.value))
        drag.setPixmap(self.grab())
        drag.setHotSpot(e.position().toPoint())
        # Only give the recipient up if somewhere actually took it: a drag
        # dropped on the desktop must not silently delete a recipient.
        if drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
                     Qt.DropAction.MoveAction) == Qt.DropAction.MoveAction:
            self.moved_away.emit(self.value)

    def mouseReleaseEvent(self, e):
        self._press_at = None
        super().mouseReleaseEvent(e)


class _InlineEdit(QLineEdit):
    """Borderless editor placed after the chips. _RecipientFlow stretches it to
    fill the rest of the row. Backspace on empty removes the previous chip."""
    backspace_empty = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrame(False)
        self.setStyleSheet("background: transparent; border: none;")

    def minimumSizeHint(self):
        h = super().minimumSizeHint()
        return QSize(_EDITOR_MIN_W, h.height())

    def keyPressEvent(self, e):
        if (e.key() == Qt.Key.Key_Backspace and not self.text()):
            self.backspace_empty.emit()
            return
        super().keyPressEvent(e)


class RecipientEdit(QWidget):
    """Chip-based recipient field with a rich autocomplete dropdown. Public API
    matches the old QLineEdit (text/setText/setFocus/setPlaceholderText)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._chips: list[_Chip] = []
        self._completer: QCompleter | None = None
        self._placeholder = ""
        self.setObjectName("recipientField")
        # A bare QWidget won't paint a stylesheet border/background without this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(34)   # match the QLineEdit form fields
        # Moving a recipient between To, Cc and Bcc used to mean deleting it
        # here and typing it again over there.
        self.setAcceptDrops(True)
        self._drop_hover = False

        self._flow = _RecipientFlow(self, margin=5, spacing=5)
        self._flow.setContentsMargins(8, 4, 8, 4)
        self._editor = _InlineEdit(self)
        self._editor.returnPressed.connect(self._commit_current)
        self._editor.textEdited.connect(self._on_text_edited)
        self._editor.backspace_empty.connect(self._pop_last_chip)
        self._relayout()

        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        self._restyle()

    # ----- public API (QLineEdit-compatible) -------------------------------
    def text(self) -> str:
        parts = [c.value for c in self._chips]
        pending = self._editor.text().strip()
        if pending:
            parts.append(pending)
        return ", ".join(parts)

    def setText(self, s: str) -> None:
        self._clear_chips()
        self._editor.clear()
        for disp in split_recipients(s or ""):
            self._add_chip(disp)
        self._update_placeholder()

    def setPlaceholderText(self, s: str) -> None:
        self._placeholder = s or ""
        self._update_placeholder()

    def setFocus(self, *a) -> None:  # noqa: D401 - match QWidget signature loosely
        self._editor.setFocus()

    def set_addresses(self, addresses: list[str]) -> None:
        completer = QCompleter(QStringListModel(list(addresses), self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        popup = completer.popup()
        popup.setItemDelegate(_RecipientDelegate(popup))
        popup.setObjectName("recipientPopup")
        popup.setStyleSheet(f"""
            QListView#recipientPopup {{
                background: {theme.BG};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
                padding: 4px;
                outline: none;
            }}
        """)
        completer.setWidget(self._editor)
        completer.activated.connect(self._on_completion)
        self._completer = completer

    # ----- chip management -------------------------------------------------
    def _relayout(self) -> None:
        """Rebuild the flow so chips come first and the editor stays last."""
        while self._flow.count():
            self._flow.takeAt(0)
        for chip in self._chips:
            self._flow.addWidget(chip)
            chip.show()
        self._flow.addWidget(self._editor)
        self._editor.show()
        self._flow.invalidate()
        self.updateGeometry()

    def _add_chip(self, display: str) -> None:
        display = display.strip().rstrip(",").strip()
        if not display:
            return
        chip = _Chip(display, self)
        chip.removed.connect(self._remove_chip)
        chip.moved_away.connect(self.take_recipient)
        self._chips.append(chip)
        self._relayout()
        self._update_placeholder()

    def _remove_chip(self, chip: _Chip) -> None:
        if chip in self._chips:
            self._chips.remove(chip)
            chip.setParent(None)
            chip.deleteLater()
            self._relayout()
            self._update_placeholder()

    # ----- moving recipients between fields --------------------------------
    @staticmethod
    def _address_of(display: str) -> str:
        return parseaddr(display)[1].strip().lower()

    def has_recipient(self, display: str) -> bool:
        """Whether this field already holds that address, in any display form."""
        wanted = self._address_of(display)
        if not wanted:
            return False
        return any(self._address_of(c.value) == wanted for c in self._chips)

    def take_recipient(self, display: str) -> None:
        """Give up a recipient because another field has taken it."""
        wanted = self._address_of(display)
        for chip in list(self._chips):
            if self._address_of(chip.value) == wanted:
                self._remove_chip(chip)
                return

    def _accepts(self, event) -> bool:
        return event.mimeData().hasFormat(RECIPIENT_MIME)

    def dragEnterEvent(self, event):
        if not self._accepts(event):
            event.ignore()
            return
        self._drop_hover = True
        self._restyle()
        event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_hover = False
        self._restyle()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self.handle_drop(event, source=event.source())

    def handle_drop(self, event, source=None) -> None:
        """The drop itself, with the dragging widget passed in.

        Split out from dropEvent so the "dropped back where it started" case
        can be tested: that case turns on who started the drag, which a
        synthesised event cannot carry.
        """
        self._drop_hover = False
        self._restyle()
        if not self._accepts(event):
            event.ignore()
            return

        # Dropped back into the field it came from. Accepting this as a move
        # would tell the source chip to remove itself — and the source is us,
        # so the recipient would just disappear.
        if source is not None and source in self._chips:
            event.setDropAction(Qt.DropAction.IgnoreAction)
            event.accept()
            return

        display = bytes(event.mimeData().data(RECIPIENT_MIME)).decode("utf-8")
        # Dropping a recipient the field already has adds nothing, but the move
        # is still accepted so the source lets go: the user asked for it to be
        # here, not in both places.
        if not self.has_recipient(display):
            self._add_chip(display)
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def _clear_chips(self) -> None:
        for chip in list(self._chips):
            chip.setParent(None)
            chip.deleteLater()
        self._chips.clear()
        self._relayout()

    def _pop_last_chip(self) -> None:
        if self._chips:
            self._remove_chip(self._chips[-1])

    # ----- typing / completion --------------------------------------------
    def _on_text_edited(self, text: str) -> None:
        # A comma finishes the current token into a chip.
        if "," in text:
            head, _, tail = text.rpartition(",")
            if head.strip():
                self._add_chip(head)
            self._editor.setText(tail.lstrip())
            return
        if self._completer is None:
            return
        token = text.strip()
        if len(token) < _MIN_PREFIX:
            self._completer.popup().hide()
            return
        self._completer.setCompletionPrefix(token)
        if self._completer.completionCount() == 0:
            self._completer.popup().hide()
            return
        self._completer.complete()

    def _on_completion(self, completion: str) -> None:
        self._add_chip(completion)
        self._editor.clear()
        self._editor.setFocus()

    def _commit_current(self) -> None:
        token = self._editor.text().strip()
        if token:
            self._add_chip(token)
            self._editor.clear()

    def focusOutEvent(self, e):
        # Commit whatever was typed so it isn't silently lost.
        self._commit_current()
        super().focusOutEvent(e)

    # ----- styling ---------------------------------------------------------
    def _update_placeholder(self) -> None:
        self._editor.setPlaceholderText("" if self._chips else self._placeholder)

    def _restyle(self) -> None:
        # A recipient being dragged has to be able to see where it may land,
        # or the gesture is a guess.
        border = theme.GOLD if self._drop_hover else theme.BORDER
        width = 2 if self._drop_hover else 1
        background = theme.GOLD_SOFT if self._drop_hover else theme.BG
        self.setStyleSheet(f"""
            QWidget#recipientField {{
                background: {background};
                border: {width}px solid {border};
                border-radius: 8px;
            }}
        """)
