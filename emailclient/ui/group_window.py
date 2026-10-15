"""Standalone window mirroring the main interface, scoped to one group."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QComboBox, QLabel, QLineEdit, QMainWindow,
                               QPushButton, QSizePolicy, QSplitter, QToolBar,
                               QTreeWidget, QTreeWidgetItem, QWidget)

from .. import outbox_preview
from ..db import Database
from .compose import ComposeDialog
from .date_filter import show_date_filter
from . import editor_icons
from .mail_actions import bulk_discard
from .message_list import MessageList
from .reader import ReaderPane
from . import theme
from .sidebar import (FOLDER_ICONS, ROLE_COUNT_TEXT, ROLE_ICON_COLOR,
                      ROLE_ICON_NAME, SidebarDelegate, add_folder_children)

ROLE_ACCOUNT_ID = Qt.ItemDataRole.UserRole
ROLE_FOLDER = Qt.ItemDataRole.UserRole + 1

_GROUP_FOLDERS = (("Sent", "SENT"), ("Trash", "TRASH"), ("Spam", "SPAM"))


class GroupSidebar(QTreeWidget):
    """Group-scoped folder/account tree, same visual conventions as Sidebar."""

    def __init__(self, db: Database, group_id: int):
        super().__init__()
        self.db = db
        self.group_id = group_id
        self.setHeaderHidden(True)
        self.setIndentation(18)
        # Accounts are top-level here and now carry folder children: keep the
        # root decorated so their expand arrows are visible.
        self.setRootIsDecorated(True)
        # No keyboard focus needed — also kills the native blue focus ring
        # painted around the branch/arrow region on click.
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setItemDelegate(SidebarDelegate(self))
        # Delegate paints its own hover fill — needs hover events delivered.
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.refresh()

    def refresh(self):
        # Preserve which account rows the user expanded across rebuilds.
        expanded = {self.topLevelItem(i).data(0, ROLE_ACCOUNT_ID)
                    for i in range(self.topLevelItemCount())
                    if self.topLevelItem(i).isExpanded()}
        self.clear()
        member_ids = set(self.db.accounts_in_group(self.group_id))
        members = [a for a in self.db.list_accounts() if a.id in member_ids]
        counts = self.db.counts_by_account()  # one query for all accounts
        folder_counts = self.db.counts_by_account_folder()
        unread = {a.id: counts.get(a.id, (0, 0))[0] for a in members}
        totals = {a.id: counts.get(a.id, (0, 0))[1] for a in members}
        total_unread = sum(unread.values())
        total_all = sum(totals.values())

        all_item = QTreeWidgetItem(["All Inboxes"])
        all_item.setData(0, ROLE_ACCOUNT_ID, None)  # None = whole group
        all_item.setData(0, ROLE_FOLDER, "INBOX")
        all_item.setData(0, ROLE_ICON_NAME, "inbox")
        all_item.setData(0, ROLE_ICON_COLOR, theme.TEXT_DIM)
        all_item.setData(0, ROLE_COUNT_TEXT,
                         f"{total_unread}/{total_all}" if total_all else "")
        f = QFont()
        f.setWeight(QFont.Weight.DemiBold)
        all_item.setFont(0, f)
        self.addTopLevelItem(all_item)

        for text, folder in _GROUP_FOLDERS:
            it = QTreeWidgetItem([text])
            it.setData(0, ROLE_ACCOUNT_ID, None)
            it.setData(0, ROLE_FOLDER, folder)
            it.setData(0, ROLE_ICON_NAME, FOLDER_ICONS[folder])
            it.setData(0, ROLE_ICON_COLOR, theme.TEXT_DIM)
            self.addTopLevelItem(it)

        for acc in members:
            n = unread[acc.id]
            t = totals[acc.id]
            item = QTreeWidgetItem([acc.name])
            item.setData(0, ROLE_ACCOUNT_ID, acc.id)
            item.setData(0, ROLE_FOLDER, "INBOX")
            item.setData(0, ROLE_ICON_NAME, "dot")
            item.setData(0, ROLE_ICON_COLOR, theme.TEXT_FAINT)
            item.setData(0, ROLE_COUNT_TEXT, f"{n}/{t}" if t else "")
            item.setForeground(0, QColor(theme.TEXT))
            if n:
                bf = QFont()
                bf.setWeight(QFont.Weight.DemiBold)
                item.setFont(0, bf)
            add_folder_children(item, acc.id, folder_counts,
                                self.db.list_account_folders(acc.id))
            self.addTopLevelItem(item)
            if acc.id in expanded:
                item.setExpanded(True)


class GroupWindow(QMainWindow):
    """Top-level window with its own sidebar/list/reader/compose for one group."""

    def __init__(self, db: Database, engine, group_id: int, parent=None):
        super().__init__(parent)
        self.db = db
        self.engine = engine
        self.group_id = group_id
        # last sidebar selection: (account_id | ("group", gid), folder)
        self._current_view = (("group", group_id), "INBOX")

        name = ""
        icon = "📁"
        for grp in db.list_groups():
            if grp["id"] == group_id:
                name = grp["name"]
                icon = grp["icon"]
                break
        self.setWindowTitle(f"{icon} {name} — AMail")
        self.resize(1180, 680)

        # -- toolbar --
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        title = QLabel(f"{icon} {name}")
        title.setObjectName("appTitle")
        tb.addWidget(title)

        compose_btn = QPushButton("✉ Compose")
        compose_btn.setObjectName("primary")
        compose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        compose_btn.clicked.connect(self.compose_new)
        tb.addWidget(compose_btn)

        self.sort_box = QComboBox()
        self.sort_box.setObjectName("sortBox")
        for label, key in (("Newest first", "newest"), ("Oldest first", "oldest"),
                           ("Unread first", "unread"), ("Sender A–Z", "sender")):
            self.sort_box.addItem(label, key)
        tb.addWidget(self.sort_box)

        unread_btn = QPushButton("● Unread")
        unread_btn.setCheckable(True)
        unread_btn.setToolTip("Show only unread")
        unread_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tb.addWidget(unread_btn)

        self.date_btn = QPushButton()
        self.date_btn.setObjectName("flatTool")
        self.date_btn.setIcon(editor_icons.icon("calendar"))
        self.date_btn.setIconSize(QSize(17, 17))
        self.date_btn.setCheckable(True)
        self.date_btn.setToolTip("Filter by date")
        self.date_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        tb.addWidget(self.date_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("🔍 Search group emails…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(220)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)
        self.search_box.textChanged.connect(lambda _: self._search_timer.start())
        tb.addWidget(self.search_box)

        # -- central: [group sidebar] | [msg_list | reader] --
        self.sidebar = GroupSidebar(db, group_id)
        self.msg_list = MessageList(db)
        self.reader = ReaderPane()
        inner = QSplitter(Qt.Orientation.Horizontal)
        inner.addWidget(self.msg_list)
        inner.addWidget(self.reader)
        inner.setSizes([380, 600])
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.sidebar)
        split.addWidget(inner)
        split.setSizes([200, 980])
        self.setCentralWidget(split)

        # -- wiring --
        self.sort_box.currentIndexChanged.connect(
            lambda i: self.msg_list.set_order(self.sort_box.itemData(i)))
        unread_btn.toggled.connect(self.msg_list.set_unread_only)
        self.date_btn.clicked.connect(
            lambda: show_date_filter(self, self.date_btn, self.msg_list))
        self.sidebar.itemClicked.connect(self._on_sidebar_clicked)
        self.msg_list.message_selected.connect(self.open_message)
        self.msg_list.outbox_selected.connect(self.preview_outbox)
        self.msg_list.delete_requested.connect(
            lambda ids: self._bulk_discard(ids, "delete"))
        self.msg_list.spam_requested.connect(
            lambda ids: self._bulk_discard(ids, "spam"))
        self.msg_list.mark_read_requested.connect(self._on_mark_read)
        self.reader.reply_requested.connect(lambda m: self._compose_from(m, "reply"))
        self.reader.reply_all_requested.connect(lambda m: self._compose_from(m, "reply_all"))
        self.reader.forward_requested.connect(lambda m: self._compose_from(m, "forward"))
        self.reader.delete_requested.connect(lambda m: self._bulk_discard([m.id], "delete"))
        self.reader.spam_requested.connect(lambda m: self._bulk_discard([m.id], "spam"))
        self.engine.new_messages.connect(self._on_new_messages)

        # Coalesce refresh bursts (many workers delivering at once) into one
        # repaint instead of clearing/rebuilding the list per batch.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self._do_refresh)

        self.msg_list.show_view(("group", group_id), "INBOX")

    def _do_refresh(self):
        self.msg_list.refresh()
        self.sidebar.refresh()

    # -- sidebar / search --
    def _on_sidebar_clicked(self, item, _col):
        acc_id = item.data(0, ROLE_ACCOUNT_ID)
        folder = item.data(0, ROLE_FOLDER) or "INBOX"
        view = acc_id if acc_id is not None else ("group", self.group_id)
        self._current_view = (view, folder)
        self.search_box.clear()  # changing view exits any active search
        self.reader.clear_view()
        self.msg_list.show_view(view, folder)

    def _run_search(self):
        text = self.search_box.text().strip()
        if text:
            self.msg_list.show_search(
                text, account_ids=self.db.accounts_in_group(self.group_id))
        else:
            view, folder = self._current_view
            self.msg_list.show_view(view, folder)

    # -- actions --
    def compose_new(self):
        member_ids = self.db.accounts_in_group(self.group_id)
        preset = member_ids[0] if member_ids else None
        dlg = ComposeDialog(self.db, preset_account_id=preset, parent=self)
        self._show_composer_nonmodal(dlg)

    def _compose_from(self, m, mode: str):
        # Non-modal so the reply's "View original" QWebEngineView doesn't get
        # created inside a modal exec() loop (that deadlocks QtWebEngine on macOS).
        dlg = ComposeDialog(self.db, original=m, mode=mode, parent=self)
        self._show_composer_nonmodal(dlg)

    def _show_composer_nonmodal(self, dlg):
        """Show a composer as its own window — non-modal so it can be minimised
        and the group window stays usable while writing."""
        composers = getattr(self, "_open_composers", None)
        if composers is None:
            composers = self._open_composers = []
        composers.append(dlg)

        def _finished(result):
            if result:
                self.statusBar().showMessage(
                    "📤 Email queued — sending from Outbox", 5000)
            try:
                composers.remove(dlg)
            except ValueError:
                pass

        dlg.finished.connect(_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()



    def _accent_for(self, account_id) -> str:
        """Avatar colour for a message: the same one its row shows in the list."""
        acc = next((a for a in self.db.list_accounts() if a.id == account_id), None)
        return self.msg_list.row_accent(acc)

    def preview_outbox(self, outbox_id: int):
        """Render a queued/failed/draft outbox row in the reader.

        These rows never reach the `messages` table, so they are built into a
        read-only EmailMessage here instead."""
        row = self.db.get_outbox(outbox_id)
        if row is None:
            self.reader.clear_view()
            return
        account = next((a for a in self.db.list_accounts()
                        if a.id == row["account_id"]), None)
        m = outbox_preview.message_from_row(row, account.email if account else "")
        self.reader.show_message(m, self._accent_for(row["account_id"]))

    def open_message(self, msg_id: int):
        m = self.db.get_message(msg_id)
        if m:
            self.db.mark_read(msg_id)
            self.reader.show_message(m, self._accent_for(m.account_id))  # show the email instantly
            self._refresh_timer.start()  # bold-clear / counts can lag 300ms

    def open_message_external(self, m):
        """Notification clicked for an email of this group: jump to it here."""
        # Un-minimize first — raise_/activateWindow alone won't restore.
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                            | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        self._current_view = (("group", self.group_id), m.folder)
        self.msg_list.show_view(("group", self.group_id), m.folder)
        self.open_message(m.id)

    def _bulk_discard(self, ids: list[int], op: str):
        n = bulk_discard(self.db, ids, op)
        if not n:
            return
        self.reader.clear_view()
        self.msg_list.refresh()
        self.sidebar.refresh()
        note = (f"🗑 {n} email{'s' if n > 1 else ''} → Trash" if op == "delete"
                else f"⚠ {n} email{'s' if n > 1 else ''} → Spam")
        self.statusBar().showMessage(note, 5000)

    def _on_mark_read(self, ids: list[int], read: bool):
        for msg_id in ids:
            (self.db.mark_read if read else self.db.mark_unread)(msg_id)
        self.msg_list.refresh()
        self.sidebar.refresh()

    def _on_new_messages(self, _account_id: int, _messages: list, _initial: bool = False):
        self._refresh_timer.start()

    def closeEvent(self, event):
        # Sync workers belong to the main app; nothing to stop here.
        event.accept()
