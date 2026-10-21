"""Tests for the sync worker's reconnect backoff when a server blocks us."""
from __future__ import annotations

import imaplib

from emailclient.sync import imap_worker
from emailclient.sync.imap_worker import BLOCKED_BACKOFF_SECONDS, _server_refused

# Verbatim from a customer log: the mail host blocked the office IP.
ANTISPAM = imaplib.IMAP4.abort(
    "unexpected response: b'[ALERT] Your IP Address is blocked from further "
    "use due to AntiSpam policy. For further clarifications please contact "
    "support* BYE IMAP4rev1 Server logging out'")


class TestServerRefused:
    def test_antispam_block_detected(self):
        assert _server_refused(ANTISPAM)

    def test_rate_limit_detected(self):
        assert _server_refused(Exception("Too many simultaneous connections"))

    def test_ordinary_errors_not_treated_as_block(self):
        for exc in (TimeoutError("timed out"),
                    ConnectionResetError("Connection reset by peer"),
                    OSError("[Errno 8] nodename nor servname provided")):
            assert not _server_refused(exc)


class _FakeDB:
    def get_blocklist(self):
        return set()


class _FakeAccount:
    id = 1
    email = "hr@example.com"


def _seconds_waited_after(monkeypatch, exc) -> int:
    """Fail one session with *exc*, then return how long the worker waited."""
    worker = imap_worker.ImapWorker.__new__(imap_worker.ImapWorker)
    worker.account = _FakeAccount()
    worker.db = _FakeDB()
    worker._stop = False
    worker.status_changed = type("S", (), {"emit": lambda *a: None})()

    sessions = []

    def session():
        sessions.append(1)
        if len(sessions) == 1:
            raise exc
        worker._stop = True   # second session: end the loop cleanly

    worker._session = session
    waited = []
    monkeypatch.setattr(imap_worker.time, "sleep", waited.append)
    monkeypatch.setattr(imap_worker.random, "uniform", lambda a, b: 0)
    worker.run()
    return int(sum(waited))


class TestBackoff:
    def test_blocked_waits_thirty_minutes(self, monkeypatch):
        assert _seconds_waited_after(monkeypatch, ANTISPAM) == BLOCKED_BACKOFF_SECONDS

    def test_glitch_retries_quickly(self, monkeypatch):
        assert _seconds_waited_after(monkeypatch, TimeoutError("timed out")) == imap_worker.RECONNECT_DELAY_SECONDS
