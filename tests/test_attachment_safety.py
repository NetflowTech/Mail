"""Attachments from email: programs never launch from AMail, and every
attachment on disk carries the OS's "came from the internet" tag."""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import sys

import pytest

from emailclient import attachment_safety
from emailclient.mailparse import parse_message

from test_attachment_names import _mail


class TestIsBlocked:
    @pytest.mark.parametrize("name", [
        "Invoice.exe", "Receipt.vbs", "run.bat", "x.cmd", "x.ps1", "x.scr",
        "x.hta", "x.msi", "x.js", "x.lnk", "x.iso", "x.img", "x.reg",
        "x.jar", "x.chm", "x.com", "x.command", "x.pkg", "x.app",
        "INVOICE.EXE",               # case doesn't matter
        "Invoice.pdf.exe",           # only the last extension counts
        "Invoice.exe.", "Invoice.exe ", "Invoice.exe. . ",  # Windows trims these
    ])
    def test_programs_scripts_and_shortcuts_are_blocked(self, name):
        assert attachment_safety.is_blocked(name)

    @pytest.mark.parametrize("name", [
        "report.docx", "sheet.xlsx", "macro.xlsm", "deck.pptx", "scan.pdf",
        "photo.jpg", "archive.zip", "page.html", "notes.txt", "data.csv",
        "Invoice.exe.pdf", "no_extension", "",
    ])
    def test_documents_are_not_blocked(self, name):
        assert not attachment_safety.is_blocked(name)


def _quarantine(path: str) -> bytes | None:
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    buf = ctypes.create_string_buffer(256)
    n = libc.getxattr(os.fsencode(path), b"com.apple.quarantine", buf, 256, 0, 0)
    return buf.raw[:n] if n > 0 else None


class TestInternetTag:
    @pytest.mark.skipif(sys.platform != "darwin", reason="macOS quarantine")
    def test_mac_sets_quarantine(self, tmp_path):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"%PDF")
        assert attachment_safety.mark_from_internet(str(f))
        tag = _quarantine(str(f))
        assert tag and tag.startswith(b"0083;") and b";AMail;" in tag
        assert f.read_bytes() == b"%PDF"   # contents untouched

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows Mark of the Web")
    def test_windows_sets_mark_of_the_web(self, tmp_path):
        f = tmp_path / "x.pdf"
        f.write_bytes(b"%PDF")
        assert attachment_safety.mark_from_internet(str(f))
        with open(str(f) + ":Zone.Identifier", encoding="ascii") as zi:
            assert "ZoneId=3" in zi.read()
        assert f.read_bytes() == b"%PDF"

    def test_missing_file_is_ignored(self, tmp_path):
        assert not attachment_safety.mark_from_internet(str(tmp_path / "nope"))

    def test_every_received_attachment_is_tagged(self, tmp_path, monkeypatch):
        import emailclient.mailparse as mp
        tagged = []
        monkeypatch.setattr(mp, "mark_from_internet", tagged.append)
        msg = parse_message(_mail("Invoice.pdf"), 1, "INBOX", 1,
                            attachment_dir=tmp_path)
        path = json.loads(msg.attachments)[0]["path"]
        assert tagged == [path]


class TestPreviewDialog:
    @pytest.fixture()
    def calls(self, monkeypatch):
        """Record what the dialog hands to the OS and what it tells the user."""
        from emailclient.ui import attachment_preview as ap
        rec = {"opened": [], "told": [], "tagged": []}
        monkeypatch.setattr(ap.QDesktopServices, "openUrl",
                            lambda url: rec["opened"].append(
                                os.path.normpath(url.toLocalFile())))
        monkeypatch.setattr(ap.QMessageBox, "information",
                            lambda *a, **k: rec["told"].append(a[2]))
        monkeypatch.setattr(ap.attachment_safety, "mark_from_internet",
                            rec["tagged"].append)
        return rec

    def _dialog(self, qtbot, tmp_path, name, **kw):
        from emailclient.ui import theme
        from emailclient.ui.attachment_preview import AttachmentPreviewDialog
        theme.apply_theme("AMail Light")
        f = tmp_path / name
        f.write_bytes(b"MZ payload")
        dlg = AttachmentPreviewDialog(str(f), **kw)
        qtbot.addWidget(dlg)
        return dlg, os.path.normpath(str(f))

    def test_open_refuses_a_program(self, qtbot, tmp_path, calls):
        dlg, _ = self._dialog(qtbot, tmp_path, "Invoice.exe")
        dlg._open_external()
        assert calls["opened"] == []
        assert "blocked" in calls["told"][0]

    def test_print_never_falls_back_to_running_a_program(self, qtbot, tmp_path, calls):
        dlg, _ = self._dialog(qtbot, tmp_path, "Invoice.bat")
        dlg._print()
        assert calls["opened"] == []
        assert "can't print" in calls["told"][0]

    def test_print_still_hands_a_word_file_to_its_app(self, qtbot, tmp_path, calls):
        dlg, path = self._dialog(qtbot, tmp_path, "report.docx")
        dlg._print()
        assert calls["opened"] == [path]
        assert calls["tagged"] == [path]

    def test_open_tags_a_received_document_first(self, qtbot, tmp_path, calls):
        dlg, path = self._dialog(qtbot, tmp_path, "sheet.xlsx")
        dlg._open_external()
        assert calls["tagged"] == [path]
        assert calls["opened"] == [path]

    def test_open_leaves_the_users_own_file_untagged(self, qtbot, tmp_path, calls):
        # The composer previews files from the user's own disk.
        dlg, path = self._dialog(qtbot, tmp_path, "mine.docx",
                                 allow_download=False)
        dlg._open_external()
        assert calls["tagged"] == []
        assert calls["opened"] == [path]
