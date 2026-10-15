from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

from .models import Account, EmailMessage

log = logging.getLogger(__name__)


def _epoch(iso: str) -> float:
    """ISO timestamp -> epoch seconds; unparsable values sort as 0."""
    try:
        dt = datetime.fromisoformat((iso or "").strip())
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    color TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    folder TEXT NOT NULL,
    uid INTEGER NOT NULL,
    subject TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,
    date TEXT NOT NULL,
    body_text TEXT NOT NULL,
    body_html TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    cc TEXT NOT NULL DEFAULT '',
    bcc TEXT NOT NULL DEFAULT '',
    flagged INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    flag_done INTEGER NOT NULL DEFAULT 0,
    UNIQUE(account_id, folder, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date DESC);
CREATE INDEX IF NOT EXISTS idx_messages_account_folder ON messages(account_id, folder, is_read);
CREATE INDEX IF NOT EXISTS idx_messages_folder_date ON messages(folder, date DESC);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    account_id INT NOT NULL,
    to_addrs TEXT NOT NULL,
    cc TEXT NOT NULL DEFAULT '',
    bcc TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    attachments TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_ops (
    id INTEGER PRIMARY KEY,
    account_id INT NOT NULL,
    op TEXT NOT NULL,
    folder TEXT NOT NULL,
    uid INT NOT NULL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    message_id INT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS folder_state (
    account_id INTEGER NOT NULL,
    folder TEXT NOT NULL,
    uidvalidity INTEGER NOT NULL DEFAULT 0,
    last_seen_uid INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(account_id, folder)
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    color TEXT NOT NULL DEFAULT '#8A6D1F',
    sort_order INTEGER NOT NULL DEFAULT 0
);
"""


class Database:
    def __init__(self, path: Path):
        # check_same_thread=False: self._lock serializes access from worker threads
        # and the GUI so that execute/commit pairs are never interleaved.
        # WAL mode allows readers to proceed concurrently with the writer.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        # Migration: add columns that may not exist on legacy DBs
        self._ensure_column("accounts", "signature", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("accounts", "group_id", "INTEGER")
        self._ensure_column("accounts", "signature_image", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("accounts", "signature_image_width", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("accounts", "sync_depth", "TEXT NOT NULL DEFAULT '50'")
        self._ensure_column("accounts", "paused", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("accounts", "signature_html", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("accounts", "attachments_only", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("accounts", "notify_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("outbox", "body_html", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("outbox", "approval_id", "TEXT NOT NULL DEFAULT ''")
        # 1 = admin approved in "send from sender" mode: skip the approval gate
        # and send straight from this (the sender's) device so the recipient's
        # Received header carries the sender's IP, not the admin's.
        self._ensure_column("outbox", "send_direct", "INTEGER NOT NULL DEFAULT 0")
        # Epoch (seconds) before which the row must NOT be sent — the Gmail-style
        # "Undo Send" window. 0 = send immediately (default; old rows).
        self._ensure_column("outbox", "send_after", "REAL NOT NULL DEFAULT 0")
        self._ensure_column("messages", "attachments", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("messages", "cc", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("messages", "bcc", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column("messages", "flagged", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("messages", "pinned", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("messages", "flag_done", "INTEGER NOT NULL DEFAULT 0")
        # RFC Message-ID: identity of the mail itself, so the same email stored
        # under different UIDs (our local sent copy + the provider's auto-saved
        # copy + an IMAP APPEND) collapses to one row. See save_message.
        self._ensure_column("messages", "message_id", "TEXT NOT NULL DEFAULT ''")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_msgid"
            " ON messages(account_id, folder, message_id)")
        self._ensure_column("groups", "icon", "TEXT NOT NULL DEFAULT '📁'")
        # Per-group notification mute: when 0, accounts in this group don't raise
        # toasts/native popups/sounds (history is still recorded).
        self._ensure_column("groups", "notify_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("folder_state", "backfill_done", "INTEGER NOT NULL DEFAULT 0")
        # One-time flag: older messages were synced before Cc/Bcc parsing, so
        # their cc/bcc columns are empty. Set once we've re-fetched those headers.
        self._ensure_column("accounts", "cc_backfill_done", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("notifications", "is_read", "INTEGER NOT NULL DEFAULT 0")
        # Per-account custom folders (server-discovered or user-created). Lets
        # empty folders show in the sidebar before any mail lands in them.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS account_folders ("
            " account_id INTEGER NOT NULL, name TEXT NOT NULL,"
            " PRIMARY KEY(account_id, name))")
        # Filters: "mail whose sender matches X goes to folder Y".
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS folder_rules ("
            " id INTEGER PRIMARY KEY, account_id INT NOT NULL,"
            " match_from TEXT NOT NULL, folder TEXT NOT NULL)")
        self.conn.commit()
        # 'target' = destination folder for a 'move' op (empty for delete/spam).
        self._ensure_column("pending_ops", "target", "TEXT NOT NULL DEFAULT ''")
        self._migrate_account_colors()
        self._migrate_attachment_inline()
        self._migrate_sent_read()
        self._migrate_dedupe_sent()

    def _migrate_account_colors(self) -> None:
        """One-time: clear the loud per-provider colors that used to be assigned
        automatically, so every account falls back to the consistent theme
        accent (Outlook-style). User-picked colors set afterwards are kept.
        Guarded by PRAGMA user_version so it runs exactly once."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 1:
            return
        auto_colors = ("#DB4437", "#E42527", "#0078D4", "#6001D2", "#6B7FA3")
        placeholders = ",".join("?" * len(auto_colors))
        self.conn.execute(
            f"UPDATE accounts SET color='' WHERE color IN ({placeholders})",
            auto_colors)
        self.conn.execute("PRAGMA user_version=1")
        self.conn.commit()

    def _migrate_attachment_inline(self) -> None:
        """One-time: add the `inline` flag to attachments stored before it
        existed, deriving it from the body — an item is inline only if its
        Content-ID is actually referenced in the message HTML. This unhides real
        attachments (PDF/xlsx) that carried a stray Content-ID and were wrongly
        treated as inline. Guarded by PRAGMA user_version so it runs once."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 2:
            return
        rows = self.conn.execute(
            "SELECT id, body_html, attachments FROM messages"
            " WHERE attachments != '' AND attachments IS NOT NULL").fetchall()
        for r in rows:
            try:
                items = json.loads(r["attachments"])
                if not isinstance(items, list):
                    continue
            except Exception:
                continue
            html = r["body_html"] or ""
            changed = False
            for it in items:
                if not isinstance(it, dict) or "inline" in it:
                    continue
                cid = str(it.get("cid", ""))
                it["inline"] = bool(cid) and cid in html
                changed = True
            if changed:
                self.conn.execute(
                    "UPDATE messages SET attachments=? WHERE id=?",
                    (json.dumps(items), r["id"]))
        self.conn.execute("PRAGMA user_version=2")
        self.conn.commit()

    def _migrate_sent_read(self) -> None:
        """One-time: mark existing Sent/Trash mail as read. These were synced
        before the worker honored the server \\Seen flag, so the server-saved
        copy of your own sent mail landed as unread — inflating unread counts
        and firing notifications for messages you sent. Guarded by user_version."""
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 3:
            return
        self.conn.execute(
            "UPDATE messages SET is_read=1 WHERE folder IN ('SENT','TRASH')"
            " AND is_read=0")
        self.conn.execute("PRAGMA user_version=3")
        self.conn.commit()

    def _migrate_dedupe_sent(self) -> None:
        """One-time: collapse Sent rows that are the same email stored twice.

        Mail sent before Message-IDs were stamped has no id to match on, so
        these old rows are matched on (account, subject, recipients) with dates
        within two minutes of each other — tight enough that two genuinely
        separate sends don't merge, loose enough to catch the local copy (whose
        timestamp is a few seconds later than the server's).

        The lowest positive UID survives, because that is the copy the server
        can actually act on; local placeholders (negative uid) go first.
        Guarded by user_version.
        """
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= 4:
            return
        rows = self.conn.execute(
            "SELECT id, account_id, subject, recipients, date, uid"
            " FROM messages WHERE folder='SENT' ORDER BY date").fetchall()
        def norm_subject(s: str) -> str:
            # The local copy stored "" where the server copy says "(no subject)".
            return (s or "").strip() or "(no subject)"

        def norm_rcpts(s: str) -> tuple:
            # "A <a@x>, " and "A <a@x>" are the same recipients — compare the
            # addresses themselves, not the header text.
            return tuple(sorted(a.lower() for _n, a in getaddresses([s or ""])
                                if a))

        groups: dict[tuple, list] = {}
        for r in rows:
            key = (r["account_id"], norm_subject(r["subject"]),
                   norm_rcpts(r["recipients"]))
            groups.setdefault(key, []).append(r)
        doomed: list[int] = []
        for bucket in groups.values():
            cluster: list = []

            def flush(c=None):
                c = cluster if c is None else c
                if len(c) < 2:
                    return
                # Keep the smallest non-negative uid; else the smallest uid.
                positives = [r for r in c if r["uid"] >= 0]
                keep = min(positives or c, key=lambda r: r["uid"])
                doomed.extend(r["id"] for r in c if r["id"] != keep["id"])

            for r in bucket:
                if cluster and abs(_epoch(r["date"]) - _epoch(cluster[0]["date"])) > 120:
                    flush()
                    cluster = []
                cluster.append(r)
            flush()
        if doomed:
            self.conn.executemany("DELETE FROM messages WHERE id=?",
                                  [(i,) for i in doomed])
            log.info("dedupe: removed %d duplicate Sent row(s)", len(doomed))
        self.conn.execute("PRAGMA user_version=4")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        """Add *column* to *table* if it is absent (PRAGMA table_info check + ALTER TABLE)."""
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {r["name"] for r in rows}
        if column not in existing:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self.conn.commit()

    # -- accounts --
    def add_account(self, a: Account) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO accounts (name, email, provider, imap_host, imap_port,"
                " smtp_host, smtp_port, color, sync_depth) VALUES (?,?,?,?,?,?,?,?,?)",
                (a.name, a.email, a.provider, a.imap_host, a.imap_port,
                 a.smtp_host, a.smtp_port, a.color, a.sync_depth or "50"))
            self.conn.commit()
            return cur.lastrowid

    def delete_account(self, account_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.conn.commit()

    def delete_account_cascade(self, account_id: int) -> None:
        """Delete all data for account_id: messages, pending_ops, outbox, then the account."""
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM pending_ops WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM outbox WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM folder_state WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM account_folders WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM folder_rules WHERE account_id=?", (account_id,))
            self.conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self.conn.commit()

    def list_accounts(self) -> list[Account]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM accounts ORDER BY provider, email").fetchall()
        return [self._row_to_account(r) for r in rows]

    def get_blocklist(self) -> set[str]:
        """License-wide blocked emails (lowercased), pushed by the fleet
        heartbeat and cached in QSettings('AMail','ui')['fleet_blocklist'].
        Read here (not via fleet.py) so the sync engine/worker enforce the
        blocklist without importing GUI/network modules. Never raises."""
        from PySide6.QtCore import QSettings
        import json
        try:
            raw = QSettings("AMail", "ui").value("fleet_blocklist") or "[]"
            return {str(e).lower() for e in json.loads(str(raw))}
        except Exception:
            return set()

    @staticmethod
    def _row_to_account(r) -> Account:
        # Keep only columns the Account dataclass declares, so DB-only bookkeeping
        # columns (e.g. cc_backfill_done) never break the mapping.
        fields = Account.__dataclass_fields__
        d = {k: v for k, v in dict(r).items() if k in fields}
        d["paused"] = bool(d["paused"])
        d["attachments_only"] = bool(d["attachments_only"])
        d["notify_enabled"] = bool(d["notify_enabled"])
        return Account(**d)

    def update_account(self, account_id: int, name: str | None = None,
                       signature: str | None = None,
                       signature_image: str | None = None,
                       signature_image_width: int | None = None,
                       sync_depth: str | None = None,
                       paused: bool | None = None,
                       signature_html: str | None = None,
                       imap_host: str | None = None,
                       imap_port: int | None = None,
                       smtp_host: str | None = None,
                       smtp_port: int | None = None,
                       attachments_only: bool | None = None,
                       notify_enabled: bool | None = None,
                       color: str | None = None) -> None:
        text_updates = {"name": name, "signature": signature,
                        "signature_image": signature_image,
                        "sync_depth": sync_depth,
                        "signature_html": signature_html,
                        "imap_host": imap_host, "smtp_host": smtp_host,
                        "color": color}
        int_updates = {"signature_image_width": signature_image_width,
                       "imap_port": imap_port, "smtp_port": smtp_port}
        bool_updates = {"paused": paused, "attachments_only": attachments_only,
                        "notify_enabled": notify_enabled}
        with self._lock:
            for col, val in text_updates.items():
                if val is not None:
                    self.conn.execute(f"UPDATE accounts SET {col}=? WHERE id=?",
                                      (val, account_id))
            for col, val in int_updates.items():
                if val is not None:
                    self.conn.execute(f"UPDATE accounts SET {col}=? WHERE id=?",
                                      (int(val), account_id))
            for col, val in bool_updates.items():
                if val is not None:
                    self.conn.execute(f"UPDATE accounts SET {col}=? WHERE id=?",
                                      (int(val), account_id))
            self.conn.commit()

    # -- groups --
    def add_group(self, name: str, color: str = "#8A6D1F", icon: str = "📁") -> int:
        """Insert a new group; raises sqlite3.IntegrityError on duplicate name."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO groups (name, color, icon) VALUES (?,?,?)",
                (name, color, icon))
            self.conn.commit()
            return cur.lastrowid

    def list_groups(self) -> list[sqlite3.Row]:
        """Return all groups ordered by sort_order, name."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM groups ORDER BY sort_order ASC, name ASC").fetchall()
        return list(rows)

    def rename_group(self, gid: int, name: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE groups SET name=? WHERE id=?", (name, gid))
            self.conn.commit()

    def set_group_icon(self, gid: int, icon: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE groups SET icon=? WHERE id=?", (icon, gid))
            self.conn.commit()

    def set_group_color(self, gid: int, color: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE groups SET color=? WHERE id=?",
                              (color, gid))
            self.conn.commit()

    def set_group_notify(self, gid: int, enabled: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE groups SET notify_enabled=? WHERE id=?",
                              (1 if enabled else 0, gid))
            self.conn.commit()

    def group_notify_enabled(self, gid: int) -> bool:
        """Whether notifications are on for this group (default True)."""
        with self._lock:
            row = self.conn.execute(
                "SELECT notify_enabled FROM groups WHERE id=?", (gid,)).fetchone()
        return bool(row["notify_enabled"]) if row is not None else True

    def delete_group(self, gid: int) -> None:
        """Delete the group and ungroup any accounts that belonged to it."""
        with self._lock:
            self.conn.execute("UPDATE accounts SET group_id=NULL WHERE group_id=?", (gid,))
            self.conn.execute("DELETE FROM groups WHERE id=?", (gid,))
            self.conn.commit()

    def set_account_group(self, account_id: int, gid_or_none: int | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE accounts SET group_id=? WHERE id=?", (gid_or_none, account_id))
            self.conn.commit()

    def accounts_in_group(self, gid: int) -> list[int]:
        """Return list of account ids belonging to the given group."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM accounts WHERE group_id=? ORDER BY id ASC", (gid,)).fetchall()
        return [r["id"] for r in rows]

    # -- messages --
    def save_message(self, m: EmailMessage) -> bool:
        """Insert; return False if we already hold this mail.

        "Already hold" means the same (account, folder, uid) OR — when the mail
        carries a Message-ID — the same mail under a different UID. One sent
        email otherwise landed in Sent three times: our local copy (negative
        uid), the provider's own auto-saved copy, and our IMAP APPEND. The
        server copy wins over the local one, since only it can be acted on.
        """
        with self._lock:
            if m.message_id:
                twin = self.conn.execute(
                    "SELECT id, uid, attachments FROM messages"
                    " WHERE account_id=? AND folder=?"
                    " AND message_id=?",
                    (m.account_id, m.folder, m.message_id)).fetchone()
                if twin is not None:
                    if twin["uid"] == m.uid:
                        return False              # exact same row
                    if twin["uid"] < 0 <= m.uid:
                        # Local placeholder gives way to the real server copy,
                        # keeping the row id so anything holding it still works.
                        # An empty attachment list from the server copy never
                        # wins: whatever the parser made of the mail, the files
                        # we recorded when sending it were really attached, and
                        # a Sent copy that loses them looks like mail that went
                        # out without them.
                        attachments = m.attachments or twin["attachments"]
                        self.conn.execute(
                            "UPDATE messages SET uid=?, date=?, attachments=?"
                            " WHERE id=?",
                            (m.uid, m.date, attachments, twin["id"]))
                        self.conn.commit()
                        m.id = twin["id"]
                        return False   # not new to the user — it was already shown
                    return False                  # duplicate copy — ignore
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO messages (account_id, folder, uid, subject,"
                " sender, recipients, cc, bcc, date, body_text, body_html, is_read,"
                " attachments, message_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m.account_id, m.folder, m.uid, m.subject, m.sender, m.recipients,
                 m.cc, m.bcc, m.date, m.body_text, m.body_html, int(m.is_read),
                 m.attachments, m.message_id))
            self.conn.commit()
            if cur.rowcount == 1:
                m.id = cur.lastrowid
                return True
            return False

    def _row_to_msg(self, r) -> EmailMessage:
        d = dict(r)
        d["is_read"] = bool(d["is_read"])
        d["flagged"] = bool(d.get("flagged", 0))
        d["pinned"] = bool(d.get("pinned", 0))
        d["flag_done"] = bool(d.get("flag_done", 0))
        return EmailMessage(**d)

    _ORDER_SQL = {
        "newest": "date DESC",
        "oldest": "date ASC",
        "unread": "is_read ASC, date DESC",
        "sender": "sender COLLATE NOCASE ASC, date DESC",
    }

    def list_messages(self, account_id: int | None = None,
                      folder: str = "INBOX", limit: int = 500,
                      order: str = "newest",
                      unread_only: bool = False,
                      account_ids: list[int] | None = None,
                      date_from: str | None = None,
                      date_to: str | None = None,
                      offset: int = 0,
                      flagged_only: bool = False) -> list[EmailMessage]:
        # Pinned rows always float to the very top, regardless of the sort order.
        order_by = "pinned DESC, " + self._ORDER_SQL.get(order, self._ORDER_SQL["newest"])
        sql = "SELECT * FROM messages WHERE folder=?"
        params: list = [folder]
        if account_id is not None:
            # account_id wins when both are given
            sql += " AND account_id=?"
            params.append(account_id)
        elif account_ids is not None:
            if len(account_ids) == 0:
                return []
            placeholders = ",".join("?" * len(account_ids))
            sql += f" AND account_id IN ({placeholders})"
            params.extend(account_ids)
        if unread_only:
            sql += " AND is_read=0"
        if flagged_only:
            sql += " AND flagged=1"
        # Date filter: stored dates are UTC ISO strings, so we compare against
        # UTC day boundaries ("YYYY-MM-DD" → 00:00:00 / 23:59:59 UTC). Day
        # boundaries in UTC (not the user's local tz) are acceptable here.
        if date_from is not None:
            sql += " AND date >= ?"
            params.append(date_from + "T00:00:00+00:00")
        if date_to is not None:
            sql += " AND date <= ?"
            params.append(date_to + "T23:59:59+00:00")
        sql += f" ORDER BY {order_by} LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def set_flagged(self, msg_id: int, flagged: bool) -> None:
        with self._lock:
            if flagged:
                self.conn.execute("UPDATE messages SET flagged=1 WHERE id=?",
                                  (msg_id,))
            else:
                # Clearing the flag also clears any 'completed' state, so the
                # row returns to a plain unflagged message (Outlook behaviour).
                self.conn.execute(
                    "UPDATE messages SET flagged=0, flag_done=0 WHERE id=?",
                    (msg_id,))
            self.conn.commit()

    def set_flag_done(self, msg_id: int, done: bool) -> None:
        """Mark a follow-up complete (green ✓) or reopen it. Completing also
        sets `flagged` so the item stays in the flag system, shown as a check."""
        with self._lock:
            if done:
                self.conn.execute(
                    "UPDATE messages SET flagged=1, flag_done=1 WHERE id=?",
                    (msg_id,))
            else:
                self.conn.execute(
                    "UPDATE messages SET flag_done=0 WHERE id=?", (msg_id,))
            self.conn.commit()

    def set_pinned(self, msg_id: int, pinned: bool) -> None:
        with self._lock:
            self.conn.execute("UPDATE messages SET pinned=? WHERE id=?",
                              (int(pinned), msg_id))
            self.conn.commit()

    def get_message(self, msg_id: int) -> EmailMessage | None:
        with self._lock:
            r = self.conn.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        return self._row_to_msg(r) if r else None

    def mark_read(self, msg_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE messages SET is_read=1 WHERE id=?", (msg_id,))
            self.conn.commit()

    def mark_all_read(self, account_ids: list[int] | None = None,
                      folder: str | None = None) -> int:
        """Mark every unread message read, optionally scoped to *account_ids*
        and/or *folder*. Returns how many were updated."""
        sql = "UPDATE messages SET is_read=1 WHERE is_read=0"
        params: list = []
        if account_ids is not None:
            if not account_ids:
                return 0
            sql += " AND account_id IN (%s)" % ",".join("?" * len(account_ids))
            params.extend(account_ids)
        if folder:
            sql += " AND folder=?"
            params.append(folder)
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.rowcount

    def mark_unread(self, msg_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE messages SET is_read=0 WHERE id=?", (msg_id,))
            self.conn.commit()

    def unread_count(self, account_id: int) -> int:
        with self._lock:
            r = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE account_id=? AND is_read=0 AND folder='INBOX'",
                (account_id,)).fetchone()
        return r[0]

    def total_count(self, account_id: int, folder: str = "INBOX") -> int:
        with self._lock:
            r = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE account_id=? AND folder=?",
                (account_id, folder)).fetchone()
        return r[0]

    def list_account_folders(self, account_id: int) -> list[str]:
        """Custom folders for this account: persisted ones (server-discovered or
        user-created, incl. empty) unioned with any folder that has synced mail —
        everything except the built-in Inbox/Sent/Trash/Spam."""
        builtins = {"INBOX", "SENT", "TRASH", "SPAM"}
        with self._lock:
            persisted = self.conn.execute(
                "SELECT name FROM account_folders WHERE account_id=?",
                (account_id,)).fetchall()
            with_mail = self.conn.execute(
                "SELECT DISTINCT folder FROM messages WHERE account_id=? "
                "AND folder NOT IN ('INBOX','SENT','TRASH','SPAM')",
                (account_id,)).fetchall()
        names = {r["name"] for r in persisted} | {r["folder"] for r in with_mail}
        names -= builtins
        return sorted(names, key=str.lower)

    def add_account_folder(self, account_id: int, name: str) -> None:
        if not name:
            return
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO account_folders (account_id, name) VALUES (?,?)",
                (account_id, name))
            self.conn.commit()

    def remove_account_folder(self, account_id: int, name: str) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM account_folders WHERE account_id=? AND name=?",
                (account_id, name))
            self.conn.commit()

    def account_folder_tree(self, account_id: int, name: str,
                            delim: str = "/") -> list[str]:
        """The folder *name* plus every descendant (rows starting with
        ``name + delim``), deepest first — so callers can delete children
        before their parent on the server."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT name FROM account_folders WHERE account_id=? "
                "AND (name=? OR name LIKE ?)",
                (account_id, name, name + delim + "%")).fetchall()
        names = [r["name"] for r in rows]
        if name not in names:
            names.append(name)
        # Deepest paths first (more delimiters = deeper).
        return sorted(names, key=lambda n: n.count(delim), reverse=True)

    def counts_by_account(self) -> dict[int, tuple[int, int]]:
        """Return {account_id: (unread, total)} for INBOX in a single query.

        One aggregate query replaces 2N per-account unread_count/total_count
        calls during sidebar refreshes — critical with 50+ accounts.
        Accounts with no INBOX messages are simply absent; callers should
        default missing ids to (0, 0).
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT account_id,"
                " SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) AS unread,"
                " COUNT(*) AS total"
                " FROM messages WHERE folder='INBOX' GROUP BY account_id"
            ).fetchall()
        return {r["account_id"]: (r["unread"], r["total"]) for r in rows}

    def counts_by_account_folder(self) -> dict[tuple[int, str], tuple[int, int]]:
        """Return {(account_id, folder): (unread, total)} in a single query —
        feeds the per-account folder rows in the sidebar."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT account_id, folder,"
                " SUM(CASE WHEN is_read=0 THEN 1 ELSE 0 END) AS unread,"
                " COUNT(*) AS total"
                " FROM messages GROUP BY account_id, folder"
            ).fetchall()
        return {(r["account_id"], r["folder"]): (r["unread"], r["total"])
                for r in rows}

    def last_uid(self, account_id: int, folder: str) -> int:
        with self._lock:
            r = self.conn.execute(
                "SELECT COALESCE(MAX(uid), 0) FROM messages WHERE account_id=? AND folder=?",
                (account_id, folder)).fetchone()
        return r[0]

    # -- folder_state (sync watermarks, independent of locally stored messages) --
    def get_folder_state(self, account_id: int, folder: str) -> tuple[int, int]:
        """Return (uidvalidity, last_seen_uid) for the folder; (0, 0) if never synced."""
        with self._lock:
            r = self.conn.execute(
                "SELECT uidvalidity, last_seen_uid FROM folder_state"
                " WHERE account_id=? AND folder=?", (account_id, folder)).fetchone()
        return (r["uidvalidity"], r["last_seen_uid"]) if r else (0, 0)

    def set_folder_state(self, account_id: int, folder: str,
                         uidvalidity: int, last_seen_uid: int) -> None:
        with self._lock:
            # UPSERT (not INSERT OR REPLACE) so backfill_done survives watermark updates
            self.conn.execute(
                "INSERT INTO folder_state"
                " (account_id, folder, uidvalidity, last_seen_uid) VALUES (?,?,?,?)"
                " ON CONFLICT(account_id, folder) DO UPDATE SET"
                " uidvalidity=excluded.uidvalidity, last_seen_uid=excluded.last_seen_uid",
                (account_id, folder, uidvalidity, last_seen_uid))
            self.conn.commit()

    def reset_folder_state(self, account_id: int) -> None:
        """Delete all folder_state rows for the account so the next sync backfills."""
        with self._lock:
            self.conn.execute("DELETE FROM folder_state WHERE account_id=?", (account_id,))
            self.conn.commit()

    def is_backfill_done(self, account_id: int, folder: str) -> bool:
        """True once the progressive 'auto' backfill has fetched all history."""
        with self._lock:
            r = self.conn.execute(
                "SELECT backfill_done FROM folder_state WHERE account_id=? AND folder=?",
                (account_id, folder)).fetchone()
        return bool(r["backfill_done"]) if r else False

    def set_backfill_done(self, account_id: int, folder: str, done: bool = True) -> None:
        """Set the backfill flag, creating the folder_state row if absent."""
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO folder_state (account_id, folder) VALUES (?,?)",
                (account_id, folder))
            self.conn.execute(
                "UPDATE folder_state SET backfill_done=? WHERE account_id=? AND folder=?",
                (int(done), account_id, folder))
            self.conn.commit()

    # -- one-time Cc/Bcc header backfill for pre-cc-parsing messages --
    def cc_backfill_done(self, account_id: int) -> bool:
        with self._lock:
            r = self.conn.execute(
                "SELECT cc_backfill_done FROM accounts WHERE id=?",
                (account_id,)).fetchone()
        return bool(r["cc_backfill_done"]) if r else False

    def set_cc_backfill_done(self, account_id: int, done: bool = True) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE accounts SET cc_backfill_done=? WHERE id=?",
                (int(done), account_id))
            self.conn.commit()

    def messages_missing_cc(self, account_id: int) -> dict[str, list[int]]:
        """{folder: [uid, ...]} for real (uid>0) messages whose cc is empty —
        candidates for a Cc/Bcc header re-fetch."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT folder, uid FROM messages"
                " WHERE account_id=? AND uid > 0 AND (cc IS NULL OR cc='')",
                (account_id,)).fetchall()
        out: dict[str, list[int]] = {}
        for r in rows:
            out.setdefault(r["folder"], []).append(r["uid"])
        return out

    def update_message_headers(self, account_id: int, folder: str, uid: int,
                               cc: str, bcc: str) -> None:
        """Fill in cc/bcc for an already-stored message (backfill only)."""
        with self._lock:
            self.conn.execute(
                "UPDATE messages SET cc=?, bcc=?"
                " WHERE account_id=? AND folder=? AND uid=?",
                (cc, bcc, account_id, folder, uid))
            self.conn.commit()

    def oldest_uid(self, account_id: int, folder: str) -> int:
        """Lowest stored UID (>0) for the folder; 0 if no messages stored."""
        with self._lock:
            r = self.conn.execute(
                "SELECT COALESCE(MIN(uid), 0) FROM messages"
                " WHERE account_id=? AND folder=? AND uid > 0",
                (account_id, folder)).fetchone()
        return r[0]

    def bump_last_seen(self, account_id: int, folder: str, uid: int) -> None:
        """Raise the watermark to *uid* if higher; never lowers it."""
        with self._lock:
            self.conn.execute(
                "UPDATE folder_state SET last_seen_uid = MAX(last_seen_uid, ?)"
                " WHERE account_id=? AND folder=?", (uid, account_id, folder))
            self.conn.commit()

    def move_message_local(self, msg_id: int, folder: str) -> None:
        with self._lock:
            try:
                self.conn.execute("UPDATE messages SET folder=? WHERE id=?", (folder, msg_id))
                self.conn.commit()
            except sqlite3.IntegrityError:
                # A row with the same (account_id, folder, uid) already exists in the
                # target folder — the server-side copy is already mirrored locally.
                # Delete the source row so the UI stays consistent.
                self.conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
                self.conn.commit()

    def delete_message_local(self, msg_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM messages WHERE id=?", (msg_id,))
            self.conn.commit()

    def search_messages(self, query: str, account_id: int | None = None,
                        folder: str | None = None,
                        account_ids: list[int] | None = None,
                        date_from: str | None = None,
                        date_to: str | None = None) -> list[EmailMessage]:
        # Escape LIKE special chars in the query
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        params: list = [pattern, pattern, pattern]
        sql = ("SELECT * FROM messages WHERE"
               " (subject LIKE ? ESCAPE '\\' OR sender LIKE ? ESCAPE '\\'"
               " OR body_text LIKE ? ESCAPE '\\')")
        if account_id is not None:
            sql += " AND account_id=?"
            params.append(account_id)
        elif account_ids is not None:
            placeholders = ",".join("?" * len(account_ids))
            sql += f" AND account_id IN ({placeholders})"
            params.extend(account_ids)
        if folder is not None:
            sql += " AND folder=?"
            params.append(folder)
        # Date filter: UTC day boundaries (see list_messages for rationale).
        if date_from is not None:
            sql += " AND date >= ?"
            params.append(date_from + "T00:00:00+00:00")
        if date_to is not None:
            sql += " AND date <= ?"
            params.append(date_to + "T23:59:59+00:00")
        sql += " ORDER BY date DESC LIMIT 300"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_msg(r) for r in rows]

    def known_addresses(self, limit: int = 500) -> list[str]:
        """Every address seen in stored mail + outbox, for recipient autocomplete.

        Parses raw To/From/Cc/Bcc header strings with email.utils.getaddresses,
        dedupes case-insensitively on the bare address, prefers a
        "Name <addr>" display form when any occurrence carried a name
        (first-seen name wins), skips entries without an '@', and returns the
        list ordered by frequency DESC then alphabetically, capped at *limit*.
        Not cached here — callers (the compose UI) cache per dialog.
        """
        with self._lock:
            msg_rows = self.conn.execute(
                "SELECT sender, recipients FROM messages").fetchall()
            out_rows = self.conn.execute(
                "SELECT to_addrs, cc, bcc FROM outbox").fetchall()
        raw: list[str] = []
        for r in msg_rows:
            raw.extend((r["sender"], r["recipients"]))
        for r in out_rows:
            raw.extend((r["to_addrs"], r["cc"], r["bcc"]))
        counts: dict[str, int] = {}
        display: dict[str, str] = {}
        named: set[str] = set()
        for name, addr in getaddresses([s for s in raw if s]):
            addr = addr.strip()
            if "@" not in addr:
                continue
            key = addr.lower()
            counts[key] = counts.get(key, 0) + 1
            name = name.strip()
            if key not in display:
                display[key] = f"{name} <{addr}>" if name else addr
                if name:
                    named.add(key)
            elif name and key not in named:
                # Upgrade a bare address to the first name form we encounter.
                display[key] = f"{name} <{addr}>"
                named.add(key)
        ordered = sorted(display,
                         key=lambda k: (-counts[k], display[k].lower()))
        return [display[k] for k in ordered[:limit]]

    # -- outbox --
    def queue_outbox(self, account_id: int, to_addrs: str, cc: str, bcc: str,
                     subject: str, body: str, attachments: str,
                     body_html: str = "", delay_seconds: int = 0) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        send_after = time.time() + max(0, delay_seconds)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO outbox (account_id, to_addrs, cc, bcc, subject, body,"
                " attachments, created_at, body_html, send_after)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (account_id, to_addrs, cc, bcc, subject, body, attachments, created_at,
                 body_html, send_after))
            self.conn.commit()
            return cur.lastrowid

    def list_outbox(self, limit: int = 200) -> list[sqlite3.Row]:
        """Mail on its way out. Drafts are deliberately excluded — they are
        messages the user is still writing, and they have their own folder."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM outbox WHERE status!='draft'"
                " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return list(rows)

    def list_drafts(self, limit: int = 200) -> list[sqlite3.Row]:
        """Unsent messages the user closed without sending, newest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM outbox WHERE status='draft'"
                " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return list(rows)

    def draft_count(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE status='draft'").fetchone()[0]

    def save_draft(self, outbox_id: int | None, account_id: int, to_addrs: str,
                   cc: str, bcc: str, subject: str, body: str,
                   attachments: str, body_html: str = "") -> int:
        """Create or update a draft; returns its outbox id.

        Closing a half-written message must never lose it, so this is called
        both on a timer while composing and when the window closes.
        """
        with self._lock:
            if outbox_id is not None:
                cur = self.conn.execute(
                    "UPDATE outbox SET account_id=?, to_addrs=?, cc=?, bcc=?,"
                    " subject=?, body=?, attachments=?, body_html=?,"
                    " status='draft', error='' WHERE id=?",
                    (account_id, to_addrs, cc, bcc, subject, body, attachments,
                     body_html, outbox_id))
                self.conn.commit()
                if cur.rowcount:
                    return outbox_id
            created_at = datetime.now(timezone.utc).isoformat()
            cur = self.conn.execute(
                "INSERT INTO outbox (account_id, to_addrs, cc, bcc, subject,"
                " body, attachments, created_at, body_html, status, send_after)"
                " VALUES (?,?,?,?,?,?,?,?,?,'draft',0)",
                (account_id, to_addrs, cc, bcc, subject, body, attachments,
                 created_at, body_html))
            self.conn.commit()
            return cur.lastrowid

    def claim_next_outbox(self) -> sqlite3.Row | None:
        """Atomically transition the oldest due queued row to 'sending'; return it
        or None. Rows still inside their Undo-Send window (send_after > now) are
        skipped until the window elapses."""
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outbox WHERE status='queued' AND send_after<=?"
                " ORDER BY created_at ASC LIMIT 1", (time.time(),)
            ).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE outbox SET status='sending' WHERE id=?", (row["id"],))
            self.conn.commit()
            # Re-fetch so the returned row reflects updated status
            row = self.conn.execute(
                "SELECT * FROM outbox WHERE id=?", (row["id"],)).fetchone()
        return row

    def finish_outbox(self, outbox_id: int, ok: bool, error: str = "") -> None:
        status = "sent" if ok else "failed"
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET status=?, error=? WHERE id=?",
                (status, error, outbox_id))
            self.conn.commit()

    def mark_outbox_awaiting(self, outbox_id: int, approval_id: str) -> None:
        """Email was sent to the admin approval queue instead of SMTP."""
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET status='awaiting_approval', error='',"
                " approval_id=? WHERE id=?", (approval_id, outbox_id))
            self.conn.commit()

    def outbox_by_approval(self, approval_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM outbox WHERE approval_id=? LIMIT 1",
                (approval_id,)).fetchone()

    def awaiting_approval_outbox(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute(
                "SELECT * FROM outbox WHERE status='awaiting_approval'").fetchall())

    def recover_stale_outbox(self) -> None:
        """Reset any rows stuck in 'sending' (from a previous crash) back to 'queued'."""
        with self._lock:
            self.conn.execute("UPDATE outbox SET status='queued' WHERE status='sending'")
            self.conn.commit()

    def requeue_outbox(self, outbox_id: int) -> None:
        """Transition a failed outbox row back to queued, clearing the error.

        send_direct is reset so a retried approval email goes through the
        approval gate again rather than sending straight out.
        """
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET status='queued', error='', send_direct=0"
                " WHERE id=?", (outbox_id,))
            self.conn.commit()

    def release_outbox_to_sender(self, outbox_id: int) -> None:
        """Admin approved this in 'send from sender' mode: re-queue it to send
        straight from this device (bypassing the approval gate). The approval_id
        is kept so the worker can tell the server the email was sent."""
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET status='queued', error='', send_direct=1"
                " WHERE id=?", (outbox_id,))
            self.conn.commit()

    def delete_outbox(self, outbox_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM outbox WHERE id=?", (outbox_id,))
            self.conn.commit()

    def get_outbox(self, outbox_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()

    def hold_outbox(self, outbox_id: int) -> bool:
        """Park an editable outbox row as a 'draft' so the worker won't send it.

        Returns True only if the row was still editable (queued/failed/draft);
        False means it was already claimed/sent/awaiting approval — caller should
        abort the edit. The conditional UPDATE makes this atomic against the
        outbox worker claiming a queued row mid-edit.
        """
        with self._lock:
            cur = self.conn.execute(
                "UPDATE outbox SET status='draft' WHERE id=?"
                " AND status IN ('queued','failed','draft')", (outbox_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def restore_outbox(self, outbox_id: int, status: str) -> None:
        """Put a held draft back to its prior status (used when an edit is cancelled)."""
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET status=? WHERE id=? AND status='draft'",
                (status, outbox_id))
            self.conn.commit()

    def update_outbox(self, outbox_id: int, account_id: int, to_addrs: str,
                      cc: str, bcc: str, subject: str, body: str,
                      attachments: str, body_html: str = "",
                      delay_seconds: int = 0) -> None:
        """Save edits to an outbox row and re-queue it for sending.

        Status goes back to 'queued' and any prior error/approval link is cleared
        so a rejected or failed email goes through the send (and approval) path
        again as if freshly composed. delay_seconds re-arms the Undo-Send window.
        """
        send_after = time.time() + max(0, delay_seconds)
        with self._lock:
            self.conn.execute(
                "UPDATE outbox SET account_id=?, to_addrs=?, cc=?, bcc=?,"
                " subject=?, body=?, attachments=?, body_html=?,"
                " status='queued', error='', approval_id='', send_direct=0,"
                " send_after=? WHERE id=?",
                (account_id, to_addrs, cc, bcc, subject, body, attachments,
                 body_html, send_after, outbox_id))
            self.conn.commit()

    # -- pending_ops --
    def add_op(self, account_id: int, op: str, folder: str, uid: int,
               target: str = "") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO pending_ops (account_id, op, folder, uid, target)"
                " VALUES (?,?,?,?,?)",
                (account_id, op, folder, uid, target))
            self.conn.commit()

    def take_ops(self, account_id: int) -> list[dict]:
        """Return and delete all pending ops for account_id atomically."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, op, folder, uid, target FROM pending_ops"
                " WHERE account_id=? ORDER BY id ASC", (account_id,)).fetchall()
            if rows:
                ids = [r["id"] for r in rows]
                placeholders = ",".join("?" * len(ids))
                self.conn.execute(
                    f"DELETE FROM pending_ops WHERE id IN ({placeholders})", ids)
                self.conn.commit()
        return [{"op": r["op"], "folder": r["folder"], "uid": r["uid"],
                 "target": r["target"]} for r in rows]

    # -- folder rules (filters) --
    def add_folder_rule(self, account_id: int, match_from: str, folder: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO folder_rules (account_id, match_from, folder)"
                " VALUES (?,?,?)", (account_id, match_from.strip(), folder))
            self.conn.commit()

    def list_folder_rules(self, account_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, match_from, folder FROM folder_rules WHERE account_id=?"
                " ORDER BY id", (account_id,)).fetchall()
        return [{"id": r["id"], "match_from": r["match_from"],
                 "folder": r["folder"]} for r in rows]

    def delete_folder_rule(self, rule_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM folder_rules WHERE id=?", (rule_id,))
            self.conn.commit()

    # -- notifications --
    def add_notification(self, message_id: int | None, title: str, body: str) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT INTO notifications (message_id, title, body, created_at)"
                " VALUES (?,?,?,?)",
                (message_id, title, body, created_at))
            self.conn.commit()

    def list_notifications(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return list(rows)

    def clear_notifications(self) -> None:
        """Delete the entire notification history."""
        with self._lock:
            self.conn.execute("DELETE FROM notifications")
            self.conn.commit()

    def mark_notification_read(self, notif_id: int) -> None:
        with self._lock:
            self.conn.execute("UPDATE notifications SET is_read=1 WHERE id=?",
                              (notif_id,))
            self.conn.commit()

    def mark_all_notifications_read(self) -> None:
        with self._lock:
            self.conn.execute("UPDATE notifications SET is_read=1")
            self.conn.commit()

    def mark_notifications_read_for_message(self, message_id: int) -> None:
        """Reading the email anywhere in-app marks its notification read too."""
        with self._lock:
            self.conn.execute(
                "UPDATE notifications SET is_read=1 WHERE message_id=?",
                (message_id,))
            self.conn.commit()

    def unread_notification_count(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM notifications WHERE is_read=0").fetchone()[0]
