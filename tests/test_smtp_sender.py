"""Tests for emailclient.smtp_sender — MIME building and address parsing."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from emailclient.smtp_sender import parse_rcpts, build_mime
from emailclient.models import Account


def _account(provider="gmail", port=465):
    return Account(
        id=1, name="Alice Test", email="alice@example.com",
        provider=provider,
        imap_host="imap.gmail.com", imap_port=993,
        smtp_host="smtp.gmail.com", smtp_port=port,
        color="#DB4437",
    )


# ---------------------------------------------------------------------------
# parse_rcpts
# ---------------------------------------------------------------------------

class TestParseRcpts:
    def test_single_address(self):
        assert parse_rcpts("bob@example.com") == ["bob@example.com"]

    def test_comma_separated(self):
        result = parse_rcpts("a@a.com, b@b.com, c@c.com")
        assert result == ["a@a.com", "b@b.com", "c@c.com"]

    def test_semicolon_separated(self):
        result = parse_rcpts("a@a.com; b@b.com")
        assert result == ["a@a.com", "b@b.com"]

    def test_mixed_delimiters(self):
        result = parse_rcpts("a@a.com; b@b.com, c@c.com")
        assert result == ["a@a.com", "b@b.com", "c@c.com"]

    def test_empty_string(self):
        assert parse_rcpts("") == []

    def test_none_skipped(self):
        assert parse_rcpts(None) == []

    def test_multiple_fields(self):
        result = parse_rcpts("a@a.com", "b@b.com")
        assert result == ["a@a.com", "b@b.com"]

    def test_strips_whitespace(self):
        result = parse_rcpts("  a@a.com  ,  b@b.com  ")
        assert result == ["a@a.com", "b@b.com"]

    def test_empty_parts_dropped(self):
        result = parse_rcpts("a@a.com,,b@b.com")
        assert result == ["a@a.com", "b@b.com"]

    def test_display_name_preserved(self):
        addr = '"Bob Smith" <bob@example.com>'
        result = parse_rcpts(addr)
        assert result == [addr]

    def test_no_fields_returns_empty(self):
        assert parse_rcpts() == []


# ---------------------------------------------------------------------------
# build_mime
# ---------------------------------------------------------------------------

class TestBuildMime:
    def test_from_header(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "Hi", "Body", [])
        assert "alice@example.com" in msg["From"]
        assert "Alice Test" in msg["From"]

    def test_to_header(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "Hi", "Body", [])
        assert msg["To"] == "bob@example.com"

    def test_subject_header(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "My Subject", "Body", [])
        assert msg["Subject"] == "My Subject"

    def test_cc_header_set(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "cc@example.com", "S", "B", [])
        assert msg["Cc"] == "cc@example.com"

    def test_no_cc_header_when_empty(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "B", [])
        assert msg["Cc"] is None

    def test_message_id_generated(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "B", [])
        assert msg["Message-ID"] is not None
        assert "@" in msg["Message-ID"]

    def test_message_id_uses_domain(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "B", [])
        assert "example.com" in msg["Message-ID"]

    def test_date_header_present(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "B", [])
        assert msg["Date"] is not None

    def test_plain_body_content(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "Hello body!", [])
        assert "Hello body!" in msg.as_string()

    def test_html_body_included(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "plain",
                         [], body_html="<b>bold</b>")
        content = msg.as_string()
        assert "<b>bold</b>" in content

    def test_attachment_included(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"PDF_DATA")
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "body",
                         [str(f)])
        assert "doc.pdf" in msg.as_string()

    def test_missing_attachment_skipped(self, tmp_path):
        acct = _account()
        # Should not raise even if file is gone
        msg = build_mime(acct, "bob@example.com", "", "S", "body",
                         [str(tmp_path / "nonexistent.pdf")])
        assert msg is not None

    def test_multiple_attachments(self, tmp_path):
        files = []
        for i in range(3):
            f = tmp_path / f"file{i}.txt"
            f.write_bytes(f"content{i}".encode())
            files.append(str(f))
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "S", "body", files)
        raw = msg.as_string()
        for i in range(3):
            assert f"file{i}.txt" in raw

    def test_empty_subject(self):
        acct = _account()
        msg = build_mime(acct, "bob@example.com", "", "", "body", [])
        assert msg["Subject"] == ""
