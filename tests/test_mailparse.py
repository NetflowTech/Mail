"""Tests for emailclient.mailparse — RFC 822 parsing."""
from __future__ import annotations

import json
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import pytest

from emailclient.mailparse import parse_message, _sanitize_filename


# ---------------------------------------------------------------------------
# Helpers to build raw RFC 822 bytes
# ---------------------------------------------------------------------------

def _simple_mail(subject="Hello", sender="bob@example.com",
                 to="alice@example.com", body="Plain body.",
                 date="Mon, 15 Jan 2024 10:30:00 +0000",
                 message_id="<test@example.com>") -> bytes:
    lines = [
        f"From: {sender}",
        f"To: {to}",
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: {message_id}",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\r\n".join(lines).encode("utf-8")


def _html_mail(subject="Rich", body_text="Plain.", body_html="<b>Bold</b>") -> bytes:
    msg = MIMEMultipart("alternative")
    msg["From"] = "bob@example.com"
    msg["To"] = "alice@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 15 Jan 2024 10:30:00 +0000"
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))
    return msg.as_bytes()


def _mail_with_attachment(subject="Attach", filename="file.txt",
                          content=b"hello") -> bytes:
    msg = MIMEMultipart()
    msg["From"] = "bob@example.com"
    msg["To"] = "alice@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 15 Jan 2024 10:30:00 +0000"
    msg.attach(MIMEText("See attachment.", "plain"))
    part = MIMEBase("application", "octet-stream")
    part.set_payload(content)
    encoders.encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    def test_plain_name(self):
        assert _sanitize_filename("report.pdf", "fallback") == "report.pdf"

    def test_null_bytes_stripped(self):
        assert _sanitize_filename("bad\x00name.txt", "fallback") == "badname.txt"

    def test_path_traversal_unix(self):
        result = _sanitize_filename("../../etc/passwd", "fallback")
        assert result == "passwd"

    def test_path_traversal_windows(self):
        result = _sanitize_filename("..\\..\\system32\\cmd.exe", "fallback")
        assert result == "cmd.exe"

    def test_empty_string_returns_fallback(self):
        assert _sanitize_filename("", "default.bin") == "default.bin"

    def test_dot_only_returns_fallback(self):
        assert _sanitize_filename(".", "default.bin") == "default.bin"

    def test_dotdot_returns_fallback(self):
        assert _sanitize_filename("..", "default.bin") == "default.bin"

    def test_none_returns_fallback(self):
        assert _sanitize_filename(None, "default.bin") == "default.bin"


# ---------------------------------------------------------------------------
# parse_message
# ---------------------------------------------------------------------------

class TestParseMessageBasic:
    def test_subject(self):
        raw = _simple_mail(subject="Test Subject")
        msg = parse_message(raw, 1, "INBOX", 10)
        assert msg.subject == "Test Subject"

    def test_sender(self):
        raw = _simple_mail(sender="bob@example.com")
        msg = parse_message(raw, 1, "INBOX", 10)
        assert "bob@example.com" in msg.sender

    def test_recipients(self):
        raw = _simple_mail(to="alice@example.com")
        msg = parse_message(raw, 1, "INBOX", 10)
        assert "alice@example.com" in msg.recipients

    def test_body_text(self):
        raw = _simple_mail(body="Hello there!")
        msg = parse_message(raw, 1, "INBOX", 10)
        assert "Hello there!" in msg.body_text

    def test_account_id(self):
        raw = _simple_mail()
        msg = parse_message(raw, 7, "INBOX", 10)
        assert msg.account_id == 7

    def test_folder(self):
        raw = _simple_mail()
        msg = parse_message(raw, 1, "Sent", 10)
        assert msg.folder == "Sent"

    def test_uid(self):
        raw = _simple_mail()
        msg = parse_message(raw, 1, "INBOX", 999)
        assert msg.uid == 999

    def test_message_id_extracted(self):
        raw = _simple_mail(message_id="<abc123@example.com>")
        msg = parse_message(raw, 1, "INBOX", 1)
        assert msg.message_id == "<abc123@example.com>"

    def test_date_normalized_to_utc_iso(self):
        raw = _simple_mail(date="Mon, 15 Jan 2024 12:00:00 +0200")
        msg = parse_message(raw, 1, "INBOX", 1)
        # +0200 → UTC → 10:00
        assert "10:00:00" in msg.date
        assert msg.date.endswith("+00:00")

    def test_missing_subject_defaults(self):
        raw = b"From: x@x.com\r\nTo: y@y.com\r\n\r\nBody"
        msg = parse_message(raw, 1, "INBOX", 1)
        assert msg.subject == "(no subject)"

    def test_bad_date_falls_back_to_now(self):
        raw = _simple_mail(date="not a real date")
        msg = parse_message(raw, 1, "INBOX", 1)
        assert "T" in msg.date  # ISO format

    def test_accepts_str_input(self):
        raw = _simple_mail().decode("utf-8")
        msg = parse_message(raw, 1, "INBOX", 1)
        assert msg.subject is not None

    def test_empty_bytes_does_not_raise(self):
        msg = parse_message(b"", 1, "INBOX", 1)
        assert msg is not None

    def test_cc_header(self):
        lines = (
            "From: b@b.com\r\nTo: a@a.com\r\nCc: c@c.com\r\n"
            "Subject: S\r\nDate: Mon, 15 Jan 2024 10:00:00 +0000\r\n\r\nBody"
        )
        msg = parse_message(lines.encode(), 1, "INBOX", 1)
        assert "c@c.com" in msg.cc


class TestParseMessageHTML:
    def test_html_body_extracted(self):
        raw = _html_mail(body_html="<b>Bold</b>")
        msg = parse_message(raw, 1, "INBOX", 1)
        assert "<b>Bold</b>" in msg.body_html

    def test_plain_body_extracted_from_alternative(self):
        raw = _html_mail(body_text="Plain here.", body_html="<p>Plain here.</p>")
        msg = parse_message(raw, 1, "INBOX", 1)
        assert "Plain here." in msg.body_text


class TestParseMessageAttachments:
    def test_no_attachments_without_dir(self):
        raw = _mail_with_attachment()
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=None)
        assert msg.attachments == ""

    def test_attachment_saved_with_dir(self, tmp_path):
        raw = _mail_with_attachment(filename="test.pdf", content=b"PDF content")
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        items = json.loads(msg.attachments)
        assert len(items) == 1
        assert items[0]["name"] == "test.pdf"
        assert Path(items[0]["path"]).read_bytes() == b"PDF content"

    def test_attachment_size_recorded(self, tmp_path):
        content = b"x" * 512
        raw = _mail_with_attachment(filename="data.bin", content=content)
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        items = json.loads(msg.attachments)
        assert items[0]["size"] == 512

    def test_multiple_attachments(self, tmp_path):
        msg_obj = MIMEMultipart()
        msg_obj["From"] = "a@a.com"
        msg_obj["To"] = "b@b.com"
        msg_obj["Subject"] = "Multi"
        msg_obj["Date"] = "Mon, 15 Jan 2024 10:00:00 +0000"
        msg_obj.attach(MIMEText("body", "plain"))
        for i in range(3):
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f"content{i}".encode())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment",
                            filename=f"file{i}.txt")
            msg_obj.attach(part)
        msg = parse_message(msg_obj.as_bytes(), 1, "INBOX", 1,
                            attachment_dir=tmp_path)
        items = json.loads(msg.attachments)
        assert len(items) == 3

    def test_unsafe_attachment_filename_sanitized(self, tmp_path):
        raw = _mail_with_attachment(filename="../../etc/passwd", content=b"data")
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        items = json.loads(msg.attachments)
        path = Path(items[0]["path"])
        # Must not have escaped out of tmp_path subtree
        assert tmp_path in path.parents

    def test_inline_cid_image_not_duplicate(self, tmp_path):
        """An inline image referenced via cid: should NOT appear as a plain
        attachment chip (inline=True)."""
        import base64
        from email.mime.image import MIMEImage
        msg_obj = MIMEMultipart("related")
        msg_obj["From"] = "a@a.com"
        msg_obj["To"] = "b@b.com"
        msg_obj["Subject"] = "CID"
        msg_obj["Date"] = "Mon, 15 Jan 2024 10:00:00 +0000"
        html = '<img src="cid:logo@example">'
        msg_obj.attach(MIMEText(html, "html"))
        img_part = MIMEImage(b"\x89PNG\r\n", _subtype="png")
        img_part["Content-ID"] = "<logo@example>"
        img_part.add_header("Content-Disposition", "inline")
        msg_obj.attach(img_part)
        msg = parse_message(msg_obj.as_bytes(), 1, "INBOX", 1,
                            attachment_dir=tmp_path)
        items = json.loads(msg.attachments or "[]")
        # The image is inline — its inline flag must be True
        assert all(i.get("inline") for i in items)


# ---------------------------------------------------------------------------
# Denial-of-service hardening (ref: MIME Parser DoS brief, D1-D3)
#
# The parser runs on every message during IMAP sync, off the wire, before the
# message is shown. A crafted email must never hang it, exhaust memory, or
# crash it: hostile input is replaced with a safe placeholder that still
# carries the real uid so the sync watermark advances and it is not re-fetched.
# ---------------------------------------------------------------------------

import resource
import time

from emailclient.mailparse import (
    MAX_MESSAGE_BYTES, _structural_reject, _placeholder,
)

PLACEHOLDER_SUBJECT = "(unreadable message)"


def _is_placeholder(msg) -> bool:
    return msg.subject == PLACEHOLDER_SUBJECT


class TestParserDoS:
    def test_d3_deeply_nested_multipart(self, tmp_path):
        body = b""
        for i in range(1000):
            body = (b'Content-Type: multipart/mixed; boundary="b%d"\r\n\r\n'
                    b"--b%d\r\n" % (i, i)) + body + (b"\r\n--b%d--\r\n" % i)
        raw = b"MIME-Version: 1.0\r\n" + body
        t0 = time.time()
        msg = parse_message(raw, 1, "INBOX", 42, attachment_dir=tmp_path)
        assert time.time() - t0 < 5.0      # does not hang
        assert _is_placeholder(msg)
        assert msg.uid == 42               # watermark still advances

    def test_d3_deeply_nested_rfc822(self, tmp_path):
        raw = b"Subject: inner\r\n\r\nhi\r\n"
        for _ in range(1200):
            raw = b"Content-Type: message/rfc822\r\n\r\n" + raw
        raw = b"MIME-Version: 1.0\r\n" + raw
        msg = parse_message(raw, 1, "INBOX", 7, attachment_dir=tmp_path)
        assert _is_placeholder(msg)
        assert msg.uid == 7

    def test_d1_param_flood_does_not_hang(self, tmp_path):
        params = b";".join(b'a%d="%d"' % (i, i) for i in range(80000))
        raw = (b"MIME-Version: 1.0\r\nContent-Type: text/plain; " + params
               + b"\r\n\r\nbody\r\n")
        t0 = time.time()
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        assert time.time() - t0 < 5.0
        assert _is_placeholder(msg)

    def test_d2_encoded_word_flood_bounds_memory(self, tmp_path):
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ew = b"=?utf-8?B?QUFBQQ==?="
        raw = (b"Subject: " + b" ".join([ew] * 50000)
               + b"\r\nMIME-Version: 1.0\r\n\r\nbody\r\n")
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS, KiB on Linux; either way the unfixed
        # parser ballooned by gigabytes. Allow a generous 1 GB headroom.
        assert (after - before) < 1024 * 1024 * 1024
        assert _is_placeholder(msg)

    def test_oversized_message_rejected_before_parse(self, tmp_path):
        raw = b"Subject: big\r\n\r\n" + b"x" * (MAX_MESSAGE_BYTES + 1)
        assert _structural_reject(raw) == "oversized"
        msg = parse_message(raw, 1, "INBOX", 5, attachment_dir=tmp_path)
        assert _is_placeholder(msg)
        assert msg.uid == 5

    def test_structural_reject_passes_normal_mail(self):
        assert _structural_reject(_simple_mail()) is None
        assert _structural_reject(_html_mail()) is None

    def test_normal_mail_unaffected_by_guards(self, tmp_path):
        # A benign multipart with an attachment must parse with full fidelity,
        # never as a placeholder.
        raw = _mail_with_attachment()
        msg = parse_message(raw, 1, "INBOX", 1, attachment_dir=tmp_path)
        assert not _is_placeholder(msg)
        assert msg.subject == "Attach"

    def test_placeholder_carries_identity(self):
        ph = _placeholder(9, "Sent", 123, "oversized")
        assert ph.account_id == 9
        assert ph.folder == "Sent"
        assert ph.uid == 123
        assert ph.subject == PLACEHOLDER_SUBJECT
