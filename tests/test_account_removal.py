"""Removing an account leaves nothing behind for a later account to inherit,
and nothing usable in the system keychain."""
from __future__ import annotations

import sqlite3

import pytest

from emailclient.db import Database
from emailclient.models import Account, EmailMessage


def _acc(email="a@example.com"):
    return Account(id=None, name="A", email=email, provider="custom",
                   imap_host="imap.x", imap_port=993, smtp_host="smtp.x",
                   smtp_port=465, color="")


def _msg(account_id, uid, folder="INBOX"):
    return EmailMessage(account_id=account_id, folder=folder, uid=uid,
                        subject="s", sender="b@x.com", recipients="a@x.com",
                        date="2026-01-01T00:00:00+00:00", body_text="t",
                        body_html="", is_read=False)


def test_late_worker_writes_are_dropped(tmp_path):
    db = Database(tmp_path / "mail.db")
    aid = db.add_account(_acc())
    db.save_message(_msg(aid, 1))
    db.delete_account_cascade(aid)
    # The sync worker was still finishing a folder:
    assert db.save_message(_msg(aid, 2, "SENT")) is False
    db.set_folder_state(aid, "SENT", 7, 100)
    db.set_backfill_done(aid, "SPAM")
    db.add_account_folder(aid, "Work")
    db.add_op(aid, "delete", "INBOX", 1)
    for t in Database._ACCOUNT_TABLES:
        assert db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t


def test_account_ids_are_never_reused(tmp_path):
    db = Database(tmp_path / "mail.db")
    first = db.add_account(_acc("a@x.com"))
    second = db.add_account(_acc("b@x.com"))
    db.delete_account_cascade(second)
    third = db.add_account(_acc("c@x.com"))
    assert third > second > first
    db.conn.close()
    # ...also after a restart.
    db = Database(tmp_path / "mail.db")
    db.delete_account_cascade(third)
    assert db.add_account(_acc("d@x.com")) > third


def test_leftovers_are_purged_at_startup(tmp_path):
    path = tmp_path / "mail.db"
    db = Database(path)
    keep = db.add_account(_acc("a@x.com"))
    db.save_message(_msg(keep, 1))
    db.conn.close()
    # Rows of an account that no longer exists, as older versions left them.
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO messages (account_id, folder, uid, subject, sender,"
                " recipients, date, body_text, body_html) VALUES"
                " (99, 'SENT', 5, 's', 'x', 'y', 'd', '', '')")
    raw.execute("INSERT INTO folder_state (account_id, folder, last_seen_uid)"
                " VALUES (99, 'SENT', 500)")
    raw.commit()
    raw.close()
    db = Database(path)
    assert db.conn.execute("SELECT COUNT(*) FROM messages WHERE account_id=99").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM folder_state WHERE account_id=99").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.add_account(_acc("z@x.com")) != keep


def test_foreign_keys_enforced(tmp_path):
    db = Database(tmp_path / "mail.db")
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute("INSERT INTO messages (account_id, folder, uid, subject,"
                        " sender, recipients, date, body_text, body_html)"
                        " VALUES (12345, 'INBOX', 1, '', '', '', '', '', '')")


def test_forget_account_clears_keychain(monkeypatch):
    from emailclient import credentials, google_oauth, oauth
    deleted = []
    monkeypatch.setattr(credentials.keyring, "delete_password",
                        lambda svc, key: deleted.append(key))
    monkeypatch.setattr(google_oauth.keyring, "delete_password",
                        lambda svc, key: deleted.append(key))
    removed = []
    monkeypatch.setattr(oauth, "forget", lambda e: removed.append(e))
    credentials.forget_account("a@x.com")
    assert "a@x.com" in deleted
    assert google_oauth._refresh_key("a@x.com") in deleted
    assert removed == ["a@x.com"]
