from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "AMail"

_private_checked: set[str] = set()


def _make_private(d: Path) -> None:
    """Owner-only access (0700) on Mac/Linux: the folder holds every email in
    plain SQLite, and the default umask leaves it readable by other users of
    the computer. Windows already keeps a user's home folder private.
    Checked once per process; a folder that can't be changed is left as is."""
    key = str(d)
    if key in _private_checked:
        return
    _private_checked.add(key)
    if os.name == "nt":
        return
    try:
        if d.stat().st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass


def data_dir() -> Path:
    d = Path.home() / ".amail"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    _make_private(d)
    return d


def db_path() -> Path:
    return data_dir() / "mail.db"


# Provider presets for the Add Account dialog
PROVIDERS = {
    "gmail": {"imap_host": "imap.gmail.com", "imap_port": 993,
              "smtp_host": "smtp.gmail.com", "smtp_port": 465, "color": "#DB4437"},
    "zoho":  {"imap_host": "imap.zoho.com", "imap_port": 993,
              "smtp_host": "smtp.zoho.com", "smtp_port": 465, "color": "#E42527"},
    "outlook": {"imap_host": "outlook.office365.com", "imap_port": 993,
                "smtp_host": "smtp.office365.com", "smtp_port": 587, "color": "#0078D4"},
    "yahoo":   {"imap_host": "imap.mail.yahoo.com", "imap_port": 993,
                "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "color": "#6001D2"},
    "custom":  {"imap_host": "", "imap_port": 993,
                "smtp_host": "", "smtp_port": 465, "color": "#6B7FA3"},
}
