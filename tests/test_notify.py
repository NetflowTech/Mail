"""Notification text from an email must not be able to run anything.

On Windows, winotify formats the title and body into a PowerShell
double-quoted here-string and runs it, so a subject with a '$(...)'
subexpression would execute on arrival of the email. windows_safe_text is
AMail's own guard; these tests exercise it directly, since winotify itself
only runs on Windows.
"""
from __future__ import annotations

import pytest

from emailclient import notify
from emailclient.notify import windows_safe_text


@pytest.mark.parametrize("subject", [
    "Invoice $(Get-Date)",                       # subexpression
    "Hello $env:USERNAME",                       # variable expansion
    "Tick `n here",                              # backtick escape
    "end\"@\n$x\n@\"start",                      # here-string delimiters on their own lines
    "a\r\n\"@\r\nb",
    "\x00\x07\x1b[31m red",                      # control characters
])
def test_powershell_expansion_cannot_survive(subject):
    out = windows_safe_text(subject)
    assert "$" not in out and "`" not in out
    assert '"@' not in out and '@"' not in out
    assert all(ch == " " or ch.isprintable() for ch in out)


def test_ordinary_text_is_kept():
    assert windows_safe_text("Re: Quote #4411 (urgent!) — 50% off, €1,000") == \
        "Re: Quote #4411 (urgent!) — 50% off, €1,000"
    assert windows_safe_text("Fatura ödeme 請求書 счёт") == "Fatura ödeme 請求書 счёт"
    assert windows_safe_text("me@example.com") == "me@example.com"
    assert windows_safe_text(None) == ""


def test_windows_path_sanitizes_before_winotify(monkeypatch):
    """The Windows branch hands winotify the cleaned text, never the raw."""
    import sys
    import types
    shown = []

    class Notification:
        def __init__(self, app_id, title, msg):
            shown.append((title, msg))

        def show(self):
            pass
    monkeypatch.setitem(sys.modules, "winotify", types.SimpleNamespace(Notification=Notification))
    monkeypatch.setattr(notify.sys, "platform", "win32")
    notify.send_blocking("📧 $(whoami)", "Invoice `$x\"@")
    assert shown == [("📧 (whoami)", "Invoice x'@")]
