from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal

from ..db import Database
from .imap_worker import ImapWorker

log = logging.getLogger(__name__)


class SyncEngine(QObject):
    """Starts one ImapWorker per account and relays their signals to the GUI."""

    new_messages = Signal(int, list, bool)  # account_id, msgs, initial_backfill
    status_changed = Signal(int, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.workers: dict[int, ImapWorker] = {}
        # Accounts whose worker died on an auth error: the watchdog must not
        # restart these (it would just fail again); a password fix / re-login
        # clears the flag via start_account.
        self.auth_failed: set[int] = set()

    # Delay between successive worker launches in start_all. Launching 50+
    # TLS handshakes simultaneously hammers the network and CPU; staggering
    # spreads the connects out (the watchdog catches any stragglers anyway).
    STAGGER_MS = 400

    def start_all(self):
        for i, account in enumerate(self.db.list_accounts()):
            QTimer.singleShot(i * self.STAGGER_MS,
                              lambda acc=account: self.start_account(acc))

    def start_account(self, account):
        if getattr(account, "paused", False):
            return  # user paused this account — no worker
        if account.email.lower() in self.db.get_blocklist():
            # Removed license-wide by the admin: never sync, even if re-added.
            log.warning("start_account: %s is blocklisted — refusing",
                        account.email)
            return
        existing = self.workers.get(account.id)
        if existing is not None:
            if existing.isRunning():
                return
            # Dead worker (e.g. auth error ended run()): replace it so the
            # account can actually restart instead of being blocked forever.
            self.workers.pop(account.id, None)
        self.auth_failed.discard(account.id)
        w = ImapWorker(account, self.db)
        w.new_messages.connect(self.new_messages)
        w.status_changed.connect(self.status_changed)
        w.status_changed.connect(self._track_status)
        self.workers[account.id] = w
        w.start()

    def sync_now(self, account_ids: list[int] | None = None) -> int:
        """Ask the given accounts (default: all) to fetch new mail immediately.

        Returns how many live workers were nudged. Progressive backfill of older
        history is untouched — it resumes right after the new-mail check."""
        ids = account_ids if account_ids is not None else list(self.workers)
        nudged = 0
        for aid in ids:
            w = self.workers.get(aid)
            if w is not None and w.isRunning():
                w.request_sync()
                nudged += 1
        return nudged

    def _track_status(self, account_id: int, status: str) -> None:
        if status == "error":
            self.auth_failed.add(account_id)

    def watchdog(self) -> None:
        """Restart any account whose worker died (except auth failures)."""
        for account in self.db.list_accounts():
            if account.id in self.auth_failed or getattr(account, "paused", False):
                continue
            w = self.workers.get(account.id)
            if w is None or not w.isRunning():
                log.warning("watchdog: worker for account %s (%s) not running"
                            " — restarting", account.id, account.email)
                self.start_account(account)

    def stop_account(self, account_id: int) -> None:
        worker = self.workers.pop(account_id, None)
        if worker:
            worker.stop()
            worker.wait(3000)

    def stop_all(self):
        """Stop every worker fast. Workers blocked in a 30s IMAP IDLE call
        would otherwise make exit hang (3s × N accounts), so we give them one
        SHARED ~1.5s grace window and then force-terminate the stragglers — the
        app is quitting anyway."""
        import time
        workers = list(self.workers.values())
        for w in workers:
            w.stop()
        deadline = time.monotonic() + 1.5
        for w in workers:
            ms = max(0, int((deadline - time.monotonic()) * 1000))
            w.wait(ms)
        for w in workers:
            if w.isRunning():
                w.terminate()
                w.wait(500)
        self.workers.clear()
