"""Watch a queued email long enough to know whether it actually left.

Sending is asynchronous: the composer closes, the row goes into the outbox,
and a worker thread picks it up later. When that worker cannot reach the mail
server the row simply sits there — the old UI said "Email queued" and then
nothing at all, so a failed send looked exactly like a successful one.

This watcher gives every queued email a deadline. Whatever the row's state is
when the deadline passes decides what the user is told:

- sent, failed, held for approval, or recalled → nothing; those paths speak
  for themselves (the failure dialog carries the reason).
- still queued or mid-send → ``slow`` fires, and the window warns that the
  email has not gone out yet while the worker keeps trying.

The deadline counts from when the worker can first touch the row, so the
Undo-Send window is added on top of the timeout rather than eaten by it.
"""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

#: How long after a send we expect a verdict from the mail server.
DEFAULT_TIMEOUT_MS = 30_000

#: Row states that mean the email's story is already told.
_SETTLED = ("sent", "failed", "awaiting_approval", "draft")


class SendWatcher(QObject):
    """Emit :attr:`slow` for queued emails that have not gone out in time."""

    slow = Signal(int)          # outbox row id

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self._timers: dict[int, QTimer] = {}
        self._deadlines: dict[int, int] = {}
        self._warned: set[int] = set()

    # -- public API ----------------------------------------------------
    def watch(self, outbox_ids, timeout_ms: int = DEFAULT_TIMEOUT_MS,
              undo_seconds: int = 0) -> None:
        """Start the clock on every id in *outbox_ids*."""
        deadline = max(0, timeout_ms) + max(0, undo_seconds) * 1000
        for oid in outbox_ids or ():
            self.resolve(oid)           # re-watching restarts the clock
            self._warned.discard(oid)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.setInterval(deadline)
            timer.timeout.connect(lambda oid=oid: self._check(oid))
            self._timers[oid] = timer
            self._deadlines[oid] = deadline
            timer.start()

    def resolve(self, outbox_id: int) -> None:
        """Stop watching *outbox_id* — its outcome is known."""
        timer = self._timers.pop(outbox_id, None)
        self._deadlines.pop(outbox_id, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()

    def stop(self) -> None:
        for oid in list(self._timers):
            self.resolve(oid)

    def pending(self) -> list[int]:
        """Ids still being watched — nothing has settled them yet."""
        return list(self._timers)

    def deadline_ms(self, outbox_id: int) -> int | None:
        return self._deadlines.get(outbox_id)

    # -- internals -----------------------------------------------------
    def _check(self, outbox_id: int) -> None:
        """The deadline passed: warn only if the email is still in limbo."""
        self.resolve(outbox_id)
        if outbox_id in self._warned:   # one warning per email, never a nag
            return
        row = self.db.get_outbox(outbox_id)
        if row is None:                 # recalled with Undo Send
            return
        if row["status"] in _SETTLED:
            return
        self._warned.add(outbox_id)
        self.slow.emit(outbox_id)
