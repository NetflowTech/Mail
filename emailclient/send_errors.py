"""Explain a failed send in words the user can act on.

SMTP failures arrive as things like ``(535, b'5.7.8 Username and password not
accepted')``. Showing that to the person who just wrote an email tells them
nothing about what to do next, so every failure we can recognise is turned
into one sentence: what went wrong, and where to fix it.

Order matters — the rules are tried top to bottom and the first hit wins, so
an authentication failure that happens to quote a 550 code stays an
authentication failure.
"""
from __future__ import annotations

# (needles, explanation). A rule matches when ANY of its needles appears in the
# lower-cased error text.
_RULES: list[tuple[tuple[str, ...], str]] = [
    # --- our own internal messages, before anything guesses at them ---
    (("account removed",),
     "This account was removed while the email was waiting to go out. "
     "Add the account again, then resend from the Outbox."),
    (("approval server",),
     "The approval server could not be reached, so the email is waiting "
     "instead of being sent for approval. Check the internet connection and "
     "resend from the Outbox."),
    (("google re-login required",),
     "Google sign-in has expired for this account. Sign in to Google again "
     "from Account Settings, then resend from the Outbox."),
    (("microsoft re-login required",),
     "Microsoft sign-in has expired for this account. Sign in to Microsoft "
     "again from Account Settings, then resend from the Outbox."),

    # --- sign-in: the common one, and it must outrank the code rules below ---
    (("authentication", "auth failed", "authenticationfailed", "535",
      "534", "password not accepted", "invalid credentials",
      "login denied", "5.7.8"),
     "The mail server rejected the sign-in for this account — the password "
     "or app password is wrong or has been changed. Update it in Account "
     "Settings, then resend from the Outbox."),

    # --- can't find / can't reach the server ---
    (("nodename nor servname", "name or service not known", "gaierror",
      "getaddrinfo", "no address associated"),
     "The outgoing mail server name could not be found. Check the SMTP "
     "server name in Account Settings, and that this PC is online."),
    (("connection refused", "timed out", "timeout", "network is unreachable",
      "connection reset", "connection aborted", "broken pipe",
      "server not connected", "unreachable"),
     "Could not reach the outgoing mail server. It may be down, or the "
     "internet connection or a firewall is blocking it. Resend from the "
     "Outbox once the connection is back."),
    (("certificate", "ssl", "tls", "wrong version number"),
     "The secure connection to the mail server could not be established. "
     "Check the SMTP port and encryption in Account Settings."),

    # --- the server answered, and said no ---
    (("size exceeds", "message too large", "552", "5.3.4", "too big"),
     "The email is too large for the mail server. Remove or shrink the "
     "attachments and send it again."),
    (("too many", "rate limit", "quota", "limit exceeded", "421", "4.7.0",
      "try again later"),
     "The mail server's sending limit has been reached for now. Wait a "
     "while, then resend from the Outbox."),
    (("recipientsrefused", "no such user", "user unknown", "unknown recipient",
      "recipient address rejected", "mailbox unavailable",
      "does not exist", "550"),
     "The mail server refused the recipient address. Check the spelling of "
     "the addresses, then resend from the Outbox."),
    (("senderrefused", "relaying denied", "relay access denied",
      "not allowed to send", "553", "sender address rejected"),
     "The mail server refused to send from this address. It usually means "
     "the account is not allowed to send as this address — check the "
     "account's email address and SMTP settings."),
]


def explain(error) -> str:
    """One actionable sentence for *error* (an exception or its text).

    An error matching no rule is quoted back verbatim rather than softened —
    an unrecognised reason is still far more useful than "sending failed".
    """
    text = str(error or "").strip()
    if not text:
        return ("The email did not go out and the mail server gave no reason. "
                "Resend it from the Outbox.")
    low = text.lower()
    for needles, explanation in _RULES:
        if any(n in low for n in needles):
            return explanation
    return f"The mail server reported: {text}"
