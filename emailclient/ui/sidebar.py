from __future__ import annotations

import sqlite3

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox, QPushButton,
                               QStyle, QStyledItemDelegate, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from ..db import Database
from ..settings import AppearanceSettings
from . import editor_icons, theme
from .group_dialog import (ColorPickerDialog, IconPickerDialog,
                           NewGroupDialog)

ROLE_ACCOUNT_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1
ROLE_GROUP_ID = Qt.ItemDataRole.UserRole + 2
ROLE_COUNT_TEXT = Qt.ItemDataRole.UserRole + 3   # "44/54" | "120" | ""
ROLE_ICON_NAME = Qt.ItemDataRole.UserRole + 4    # editor_icons name
ROLE_ICON_COLOR = Qt.ItemDataRole.UserRole + 5   # icon tint (hex)
ROLE_ICON_EMOJI = Qt.ItemDataRole.UserRole + 6   # custom group emoji (text)

# Painted icon per folder key (see editor_icons._paint)
FOLDER_ICONS = {"INBOX": "inbox", "SENT": "sent_folder", "OUTBOX": "outbox",
                "DRAFTS": "compose",
                "TRASH": "trash", "SPAM": "spam"}

# Per-account folder children (under every account row, collapsed by default)
ACCOUNT_FOLDERS = (("Inbox", "INBOX"), ("Sent", "SENT"),
                   ("Trash", "TRASH"), ("Spam", "SPAM"))


class SidebarDelegate(QStyledItemDelegate):
    """Paints sidebar rows: 16px painted icon, name, right-aligned dim count.

    Items carry their parts via roles (ROLE_ICON_NAME / ROLE_ICON_COLOR /
    ROLE_COUNT_TEXT) instead of emoji-and-count baked into the label text.
    """

    ROW_H = 30
    PAD = 8
    ICON = 16

    def sizeHint(self, option, index):
        explicit = index.data(Qt.ItemDataRole.SizeHintRole)
        if explicit is not None:
            return explicit  # spacer rows keep their thin 8px hint
        base = super().sizeHint(option, index)
        return QSize(base.width(), self.ROW_H)

    def paint(self, painter, option, index):
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 1, -4, -1)
        if bool(index.flags() & Qt.ItemFlag.ItemIsSelectable):
            if option.state & QStyle.StateFlag.State_Selected:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.GOLD_SOFT))
                painter.drawRoundedRect(rect, 8, 8)
            elif option.state & QStyle.StateFlag.State_MouseOver:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(theme.PANEL_DARK))
                painter.drawRoundedRect(rect, 8, 8)
        if not text:  # spacer rows
            painter.restore()
            return

        x = rect.left() + self.PAD
        emoji = index.data(ROLE_ICON_EMOJI)
        icon_name = index.data(ROLE_ICON_NAME)
        if emoji:
            ef = QFont(option.font)
            ef.setPixelSize(14)
            painter.setFont(ef)
            painter.setPen(QColor(theme.TEXT))
            painter.drawText(QRect(x - 1, rect.top(), self.ICON + 2,
                                   rect.height()),
                             Qt.AlignmentFlag.AlignCenter, emoji)
            x += self.ICON + self.PAD
        elif icon_name:
            color = index.data(ROLE_ICON_COLOR) or theme.TEXT_DIM
            ico = editor_icons.icon(icon_name, color)
            iy = rect.top() + (rect.height() - self.ICON) // 2
            ico.paint(painter, QRect(x, iy, self.ICON, self.ICON))
            x += self.ICON + self.PAD

        # Count: right-aligned, faint 11px; whole count goes accent DemiBold
        # when it is "u/t" with unread > 0.
        right = rect.right() - self.PAD
        count = index.data(ROLE_COUNT_TEXT) or ""
        if count:
            unread_part = count.split("/", 1)[0]
            hot = ("/" in count and unread_part.isdigit()
                   and int(unread_part) > 0)
            cf = QFont(option.font)
            cf.setPixelSize(11)
            if hot:
                cf.setWeight(QFont.Weight.DemiBold)
            painter.setFont(cf)
            cw = QFontMetrics(cf).horizontalAdvance(count)
            painter.setPen(QColor(theme.GOLD_DEEP if hot else theme.TEXT_FAINT))
            painter.drawText(QRect(right - cw, rect.top(), cw, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter
                             | Qt.AlignmentFlag.AlignRight, count)
            right -= cw + self.PAD

        # Name: item font (DemiBold-on-unread etc.), elided to the count edge
        font = index.data(Qt.ItemDataRole.FontRole) or QFont(option.font)
        fg = index.data(Qt.ItemDataRole.ForegroundRole)
        painter.setFont(font)
        painter.setPen(fg.color() if fg is not None else QColor(theme.TEXT))
        fm = QFontMetrics(font)
        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight,
                               max(right - x, 0))
        painter.drawText(QRect(x, rect.top(), max(right - x, 0), rect.height()),
                         Qt.AlignmentFlag.AlignVCenter
                         | Qt.AlignmentFlag.AlignLeft, elided)
        painter.restore()


def detect_folder_delim(folders: list[str]) -> str:
    """Best-effort IMAP hierarchy delimiter for a set of folder names. Prefer
    '/' (the common case across Gmail/Outlook/most servers); fall back to '.'
    (Dovecot-style) only when no name contains a '/'. Defaults to '/'."""
    if any("/" in f for f in folders):
        return "/"
    if any("." in f for f in folders):
        return "."
    return "/"


def add_folder_children(item: QTreeWidgetItem, account_id: int,
                        folder_counts: dict | None = None,
                        custom_folders: list[str] | None = None) -> None:
    """Append the per-account folder rows to *item*: the built-in Inbox/Sent/
    Trash/Spam, then any custom (user-created) server folders, nested as a tree
    under their parents ("Work/Projects/Bank" -> Work ▸ Projects ▸ Bank).

    *folder_counts*: {(account_id, folder): (unread, total)} — Inbox shows
    "unread/total", other folders show just their total.
    *custom_folders*: extra server folder names to list under the account.
    """
    small = QFont()
    small.setPointSizeF(small.pointSizeF() - 1)

    def _row(parent, text, folder, icon):
        count = ""
        unread = 0
        if folder_counts is not None:
            unread, total = folder_counts.get((account_id, folder), (0, 0))
            # Any folder with unread mail shows "unread/total" — a filter rule
            # can drop new mail into a custom folder, and a bare total hid it.
            if total and (unread or folder == "INBOX"):
                count = f"{unread}/{total}"
            elif total:
                count = str(total)
        child = QTreeWidgetItem([text])
        child.setData(0, ROLE_ACCOUNT_ID, account_id)
        child.setData(0, ROLE_FOLDER, folder)
        child.setData(0, ROLE_COUNT_TEXT, count)
        child.setData(0, ROLE_ICON_NAME, icon)
        child.setData(0, ROLE_ICON_COLOR, theme.TEXT_DIM)
        child.setForeground(0, QColor(theme.TEXT if unread else theme.TEXT_DIM))
        row_font = QFont(small)
        if unread:
            row_font.setWeight(QFont.Weight.DemiBold)
        child.setFont(0, row_font)
        parent.addChild(child)
        return child

    for text, folder in ACCOUNT_FOLDERS:
        _row(item, text, folder, FOLDER_ICONS[folder])

    # Custom folders → nested tree. Split each full name on the account's
    # hierarchy delimiter and create one node per path prefix, reusing parents
    # so siblings share them. Intermediate containers that the server didn't
    # list on their own still get a node so the hierarchy stays intact.
    folders = sorted(custom_folders or [], key=str.lower)
    delim = detect_folder_delim(folders)
    nodes: dict[str, QTreeWidgetItem] = {}
    for folder in folders:
        parts = folder.split(delim) if delim else [folder]
        prefix = ""
        parent_item = item
        for i, part in enumerate(parts):
            prefix = part if i == 0 else prefix + delim + part
            # Some servers (e.g. Zoho) nest user folders under the INBOX
            # namespace ("INBOX/Newsletter"). Don't draw a second "INBOX" node
            # that duplicates the built-in Inbox — skip the namespace component
            # and let its children sit at the top level. ROLE_FOLDER still
            # carries the full server path, so sync/selection are unaffected.
            if prefix == "INBOX":
                continue
            existing = nodes.get(prefix)
            if existing is not None:
                parent_item = existing
                continue
            parent_item = _row(parent_item, part or prefix, prefix, "folder")
            nodes[prefix] = parent_item


class _FilterManagerDialog(QDialog):
    """See and manage all filters (sender → folder rules) for an account."""

    def __init__(self, db, account_id: int, folder: str | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.account_id = account_id
        self._folder = folder
        self.setWindowTitle(f"Filters — {folder}" if folder else "Mail filters")
        self.resize(480, 360)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Incoming mail matching a rule is moved to its "
                             "folder. Select a rule and Remove to delete it."))
        self.listw = QListWidget()
        lay.addWidget(self.listw, 1)
        row = QHBoxLayout()
        self.remove_btn = QPushButton("✖ Remove selected")
        self.remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.remove_btn.clicked.connect(self._remove_selected)
        row.addWidget(self.remove_btn)
        row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        lay.addLayout(row)
        self._reload()

    def _reload(self):
        self.listw.clear()
        rules = [r for r in self.db.list_folder_rules(self.account_id)
                 if self._folder is None or r["folder"] == self._folder]
        for r in rules:
            it = QListWidgetItem(
                f"From “{r['match_from']}”   →   {r['folder']}")
            it.setData(Qt.ItemDataRole.UserRole, r["id"])
            self.listw.addItem(it)
        if not rules:
            self.listw.addItem("No filters set.")
            self.remove_btn.setEnabled(False)
        else:
            self.remove_btn.setEnabled(True)
            self.listw.setCurrentRow(0)

    def _remove_selected(self):
        it = self.listw.currentItem()
        if it is None:
            return
        rid = it.data(Qt.ItemDataRole.UserRole)
        if rid is not None:
            self.db.delete_folder_rule(rid)
            self._reload()


class Sidebar(QTreeWidget):
    """Account tree: folders + user groups + provider groups with unread badges."""

    selection_changed = Signal(object, str)  # (account_id | ("group", gid) | None, folder)
    settings_requested = Signal(int)          # account_id
    remove_requested = Signal(int)            # account_id
    remove_multi_requested = Signal(list)     # [account_id, ...] — bulk remove
    bulk_edit_requested = Signal(list)        # [account_id, ...] — bulk edit name/server/pw
    groups_changed = Signal()                 # groups created/renamed/deleted/membership
    open_group_window = Signal(int)           # group_id -> open in separate window
    pause_toggled = Signal(int, bool)         # account_id, paused
    folder_created = Signal(int)              # account_id -> create folder on server now
    marked_all_read = Signal()               # all-as-read done -> reload current view
    sync_requested = Signal(list)            # [account_id, ...] | [] for every account

    STATUS_COLORS = {"connected": "#34A853", "reconnecting": "#F4B400",
                     "error": "#EA4335"}
    STATUS_TIPS = {
        "connected": "🟢 Connected — live sync",
        "reconnecting": "🟡 Reconnecting…",
        "error": "🔴 Login problem — open Settings",
        "paused": "⏸ Paused — sync is off",
    }
    DEFAULT_TIP = "● Connecting…"

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._statuses: dict[int, str] = {}
        self._filter = ""   # account search text (see set_filter)
        self.setHeaderHidden(True)
        self.setIndentation(18)
        self.setRootIsDecorated(True)  # tree arrows: groups visibly contain accounts
        # No keyboard focus needed — also kills the native blue focus ring
        # painted around the branch/arrow region on click.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Ctrl/Shift multi-select: move several accounts to a group at once.
        self.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setItemDelegate(SidebarDelegate(self))
        # Delegate paints its own hover fill — needs hover events delivered.
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.itemClicked.connect(self._on_click)
        self.itemDoubleClicked.connect(self._on_double_click)
        # Status changes from 50+ workers can arrive in bursts; coalesce the
        # resulting repaints instead of rebuilding the tree per status event.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(400)
        self._status_timer.timeout.connect(self.refresh)
        self.refresh()

    def set_filter(self, text: str) -> None:
        """Narrow the tree to whatever matches *text* — accounts (name / email /
        provider) and groups (group name, which brings its accounts with it).
        With 50+ accounts, scrolling to find one is the slow part."""
        text = (text or "").strip().lower()
        if text == self._filter:
            return
        self._filter = text
        self.refresh()

    def _matches(self, acc) -> bool:
        q = self._filter
        if not q:
            return True
        return (q in (acc.name or "").lower()
                or q in (acc.email or "").lower()
                or q in (acc.provider or "").lower())

    def set_status(self, account_id: int, status: str) -> None:
        """Remember an account's connection status and schedule a repaint."""
        if self._statuses.get(account_id) == status:
            return
        self._statuses[account_id] = status
        self._status_timer.start()

    def _account_item(self, acc, unread: int, total: int,
                      folder_counts: dict | None = None) -> QTreeWidgetItem:
        if acc.paused:
            # Paused overrides any live connection status color.
            dot_color = theme.TEXT_FAINT
            tip = self.STATUS_TIPS["paused"]
        else:
            status = self._statuses.get(acc.id)
            dot_color = self.STATUS_COLORS.get(status, theme.TEXT_FAINT)
            tip = self.STATUS_TIPS.get(status, self.DEFAULT_TIP)
        # Show the email address instead of the display name when the user has
        # turned that on in Appearance settings (falls back to the other if one
        # is empty).
        label = (acc.email or acc.name) if AppearanceSettings().show_email \
            else (acc.name or acc.email)
        item = QTreeWidgetItem([label])
        item.setToolTip(0, tip)
        item.setData(0, ROLE_ACCOUNT_ID, acc.id)
        item.setData(0, ROLE_FOLDER, "INBOX")
        item.setData(0, ROLE_GROUP_ID, acc.group_id)
        item.setData(0, ROLE_ICON_NAME, "dot")
        item.setData(0, ROLE_ICON_COLOR, dot_color)
        item.setData(0, ROLE_COUNT_TEXT, f"{unread}/{total}" if total else "")
        if acc.paused:
            item.setForeground(0, QColor(theme.TEXT_FAINT))
        else:
            # Account label stays a neutral dark grey; the account's color shows
            # on its avatar/dots, not the text.
            item.setForeground(0, QColor(theme.TEXT))
        if unread:
            bf = QFont()
            bf.setWeight(QFont.Weight.DemiBold)
            item.setFont(0, bf)
        add_folder_children(item, acc.id, folder_counts,
                            self.db.list_account_folders(acc.id))
        return item

    def _add_section_gap(self) -> None:
        """Thin non-interactive spacer row — breathing room between sections."""
        gap = QTreeWidgetItem([""])
        gap.setFlags(Qt.ItemFlag.NoItemFlags)
        gap.setSizeHint(0, QSize(0, 8))
        self.addTopLevelItem(gap)

    def _expanded_account_ids(self) -> set[int]:
        """Account ids whose folder children are currently expanded."""
        ids = set()
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            for j in range(top.childCount()):
                child = top.child(j)
                acc_id = child.data(0, ROLE_ACCOUNT_ID)
                if acc_id is not None and child.isExpanded():
                    ids.add(acc_id)
        return ids

    def _top_key(self, item):
        """Stable identity for a top-level header (group or provider), used to
        remember whether the user collapsed it across a rebuild."""
        gid = item.data(0, ROLE_GROUP_ID)
        acc_id = item.data(0, ROLE_ACCOUNT_ID)
        if gid is not None and acc_id is None:
            return ("group", gid)
        if acc_id is None and gid is None and item.childCount() > 0:
            return ("prov", item.text(0))
        return None

    def _collapsed_top_keys(self) -> set:
        """Top-level headers the user has collapsed — so selecting an email (which
        triggers a refresh) doesn't force every group/provider open again."""
        out = set()
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            if top.childCount() > 0 and not top.isExpanded():
                k = self._top_key(top)
                if k is not None:
                    out.add(k)
        return out

    def _selection_key(self):
        """Identity of the currently selected row, stable across a rebuild:
        (group_id, account_id, folder, is_leaf). is_leaf separates an account
        row from its own 'Inbox' child (both share account_id + INBOX)."""
        it = self.currentItem()
        if it is None:
            return None
        return (it.data(0, ROLE_GROUP_ID), it.data(0, ROLE_ACCOUNT_ID),
                it.data(0, ROLE_FOLDER), it.childCount() == 0)

    def _restore_selection(self, key):
        """Re-select the row matching *key* after a rebuild so the folder the
        user is viewing stays highlighted (clicking an email used to clear it)."""
        if key is None:
            return

        def walk(parent):
            n = parent.childCount() if parent else self.topLevelItemCount()
            for i in range(n):
                item = parent.child(i) if parent else self.topLevelItem(i)
                k = (item.data(0, ROLE_GROUP_ID), item.data(0, ROLE_ACCOUNT_ID),
                     item.data(0, ROLE_FOLDER), item.childCount() == 0)
                if k == key and bool(item.flags() & Qt.ItemFlag.ItemIsSelectable):
                    self.setCurrentItem(item)
                    return True
                if walk(item):
                    return True
            return False

        walk(None)

    def refresh(self):
        # Status-debounce refreshes happen often — remember which account
        # rows the user expanded so the tree doesn't snap shut on rebuild,
        # and which folder is selected so its highlight survives the rebuild.
        expanded = self._expanded_account_ids()
        collapsed_tops = self._collapsed_top_keys()
        sel_key = self._selection_key()
        self.clear()
        accounts = self.db.list_accounts()
        counts = self.db.counts_by_account()  # one query for all accounts
        folder_counts = self.db.counts_by_account_folder()
        unread = {a.id: counts.get(a.id, (0, 0))[0] for a in accounts}
        totals = {a.id: counts.get(a.id, (0, 0))[1] for a in accounts}
        total_unread = sum(unread.values())
        total_all = sum(totals.values())
        searching = bool(self._filter)
        if searching:
            self._build_search_results(accounts, unread, totals, folder_counts)
            return

        all_item = QTreeWidgetItem(["All Inboxes"])
        all_item.setData(0, ROLE_ACCOUNT_ID, None)
        all_item.setData(0, ROLE_FOLDER, "INBOX")
        all_item.setData(0, ROLE_ICON_NAME, "inbox")
        all_item.setData(0, ROLE_ICON_COLOR, theme.TEXT_DIM)
        all_item.setData(0, ROLE_COUNT_TEXT,
                         f"{total_unread}/{total_all}" if total_all else "")
        f = QFont()
        f.setWeight(QFont.Weight.DemiBold)
        all_item.setFont(0, f)
        self.addTopLevelItem(all_item)

        # Drafts/Outbox carry a count so an unsent message is never forgotten:
        # the row goes DemiBold with the number next to it, like an unread inbox.
        pending = {"DRAFTS": self.db.draft_count(),
                   "OUTBOX": len(self.db.list_outbox())}
        for text, folder in (("Sent", "SENT"), ("Drafts", "DRAFTS"),
                             ("Outbox", "OUTBOX"),
                             ("Trash", "TRASH"), ("Spam", "SPAM")):
            it = QTreeWidgetItem([text])
            it.setData(0, ROLE_ACCOUNT_ID, None)
            it.setData(0, ROLE_FOLDER, folder)
            it.setData(0, ROLE_ICON_NAME, FOLDER_ICONS[folder])
            it.setData(0, ROLE_ICON_COLOR, theme.TEXT_DIM)
            n = pending.get(folder, 0)
            if n:
                it.setData(0, ROLE_COUNT_TEXT, str(n))
                pf = QFont()
                pf.setWeight(QFont.Weight.DemiBold)
                it.setFont(0, pf)
            self.addTopLevelItem(it)

        # User-defined groups: selectable header (combined inbox) + member accounts
        groups = self.db.list_groups()
        if groups:
            self._add_section_gap()
        for grp in groups:
            gid = grp["id"]
            members = [a for a in accounts if a.group_id == gid]
            g_unread = sum(unread[a.id] for a in members)
            g_total = sum(totals[a.id] for a in members)
            # 🔕 next to the name when this group's notifications are muted, so
            # it's obvious at a glance which groups won't pop up.
            muted = not bool(grp["notify_enabled"])
            header = QTreeWidgetItem([grp["name"] + ("  🔕" if muted else "")])
            if muted:
                header.setToolTip(0, "Notifications are OFF for this group")
            header.setData(0, ROLE_GROUP_ID, gid)
            header.setData(0, ROLE_FOLDER, "INBOX")
            header.setData(0, ROLE_COUNT_TEXT,
                           f"{g_unread}/{g_total}" if g_total else "")
            icon = grp["icon"]
            if icon and icon != "📁":
                # Custom emoji group icon: rendered as text in the icon slot
                header.setData(0, ROLE_ICON_EMOJI, icon)
            else:
                header.setData(0, ROLE_ICON_NAME, "folder")
                header.setData(0, ROLE_ICON_COLOR, grp["color"])
            hf = QFont()
            hf.setWeight(QFont.Weight.DemiBold)
            header.setFont(0, hf)
            header.setForeground(0, QColor(grp["color"]))
            self.addTopLevelItem(header)
            for acc in members:
                header.addChild(self._account_item(acc, unread[acc.id],
                                                   totals[acc.id],
                                                   folder_counts))

        # Ungrouped accounts under provider headers (non-selectable, as before)
        providers: dict[str, QTreeWidgetItem] = {}
        for acc in accounts:
            if acc.group_id is not None:
                continue
            if acc.provider not in providers:
                if not providers:
                    self._add_section_gap()  # gap before the providers section
                g = QTreeWidgetItem([acc.provider.upper()])
                g.setFlags(g.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                gf = QFont()
                gf.setPointSizeF(gf.pointSizeF() - 3)
                gf.setWeight(QFont.Weight.DemiBold)
                gf.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)
                g.setFont(0, gf)
                g.setForeground(0, QColor(theme.TEXT_FAINT))
                providers[acc.provider] = g
                self.addTopLevelItem(g)
            providers[acc.provider].addChild(
                self._account_item(acc, unread[acc.id], totals[acc.id],
                                   folder_counts))
        # Group/provider headers default to expanded, but stay collapsed if the
        # user had collapsed them (so selecting an email doesn't re-open every
        # group). Account-folder children stay collapsed unless previously open.
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            top.setExpanded(self._top_key(top) not in collapsed_tops)
            for j in range(top.childCount()):
                child = top.child(j)
                child.setExpanded(child.data(0, ROLE_ACCOUNT_ID) in expanded)

        self._restore_selection(sel_key)

    def _build_search_results(self, accounts, unread, totals,
                              folder_counts) -> None:
        """One box searches both: matching GROUPS come first as real group rows
        (selectable, with their accounts under them), then the accounts that
        matched on their own — flat, because during a search the provider
        headers only stand between the user and what they typed."""
        q = self._filter
        groups = self.db.list_groups()
        group_hits = [g for g in groups if q in (g["name"] or "").lower()]
        members = {g["id"]: [a for a in accounts if a.group_id == g["id"]]
                   for g in group_hits}
        shown_ids = {a.id for g in group_hits for a in members[g["id"]]}
        acc_hits = [a for a in accounts
                    if self._matches(a) and a.id not in shown_ids]
        if not group_hits and not acc_hits:
            empty = QTreeWidgetItem([f'Nothing matches "{self._filter}"'])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            empty.setForeground(0, QColor(theme.TEXT_FAINT))
            self.addTopLevelItem(empty)
            return

        for grp in group_hits:
            gid = grp["id"]
            g_unread = sum(unread[a.id] for a in members[gid])
            g_total = sum(totals[a.id] for a in members[gid])
            header = QTreeWidgetItem([grp["name"]])
            header.setData(0, ROLE_GROUP_ID, gid)
            header.setData(0, ROLE_FOLDER, "INBOX")
            header.setData(0, ROLE_COUNT_TEXT,
                           f"{g_unread}/{g_total}" if g_total else "")
            icon = grp["icon"]
            if icon and icon != "📁":
                header.setData(0, ROLE_ICON_EMOJI, icon)
            else:
                header.setData(0, ROLE_ICON_NAME, "folder")
                header.setData(0, ROLE_ICON_COLOR, grp["color"])
            hf = QFont()
            hf.setWeight(QFont.Weight.DemiBold)
            header.setFont(0, hf)
            header.setForeground(0, QColor(grp["color"]))
            header.setToolTip(0, f"Group — {len(members[gid])} account(s)")
            self.addTopLevelItem(header)
            for acc in members[gid]:
                header.addChild(self._account_item(acc, unread[acc.id],
                                                   totals[acc.id],
                                                   folder_counts))
            header.setExpanded(True)

        if group_hits and acc_hits:
            self._add_section_gap()
        group_names = {g["id"]: g["name"] for g in groups}
        for acc in acc_hits:
            item = self._account_item(acc, unread[acc.id], totals[acc.id],
                                      folder_counts)
            where = group_names.get(acc.group_id) or acc.provider.upper()
            item.setToolTip(0, f"{item.toolTip(0)}\n{where} — {acc.email}")
            self.addTopLevelItem(item)

    def _on_click(self, item, _col):
        if not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            return
        gid = item.data(0, ROLE_GROUP_ID)
        acc_id = item.data(0, ROLE_ACCOUNT_ID)
        if gid is not None and acc_id is None:
            self.selection_changed.emit(("group", gid),
                                        item.data(0, ROLE_FOLDER) or "INBOX")
        else:
            self.selection_changed.emit(acc_id,
                                        item.data(0, ROLE_FOLDER) or "INBOX")

    @staticmethod
    def _is_account_item(item: QTreeWidgetItem) -> bool:
        """True for account rows: has an account id, INBOX folder and folder
        children (excludes the "Inbox" leaf rows). Search results list account
        rows at top level, so the row's depth doesn't matter."""
        return (item.data(0, ROLE_ACCOUNT_ID) is not None
                and item.data(0, ROLE_FOLDER) == "INBOX"
                and item.childCount() > 0)

    def _selected_account_ids(self) -> list[int]:
        """Account ids of all currently selected account rows."""
        return [it.data(0, ROLE_ACCOUNT_ID) for it in self.selectedItems()
                if self._is_account_item(it)]

    def _on_double_click(self, item, _col):
        gid = item.data(0, ROLE_GROUP_ID)
        acc_id = item.data(0, ROLE_ACCOUNT_ID)
        if gid is not None and acc_id is None:
            self.open_group_window.emit(gid)

    def _exec_menu(self, menu: QMenu, pos):
        """Pop up a context menu at *pos* and return the chosen action.
        One seam for every menu here — QMenu.exec is a C++ slot that cannot
        be patched, so tests drive menus by replacing this method."""
        return menu.exec(self.viewport().mapToGlobal(pos))

    def _do_mark_all_read(self, account_ids, folder):
        """Mark all unread read for the given scope, refresh the tree counts and
        tell the window to reload the open view."""
        self.db.mark_all_read(account_ids, folder)
        self.refresh()
        self.marked_all_read.emit()

    def _simple_mark_read_menu(self, pos, account_ids, folder):
        """Tiny context menu (just 'Mark all as read') for folder rows that have
        no other actions — the unified top folders and account folder leaves."""
        menu = QMenu(self)
        sync_act = menu.addAction("⟳ Sync latest now")
        act = menu.addAction("✓ Mark all as read")
        chosen = self._exec_menu(menu, pos)
        if chosen == act:
            self._do_mark_all_read(account_ids, folder)
        elif chosen == sync_act:
            # No account ids on the unified rows → every account.
            self.sync_requested.emit(list(account_ids or []))

    def _on_context_menu(self, pos):
        item = self.itemAt(pos)
        if item is None:
            return
        acc_id = item.data(0, ROLE_ACCOUNT_ID)
        gid = item.data(0, ROLE_GROUP_ID)
        folder = item.data(0, ROLE_FOLDER)
        if acc_id is None and gid is not None:
            self._group_header_menu(pos, gid)
            return
        # Unified top-level folders (All Inboxes / Sent / Trash / Spam): the only
        # action is mark-all-read across every account for that folder.
        if acc_id is None and gid is None and folder:
            self._simple_mark_read_menu(pos, None, folder)
            return
        # Custom folder row → filter / delete menu.
        if (acc_id is not None and folder
                and folder not in ("INBOX", "SENT", "TRASH", "SPAM")):
            self._folder_menu(pos, acc_id, folder)
            return
        # A standard-folder leaf under an account (its Inbox/Sent/Trash/Spam) →
        # mark-all-read for just that account + folder.
        if (acc_id is not None and folder in ("INBOX", "SENT", "TRASH", "SPAM")
                and not self._is_account_item(item)):
            self._simple_mark_read_menu(pos, [acc_id], folder)
            return
        # Only show the full account menu for account items (see _is_account_item).
        if folder != "INBOX" or not self._is_account_item(item):
            return
        # Multi-select: 2+ account rows selected (incl. the clicked one) →
        # group-move menu applying to all of them.
        selected = self._selected_account_ids()
        if len(selected) >= 2 and acc_id in selected:
            self._multi_account_menu(pos, selected)
            return
        acc = next((a for a in self.db.list_accounts() if a.id == acc_id), None)
        is_paused = bool(acc and acc.paused)
        menu = QMenu(self)
        sync_action = menu.addAction("⟳ Sync latest now")
        sync_action.setToolTip(
            "Fetch new mail for this account right away — syncing of older "
            "history keeps running in the background")
        sync_action.setEnabled(not is_paused)
        mark_read_action = menu.addAction("✓ Mark all as read")
        menu.addSeparator()
        settings_action = menu.addAction("⚙ Account Settings…")
        new_folder_action = menu.addAction("📁 New Folder…")
        pause_action = menu.addAction(
            "▶ Resume Account" if is_paused else "⏸ Pause Account")
        remove_action = menu.addAction("🗑 Remove Account")
        group_menu = menu.addMenu("📁 Group")
        group_actions = {}
        for grp in self.db.list_groups():
            a = group_menu.addAction(grp["name"])
            a.setCheckable(True)
            a.setChecked(gid == grp["id"])
            group_actions[a] = grp["id"]
        group_menu.addSeparator()
        new_group_action = group_menu.addAction("➕ New Group…")
        no_group_action = group_menu.addAction("❌ No group")
        action = self._exec_menu(menu, pos)
        if action is None:
            return
        if action == sync_action:
            self.sync_requested.emit([acc_id])
        elif action == mark_read_action:
            self._do_mark_all_read([acc_id], "INBOX")
        elif action == settings_action:
            self.settings_requested.emit(acc_id)
        elif action == new_folder_action:
            name, ok = QInputDialog.getText(self, "New Folder",
                                            "Folder name:")
            name = (name or "").strip()
            if ok and name:
                # Show it immediately, queue the server-side create, then nudge
                # the worker to run the op now (so it appears on the server).
                self.db.add_account_folder(acc_id, name)
                self.db.add_op(acc_id, "create_folder", name, 0)
                self.refresh()
                self.folder_created.emit(acc_id)
        elif action == pause_action:
            self.pause_toggled.emit(acc_id, not is_paused)
        elif action == remove_action:
            self.remove_requested.emit(acc_id)
        elif action == new_group_action:
            dlg = NewGroupDialog(self)
            if dlg.exec():
                try:
                    new_gid = self.db.add_group(dlg.name(), dlg.color(),
                                                dlg.icon())
                except sqlite3.IntegrityError:
                    QMessageBox.warning(self, "New Group",
                                        "A group with this name already exists")
                    return
                self.db.set_account_group(acc_id, new_gid)
                self._after_group_change()
        elif action == no_group_action:
            self.db.set_account_group(acc_id, None)
            self._after_group_change()
        elif action in group_actions:
            self.db.set_account_group(acc_id, group_actions[action])
            self._after_group_change()

    def _folder_menu(self, pos, acc_id: int, folder: str):
        """Right-click on a custom folder: add a filter (sender -> this folder)
        or delete the folder."""
        menu = QMenu(self)
        mark_read_action = menu.addAction("✓ Mark all as read")
        menu.addSeparator()
        new_subfolder = menu.addAction("📁 New Subfolder…")
        add_filter = menu.addAction("🔎 Filter mail into this folder…")
        folder_rules = [r for r in self.db.list_folder_rules(acc_id)
                        if r["folder"] == folder]
        manage_filters = (menu.addAction(f"⚙ Manage filters ({len(folder_rules)})…")
                          if folder_rules else None)
        menu.addSeparator()
        delete_folder = menu.addAction("🗑 Delete folder")
        action = self._exec_menu(menu, pos)
        if action is None:
            return
        if action == mark_read_action:
            self._do_mark_all_read([acc_id], folder)
        elif action == new_subfolder:
            name, ok = QInputDialog.getText(
                self, "New Subfolder", f"New folder inside “{folder}”:")
            name = (name or "").strip()
            if ok and name:
                delim = detect_folder_delim(self.db.list_account_folders(acc_id))
                # Don't double up the separator if the user typed one.
                full = folder + delim + name.lstrip(delim)
                self.db.add_account_folder(acc_id, full)
                self.db.add_op(acc_id, "create_folder", full, 0)
                self.refresh()
                self.folder_created.emit(acc_id)
        elif action == add_filter:
            pat, ok = QInputDialog.getText(
                self, "Folder filter",
                "Move incoming mail whose sender contains this "
                "(an email address or domain):")
            pat = (pat or "").strip()
            if ok and pat:
                self.db.add_folder_rule(acc_id, pat, folder)
                QMessageBox.information(
                    self, "Filter added",
                    f"New mail from “{pat}” will now go to “{folder}”.")
        elif manage_filters is not None and action == manage_filters:
            _FilterManagerDialog(self.db, acc_id, folder, self).exec()
        elif action == delete_folder:
            ans = QMessageBox.question(
                self, "Delete folder",
                f"Delete the folder “{folder}” (and any subfolders) on the "
                "server?\nTheir mails are removed from this app.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans == QMessageBox.StandardButton.Yes:
                delim = detect_folder_delim(self.db.list_account_folders(acc_id))
                # Delete the folder and every subfolder, deepest first (a server
                # usually refuses to delete a folder that still has children).
                for name in self.db.account_folder_tree(acc_id, folder, delim):
                    self.db.remove_account_folder(acc_id, name)
                    self.db.add_op(acc_id, "delete_folder", name, 0)
                self.refresh()
                self.folder_created.emit(acc_id)   # restart worker to run the op

    def _multi_account_menu(self, pos, account_ids: list[int]):
        """Context menu when several account rows are selected: move them all
        to a group at once. Single-account actions are hidden on purpose."""
        n = len(account_ids)
        menu = QMenu(self)
        hint = menu.addAction(f"{n} accounts selected")
        hint.setEnabled(False)
        sync_action = menu.addAction(f"⟳ Sync latest for {n} accounts")
        group_menu = menu.addMenu(f"📁 Move {n} accounts to group")
        group_actions = {}
        for grp in self.db.list_groups():
            group_actions[group_menu.addAction(grp["name"])] = grp["id"]
        group_menu.addSeparator()
        new_group_action = group_menu.addAction("➕ New Group…")
        no_group_action = group_menu.addAction("❌ No group")
        menu.addSeparator()
        edit_action = menu.addAction(f"✏️ Bulk edit {n} accounts…")
        remove_action = menu.addAction(f"🗑 Remove {n} accounts")
        action = self._exec_menu(menu, pos)
        if action is None:
            return
        if action == sync_action:
            self.sync_requested.emit(list(account_ids))
            return
        if action == edit_action:
            self.bulk_edit_requested.emit(list(account_ids))
            return
        if action == remove_action:
            self.remove_multi_requested.emit(list(account_ids))
            return
        if action == new_group_action:
            dlg = NewGroupDialog(self)
            if not dlg.exec():
                return
            try:
                target_gid = self.db.add_group(dlg.name(), dlg.color(),
                                               dlg.icon())
            except sqlite3.IntegrityError:
                QMessageBox.warning(self, "New Group",
                                    "A group with this name already exists")
                return
        elif action == no_group_action:
            target_gid = None
        elif action in group_actions:
            target_gid = group_actions[action]
        else:
            return
        for acc_id in account_ids:
            self.db.set_account_group(acc_id, target_gid)
        self._after_group_change()

    def _group_header_menu(self, pos, gid: int):
        menu = QMenu(self)
        mark_read_action = menu.addAction("✓ Mark all as read")
        menu.addSeparator()
        notify_on = self.db.group_notify_enabled(gid)
        notify_action = menu.addAction(
            "🔕 Turn notifications OFF" if notify_on else "🔔 Turn notifications ON")
        menu.addSeparator()
        open_window_action = menu.addAction("↗ Open in separate window")
        rename_action = menu.addAction("✏ Rename Group")
        icon_action = menu.addAction("🎨 Change icon…")
        color_action = menu.addAction("🌈 Change color…")
        delete_action = menu.addAction("🗑 Delete Group")
        action = self._exec_menu(menu, pos)
        if action == mark_read_action:
            self._do_mark_all_read(self.db.accounts_in_group(gid), "INBOX")
        elif action == notify_action:
            # Toggle: emails from every account in this group stop/start popping up.
            self.db.set_group_notify(gid, not notify_on)
            self._after_group_change()
        elif action == open_window_action:
            self.open_group_window.emit(gid)
        elif action == icon_action:
            current = "📁"
            for grp in self.db.list_groups():
                if grp["id"] == gid:
                    current = grp["icon"]
                    break
            dlg = IconPickerDialog(current, self)
            if dlg.exec():
                self.db.set_group_icon(gid, dlg.icon())
                self._after_group_change()
        elif action == color_action:
            current = ""
            for grp in self.db.list_groups():
                if grp["id"] == gid:
                    current = grp["color"]
                    break
            dlg = ColorPickerDialog(current, self)
            if dlg.exec():
                self.db.set_group_color(gid, dlg.color())
                self._after_group_change()
        elif action == rename_action:
            current = ""
            for grp in self.db.list_groups():
                if grp["id"] == gid:
                    current = grp["name"]
                    break
            name, ok = QInputDialog.getText(self, "Rename Group",
                                            "New name:", text=current)
            name = name.strip()
            if ok and name:
                try:
                    self.db.rename_group(gid, name)
                except sqlite3.IntegrityError:
                    QMessageBox.warning(self, "Rename Group",
                                        "A group with this name already exists")
                    return
                self._after_group_change()
        elif action == delete_action:
            reply = QMessageBox.question(
                self, "Delete Group",
                "The group will be deleted; its accounts stay (ungrouped)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.Yes:
                self.db.delete_group(gid)
                self._after_group_change()

    def _after_group_change(self):
        self.refresh()
        self.groups_changed.emit()


class SidebarPanel(QWidget):
    """The sidebar tree with an account search box above it."""

    def __init__(self, db: Database):
        super().__init__()
        self.tree = Sidebar(db)
        self.search = QLineEdit()
        self.search.setObjectName("searchBox")
        self.search.setPlaceholderText("Search accounts & groups…")
        self.search.addAction(editor_icons.icon("search", theme.TEXT_FAINT),
                              QLineEdit.ActionPosition.LeadingPosition)
        self.search.setClearButtonEnabled(True)
        # Typing in a 50+ account list would rebuild the tree per keystroke;
        # coalesce into one rebuild once typing settles.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(
            lambda: self.tree.set_filter(self.search.text()))
        self.search.textChanged.connect(lambda _: self._timer.start())
        # Esc clears the filter and returns the full tree.
        self.search.returnPressed.connect(self._timer.stop)
        self.search.returnPressed.connect(
            lambda: self.tree.set_filter(self.search.text()))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 0)
        lay.setSpacing(6)
        lay.addWidget(self.search)
        lay.addWidget(self.tree, 1)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape and self.search.text():
            self.search.clear()
            return
        super().keyPressEvent(ev)
