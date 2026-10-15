"""Shared message actions used by MainWindow and GroupWindow."""
from __future__ import annotations


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
        if m.folder == "TRASH" and op == "delete":
            # Already in Trash: remove locally only (no server purge yet).
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
        if m.uid > 0:
            db.add_op(m.account_id, "move", m.folder, m.uid, target="INBOX")
        db.move_message_local(msg_id, "INBOX")
        n += 1
    return n
