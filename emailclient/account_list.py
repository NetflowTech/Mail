"""Rows for the Settings → License → "Export list" spreadsheet.

Pure data shaping (no Qt) so it is unit-testable: given this PC's accounts and
the live sync engine, produce one row per account with the computer name, email,
provider, date added and status. The spreadsheet itself is written by xlsx.py.
"""
from __future__ import annotations

import socket
from datetime import datetime

from . import __version__

HEADERS = ["#", "Computer name", "AMail version", "Email address", "Mail provider",
           "Date added", "Status"]
COL_WIDTHS = [5, 24, 14, 38, 20, 20, 16]

PROVIDER_LABELS = {
    "gmail": "Gmail", "outlook": "Outlook / Hotmail", "yahoo": "Yahoo",
    "zoho": "Zoho", "custom": "Other (IMAP)",
}

UNKNOWN_ADDED = "Unknown (added before this update)"


def computer_name() -> str:
    try:
        return socket.gethostname() or "PC"
    except Exception:  # noqa: BLE001
        return "PC"


def provider_label(account) -> str:
    key = (getattr(account, "provider", "") or "").lower()
    label = PROVIDER_LABELS.get(key)
    if label is None:
        label = key.capitalize() or "Unknown"
    if key == "custom" and getattr(account, "imap_host", ""):
        label = f"{label} — {account.imap_host}"
    return label


def added_label(account) -> str:
    """DB stores UTC ISO-8601; show local wall-clock time, minute precision."""
    raw = getattr(account, "added_at", "") or ""
    if not raw:
        return UNKNOWN_ADDED
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%Y-%m-%d %H:%M")


def status_label(account, engine=None) -> str:
    """Mirror MainWindow._account_status, in words a non-developer reads:
    Paused (by the user) > Login failed (needs re-authentication) > Active
    (sync worker running) > Not syncing (worker stopped). Without an engine
    (dialog opened standalone) only Paused/Active can be known."""
    if getattr(account, "paused", False):
        return "Paused"
    if engine is None:
        return "Active"
    if account.id in getattr(engine, "auth_failed", set()):
        return "Login failed"
    worker = getattr(engine, "workers", {}).get(account.id)
    try:
        running = bool(worker and worker.isRunning())
    except Exception:  # noqa: BLE001
        running = False
    return "Active" if running else "Not syncing"


def build_rows(accounts, engine=None, pc_name: str | None = None) -> list[list]:
    pc = pc_name if pc_name is not None else computer_name()
    return [[i, pc, __version__, a.email, provider_label(a), added_label(a),
             status_label(a, engine)]
            for i, a in enumerate(accounts, 1)]
