"""What people do with their company mailboxes, reported to the organization.

Recorded only for mailboxes the organization manages on this PC; accounts
the person added themselves are never recorded, and nothing here ever holds
an email's body. Each event is a few words: what happened, which mailbox,
the subject and the other party. Events queue on disk and go to the
organization's server with the next check-in (see org.checkin), so nothing
is lost while the PC is offline.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from . import org

log = logging.getLogger(__name__)

MAX_QUEUE = 5000          # kept on disk while the server can't be reached
BATCH = 100               # sent per check-in
_lock = threading.Lock()
_db = None
_emails: tuple[float, dict] = (0.0, {})
_written = 0


def bind(db) -> None:
    """Give the recorder the account list (to turn account ids into addresses)."""
    global _db
    _db = db


def email_for(account_id) -> str:
    global _emails
    if _db is None:
        return ""
    now = time.time()
    if now - _emails[0] > 5:
        try:
            _emails = (now, {a.id: a.email.lower() for a in _db.list_accounts()})
        except Exception:
            return ""
    return _emails[1].get(account_id, "")


def _path():
    from .config import data_dir
    return data_dir() / "org_activity.jsonl"


def record(email: str, kind: str, **detail) -> None:
    """Note that *kind* happened on *email*'s mailbox. Silently does nothing
    for an account the organization doesn't manage, or when not joined."""
    global _written
    email = (email or "").lower()
    if not org.is_joined():
        return
    if email:
        if not org.is_managed(email):
            return
    elif not kind.startswith("app_"):
        return
    ev = {"t": round(time.time(), 3), "email": email, "kind": kind[:32],
          "detail": {k: str(v)[:200] for k, v in detail.items() if v}}
    with _lock:
        try:
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
            _written += 1
            if _written % 200 == 0:
                _trim()
        except OSError as e:
            log.debug("activity: not recorded: %s", e)


def _who(m, kind: str) -> str:
    if kind in ("sent", "replied"):
        return (m.recipients or "")[:120]
    return (m.sender or "")[:120]


def record_message(m, kind: str, **extra) -> None:
    """*m* is an EmailMessage; its account decides whether anything is kept."""
    if m is None:
        return
    email = email_for(m.account_id)
    if email:
        record(email, kind, subject=(m.subject or "")[:120], who=_who(m, kind),
               **extra)


def _lines() -> list[str]:
    try:
        with open(_path(), encoding="utf-8") as f:
            return [ln for ln in f.read().split("\n") if ln.strip()]
    except OSError:
        return []


def _trim() -> None:
    lines = _lines()
    if len(lines) > MAX_QUEUE:
        _rewrite(lines[-MAX_QUEUE:])


def _rewrite(lines: list[str]) -> None:
    p = _path()
    tmp = str(p) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("".join(ln + "\n" for ln in lines))
    os.replace(tmp, p)


def take() -> tuple[list[dict], int]:
    """The oldest events to send, and how many lines they were (for ack)."""
    with _lock:
        lines = _lines()[:BATCH]
    events = []
    for ln in lines:
        try:
            ev = json.loads(ln)
            if isinstance(ev, dict) and ev.get("kind"):
                events.append(ev)
        except ValueError:
            pass
    return events, len(lines)


def ack(n: int) -> None:
    """The server took the first *n* lines: drop them, keep anything newer."""
    if n <= 0:
        return
    with _lock:
        lines = _lines()
        try:
            _rewrite(lines[n:])
        except OSError as e:
            log.debug("activity: queue not trimmed: %s", e)
