"""Tests for emailclient.eml — .eml serialisation and filename utilities."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from emailclient.eml import (
    safe_filename,
    is_eml,
    build_eml,
    write_eml,
    _attachment_items,
)
from emailclient.models import EmailMessage


def _msg(**kwargs) -> EmailMessage:
    defaults = dict(
        account_id=1,
        folder="INBOX",
        uid=1,
        subject="Test",
        sender="alice@example.com",
        recipients="bob@example.com",
        date="2024-01-15T10:30:00+00:00",
        body_text="Hello.",
        body_html="<p>Hello.</p>",
    )
    defaults.update(kwargs)
    return EmailMessage(**defaults)


# ---------------------------------------------------------------------------
# safe_filename
# ---------------------------------------------------------------------------

class TestSafeFilename:
    def test_normal_subject(self):
        assert safe_filename("Meeting notes") == "Meeting notes.eml"

    def test_unsafe_chars_replaced(self):
        name = safe_filename('Hello: "World" / <Today>')
        assert "/" not in name
        assert "<" not in name
        assert ">" not in name
        assert '"' not in name
        assert ":" not in name

    def test_empty_subject_fallback(self):
        assert safe_filename("") == "message.eml"

    def test_none_fallback(self):
        assert safe_filename(None) == "message.eml"

    def test_max_length_64(self):
        long = "A" * 100
        result = safe_filename(long)
        stem = result[: -len(".eml")]
        assert len(stem) <= 64

    def test_ends_with_eml(self):
        assert safe_filename("Report").endswith(".eml")

    def test_dots_only_fallback(self):
        assert safe_filename("...") == "message.eml"

    def test_whitespace_stripped(self):
        name = safe_filename("  Report  ")
        assert not name.startswith(" ")


# ---------------------------------------------------------------------------
# is_eml
# ---------------------------------------------------------------------------

class TestIsEml:
    def test_eml_extension(self):
        assert is_eml("email.eml") is True

    def test_msg_eml_extension(self):
        assert is_eml("saved.msg.eml") is True

    def test_txt_extension(self):
        assert is_eml("text.txt") is False

    def test_pdf_extension(self):
        assert is_eml("doc.pdf") is False

    def test_case_insensitive(self):
        assert is_eml("EMAIL.EML") is True

    def test_empty_string(self):
        assert is_eml("") is False

    def test_none(self):
        assert is_eml(None) is False


# ---------------------------------------------------------------------------
# _attachment_items
# ---------------------------------------------------------------------------

class TestAttachmentItems:
    def test_list_format(self):
        j = json.dumps([{"path": "/tmp/a.pdf"}, {"path": "/tmp/b.pdf"}])
        items = _attachment_items(j)
        assert len(items) == 2
        assert items[0]["path"] == "/tmp/a.pdf"

    def test_dict_format_files_and_inline(self):
        data = {
            "files": ["/tmp/a.pdf"],
            "inline": [{"path": "/tmp/img.png", "cid": "logo"}],
        }
        items = _attachment_items(json.dumps(data))
        paths = [i["path"] for i in items]
        assert "/tmp/a.pdf" in paths
        assert "/tmp/img.png" in paths

    def test_string_items_in_list(self):
        j = json.dumps(["/tmp/a.pdf", "/tmp/b.pdf"])
        items = _attachment_items(j)
        assert items[0]["path"] == "/tmp/a.pdf"

    def test_empty_json(self):
        assert _attachment_items("[]") == []

    def test_empty_string(self):
        assert _attachment_items("") == []

    def test_none(self):
        assert _attachment_items(None) == []

    def test_malformed_json(self):
        assert _attachment_items("{{{bad}}}") == []


# ---------------------------------------------------------------------------
# build_eml
# ---------------------------------------------------------------------------

class TestBuildEml:
    def test_returns_bytes(self):
        raw = build_eml(_msg())
        assert isinstance(raw, bytes)

    def test_subject_in_output(self):
        raw = build_eml(_msg(subject="Meeting"))
        assert b"Meeting" in raw

    def test_from_header(self):
        raw = build_eml(_msg(sender="alice@example.com"))
        assert b"alice@example.com" in raw

    def test_to_header(self):
        raw = build_eml(_msg(recipients="bob@example.com"))
        assert b"bob@example.com" in raw

    def test_cc_header_when_set(self):
        raw = build_eml(_msg(cc="cc@example.com"))
        assert b"cc@example.com" in raw

    def test_no_cc_when_empty(self):
        raw = build_eml(_msg(cc=""))
        assert b"Cc:" not in raw

    def test_message_id_included(self):
        raw = build_eml(_msg(message_id="<uid@domain.com>"))
        assert b"<uid@domain.com>" in raw

    def test_plain_body(self):
        raw = build_eml(_msg(body_text="Plain content here."))
        assert b"Plain content here." in raw

    def test_html_body(self):
        raw = build_eml(_msg(body_html="<b>Bold</b>"))
        assert b"<b>Bold</b>" in raw

    def test_attachment_included(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"PDF_CONTENT")
        msg = _msg(attachments=json.dumps([{"path": str(f), "name": "doc.pdf"}]))
        raw = build_eml(msg)
        assert b"doc.pdf" in raw

    def test_missing_attachment_skipped(self, tmp_path):
        missing = str(tmp_path / "gone.pdf")
        msg = _msg(attachments=json.dumps([{"path": missing, "name": "gone.pdf"}]))
        raw = build_eml(msg)
        assert isinstance(raw, bytes)  # did not raise

    def test_invalid_date_falls_back(self):
        raw = build_eml(_msg(date="NOT A DATE"))
        assert b"Date:" in raw

    def test_empty_attachments_field(self):
        raw = build_eml(_msg(attachments=""))
        assert isinstance(raw, bytes)


# ---------------------------------------------------------------------------
# write_eml
# ---------------------------------------------------------------------------

class TestWriteEml:
    def test_file_created(self, tmp_path):
        msg = _msg(subject="My Report")
        path = write_eml(msg, tmp_path)
        assert Path(path).is_file()

    def test_filename_based_on_subject(self, tmp_path):
        msg = _msg(subject="Budget 2024")
        path = write_eml(msg, tmp_path)
        assert "Budget 2024" in Path(path).name

    def test_no_collision_same_subject(self, tmp_path):
        msg = _msg(subject="Duplicate")
        p1 = write_eml(msg, tmp_path)
        p2 = write_eml(msg, tmp_path)
        assert p1 != p2
        assert Path(p1).is_file()
        assert Path(p2).is_file()

    def test_creates_directory(self, tmp_path):
        subdir = tmp_path / "a" / "b" / "c"
        msg = _msg(subject="Nested")
        write_eml(msg, subdir)
        assert subdir.is_dir()

    def test_file_content_is_valid_eml(self, tmp_path):
        msg = _msg(subject="Valid", body_text="Check")
        path = write_eml(msg, tmp_path)
        content = Path(path).read_bytes()
        assert b"Subject: Valid" in content
        assert b"Check" in content
