"""OutboxWorker: a QThread that drains the outbox table by sending queued messages.

Design notes:
- Negative synthetic UIDs (uid = -outbox_row_id) are used for locally-saved SENT
  copies to avoid collision with server-assigned UIDs. Server UIDs are always
  positive integers; negative values are an internal convention only.
- IMAP APPEND (for non-gmail, non-outlook providers) is attempted best-effort:
  any failure is swallowed; the send is already complete.
- Gmail and Outlook auto-save sent messages server-side, so APPEND is skipped
  for those providers.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

from PySide6.QtCore import QThread, Signal

from .. import approval, outbox_preview, smtp_sender
from ..config import data_dir
from ..db import Database
from ..models import EmailMessage

log = logging.getLogger(__name__)

_SLEEP_SECONDS = 3


class OutboxWorker(QThread):
    """Poll the outbox table and send queued messages via SMTP."""

    sent = Signal(int)          # outbox row id
    failed = Signal(int, str)   # outbox row id, error message
    held = Signal(int)          # outbox row id sent to the admin approval queue

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # pragma: no cover — thread loop; logic tested via _process_row
        self.db.recover_stale_outbox()
        while not self._stop:
            row = self.db.claim_next_outbox()
            if row is None:
                # Stop-aware sleep: check flag every second
                for _ in range(_SLEEP_SECONDS):
                    if self._stop:
                        return
                    time.sleep(1)
            else:
                self._process_row(row)

    def _process_row(self, row) -> None:
        """Process a single outbox row (already in 'sending' state).

        Separated from run() for unit-test convenience — call directly with a
        sqlite3.Row (or any dict-like object with the expected keys).
        """
        row_id = row["id"]
        try:
            accounts = self.db.list_accounts()
            account = next((a for a in accounts if a.id == row["account_id"]), None)
            if account is None:
                self.db.finish_outbox(row_id, False, "account removed")
                self.failed.emit(row_id, "account removed")
                return

            # attachments JSON: legacy format is a plain list of file paths;
            # new format is {"files": [...], "inline": [{"cid":..., "path":...}]}
            files, inline = outbox_preview.split_attachments(row["attachments"])

            # Approval gate: any email from an approval-required account is held
            # in the shared queue (including the admin's own) — the admin
            # releases it from "Waiting for Approval", and their app then sends
            # it. The approve path sends via SMTP directly, so it won't loop.
            #
            # send_direct rows are the exception: the admin approved in "send
            # from sender" mode, so we send straight from this device (skipping
            # the gate) — that makes the recipient's Received header show this
            # sender's IP, not the admin's.
            send_direct = bool(row["send_direct"]) if "send_direct" in row.keys() \
                else False
            if (not send_direct
                    and account.email.lower() in approval.cached_accounts()):
                if self._submit_for_approval(row, account, files, inline):
                    return
                # upload failed → mark failed so the user can retry later
                self.db.finish_outbox(row_id, False,
                                      "Could not reach approval server — retry")
                self.failed.emit(row_id, "approval upload failed")
                return

            msg = smtp_sender.build_mime(
                account=account,
                to_addrs=row["to_addrs"],
                cc=row["cc"],
                subject=row["subject"],
                body_text=row["body"],
                attachments=files,
                body_html=row["body_html"],
                inline_images=[(i["cid"], i["path"]) for i in inline],
            )
            all_rcpts = smtp_sender.parse_rcpts(
                row["to_addrs"], row["cc"], row["bcc"]
            )
            smtp_sender.send(account, msg, all_rcpts)

            # Mark sent in DB
            self.db.finish_outbox(row_id, True)

            # If this was an admin-approved "send from sender" release, tell the
            # server the email went out so it leaves the admin queue.
            if send_direct and row["approval_id"]:
                try:
                    approval.sender_sent(row["approval_id"])
                except Exception:  # best-effort; the email is already sent
                    log.debug("approval.sender_sent failed (non-fatal)")

            # Save a local SENT copy.
            # Negative uid avoids collision with server-assigned positive UIDs.
            # Preserve inline images + attachments in the reader's JSON format so
            # the sent copy renders cid: signature images and shows chips too.
            now_iso = datetime.now(timezone.utc).isoformat()
            sent_copy = EmailMessage(
                account_id=account.id,
                folder="SENT",
                uid=-row_id,
                subject=row["subject"],
                sender=account.email,
                recipients=row["to_addrs"],
                cc=row["cc"] or "",
                bcc=row["bcc"] or "",
                date=now_iso,
                body_text=row["body"],
                body_html=row["body_html"],
                # Copy the picked files into the app's cache: this Sent copy
                # has to keep working after the user moves or deletes them.
                attachments=outbox_preview.attachments_json(
                    files, inline, cache_dir=data_dir() / "attachments"),
                is_read=True,
                # Same id the mail went out with, so when the server's own copy
                # syncs back it replaces this placeholder instead of showing up
                # next to it as a second (and third) Sent entry.
                message_id=str(msg.get("Message-ID", "") or ""),
            )
            self.db.save_message(sent_copy)

            # Best-effort IMAP APPEND for providers that don't auto-save.
            if account.provider not in ("gmail", "outlook"):
                self._try_imap_append(account, msg)

            self.sent.emit(row_id)

        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            log.exception("outbox_worker: failed to send outbox row %s", row_id)
            self.db.finish_outbox(row_id, False, err)
            self.failed.emit(row_id, err)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _submit_for_approval(self, row, account, files, inline) -> bool:
        """Upload this outbox row to the admin approval queue. Returns True on
        success (and marks the row 'awaiting_approval'), False on failure."""
        attachments: list[tuple[str, bytes]] = []
        inline_meta: list[dict] = []
        for path in files:
            try:
                with open(path, "rb") as fh:
                    attachments.append((os.path.basename(path), fh.read()))
            except OSError:
                log.warning("approval: cannot read attachment %s", path)
        for img in inline:
            try:
                with open(img["path"], "rb") as fh:
                    inline_meta.append({"cid": img["cid"], "idx": len(attachments)})
                    attachments.append((os.path.basename(img["path"]), fh.read()))
            except OSError:
                log.warning("approval: cannot read inline image %s", img.get("path"))
        approval_id = approval.submit(
            account_email=account.email,
            to=row["to_addrs"], cc=row["cc"], bcc=row["bcc"],
            subject=row["subject"], body_text=row["body"],
            body_html=row["body_html"], sender_label=account.email,
            attachments=attachments, inline_meta=inline_meta)
        if not approval_id:
            return False
        self.db.mark_outbox_awaiting(row["id"], approval_id)
        self.held.emit(row["id"])
        return True

    def _try_imap_append(self, account, msg) -> None:  # pragma: no cover
        """Try to APPEND the sent message to the server Sent folder.

        All errors are silently swallowed — the send has already succeeded.
        """
        try:
            import ssl as _ssl
            from imapclient import IMAPClient
            from .. import credentials, oauth
            from ..sync.imap_worker import FALLBACKS, DEFAULT_FALLBACK

            fallbacks = FALLBACKS.get(account.provider, DEFAULT_FALLBACK)
            sent_folder = fallbacks.get("SENT", "Sent")

            client = IMAPClient(account.imap_host, port=account.imap_port,
                                ssl=True, ssl_context=_ssl.create_default_context())
            try:
                if account.provider == "outlook":
                    # Should never reach here (outlook skipped above), but guard anyway
                    return
                else:
                    password = credentials.get_password(account.email) or ""
                    client.login(account.email, password)
                raw = msg.as_bytes()
                client.append(sent_folder, raw)
            finally:
                try:
                    client.logout()
                except Exception:
                    pass
        except Exception:
            log.debug("outbox_worker: IMAP APPEND failed (non-fatal), ignoring")
