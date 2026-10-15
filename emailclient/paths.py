"""Asset path resolution that works both from source and frozen (PyInstaller)."""
from __future__ import annotations

import sys
from pathlib import Path


def asset_dir() -> Path:
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"
