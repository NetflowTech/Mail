"""A crafted attachment filename must never write outside its folder."""
from __future__ import annotations

import json
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path, PureWindowsPath

import pytest

from emailclient.mailparse import _sanitize_filename, parse_message


def _mail(filename: str) -> bytes:
    msg = MIMEMultipart()
    msg["From"] = "bob@example.com"
    msg["To"] = "alice@example.com"
    msg["Subject"] = "Attach"
    msg.attach(MIMEText("See attachment.", "plain"))
    part = MIMEBase("application", "octet-stream")
    part.set_payload(b"payload")
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    return msg.as_bytes()


class TestSanitizeFilename:
    @pytest.mark.parametrize("name,expected", [
        ("D:evil.exe", "D_evil.exe"),
        ("C:evil.exe", "C_evil.exe"),
        ("c:\\Users\\me\\evil.exe", "evil.exe"),
        ("report: Q1.pdf", "report_ Q1.pdf"),
        ("notes.txt:stream", "notes.txt_stream"),
        ("../../etc/passwd", "passwd"),
        ("..\\..\\boot.ini", "boot.ini"),
    ])
    def test_colons_and_separators_are_neutralised(self, name, expected):
        assert _sanitize_filename(name, "fallback") == expected

    @pytest.mark.parametrize("name", ["", ":", "::", ".", "..", "   "])
    def test_empty_results_use_the_fallback(self, name):
        got = _sanitize_filename(name, "fallback")
        assert got == "fallback" or got == "_" * len(name.strip())

    def test_joining_on_windows_stays_inside_the_folder(self):
        # On Windows "D:evil.exe" would make pathlib discard the folder and
        # target drive D. After cleaning, the join must stay put.
        folder = PureWindowsPath(r"C:\Users\me\.amail\attachments\abc")
        for raw in ("D:evil.exe", "C:evil.exe", "\\\\host\\share\\evil.exe"):
            joined = folder / _sanitize_filename(raw, "x")
            assert joined.drive == "C:"
            assert joined.parent == folder


class TestParseWritesInsideFolder:
    def test_drive_letter_name_lands_in_attachment_dir(self, tmp_path):
        msg = parse_message(_mail("D:evil.exe"), 1, "INBOX", 1,
                            attachment_dir=tmp_path)
        items = json.loads(msg.attachments)
        assert len(items) == 1
        path = Path(items[0]["path"])
        assert path.resolve().is_relative_to(tmp_path.resolve())
        assert path.name == "D_evil.exe"
        assert path.read_bytes() == b"payload"
        # The display name keeps what the sender called it.
        assert items[0]["name"] == "D:evil.exe"

    def test_write_is_skipped_if_destination_escapes(self, tmp_path, monkeypatch):
        # Even if a future cleaner bug let a bad name through, nothing is
        # written outside the per-attachment folder.
        import emailclient.mailparse as mp
        monkeypatch.setattr(mp, "_sanitize_filename",
                            lambda name, fb: "../escaped.bin")
        msg = parse_message(_mail("anything.bin"), 1, "INBOX", 1,
                            attachment_dir=tmp_path)
        assert not (tmp_path / "escaped.bin").exists()
        assert not list(tmp_path.rglob("escaped.bin"))
        assert msg.attachments in ("", "[]")
