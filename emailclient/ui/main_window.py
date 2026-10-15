from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtCore import (QFileSystemWatcher, QSettings, QSize, Qt, QTimer,
                            Signal)
from PySide6.QtWidgets import (QApplication, QButtonGroup, QComboBox, QFileDialog,
                               QFrame, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
                               QMessageBox, QPushButton, QSizePolicy, QSplitter,
                               QTabBar, QToolBar, QVBoxLayout, QWidget)

from .. import (approval, backup, credentials, fleet, google_oauth, notify,
                oauth, outbox_preview, send_errors)
from ..license import LicenseManager
from ..settings import (BackupSettings, NotifSettings, SendSettings,
                        play_sound)
from . import editor_icons, theme
from ..db import Database
from ..models import Account
from ..sync.engine import SyncEngine
from .account_settings import AccountSettingsDialog
from .add_account_dialog import AddAccountDialog
from .approval_dialog import ApprovalQueueDialog
from .bulk_add_dialog import BulkAddDialog
from .bulk_edit_dialog import BulkEditAccountsDialog
from .compose import ComposeDialog
from .date_filter import show_date_filter
from .group_window import GroupWindow
from .license_dialog import LicenseDialog
from .mail_actions import bulk_discard, bulk_restore
from . import message_list
from .message_list import MessageList
from .notif_center import NotifPopup
from .reader import MessageWindow, ReaderPane
from .settings_dialog import SettingsDialog
from .send_watch import SendWatcher
from .sidebar import SidebarPanel
from .toast import NotificationStack

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    # Emitted from the background approval poll → updates UI on the main thread.
    # (is_admin, pending_count, list_changed, new_pending_items)
    _approval_polled = Signal(bool, int, bool, list, list)
    # Emitted from the background fleet heartbeat → applied on the main thread.
    _fleet_applied = Signal(dict)
    # Sentinel toast msg_id meaning "open the approvals queue" (not an email).
    _APPROVAL_TOAST_ID = -100000

    def __init__(self, db: Database, engine: SyncEngine, outbox_worker=None):
        super().__init__()
        self.db = db
        self.engine = engine
        self._outbox = outbox_worker
        self.setWindowTitle("AMail")
        # Start maximized, like Outlook — a mail client is a full-workspace app,
        # and the three panes (sidebar / list / reader) need the room. The
        # restore size below is what the window returns to when un-maximized.
        from PySide6.QtGui import QGuiApplication
        avail = QGuiApplication.primaryScreen().availableGeometry()
        w = min(1680, int(avail.width() * 0.9))
        h = min(1000, int(avail.height() * 0.9))
        self.resize(w, h)
        self.setMinimumWidth(1100)
        self.move(avail.center().x() - w // 2, avail.center().y() - h // 2)
        # Flag only — the caller decides WHEN to show (splash first); calling
        # showMaximized() here would pop the window up behind the splash.
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

        self.sidebar_panel = SidebarPanel(db)
        self.sidebar = self.sidebar_panel.tree
        self.msg_list = MessageList(db)
        self.reader = ReaderPane()
        self._current_account_id = None
        self._current_folder = "INBOX"
        self._notif_popup = None
        self._msg_windows = []   # standalone email windows (double-click to open)
        self._group_windows: list = []
        self._settings = QSettings("AMail", "ui")
        self._license = LicenseManager()
        self._shutdown_done = False

        # Layout: [sidebar] | [tabbar over (msg_list | reader)]
        self.tabbar = QTabBar()
        self.tabbar.setDocumentMode(True)
        self.tabbar.setDrawBase(False)
        self.tabbar.hide()
        self.tabbar.currentChanged.connect(self._on_tab_changed)

        # Sub-bar under the tabbar: account "chips" when a group tab is active
        self.subbar = QWidget()
        self.subbar.setObjectName("groupBar")
        self.subbar.setFixedHeight(42)  # slim bar — never share vertical space
        self.subbar.hide()
        self._subbar_layout = QHBoxLayout(self.subbar)
        self._subbar_layout.setContentsMargins(12, 6, 12, 6)
        self._subbar_layout.setSpacing(6)
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)

        inner = QSplitter(Qt.Orientation.Horizontal)
        inner.addWidget(self.msg_list)
        inner.addWidget(self.reader)
        inner.setSizes([380, 600])
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.tabbar)
        right_layout.addWidget(self.subbar)
        right_layout.addWidget(inner, 1)  # splitter takes ALL extra height

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.sidebar_panel)
        split.addWidget(right)
        split.setSizes([220, 980])
        self.setCentralWidget(split)

        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        title = QLabel("✦ AMail")
        title.setObjectName("appTitle")
        tb.addWidget(title)
        compose_btn = QPushButton(" Compose")
        compose_btn.setObjectName("primary")
        compose_btn.setIcon(editor_icons.icon("compose", "#FFFFFF"))
        compose_btn.setIconSize(QSize(16, 16))
        compose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        compose_btn.clicked.connect(self.compose_new)
        tb.addWidget(compose_btn)

        self.sort_box = QComboBox()
        self.sort_box.setObjectName("sortBox")
        for label, key in (("Newest first", "newest"), ("Oldest first", "oldest"),
                           ("Unread first", "unread"), ("Sender A–Z", "sender")):
            self.sort_box.addItem(label, key)
        self.sort_box.currentIndexChanged.connect(
            lambda i: self.msg_list.set_order(self.sort_box.itemData(i)))
        tb.addWidget(self.sort_box)

        unread_btn = QPushButton("● Unread")
        unread_btn.setCheckable(True)
        unread_btn.setToolTip("Show only unread")
        unread_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        unread_btn.toggled.connect(self.msg_list.set_unread_only)
        tb.addWidget(unread_btn)

        flag_btn = QPushButton("⚑ Flagged")
        flag_btn.setCheckable(True)
        flag_btn.setToolTip("Show only flagged")
        flag_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        flag_btn.toggled.connect(self.msg_list.set_flagged_only)
        tb.addWidget(flag_btn)

        self.date_btn = QPushButton()
        self.date_btn.setObjectName("flatTool")
        self.date_btn.setIcon(editor_icons.icon("calendar"))
        self.date_btn.setIconSize(QSize(17, 17))
        self.date_btn.setCheckable(True)
        self.date_btn.setToolTip("Filter by date")
        self.date_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.date_btn.clicked.connect(
            lambda: show_date_filter(self, self.date_btn, self.msg_list))
        tb.addWidget(self.date_btn)

        # Manual "fetch new mail now" — accounts idle-sync on their own, but a
        # big mailbox can be busy pulling old history, so give the user a way to
        # ask for the latest immediately.
        self.sync_btn = QPushButton("⟳ Sync")
        self.sync_btn.setToolTip(
            "Fetch new mail now (syncing of older history keeps running)")
        self.sync_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sync_btn.clicked.connect(lambda: self._sync_now([]))
        tb.addWidget(self.sync_btn)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("searchBox")
        self.search_box.setPlaceholderText("Search all emails…")
        self.search_box.addAction(editor_icons.icon("search", theme.TEXT_FAINT),
                                  QLineEdit.ActionPosition.LeadingPosition)
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setFixedWidth(260)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._run_search)
        self.search_box.textChanged.connect(lambda _: self._search_timer.start())
        tb.addWidget(self.search_box)

        self.search_scope = QComboBox()
        self.search_scope.setToolTip("Where to search")
        self._refresh_search_scope()
        self.search_scope.currentIndexChanged.connect(self._on_scope_changed)
        tb.addWidget(self.search_scope)

        self.layout_btn = QPushButton()
        self.layout_btn.setObjectName("flatTool")
        self.layout_btn.setIcon(editor_icons.icon("grid"))
        self.layout_btn.setIconSize(QSize(17, 17))
        self.layout_btn.setCheckable(True)
        self.layout_btn.setToolTip("Switch between Sidebar / Tabs view")
        self.layout_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.layout_btn.toggled.connect(self._on_layout_toggled)
        tb.addWidget(self.layout_btn)

        notif_btn = QPushButton()
        notif_btn.setObjectName("flatTool")
        notif_btn.setIcon(editor_icons.icon("bell"))
        notif_btn.setIconSize(QSize(17, 17))
        notif_btn.setToolTip("Notifications")
        notif_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        notif_btn.clicked.connect(self._show_notifications)
        tb.addWidget(notif_btn)

        self.approvals_btn = QPushButton(" Approvals")
        self.approvals_btn.setIcon(editor_icons.icon("hourglass"))
        self.approvals_btn.setIconSize(QSize(16, 16))
        self.approvals_btn.setToolTip("Emails waiting for your approval")
        self.approvals_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.approvals_btn.clicked.connect(self._show_approvals)
        self.approvals_btn.setVisible(approval.is_admin())
        tb.addWidget(self.approvals_btn)

        settings_btn = QPushButton()
        settings_btn.setObjectName("flatTool")
        settings_btn.setIcon(editor_icons.icon("gear"))
        settings_btn.setIconSize(QSize(17, 17))
        settings_btn.setToolTip("Settings")
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.clicked.connect(self._show_settings)
        tb.addWidget(settings_btn)

        # Secondary actions live in a "»" overflow panel that stays open until
        # the toggle is clicked again (a QToolBar's native extension is a QMenu
        # that closes as soon as focus leaves it — not what we want here).
        add_btn = QPushButton(" Add Account")
        add_btn.setObjectName("primary")
        add_btn.setIcon(editor_icons.icon("plus", "#FFFFFF"))
        add_btn.setIconSize(QSize(16, 16))
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self.add_account)

        bulk_btn = QPushButton(" Bulk")
        bulk_btn.setIcon(editor_icons.icon("bulk"))
        bulk_btn.setIconSize(QSize(16, 16))
        bulk_btn.setToolTip("Add many accounts at once")
        bulk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        bulk_btn.clicked.connect(self.bulk_add_accounts)

        exit_btn = QPushButton(" Exit")
        exit_btn.setIcon(editor_icons.icon("power", "#C0392B"))
        exit_btn.setIconSize(QSize(17, 17))
        exit_btn.setToolTip("Exit — quit AMail completely "
                            "(stops background sync)")
        exit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        exit_btn.clicked.connect(self._exit_app)

        self.more_btn = QPushButton("»")
        self.more_btn.setObjectName("flatTool")
        self.more_btn.setCheckable(True)
        self.more_btn.setFixedSize(34, 30)
        self.more_btn.setToolTip("More actions")
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.toggled.connect(self._on_more_toggled)
        tb.addWidget(self.more_btn)

        self.more_panel = QFrame(self)
        self.more_panel.setObjectName("morePanel")
        self.more_panel.setFrameShape(QFrame.Shape.StyledPanel)
        _mp = QVBoxLayout(self.more_panel)
        _mp.setContentsMargins(8, 8, 8, 8)
        _mp.setSpacing(6)
        for _b in (add_btn, bulk_btn, exit_btn):
            # Close the panel after an action fires, but let the action run.
            _b.clicked.connect(lambda *_: self.more_btn.setChecked(False))
            _mp.addWidget(_b)
        self.more_panel.hide()

        self.sidebar.selection_changed.connect(self.msg_list.show_view)
        self.sidebar.selection_changed.connect(self._on_account_selected)
        # A sidebar pick is authoritative — snap the view dropdown back to
        # "Current view" so the two controls never fight over the list.
        self.sidebar.selection_changed.connect(self._reset_scope_to_view)
        self.msg_list.message_selected.connect(self.open_message)
        self.msg_list.outbox_selected.connect(self.preview_outbox)
        self.msg_list.message_open_external.connect(self._open_message_window)
        self.msg_list.delete_requested.connect(
            lambda ids: self._bulk_discard(ids, "delete"))
        self.msg_list.spam_requested.connect(
            lambda ids: self._bulk_discard(ids, "spam"))
        self.msg_list.restore_requested.connect(self._bulk_restore)
        self.msg_list.mark_read_requested.connect(self._on_mark_read)
        self.msg_list.outbox_retry.connect(self._on_outbox_retry)
        self.msg_list.outbox_delete.connect(self._on_outbox_delete)
        self.msg_list.outbox_edit.connect(self._on_outbox_edit)
        # Date-filter count: a permanent status-bar tally that survives the
        # transient showMessage() notices used elsewhere.
        self.date_count_label = QLabel("")
        self.date_count_label.setObjectName("dateCount")
        self.statusBar().addPermanentWidget(self.date_count_label)
        self._build_composer_dock()
        self.msg_list.counts_changed.connect(self._on_view_counts)
        self.msg_list.copy_requested.connect(self._copy_messages)
        self.reader.reply_requested.connect(lambda m: self._compose_from(m, "reply"))
        self.reader.reply_all_requested.connect(lambda m: self._compose_from(m, "reply_all"))
        self.reader.forward_requested.connect(lambda m: self._compose_from(m, "forward"))
        self.reader.delete_requested.connect(lambda m: self._discard(m, "delete"))
        self.reader.spam_requested.connect(lambda m: self._discard(m, "spam"))
        self.reader.attachment_clicked.connect(self._open_attachment)
        self.sidebar.settings_requested.connect(self._on_settings_requested)
        self.sidebar.remove_requested.connect(self._on_remove_requested)
        self.sidebar.remove_multi_requested.connect(self._on_remove_multi_requested)
        self.sidebar.bulk_edit_requested.connect(self._on_bulk_edit_requested)
        self.sidebar.pause_toggled.connect(self._on_pause_toggled)
        self.sidebar.folder_created.connect(self._on_folder_created)
        self.sidebar.marked_all_read.connect(self._reload_current_view)
        self.sidebar.sync_requested.connect(self._sync_now)
        self.sidebar.groups_changed.connect(self._rebuild_tabs)
        self.sidebar.groups_changed.connect(self._refresh_search_scope)
        self.sidebar.open_group_window.connect(self._open_group_window)
        self.engine.new_messages.connect(self.on_new_messages)

        # Native-notification clicks: terminal-notifier writes the message id
        # to CLICK_FILE; watching it lets us jump straight to that email.
        notify.CLICK_FILE.touch(exist_ok=True)
        self._click_watcher = QFileSystemWatcher([str(notify.CLICK_FILE)])
        self._click_watcher.fileChanged.connect(self._on_notify_click)

        # Persistent notification window: cards stay until the email is opened.
        self.toast = NotificationStack()
        self.toast.message_clicked.connect(self._open_from_notification)
        # ✕ on a card schedules an unread reminder a minute later.
        self._reminder_counts: dict[int, int] = {}
        self.toast.card_dismissed.connect(self._schedule_unread_reminder)
        self.toast.mark_all_read.connect(self._on_notif_mark_all_read)
        from PySide6.QtGui import QGuiApplication
        QGuiApplication.instance().applicationStateChanged.connect(
            lambda _state: self.toast.reshow_if_pending())
        self._account_names = {}
        # Auth-class sync errors get one toast card per account per session;
        # cleared when the account reconnects so a later failure re-alerts.
        self._error_notified: set[int] = set()
        self.engine.status_changed.connect(self._on_status)
        self.engine.status_changed.connect(self.sidebar.set_status)

        # Watchdog: every minute, restart any dead (non-auth-failed) worker.
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(60_000)
        self._watchdog_timer.timeout.connect(self.engine.watchdog)
        self._install_shortcuts()
        self._watchdog_timer.start()

        # Coalesce refresh bursts: with 50+ accounts, dozens of new-message
        # batches can land within milliseconds — one repaint 300ms later
        # instead of a full list/sidebar rebuild per batch.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(300)
        self._refresh_timer.timeout.connect(self._do_refresh)

        # Lighter than _do_refresh: opening an email only needs the sidebar
        # unread counts updated, not a full message-list rebuild.
        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.setSingleShot(True)
        self._sidebar_timer.setInterval(400)
        self._sidebar_timer.timeout.connect(self.sidebar.refresh)

        # Periodic backups: hourly tick checks the chosen frequency and the
        # time since the last run (persisted in QSettings, survives restarts).
        self._backup_timer = QTimer(self)
        self._backup_timer.setInterval(60 * 60_000)
        self._backup_timer.timeout.connect(self._backup_tick)
        self._backup_timer.start()

        # Fleet heartbeat: report this device's accounts + statuses, pull the
        # license-wide blocklist and any admin pause/resume commands. All network
        # work is fire-and-forget; results re-enter the GUI via _fleet_applied.
        self._fleet_applied.connect(self._apply_fleet)
        self._fleet_timer = QTimer(self)
        self._fleet_timer.setInterval(120_000)
        self._fleet_timer.timeout.connect(self._fleet_tick)

        # Every queued email gets a deadline: if it has not left by then the
        # user hears about it, instead of "Email queued" being the last word.
        self.send_watch = SendWatcher(self.db, self)
        self.send_watch.slow.connect(self._on_send_slow)

        if self._outbox is not None:
            self._outbox.sent.connect(self._on_outbox_sent)
            self._outbox.failed.connect(self._on_outbox_failed)
            self._outbox.held.connect(self._on_outbox_held)

        # Approval queue housekeeping: refresh the approval-accounts cache, the
        # admin pending badge, and reconcile our submitted emails' status.
        self._seen_pending_ids: set = set()
        self._approval_first_poll = True   # don't alert for pre-existing items
        self._approval_polled.connect(self._on_approval_polled)
        self._approval_timer = QTimer(self)
        self._approval_timer.setInterval(45_000)
        self._approval_timer.timeout.connect(self._poll_approval)
        self._approval_timer.start()
        QTimer.singleShot(2000, self._poll_approval)  # initial, after startup

        if self._settings.value("layout", "sidebar") == "tabs":
            self.layout_btn.setChecked(True)  # triggers _on_layout_toggled

        self.msg_list.refresh()

    def _do_refresh(self):
        self.msg_list.refresh()
        self.sidebar.refresh()

    def _on_account_selected(self, account_id, folder="INBOX"):
        self._current_account_id = account_id
        self._current_folder = folder
        self.reader.clear_view()

    # -- backups --
    def _backup_tick(self):
        bs = BackupSettings()
        freq = bs.frequency
        if freq == "startup":
            return  # startup-only: app.py already ran one at launch
        elapsed = time.time() - bs.last_run_epoch
        need = 6 * 3600 if freq == "6h" else 24 * 3600
        if elapsed < need:
            return

        def _do():
            # run_backup opens its own connections — safe off the GUI thread
            # alongside the app's WAL connection.
            if backup.run_backup() is not None:
                BackupSettings().last_run_epoch = time.time()

        threading.Thread(target=_do, daemon=True).start()

    # -- fleet (cross-device admin) --
    def _account_status(self, acc) -> str:
        """A snapshot of one account's live state for the fleet heartbeat."""
        if getattr(acc, "paused", False):
            return "paused"
        if acc.id in self.engine.auth_failed:
            return "error"
        w = self.engine.workers.get(acc.id)
        return "running" if (w and w.isRunning()) else "error"

    def _fleet_tick(self):
        """Fire-and-forget heartbeat (mirrors _poll_approval threading)."""
        groups = {g["id"]: g["name"] for g in self.db.list_groups()}
        snapshot = [{"email": a.email, "name": a.name,
                     "status": self._account_status(a),
                     "group": groups.get(a.group_id, "")}
                    for a in self.db.list_accounts()]
        # Report the license expiry date so the web panel can show days-left
        # (the activation clock lives per-install; the server has no other source).
        exp = self._license.status().get("expires_at")
        expires = exp.date().isoformat() if exp else ""

        def work():
            resp = fleet.heartbeat(snapshot, expires=expires)
            if resp is not None and not self._shutdown_done:
                try:
                    self._fleet_applied.emit(resp)
                except RuntimeError:
                    pass   # window torn down between the check and the emit

        threading.Thread(target=work, daemon=True).start()

    def _apply_fleet(self, resp: dict):
        """Apply a heartbeat response on the GUI thread: enforce the blocklist
        (self-remove blocklisted accounts) and run pause/resume commands."""
        fleet.set_blocklist(resp.get("blocklist", []))
        fleet.set_can_export(resp.get("can_export", False))
        blocked = fleet.blocked_emails()
        to_remove = [a.id for a in self.db.list_accounts()
                     if a.email.lower() in blocked]
        if to_remove:
            self._remove_accounts(to_remove)   # stops workers, cascades, refresh
            self.statusBar().showMessage(
                f"{len(to_remove)} account(s) removed by the administrator", 6000)
        # Apply (and ack) pending pause/resume commands.
        accts = {a.email.lower(): a for a in self.db.list_accounts()}
        applied = []
        for cmd in resp.get("commands", []):
            cid = cmd.get("id")
            if cid is None:
                continue
            acc = accts.get(str(cmd.get("email", "")).lower())
            action = cmd.get("action")
            if acc is None:
                applied.append(cid)            # account gone → ack so it clears
            elif action == "pause" and not acc.paused:
                try:
                    self._on_pause_toggled(acc.id, True)
                    applied.append(cid)        # ack only once it actually applied
                except Exception:
                    log.exception("fleet: failed to pause account %s", acc.id)
            elif action == "resume" and acc.paused:
                try:
                    self._on_pause_toggled(acc.id, False)
                    applied.append(cid)
                except Exception:
                    log.exception("fleet: failed to resume account %s", acc.id)
            else:
                applied.append(cid)            # already in desired state → ack
        if applied:
            threading.Thread(target=lambda: fleet.ack(applied),
                             daemon=True).start()

    # -- search --
    def _refresh_search_scope(self):
        """(Re)build the search-scope dropdown: Current view (default — so being
        inside a group searches only that group), All accounts, then one entry
        per group to search a specific group directly. Preserves the current
        choice across a rebuild when possible."""
        prev = self.search_scope.currentData() if self.search_scope.count() else "view"
        self.search_scope.blockSignals(True)
        self.search_scope.clear()
        self.search_scope.addItem("Current view", "view")
        self.search_scope.addItem("All accounts", "all")
        for grp in self.db.list_groups():
            icon = grp["icon"] or "📁"
            self.search_scope.addItem(f"{icon} {grp['name']}", ("group", grp["id"]))
        idx = self.search_scope.findData(prev)
        self.search_scope.setCurrentIndex(idx if idx >= 0 else 0)
        self.search_scope.blockSignals(False)

    def _search_scope_ids(self) -> list[int] | None:
        """Account ids for scope 'Current view'; None = all accounts."""
        cur = self._current_account_id
        if isinstance(cur, tuple) and cur[0] == "group":
            return self.db.accounts_in_group(cur[1])
        if isinstance(cur, int):
            return [cur]
        return None  # unified ("All") view → search everything

    # -- keyboard shortcuts ---------------------------------------------
    # Outlook-compatible where it matters (Ctrl+P print, Ctrl+R reply,
    # Ctrl+N new). Everything here already exists as a button; these are the
    # keyboard route to the same slots.
    SHORTCUTS = (
        ("Ctrl+P", "_shortcut_print"),
        ("Ctrl+N", "compose_new"),
        ("Ctrl+R", "_shortcut_reply"),
        ("Ctrl+Shift+R", "_shortcut_reply_all"),
        ("Ctrl+Shift+F", "_shortcut_forward"),
        ("Ctrl+E", "_shortcut_focus_search"),
        ("Ctrl+F", "_shortcut_focus_search"),
        ("F5", "_shortcut_sync"),
    )

    def _install_shortcuts(self) -> None:
        for keys, slot in self.SHORTCUTS:
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(getattr(self, slot))

    def _shortcut_print(self):
        self.reader.print_current()

    def _shortcut_reply(self):
        if self.reader.current_message() is not None:
            self._compose_from(self.reader.current_message(), "reply")

    def _shortcut_reply_all(self):
        if self.reader.current_message() is not None:
            self._compose_from(self.reader.current_message(), "reply_all")

    def _shortcut_forward(self):
        if self.reader.current_message() is not None:
            self._compose_from(self.reader.current_message(), "forward")

    def _shortcut_focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _shortcut_sync(self):
        self._sync_now([])

    def _sync_now(self, account_ids: list | None = None):
        """Ask the sync engine to fetch new mail immediately for these accounts
        (empty/None = all). Backfill of older history is not interrupted."""
        ids = list(account_ids) if account_ids else None
        nudged = self.engine.sync_now(ids)
        if nudged:
            self.statusBar().showMessage(
                f"⟳ Checking {nudged} account(s) for new mail…", 4000)
        else:
            self.statusBar().showMessage(
                "No account is connected right now — nothing to sync", 4000)

    def _reload_current_view(self):
        """Re-render the open view (e.g. after a bulk mark-all-read)."""
        if self.search_box.text().strip():
            self._run_search()
        else:
            self.msg_list.show_view(self._current_account_id, self._current_folder)

    def _on_scope_changed(self, _):
        """The view dropdown does double duty: while a search is active it
        scopes the search; with an empty search box it switches the visible
        view (All accounts / a group / the sidebar's current selection)."""
        if self.search_box.text().strip():
            self._search_timer.start()
            return
        scope = self.search_scope.currentData()
        if scope == "all":
            self._on_account_selected(None, self._current_folder)
        elif isinstance(scope, tuple) and scope[0] == "group":
            self._on_account_selected(scope, self._current_folder)
        else:  # "view" — nothing to jump to; keep current selection
            return
        self.msg_list.show_view(self._current_account_id, self._current_folder)

    def _reset_scope_to_view(self, *_):
        if self.search_scope.currentData() != "view":
            self.search_scope.blockSignals(True)
            self.search_scope.setCurrentIndex(0)  # "Current view"
            self.search_scope.blockSignals(False)

    def _run_search(self):
        text = self.search_box.text().strip()
        if text:
            account_ids = None
            scope = self.search_scope.currentData()
            if scope == "view":
                account_ids = self._search_scope_ids()
            elif isinstance(scope, tuple) and scope[0] == "group":
                account_ids = self.db.accounts_in_group(scope[1])
            self.msg_list.show_search(text, account_ids=account_ids)
        else:
            self.msg_list.show_view(self._current_account_id, self._current_folder)

    # -- notifications --
    def _on_more_toggled(self, on: bool):
        if on:
            self._position_more_panel()
            self.more_panel.show()
            self.more_panel.raise_()
        else:
            self.more_panel.hide()

    def _position_more_panel(self):
        """Anchor the overflow panel just below the » toggle, right-aligned,
        kept inside the window bounds."""
        self.more_panel.adjustSize()
        pw = self.more_panel.width()
        # » button's bottom-right, in main-window coordinates.
        br = self.more_btn.mapTo(self, self.more_btn.rect().bottomRight())
        x = min(br.x() - pw, self.width() - pw - 6)
        x = max(6, x)
        y = br.y() + 4
        self.more_panel.move(x, y)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.more_panel.isVisible():
            self._position_more_panel()

    def _show_notifications(self):
        if self._notif_popup is None:
            self._notif_popup = NotifPopup(self.db, self)
            self._notif_popup.open_message.connect(self.open_message)
        self._notif_popup.show()
        self._notif_popup.raise_()
        self._notif_popup.activateWindow()


    def _apply_appearance(self) -> None:
        """Theme or density changed in Settings — repaint everything that draws
        itself from the theme globals instead of from the stylesheet."""
        message_list.apply_density()
        for lst in [self.msg_list] + [w.msg_list for w in self._group_windows]:
            lst.setUniformItemSizes(False)   # force fresh sizeHint() calls
            lst.setUniformItemSizes(True)
            lst.reset()
            lst.viewport().update()
        self.sidebar.refresh()
        self.reader.repaint_theme()
        self._refresh_composer_dock()

    def _show_settings(self):
        dlg = SettingsDialog(self, license_manager=self._license)
        dlg.test_requested.connect(lambda: QTimer.singleShot(
            3000, lambda: self.toast.push(
                0, "AMail Test 🔔",
                "If this appeared over Chrome/another app, everything works ✅",
                theme.GOLD)))
        dlg.appearance_changed.connect(self._apply_appearance)
        dlg.exec()
        # Popup position may have changed → move any visible cards immediately.
        self.toast.apply_settings()
        # The account-list label (name vs email) may have been toggled.
        self.sidebar.refresh()
        # Admin login state may have changed inside Settings → Security.
        self.approvals_btn.setVisible(approval.is_admin())
        self._poll_approval()

    def _exit_app(self):
        """Quit the whole app (not just hide the window). Confirms first since
        closing stops background sync and notifications."""
        # Make sure the confirm can't hide behind other windows / off-screen and
        # deadlock the app (a modal box the user can't see blocks every click).
        self.show()
        self.raise_()
        self.activateWindow()
        box = QMessageBox(self)
        box.setWindowTitle("Exit AMail")
        box.setText("Quit AMail completely?\n\nBackground sync and new-email "
                    "notifications will stop until you open it again.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setWindowModality(Qt.WindowModality.ApplicationModal)
        box.raise_()
        box.activateWindow()
        ans = box.exec()
        if ans == QMessageBox.StandardButton.Yes:
            from PySide6.QtWidgets import QApplication
            self.shutdown()
            QApplication.instance().quit()

    def _show_approvals(self):
        self.toast.remove_for_message(self._APPROVAL_TOAST_ID)  # clear alerts
        ApprovalQueueDialog(self.db, self).exec()
        self._poll_approval()

    def _poll_approval(self):
        """Approval housekeeping. Button visibility is a fast local keychain
        read done instantly; all network work runs on a background thread so
        the UI never freezes (results come back via _approval_polled)."""
        self.approvals_btn.setVisible(approval.is_admin())
        threading.Thread(target=self._poll_approval_worker, daemon=True).start()

    def _poll_approval_worker(self):
        admin = approval.is_admin()
        count, changed, new_items, rejections = 0, False, [], []
        try:
            approval.refresh_accounts_cache()
            approval.refresh_settings_cache()   # keep delete-lock state current
            if admin:
                items = approval.pending()
                count = len(items)
                new_items = [it for it in items
                             if it.get("id") not in self._seen_pending_ids]
            changed, rejections = self._reconcile_awaiting()
        except Exception:  # never let the poller bubble into the UI
            pass
        self._approval_polled.emit(admin, count, changed, new_items, rejections)

    def _on_approval_polled(self, admin: bool, count: int, changed: bool,
                            new_items: list, rejections: list = ()):
        self._sync_cloud_creds()
        if rejections:
            self._notify_rejections(rejections)
        self.approvals_btn.setVisible(admin)
        if admin:
            self.approvals_btn.setText(
                f" Approvals ({count})" if count else " Approvals")
            for it in new_items:
                self._seen_pending_ids.add(it.get("id"))
            if new_items and not self._approval_first_poll:
                self._notify_approval(new_items)
            self._approval_first_poll = False
        if changed:
            self.msg_list.refresh()
            self.sidebar.refresh()

    _REJECT_TOAST_ID = -100001

    def _notify_rejections(self, rejections: list):
        """Alert the SENDER that the admin rejected their email(s). A persistent
        entry goes into the 🔔 notification center (stays until the user opens/
        clears it), plus a toast + native alert. The rejected email itself stays
        in the Outbox (as a failed row) with the reason."""
        ns = NotifSettings()
        for r in rejections:
            subject = r.get("subject") or "(no subject)"
            reason = r.get("reason") or "no reason given"
            body = f"{subject} — {reason}"
            # Persistent history (🔔): survives until the user reads/clears it.
            self.db.add_notification(None, "⛔ Email rejected by admin", body)
            self.toast.push(self._REJECT_TOAST_ID,
                            "⛔ Email rejected by admin", body, "#C0392B")
            if ns.enabled:
                notify.send("⛔ Email rejected by admin", body, sound="default")
        if ns.enabled and ns.sound_enabled:
            play_sound(ns.sound)
        # Refresh the open notification popup, if any, and the Outbox view.
        if getattr(self, "_notif_popup", None) is not None:
            try:
                self._notif_popup.refresh()
            except RuntimeError:
                pass
        self.statusBar().showMessage(
            f"⛔ {len(rejections)} email(s) rejected by the admin — see Outbox",
            8000)

    def _notify_approval(self, new_items: list):
        """Alert the admin that email(s) are waiting for approval."""
        for it in new_items:
            where = it.get("account_email", "")
            subj = it.get("subject") or "(no subject)"
            self.toast.push(self._APPROVAL_TOAST_ID,
                            f"⏳ Approval needed — {where}",
                            subj, theme.GOLD)
        n = len(new_items)
        self.statusBar().showMessage(
            f"⏳ {n} email{'s' if n != 1 else ''} waiting for your approval", 8000)
        if NotifSettings().enabled:
            notify.send("AMail",
                        f"{n} email{'s' if n != 1 else ''} need your approval",
                        sound="default")
            if NotifSettings().sound_enabled:
                play_sound(NotifSettings().sound)

    def _reconcile_awaiting(self):
        """DB-only reconciliation (safe off the main thread). Returns
        (changed, rejections) — whether any row changed, and a list of emails the
        admin just rejected (each {subject, reason, account}) so the caller can
        alert the sender. Each rejection is reported exactly once: finish_outbox
        moves the row out of awaiting_approval_outbox()."""
        rows = self.db.awaiting_approval_outbox()
        if not rows:
            return False, []
        by_id = {it["id"]: it for it in approval.status()}
        changed = False
        rejections = []
        for row in rows:
            st = by_id.get(row["approval_id"])
            if not st:
                continue
            if st["status"] == "sent":
                self.db.finish_outbox(row["id"], True)
                self._save_sent_copy(row)
                changed = True
            elif st["status"] == "released":
                # Admin approved in "send from sender" mode: send it ourselves
                # so the recipient's Received header shows our IP, not theirs.
                # The outbox worker picks it up, sends, and reports to the server.
                self.db.release_outbox_to_sender(row["id"])
                changed = True
            elif st["status"] == "rejected":
                reason = st.get("reason") or "no reason given"
                self.db.finish_outbox(row["id"], False, f"Rejected: {reason}")
                rejections.append({
                    "subject": row["subject"] or st.get("subject") or "(no subject)",
                    "reason": reason,
                })
                changed = True
        return changed, rejections

    def _save_sent_copy(self, row):
        """After the admin sends our approved email, keep a local SENT copy."""
        from datetime import datetime, timezone

        from ..models import EmailMessage
        acc = next((a for a in self.db.list_accounts()
                    if a.id == row["account_id"]), None)
        self.db.save_message(EmailMessage(
            account_id=row["account_id"], folder="SENT", uid=-row["id"],
            subject=row["subject"], sender=acc.email if acc else "",
            recipients=row["to_addrs"],
            date=datetime.now(timezone.utc).isoformat(),
            body_text=row["body"], body_html=row["body_html"], is_read=True))

    # -- layout mode (sidebar / tabs) --
    def _on_layout_toggled(self, tabs: bool):
        if tabs:
            self._rebuild_tabs()
            self.sidebar_panel.hide()
            self.tabbar.show()
            self.layout_btn.setText("☰")
        else:
            self.tabbar.hide()
            self.subbar.hide()
            self.sidebar_panel.show()
            self.layout_btn.setText("⊞")
        self._settings.setValue("layout", "tabs" if tabs else "sidebar")

    def _rebuild_tabs(self):
        self.tabbar.blockSignals(True)
        while self.tabbar.count():
            self.tabbar.removeTab(0)
        # "All" tab — shows all accounts
        self.tabbar.addTab("All")
        self.tabbar.setTabData(0, None)
        # Group tabs
        for grp in self.db.list_groups():
            i = self.tabbar.addTab(f"{grp['icon']} {grp['name']}")
            self.tabbar.setTabData(i, ("group", grp["id"]))
        # Per-account tabs — ONLY ungrouped accounts (grouped ones live in
        # their group's chip bar, not duplicated at the top level).
        for acc in self.db.list_accounts():
            if acc.group_id is None:
                i = self.tabbar.addTab(acc.name)
                self.tabbar.setTabData(i, acc.id)
        # Folder tabs
        for label, folder in (("📤 Sent", "SENT"), ("📦 Outbox", "OUTBOX"),
                               ("🗑 Trash", "TRASH"), ("⚠ Spam", "SPAM")):
            i = self.tabbar.addTab(label)
            self.tabbar.setTabData(i, ("folder", folder))
        # Keep current account's tab selected if it still exists
        for i in range(self.tabbar.count()):
            if self.tabbar.tabData(i) == self._current_account_id:
                self.tabbar.setCurrentIndex(i)
                break
        self.tabbar.blockSignals(False)
        # Tabs (and group membership) may have changed under our feet —
        # rebuild the chip bar for whatever tab is now current.
        if self.layout_btn.isChecked():
            self._update_subbar(self.tabbar.tabData(self.tabbar.currentIndex()))
        else:
            self.subbar.hide()

    def _on_tab_changed(self, index: int):
        if index < 0:
            return
        data = self.tabbar.tabData(index)
        self.search_box.clear()  # switching tab exits any active search
        if isinstance(data, tuple) and data[0] == "folder":
            folder = data[1]
            self._on_account_selected(None, folder)
            self.msg_list.show_view(None, folder)
        elif isinstance(data, tuple) and data[0] == "group":
            self._on_account_selected(data, "INBOX")
            self.msg_list.show_view(data, "INBOX")
        else:
            account_id = data  # None (All) or int
            self._on_account_selected(account_id, "INBOX")
            self.msg_list.show_view(account_id, "INBOX")
        self._update_subbar(data)

    def _update_subbar(self, data):
        """Show account chips under the tabbar when a group tab is active."""
        for btn in self._chip_group.buttons():
            self._chip_group.removeButton(btn)
        while self._subbar_layout.count():
            w = self._subbar_layout.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if not (isinstance(data, tuple) and data[0] == "group"):
            self.subbar.hide()
            return
        gid = data[1]
        member_ids = set(self.db.accounts_in_group(gid))
        members = [a for a in self.db.list_accounts() if a.id in member_ids]

        def make_chip(label: str, view, dot_color: str | None = None):
            chip = QPushButton(label)
            chip.setObjectName("chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            if dot_color:
                # Colored account dot as an icon; the label text stays the
                # neutral chip color (not tinted by the account color).
                chip.setIcon(editor_icons.dot_icon(dot_color))
            chip.clicked.connect(lambda _=False, v=view: self._on_chip_clicked(v))
            self._chip_group.addButton(chip)
            self._subbar_layout.addWidget(chip)
            return chip

        cur = self._current_account_id
        all_chip = make_chip("All", ("group", gid))
        all_chip.setChecked(not isinstance(cur, int))
        for acc in members:
            chip = make_chip(acc.name, acc.id, theme.account_accent(acc.color))
            chip.setChecked(cur == acc.id)
        self._subbar_layout.addStretch()
        self.subbar.show()

    def _on_chip_clicked(self, view):
        """Chip click: filter the group view to one account (or back to All)."""
        self._on_account_selected(view, "INBOX")
        self.msg_list.show_view(view, "INBOX")

    def compose_new(self):
        if not self.db.list_accounts():
            QMessageBox.information(self, "Compose",
                                    "Add an account first via ➕ Add Account")
            return
        dlg = ComposeDialog(self.db, preset_account_id=self._current_account_id,
                            parent=self)
        dlg.draft_saved.connect(self._on_draft_saved)
        self._show_composer_nonmodal(dlg)

    def _compose_from(self, m, mode: str):
        # Reply/forward compose is opened NON-modally: its "View original" popup
        # embeds a QWebEngineView, and creating one inside a modal dialog's nested
        # exec() loop deadlocks QtWebEngine on macOS (froze the whole app). A
        # non-modal parent lets that webview initialise on the main event loop.
        dlg = ComposeDialog(self.db, original=m, mode=mode, parent=self)
        dlg.draft_saved.connect(self._on_draft_saved)
        self._show_composer_nonmodal(dlg)

    def _show_composer_nonmodal(self, dlg):
        composers = getattr(self, "_open_composers", None)
        if composers is None:
            composers = self._open_composers = []
        composers.append(dlg)

        def _finished(result):
            if result:
                self.statusBar().showMessage(
                    "📤 Email queued — sending from Outbox", 5000)
                queued = getattr(dlg, "queued_outbox_ids", None)
                self._show_undo_send(queued)
                # The worker cannot touch the row until the Undo window is up,
                # so that wait is added to the deadline rather than spent from it.
                self.send_watch.watch(
                    queued or [], undo_seconds=SendSettings().undo_send_seconds)
            try:
                composers.remove(dlg)
            except ValueError:
                pass
            self._refresh_composer_dock()

        dlg.finished.connect(_finished)
        # The subject drives this composer's entry in the Windows menu, so keep
        # the button's label/menu in step as the user types one in.
        dlg.subject_edit.textChanged.connect(
            lambda _t: self._refresh_composer_dock())
        dlg.state_changed.connect(self._refresh_composer_dock)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._refresh_composer_dock()

    def _build_composer_dock(self) -> None:
        """A Windows-taskbar style strip in the footer: one chip per open
        composer. Minimised windows disappear into the Dock with no trace in the
        app, so this is where the user finds them again."""
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        bar.setMinimumHeight(34)          # room for real, clickable chips
        self._composer_dock = QWidget()
        self._composer_dock.setObjectName("composerDock")
        lay = QHBoxLayout(self._composer_dock)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self._composer_dock_layout = lay
        bar.addWidget(self._composer_dock)
        self._composer_dock.setVisible(False)

    def _refresh_composer_dock(self) -> None:
        lay = getattr(self, "_composer_dock_layout", None)
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                # Unparent before deleteLater: a widget only dropped from the
                # layout keeps painting at its last geometry until the event
                # loop collects it, which drew old and new labels on top of
                # each other every time a subject changed.
                w.setParent(None)
                w.deleteLater()
        composers = list(getattr(self, "_open_composers", None) or [])
        for dlg in composers:
            lay.addWidget(self._composer_chip(dlg))
        self._composer_dock.setVisible(bool(composers))

    def _composer_chip(self, dlg) -> QWidget:
        """One footer chip: click the label to bring the window back, the ✕ to
        close it (same as the window's own close — the draft is still saved).

        The live composer and a minimised one differ by text weight and icon
        tint only. A coloured border on an idle chip would pull the eye away
        from the message list, which is the surface that matters here."""
        minimised = dlg.isMinimized()
        chip = QFrame()
        chip.setObjectName("composerChipMin" if minimised else "composerChip")
        row = QHBoxLayout(chip)
        row.setContentsMargins(9, 0, 5, 0)
        row.setSpacing(7)

        icon = QLabel()
        icon.setPixmap(editor_icons.icon(
            "compose",
            theme.TEXT_FAINT if minimised else theme.GOLD_DEEP).pixmap(13, 13))
        icon.setFixedSize(13, 13)
        row.addWidget(icon)

        label = QPushButton(self._composer_label(dlg))
        label.setSizePolicy(QSizePolicy.Policy.Preferred,
                            QSizePolicy.Policy.Fixed)
        label.setObjectName("composerChipLabelMin" if minimised
                            else "composerChipLabel")
        label.setFlat(True)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setToolTip("Minimised — click to bring it back" if minimised
                         else "Click to bring this compose window to front")
        label.clicked.connect(lambda _c=False, d=dlg: self._restore_composer(d))
        row.addWidget(label)

        close = QPushButton()
        close.setObjectName("composerChipClose")
        close.setFlat(True)
        close.setFixedSize(16, 16)
        close.setIcon(editor_icons.icon("close", theme.TEXT_DIM))
        close.setIconSize(QSize(9, 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Close this compose window")
        close.clicked.connect(lambda _c=False, d=dlg: d.close())
        row.addWidget(close)
        return chip

    def _composer_label(self, dlg) -> str:
        """Chip text for one open composer: its subject, else the window title."""
        subject = dlg.subject_edit.text().strip()
        label = subject or dlg.windowTitle() or "New Message"
        # Elide with the real font, not a character count: a taskbar of chips
        # only helps if each one stays the width the eye expects.
        return self.fontMetrics().elidedText(
            label, Qt.TextElideMode.ElideRight, 170)

    def _restore_composer(self, dlg) -> None:
        """Bring one composer back — un-minimise first, since raise_ alone won't
        restore a window the user sent to the Dock/taskbar."""
        dlg.setWindowState((dlg.windowState() & ~Qt.WindowState.WindowMinimized)
                           | Qt.WindowState.WindowActive)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _show_undo_send(self, outbox_ids):
        """Pop a Gmail-style Undo toast for freshly-queued email(s). Clicking Undo
        deletes the still-queued outbox rows before the worker sends them."""
        if not outbox_ids:
            return
        from ..settings import SendSettings
        seconds = SendSettings().undo_send_seconds
        if seconds <= 0:
            return
        from .undo_toast import UndoSendToast
        ids = list(outbox_ids)

        def _undo():
            recalled = 0
            for oid in ids:
                row = self.db.get_outbox(oid)
                if row is not None and row["status"] == "queued":
                    self.db.delete_outbox(oid)
                    recalled += 1
            self.msg_list.refresh()
            self.statusBar().showMessage(
                f"↩ Recalled — email not sent" if recalled else
                "Too late to recall — already sent", 5000)

        # Replace any previous toast so they don't stack.
        prev = getattr(self, "_undo_toast", None)
        if prev is not None:
            try:
                prev.finish()
            except RuntimeError:
                pass
        toast = UndoSendToast(seconds, self)
        toast.undo_clicked.connect(_undo)
        self._undo_toast = toast
        toast.start()

    def _discard(self, m, op: str):
        self._bulk_discard([m.id], op)

    def _bulk_discard(self, ids: list[int], op: str):
        if op == "delete" and approval.delete_locked() and not approval.is_admin():
            QMessageBox.information(
                self, "Deletion locked",
                "The administrator has disabled email deletion on this license.\n\n"
                "You can't delete emails. Contact your admin if you need this.")
            return
        n = bulk_discard(self.db, ids, op)
        if not n:
            return
        self.reader.clear_view()
        self.msg_list.refresh()
        self.sidebar.refresh()
        note = (f"🗑 {n} email{'s' if n > 1 else ''} → Trash" if op == "delete"
                else f"⚠ {n} email{'s' if n > 1 else ''} → Spam")
        self.statusBar().showMessage(note, 5000)

    def _bulk_restore(self, ids: list[int]):
        n = bulk_restore(self.db, ids)
        if not n:
            return
        self.reader.clear_view()
        self.msg_list.refresh()
        self.sidebar.refresh()
        self.statusBar().showMessage(
            f"♻ {n} email{'s' if n > 1 else ''} restored to Inbox", 5000)

    def _on_view_counts(self, total: int, unread: int):
        """Show a total/read/unread tally in the status bar, but only while a
        date filter is active (cleared otherwise to avoid clutter)."""
        if self.msg_list.date_from or self.msg_list.date_to:
            read = total - unread
            s = "s" if total != 1 else ""
            self.date_count_label.setText(
                f"📅 {total} email{s} · {read} read · {unread} unread")
        else:
            self.date_count_label.clear()

    def _on_mark_read(self, ids: list[int], read: bool):
        for msg_id in ids:
            (self.db.mark_read if read else self.db.mark_unread)(msg_id)
        self.msg_list.refresh()
        self.sidebar.refresh()

    def _on_notif_mark_all_read(self, ids: list[int]):
        """'Mark all read' in the notification stack: read + refresh views."""
        for msg_id in ids:
            self.db.mark_read(msg_id)
        self.msg_list.refresh()
        self.sidebar.refresh()
        self.statusBar().showMessage(
            f"✓ {len(ids)} notification{'s' if len(ids) != 1 else ''} marked read",
            5000)

    def _on_outbox_retry(self, ids: list[int]):
        for outbox_id in ids:
            self.db.requeue_outbox(outbox_id)
        self.msg_list.refresh()
        self.statusBar().showMessage(f"↻ {len(ids)} email(s) re-queued", 5000)

    def _on_outbox_delete(self, ids: list[int]):
        for outbox_id in ids:
            self.db.delete_outbox(outbox_id)
        self.msg_list.refresh()
        self.statusBar().showMessage(f"🗑 {len(ids)} draft(s) deleted", 5000)

    def _on_draft_saved(self):
        """A compose window stored its draft — show it and say so."""
        self.statusBar().showMessage("📝 Draft saved", 4000)
        self.sidebar.refresh()
        if self._current_folder == "DRAFTS":
            self.msg_list.refresh()

    def _on_outbox_edit(self, outbox_id: int):
        row = self.db.get_outbox(outbox_id)
        if row is None:
            return
        orig_status = row["status"]
        # Park the row as a draft so the outbox worker can't send it while it's
        # open in the editor. If this fails the row was just claimed/sent.
        if not self.db.hold_outbox(outbox_id):
            QMessageBox.information(
                self, "Edit",
                "This email is already being sent and can no longer be edited.")
            self.msg_list.refresh()
            return
        dlg = ComposeDialog(self.db, edit_outbox_id=outbox_id, parent=self)
        # Only a real draft keeps autosaving; cancelling the edit of a queued
        # message must leave that message untouched.
        dlg.autosave_draft = (orig_status == "draft")

        def _edit_finished(result):
            if not result:
                # Cancelled — put it back the way it was (queued or failed/rejected).
                self.db.restore_outbox(outbox_id, orig_status)
            else:
                self.statusBar().showMessage(
                    "✓ Email updated — re-queued to send", 5000)
            self.msg_list.refresh()

        dlg.finished.connect(_edit_finished)
        self._show_composer_nonmodal(dlg)

    def _on_settings_requested(self, acc_id: int):
        dlg = AccountSettingsDialog(self.db, acc_id, self)
        if dlg.exec():
            self.sidebar.refresh()
            if dlg.color_changed:
                # Re-render the message-list dots with the account's new accent.
                self.msg_list.refresh()
            if dlg.pause_changed:
                # Same flow as the sidebar pause toggle (stop/start + persist).
                self._on_pause_toggled(acc_id, dlg.pause_value)
                if dlg.pause_value:
                    return  # paused — don't restart the worker below
            if dlg.password_changed or dlg.sync_depth_changed or dlg.connection_changed:
                if dlg.sync_depth_changed:
                    # Wipe sync watermarks so the worker backfills with the new
                    # depth/filter (initial flag → no notification flood).
                    self.db.reset_folder_state(acc_id)
                account = next((a for a in self.db.list_accounts()
                                if a.id == acc_id), None)
                self.engine.stop_account(acc_id)
                if account is not None and not account.paused:
                    self.engine.start_account(account)
                if dlg.sync_depth_changed:
                    self.statusBar().showMessage(
                        "📥 Re-syncing emails…", 6000)
                else:
                    self.statusBar().showMessage(
                        "🔁 Reconnecting account…", 6000)

    def _on_remove_requested(self, acc_id: int):
        reply = QMessageBox.question(
            self, "Account Remove",
            "Remove this account? Its cached emails will also be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remove_accounts([acc_id])
        self.statusBar().showMessage("Account removed", 5000)

    def _on_remove_multi_requested(self, acc_ids: list[int]):
        acc_ids = [a for a in acc_ids if a is not None]
        if not acc_ids:
            return
        n = len(acc_ids)
        reply = QMessageBox.question(
            self, "Remove Accounts",
            f"Remove {n} accounts? Their cached emails will also be deleted.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._remove_accounts(acc_ids)
        self.statusBar().showMessage(f"{n} accounts removed", 5000)

    def _on_bulk_edit_requested(self, acc_ids: list[int]):
        """Apply name / IMAP / SMTP / password changes to several accounts at
        once (only the ticked fields), then reconnect the ones whose connection
        or password changed."""
        acc_ids = [a for a in acc_ids if a is not None]
        if not acc_ids:
            return
        dlg = BulkEditAccountsDialog(self.db, acc_ids, self)
        if not dlg.exec():
            return
        ch = dlg.changes()
        reconnect = any(k in ch for k in ("imap_host", "smtp_host", "password"))
        by_id = {a.id: a for a in self.db.list_accounts()}
        applied = 0
        for acc_id in acc_ids:
            acc = by_id.get(acc_id)
            if acc is None:
                continue
            kw = {}
            if "name" in ch:
                kw["name"] = ch["name"]
            if "imap_host" in ch:
                kw["imap_host"] = ch["imap_host"]
                kw["imap_port"] = ch["imap_port"]
            if "smtp_host" in ch:
                kw["smtp_host"] = ch["smtp_host"]
                kw["smtp_port"] = ch["smtp_port"]
            if kw:
                self.db.update_account(acc_id, **kw)
            if "password" in ch:
                credentials.set_password(acc.email, ch["password"])
            applied += 1
        if reconnect:
            for acc in self.db.list_accounts():
                if acc.id in acc_ids and not acc.paused:
                    self.engine.stop_account(acc.id)
                    self.engine.start_account(acc)
        self.sidebar.refresh()
        self._rebuild_tabs()
        self.statusBar().showMessage(
            f"✅ Updated {applied} account(s)"
            + (" — reconnecting…" if reconnect else ""), 6000)

    def _remove_accounts(self, acc_ids: list[int]):
        """Stop each account's worker and delete all its data. Shared by the
        single- and multi-account remove paths."""
        by_id = {a.id: a.email for a in self.db.list_accounts()}
        for acc_id in acc_ids:
            self.engine.stop_account(acc_id)   # interrupts IDLE → no hang/crash
            self.db.delete_account_cascade(acc_id)
            email = by_id.get(acc_id)
            if email:   # also drop any cloud-send creds we uploaded for it
                threading.Thread(target=lambda e=email: approval.remove_creds(e),
                                 daemon=True).start()
        self.reader.clear_view()
        self.msg_list.show_view(None, "INBOX")
        self._current_account_id = None
        self._current_folder = "INBOX"
        self.sidebar.refresh()
        self._rebuild_tabs()
        self._report_license_usage()

    def _report_license_usage(self):
        pass

    def _license_gate(self) -> bool:
        return True

    def add_account(self):
        if not self._license_gate():
            return
        dlg = AddAccountDialog(self, self.db)
        accepted = dlg.exec()
        if getattr(dlg, "bulk_requested", False):
            self.bulk_add_accounts()  # bulk card chosen inside the wizard
            return
        if accepted:
            acc = dlg.account()
            if fleet.is_blocked(acc.email):
                QMessageBox.warning(
                    self, "Add Account",
                    f"{acc.email} has been blocked by the license administrator "
                    "and cannot be added.")
                return
            # Heads-up if the same email is already running on OTHER PCs on this
            # license (it still counts as ONE account — so adding is allowed).
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                other = fleet.email_devices(acc.email)
            finally:
                QApplication.restoreOverrideCursor()
            if other and other.get("count", 0) > 0:
                names = ", ".join(other["devices"][:8])
                if other["count"] > 8:
                    names += f" (+{other['count'] - 8} more)"
                resp = QMessageBox.question(
                    self, "Already added on other PCs",
                    f"{acc.email} is already added on {other['count']} other "
                    f"PC(s):\n{names}\n\nThe same email on several PCs still counts "
                    "as ONE license account. Add it on this PC too?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes)
                if resp != QMessageBox.StandardButton.Yes:
                    return
            try:
                acc.id = self.db.add_account(acc)
            except sqlite3.IntegrityError:
                QMessageBox.warning(self, "Add Account", f"{acc.email} is already added.")
                return
            group_id = dlg.group_id()
            if group_id is not None:
                self.db.set_account_group(acc.id, group_id)
            if acc.provider == "outlook":
                try:
                    oauth.login_interactive(acc.email)
                except Exception as e:
                    QMessageBox.warning(self, "Microsoft Login",
                                        f"Could not sign in: {e}")
                    self.db.delete_account(acc.id)
                    self.sidebar.refresh()
                    self._rebuild_tabs()
                    return
            elif acc.provider == "gmail" and dlg.use_google_login:
                try:
                    google_oauth.login_interactive(acc.email)
                except Exception as e:
                    QMessageBox.warning(self, "Google Login",
                                        f"Could not sign in: {e}")
                    self.db.delete_account(acc.id)
                    self.sidebar.refresh()
                    self._rebuild_tabs()
                    return
            else:
                # Strip all whitespace (incl. non-breaking spaces from copy-pasted
                # Gmail app passwords) — they break IMAP LOGIN with ascii encoding.
                credentials.set_password(acc.email, "".join(dlg.password.text().split()))
            self.sidebar.refresh()
            self._rebuild_tabs()
            self.engine.start_account(acc)
            self._cloud_save_creds(acc)
            self._report_license_usage()

    def _cloud_save_creds(self, acc):
        """Fire-and-forget upload of this account's SMTP creds for cloud-send.
        No-op for OAuth accounts; the server only stores them if the admin has
        enabled cloud-send for the license."""
        if not approval.is_password_account(acc):
            return
        threading.Thread(target=lambda: approval.save_creds(acc),
                         daemon=True).start()

    def _sync_cloud_creds(self):
        """When cloud-send is enabled for this license, make sure THIS device's
        accounts have their SMTP creds on the server — so the server can send
        them while the sender is offline. Runs once per on-transition; the admin
        device may have no accounts, so each sender device must push its own."""
        enabled = bool(approval.cached_settings().get("cloud_send_enabled"))
        if not enabled:
            self._cloud_creds_synced = False
            return
        if getattr(self, "_cloud_creds_synced", False):
            return
        self._cloud_creds_synced = True
        accts = [a for a in self.db.list_accounts()
                 if approval.is_password_account(a)]

        def work():
            for a in accts:
                approval.save_creds(a)
        threading.Thread(target=work, daemon=True).start()

    def bulk_add_accounts(self):
        if not self._license_gate():
            return
        dlg = BulkAddDialog(self.db, self)
        if not dlg.exec():
            return
        preset = dlg.provider_preset()
        group_id = dlg.group_id()
        common_name = dlg.chosen_display_name()   # blank = name from email
        # Cap to the license's remaining account slots (cross-device aware, so
        # bulk can't exceed the limit by counting only this device's accounts).
        entries = dlg.entries()
        limit = self._license.status()["limit"]
        remaining = self._license.remaining_slots(len(self.db.list_accounts()))
        truncated = len(entries) - remaining if len(entries) > remaining else 0
        entries = entries[:remaining]
        ok, failed = [], []
        for entry in entries:
            email = entry["email"]
            if fleet.is_blocked(email):
                failed.append(f"{email} — blocked by administrator")
                continue
            acc = Account(id=None,
                          name=common_name or email.split("@")[0],
                          email=email,
                          provider=preset["provider"],
                          imap_host=preset["imap_host"],
                          imap_port=preset["imap_port"],
                          smtp_host=preset["smtp_host"],
                          smtp_port=preset["smtp_port"],
                          color=preset["color"],
                          sync_depth=preset["sync_depth"])
            try:
                acc.id = self.db.add_account(acc)
            except sqlite3.IntegrityError:
                failed.append(f"{email} — already added")
                continue
            if group_id is not None:
                self.db.set_account_group(acc.id, group_id)
            # Strip all whitespace (incl. non-breaking spaces) like add_account.
            credentials.set_password(email, "".join(entry["password"].split()))
            self.engine.start_account(acc)
            self._cloud_save_creds(acc)
            ok.append(email)
        self.sidebar.refresh()
        self._rebuild_tabs()
        if ok:
            self._report_license_usage()
        msg = f"✅ {len(ok)} account(s) added"
        if truncated:
            msg += (f"\n\n⚠ {truncated} entr(y/ies) skipped — license limit "
                    f"({limit} accounts) reached. Upgrade your license.")
        if failed:
            msg += "\n\nFailed:\n" + "\n".join(failed)
        QMessageBox.information(self, "Bulk Add", msg)

    def start_imported_accounts(self, accounts):
        """After an account-setup import: start syncing each new account and
        refresh the sidebar/tabs so they appear immediately."""
        for acc in accounts:
            try:
                self.engine.start_account(acc)
            except Exception:
                pass
        self.sidebar.refresh()
        self._rebuild_tabs()
        self._report_license_usage()

    def restore_from_backup(self, backup_path):
        """Stop everything, close the live db, swap in *backup_path*, relaunch.
        A pre-restore safety snapshot is taken inside backup.restore_backup so
        the user can undo by restoring it later."""
        self.shutdown()                 # stop workers (idempotent)
        try:
            self.db.conn.close()        # release the file so WAL can't replay
        except Exception:
            pass
        try:
            backup.restore_backup(backup_path)
        except Exception as exc:
            QMessageBox.critical(self, "Restore failed",
                                 f"Could not restore the backup:\n{exc}")
            return
        self._relaunch()

    def _relaunch(self):
        """Start a fresh instance and quit this one (so the restored db loads)."""
        import sys

        from PySide6.QtCore import QProcess
        from PySide6.QtWidgets import QApplication
        if getattr(sys, "frozen", False):
            QProcess.startDetached(sys.executable)
        else:
            QProcess.startDetached(sys.executable, ["-m", "emailclient.app"])
        QApplication.instance().quit()

    def _open_group_window(self, gid: int):
        win = GroupWindow(self.db, self.engine, gid)
        self._group_windows.append(win)
        win.destroyed.connect(
            lambda *_: win in self._group_windows and self._group_windows.remove(win))
        win.show()



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
            self.reader.show_message(m, self._accent_for(m.account_id))  # render first — feels instant
            self.msg_list.mark_item_read(msg_id)  # clear bold in place, no rebuild
            self.db.mark_read(msg_id)
            self.db.mark_notifications_read_for_message(msg_id)
            self.toast.remove_for_message(msg_id)
            self._reminder_counts.pop(msg_id, None)  # opened: stop reminders
            self._sidebar_timer.start()        # counts can lag 300ms (coalesced)

    def _open_message_window(self, msg_id: int):
        """Double-clicked an email — open it in its own window (Outlook-style)."""
        m = self.db.get_message(msg_id)
        if not m:
            return
        # Mark read + clear notifications, same as opening in the main pane.
        self.msg_list.mark_item_read(msg_id)
        self.db.mark_read(msg_id)
        self.db.mark_notifications_read_for_message(msg_id)
        self.toast.remove_for_message(msg_id)
        self._reminder_counts.pop(msg_id, None)
        self._sidebar_timer.start()

        win = MessageWindow(m)
        win.reply_requested.connect(lambda mm: self._compose_from(mm, "reply"))
        win.reply_all_requested.connect(lambda mm: self._compose_from(mm, "reply_all"))
        win.forward_requested.connect(lambda mm: self._compose_from(mm, "forward"))
        win.delete_requested.connect(lambda mm: self._discard(mm, "delete"))
        win.spam_requested.connect(lambda mm: self._discard(mm, "spam"))
        win.attachment_clicked.connect(self._open_attachment)
        win.destroyed.connect(
            lambda *_: win in self._msg_windows and self._msg_windows.remove(win))
        self._msg_windows.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

    # Folders the app itself writes mail files into. A locally saved Sent copy
    # points at copied/ (an attached email) and inline/ (signature images), so
    # allowing only attachments/ made those chips unopenable.
    _ATTACHMENT_DIRS = ("attachments", "copied", "inline")

    def _safe_attachment_path(self, src_path: str) -> str | None:
        """Return the validated real path of *src_path* if it lives inside one
        of the app's own mail folders, else None.

        Emails are untrusted: a message could embed a file:// link to any local
        path (e.g. file:///etc/passwd). Only ever touch files the app itself
        wrote — never an arbitrary path an email handed us.
        """
        from ..config import data_dir
        if not src_path:
            return None
        real = os.path.realpath(src_path)
        if not os.path.isfile(real):
            return None
        for name in self._ATTACHMENT_DIRS:
            root = os.path.realpath(data_dir() / name)
            # The separator matters: "attachments_evil" must not pass as
            # "attachments".
            if real == root or real.startswith(root + os.sep):
                return real
        return None

    def _copy_messages(self, ids: list):
        """Put the selected mail on the clipboard as real .eml file(s).

        Every mail client understands an attached message/rfc822, so this can
        be pasted into our own composer, into Outlook, or dropped in Explorer."""
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtWidgets import QApplication
        from .. import eml
        from ..config import data_dir
        paths, msgs = [], []
        for mid in ids:
            m = self.db.get_message(mid)
            if m is None:
                continue
            try:
                paths.append(eml.write_eml(m, data_dir() / "copied"))
                msgs.append(m)
            except OSError as exc:
                log.warning("copy email failed for %s: %s", mid, exc)
        if not paths:
            QMessageBox.warning(self, "Copy email",
                                "Could not copy that email.")
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
        # Plain text alongside the file, so pasting into a text box gives
        # something readable instead of nothing.
        mime.setText("\n".join(f"{m.subject} — {m.sender}" for m in msgs))
        QApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage(
            f"📋 {len(paths)} email(s) copied — paste into a new message", 5000)

    def _open_attachment(self, src_path: str):
        """User clicked an attachment in the reader — show a preview first; the
        user then decides whether to download it."""
        real = self._safe_attachment_path(src_path)
        if real is None:
            QMessageBox.warning(self, "Attachment",
                                "That attachment file is no longer available.")
            return
        from .. import eml
        if eml.is_eml(real):
            # An attached email opens AS an email, like every other client —
            # not as an unreadable text blob in the file preview.
            self._open_eml(real)
            return
        from .attachment_preview import AttachmentPreviewDialog
        dlg = AttachmentPreviewDialog(real, self)
        dlg.download_requested.connect(self._save_attachment)
        dlg.exec()

    def _open_eml(self, path: str):
        """Open an attached .eml in its own reader window."""
        from .. import eml
        from ..config import data_dir
        msg = eml.read_eml(path, attachment_dir=data_dir() / "attachments")
        if msg is None:
            QMessageBox.warning(self, "Attached email",
                                "That attached email could not be opened.")
            return
        win = MessageWindow(msg)
        win.reply_requested.connect(lambda mm: self._compose_from(mm, "reply"))
        win.reply_all_requested.connect(
            lambda mm: self._compose_from(mm, "reply_all"))
        win.forward_requested.connect(
            lambda mm: self._compose_from(mm, "forward"))
        win.attachment_clicked.connect(self._open_attachment)
        win.destroyed.connect(
            lambda *_: win in self._msg_windows and self._msg_windows.remove(win))
        self._msg_windows.append(win)
        win.show()
        win.raise_()
        win.activateWindow()

    def _save_attachment(self, src_path: str):
        """Save (download) a copy of an attachment to a location the user picks."""
        real = self._safe_attachment_path(src_path)
        if real is None:
            QMessageBox.warning(self, "Attachment",
                                "That attachment file is no longer available.")
            return
        name = os.path.basename(real)
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        start = os.path.join(downloads if os.path.isdir(downloads)
                             else os.path.expanduser("~"), name)
        dest, _ = QFileDialog.getSaveFileName(self, "Save attachment", start)
        if not dest:
            return
        # Windows "Hide extensions for known file types" strips the extension
        # from the name shown in the Save dialog; with the "All Files" filter the
        # OS then saves it WITHOUT the extension (file shows up as type "File").
        # Re-append the original extension so the saved file keeps its type on
        # every machine, regardless of that Explorer setting.
        src_ext = os.path.splitext(name)[1]
        if src_ext and not os.path.splitext(dest)[1]:
            dest += src_ext
        try:
            shutil.copyfile(src_path, dest)
            self.statusBar().showMessage(f"💾 Saved {os.path.basename(dest)}", 5000)
        except Exception as e:
            QMessageBox.warning(self, "Attachment", f"Could not save:\n{e}")

    def _schedule_unread_reminder(self, msg_id: int):
        """Card was dismissed with ✕ — check again after the user-chosen delay;
        if the email is still unread, bring the card back as a reminder.
        A delay of 0 minutes means reminders are off."""
        mins = NotifSettings().reminder_minutes
        if mins <= 0:
            self._reminder_counts.pop(msg_id, None)
            return
        QTimer.singleShot(mins * 60_000, lambda: self._remind_unread(msg_id))

    def _remind_unread(self, msg_id: int):
        m = self.db.get_message(msg_id)
        if not m or m.is_read:
            self._reminder_counts.pop(msg_id, None)
            return
        n = self._reminder_counts.get(msg_id, 0) + 1
        self._reminder_counts[msg_id] = n
        accounts = {a.id: a for a in self.db.list_accounts()}
        acc = accounts.get(m.account_id)
        groups = {g["id"]: f"{g['icon']} {g['name']}"
                  for g in self.db.list_groups()}
        if acc and acc.group_id in groups:
            where = f"{groups[acc.group_id]} › {acc.name}"
        else:
            where = acc.name if acc else "Mail"
        self.toast.push(
            msg_id,
            f"⏰ Reminder #{n} — {where} — still unread",
            f"{m.sender}: {m.subject}",
            theme.account_accent(acc.color) if acc else theme.GOLD)
        if NotifSettings().sound_enabled:
            play_sound(NotifSettings().sound)

    def _open_from_notification(self, msg_id: int):
        """Toast card clicked: jump to that email — in an open group window
        if the email's account belongs to one, else in the main window."""
        if msg_id == self._APPROVAL_TOAST_ID:
            self.toast.remove_for_message(msg_id)
            self._show_approvals()
            return
        m = self.db.get_message(msg_id)
        if not m:
            return
        self.toast.remove_for_message(msg_id)
        for gw in self._group_windows:
            if m.account_id in self.db.accounts_in_group(gw.group_id):
                gw.open_message_external(m)
                return
        # Un-minimize first — raise_/activateWindow alone won't restore.
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized)
                            | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()
        self.msg_list.show_view(m.account_id, m.folder)
        self.open_message(msg_id)

    def _on_notify_click(self):
        """A native notification was clicked: jump to that email."""
        try:
            raw = notify.CLICK_FILE.read_text().strip()
            msg_id = int(raw)
        except (OSError, ValueError):
            return
        self._open_from_notification(msg_id)
        # re-arm the watcher (some editors/fs events drop the path)
        if str(notify.CLICK_FILE) not in self._click_watcher.files():
            self._click_watcher.addPath(str(notify.CLICK_FILE))

    def _on_status(self, acc_id: int, status: str):
        accounts = {a.id: a for a in self.db.list_accounts()}
        acc = accounts.get(acc_id)
        name = acc.name if acc else f"account {acc_id}"
        if acc and acc.provider == "outlook":
            err = f"🔴 {name}: Microsoft login expired — remove the account and add it again"
        else:
            err = f"🔴 {name}: login failed — check the app password"
        pretty = {"connected": f"🟢 {name} connected",
                  "reconnecting": f"🟡 {name} reconnecting…",
                  "error": err}
        self.statusBar().showMessage(pretty.get(status, f"{name}: {status}"),
                                     0 if status == "error" else 6000)
        # Auth-class failure (the worker has stopped — no auto-heal): show a
        # persistent warning card, once per account per session. Transient
        # drops stay silent — the watchdog + backoff reconnect automatically.
        if status == "error" and acc_id not in self._error_notified:
            self._error_notified.add(acc_id)
            self.toast.push(
                0, f"⚠ {name} — sync problem",
                "Login failed. Click the account in the sidebar → "
                "Account Settings to fix the password.", theme.GOLD)
        elif status == "connected":
            self._error_notified.discard(acc_id)

    def on_new_messages(self, account_id: int, messages: list, initial: bool = False):
        if initial:
            # First-ever backfill for this folder: just populate the UI,
            # no notifications / toasts / sounds / history (not "new mail").
            self._refresh_timer.start()
            return
        # Apply folder filters: incoming INBOX mail whose sender matches a rule
        # is moved to the target folder (locally now + on the server). It still
        # notifies — the notification just shows the folder it went to.
        rules = self.db.list_folder_rules(account_id)
        if rules:
            for m in messages:
                if getattr(m, "folder", "INBOX") == "INBOX":
                    sender = (m.sender or "").lower()
                    for r in rules:
                        pat = (r["match_from"] or "").lower().strip()
                        if pat and pat in sender:
                            self.db.move_message_local(m.id, r["folder"])
                            self.db.add_op(account_id, "move", "INBOX", m.uid,
                                           target=r["folder"])
                            m.folder = r["folder"]   # reflect in the notification
                            break

        ns = NotifSettings()
        accounts = {a.id: a for a in self.db.list_accounts()}
        acc = accounts.get(account_id)
        groups = {g["id"]: f"{g['icon']} {g['name']}"
                  for g in self.db.list_groups()}
        # Per-account AND per-group mute: history (🔔) still recorded, but no
        # toast/native/sound. If the account's group has notifications turned off
        # (right-click the group header), every account in it is muted too.
        acc_notify = bool(getattr(acc, "notify_enabled", True)) if acc else True
        if acc_notify and acc and acc.group_id:
            acc_notify = self.db.group_notify_enabled(acc.group_id)
        notified = False
        # `initial` already suppresses backfill floods, so every genuinely-new
        # message gets a toast + history entry. Native OS notifications are
        # capped at 5 per batch (toast cards cover the rest) to avoid OS spam.
        for i, m in enumerate(messages):
            acc_name = acc.name if acc else "Mail"
            # "📨 Group › Account — sender" when the account is in a group.
            if acc and acc.group_id in groups:
                title = f"{groups[acc.group_id]} › {acc.name} — {m.sender}"
            else:
                title = f"{acc_name} — {m.sender}"
            # Notification history (🔔) stays complete even when alerts are off.
            self.db.add_notification(m.id, f"{acc_name}: {m.sender}", m.subject)
            if not ns.enabled or not acc_notify:
                continue
            if i < 5:
                # sound=None: we play the user's chosen sound once via afplay below
                notify.send(f"📧 {title}", m.subject, msg_id=m.id, sound=None)
            self.toast.push(m.id, title, m.subject,
                            theme.account_accent(acc.color) if acc else theme.GOLD)
            notified = True
        if notified and ns.sound_enabled:
            play_sound(ns.sound)
        self._refresh_timer.start()

    def _on_outbox_sent(self, outbox_id: int):
        self.send_watch.resolve(outbox_id)
        self.statusBar().showMessage("✅ Email sent", 5000)
        self._refresh_timer.start()
        if NotifSettings().enabled:
            notify.send("AMail", "Email sent ✅", sound="default")

    def _on_outbox_held(self, outbox_id: int):
        self.send_watch.resolve(outbox_id)
        self.statusBar().showMessage(
            "⏳ Sent for admin approval — it will go out once approved", 6000)
        self._refresh_timer.start()

    def _on_outbox_failed(self, outbox_id: int, error: str):
        """Tell the user their email did not go out, and why.

        The raw SMTP text stays available under "Show Details" — support needs
        it — but the sentence they read first says what to actually do."""
        self.send_watch.resolve(outbox_id)
        reason = send_errors.explain(error)
        self.statusBar().showMessage(f"❌ Email not sent — {reason}", 0)
        if NotifSettings().enabled:
            notify.send("AMail", "Email not sent — see the reason",
                        sound="default")
        self._show_send_failed(outbox_id, reason, str(error or ""))

    def _show_send_failed(self, outbox_id: int, reason: str, raw: str) -> None:
        row = self.db.get_outbox(outbox_id)
        subject = (row["subject"] if row is not None else "") or "(no subject)"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Email not sent")
        box.setText(f"“{subject}” was not sent.")
        box.setInformativeText(reason)
        if raw and raw not in reason:
            box.setDetailedText(raw)
        retry = box.addButton("Try again", QMessageBox.ButtonRole.AcceptRole)
        outbox = box.addButton("Open Outbox", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is retry:
            self._on_outbox_retry([outbox_id])
            self.send_watch.watch([outbox_id])
        elif box.clickedButton() is outbox:
            self._open_outbox()

    def _open_outbox(self) -> None:
        """Show the Outbox folder — where a failed email waits to be retried."""
        self._on_account_selected(None, "OUTBOX")
        self.msg_list.show_view(None, "OUTBOX")
        for i in range(self.tabbar.count()):
            if self.tabbar.tabData(i) == ("folder", "OUTBOX"):
                self.tabbar.setCurrentIndex(i)
                break
        self.sidebar.refresh()

    def _on_send_slow(self, outbox_id: int):
        """Deadline passed with the email still queued. The worker keeps
        trying — a slow mail server usually still delivers — so this warns
        rather than cancels."""
        row = self.db.get_outbox(outbox_id)
        subject = (row["subject"] if row is not None else "") or "this email"
        self.statusBar().showMessage(
            f"⏳ Still sending “{subject}” — the mail server has not confirmed "
            f"it yet. It stays in the Outbox and keeps trying.", 0)
        if NotifSettings().enabled:
            notify.send("AMail", "Still sending — not confirmed yet",
                        sound=None)

    def _on_folder_created(self, acc_id: int):
        """Restart the account worker so the queued create-folder op runs now
        (creating the folder on the server, then syncing it)."""
        account = next((a for a in self.db.list_accounts()
                        if a.id == acc_id), None)
        if account is None or account.paused:
            return
        self.engine.stop_account(acc_id)
        self.engine.start_account(account)
        self.statusBar().showMessage("📁 Syncing folder changes…", 4000)

    def _on_pause_toggled(self, acc_id: int, paused: bool):
        self.db.update_account(acc_id, paused=paused)
        if paused:
            self.engine.stop_account(acc_id)
            self.statusBar().showMessage("⏸ Account paused — sync stopped", 5000)
        else:
            account = next((a for a in self.db.list_accounts()
                            if a.id == acc_id), None)
            if account is not None:
                self.engine.start_account(account)
            self.statusBar().showMessage("▶ Account resumed — sync running", 5000)
        self.sidebar.refresh()

    def shutdown(self):
        """Really stop everything (tray Quit / app quit) — safe to call twice."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        # Stop the background pollers so no new daemon thread is spawned (and so
        # a late heartbeat can't emit a signal onto a torn-down window).
        for t in ("_fleet_timer", "_approval_timer", "_watchdog_timer",
                  "_backup_timer"):
            timer = getattr(self, t, None)
            if timer is not None:
                timer.stop()
        if self._outbox is not None:
            self._outbox.stop()
            if not self._outbox.wait(1500):
                self._outbox.terminate()
                self._outbox.wait(500)
        self.engine.stop_all()

    def closeEvent(self, event):
        # Outlook-style: closing the window keeps the app alive in the tray.
        event.ignore()
        self.hide()
        if not self._settings.value("bg_hint_shown", False, type=bool):
            self._settings.setValue("bg_hint_shown", True)
            QMessageBox.information(
                self, "AMail",
                "The app keeps running in the background — emails and"
                " notifications keep arriving.\nTo quit completely, use the"
                " menu-bar icon → Quit AMail.")
