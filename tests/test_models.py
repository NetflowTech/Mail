"""Tests for emailclient.models dataclasses."""
from __future__ import annotations

import pytest

from emailclient.models import Account, EmailMessage


class TestAccount:
    def test_required_fields(self):
        a = Account(
            id=None,
            name="Alice",
            email="alice@example.com",
            provider="gmail",
            imap_host="imap.gmail.com",
            imap_port=993,
            smtp_host="smtp.gmail.com",
            smtp_port=465,
            color="#DB4437",
        )
        assert a.email == "alice@example.com"
        assert a.provider == "gmail"

    def test_defaults(self):
        a = Account(
            id=1, name="Bob", email="bob@example.com", provider="zoho",
            imap_host="imap.zoho.com", imap_port=993,
            smtp_host="smtp.zoho.com", smtp_port=465,
            color="#E42527",
        )
        assert a.signature == ""
        assert a.group_id is None
        assert a.signature_image == ""
        assert a.signature_image_width == 0
        assert a.sync_depth == "50"
        assert a.paused is False
        assert a.signature_html == ""
        assert a.attachments_only is False
        assert a.notify_enabled is True

    def test_id_none_allowed(self):
        a = Account(id=None, name="X", email="x@x.com", provider="custom",
                    imap_host="", imap_port=993, smtp_host="", smtp_port=465,
                    color="#000")
        assert a.id is None

    def test_provider_values(self):
        for prov in ("gmail", "zoho", "outlook", "yahoo", "custom"):
            a = Account(id=1, name="T", email="t@t.com", provider=prov,
                        imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                        color="#000")
            assert a.provider == prov

    def test_paused_flag(self):
        a = Account(id=1, name="T", email="t@t.com", provider="gmail",
                    imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                    color="#000", paused=True)
        assert a.paused is True

    def test_attachments_only_flag(self):
        a = Account(id=1, name="T", email="t@t.com", provider="gmail",
                    imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                    color="#000", attachments_only=True)
        assert a.attachments_only is True

    def test_notify_enabled_false(self):
        a = Account(id=1, name="T", email="t@t.com", provider="gmail",
                    imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                    color="#000", notify_enabled=False)
        assert a.notify_enabled is False

    def test_sync_depth_variants(self):
        for depth in ("50", "30d", "90d", "365d", "all"):
            a = Account(id=1, name="T", email="t@t.com", provider="gmail",
                        imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                        color="#000", sync_depth=depth)
            assert a.sync_depth == depth

    def test_signature_html_stored(self):
        a = Account(id=1, name="T", email="t@t.com", provider="gmail",
                    imap_host="h", imap_port=993, smtp_host="h", smtp_port=465,
                    color="#000", signature_html="<b>Alice</b>")
        assert a.signature_html == "<b>Alice</b>"


class TestEmailMessage:
    def test_required_fields(self, sample_email_message):
        msg = sample_email_message
        assert msg.account_id == 1
        assert msg.folder == "INBOX"
        assert msg.uid == 42
        assert msg.subject == "Hello World"
        assert msg.sender == "bob@example.com"

    def test_defaults(self, sample_email_message):
        msg = sample_email_message
        assert msg.is_read is False
        assert msg.id is None
        assert msg.attachments == ""
        assert msg.cc == ""
        assert msg.bcc == ""
        assert msg.flagged is False
        assert msg.pinned is False
        assert msg.flag_done is False
        assert msg.message_id == ""

    def test_flagged(self, sample_email_message):
        sample_email_message.flagged = True
        assert sample_email_message.flagged is True

    def test_pinned(self, sample_email_message):
        sample_email_message.pinned = True
        assert sample_email_message.pinned is True

    def test_flag_done(self, sample_email_message):
        sample_email_message.flag_done = True
        assert sample_email_message.flag_done is True

    def test_message_id_stored(self):
        from emailclient.models import EmailMessage
        msg = EmailMessage(
            account_id=1, folder="INBOX", uid=1,
            subject="s", sender="s@s.com", recipients="r@r.com",
            date="2024-01-01T00:00:00+00:00",
            body_text="", body_html="",
            message_id="<unique-id@example.com>",
        )
        assert msg.message_id == "<unique-id@example.com>"

    def test_bcc_stored(self):
        from emailclient.models import EmailMessage
        msg = EmailMessage(
            account_id=1, folder="Sent", uid=1,
            subject="s", sender="s@s.com", recipients="r@r.com",
            date="2024-01-01T00:00:00+00:00",
            body_text="", body_html="",
            bcc="secret@example.com",
        )
        assert msg.bcc == "secret@example.com"
