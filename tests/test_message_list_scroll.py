"""A list rebuild (new mail arrived, email sent) must not move the user's view."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint

from emailclient.db import Database
from emailclient.models import Account, EmailMessage
from emailclient.ui import theme
from emailclient.ui.message_list import ROLE_MSG_ID, MessageList


@pytest.fixture(autouse=True)
def _theme():
    theme.apply_theme("AMail Light")


def _msg(uid: int, folder="INBOX") -> EmailMessage:
    return EmailMessage(
        account_id=1, folder=folder, uid=uid, subject=f"Mail {uid}",
        sender=f"s{uid}@example.com", recipients="me@example.com",
        date=f"2026-01-{1 + uid // 1000:02d}T{(uid // 60) % 24:02d}:{uid % 60:02d}:00+00:00",
        body_text="x", body_html="", message_id=f"<{uid}@x>")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "mail.db")
    d.add_account(Account(id=None, name="A", email="me@example.com",
                          provider="imap", imap_host="h", imap_port=993,
                          smtp_host="h", smtp_port=465, color="#123456"))
    for uid in range(1, 251):          # 2.5 pages of 100
        d.save_message(_msg(uid))
    yield d
    d.conn.close()


@pytest.fixture
def lst(qtbot, db):
    w = MessageList(db)
    w.page_size = 100
    qtbot.addWidget(w)
    w.resize(420, 400)
    w.show()
    qtbot.waitExposed(w)
    w.show_view(1, "INBOX")
    return w


def _top_id(lst):
    idx = lst.indexAt(QPoint(0, 0))
    return lst.item(idx.row()).data(ROLE_MSG_ID)


def _scroll_to_row(lst, row):
    """Scroll until *row* exists (paging in as needed) and put it at the top."""
    sb = lst.verticalScrollBar()
    while lst.count() <= row and lst._has_more:
        sb.setValue(sb.maximum())
    lst.scrollToItem(lst.item(row), lst.ScrollHint.PositionAtTop)


class TestRebuildKeepsPlace:
    def test_first_view_is_one_page(self, lst):
        assert lst.count() == 100

    def test_scrolled_pages_and_top_row_survive_a_refresh(self, lst):
        _scroll_to_row(lst, 170)
        assert lst.count() == 200
        top = _top_id(lst)
        lst.refresh()
        assert lst.count() == 200
        assert _top_id(lst) == top

    def test_selection_is_restored_without_moving_the_view(self, lst):
        _scroll_to_row(lst, 120)
        lst.setCurrentItem(lst.item(120))
        selected = lst.item(120).data(ROLE_MSG_ID)
        # Reader is open on row 120; user has scrolled on down to row 190.
        _scroll_to_row(lst, 190)
        top = _top_id(lst)
        lst.refresh()
        assert lst.currentItem().data(ROLE_MSG_ID) == selected
        assert _top_id(lst) == top

    def test_at_the_top_new_mail_shows_at_once(self, lst, db):
        assert lst.verticalScrollBar().value() == 0
        db.save_message(_msg(999))
        lst.refresh()
        assert lst.verticalScrollBar().value() == 0
        assert lst.item(0).data(ROLE_MSG_ID) == db.list_messages(
            account_id=1, limit=1)[0].id

    def test_new_mail_above_does_not_shift_the_view(self, lst, db):
        _scroll_to_row(lst, 150)
        top = _top_id(lst)
        db.save_message(_msg(999))   # newest-first: lands at row 0
        lst.refresh()
        assert _top_id(lst) == top

    def test_anchor_gone_keeps_the_offset(self, lst, db):
        _scroll_to_row(lst, 150)
        sb = lst.verticalScrollBar()
        value = sb.value()
        db.delete_message_local(lst.item(150).data(ROLE_MSG_ID))
        lst.refresh()
        assert sb.value() == min(value, sb.maximum()) and sb.value() > 0

    def test_later_page_restore_does_not_jump(self, lst):
        # The open email is on page 2 and page 1 is all that is loaded after
        # a refresh. When page 2 pages in as the user scrolls, restoring the
        # selection must not yank the view down to it.
        target = lst.db.list_messages(account_id=1, limit=200)[150].id
        lst._restore_id = target
        sb = lst.verticalScrollBar()
        sb.setValue(sb.maximum())      # bottom of page 1 → page 2 loads
        assert lst.count() == 200
        assert lst.currentItem() is not None
        assert lst.currentItem().data(ROLE_MSG_ID) == target
        assert lst.indexAt(QPoint(0, 0)).row() < 120   # still near page 1's end
