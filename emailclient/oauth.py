from __future__ import annotations

import keyring
import msal

from .config import APP_NAME

# Thunderbird's public client id — the de-facto standard for personal IMAP tools.
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["https://outlook.office365.com/IMAP.AccessAsUser.All",
          "https://outlook.office365.com/SMTP.Send"]
_CACHE_KEY = "msal-cache"


def _load_cache() -> "msal.SerializableTokenCache":
    cache = msal.SerializableTokenCache()
    raw = keyring.get_password(APP_NAME, _CACHE_KEY)
    if raw:
        cache.deserialize(raw)
    return cache


def _save_cache(cache) -> None:
    if cache.has_state_changed:
        keyring.set_password(APP_NAME, _CACHE_KEY, cache.serialize())


def _app(cache):
    return msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY,
                                        token_cache=cache)


def login_interactive(email: str) -> str:
    """Open the browser for Microsoft login; return access token."""
    cache = _load_cache()
    app = _app(cache)
    result = app.acquire_token_interactive(SCOPES, login_hint=email)
    _save_cache(cache)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "Microsoft login failed"))
    return result["access_token"]


def get_token(email: str) -> str | None:
    """Silent token (refresh if needed); None if re-login required."""
    cache = _load_cache()
    app = _app(cache)
    accounts = [a for a in app.get_accounts()
                if a.get("username", "").lower() == email.lower()]
    if not accounts:
        return None
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    return result.get("access_token") if result else None
