"""Guards for file paths that untrusted email content can hand the app.

An email can name any path it likes in an ``<img src>`` or a link: a UNC
share (``\\\\host\\share\\x.png`` or ``file://host/share/x.png``) that makes
Windows send the user's NTLM login hash to the host the moment anything
touches it, or a local file (``file:///etc/passwd``) that must never be read
and mailed back out. The only files the app ever opens on an email's behalf
are the ones it wrote itself, under its own data folder.
"""
from __future__ import annotations

import os
import re

from PySide6.QtCore import QUrl

from . import config

# Sub-folders of the data dir the app itself writes mail files into: synced
# attachments, copied/attached .eml files, inline images exported on send and
# rendered signature images.
APP_FILE_DIRS = ("attachments", "copied", "inline", "sig_images")

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)
_DRIVE_RE = re.compile(r"^[a-z]:[\\/]", re.IGNORECASE)


def is_network_path(path: str) -> bool:
    """True for UNC / network paths — never touch these, not even to stat."""
    return path.startswith(("\\\\", "//"))


def local_path_from_src(src: str) -> str | None:
    """Turn an image/link source into a candidate local path.

    ``file:`` URLs become their local path (a ``file://host/…`` URL keeps its
    host as a ``//host/…`` prefix, which :func:`is_network_path` then rejects);
    bare paths pass through. Any other scheme (data:, http:, cid:, …) is not a
    local file and gives None.
    """
    s = (src or "").strip()
    if not s:
        return None
    if s.lower().startswith("file:"):
        return QUrl(s).toLocalFile() or None
    if _SCHEME_RE.match(s) and not _DRIVE_RE.match(s):
        return None
    return s


def safe_app_file(src: str) -> str | None:
    """Return the real path of *src* if it is an existing file inside one of
    the app's own mail folders, else None. Network paths are refused before
    any filesystem call is made.
    """
    path = local_path_from_src(src)
    if not path or is_network_path(path):
        return None
    real = os.path.realpath(path)
    if is_network_path(real) or not os.path.isfile(real):
        return None
    for name in APP_FILE_DIRS:
        root = os.path.realpath(config.data_dir() / name)
        # The separator matters: "attachments_evil" must not pass as
        # "attachments".
        if real == root or real.startswith(root + os.sep):
            return real
    return None
