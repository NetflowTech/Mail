"""Shared fixtures for the AMail test suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make sure the project root is on sys.path so `emailclient` is importable
# without an editable install.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def sample_account():
    """A minimal Account object suitable for tests that need one."""
    from emailclient.models import Account
    return Account(
        id=1,
        name="Alice",
        email="alice@example.com",
        provider="gmail",
        imap_host="imap.gmail.com",
        imap_port=993,
        smtp_host="smtp.gmail.com",
        smtp_port=465,
        color="#DB4437",
    )


@pytest.fixture()
def sample_email_message():
    """A minimal EmailMessage object suitable for tests that need one."""
    from emailclient.models import EmailMessage
    return EmailMessage(
        account_id=1,
        folder="INBOX",
        uid=42,
        subject="Hello World",
        sender="bob@example.com",
        recipients="alice@example.com",
        date="2024-01-15T10:30:00+00:00",
        body_text="Plain text body.",
        body_html="<p>Plain text body.</p>",
    )
