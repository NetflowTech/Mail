from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from .config import data_dir

log = logging.getLogger(__name__)

# File the app watches (QFileSystemWatcher); clicking a notification writes
# the message id here so the GUI can jump straight to that email.
CLICK_FILE = data_dir() / "notify_click"

_TERMINAL_NOTIFIER = shutil.which("terminal-notifier") or (
    "/opt/homebrew/bin/terminal-notifier"
    if os.path.exists("/opt/homebrew/bin/terminal-notifier") else None)


def windows_safe_text(text: str) -> str:
    """Notification text (a sender name or a subject, so chosen by whoever
    sent the email) made safe to hand to winotify on Windows.

    winotify shows a banner by string-formatting the title and body into a
    PowerShell script, inside a double-quoted here-string (@"..."@), and
    running it with powershell.exe. PowerShell expands $variables and $(...)
    subexpressions in such a string before anything else sees it, so a
    subject containing one would run as PowerShell the moment the banner
    appeared, with no click. The CDATA wrapper around the text doesn't help;
    PowerShell evaluates the here-string before the XML is parsed.

    So nothing that can start an expansion or end the string gets through:
    '$' and the backtick are dropped, the here-string delimiters '"@' and
    '@"' are broken up, and control characters go (a newline could put '"@'
    at the start of a line). Ordinary punctuation and non-Latin text stay."""
    text = "".join(ch for ch in (text or "")
                   if ch == " " or (ch.isprintable() and ch not in "$`"))
    return text.replace('"@', "'@").replace('@"', "@'")


def _send_macos_native(title: str, body: str) -> None:
    """Send a banner notification via PyObjC UNUserNotificationCenter.

    Falls back to osascript if the framework is unavailable. Using
    UNUserNotificationCenter (the modern macOS API) ensures the notification
    always appears as a Banner — never as a blocking Alert dialog — regardless
    of how macOS has the app's notification style configured for osascript.
    """
    try:
        from UserNotifications import (  # type: ignore[import]
            UNUserNotificationCenter,
            UNMutableNotificationContent,
            UNNotificationRequest,
        )
        import uuid
        center = UNUserNotificationCenter.currentNotificationCenter()
        content = UNMutableNotificationContent.alloc().init()
        content.setTitle_(title)
        content.setBody_(body)
        req = UNNotificationRequest.requestWithIdentifier_content_trigger_(
            str(uuid.uuid4()), content, None)
        center.addNotificationRequest_withCompletionHandler_(req, None)
        return
    except Exception:
        pass
    # Fallback: osascript display notification (banner, not alert dialog).
    # Values passed via env so untrusted email content never appears in the
    # AppleScript source text — no injection possible.
    script = ('display notification (system attribute "NOTIF_BODY") '
              'with title (system attribute "NOTIF_TITLE")')
    env = {**os.environ, "NOTIF_TITLE": title, "NOTIF_BODY": body}
    subprocess.run(["osascript", "-e", script], env=env, check=False, timeout=5)


# One background worker delivers every notification. Showing a native banner
# costs real time — a macOS subprocess is ~230 ms, and winotify runs a
# PowerShell script per notification — and this used to happen on the GUI
# thread inside on_new_messages. With a burst of accounts syncing, that is
# seconds of a frozen window ("AMail (Not Responding)").
# One worker, not a pool: banners should still appear in order.
_SENDER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="notify")


def send(title: str, body: str, msg_id: int | None = None,
         sound: str | None = None) -> None:
    """Queue a native OS notification. Returns immediately; never raises."""
    try:
        _SENDER.submit(send_blocking, title, body, msg_id, sound)
    except RuntimeError:      # interpreter shutting down — nothing to show
        pass


def send_blocking(title: str, body: str, msg_id: int | None = None,
                  sound: str | None = None) -> None:
    """Do the actual OS call. Runs off the GUI thread; never raises.

    On macOS prefers terminal-notifier (clickable: writes msg_id to CLICK_FILE
    which the app watches). Falls back to osascript banners.
    `sound` (e.g. "default") makes terminal-notifier play it; None = silent
    (the app may play its own chosen sound via settings.play_sound instead).
    """
    try:
        if sys.platform == "darwin":
            if _TERMINAL_NOTIFIER:
                cmd = [_TERMINAL_NOTIFIER, "-title", title, "-message", body,
                       "-group", f"amail-{msg_id or 'general'}",
                       # Auto-dismiss after 4 s so it never blocks space-switching.
                       # Without this terminal-notifier defaults to Alert style
                       # (persistent, requires acknowledgement) on many systems.
                       "-timeout", "4"]
                if sound:
                    cmd += ["-sound", sound]
                if msg_id is not None and msg_id > 0:
                    # shell-safe: only the integer id is interpolated
                    cmd += ["-execute",
                            f"/bin/sh -c 'echo {int(msg_id)} > \"{CLICK_FILE}\"'"]
                subprocess.run(cmd, check=False, timeout=8)
            else:
                _send_macos_native(title, body)
        elif sys.platform == "win32":
            from winotify import Notification
            # Never hand email text to winotify as it came: see windows_safe_text.
            Notification(app_id="AMail", title=windows_safe_text(title),
                         msg=windows_safe_text(body)).show()
    except Exception:
        pass
