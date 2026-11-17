"""Undo Send puts the mail back in Drafts (Gmail-style) instead of deleting it."""
from __future__ import annotations

import time

import pytest

from emailclient.db import Database
from emailclient.models import Account


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "mail.db")
    d.add_account(Account(id=None, name="A", email="me@example.com",
                          provider="imap", imap_host="h", imap_port=993,
                          smtp_host="h", smtp_port=465, color="#123456"))
    yield d
    d.conn.close()


def _queue(db, delay=10):
    return db.queue_outbox(1, "to@example.com", "", "", "Subject", "body",
                           "[]", body_html="<p>body</p>", delay_seconds=delay)


class TestRecallOutbox:
    def test_queued_mail_becomes_a_draft_with_its_text(self, db):
        oid = _queue(db)
        assert db.recall_outbox(oid)
        row = db.get_outbox(oid)
        assert row["status"] == "draft"
        assert row["body_html"] == "<p>body</p>" and row["subject"] == "Subject"
        assert row["send_after"] == 0
        assert db.claim_next_outbox() is None          # the worker won't send it
        assert [r["id"] for r in db.list_drafts()] == [oid]

    def test_too_late_once_the_worker_has_it(self, db):
        oid = _queue(db, delay=0)
        time.sleep(0.01)
        assert db.claim_next_outbox()["id"] == oid     # status: sending
        assert not db.recall_outbox(oid)
        assert db.get_outbox(oid)["status"] == "sending"

    def test_sent_and_missing_rows_are_left_alone(self, db):
        oid = _queue(db, delay=0)
        time.sleep(0.01)
        db.claim_next_outbox()
        db.finish_outbox(oid, ok=True)
        assert not db.recall_outbox(oid)
        assert db.get_outbox(oid)["status"] == "sent"
        assert not db.recall_outbox(999)

    def test_recalled_draft_can_be_sent_again(self, db):
        oid = _queue(db)
        db.recall_outbox(oid)
        db.update_outbox(oid, 1, "to@example.com", "", "", "Subject 2",
                         "body 2", "[]", body_html="<p>2</p>", delay_seconds=0)
        time.sleep(0.01)
        assert db.claim_next_outbox()["id"] == oid


class TestReopenedDraft:
    def test_compose_reopens_the_recalled_mail_and_keeps_it_a_draft(self, qtbot, db):
        from emailclient.ui import theme
        from emailclient.ui.compose import ComposeDialog
        theme.apply_theme("AMail Light")
        oid = _queue(db)
        assert db.recall_outbox(oid)
        dlg = ComposeDialog(db, edit_outbox_id=oid)
        qtbot.addWidget(dlg)
        assert dlg.subject_edit.text() == "Subject"
        assert dlg.to_edit.text() == "to@example.com"
        assert "body" in dlg.body_edit.toPlainText()
        dlg.reject()                     # closed without sending
        assert db.get_outbox(oid)["status"] == "draft"
        assert db.claim_next_outbox() is None
