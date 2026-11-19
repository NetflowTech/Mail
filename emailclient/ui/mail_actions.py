"""Shared message actions used by MainWindow and GroupWindow."""
from __future__ import annotations

from .. import activity


def bulk_discard(db, ids: list[int], op: str) -> int:
    """Move messages to TRASH/SPAM (queueing server ops) or purge from Trash.

    op: "delete" -> TRASH, "spam" -> SPAM. Returns the number of messages
    actually processed. Callers handle their own UI refresh/status.
    """
    target = "TRASH" if op == "delete" else "SPAM"
    n = 0
    for msg_id in ids:
        m = db.get_message(msg_id)
        if m is None:
            continue
        activity.record_message(
            m, "deleted_forever" if (m.folder == "TRASH" and op == "delete")
            else "deleted" if op == "delete" else "spam")
        if m.folder == "TRASH" and op == "delete":
            # Already in Trash: delete it for good, here and on the server.
            # The server copy is found by Message-ID, not uid: a mail moved to
            # Trash here still carries its old folder's uid, which in the
            # server's Trash can belong to a different email. Without a
            # Message-ID there's no safe way to find it, so the server copy
            # stays (and the server empties its Trash on its own schedule).
            if m.uid > 0 and m.message_id:
                db.add_op(m.account_id, "purge", "TRASH", m.uid,
                          target=m.message_id)
            db.delete_message_local(msg_id)
        else:
            # Negative synthetic uids are local-only rows: nothing on server.
            if m.uid > 0:
                db.add_op(m.account_id, op, m.folder, m.uid)
            db.move_message_local(msg_id, target)
        n += 1
    return n


def bulk_restore(db, ids: list[int]) -> int:
    """Move messages from Trash/Spam back to the Inbox (queueing a server move
    for real messages). Returns the number actually restored."""
    n = 0
    for msg_id in ids:
        m = db.get_message(msg_id)
        if m is None or m.folder not in ("TRASH", "SPAM"):
            continue
        activity.record_message(m, "restored")
        if m.uid > 0:
            db.add_op(m.account_id, "move", m.folder, m.uid, target="INBOX")
        db.move_message_local(msg_id, "INBOX")
        n += 1
    return n
