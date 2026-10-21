from __future__ import annotations

import datetime
import email
import email.policy
import logging
import random
import socket
import ssl
import time

from PySide6.QtCore import QThread, Signal
from imapclient import IMAPClient
import imapclient.imapclient as _ic
import imapclient.exceptions

from .. import credentials, google_oauth, nethttp, oauth
from ..config import data_dir
from ..db import Database
from ..mailparse import parse_message
from ..models import Account, EmailMessage

log = logging.getLogger(__name__)

IDLE_REFRESH_SECONDS = 25 * 60   # servers drop IDLE around 29 min
# How long one IDLE wait blocks before the loop looks around (stop requested?
# manual sync? backfill batch due?). Short enough that "Sync now" feels
# immediate; it is a plain socket wait, so polling costs nothing per account.
IDLE_POLL_SECONDS = 10
RECONNECT_DELAY_SECONDS = 15
BLOCKED_BACKOFF_SECONDS = 30 * 60  # after the server says we are blocked

# Phrases servers use when they refuse us outright (anti-spam IP blocks, rate
# limits). Matched case-insensitively against the error text.
_REFUSAL_MARKERS = ("blocked", "antispam", "anti-spam", "too many",
                    "rate limit", "throttl")


def _server_refused(exc: BaseException) -> bool:
    """True if the server rejected us for abuse/rate reasons, not a glitch."""
    text = str(exc).lower()
    return any(m in text for m in _REFUSAL_MARKERS)
INITIAL_FETCH_COUNT = 50
FETCH_CHUNK_SIZE = 50    # uids per client.fetch call (protects memory on big mailboxes)
BACKFILL_CHUNK_START = 100   # "auto" mode: first backfill batch size
BACKFILL_CHUNK_MAX = 800     # "auto" mode: batch size cap (doubles each step)
DEPTH_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}

# Map canonical folder names to imapclient special-use attributes
SPECIAL = {"SENT": _ic.SENT, "TRASH": _ic.TRASH, "SPAM": _ic.JUNK}

# Provider-specific server folder name fallbacks
FALLBACKS = {
    "gmail": {"SENT": "[Gmail]/Sent Mail", "TRASH": "[Gmail]/Trash", "SPAM": "[Gmail]/Spam"},
    "outlook": {"SENT": "Sent", "TRASH": "Deleted", "SPAM": "Junk"},
}
DEFAULT_FALLBACK = {"SENT": "Sent", "TRASH": "Trash", "SPAM": "Spam"}


def _raw_body(data: dict) -> bytes | None:
    """Raw message bytes out of a FETCH response.

    We ask for BODY.PEEK[] — a plain RFC822/BODY[] fetch implicitly sets \\Seen
    on the server, which made every synced email arrive already-read. Servers
    answer PEEK with the b"BODY[]" key; RFC822 stays as a fallback for the odd
    server that echoes the legacy name.
    """
    for key in (b"BODY[]", b"RFC822"):
        raw = data.get(key)
        if raw is not None:
            return raw
    return None


class AuthError(Exception):
    """Raised when authentication credentials are missing or invalid."""


class ImapWorker(QThread):
    """One per account. Initial fetch then IMAP IDLE; new mail -> signals."""

    new_messages = Signal(int, list, bool)  # account_id, list[EmailMessage], initial_backfill
    status_changed = Signal(int, str)     # account_id, "connected"|"reconnecting"|"error"

    def __init__(self, account: Account, db: Database):
        super().__init__()
        self.account = account
        self.db = db
        self._stop = False
        # Live IMAPClient for the current session, or None between sessions.
        # stop() shuts down its socket to interrupt a blocking IDLE read so the
        # thread exits promptly (otherwise it sits in a 30s read and the quit
        # path force-terminates it — destroying a running QThread → SIGABRT).
        self._client: IMAPClient | None = None
        # "auto" sync mode: progressive backfill batch size (grows per step)
        # and per-folder floor of the lowest UID already fetched this session
        # (prevents re-fetching the same batch when attachments_only filtered
        # everything out so oldest_uid did not move).
        self._backfill_chunk = BACKFILL_CHUNK_START
        self._backfill_floor: dict[str, int] = {}
        # Set by request_sync() from the GUI thread: check for new mail at the
        # next poll boundary instead of waiting for an IDLE event.
        self._sync_now = False

    def request_sync(self) -> None:
        """Ask this worker to check for new mail as soon as it can.

        Thread-safe (a single bool flag): the session loop leaves IDLE at its
        next poll and runs a normal INBOX sync. Any progressive backfill keeps
        running afterwards — asking for the latest mail never cancels history."""
        self._sync_now = True

    def _resolve_folders(self, client) -> dict:
        """Resolve server folder names for canonical folders (SENT/TRASH/SPAM).

        Returns dict: canonical -> server name (str) or None if unresolvable.
        """
        provider = self.account.provider
        fallback_table = FALLBACKS.get(provider, DEFAULT_FALLBACK)
        result = {}
        for canonical, special_attr in SPECIAL.items():
            server_name = None
            try:
                server_name = client.find_special_folder(special_attr)
            except Exception:
                pass
            if not server_name:
                server_name = fallback_table.get(canonical)
            result[canonical] = server_name
        return result

    def _resolve_custom_folders(self, client, special: dict) -> list[str]:
        """Server folders the USER created (custom labels/folders) — everything
        selectable except INBOX, the special-use folders (Sent/Trash/Spam/Drafts/
        All/Archive/etc.) and non-selectable container folders. These hold mail
        that server rules file away, which otherwise never synced."""
        skip = {"INBOX"}
        for v in special.values():
            if v:
                skip.add(v)
        special_flags = ("\\sent", "\\trash", "\\junk", "\\drafts", "\\all",
                         "\\archive", "\\flagged", "\\important", "\\noselect")
        out = []
        try:
            for flags, _delim, name in client.list_folders():
                if not name or name in skip:
                    continue
                fl = " ".join(
                    (f.decode() if isinstance(f, bytes) else str(f)).lower()
                    for f in (flags or ()))
                if any(sf in fl for sf in special_flags):
                    continue
                if name == "[Gmail]" or name.endswith("/"):
                    continue
                out.append(name)
        except Exception:
            log.warning("worker %s: could not list custom folders",
                        self.account.email)
        return out

    def _fetch_new(self, client, folder: str = "INBOX",
                   uidvalidity: int = 0) -> tuple[list[EmailMessage], bool]:
        """Fetch UIDs above the persisted watermark, save to DB.

        The watermark lives in folder_state (independent of locally stored
        messages, so local deletions never lower it). A UIDVALIDITY mismatch
        invalidates stored UIDs and forces a re-fetch of the last
        INITIAL_FETCH_COUNT messages (deduped by save_message's INSERT OR IGNORE).

        Returns (genuinely-new messages, initial) where initial is True only on
        the first-ever fetch for this folder (no prior folder_state).
        """
        stored_validity, last_seen = self.db.get_folder_state(self.account.id, folder)
        if stored_validity and uidvalidity != stored_validity:
            log.warning("worker %s: UIDVALIDITY changed for %s (%s -> %s), full resync",
                        self.account.email, folder, stored_validity, uidvalidity)
            last_seen = 0
        initial = (last_seen == 0 and stored_validity == 0)
        if last_seen == 0:
            depth = getattr(self.account, "sync_depth", "50") or "50"
            if depth == "all":
                uids = client.search(["ALL"])
            elif depth in DEPTH_DAYS:
                since = datetime.date.today() - datetime.timedelta(days=DEPTH_DAYS[depth])
                uids = client.search(["SINCE", since])
            else:
                # "50" and "auto": newest 50 for instant usability. In "auto"
                # mode older history is then pulled in by _backfill_step.
                uids = client.search(["ALL"])[-INITIAL_FETCH_COUNT:]
        else:
            uids = [u for u in client.search(["UID", f"{last_seen + 1}:*"]) if u > last_seen]
        new = []
        for start in range(0, len(uids), FETCH_CHUNK_SIZE):
            chunk = uids[start:start + FETCH_CHUNK_SIZE]
            for uid, data in client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS"]).items():
                raw = _raw_body(data)
                if raw is None:
                    log.warning("worker %s: uid %s missing body, skipping (expunged?)",
                                self.account.email, uid)
                    continue
                try:
                    msg = parse_message(raw, self.account.id, folder, uid,
                                        attachment_dir=data_dir() / "attachments")
                except Exception:
                    # parse_message is hardened to never raise, but one hostile
                    # message must never kill the worker or wedge sync, so guard
                    # the call site too. The watermark still advances below.
                    log.exception("worker %s: parse failed, skipping uid %s",
                                  self.account.email, uid)
                    continue
                # Honor the server's read state. Sent/Trash are always "read"
                # (own mail / discarded) so they never inflate unread counts
                # or fire notifications, even on servers that omit \Seen there.
                if folder in ("SENT", "TRASH"):
                    msg.is_read = True
                else:
                    flags = data.get(b"FLAGS") or ()
                    msg.is_read = b"\\Seen" in flags or "\\Seen" in flags
                if (getattr(self.account, "attachments_only", False)
                        and folder == "INBOX" and not msg.attachments):
                    # User opted to keep only attachment-bearing INBOX mail;
                    # skip saving (watermark still advances below).
                    continue
                if self.db.save_message(msg):
                    new.append(msg)
        new_max = max(uids) if uids else last_seen
        self.db.set_folder_state(self.account.id, folder, uidvalidity,
                                 max(last_seen, new_max))
        return new, initial

    def _backfill_step(self, client, folder: str = "INBOX",
                       uidvalidity: int = 0) -> bool:
        """One progressive-backfill batch for "auto" sync mode.

        Fetches the newest `self._backfill_chunk` UIDs older than the oldest
        stored message, growing the chunk (x2, capped at BACKFILL_CHUNK_MAX)
        each step. The folder must already be selected on `client` and the
        worker must NOT be in IDLE (search/fetch are issued).

        Cost note: client.search(["ALL"]) runs once per step. It returns UID
        integers only (no bodies), so even on very large mailboxes this is a
        small response — acceptable for an idle-cycle background task.

        Returns True if more backfill work likely remains, False when done
        (or when this account/folder does not backfill at all).
        """
        if (getattr(self.account, "sync_depth", "50") or "50") != "auto":
            return False
        if self.db.is_backfill_done(self.account.id, folder):
            return False
        oldest = self.db.oldest_uid(self.account.id, folder)
        if oldest <= 1:
            # Nothing stored yet (0) or history reaches UID 1 — fully synced.
            self.db.set_backfill_done(self.account.id, folder)
            return False
        bound = min(oldest, self._backfill_floor.get(folder, oldest))
        uids = [u for u in client.search(["ALL"]) if u < bound]
        if not uids:
            self.db.set_backfill_done(self.account.id, folder)
            return False
        take = uids[-self._backfill_chunk:]
        saved = []
        for start in range(0, len(take), FETCH_CHUNK_SIZE):
            chunk = take[start:start + FETCH_CHUNK_SIZE]
            for uid, data in client.fetch(chunk, [b"BODY.PEEK[]", b"FLAGS"]).items():
                raw = _raw_body(data)
                if raw is None:
                    continue
                try:
                    msg = parse_message(raw, self.account.id, folder, uid,
                                        attachment_dir=data_dir() / "attachments")
                except Exception:
                    log.exception("worker %s: parse failed, skipping uid %s",
                                  self.account.email, uid)
                    continue
                if folder in ("SENT", "TRASH"):
                    msg.is_read = True
                else:
                    flags = data.get(b"FLAGS") or ()
                    msg.is_read = b"\\Seen" in flags or "\\Seen" in flags
                if (getattr(self.account, "attachments_only", False)
                        and folder == "INBOX" and not msg.attachments):
                    continue
                if self.db.save_message(msg):  # INSERT OR IGNORE dedups
                    saved.append(msg)
        self._backfill_floor[folder] = min(take)
        self._backfill_chunk = min(self._backfill_chunk * 2, BACKFILL_CHUNK_MAX)
        if saved:
            # initial=True → UI list refresh only, NO notifications
            self.new_messages.emit(self.account.id, saved, True)
        log.debug("worker %s: backfilled %d msgs in %s (next chunk %d)",
                  self.account.email, len(saved), folder, self._backfill_chunk)
        return True

    def _backfill_cc(self, client, folders: dict, custom_folders: list) -> None:
        """One-time: fill Cc/Bcc for messages synced before cc parsing existed.

        Fetches ONLY the CC/BCC header fields by UID (cheap — no bodies) and
        updates the stored rows. Guarded by a per-account flag so it runs at
        most once. Must NOT run while IDLE is active. Leaves INBOX selected.
        """
        if self.db.cc_backfill_done(self.account.id):
            return
        missing = self.db.messages_missing_cc(self.account.id)
        if not missing:
            self.db.set_cc_backfill_done(self.account.id)
            return

        def server_of(folder: str) -> str | None:
            if folder == "INBOX":
                return "INBOX"
            if folder in ("SENT", "TRASH", "SPAM"):
                return folders.get(folder)
            return folder if folder in custom_folders else folder

        try:
            for folder, uids in missing.items():
                server = server_of(folder)
                if not server:
                    continue
                try:
                    client.select_folder(server)
                except Exception:
                    continue  # folder gone / not selectable — skip it
                for i in range(0, len(uids), FETCH_CHUNK_SIZE):
                    if self._stop:
                        return  # interrupted → flag unset → resume next session
                    chunk = uids[i:i + FETCH_CHUNK_SIZE]
                    try:
                        resp = client.fetch(
                            chunk, [b"BODY.PEEK[HEADER.FIELDS (CC BCC)]"])
                    except Exception:
                        continue
                    for uid, data in resp.items():
                        # Response key drops PEEK and can vary in spacing/case;
                        # grab whatever header-fields blob came back.
                        raw = next((v for k, v in data.items()
                                    if b"HEADER" in k
                                    and isinstance(v, (bytes, bytearray))), None)
                        if not raw:
                            continue
                        try:
                            hdr = email.message_from_bytes(
                                bytes(raw), policy=email.policy.default)
                            cc = str(hdr.get("Cc", "") or "")
                            bcc = str(hdr.get("Bcc", "") or "")
                        except Exception:
                            continue
                        if cc or bcc:
                            self.db.update_message_headers(
                                self.account.id, folder, uid, cc, bcc)
            self.db.set_cc_backfill_done(self.account.id)
        finally:
            try:
                client.select_folder("INBOX")
            except Exception:
                pass

    def _sync_folder(self, client, canonical: str, server_name: str | None) -> list[EmailMessage]:
        """Select server_name and fetch new messages, saving with canonical folder name.

        Returns list of newly saved EmailMessage objects, or [] on any failure.
        """
        if not server_name:
            return [], False
        try:
            info = client.select_folder(server_name)
        except Exception:
            log.warning("worker %s: could not select folder %s (server: %s)",
                        self.account.email, canonical, server_name)
            return [], False
        uidvalidity = info.get(b"UIDVALIDITY", 0) if isinstance(info, dict) else 0
        return self._fetch_new(client, canonical, uidvalidity)

    def _sync_inbox(self, client) -> tuple[list[EmailMessage], bool]:
        """Select INBOX (capturing UIDVALIDITY) and fetch new messages."""
        info = client.select_folder("INBOX")
        uidvalidity = info.get(b"UIDVALIDITY", 0) if isinstance(info, dict) else 0
        return self._fetch_new(client, "INBOX", uidvalidity)

    def _process_ops(self, client, folders: dict) -> None:
        """Execute pending ops (delete/spam) from db against the server.

        folders: dict mapping canonical -> server name (from _resolve_folders).
        """
        ops = self.db.take_ops(self.account.id)
        for op_dict in ops:
            op = op_dict["op"]
            source_canonical = op_dict["folder"]
            uid = op_dict["uid"]
            try:
                if op == "create_folder":
                    # 'folder' holds the new folder name; uid is unused.
                    try:
                        client.create_folder(source_canonical)
                    except Exception:
                        pass  # already exists / server refused — non-fatal
                    continue
                if op == "delete_folder":
                    try:
                        client.delete_folder(source_canonical)
                    except Exception:
                        pass
                    continue
                if op == "move":
                    # Filter rule: move uid from its source folder to a custom
                    # target folder (the folder name is the server name itself).
                    target = op_dict.get("target") or None
                elif op == "delete":
                    target = folders.get("TRASH")
                elif op == "spam":
                    target = folders.get("SPAM")
                else:
                    log.warning("worker %s: unknown op %s, skipping", self.account.email, op)
                    continue

                if not target:
                    log.warning("worker %s: no server folder for op %s, skipping",
                                self.account.email, op)
                    continue

                # Select source folder
                source_server = "INBOX" if source_canonical == "INBOX" else folders.get(source_canonical)
                if not source_server:
                    log.warning("worker %s: unresolvable source folder %s for op %s uid %s, skipping",
                                self.account.email, source_canonical, op, uid)
                    continue
                client.select_folder(source_server)

                try:
                    client.move([uid], target)
                except Exception:
                    # Fallback: copy + delete + expunge
                    client.copy([uid], target)
                    client.delete_messages([uid])
                    client.expunge()
            except Exception:
                log.exception("worker %s: failed to execute op %s uid %s",
                              self.account.email, op, uid)
                continue

    # ---- thread loop ----

    def stop(self):
        self._stop = True
        # Wake the worker if it is blocked in a socket read (IDLE check/done):
        # shutting the socket down makes that read return/raise at once, so the
        # run loop sees _stop and exits cleanly without needing terminate().
        client = self._client
        if client is not None:
            try:
                client.socket().shutdown(socket.SHUT_RDWR)
            except Exception:
                pass  # socket already closed / not connected — nothing to wake

    def run(self):
        # Defense in depth: even if a blocklisted account slipped past
        # start_account (e.g. re-added then started directly), refuse to sync.
        if self.account.email.lower() in self.db.get_blocklist():
            log.warning("worker %s: blocklisted — refusing to sync",
                        self.account.email)
            self.status_changed.emit(self.account.id, "error")
            return
        failures = 0
        while not self._stop:
            try:
                self._session()
                # Session reached "connected" and ended cleanly — reset failure counter
                failures = 0
            except AuthError:
                log.error("worker %s: authentication failed (no password stored)",
                          self.account.email)
                self.status_changed.emit(self.account.id, "error")
                return
            except imapclient.exceptions.LoginError as exc:
                log.error("worker %s: login error: %s", self.account.email, exc)
                self.status_changed.emit(self.account.id, "error")
                return
            except Exception as exc:
                if self._stop:
                    return  # session interrupted by stop() — intentional, not a failure
                failures += 1
                if _server_refused(exc):
                    # The server has blocked us (anti-spam / rate limit).
                    # Retrying every minute only keeps the block in place, so
                    # back off for a long time instead.
                    log.warning("worker %s: server refused the connection, "
                                "retrying in %d min: %s", self.account.email,
                                BLOCKED_BACKOFF_SECONDS // 60, exc)
                    delay = BLOCKED_BACKOFF_SECONDS
                else:
                    log.exception("worker %s: session failed", self.account.email)
                    # Cap the backoff low (60s): this is a desktop client the user
                    # watches live, so a long wait looks "stuck on Reconnecting".
                    delay = min(RECONNECT_DELAY_SECONDS * 2 ** (failures - 1), 60)
                delay += random.uniform(0, 5)
                self.status_changed.emit(self.account.id, "reconnecting")
                elapsed = 0.0
                while elapsed < delay:
                    if self._stop:
                        return
                    time.sleep(1)
                    elapsed += 1

    def _connect(self) -> IMAPClient:
        client = IMAPClient(self.account.imap_host, port=self.account.imap_port,
                            ssl=True, ssl_context=nethttp.ssl_context())
        if self.account.provider == "outlook":
            token = oauth.get_token(self.account.email)
            if not token:
                self.status_changed.emit(self.account.id, "error")
                log.error("no valid Microsoft token for %s", self.account.email)
                raise AuthError("microsoft re-login required")
            client.oauth2_login(self.account.email, token)
        elif (self.account.provider == "gmail"
              and google_oauth.has_login(self.account.email)):
            token = google_oauth.get_token(self.account.email)
            if not token:
                self.status_changed.emit(self.account.id, "error")
                log.error("no valid Google token for %s", self.account.email)
                raise AuthError("google re-login required")
            client.oauth2_login(self.account.email, token)
        else:
            # gmail without a Google login falls through here — app-password
            # accounts keep working unchanged.
            password = credentials.get_password(self.account.email)
            if not password:
                self.status_changed.emit(self.account.id, "error")
                raise AuthError("no password stored")
            try:
                client.login(self.account.email, password)
            except UnicodeEncodeError:
                raise AuthError("password contains non-ASCII characters — update it in Settings")
        client.select_folder("INBOX")
        return client

    def _session(self):
        client = self._connect()
        self._client = client  # exposed so stop() can interrupt blocking reads
        try:
            self.status_changed.emit(self.account.id, "connected")

            # Resolve server-side folder names once per session
            folders = self._resolve_folders(client)
            custom_folders = self._resolve_custom_folders(client, folders)
            # Persist discovered folders so they show in the sidebar even empty.
            for cf in custom_folders:
                self.db.add_account_folder(self.account.id, cf)

            # Process any pending ops before initial sync (may create folders).
            self._process_ops(client, folders)
            # A create-folder op may have added a folder not in the first listing.
            custom_folders = self._resolve_custom_folders(client, folders)

            # Select INBOX back (process_ops may have changed selection),
            # capturing UIDVALIDITY from the select response
            new, initial = self._sync_inbox(client)
            if new:
                self.new_messages.emit(self.account.id, new, initial)

            for canonical in ("SENT", "TRASH", "SPAM"):
                self._sync_folder(client, canonical, folders.get(canonical))
            # Custom (user-created) folders — stored under their own name.
            for cf in custom_folders:
                self._sync_folder(client, cf, cf)
                # Re-select INBOX for next operation
            info = client.select_folder("INBOX")
            inbox_validity = info.get(b"UIDVALIDITY", 0) if isinstance(info, dict) else 0

            # "auto" mode: one backfill batch right after the first full pass
            # (INBOX is selected, not in IDLE). No-op for other depths.
            backfill_pending = self._backfill_step(client, "INBOX", inbox_validity)

            # One-time: re-fetch Cc/Bcc headers for messages stored before cc
            # parsing existed, so older mail shows Cc like new mail does.
            self._backfill_cc(client, folders, custom_folders)

            while not self._stop:
                client.idle()
                idled = 0.0
                got_event = False
                while idled < IDLE_REFRESH_SECONDS and not self._stop:
                    if client.idle_check(timeout=IDLE_POLL_SECONDS):
                        got_event = True
                        break
                    idled += IDLE_POLL_SECONDS
                    if self._sync_now:
                        break   # user pressed Sync — leave IDLE and fetch now
                    if backfill_pending:
                        # Quiet 30s poll with backfill remaining: leave IDLE so
                        # we can run one backfill step (search/fetch are illegal
                        # while IDLE is active), then re-enter IDLE.
                        break
                client.idle_done()
                if self._stop:
                    break

                if got_event or self._sync_now or idled >= IDLE_REFRESH_SECONDS:
                    self._sync_now = False
                    # Process pending server ops
                    self._process_ops(client, folders)

                    # Re-select INBOX and sync it (with notification)
                    new, initial = self._sync_inbox(client)
                    if new:
                        self.new_messages.emit(self.account.id, new, initial)

                    # Re-sync other folders (no notification)
                    for canonical in ("SENT", "TRASH", "SPAM"):
                        self._sync_folder(client, canonical, folders.get(canonical))
                    for cf in custom_folders:
                        self._sync_folder(client, cf, cf)

                    # Select INBOX again before entering IDLE
                    info = client.select_folder("INBOX")
                    inbox_validity = (info.get(b"UIDVALIDITY", 0)
                                      if isinstance(info, dict) else 0)
                elif backfill_pending:
                    # Quiet moment: INBOX is selected, IDLE is done — pull one
                    # batch of older history. Backfilled mail never notifies.
                    #
                    # New mail first, though: a backfill batch can occupy the
                    # connection for minutes, and an EXISTS that arrives while a
                    # FETCH is running is consumed by that command's response —
                    # the next idle_check never reports it. Without this poll,
                    # today's mail could sit unsynced until the 25-minute IDLE
                    # refresh (or longer, on a mailbox that backfills for days).
                    new, initial = self._sync_inbox(client)
                    if new:
                        self.new_messages.emit(self.account.id, new, initial)
                    backfill_pending = self._backfill_step(client, "INBOX",
                                                           inbox_validity)
        finally:
            self._client = None
            try:
                client.logout()
            except Exception:
                pass
