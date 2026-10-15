from __future__ import annotations

from pathlib import Path

APP_NAME = "AMail"


def data_dir() -> Path:
    d = Path.home() / ".amail"
    d.mkdir(parents=True, exist_ok=True)
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
