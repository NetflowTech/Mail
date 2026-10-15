"""Turn an outbox row into something the reader can display.

Outbox rows are not `messages` rows: they live in their own table until the
worker sends them, so clicking one used to leave the reader blank. This builds
a read-only EmailMessage view of a row so queued, failed and awaiting-approval
mail can be checked before it goes out.

`attachments_json` is shared with the outbox worker, which stores the same
shape on the locally-saved SENT copy.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path

from .models import EmailMessage


def _cache_copy(path: str, cache_dir) -> str:
    """Copy *path* into the app's attachment cache and return the new path.

    Each file gets its own folder, so two attachments named the same never
    collide. Returns the original path if the copy fails — a Sent copy that
    points somewhere is better than one that points nowhere.
    """
    try:
        dest_dir = Path(cache_dir) / uuid.uuid4().hex
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / os.path.basename(path)
        shutil.copyfile(path, dest)
        return str(dest)
    except OSError:
        return path


def attachments_json(files: list, inline: list, cache_dir=None) -> str:
    """Build the reader's attachment JSON from an outbox row's file lists.

    Mirrors the {name, path, size, cid, inline} shape produced by mailparse so
    the reader resolves cid: signature images and shows attachment chips. Inline
    images carry their cid and inline=True (no chip); real attachments get a
    chip. Missing files are skipped. Returns "" when there is nothing to store.

    With *cache_dir*, real attachments are COPIED there and the copy is what
    gets recorded. The user picked those files from anywhere on disk, and a
    saved Sent mail must not lose them when the original is moved or deleted.
    Inline images are left alone: they already live in the app's own inline/
    folder and the body references them from there by cid.
    """
    items = []
    for img in inline or []:
        path = img.get("path", "")
        try:
            size = os.path.getsize(path)
        except OSError:
            continue  # inline image gone → skip (body just shows broken ref)
        items.append({"name": os.path.basename(path), "path": path,
                      "size": size, "cid": img.get("cid", ""), "inline": True})
    for path in files or []:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        stored = _cache_copy(path, cache_dir) if cache_dir else path
        items.append({"name": os.path.basename(path), "path": stored,
                      "size": size, "cid": "", "inline": False})
    return json.dumps(items) if items else ""


def split_attachments(raw: str) -> tuple[list, list]:
    """Outbox `attachments` column → (files, inline).

    Legacy rows stored a plain list of paths; newer ones store
    {"files": [...], "inline": [{"cid":..., "path":...}]}.
    """
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return [], []
    if isinstance(parsed, dict):
        return parsed.get("files", []), parsed.get("inline", [])
    if isinstance(parsed, list):
        return parsed, []
    return [], []


def message_from_row(row, sender: str) -> EmailMessage:
    """Read-only EmailMessage for outbox `row`, sent from address *sender*.

    The uid is the negated row id, matching what the worker gives the SENT copy,
    so nothing downstream mistakes it for a real server uid.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    def get(name, default=""):
        return (row[name] if name in keys else default) or default

    files, inline = split_attachments(get("attachments"))
    return EmailMessage(
        account_id=get("account_id", 0),
        folder="OUTBOX",
        uid=-int(get("id", 0) or 0),
        subject=get("subject"),
        sender=sender,
        recipients=get("to_addrs"),
        cc=get("cc"),
        bcc=get("bcc"),
        date=get("created_at"),
        body_text=get("body"),
        body_html=get("body_html"),
        attachments=attachments_json(files, inline),
        is_read=True,
    )
