"""Google sign-in: PKCE, and stray local requests can't end the sign-in."""
from __future__ import annotations

import base64
import hashlib
import threading
import urllib.error
import urllib.parse
import urllib.request

from emailclient import google_oauth


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_pkce_and_stray_requests(monkeypatch):
    seen = {}

    def fake_open(url):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        seen["challenge"] = q["code_challenge"]
        assert q["code_challenge_method"] == "S256"
        base = q["redirect_uri"]

        def browser():
            seen["stray"] = [_get(base + "/"), _get(base + "/?x=1"),
                             _get(base + "/?state=wrong&code=evil")]
            seen["real"] = _get(base + "/?" + urllib.parse.urlencode(
                {"state": q["state"], "code": "good"}))
        t = threading.Thread(target=browser, daemon=True)
        seen["thread"] = t
        t.start()
        return True

    def fake_exchange(email, code, redirect_uri, verifier=""):
        seen["code"], seen["verifier"] = code, verifier
        return "token"

    monkeypatch.setattr(google_oauth.webbrowser, "open", fake_open)
    monkeypatch.setattr(google_oauth, "_exchange_code", fake_exchange)
    assert google_oauth.login_interactive("a@gmail.com") == "token"
    seen["thread"].join(5)
    assert seen["stray"] == [404, 404, 404]
    assert seen["real"] == 200
    assert seen["code"] == "good"
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(seen["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert seen["challenge"] == expect


def test_exchange_sends_verifier(monkeypatch):
    sent = {}
    monkeypatch.setattr(google_oauth, "_post_token",
                        lambda form: sent.update(form) or {"access_token": "t"})
    monkeypatch.setattr(google_oauth.keyring, "set_password", lambda *a: None)
    google_oauth._exchange_code("a@gmail.com", "c", "http://127.0.0.1:1", "v")
    assert sent["code_verifier"] == "v"
