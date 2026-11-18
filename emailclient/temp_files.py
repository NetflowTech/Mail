"""Short-lived copies of emails written to disk, and their cleanup.

The reader shows each email from an HTML file (large emails don't load any
other way) and Print renders the email to a PDF first. Those files are plain
copies of the email, so they are deleted as soon as they are no longer shown,
when AMail quits, and on the next start for any a crash left behind.
"""
from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path

PATTERNS = ("reader_view_*.html", "print_*.pdf")

_live: set[str] = set()
_lock = threading.Lock()


def track(path) -> None:
    """Remember *path* so it is deleted when AMail quits."""
    with _lock:
        _live.add(str(path))


def remove(path) -> None:
    """Delete *path* now (never raises)."""
    with _lock:
        _live.discard(str(path))
    try:
        os.unlink(path)
    except OSError:
        pass


def _remove_all_live() -> None:
    with _lock:
        paths = list(_live)
        _live.clear()
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass


atexit.register(_remove_all_live)


def clean_leftovers(folder: Path | None = None) -> int:
    """Delete copies left by an earlier run. Called once at startup, before any
    reader exists. Returns how many files were removed."""
    if folder is None:
        from .config import data_dir
        folder = data_dir()
    n = 0
    for pattern in PATTERNS:
        for p in Path(folder).glob(pattern):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n
