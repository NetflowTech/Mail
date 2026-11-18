"""Delete permanently from Trash also deletes on the server, by Message-ID."""
from __future__ import annotations

from types import SimpleNamespace

from emailclient.db import Database
from emailclient.models import Account, EmailMessage
from emailclient.sync.imap_worker import ImapWorker
from emailclient.ui.mail_actions import bulk_discard


def _setup(tmp_path, message_id="<abc@x>", uid=7):
    db = Database(tmp_path / "mail.db")
    aid = db.add_account(Account(id=None, name="A", email="a@x.com",
                                 provider="custom", imap_host="i", imap_port=993,
                                 smtp_host="s", smtp_port=465, color=""))
    db.save_message(EmailMessage(account_id=aid, folder="TRASH", uid=uid,
                                 subject="s", sender="b", recipients="a",
                                 date="2026-01-01", body_text="", body_html="",
                                 is_read=True, message_id=message_id))
    mid = db.conn.execute("SELECT id FROM messages").fetchone()[0]
    return db, aid, mid


def test_delete_from_trash_queues_purge(tmp_path):
    db, aid, mid = _setup(tmp_path)
    assert bulk_discard(db, [mid], "delete") == 1
    assert db.get_message(mid) is None
    assert db.take_ops(aid) == [{"op": "purge", "folder": "TRASH", "uid": 7,
                                 "target": "<abc@x>"}]


def test_no_message_id_means_no_server_purge(tmp_path):
    db, aid, mid = _setup(tmp_path, message_id="")
    bulk_discard(db, [mid], "delete")
    assert db.take_ops(aid) == []


class FakeClient:
    def __init__(self, found, uidplus=True):
        self.found, self.uidplus, self.calls = found, uidplus, []

    def select_folder(self, f):
        self.calls.append(("select", f))

    def search(self, crit):
        self.calls.append(("search", tuple(crit)))
        return self.found

    def delete_messages(self, uids):
        self.calls.append(("delete", tuple(uids)))

    def has_capability(self, c):
        return self.uidplus

    def expunge(self, uids=None):
        self.calls.append(("expunge", tuple(uids) if uids else None))


def _worker():
    w = ImapWorker.__new__(ImapWorker)
    w.account = SimpleNamespace(email="a@x.com")
    return w


def test_purge_targets_only_matching_mail():
    c = FakeClient([42])
    _worker()._purge(c, "Trash", "<abc@x>")
    assert c.calls == [("select", "Trash"),
                       ("search", ("HEADER", "Message-ID", "<abc@x>")),
                       ("delete", (42,)), ("expunge", (42,))]


def test_purge_without_uidplus_uses_plain_expunge():
    c = FakeClient([42], uidplus=False)
    _worker()._purge(c, "Trash", "<abc@x>")
    assert c.calls[-1] == ("expunge", None)


def test_purge_nothing_found_or_bad_id():
    c = FakeClient([])
    _worker()._purge(c, "Trash", "<abc@x>")
    assert ("delete", (42,)) not in c.calls
    c = FakeClient([42])
    _worker()._purge(c, "Trash", 'x" OR ALL')
    assert c.calls == []
