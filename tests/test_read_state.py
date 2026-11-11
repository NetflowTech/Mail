"""Per-computer read state: what a freshly downloaded message starts as."""
from __future__ import annotations

import pytest

from emailclient.sync.imap_worker import initial_read_state

SEEN = {b"FLAGS": (b"\\Seen",)}
UNSEEN = {b"FLAGS": ()}


class TestInitialReadState:
    def test_new_arrival_is_unread_here_even_if_seen_elsewhere(self):
        # The whole point: a colleague's PC / phone / webmail set \Seen.
        assert initial_read_state("INBOX", SEEN, history=False) is False

    def test_new_arrival_unseen_is_unread(self):
        assert initial_read_state("INBOX", UNSEEN, history=False) is False

    def test_history_follows_server_so_setup_is_not_all_unread(self):
        assert initial_read_state("INBOX", SEEN, history=True) is True
        assert initial_read_state("INBOX", UNSEEN, history=True) is False

    @pytest.mark.parametrize("folder", ["SENT", "TRASH"])
    def test_sent_and_trash_always_read(self, folder):
        assert initial_read_state(folder, UNSEEN, history=False) is True

    def test_custom_folders_behave_like_inbox(self):
        assert initial_read_state("Projects", SEEN, history=False) is False


class TestFetchNewUsesWatermark:
    """_fetch_new treats mail above the watermark as new arrivals and mail
    pulled with no watermark (setup / UIDVALIDITY resync) as history."""

    def _worker(self, last_seen: int, saved: list):
        from emailclient.sync import imap_worker

        class DB:
            def get_folder_state(self, *_):
                return (7, last_seen)   # uidvalidity 7

            def set_folder_state(self, *_):
                pass

            def save_message(self, m):
                saved.append(m)
                return True

        class Account:
            id = 1
            email = "team@example.com"
            sync_depth = "50"
            attachments_only = False

        w = imap_worker.ImapWorker.__new__(imap_worker.ImapWorker)
        w.db = DB()
        w.account = Account()
        return w

    def _client(self, uids):
        raw = (b"From: a@example.com\r\nTo: team@example.com\r\n"
               b"Subject: hi\r\nMessage-ID: <%d@x>\r\n\r\nbody")

        class Client:
            def search(self, _q):
                return list(uids)

            def fetch(self, chunk, _items):
                return {u: {b"BODY[]": raw % u, b"FLAGS": (b"\\Seen",)}
                        for u in chunk}
        return Client()

    def test_new_mail_above_watermark_is_unread(self):
        saved: list = []
        w = self._worker(last_seen=10, saved=saved)
        w._fetch_new(self._client([11, 12]), "INBOX", uidvalidity=7)
        assert [m.is_read for m in saved] == [False, False]

    def test_history_at_setup_follows_server(self):
        saved: list = []
        w = self._worker(last_seen=0, saved=saved)
        w._fetch_new(self._client([1, 2]), "INBOX", uidvalidity=7)
        assert [m.is_read for m in saved] == [True, True]

    def test_sent_folder_new_mail_is_read(self):
        saved: list = []
        w = self._worker(last_seen=10, saved=saved)
        w._fetch_new(self._client([11]), "SENT", uidvalidity=7)
        assert [m.is_read for m in saved] == [True]
