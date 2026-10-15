from __future__ import annotations

import html
import json
import re
from email.utils import parseaddr

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QListWidget, QListWidgetItem,
                               QMenu, QStyle, QStyledItemDelegate)

from ..db import Database
from . import editor_icons, theme

ROLE_MSG_ID = Qt.ItemDataRole.UserRole
ROLE_SENDER = Qt.ItemDataRole.UserRole + 1
ROLE_SUBJECT = Qt.ItemDataRole.UserRole + 2
ROLE_TIME = Qt.ItemDataRole.UserRole + 3
ROLE_ACC_NAME = Qt.ItemDataRole.UserRole + 4
ROLE_ACC_COLOR = Qt.ItemDataRole.UserRole + 5
ROLE_UNREAD = Qt.ItemDataRole.UserRole + 6
ROLE_SNIPPET = Qt.ItemDataRole.UserRole + 7
ROLE_STATUS = Qt.ItemDataRole.UserRole + 8  # outbox row status, for the edit menu
ROLE_HAS_ATTACH = Qt.ItemDataRole.UserRole + 9
ROLE_FLAGGED = Qt.ItemDataRole.UserRole + 10
ROLE_PINNED = Qt.ItemDataRole.UserRole + 11
ROLE_FLAG_DONE = Qt.ItemDataRole.UserRole + 12  # follow-up marked complete (✓)

# Hover quick-actions drawn on the right of each row (Outlook-style).
ACTION_ORDER = ("read", "flag", "pin", "delete")
ACT_SLOT = 30          # click target width per action
ACT_ICON = 18
FLAG_COLOR = "#D93025"  # red flag when set
DONE_COLOR = "#1E8E3E"  # green check when a follow-up is marked complete

PAGE_SIZE = 100        # rows loaded per page (infinite scroll appends more)


def _has_real_attachment(attachments_json: str) -> bool:
    """True if the message carries a non-inline (real) attachment.

    Uses the per-item `inline` flag set at parse time; for older records that
    predate the flag, falls back to 'has a Content-ID == inline'."""
    try:
        items = json.loads(attachments_json or "[]")
    except Exception:
        return False
    return isinstance(items, list) and any(
        isinstance(i, dict) and not i.get("inline", bool(i.get("cid")))
        for i in items)

# Row metrics come from the density setting; apply_density() mutates them in
# place so the delegate (which reads them at paint time) picks a change up on
# the next repaint, exactly like the colour globals in theme.
ROW_H = 84
PAD = 14
AVATAR = 38

# Text block: sender line, subject line, chip/snippet line.
_BLOCK_H = 61
_LINE_PITCH = 21


def apply_density(name: str | None = None) -> None:
    globals().update(theme.density_metrics(name))


def _row_offsets() -> tuple[int, int, int]:
    """Y offsets (relative to the row top) of the three text lines.

    Derived from ROW_H so every density keeps the block vertically centred
    instead of needing its own hand-tuned numbers.
    """
    y0 = max(2, (ROW_H - _BLOCK_H) // 2)
    return y0, y0 + _LINE_PITCH, y0 + _LINE_PITCH * 2 + 3


apply_density()


def action_hotspots(r) -> list:
    """[(name, QRect), ...] — the clickable rects for the hover quick-actions,
    a cluster on the right of a row. Shared by the delegate (draw) and the list
    (hit-test) so both agree on where each icon lives."""
    cluster_w = len(ACTION_ORDER) * ACT_SLOT
    x0 = r.right() - PAD - cluster_w
    cy = r.y() + ROW_H // 2
    out = []
    for i, name in enumerate(ACTION_ORDER):
        out.append((name, QRect(int(x0 + i * ACT_SLOT), int(cy - ACT_SLOT // 2),
                                ACT_SLOT, ACT_SLOT)))
    return out


def _display_sender(raw: str) -> str:
    # Python 3.14's email._parseaddr can hit RecursionError (and other
    # parse errors) on malformed/adversarial address strings; never let a
    # single bad sender crash the whole message list.
    try:
        name, addr = parseaddr(raw)
    except (RecursionError, ValueError, IndexError):
        return raw
    return name or addr or raw


def _initial(name: str) -> str:
    """First letter-or-digit of a display name, uppercased ('?' if none)."""
    for ch in name:
        if ch.isalpha() or ch.isdigit():
            return ch.upper()
    return "?"


def _scaled(base: QFont, delta: float) -> QFont:
    """Copy of *base* resized by *delta*; QSS fonts carry pixel, not point, size."""
    f = QFont(base)
    if base.pixelSize() > 0:
        f.setPixelSize(max(8, base.pixelSize() + round(delta)))
    elif base.pointSizeF() > 0:
        f.setPointSizeF(max(6.0, base.pointSizeF() + delta))
    return f


def _snippet(text: str, limit: int = 90) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _msg_snippet(m, limit: int = 90) -> str:
    """Preview text for a message. Prefer the HTML body (stripped) so the preview
    matches what the reader shows — the plain-text part is sometimes worse (e.g.
    Zoho's footer leaves a literal 'Sent using {0}' in plain text but renders
    'Sent using Zoho Mail' in HTML). Falls back to body_text."""
    raw = getattr(m, "body_html", "") or ""
    if raw:
        chunk = raw[:8000]
        # Drop <style>/<script>/<head> blocks entirely — their CSS/JS is not
        # preview text (Qt's toHtml() embeds a <style> block that otherwise
        # leaked "p, li { white-space: pre-wrap; }" into the snippet).
        chunk = re.sub(r"(?is)<(style|script|head)\b[^>]*>.*?</\1>", " ", chunk)
        chunk = re.sub(r"<[^>]*$", "", chunk)           # drop a half-cut tag
        text = re.sub(r"<[^>]+>", " ", chunk)           # strip remaining tags
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 3:
            return text[:limit]
    return _snippet(getattr(m, "body_text", "") or "", limit)


def _local_time(iso: str) -> str:
    """UTC ISO timestamp -> local time. Always show the full date WITH the
    year and time (e.g. '01 Jul 2026  12:11') so the day is never ambiguous,
    even for today's emails."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # local tz
        return dt.strftime("%d %b %Y  %H:%M")
    except ValueError:
        return iso[5:16].replace("T", "  ")


class MessageDelegate(QStyledItemDelegate):
    """Three-line triage row: sender + time / subject / account chip + snippet."""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), ROW_H)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = option.rect

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovering = bool(option.state & QStyle.StateFlag.State_MouseOver)
        # Rounded selection / hover highlight (inset like the reference)
        hl = QRectF(r.x() + 8, r.y() + 3, r.width() - 16, r.height() - 6)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.GOLD_SOFT))
            painter.drawRoundedRect(hl, 12, 12)
        elif hovering:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.PANEL))
            painter.drawRoundedRect(hl, 12, 12)

        mid = index.data(ROLE_MSG_ID)
        is_msg = mid is not None and mid > 0
        flagged = bool(index.data(ROLE_FLAGGED))
        flag_done = bool(index.data(ROLE_FLAG_DONE))
        pinned = bool(index.data(ROLE_PINNED))
        unread = index.data(ROLE_UNREAD)
        sender = index.data(ROLE_SENDER) or ""
        subject = index.data(ROLE_SUBJECT) or ""
        when = index.data(ROLE_TIME) or ""
        acc_name = index.data(ROLE_ACC_NAME) or ""
        acc_color = QColor(index.data(ROLE_ACC_COLOR) or theme.TEXT_FAINT)
        snippet = index.data(ROLE_SNIPPET) or ""

        x = r.x() + PAD + AVATAR + 10   # text starts right of the avatar
        right = r.right() - PAD
        base = option.font
        y_sender, y_subject, y_third = _row_offsets()

        # Avatar: account-colored circle + subtle ring + sender initial
        ax = r.x() + PAD
        ay = r.y() + (ROW_H - AVATAR) // 2
        painter.setPen(QColor(theme.BORDER))
        painter.setBrush(acc_color)
        painter.drawEllipse(ax, ay, AVATAR, AVATAR)
        f_init = _scaled(base, 3)
        f_init.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f_init)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(QRectF(ax, ay, AVATAR, AVATAR),
                         Qt.AlignmentFlag.AlignCenter, _initial(sender))

        # Unread dot: small gold dot at the avatar's top-right edge
        if unread:
            # Ring in the row's own surface colour so the dot reads as cut out
            # of the avatar on light and dark chrome alike.
            painter.setPen(QColor(theme.BG))
            painter.setBrush(QColor(theme.GOLD))
            painter.drawEllipse(ax + AVATAR - 9, ay - 1, 8, 8)

        # Line 1: sender (left, 13.5px DemiBold) + time (right, 11px)
        f_sender = _scaled(base, 0.5)
        f_sender.setWeight(QFont.Weight.DemiBold)
        f_time = _scaled(base, -2)
        tw = QFontMetrics(f_time).horizontalAdvance(when)

        painter.setFont(f_time)
        painter.setPen(QColor(theme.TEXT_DIM if unread else theme.TEXT_FAINT))
        painter.drawText(QRectF(right - tw, r.y() + y_sender + 2, tw, 16),
                         Qt.AlignmentFlag.AlignRight, when)

        # Persistent flag / pin markers, drawn just left of the time. A completed
        # follow-up shows a green ✓ in place of the red flag (Outlook-style).
        ind_w = 0
        ix = right - tw - 6
        if flag_done:
            ix -= 15
            painter.drawPixmap(int(ix), r.y() + y_sender + 1,
                               editor_icons.icon("check", DONE_COLOR).pixmap(14, 14))
            ix -= 4
            ind_w += 19
        elif flagged:
            ix -= 15
            painter.drawPixmap(int(ix), r.y() + y_sender + 1,
                               editor_icons.icon("flag", FLAG_COLOR).pixmap(14, 14))
            ix -= 4
            ind_w += 19
        if pinned:
            ix -= 15
            painter.drawPixmap(int(ix), r.y() + y_sender + 1,
                               editor_icons.icon("pin", theme.GOLD_DEEP).pixmap(14, 14))
            ind_w += 19

        if not unread:
            f_sender.setWeight(QFont.Weight.Medium)
        painter.setFont(f_sender)
        painter.setPen(QColor(theme.TEXT))
        fm = QFontMetrics(f_sender)
        sender_w = int(right - x - tw - 10 - ind_w)
        painter.drawText(QRectF(x, r.y() + y_sender, sender_w, 18),
                         Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(sender, Qt.TextElideMode.ElideRight, sender_w))

        # Line 2: subject (regular; unread: DemiBold) + paperclip if attachments
        has_attach = bool(index.data(ROLE_HAS_ATTACH))
        clip_w = 18 if has_attach else 0
        f_subj = QFont(base)
        f_subj.setWeight(QFont.Weight.DemiBold if unread else QFont.Weight.Normal)
        painter.setFont(f_subj)
        painter.setPen(QColor(theme.TEXT if unread else theme.TEXT_DIM))
        fm = QFontMetrics(f_subj)
        painter.drawText(QRectF(x, r.y() + y_subject, right - x - clip_w, 17),
                         Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(subject, Qt.TextElideMode.ElideRight,
                                       int(right - x - clip_w)))
        if has_attach:
            pm = editor_icons.icon("attach").pixmap(14, 14)
            painter.drawPixmap(int(right - 15), r.y() + y_subject + 1, pm)

        # Line 3: account chip + snippet
        f_chip = _scaled(base, -2.5)
        f_chip.setWeight(QFont.Weight.DemiBold)
        fm_chip = QFontMetrics(f_chip)
        chip_w = fm_chip.horizontalAdvance(acc_name) + 14
        chip_rect = QRectF(x, r.y() + y_third, chip_w, 16)
        chip_bg = QColor(acc_color)
        # Dark chrome needs a stronger tint to show at all, and the label has to
        # go lighter than the dot instead of darker.
        chip_bg.setAlpha(46 if theme.IS_DARK else 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(chip_bg)
        painter.drawRoundedRect(chip_rect, 8, 8)
        painter.setFont(f_chip)
        painter.setPen(acc_color.lighter(135) if theme.IS_DARK
                       else acc_color.darker(115))
        painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, acc_name)

        f_snip = _scaled(base, -1)
        painter.setFont(f_snip)
        painter.setPen(QColor(theme.TEXT_FAINT))
        fm = QFontMetrics(f_snip)
        sx = x + chip_w + 8
        painter.drawText(QRectF(sx, r.y() + y_third - 1, right - sx, 17),
                         Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(snippet, Qt.TextElideMode.ElideRight,
                                       int(right - sx)))

        # Hover quick-actions (Outlook-style): mark read/unread, flag, pin,
        # delete — a cluster on the right, only while hovering a real message.
        if hovering and is_msg:
            hs = action_hotspots(r)
            cy = r.y() + ROW_H // 2
            left = hs[0][1].left() - 5
            wide = hs[-1][1].right() + 5 - left
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.GOLD_SOFT if selected else theme.PANEL))
            painter.drawRoundedRect(QRectF(left, cy - 16, wide, 32), 9, 9)
            icon_map = {"read": "mail", "flag": "flag",
                        "pin": "pin", "delete": "trash"}
            # Which action icon (if any) the cursor is over, so we can give it a
            # circular highlight + a stronger tint — per-icon hover feedback.
            widget = option.widget
            hovered = getattr(widget, "_hover_action", None)
            for name, slot in hs:
                is_hot = hovered == (index.row(), name)
                if is_hot:
                    # Circle that contrasts with the cluster bg (GOLD_SOFT when
                    # the row is selected, PANEL otherwise) so it's always seen.
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.setBrush(QColor(theme.GOLD_HOVER if selected
                                            else theme.GOLD_SOFT))
                    d = ACT_ICON + 10
                    painter.drawEllipse(QRectF(slot.center().x() - d / 2,
                                               slot.center().y() - d / 2, d, d))
                if name == "flag":
                    col = FLAG_COLOR
                elif name == "pin":
                    col = theme.GOLD_DEEP if pinned else theme.TEXT_DIM
                else:
                    col = theme.TEXT_DIM
                # On hover, darken the neutral icons so they read as "active".
                if is_hot and name not in ("flag",):
                    col = theme.GOLD_DEEP
                pm = editor_icons.icon(icon_map[name], col).pixmap(ACT_ICON, ACT_ICON)
                painter.drawPixmap(slot.center().x() - ACT_ICON // 2,
                                   slot.center().y() - ACT_ICON // 2, pm)

        # Row separator — only between rows, inset to the text start
        if index.row() < index.model().rowCount() - 1:
            painter.setPen(QColor(theme.ROW_SEP))
            painter.drawLine(int(x), r.bottom(), r.right() - PAD, r.bottom())
        painter.restore()


class MessageList(QListWidget):
    message_selected = Signal(int)        # message id (positive = real message)
    message_open_external = Signal(int)   # double-click -> open in its own window
    delete_requested = Signal(list)       # [msg_id, ...]
    spam_requested = Signal(list)         # [msg_id, ...]
    restore_requested = Signal(list)      # [msg_id, ...] — Trash/Spam -> Inbox
    mark_read_requested = Signal(list, bool)  # ([msg_id, ...], read)
    outbox_retry = Signal(list)           # [outbox_id, ...] (positive)
    outbox_delete = Signal(list)          # [outbox_id, ...] (positive)
    outbox_edit = Signal(int)             # outbox_id (positive)
    outbox_selected = Signal(int)         # outbox_id (positive) — preview it
    counts_changed = Signal(int, int)     # (total, unread) of the shown view
    copy_requested = Signal(list)         # [msg_id, ...] -> copy as .eml

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.account_id = None  # None = unified
        self.folder = "INBOX"
        self.order = "newest"
        self.unread_only = False
        self.flagged_only = False
        self._search = None  # active search query, or None
        self._search_account_ids = None  # optional account scope for search
        self.date_from = None  # ISO "YYYY-MM-DD" or None ((None, None) = off)
        self.date_to = None
        # Pagination (infinite scroll): the folder/group view loads one page at a
        # time and appends more as the user scrolls near the bottom.
        self.page_size = PAGE_SIZE
        self._offset = 0
        self._has_more = False
        self._loading = False
        self._accounts = {}
        self._group_colors: dict[int, str] = {}
        self._restore_id = None
        self._hover_action = None  # (row, action_name) the cursor is over, or None
        self.setItemDelegate(MessageDelegate(self))
        self.setMouseTracking(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.itemClicked.connect(self._on_item_clicked)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _on_item_clicked(self, it):
        mid = it.data(ROLE_MSG_ID)
        if mid is None:
            return
        if mid > 0:
            self.message_selected.emit(mid)
        else:
            # Negative ids are outbox/draft rows. They have no `messages` row,
            # so they get their own signal and the reader renders them straight
            # from the outbox table — leaving the pane blank looked broken.
            self.outbox_selected.emit(-mid)

    def _on_item_double_clicked(self, it):
        # Double-clicking an editable outbox row opens it for re-editing.
        if self.folder in ("OUTBOX", "DRAFTS"):
            edit_id = self._editable_outbox_id()
            if edit_id is not None:
                self.outbox_edit.emit(edit_id)
            return
        # Any other message → open it in its own window (Outlook-style).
        mid = it.data(ROLE_MSG_ID)
        if mid is not None and mid > 0:
            self.message_open_external.emit(mid)

    def show_view(self, account_id, folder: str = "INBOX"):
        self._search = None
        self.account_id = account_id
        self.folder = folder
        self.refresh()

    def show_search(self, query: str, account_ids: list[int] | None = None):
        """Search across all folders; optionally scoped to *account_ids*."""
        self._search = query
        self._search_account_ids = account_ids
        self.refresh()

    def set_order(self, order: str):
        self.order = order
        self.refresh()

    def set_unread_only(self, unread_only: bool):
        self.unread_only = unread_only
        self.refresh()

    def set_flagged_only(self, flagged_only: bool):
        self.flagged_only = flagged_only
        self.refresh()

    def set_date_range(self, date_from: str | None, date_to: str | None):
        """Filter by date ("YYYY-MM-DD" strings); (None, None) turns it off."""
        self.date_from = date_from
        self.date_to = date_to
        self.refresh()

    def show_account(self, account_id):
        """Back-compat alias: show the INBOX of *account_id* (None = unified)."""
        self.show_view(account_id, "INBOX")

    # -- selection helpers --
    def _selected_ids(self) -> list[int]:
        """Selected positive message ids (excludes outbox rows)."""
        ids = [it.data(ROLE_MSG_ID) for it in self.selectedItems()]
        return [i for i in ids if i is not None and i > 0]

    def _selected_outbox_ids(self) -> list[int]:
        """Selected outbox ids, converted back to positive."""
        ids = [it.data(ROLE_MSG_ID) for it in self.selectedItems()]
        return [-i for i in ids if i is not None and i < 0]

    _EDITABLE_OUTBOX = ("queued", "failed", "draft")

    def _editable_outbox_id(self) -> int | None:
        """Outbox id of the single selected row if it can be edited, else None.

        Editing is offered only for one row at a time and only when it isn't
        mid-send, already sent, or awaiting admin approval.
        """
        items = self.selectedItems()
        if len(items) != 1:
            return None
        it = items[0]
        mid = it.data(ROLE_MSG_ID)
        if mid is None or mid >= 0:
            return None
        if it.data(ROLE_STATUS) not in self._EDITABLE_OUTBOX:
            return None
        return -mid

    def refresh(self):
        current = self.currentItem().data(ROLE_MSG_ID) if self.currentItem() else None
        self.clear()
        # Search / outbox views load in full and don't paginate; make sure a
        # stale "more pages" flag from a previous folder view can't trigger a
        # folder query while those views are showing.
        self._has_more = False
        accounts = {a.id: a for a in self.db.list_accounts()}
        # Colour rows by group so mail from different markets is telling apart
        # at a glance — see row_accent for the precedence.
        self._group_colors = {g["id"]: g["color"] for g in self.db.list_groups()}
        if self._search:
            for m in self.db.search_messages(self._search,
                                             account_ids=self._search_account_ids,
                                             date_from=self.date_from,
                                             date_to=self.date_to):
                acc = accounts.get(m.account_id)
                item = QListWidgetItem()
                item.setData(ROLE_MSG_ID, m.id)
                item.setData(ROLE_SENDER, _display_sender(m.sender))
                item.setData(ROLE_SUBJECT, m.subject)
                item.setData(ROLE_TIME, _local_time(m.date))
                item.setData(ROLE_ACC_NAME, acc.name if acc else "?")
                item.setData(ROLE_ACC_COLOR, self.row_accent(acc))
                item.setData(ROLE_UNREAD, not m.is_read)
                item.setData(ROLE_SNIPPET, f"[{m.folder}] {_msg_snippet(m)}")
                item.setData(ROLE_HAS_ATTACH, _has_real_attachment(m.attachments))
                item.setData(ROLE_FLAGGED, getattr(m, "flagged", False))
                item.setData(ROLE_FLAG_DONE, getattr(m, "flag_done", False))
                item.setData(ROLE_PINNED, getattr(m, "pinned", False))
                self.addItem(item)
                if current is not None and m.id == current:
                    self.setCurrentItem(item)
            self._emit_counts()
            return
        if self.folder in ("OUTBOX", "DRAFTS"):
            rows = (self.db.list_drafts() if self.folder == "DRAFTS"
                    else self.db.list_outbox())
            for row in rows:
                acc = accounts.get(row["account_id"])
                status = row["status"]
                item = QListWidgetItem()
                item.setData(ROLE_MSG_ID, -row["id"])  # negative marks outbox row
                item.setData(ROLE_SENDER, f"To: {row['to_addrs']}")
                item.setData(ROLE_SUBJECT, row["subject"])
                item.setData(ROLE_TIME, _local_time(row["created_at"]))
                item.setData(ROLE_ACC_NAME, acc.name if acc else "?")
                item.setData(ROLE_ACC_COLOR, self.row_accent(acc))
                item.setData(ROLE_UNREAD, status == "failed")
                item.setData(ROLE_STATUS, status)
                if self.folder == "DRAFTS":
                    # A draft is not "in a state" — just show what it says.
                    item.setData(ROLE_SNIPPET, _snippet(row["body"]))
                else:
                    label = ("⏳ WAITING FOR APPROVAL"
                             if status == "awaiting_approval" else status.upper())
                    item.setData(ROLE_SNIPPET,
                                 f"[{label}] "
                                 f"{row['error'] or _snippet(row['body'])}")
                self.addItem(item)
                if current is not None and -row["id"] == current:
                    self.setCurrentItem(item)
            self._emit_counts()
            return
        # Folder / group view — paginated: load the first page now, append more
        # as the user scrolls (see _on_scroll / _load_next_page).
        self._accounts = accounts
        self._restore_id = current
        self._offset = 0
        self._has_more = True
        self._loading = False
        self._load_next_page()

    def _query_messages(self, offset: int):
        """One page of messages for the current folder/group view."""
        if isinstance(self.account_id, tuple) and self.account_id[0] == "group":
            ids = self.db.accounts_in_group(self.account_id[1])
            return self.db.list_messages(
                account_ids=ids, folder=self.folder, order=self.order,
                unread_only=self.unread_only, flagged_only=self.flagged_only,
                date_from=self.date_from, date_to=self.date_to,
                limit=self.page_size, offset=offset)
        return self.db.list_messages(
            account_id=self.account_id, folder=self.folder, order=self.order,
            unread_only=self.unread_only, flagged_only=self.flagged_only,
            date_from=self.date_from, date_to=self.date_to,
            limit=self.page_size, offset=offset)

    def row_accent(self, acc) -> str:
        """Colour of a row's avatar circle and account chip.

        Order: the account's OWN colour when one was explicitly picked, then
        its group's colour (markets are identified by group), then the theme
        accent so everything else stays consistent.
        """
        if acc is None:
            return theme.TEXT_FAINT
        if getattr(acc, "color", ""):
            return acc.color
        gid = getattr(acc, "group_id", None)
        if gid is not None:
            group_color = self._group_colors.get(gid)
            if group_color:
                return group_color
        return theme.account_accent(getattr(acc, "color", ""))

    def _build_msg_item(self, m, acc, snippet) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(ROLE_MSG_ID, m.id)
        item.setData(ROLE_SENDER, _display_sender(m.sender))
        item.setData(ROLE_SUBJECT, m.subject)
        item.setData(ROLE_TIME, _local_time(m.date))
        item.setData(ROLE_ACC_NAME, acc.name if acc else "?")
        item.setData(ROLE_ACC_COLOR, self.row_accent(acc))
        item.setData(ROLE_UNREAD, not m.is_read)
        item.setData(ROLE_SNIPPET, snippet)
        item.setData(ROLE_HAS_ATTACH, _has_real_attachment(m.attachments))
        item.setData(ROLE_FLAGGED, getattr(m, "flagged", False))
        item.setData(ROLE_FLAG_DONE, getattr(m, "flag_done", False))
        item.setData(ROLE_PINNED, getattr(m, "pinned", False))
        return item

    def _load_next_page(self):
        if self._loading or not self._has_more:
            return
        self._loading = True
        try:
            messages = self._query_messages(self._offset)
            # Keep _loading True through the addItem loop: appending rows moves
            # the scrollbar, which re-fires _on_scroll → _load_next_page. If the
            # guard is already cleared, that recurses without bound (RecursionError
            # on scroll). Clearing it only after the whole page is inserted makes
            # the reentrant _on_scroll return early.
            for m in messages:
                acc = self._accounts.get(m.account_id)
                item = self._build_msg_item(m, acc, _msg_snippet(m))
                self.addItem(item)
                if self._restore_id is not None and m.id == self._restore_id:
                    self.setCurrentItem(item)
            self._offset += len(messages)
            self._has_more = len(messages) == self.page_size
            self._emit_counts()
        finally:
            self._loading = False

    def _on_scroll(self, value: int):
        sb = self.verticalScrollBar()
        if self._has_more and not self._loading and value >= sb.maximum() - 4:
            self._load_next_page()

    def mark_item_read(self, msg_id: int):
        """Clear one row's unread (bold/dot) in place — no full rebuild, so
        opening an email stays instant even with a long list."""
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_MSG_ID) == msg_id:
                if it.data(ROLE_UNREAD):
                    it.setData(ROLE_UNREAD, False)
                    self.viewport().update()
                break

    # -- flag / pin (app-local, stored in the DB) --
    def _apply_flag(self, ids: list[int], value: bool):
        for mid in ids:
            self.db.set_flagged(mid, value)
        # When the "Flagged only" filter is active, clearing a flag must drop the
        # row from view — rebuild. Otherwise flagging doesn't change order, so
        # update in place (keeps scroll position, stays snappy).
        if self.flagged_only:
            self.refresh()
            return
        idset = set(ids)
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_MSG_ID) in idset:
                it.setData(ROLE_FLAGGED, value)
                if not value:               # clearing the flag also clears ✓
                    it.setData(ROLE_FLAG_DONE, False)
        self.viewport().update()

    def _apply_complete(self, ids: list[int]):
        """Mark follow-ups complete → green ✓ (in place, keeps scroll/order)."""
        for mid in ids:
            self.db.set_flag_done(mid, True)
        idset = set(ids)
        for i in range(self.count()):
            it = self.item(i)
            if it.data(ROLE_MSG_ID) in idset:
                it.setData(ROLE_FLAGGED, True)
                it.setData(ROLE_FLAG_DONE, True)
        self.viewport().update()

    def _apply_pin(self, ids: list[int], value: bool):
        for mid in ids:
            self.db.set_pinned(mid, value)
        self.refresh()  # pinned rows float to the top → reorder needs a rebuild

    # -- hover quick-action clicks --
    def _action_at(self, pos):
        """(row, name) of the quick-action hotspot under *pos*, or None."""
        idx = self.indexAt(pos)
        if not idx.isValid():
            return None
        it = self.item(idx.row())
        mid = it.data(ROLE_MSG_ID) if it else None
        if mid is None or mid <= 0:
            return None  # actions only show on real messages
        for name, slot in action_hotspots(self.visualRect(idx)):
            if slot.contains(pos):
                return (idx.row(), name)
        return None

    def mouseMoveEvent(self, event):
        hot = self._action_at(event.position().toPoint())
        if hot != self._hover_action:
            self._hover_action = hot
            # Pointing hand over a clickable icon; default arrow elsewhere.
            self.viewport().setCursor(
                Qt.CursorShape.PointingHandCursor if hot
                else Qt.CursorShape.ArrowCursor)
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self._hover_action is not None:
            self._hover_action = None
            self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            if idx.isValid():
                it = self.item(idx.row())
                mid = it.data(ROLE_MSG_ID) if it else None
                if mid is not None and mid > 0:
                    pos = event.position().toPoint()
                    for name, slot in action_hotspots(self.visualRect(idx)):
                        if slot.contains(pos):
                            self._do_row_action(name, it, mid)
                            event.accept()
                            return
        super().mousePressEvent(event)

    def _do_row_action(self, name: str, it, mid: int):
        if name == "read":
            # Toggle: unread rows become read, read rows become unread.
            self.mark_read_requested.emit([mid], bool(it.data(ROLE_UNREAD)))
        elif name == "flag":
            self._apply_flag([mid], not bool(it.data(ROLE_FLAGGED)))
        elif name == "pin":
            self._apply_pin([mid], not bool(it.data(ROLE_PINNED)))
        elif name == "delete":
            self.delete_requested.emit([mid])

    def _emit_counts(self):
        """Tell listeners how many real messages are shown and how many are
        unread (outbox rows, which have negative ids, are excluded)."""
        total = unread = 0
        for i in range(self.count()):
            it = self.item(i)
            mid = it.data(ROLE_MSG_ID)
            if mid is None or mid <= 0:
                continue
            total += 1
            if it.data(ROLE_UNREAD):
                unread += 1
        self.counts_changed.emit(total, unread)

    # -- keyboard --
    _NAV_KEYS = (
        Qt.Key.Key_Up, Qt.Key.Key_Down,
        Qt.Key.Key_Home, Qt.Key.Key_End,
        Qt.Key.Key_PageUp, Qt.Key.Key_PageDown,
    )

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.folder in ("OUTBOX", "DRAFTS"):
                ids = self._selected_outbox_ids()
                if ids:
                    self.outbox_delete.emit(ids)
            else:
                ids = self._selected_ids()
                if ids:
                    self.delete_requested.emit(ids)
            return
        if event.key() in self._NAV_KEYS:
            # Let the list move the selection, then open the newly-focused
            # message — keyboard navigation should load the reader just like
            # a click does. (currentItemChanged can't be used here because
            # refresh() restores selection programmatically and would reopen.)
            before = self.currentItem()
            super().keyPressEvent(event)
            it = self.currentItem()
            if it is not None and it is not before:
                mid = it.data(ROLE_MSG_ID)
                if mid is not None and mid > 0:
                    self.message_selected.emit(mid)
            return
        super().keyPressEvent(event)

    # -- context menu --
    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Select All (⌘A)", self.selectAll)
        menu.addSeparator()
        if self.folder in ("OUTBOX", "DRAFTS"):
            ids = self._selected_outbox_ids()
            if not ids:
                menu.exec(event.globalPos())
                return
            failed = {-it.data(ROLE_MSG_ID) for it in self.selectedItems()
                      if it.data(ROLE_UNREAD)}  # ROLE_UNREAD == failed in outbox
            edit_id = self._editable_outbox_id()
            if edit_id is not None:
                menu.addAction("✏️ Edit",
                               lambda: self.outbox_edit.emit(edit_id))
            retry_ids = [i for i in ids if i in failed]
            if retry_ids:
                menu.addAction("↻ Retry",
                               lambda: self.outbox_retry.emit(retry_ids))
            menu.addAction("Delete draft",
                           lambda: self.outbox_delete.emit(ids))
        else:
            ids = self._selected_ids()
            if not ids:
                menu.exec(event.globalPos())
                return
            menu.addAction("Open", lambda: self.message_selected.emit(ids[0]))
            # Copy the mail itself (as .eml) so it can be pasted into a new
            # message — Outlook's "attach this email" in reverse.
            copy_label = ("📋 Copy email" if len(ids) == 1
                          else f"📋 Copy {len(ids)} emails")
            menu.addAction(copy_label, lambda: self.copy_requested.emit(ids))
            menu.addAction("Mark as read",
                           lambda: self.mark_read_requested.emit(ids, True))
            menu.addAction("Mark as unread",
                           lambda: self.mark_read_requested.emit(ids, False))
            menu.addSeparator()
            sel = self.selectedItems()
            any_flagged = any(it.data(ROLE_FLAGGED) for it in sel)
            any_done = any(it.data(ROLE_FLAG_DONE) for it in sel)
            any_pinned = any(it.data(ROLE_PINNED) for it in sel)
            menu.addAction("🚩 Flag", lambda: self._apply_flag(ids, True))
            menu.addAction("✓ Mark complete",
                           lambda: self._apply_complete(ids))
            if any_flagged or any_done:
                menu.addAction("⚑ Clear flag", lambda: self._apply_flag(ids, False))
            if any_pinned:
                menu.addAction("📌 Unpin", lambda: self._apply_pin(ids, False))
            else:
                menu.addAction("📌 Pin to top", lambda: self._apply_pin(ids, True))
            menu.addSeparator()
            if self.folder in ("TRASH", "SPAM"):
                menu.addAction("♻ Restore to Inbox",
                               lambda: self.restore_requested.emit(ids))
            if self.folder == "TRASH":
                menu.addAction("🗑 Delete permanently",
                               lambda: self.delete_requested.emit(ids))
            else:
                menu.addAction("🗑 Delete", lambda: self.delete_requested.emit(ids))
                menu.addAction("⚠ Mark as Spam",
                               lambda: self.spam_requested.emit(ids))
        menu.exec(event.globalPos())
