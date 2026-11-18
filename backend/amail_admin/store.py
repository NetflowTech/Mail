"""The backend's SQLite database.

One connection behind a lock: with around ten PCs checking in every 30
seconds, a lock costs nothing and rules out every concurrency surprise.
Mailbox passwords are stored only as AES-GCM ciphertext (see crypto.py).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    os           TEXT NOT NULL DEFAULT '',
    public_key   BLOB NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | revoked
    created_at   REAL NOT NULL,
    approved_at  REAL,
    last_seen    REAL,
    last_ip      TEXT NOT NULL DEFAULT '',
    app_version  TEXT NOT NULL DEFAULT '',
    report       TEXT NOT NULL DEFAULT '[]'          -- last check-in's account list
);
CREATE TABLE IF NOT EXISTS mailboxes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    imap_host    TEXT NOT NULL,
    imap_port    INTEGER NOT NULL DEFAULT 993,
    smtp_host    TEXT NOT NULL,
    smtp_port    INTEGER NOT NULL DEFAULT 465,
    password_enc BLOB NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,      -- bumped on any change PCs need
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    device_id    TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    mailbox_id   INTEGER NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    created_at   REAL NOT NULL,
    PRIMARY KEY (device_id, mailbox_id)
);
CREATE TABLE IF NOT EXISTS admins (
    username     TEXT PRIMARY KEY COLLATE NOCASE,
    pw_hash      TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,                -- SHA-256 of the cookie's token
    username     TEXT NOT NULL,
    created_at   REAL NOT NULL,
    last_seen    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           REAL NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    def __init__(self, path: Path | str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._ensure_column("devices", "prev_token_hash", "TEXT")
            self._ensure_column("devices", "token_issued_at", "REAL")
            self._ensure_column("admins", "totp_secret", "TEXT NOT NULL DEFAULT ''")
            # password | google | microsoft: what password_enc holds (the
            # mailbox password, or a Google / Microsoft refresh token).
            self._ensure_column("mailboxes", "auth",
                                "TEXT NOT NULL DEFAULT 'password'")
            self._ensure_column("admins", "totp_last_step",
                                "INTEGER NOT NULL DEFAULT 0")

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    # ----- settings -----
    def get_setting(self, key: str, default: str = "") -> str:
        rows = self._q("SELECT value FROM settings WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self._x("INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ----- audit -----
    def audit(self, actor: str, action: str, detail: str = "") -> None:
        self._x("INSERT INTO audit(at, actor, action, detail) VALUES(?,?,?,?)",
                (time.time(), actor, action, detail))

    def audit_log(self, limit: int = 300) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    def audit_count(self, action: str, since: float) -> int:
        return self._q("SELECT COUNT(*) AS n FROM audit WHERE action=? AND at>=?",
                       (action, since))[0]["n"]

    def prune_audit(self, older_than: float) -> None:
        self._x("DELETE FROM audit WHERE at < ?", (older_than,))

    # ----- admins -----
    def admin_count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM admins")[0]["n"]

    def get_admin(self, username: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM admins WHERE username=?", (username,))
        return rows[0] if rows else None

    def set_admin(self, username: str, pw_hash: str) -> None:
        self._x("INSERT INTO admins(username, pw_hash, created_at) VALUES(?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET pw_hash=excluded.pw_hash",
                (username, pw_hash, time.time()))
        # A new password ends every session that admin had open.
        self.end_sessions(username)

    def set_totp(self, username: str, secret: str, last_step: int = 0) -> None:
        self._x("UPDATE admins SET totp_secret=?, totp_last_step=? WHERE username=?",
                (secret, last_step, username))

    def use_totp_step(self, username: str, step: int) -> bool:
        """Record *step* as used; False if it (or a later one) already was."""
        cur = self._x("UPDATE admins SET totp_last_step=? "
                      "WHERE username=? AND totp_last_step < ?",
                      (step, username, step))
        return cur.rowcount == 1

    # ----- admin sessions (server side, so they can be ended) -----
    def add_session(self, sid_hash: str, username: str) -> None:
        now = time.time()
        self._x("INSERT INTO sessions(id, username, created_at, last_seen) "
                "VALUES(?,?,?,?)", (sid_hash, username, now, now))

    def session(self, sid_hash: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM sessions WHERE id=?", (sid_hash,))
        return rows[0] if rows else None

    def touch_session(self, sid_hash: str) -> None:
        self._x("UPDATE sessions SET last_seen=? WHERE id=?", (time.time(), sid_hash))

    def end_session(self, sid_hash: str) -> None:
        self._x("DELETE FROM sessions WHERE id=?", (sid_hash,))

    def end_sessions(self, username: str) -> None:
        self._x("DELETE FROM sessions WHERE username=?", (username,))

    def prune_sessions(self, idle: float, absolute: float) -> None:
        now = time.time()
        self._x("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                (now - idle, now - absolute))

    # ----- devices -----
    def add_device(self, device_id: str, name: str, os_name: str,
                   public_key: bytes, token_hash: str, ip: str) -> None:
        self._x("INSERT INTO devices(id, name, os, public_key, token_hash, "
                "status, created_at, last_seen, last_ip, token_issued_at) "
                "VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (device_id, name, os_name, public_key, token_hash,
                 time.time(), time.time(), ip, time.time()))

    def device_by_token(self, token_hash: str) -> sqlite3.Row | None:
        """The device whose current or previous token this is. The previous
        one stays valid until the PC has used its replacement once, so a lost
        check-in answer can't lock the PC out."""
        rows = self._q("SELECT * FROM devices WHERE token_hash=? "
                       "OR prev_token_hash=?", (token_hash, token_hash))
        return rows[0] if rows else None

    def rotate_token(self, device_id: str, new_hash: str) -> None:
        self._x("UPDATE devices SET prev_token_hash=token_hash, token_hash=?, "
                "token_issued_at=? WHERE id=?", (new_hash, time.time(), device_id))

    def retire_prev_token(self, device_id: str) -> None:
        self._x("UPDATE devices SET prev_token_hash=NULL WHERE id=?", (device_id,))

    def prune_pending(self, older_than: float) -> int:
        cur = self._x("DELETE FROM devices WHERE status='pending' AND created_at < ?",
                      (older_than,))
        return cur.rowcount

    def device(self, device_id: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM devices WHERE id=?", (device_id,))
        return rows[0] if rows else None

    def devices(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM devices ORDER BY status='revoked', "
                       "status='approved', name COLLATE NOCASE")

    def pending_count(self) -> int:
        return self._q("SELECT COUNT(*) AS n FROM devices "
                       "WHERE status='pending'")[0]["n"]

    def set_device_status(self, device_id: str, status: str) -> None:
        if status == "approved":
            self._x("UPDATE devices SET status=?, approved_at=? WHERE id=?",
                    (status, time.time(), device_id))
        else:
            self._x("UPDATE devices SET status=? WHERE id=?", (status, device_id))
        if status == "revoked":
            # A revoked PC keeps no grants: re-approving it starts from nothing.
            self._x("DELETE FROM grants WHERE device_id=?", (device_id,))

    def rename_device(self, device_id: str, name: str) -> None:
        self._x("UPDATE devices SET name=? WHERE id=?", (name, device_id))

    def delete_device(self, device_id: str) -> None:
        self._x("DELETE FROM devices WHERE id=?", (device_id,))

    def record_checkin(self, device_id: str, ip: str, app_version: str,
                       report: list[dict]) -> None:
        self._x("UPDATE devices SET last_seen=?, last_ip=?, app_version=?, "
                "report=? WHERE id=?",
                (time.time(), ip, app_version, json.dumps(report), device_id))

    # ----- mailboxes -----
    def mailboxes(self) -> list[sqlite3.Row]:
        return self._q("SELECT * FROM mailboxes ORDER BY email COLLATE NOCASE")

    def mailbox(self, mailbox_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM mailboxes WHERE id=?", (mailbox_id,))
        return rows[0] if rows else None

    def mailbox_by_email(self, email: str) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM mailboxes WHERE email=?", (email,))
        return rows[0] if rows else None

    def add_mailbox(self, email: str, display_name: str, imap_host: str,
                    imap_port: int, smtp_host: str, smtp_port: int,
                    password_enc: bytes, auth: str = "password") -> int:
        cur = self._x(
            "INSERT INTO mailboxes(email, display_name, imap_host, imap_port, "
            "smtp_host, smtp_port, password_enc, version, updated_at, auth) "
            "VALUES(?,?,?,?,?,?,?,1,?,?)",
            (email, display_name, imap_host, imap_port, smtp_host, smtp_port,
             password_enc, time.time(), auth))
        return cur.lastrowid

    def update_mailbox(self, mailbox_id: int, *, display_name: str,
                       imap_host: str, imap_port: int, smtp_host: str,
                       smtp_port: int, password_enc: bytes | None,
                       auth: str | None = None) -> None:
        sql = ("UPDATE mailboxes SET display_name=?, imap_host=?, imap_port=?, "
               "smtp_host=?, smtp_port=?, version=version+1, updated_at=?")
        args: list = [display_name, imap_host, imap_port, smtp_host, smtp_port,
                      time.time()]
        if password_enc is not None:
            sql += ", password_enc=?"
            args.append(password_enc)
        if auth is not None:
            sql += ", auth=?"
            args.append(auth)
        self._x(sql + " WHERE id=?", tuple(args + [mailbox_id]))

    def delete_mailbox(self, mailbox_id: int) -> None:
        self._x("DELETE FROM mailboxes WHERE id=?", (mailbox_id,))

    # ----- grants -----
    def grant(self, device_id: str, mailbox_id: int) -> None:
        self._x("INSERT OR IGNORE INTO grants(device_id, mailbox_id, created_at) "
                "VALUES(?,?,?)", (device_id, mailbox_id, time.time()))

    def revoke(self, device_id: str, mailbox_id: int) -> None:
        self._x("DELETE FROM grants WHERE device_id=? AND mailbox_id=?",
                (device_id, mailbox_id))

    def granted_mailboxes(self, device_id: str) -> list[sqlite3.Row]:
        return self._q(
            "SELECT m.* FROM mailboxes m JOIN grants g ON g.mailbox_id=m.id "
            "WHERE g.device_id=? ORDER BY m.email COLLATE NOCASE", (device_id,))

    def devices_for_mailbox(self, mailbox_id: int) -> list[sqlite3.Row]:
        return self._q(
            "SELECT d.* FROM devices d JOIN grants g ON g.device_id=d.id "
            "WHERE g.mailbox_id=? ORDER BY d.name COLLATE NOCASE", (mailbox_id,))
