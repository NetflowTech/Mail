"""Attachments that came in an email: what AMail will hand to the OS.

Two defences, both modelled on what Outlook does:

  • Blocked types. Programs, scripts, shortcuts and disk images are never
    launched from inside AMail: not by Open, and not by Print falling back to
    the default app. The user can still Download one and run it themselves,
    where the OS's own internet-file warning applies.
  • Internet tag. Every attachment written to disk carries the OS's "came from
    the internet" marker: the Mark of the Web (Zone.Identifier, ZoneId=3) on
    Windows, the quarantine flag on macOS. Windows SmartScreen and Office's
    Protected View, and macOS Gatekeeper, then treat it as a download, wherever
    the file is opened from later.
"""
from __future__ import annotations

import os
import sys
import time

# Outlook's Level 1 list (files it refuses to open), plus disk images and
# archives that mount or unpack without carrying the internet tag to their
# contents, plus the macOS equivalents of a program or script.
BLOCKED_EXTENSIONS = frozenset({
    # Windows programs, installers and libraries
    ".exe", ".com", ".scr", ".pif", ".msi", ".msp", ".mst", ".msix",
    ".msixbundle", ".appx", ".appxbundle", ".appinstaller", ".application",
    ".appref-ms", ".gadget", ".cpl", ".dll", ".ocx", ".sys", ".drv",
    ".msc", ".diagcab", ".xbap", ".vxd", ".ade", ".adp", ".app", ".bas",
    ".crt", ".der", ".fxp", ".hlp", ".ins", ".isp", ".its", ".mad", ".maf",
    ".mag", ".mam", ".maq", ".mar", ".mas", ".mat", ".mau", ".mav", ".maw",
    ".mcf", ".mda", ".mdb", ".mde", ".mdt", ".mdw", ".mdz", ".prf", ".prg",
    ".pst", ".tmp", ".xll", ".xnk",
    # Scripts
    ".bat", ".cmd", ".vb", ".vbs", ".vbe", ".js", ".jse", ".ws", ".wsc",
    ".wsf", ".wsh", ".ps1", ".ps1xml", ".ps2", ".ps2xml", ".psc1", ".psc2",
    ".psd1", ".psm1", ".hta", ".jar", ".py", ".pyc", ".pyw", ".pyz", ".pl",
    ".rb", ".sh", ".scf", ".sct", ".shb", ".shs",
    ".msh", ".msh1", ".msh2", ".mshxml", ".msh1xml", ".msh2xml",
    # Shortcuts and settings that point at, or run, something else
    ".lnk", ".url", ".website", ".inf", ".reg", ".library-ms",
    ".searchconnector-ms", ".settingcontent-ms", ".mof", ".chm", ".cer",
    # Files that connect the PC somewhere, or fetch and run content, the
    # moment they open: remote desktop, Windows Sandbox, Java Web Start,
    # Excel web queries, SYLK, web archives, scriptable SVG, OneNote
    ".rdp", ".wsb", ".jnlp", ".iqy", ".slk", ".mht", ".mhtml", ".svg",
    ".one", ".onepkg",
    # Disk images and archives whose contents escape the internet tag
    ".iso", ".img", ".vhd", ".vhdx", ".udf", ".cab",
    # macOS programs, installers and scripts
    ".command", ".terminal", ".tool", ".pkg", ".mpkg", ".dmg", ".workflow",
    ".action", ".scpt", ".scptd", ".applescript", ".osax", ".prefpane",
    ".webloc", ".inetloc", ".fileloc",
})


def real_extension(path: str) -> str:
    """The extension the OS acts on, lower-cased.

    Windows ignores trailing dots and spaces in a file name, so
    "Invoice.exe." and "Invoice.exe " both run as .exe.
    """
    name = os.path.basename(path or "").rstrip(". ")
    return os.path.splitext(name)[1].lower()


def is_blocked(path: str) -> bool:
    """True when AMail must not launch this file."""
    return real_extension(path) in BLOCKED_EXTENSIONS


# --- internet tag -----------------------------------------------------------

_MOTW = "[ZoneTransfer]\r\nZoneId=3\r\n"
_QUARANTINE_ATTR = b"com.apple.quarantine"


def mark_from_internet(path: str) -> bool:
    """Tag a file as downloaded from the internet. Best effort: a disk that
    can't hold the tag (FAT32, a network share) leaves the file as it was.
    Returns True if the tag was written."""
    try:
        if not os.path.isfile(path):
            return False
        if sys.platform == "win32":
            return _mark_windows(path)
        if sys.platform == "darwin":
            return _mark_mac(path)
    except Exception:
        pass
    return False


def _mark_windows(path: str) -> bool:
    # The tag lives in the file's "Zone.Identifier" alternate data stream.
    # Attachment names never contain ':' (mailparse strips it), so the stream
    # name can't be chosen by the sender.
    with open(path + ":Zone.Identifier", "w", encoding="ascii") as f:
        f.write(_MOTW)
    return True


def _mark_mac(path: str) -> bool:
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    # Flags 0083 as Safari writes them; then a hex timestamp and the agent.
    value = f"0083;{int(time.time()):08x};AMail;".encode("ascii")
    rc = libc.setxattr(os.fsencode(path), _QUARANTINE_ATTR, value,
                       len(value), 0, 0)
    return rc == 0
