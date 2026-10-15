"""Tests for emailclient.send_errors — SMTP error translation."""
from __future__ import annotations

import pytest

from emailclient.send_errors import explain


class TestExplain:
    # --- empty / None input ---
    def test_empty_string(self):
        msg = explain("")
        assert "no reason" in msg.lower()

    def test_none(self):
        msg = explain(None)
        assert "no reason" in msg.lower()

    # --- authentication ---
    def test_535_auth_failure(self):
        msg = explain("(535, b'5.7.8 Username and Password not Accepted')")
        assert "password" in msg.lower() or "sign-in" in msg.lower()

    def test_authentication_keyword(self):
        msg = explain("Authentication failed")
        assert "password" in msg.lower() or "sign-in" in msg.lower()

    def test_invalid_credentials(self):
        msg = explain("AUTHENTICATE failed: invalid credentials")
        assert "password" in msg.lower() or "sign-in" in msg.lower()

    def test_google_relogin(self):
        msg = explain("google re-login required")
        assert "Google" in msg or "google" in msg.lower()

    def test_microsoft_relogin(self):
        msg = explain("microsoft re-login required")
        assert "Microsoft" in msg or "microsoft" in msg.lower()

    # --- DNS / address resolution ---
    def test_gaierror(self):
        msg = explain("[Errno 11001] getaddrinfo failed")
        assert "server name" in msg.lower() or "smtp" in msg.lower()

    def test_nodename(self):
        msg = explain("nodename nor servname provided, or not known")
        assert "server name" in msg.lower()

    # --- connection refused / timeout ---
    def test_connection_refused(self):
        msg = explain("Connection refused")
        assert "reach" in msg.lower() or "connect" in msg.lower()

    def test_timed_out(self):
        msg = explain("timed out connecting to smtp.example.com")
        assert "reach" in msg.lower() or "connect" in msg.lower()

    def test_network_unreachable(self):
        msg = explain("Network is unreachable")
        assert "reach" in msg.lower() or "connect" in msg.lower()

    # --- TLS / SSL ---
    def test_ssl_error(self):
        msg = explain("SSL: WRONG_VERSION_NUMBER")
        assert "secure" in msg.lower() or "ssl" in msg.lower() or "port" in msg.lower()

    def test_certificate_error(self):
        msg = explain("certificate verify failed")
        assert "secure" in msg.lower() or "ssl" in msg.lower()

    # --- message too large ---
    def test_552_too_large(self):
        msg = explain("552 5.3.4 Message size exceeds fixed maximum message size")
        assert "large" in msg.lower() or "attachment" in msg.lower()

    def test_message_too_large_text(self):
        msg = explain("message too large")
        assert "large" in msg.lower()

    # --- rate limit ---
    def test_421_rate_limit(self):
        msg = explain("421 Try again later")
        assert "limit" in msg.lower() or "wait" in msg.lower()

    def test_quota_exceeded(self):
        msg = explain("quota exceeded for user")
        assert "limit" in msg.lower() or "wait" in msg.lower()

    # --- recipient refused ---
    def test_550_user_unknown(self):
        msg = explain("550 User unknown")
        assert "recipient" in msg.lower() or "address" in msg.lower()

    def test_no_such_user(self):
        msg = explain("No such user here")
        assert "recipient" in msg.lower() or "address" in msg.lower()

    # --- sender refused ---
    def test_relay_denied(self):
        msg = explain("Relay access denied")
        assert "address" in msg.lower() or "send" in msg.lower() or "relay" in msg.lower()

    def test_553_sender(self):
        msg = explain("553 5.1.3 Sender address rejected")
        assert "sender" in msg.lower() or "address" in msg.lower()

    # --- account removed ---
    def test_account_removed(self):
        msg = explain("account removed while the email was waiting")
        assert "account" in msg.lower() or "removed" in msg.lower()

    # --- approval server ---
    def test_approval_server(self):
        msg = explain("approval server could not be reached")
        assert "approval" in msg.lower()

    # --- unrecognised error quoted verbatim ---
    def test_unknown_error_quoted(self):
        error_text = "Something completely unexpected from the server"
        msg = explain(error_text)
        assert error_text in msg

    def test_exception_object(self):
        exc = ValueError("5.7.8 password not accepted")
        msg = explain(exc)
        assert "password" in msg.lower() or "sign-in" in msg.lower()
