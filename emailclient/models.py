from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Account:
    id: int | None
    name: str
    email: str
    provider: str          # "gmail" | "zoho" | "outlook"
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    color: str             # hex color badge for unified inbox
    signature: str = ""    # per-account email signature (appended LAST for positional compat)
    group_id: int | None = None  # FK → groups.id; NULL = ungrouped
    signature_image: str = ""    # path to per-account signature image (appended last)
    signature_image_width: int = 0  # 0 means default 320px
    sync_depth: str = "50"  # "50" | "30d" | "90d" | "365d" | "all" — initial fetch depth
    paused: bool = False   # True = sync paused by user (worker not started)
    signature_html: str = ""  # rich (HTML) signature; empty = use plain `signature`
    attachments_only: bool = False  # True = only save new INBOX emails that have attachments
    notify_enabled: bool = True     # False = no toast/native/sound for this account


@dataclass
class EmailMessage:
    account_id: int
    folder: str
    uid: int
    subject: str
    sender: str
    recipients: str
    date: str              # ISO 8601
    body_text: str
    body_html: str
    is_read: bool = False
    id: int | None = None
    attachments: str = ""  # JSON list of {"name","path","size"}; "" or "[]" = none
    cc: str = ""           # Cc header (incoming) / Cc line (sent copy)
    bcc: str = ""          # Bcc — only known for our own sent copies
    flagged: bool = False  # user star/flag (app-local, Outlook-style)
    pinned: bool = False   # pinned to the top of the list (app-local)
    flag_done: bool = False  # follow-up marked complete → green ✓ (Outlook-style)
    message_id: str = ""   # RFC 5322 Message-ID — identity across copies
