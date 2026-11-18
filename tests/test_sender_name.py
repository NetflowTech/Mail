from emailclient.sender_name import safe_display_name
from emailclient.ui.message_list import _display_sender


def test_spoofed_address_in_name_shows_real_address():
    assert _display_sender('"security@paypal.com" <attacker@evil.com>') == "attacker@evil.com"
    assert safe_display_name("PayPal (security@paypal.com)", "x@evil.com") == "x@evil.com"


def test_honest_names_are_kept():
    assert _display_sender('"Alice Smith" <alice@example.com>') == "Alice Smith"
    assert safe_display_name("alice@example.com", "Alice@Example.com") == "alice@example.com"
    assert safe_display_name("Support @ Acme", "s@acme.com") == "Support @ Acme"


def test_missing_name_falls_back_to_address():
    assert _display_sender("bob@example.com") == "bob@example.com"
    assert safe_display_name("", "bob@example.com") == "bob@example.com"


def test_invisible_characters_removed():
    assert safe_display_name("Al‮ice", "a@x.com") == "Alice"
