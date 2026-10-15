"""Notification settings: on/off, sound on/off, choosable sound.

Pure logic (no widgets) so it stays unit-testable.
"""
from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import QSettings

# macOS built-in system sounds (afplay /System/Library/Sounds/<name>.aiff)
SOUNDS = ["Glass", "Ping", "Pop", "Purr", "Tink", "Hero", "Submarine", "Funk",
          "Basso", "Blow"]

# Where the in-app new-email popup stack sits on screen. Keys are stored in
# QSettings; the UI maps them to human labels.
POPUP_POSITIONS = ("top-right", "top-left", "bottom-right", "bottom-left",
                   "top-center", "bottom-center")


def _as_bool(v, default: bool) -> bool:
    """QSettings stores bools as strings on some backends — normalize."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).lower() in ("true", "1")


class NotifSettings:
    """Read/write notification preferences via QSettings."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def enabled(self) -> bool:
        return _as_bool(self._qs.value("notif_enabled"), True)

    @enabled.setter
    def enabled(self, v: bool) -> None:
        self._qs.setValue("notif_enabled", bool(v))

    @property
    def sound_enabled(self) -> bool:
        return _as_bool(self._qs.value("notif_sound_enabled"), True)

    @sound_enabled.setter
    def sound_enabled(self, v: bool) -> None:
        self._qs.setValue("notif_sound_enabled", bool(v))

    @property
    def sound(self) -> str:
        return str(self._qs.value("notif_sound", "Glass"))

    @sound.setter
    def sound(self, name: str) -> None:
        self._qs.setValue("notif_sound", name)

    @property
    def reminder_minutes(self) -> int:
        """Minutes to wait before re-showing a dismissed-but-still-unread card.
        0 means reminders are off (✕ dismisses for good)."""
        try:
            return max(0, int(self._qs.value("notif_reminder_minutes", 1)))
        except (TypeError, ValueError):
            return 1

    @reminder_minutes.setter
    def reminder_minutes(self, v: int) -> None:
        self._qs.setValue("notif_reminder_minutes", max(0, int(v)))

    @property
    def popup_position(self) -> str:
        """Corner/edge of the screen where the new-email popup stack appears.
        One of POPUP_POSITIONS; defaults to top-right (the classic spot)."""
        v = str(self._qs.value("notif_popup_position", "top-right"))
        return v if v in POPUP_POSITIONS else "top-right"

    @popup_position.setter
    def popup_position(self, v: str) -> None:
        self._qs.setValue("notif_popup_position",
                          v if v in POPUP_POSITIONS else "top-right")

    @property
    def auto_dismiss_enabled(self) -> bool:
        """When on, a popup card disappears on its own after
        ``auto_dismiss_seconds``. Off by default so cards stay until the user
        clicks or dismisses them."""
        return _as_bool(self._qs.value("notif_auto_dismiss_enabled"), False)

    @auto_dismiss_enabled.setter
    def auto_dismiss_enabled(self, v: bool) -> None:
        self._qs.setValue("notif_auto_dismiss_enabled", bool(v))

    @property
    def auto_dismiss_seconds(self) -> int:
        """How long a popup card stays before auto-dismissing (when enabled).
        Clamped to 3..600 seconds; defaults to 10."""
        try:
            return max(3, min(600, int(self._qs.value("notif_auto_dismiss_seconds", 10))))
        except (TypeError, ValueError):
            return 10

    @auto_dismiss_seconds.setter
    def auto_dismiss_seconds(self, v: int) -> None:
        self._qs.setValue("notif_auto_dismiss_seconds",
                          max(3, min(600, int(v))))


class SendSettings:
    """Compose/send preferences (Gmail-style Undo Send delay)."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def undo_send_seconds(self) -> int:
        """Seconds an email waits in the Outbox before it is actually sent, so
        the user can Recall/Undo it. 0 = send immediately (no undo window).
        Capped at 60s."""
        try:
            return max(0, min(60, int(self._qs.value("undo_send_seconds", 15))))
        except (TypeError, ValueError):
            return 15

    @undo_send_seconds.setter
    def undo_send_seconds(self, v: int) -> None:
        self._qs.setValue("undo_send_seconds", max(0, min(60, int(v))))


class ReaderSettings:
    """Reader preferences (remote-image loading / privacy)."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def load_remote_images(self) -> bool:
        """Whether the reader loads remote (http) images in emails. On by default
        so marketing/hosted images show; turning it off blocks tracking pixels
        (senders can't see that the mail was opened or your IP)."""
        return _as_bool(self._qs.value("load_remote_images"), True)

    @load_remote_images.setter
    def load_remote_images(self, v: bool) -> None:
        self._qs.setValue("load_remote_images", bool(v))


class ComposeSettings:
    """Preferences for writing mail."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def spell_check(self) -> bool:
        """Underline misspelled words while composing. On by default; people
        who write in a language we have no dictionary for turn it off once and
        it stays off."""
        return _as_bool(self._qs.value("spell_check"), True)

    @spell_check.setter
    def spell_check(self, v: bool) -> None:
        self._qs.setValue("spell_check", bool(v))


class AppearanceSettings:
    """Look-and-feel preferences that aren't the theme itself."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def show_email(self) -> bool:
        """Sidebar account rows show the email address instead of the display
        name. Off by default (show the friendly name)."""
        return _as_bool(self._qs.value("sidebar_show_email"), False)

    @show_email.setter
    def show_email(self, v: bool) -> None:
        self._qs.setValue("sidebar_show_email", bool(v))


BACKUP_FREQUENCIES = ("startup", "6h", "daily")


class BackupSettings:
    """Read/write backup preferences via QSettings (same file as NotifSettings)."""

    def __init__(self, qs: QSettings | None = None):
        self._qs = qs if qs is not None else QSettings("AMail", "ui")

    @property
    def frequency(self) -> str:
        v = str(self._qs.value("backup_freq", "daily"))
        return v if v in BACKUP_FREQUENCIES else "daily"

    @frequency.setter
    def frequency(self, v: str) -> None:
        self._qs.setValue("backup_freq", v if v in BACKUP_FREQUENCIES else "daily")

    @property
    def folder(self) -> str:
        """Where backups are written. Defaults to <data_dir>/backups."""
        from . import config
        default = str(config.data_dir() / "backups")
        return str(self._qs.value("backup_folder", default) or default)

    @folder.setter
    def folder(self, path: str) -> None:
        self._qs.setValue("backup_folder", path)

    @property
    def last_run_epoch(self) -> float:
        try:
            return float(self._qs.value("backup_last_run", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @last_run_epoch.setter
    def last_run_epoch(self, v: float) -> None:
        self._qs.setValue("backup_last_run", float(v))


# Map our sound names to Windows system sound aliases (closest match).
_WIN_SOUND_ALIASES = {
    "Glass": "SystemAsterisk", "Ping": "SystemAsterisk",
    "Pop": "SystemDefault", "Purr": "SystemDefault",
    "Tink": "SystemAsterisk", "Hero": "SystemExclamation",
    "Submarine": "SystemNotification", "Funk": "SystemHand",
    "Basso": "SystemHand", "Blow": "SystemExclamation",
}


def play_sound(name: str) -> None:
    """Play a notification sound. macOS: system .aiff; Windows: winsound
    system alias. No-op on other platforms / bad names."""
    try:
        if sys.platform == "darwin":
            path = f"/System/Library/Sounds/{name}.aiff"
            if os.path.exists(path):
                subprocess.Popen(["afplay", path])
        elif sys.platform == "win32":
            import winsound
            alias = _WIN_SOUND_ALIASES.get(name, "SystemNotification")
            winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass
