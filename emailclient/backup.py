"""Automatic database backups.

Uses sqlite3's online backup API (``src_conn.backup(dst_conn)``) which is
WAL-safe: it takes a consistent snapshot even while the app's own connection
keeps writing. Backups land in ``data_dir()/backups/mail-YYYYmmdd-HHMM.db``
and the oldest beyond *keep* are pruned.

Passwords live in the macOS Keychain — they are never in these files.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

BACKUP_PREFIX = "mail-"
BACKUP_SUFFIX = ".db"


def _default_dest() -> str:
    """User-chosen backup folder, or <data_dir>/backups by default."""
    from .settings import BackupSettings
    return BackupSettings().folder


def _backup_files(dest_dir: Path) -> list[Path]:
    """Backup files in *dest_dir*, sorted oldest → newest (by name)."""
    if not dest_dir.is_dir():
        return []
    return sorted(p for p in dest_dir.iterdir()
                  if p.name.startswith(BACKUP_PREFIX)
                  and p.name.endswith(BACKUP_SUFFIX))


def run_backup(src_path=None, dest_dir=None, keep: int = 10) -> Path | None:
    """Snapshot the mail db into *dest_dir*; prune old backups beyond *keep*.

    Returns the new backup's path, or None on failure (logged, never raises).
    Opens its own sqlite connections, so it's safe to run from any thread
    alongside the app's WAL connection.
    """
    try:
        src_path = Path(src_path) if src_path else config.db_path()
        dest_dir = Path(dest_dir) if dest_dir else Path(_default_dest())
        if not src_path.exists():
            log.warning("backup: source db %s missing", src_path)
            return None
        dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dest_dir, 0o700)  # in case the dir pre-existed
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        dst_path = dest_dir / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
        # Pre-create owner-only so sqlite never leaves a world-readable gap
        # (backups contain full email bodies).
        fd = os.open(str(dst_path), os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        src_conn = sqlite3.connect(str(src_path))
        try:
            dst_conn = sqlite3.connect(str(dst_path))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        # Prune oldest beyond keep (names embed timestamps → name sort = time sort)
        files = _backup_files(dest_dir)
        for old in files[:-keep] if keep > 0 else files:
            try:
                old.unlink()
            except OSError:
                log.warning("backup: could not prune %s", old)
        return dst_path
    except Exception:
        log.warning("backup failed", exc_info=True)
        return None


def list_backups(dest_dir=None) -> list[dict]:
    """All backups newest → oldest as {"path", "when" (datetime), "label"}.

    The timestamp in each filename is the snapshot moment, i.e. the backup
    holds every email/setting up to that date — that's what we surface.
    """
    dest_dir = Path(dest_dir) if dest_dir else Path(_default_dest())
    out = []
    for p in reversed(_backup_files(dest_dir)):  # newest first
        stamp = p.name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
        try:
            dt = datetime.strptime(stamp, "%Y%m%d-%H%M")
        except ValueError:
            continue
        out.append({"path": p, "when": dt,
                    "label": dt.strftime("%d %b %Y %H:%M")})
    return out


def is_valid_backup(path) -> bool:
    """True if *path* is a readable mail backup (a SQLite db with the messages
    table). Guards the "restore from file" path so a wrong file the user picks
    can't overwrite — and brick — their live database. Never raises.
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return ("messages",) in rows


def restore_backup(backup_path, src_path=None) -> Path | None:
    """Replace the live db with *backup_path*, snapshotting the current db
    first so the restore is undoable.

    The caller MUST have closed the app's own sqlite connection beforehand,
    otherwise its WAL could replay over the restored file. Returns the
    safety-snapshot path (the pre-restore state). Raises on hard failure.
    """
    import shutil
    import tempfile
    backup_path = Path(backup_path)
    src_path = Path(src_path) if src_path else config.db_path()
    if not backup_path.exists():
        raise FileNotFoundError(backup_path)
    # 0. Copy the chosen backup aside first — the safety snapshot below prunes
    #    old backups, which could otherwise delete the very file we're about to
    #    restore (when it's the oldest and we're at the keep limit).
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=BACKUP_SUFFIX)
    os.close(tmp_fd)
    try:
        shutil.copyfile(backup_path, tmp_name)
        # 1. Safety snapshot of the current state (reads through sqlite, so it
        #    captures any WAL-only commits before we drop the sidecars).
        safety = run_backup(src_path=src_path)
        # 2. Remove stale WAL/SHM so they can't replay over the restore.
        for side in ("-wal", "-shm"):
            sp = Path(str(src_path) + side)
            if sp.exists():
                try:
                    sp.unlink()
                except OSError:
                    log.warning("restore: could not remove %s", sp)
        # 3. Overwrite the live db file with the chosen backup (owner-only).
        fd = os.open(str(src_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.close(fd)
        shutil.copyfile(tmp_name, src_path)
        os.chmod(src_path, 0o600)
        return safety
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def last_backup_time(dest_dir=None) -> str | None:
    """Human-readable time of the newest backup ("06 Jun 2026 19:30") or None."""
    dest_dir = Path(dest_dir) if dest_dir else Path(_default_dest())
    files = _backup_files(dest_dir)
    if not files:
        return None
    stamp = files[-1].name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
    try:
        dt = datetime.strptime(stamp, "%Y%m%d-%H%M")
    except ValueError:
        return None
    return dt.strftime("%d %b %Y %H:%M")
