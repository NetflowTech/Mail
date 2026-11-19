"""The admin backend end to end, and each security property it promises."""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amail_admin import crypto                       # noqa: E402
from amail_admin.config import Config                # noqa: E402
from amail_admin.device_api import create_device_app  # noqa: E402
from amail_admin.panel import create_panel_app       # noqa: E402
from amail_admin.ratelimit import RateLimiter        # noqa: E402
from amail_admin.store import Store                  # noqa: E402

PW = "correct horse battery staple"
SERVERS = crypto.server_tag("mail.example.com", 993, "mail.example.com", 465)


@pytest.fixture()
def env(tmp_path):
    cfg = Config(data_dir=tmp_path)
    store = Store(cfg.db_path)
    master = crypto.load_or_create_master_key(cfg.master_key_path)
    sign = crypto.load_or_create_signing_key(cfg.signing_key_path)
    crypto.ensure_certificate(cfg.cert_path, cfg.cert_key_path)
    store.set_setting("enroll_code", "ABCD-EFGH-JKLM-NPQR")
    store.set_setting("public_url", "https://mail-admin.local:8443")
    store.set_admin("admin", crypto.hash_password(PW))
    tested = []

    def tester(*a):
        tested.append(a)
        return None

    return {"cfg": cfg, "store": store, "master": master, "sign": sign,
            "dev": create_device_app(store, master, 30, sign).test_client(),
            "panel_app": create_panel_app(cfg, store, master, b"k" * 32,
                                          crypto.signing_public(sign),
                                          RateLimiter(), mailbox_tester=tester),
            "tested": tested}


def lan(app, ip="192.168.1.20"):
    c = app.test_client()
    c.environ_base["REMOTE_ADDR"] = ip
    c.environ_base["wsgi.url_scheme"] = "https"
    return c


def csrf_in(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def totp_now(secret: str, offset: int = 0) -> str:
    return crypto.totp_at(secret, int(time.time() // 30) + offset)


def login(c, env, code_offset=0):
    """Password, then the authenticator code (set up on first sign-in)."""
    tok = csrf_in(c.get("/login").get_data(as_text=True))
    r = c.post("/login", data={"csrf": tok, "username": "admin", "password": PW})
    assert r.status_code == 302 and "/login/code" in r.headers["Location"]
    page = c.get("/login/code").get_data(as_text=True)
    row = env["store"].get_admin("admin")
    secret = row["totp_secret"] or re.search(
        r'<code class="key">([A-Z2-7]+)</code>', page).group(1)
    r = c.post("/login/code", data={"csrf": csrf_in(page),
                                    "code": totp_now(secret, code_offset)})
    assert r.status_code == 302 and r.headers["Location"].endswith("/"), r.data
    return csrf_in(c.get("/settings").get_data(as_text=True)), secret


def enroll(dev, name="Reception PC"):
    key = X25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)
    r = dev.post("/api/v1/enroll", json={"code": "abcd-efgh-jklm-npqr",
                                         "name": name, "os": "Windows 11",
                                         "public_key": crypto.b64e(pub)})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    return key, j["device_id"], j["token"], pub


def checkin(dev, token, accounts=()):
    return dev.post("/api/v1/checkin", json={"app_version": "2.0.14",
                                             "accounts": list(accounts)},
                    headers={"Authorization": f"Bearer {token}"})


def add_mailbox(env, email="sales@example.com", pw="mailpw"):
    s = env["store"]
    mid = s.add_mailbox(email, "Sales", "mail.example.com", 993,
                        "mail.example.com", 465, b"x")
    s.update_mailbox(mid, display_name="Sales", imap_host="mail.example.com",
                     imap_port=993, smtp_host="mail.example.com", smtp_port=465,
                     password_enc=crypto.encrypt_stored(env["master"], pw,
                                                        f"mailbox:{mid}"))
    return mid


# ----- crypto -----

def test_sealed_password_opens_only_on_its_pc_for_its_servers():
    a, b = X25519PrivateKey.generate(), X25519PrivateKey.generate()
    pub = a.public_key().public_bytes(serialization.Encoding.Raw,
                                      serialization.PublicFormat.Raw)
    box = crypto.seal_for_device(pub, "dev1", "Sales@Example.com", 3, "s3cret",
                                 SERVERS)
    assert crypto.open_sealed(a, "dev1", "sales@example.com", 3, box, SERVERS) == "s3cret"
    evil = crypto.server_tag("evil.example.net", 993, "mail.example.com", 465)
    for args in ((b, "dev1", "sales@example.com", 3, box, SERVERS),   # other PC
                 (a, "dev2", "sales@example.com", 3, box, SERVERS),   # replayed
                 (a, "dev1", "boss@example.com", 3, box, SERVERS),    # other mailbox
                 (a, "dev1", "sales@example.com", 2, box, SERVERS),   # old version
                 (a, "dev1", "sales@example.com", 3, box, evil)):     # other server
        with pytest.raises(Exception):
            crypto.open_sealed(*args)


def test_server_tag_ignores_case_and_spaces():
    assert crypto.server_tag(" Mail.Example.com ", "993", "MAIL.example.com", 465) == SERVERS


def test_stored_password_is_bound_to_its_mailbox():
    key = b"m" * 32
    blob = crypto.encrypt_stored(key, "pw", "mailbox:1")
    assert crypto.decrypt_stored(key, blob, "mailbox:1") == "pw"
    with pytest.raises(Exception):
        crypto.decrypt_stored(key, blob, "mailbox:2")


def test_totp_matches_rfc6238_and_works_once():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert crypto.totp_at(secret, 59 // 30) == "287082"        # RFC 6238 vector
    now = 1_000_000_000.0
    step = int(now // 30)
    code = crypto.totp_at(secret, step)
    assert crypto.totp_verify(secret, code, 0, now) == step
    assert crypto.totp_verify(secret, code, step, now) is None   # replay
    assert crypto.totp_verify(secret, crypto.totp_at(secret, step - 5), 0, now) is None


def test_check_code_is_stable_and_short():
    c = crypto.check_code(b"\x01" * 32)
    assert re.fullmatch(r"[A-Z2-7]{4}-[A-Z2-7]{4}", c)
    assert c == crypto.check_code(b"\x01" * 32) != crypto.check_code(b"\x02" * 32)


def test_setup_code_carries_the_signing_key():
    code = crypto.make_setup_code("https://h:8443", "ab" * 32, "ABCD", b"\x07" * 32)
    payload = json.loads(crypto.b64d(code[7:]))
    assert payload == {"u": "https://h:8443", "f": "ab" * 32, "c": "ABCD",
                       "s": crypto.b64e(b"\x07" * 32)}


# ----- device API -----

def test_wrong_or_closed_enrollment_is_refused(env):
    r = env["dev"].post("/api/v1/enroll", json={"code": "nope", "name": "x",
                                                "public_key": crypto.b64e(b"\0" * 32)})
    assert r.status_code == 403
    env["store"].set_setting("enroll_open", "0")
    with pytest.raises(AssertionError):
        enroll(env["dev"])


def test_unknown_token_is_refused(env):
    assert checkin(env["dev"], "made-up").status_code == 401


def test_answers_carry_a_signed_server_time(env):
    _, device_id, token, _ = enroll(env["dev"])
    lease = checkin(env["dev"], token).get_json()["lease"]
    assert abs(lease["t"] - time.time()) < 5
    Ed25519PublicKey.from_public_bytes(crypto.signing_public(env["sign"])).verify(
        crypto.b64d(lease["sig"]), crypto.lease_message(device_id, lease["t"]))


def test_pending_pc_gets_nothing(env):
    _, _, token, _ = enroll(env["dev"])
    j = checkin(env["dev"], token).get_json()
    assert j["state"] == "pending" and j["managed"] == []


def test_full_sign_in_and_sign_out(env):
    store, dev = env["store"], env["dev"]
    key, device_id, token, _ = enroll(dev)
    store.set_device_status(device_id, "approved")
    mid = add_mailbox(env)
    store.grant(device_id, mid)

    item = checkin(dev, token).get_json()["managed"][0]
    assert item["email"] == "sales@example.com" and "mailpw" not in str(item)
    servers = crypto.server_tag(item["imap_host"], item["imap_port"],
                                item["smtp_host"], item["smtp_port"])
    assert crypto.open_sealed(key, device_id, item["email"], item["version"],
                              item["secret"], servers) == "mailpw"
    held = {"email": "sales@example.com", "status": "running", "managed": True,
            "version": item["version"]}
    assert "secret" not in checkin(dev, token, [held]).get_json()["managed"][0]
    store.revoke(device_id, mid)
    assert checkin(dev, token, [held]).get_json()["managed"] == []


def test_blocked_pc_is_told_so_and_loses_grants(env):
    store, dev = env["store"], env["dev"]
    _, device_id, token, _ = enroll(dev)
    store.set_device_status(device_id, "approved")
    store.grant(device_id, add_mailbox(env))
    store.set_device_status(device_id, "revoked")
    j = checkin(dev, token).get_json()
    assert j["state"] == "revoked" and j["managed"] == []
    assert store.granted_mailboxes(device_id) == []


def test_old_tokens_are_rotated_without_locking_the_pc_out(env):
    store, dev = env["store"], env["dev"]
    _, device_id, token, _ = enroll(dev)
    store._x("UPDATE devices SET token_issued_at=? WHERE id=?",
             (time.time() - 31 * 86400, device_id))
    new = checkin(dev, token).get_json()["new_token"]
    # The answer might have been lost: the old token still works...
    assert checkin(dev, token).status_code == 200
    # ...until the new one has been used once.
    assert checkin(dev, new).status_code == 200
    assert checkin(dev, token).status_code == 401


def test_waiting_pcs_are_capped_and_expire(env, monkeypatch):
    from amail_admin import device_api
    monkeypatch.setattr(device_api, "MAX_PENDING", 3)
    dev = create_device_app(env["store"], env["master"], 30, env["sign"],
                            limiter=RateLimiter()).test_client()
    for i in range(3):
        enroll(dev, f"pc{i}")
    with pytest.raises(AssertionError):
        enroll(dev, "one too many")
    env["store"]._x("UPDATE devices SET created_at=?", (time.time() - 8 * 86400,))
    enroll(dev, "after a week")          # stale requests were cleared


def test_a_pc_hammering_checkin_is_slowed(env):
    _, _, token, _ = enroll(env["dev"])
    codes = [checkin(env["dev"], token).status_code for _ in range(25)]
    assert 429 in codes and codes[0] == 200


def test_forwarded_address_only_believed_from_a_trusted_proxy(env):
    import ipaddress
    app = create_device_app(env["store"], env["master"], 30, env["sign"],
                            trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),))
    via_proxy = app.test_client()
    via_proxy.environ_base["REMOTE_ADDR"] = "127.0.0.1"
    key = X25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    via_proxy.post("/api/v1/enroll", json={"code": "ABCD-EFGH-JKLM-NPQR",
                                           "public_key": crypto.b64e(key)},
                   headers={"CF-Connecting-IP": "203.0.113.50"})
    direct = app.test_client()
    direct.environ_base["REMOTE_ADDR"] = "198.51.100.7"
    direct.post("/api/v1/enroll", json={"code": "ABCD-EFGH-JKLM-NPQR",
                                        "public_key": crypto.b64e(key)},
                headers={"X-Forwarded-For": "1.2.3.4"})
    ips = sorted(d["last_ip"] for d in env["store"].devices())
    assert ips == ["198.51.100.7", "203.0.113.50"]


# ----- panel: access -----

def test_panel_refuses_addresses_outside_the_admin_networks(env, tmp_path):
    assert lan(env["panel_app"], "8.8.8.8").get("/login").status_code == 403
    import ipaddress
    narrow = Config(data_dir=tmp_path,
                    admin_networks=(ipaddress.ip_network("192.168.10.0/28"),))
    app = create_panel_app(narrow, env["store"], env["master"], b"k" * 32,
                           b"s" * 32)
    assert lan(app, "192.168.1.20").get("/login").status_code == 403
    assert lan(app, "192.168.10.5").get("/login").status_code == 200


def test_password_alone_is_not_enough(env):
    c = lan(env["panel_app"])
    tok = csrf_in(c.get("/login").get_data(as_text=True))
    c.post("/login", data={"csrf": tok, "username": "admin", "password": PW})
    assert c.get("/").status_code == 302                     # still out
    page = c.get("/login/code").get_data(as_text=True)
    r = c.post("/login/code", data={"csrf": csrf_in(page), "code": "000000"})
    assert c.get("/").status_code == 302


def test_two_step_setup_then_codes_work_once(env):
    c = lan(env["panel_app"])
    _, secret = login(c, env)
    assert env["store"].get_admin("admin")["totp_secret"] == secret
    c.post("/logout", data={"csrf": csrf_in(c.get("/settings").get_data(as_text=True))})
    # The code just used can't be used again (even within its 30 seconds).
    c2 = lan(env["panel_app"])
    tok = csrf_in(c2.get("/login").get_data(as_text=True))
    c2.post("/login", data={"csrf": tok, "username": "admin", "password": PW})
    page = c2.get("/login/code").get_data(as_text=True)
    assert 'class="key"' not in page                         # no secret shown again
    c2.post("/login/code", data={"csrf": csrf_in(page), "code": totp_now(secret)})
    assert c2.get("/").status_code == 302
    # The next code does.
    c3 = lan(env["panel_app"])
    login(c3, env, code_offset=1)


def test_wrong_passwords_dont_lock_out_the_admin_elsewhere(env):
    attacker = lan(env["panel_app"], "192.168.1.66")
    tok = csrf_in(attacker.get("/login").get_data(as_text=True))
    for _ in range(12):
        attacker.post("/login", data={"csrf": tok, "username": "admin",
                                      "password": "wrong"})
    r = attacker.post("/login", data={"csrf": tok, "username": "admin",
                                      "password": PW})
    assert r.status_code == 429                              # that computer: blocked
    login(lan(env["panel_app"], "192.168.1.20"), env)        # the admin: fine


def test_logout_ends_the_session_on_the_server(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    stolen = c.get_cookie("amail_admin").value
    c.post("/logout", data={"csrf": tok})
    thief = lan(env["panel_app"])
    thief.set_cookie("amail_admin", stolen)
    assert thief.get("/").status_code == 302


def test_password_change_ends_every_session(env):
    a, b = lan(env["panel_app"]), lan(env["panel_app"], "192.168.1.21")
    tok_a, secret = login(a, env)
    login(b, env, code_offset=1)
    a.post("/settings/password", data={"csrf": tok_a, "current": PW,
                                       "new": "a much longer new password"})
    assert a.get("/").status_code == 302 and b.get("/").status_code == 302


def test_idle_session_expires(env, monkeypatch):
    from amail_admin import panel
    c = lan(env["panel_app"])
    login(c, env)
    env["store"]._x("UPDATE sessions SET last_seen=?", (time.time() - 31 * 60,))
    assert c.get("/").status_code == 302


def test_post_without_csrf_is_rejected(env):
    c = lan(env["panel_app"])
    login(c, env)
    assert c.post("/mailboxes/add", data={"email": "a@b.com"}).status_code == 400


def test_security_headers_and_certificate_download(env):
    c = lan(env["panel_app"])
    r = c.get("/login")
    assert "script-src 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    cert = c.get("/certificate")
    assert cert.status_code == 200 and b"BEGIN CERTIFICATE" in cert.data


# ----- panel: devices and mailboxes -----

def test_approval_needs_the_code_shown_on_the_pc(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    _, device_id, _, pub = enroll(env["dev"])
    page = c.get(f"/devices/{device_id}").get_data(as_text=True)
    assert crypto.check_code(pub) not in page                # not given away
    c.post(f"/devices/{device_id}/approve", data={"csrf": tok, "check_code": "AAAA-AAAA"})
    assert env["store"].device(device_id)["status"] == "pending"
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub).lower()})
    assert env["store"].device(device_id)["status"] == "approved"


def test_panel_flow_add_mailbox_sign_in_and_out(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    _, device_id, ptoken, pub = enroll(env["dev"])
    r = c.post("/mailboxes/add", data={
        "csrf": tok, "email": "Sales@Example.com", "display_name": "Sales",
        "imap_host": "mail.example.com", "imap_port": "993",
        "smtp_host": "mail.example.com", "smtp_port": "465",
        "password": "mailpw", "test": "1"})
    assert r.status_code == 302
    assert env["tested"][0][:2] == ("sales@example.com", "mailpw")
    mb = store.mailbox_by_email("sales@example.com")
    c.post(f"/devices/{device_id}/grant", data={"csrf": tok, "mailbox_id": mb["id"]})
    assert store.granted_mailboxes(device_id) == []          # not approved yet
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub)})
    c.post(f"/devices/{device_id}/grant", data={"csrf": tok, "mailbox_id": mb["id"]})
    assert len(store.granted_mailboxes(device_id)) == 1
    for path in ("/", "/mailboxes", f"/mailboxes/{mb['id']}", "/settings",
                 "/audit", f"/devices/{device_id}"):
        assert "mailpw" not in c.get(path).get_data(as_text=True)
    c.post(f"/devices/{device_id}/ungrant", data={"csrf": tok, "mailbox_id": mb["id"]})
    assert checkin(env["dev"], ptoken).get_json()["managed"] == []


def test_saved_password_is_never_tested_against_a_new_server(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    mid = add_mailbox(env)
    form = {"csrf": tok, "email": "sales@example.com", "display_name": "Sales",
            "imap_host": "attacker.example.net", "imap_port": "993",
            "smtp_host": "mail.example.com", "smtp_port": "465", "test": "1"}
    c.post(f"/mailboxes/{mid}/edit", data=form)
    assert env["tested"] == []                                # never sent
    assert env["store"].mailbox(mid)["imap_host"] == "mail.example.com"
    c.post(f"/mailboxes/{mid}/edit", data={**form, "password": "typed-again"})
    assert env["tested"][0][1] == "typed-again"


def test_connection_test_refuses_this_machine():
    from amail_admin.panel import test_mailbox
    err = test_mailbox("a@b.com", "pw", "localhost", 993, "localhost", 465)
    assert err and "won't connect" in err


def test_alerts_show_on_suspicious_activity(env):
    c = lan(env["panel_app"])
    login(c, env)
    for _ in range(6):
        env["store"].audit("device", "enroll_refused", "ip=x")
    assert "tried to join with a wrong code" in c.get("/").get_data(as_text=True)


def test_settings_show_a_setup_code(env):
    c = lan(env["panel_app"])
    login(c, env)
    html = c.get("/settings").get_data(as_text=True)
    payload = json.loads(crypto.b64d(
        re.search(r"AMAIL1\.([A-Za-z0-9_-]+)", html).group(1)))
    assert payload["u"] == "https://mail-admin.local:8443"
    assert payload["f"] == crypto.certificate_fingerprint(env["cfg"].cert_path)
    assert payload["s"] == crypto.b64e(crypto.signing_public(env["sign"]))


def test_device_delete_requires_block_first(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    _, device_id, _, pub = enroll(env["dev"])
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub)})
    c.post(f"/devices/{device_id}/delete", data={"csrf": tok})
    assert env["store"].device(device_id) is not None
    c.post(f"/devices/{device_id}/block", data={"csrf": tok})
    c.post(f"/devices/{device_id}/delete", data={"csrf": tok})
    assert env["store"].device(device_id) is None


# ----- Google / Microsoft sign-in -----

def test_sign_in_link_and_pasted_address_checks():
    from amail_admin import oauth
    p = oauth.start("google", "a@gmail.com", oauth.DEFAULT_CLIENTS["google"])
    assert "code_challenge_method=S256" in p["url"] and "login_hint=a%40gmail.com" in p["url"]
    good = f"{p['redirect']}?state={p['state']}&code=4/abc&scope=x"
    assert oauth.code_from_pasted(p, good) == "4/abc"
    for bad in (f"{p['redirect']}?state=WRONG&code=x",              # other sign-in
                f"http://127.0.0.1:1/?state={p['state']}&code=x",    # other port
                f"{p['redirect']}?state={p['state']}&error=access_denied",
                f"{p['redirect']}?state={p['state']}"):
        with pytest.raises(oauth.OAuthError):
            oauth.code_from_pasted(p, bad)
    ms = oauth.start("microsoft", "a@outlook.com", oauth.DEFAULT_CLIENTS["microsoft"])
    assert ms["redirect"].startswith("http://localhost:") and "offline_access" in ms["url"]


class _FakeOAuth:
    """The panel's sign-in steps without Google: records what happened."""
    from amail_admin.oauth import OAuthError, PROVIDERS
    def __init__(self):
        self.revoked, self.n = [], 0
    def start(self, provider, email, client):
        from amail_admin import oauth
        return oauth.start(provider, email, client)
    def code_from_pasted(self, pending, pasted):
        from amail_admin import oauth
        return oauth.code_from_pasted(pending, pasted)
    def exchange(self, pending, code, client):
        self.n += 1
        return {"refresh_token": f"rt-{self.n}", "access_token": "at"}
    def check_mailbox(self, provider, email, token):
        pass
    def revoke(self, provider, rt):
        self.revoked.append(rt)


def _oauth_panel(env):
    fake = _FakeOAuth()
    app = create_panel_app(env["cfg"], env["store"], env["master"], b"k" * 32,
                           crypto.signing_public(env["sign"]), RateLimiter(),
                           oauth_api=fake)
    return app, fake


def _sign_in(c, tok, **form):
    c.post("/mailboxes/oauth/start", data={"csrf": tok, **form})
    page = c.get("/mailboxes/oauth").get_data(as_text=True)
    url = re.search(r'href="([^"]+accounts\.google[^"]+)"', page).group(1).replace("&amp;", "&")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    landed = f"{q['redirect_uri'][0]}?state={q['state'][0]}&code=4/xyz"
    return c.post("/mailboxes/oauth/finish", data={"csrf": tok, "pasted": landed})


def test_google_mailbox_added_without_a_password(env):
    app, fake = _oauth_panel(env)
    c = lan(app)
    tok, _ = login(c, env)
    r = _sign_in(c, tok, provider="google", email="Team@Gmail.com", display_name="Team")
    assert r.status_code == 302
    m = env["store"].mailbox_by_email("team@gmail.com")
    assert m["auth"] == "google" and m["imap_host"] == "imap.gmail.com"
    assert crypto.decrypt_stored(env["master"], m["password_enc"],
                                 f"mailbox:{m['id']}") == "rt-1"
    # Renewing replaces it everywhere and cancels the old one at Google.
    _sign_in(c, tok, provider="google", mailbox_id=str(m["id"]))
    m2 = env["store"].mailbox(m["id"])
    assert m2["version"] > m["version"] and fake.revoked == ["rt-1"]


def test_pcs_get_the_sign_in_sealed_with_its_app_registration(env):
    app, _ = _oauth_panel(env)
    c = lan(app)
    tok, _ = login(c, env)
    _sign_in(c, tok, provider="google", email="team@gmail.com")
    store = env["store"]
    key, device_id, ptoken, _ = enroll(env["dev"])
    store.set_device_status(device_id, "approved")
    store.grant(device_id, store.mailbox_by_email("team@gmail.com")["id"])
    item = checkin(env["dev"], ptoken).get_json()["managed"][0]
    from amail_admin import oauth
    assert item["auth"] == "google"
    assert item["oauth_client"]["id"] == oauth.DEFAULT_CLIENTS["google"]["id"]
    servers = crypto.server_tag("imap.gmail.com", 993, "smtp.gmail.com", 465)
    assert crypto.open_sealed(key, device_id, "team@gmail.com", item["version"],
                              item["secret"], servers) == "rt-1"


def test_signin_mailbox_cant_be_edited_like_a_password_one(env):
    app, _ = _oauth_panel(env)
    c = lan(app)
    tok, _ = login(c, env)
    _sign_in(c, tok, provider="google", email="team@gmail.com")
    mid = env["store"].mailbox_by_email("team@gmail.com")["id"]
    r = c.post(f"/mailboxes/{mid}/edit", data={
        "csrf": tok, "email": "team@gmail.com", "imap_host": "evil.net",
        "imap_port": "993", "smtp_host": "evil.net", "smtp_port": "465",
        "password": "x"})
    assert r.status_code == 400
    assert env["store"].mailbox(mid)["imap_host"] == "imap.gmail.com"


# ----- the panel for a non-technical admin -----

def test_home_page_guides_setup_and_shows_what_needs_doing(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    html = c.get("/").get_data(as_text=True)
    assert "Set up in three steps" in html           # no mailbox, no PC yet
    _, device_id, ptoken, pub = enroll(env["dev"], "Front desk")
    html = c.get("/").get_data(as_text=True)
    assert "Needs you" in html and "Front desk</b> wants to join" in html
    assert f'action="/devices/{device_id}/approve"' in html   # approve inline
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub)})
    add_mailbox(env)
    html = c.get("/").get_data(as_text=True)
    assert "Set up in three steps" not in html and "Needs you" not in html


def test_pc_note_is_saved_and_shown(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    _, device_id, _, _ = enroll(env["dev"])
    r = c.post(f"/devices/{device_id}/edit",
               data={"csrf": tok, "name": "Reception", "note": "Next to the door"})
    assert r.status_code == 302
    dev = env["store"].device(device_id)
    assert (dev["name"], dev["note"]) == ("Reception", "Next to the door")
    assert "Next to the door" in c.get("/").get_data(as_text=True)
    assert "Next to the door" in c.get(f"/devices/{device_id}").get_data(as_text=True)
    c.post(f"/devices/{device_id}/edit", data={"csrf": tok, "name": "", "note": "x"})
    assert env["store"].device(device_id)["name"] == "Reception"   # unchanged


def test_admins_can_be_added_and_removed_from_the_panel(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    # Needs the current admin's own password.
    c.post("/admins/add", data={"csrf": tok, "username": "sara",
                                "password": "a-long-enough-password", "confirm": "wrong"})
    assert store.get_admin("sara") is None
    c.post("/admins/add", data={"csrf": tok, "username": "sa ra",
                                "password": "a-long-enough-password", "confirm": PW})
    assert store.get_admin("sa ra") is None                        # bad username
    c.post("/admins/add", data={"csrf": tok, "username": "sara",
                                "password": "short", "confirm": PW})
    assert store.get_admin("sara") is None
    c.post("/admins/add", data={"csrf": tok, "username": "sara",
                                "password": "a-long-enough-password", "confirm": PW})
    assert store.get_admin("sara") is not None
    html = c.get("/admins").get_data(as_text=True)
    assert "sara" in html and "(you)" in html
    # Can't remove yourself; can remove another, with your password.
    c.post("/admins/remove", data={"csrf": tok, "username": "admin", "confirm": PW})
    assert store.get_admin("admin") is not None
    c.post("/admins/remove", data={"csrf": tok, "username": "sara", "confirm": "wrong"})
    assert store.get_admin("sara") is not None
    c.post("/admins/remove", data={"csrf": tok, "username": "sara", "confirm": PW})
    assert store.get_admin("sara") is None
    actions = [r["action"] for r in store.audit_log()]
    assert "admin_added" in actions and "admin_removed" in actions


def test_two_step_reset_makes_the_admin_set_it_up_again(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    store.set_admin("sara", crypto.hash_password("a-long-enough-password"))
    store.set_totp("sara", crypto.new_totp_secret(), 5)
    c.post("/admins/reset-two-step", data={"csrf": tok, "username": "sara",
                                           "confirm": PW})
    assert store.get_admin("sara")["totp_secret"] == ""
    # Resetting your own ends your session too.
    r = c.post("/admins/reset-two-step", data={"csrf": tok, "username": "admin",
                                               "confirm": PW})
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert c.get("/").status_code == 302


def test_activity_page_speaks_plainly(env):
    c = lan(env["panel_app"])
    login(c, env)
    env["store"].audit("device", "enroll_refused", "ip=10.0.0.9")
    html = c.get("/audit").get_data(as_text=True)
    assert "A PC tried to join with a wrong join code" in html
    assert "enroll_refused" not in html
    assert "Signed in" in html                        # the login just made


# ----- taking over accounts the user added themselves -----

def _checkin(env, token, accounts=(), **extra):
    return env["dev"].post("/api/v1/checkin",
                           json={"app_version": "2.0.14", "accounts": list(accounts),
                                 **extra},
                           headers={"Authorization": f"Bearer {token}"})


def test_admin_takes_over_an_account_and_the_pc_hands_it_over(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    _, device_id, ptoken, pub = enroll(env["dev"], "Reception PC")
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub)})
    _checkin(env, ptoken, [{"email": "me@example.com", "managed": False, "kind": "password"},
                           {"email": "me@gmail.com", "managed": False, "kind": "google"}])
    page = c.get(f"/devices/{device_id}").get_data(as_text=True)
    assert "me@example.com" in page and "Take over" in page
    assert "Google sign-in" in page                    # the Gmail one can't be
    # Nothing is asked of the PC until the admin clicks.
    assert _checkin(env, ptoken).get_json()["collect"] == []
    # A hand-over nobody asked for is ignored.
    _checkin(env, ptoken, [{"email": "me@example.com", "managed": False}],
             handover=[{"email": "me@example.com", "password": "x",
                                     "imap_host": "h", "imap_port": 993,
                                     "smtp_host": "h", "smtp_port": 465}])
    assert store.mailbox_by_email("me@example.com") is None

    c.post(f"/devices/{device_id}/takeover", data={"csrf": tok, "email": "me@gmail.com"})
    assert store.takeovers_for(device_id) == []       # Google: refused
    c.post(f"/devices/{device_id}/takeover", data={"csrf": tok, "email": "me@example.com"})
    r = _checkin(env, ptoken, [{"email": "me@example.com", "managed": False}])
    assert r.get_json()["collect"] == ["me@example.com"]
    page = c.get(f"/devices/{device_id}").get_data(as_text=True)
    assert "Waiting for the user" in page

    r = _checkin(env, ptoken, [{"email": "me@example.com", "managed": False}],
                 handover=[{"email": "me@example.com", "password": "s3cret",
                            "display_name": "Me", "imap_host": "mail.example.com",
                            "imap_port": 993, "smtp_host": "mail.example.com",
                            "smtp_port": 465}]).get_json()
    mb = store.mailbox_by_email("me@example.com")
    assert mb is not None and mb["auth"] == "password" and mb["display_name"] == "Me"
    assert crypto.decrypt_stored(env["master"], mb["password_enc"],
                                 f"mailbox:{mb['id']}") == "s3cret"
    assert r["collect"] == []
    assert [m["email"] for m in r["managed"]] == ["me@example.com"]
    assert "secret" in r["managed"][0]                # sealed for this PC, same answer
    assert store.takeovers_for(device_id)[0]["status"] == "done"
    for path in ("/", f"/devices/{device_id}", "/mailboxes", "/audit"):
        assert "s3cret" not in c.get(path).get_data(as_text=True)


def test_declined_and_existing_mailbox_hand_overs(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    _, d1, t1, pub1 = enroll(env["dev"], "PC one")
    c.post(f"/devices/{d1}/approve", data={"csrf": tok, "check_code": crypto.check_code(pub1)})
    _checkin(env, t1, [{"email": "shared@example.com", "managed": False}])
    c.post(f"/devices/{d1}/takeover", data={"csrf": tok, "email": "shared@example.com"})
    _checkin(env, t1, [{"email": "shared@example.com", "managed": False}],
             declined=[{"email": "shared@example.com", "reason": "the user said no"}])
    assert store.takeovers_for(d1)[0]["status"] == "declined"
    assert "declined to hand over shared@example.com" in c.get("/").get_data(as_text=True)
    c.post(f"/devices/{d1}/takeover/ignore", data={"csrf": tok, "email": "shared@example.com"})
    assert store.takeovers_for(d1)[0]["status"] == "ignored"
    assert "declined to hand over" not in c.get("/").get_data(as_text=True)

    # The mailbox already exists (added by the admin): a hand-over from a
    # second PC just signs that PC in; the admin's password is kept.
    mid = add_mailbox(env, "shared@example.com", "admins-pw")
    _, d2, t2, pub2 = enroll(env["dev"], "PC two")
    c.post(f"/devices/{d2}/approve", data={"csrf": tok, "check_code": crypto.check_code(pub2)})
    _checkin(env, t2, [{"email": "shared@example.com", "managed": False}])
    c.post(f"/devices/{d2}/takeover", data={"csrf": tok, "email": "shared@example.com"})
    r = _checkin(env, t2, [{"email": "shared@example.com", "managed": False}],
                 handover=[{"email": "shared@example.com", "password": "pcs-pw",
                                     "imap_host": "h", "imap_port": 993,
                                     "smtp_host": "h", "smtp_port": 465}]).get_json()
    assert [m["email"] for m in r["managed"]] == ["shared@example.com"]
    mb = store.mailbox(mid)
    assert crypto.decrypt_stored(env["master"], mb["password_enc"], f"mailbox:{mid}") == "admins-pw"
    assert mb["imap_host"] == "mail.example.com"


# ----- what users do with company mailboxes -----

def test_user_activity_is_kept_shown_and_filtered(env, monkeypatch):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    add_mailbox(env, "sales@example.com")
    _, device_id, ptoken, pub = enroll(env["dev"], "Reception PC")
    events = [
        {"t": time.time() - 60, "email": "sales@example.com", "kind": "opened",
         "detail": {"subject": "Invoice 4411", "who": "acme@example.net",
                    "body": "never stored", "x" * 50: "junk"}},
        {"t": time.time() - 30, "email": "sales@example.com", "kind": "attachment_saved",
         "detail": {"subject": "Invoice 4411", "name": "invoice.pdf"}},
        {"t": time.time() - 20, "email": "personal@gmail.com", "kind": "opened",
         "detail": {"subject": "secret"}},                   # not a company mailbox
        {"t": time.time() - 10, "email": "", "kind": "app_start"},
        {"t": time.time() - 5, "email": "", "kind": "opened"},   # PC-level needs app_*
        {"t": "garbage", "email": "sales@example.com", "kind": "sent",
         "detail": {"subject": "Re: quote", "who": "bob@example.net"}},
    ]
    # Not approved yet: nothing is kept.
    _checkin(env, ptoken, events=events)
    assert store.events() == []
    c.post(f"/devices/{device_id}/approve",
           data={"csrf": tok, "check_code": crypto.check_code(pub)})
    _checkin(env, ptoken, events=events)
    kept = store.events()
    assert [e["kind"] for e in kept] == ["sent", "app_start", "attachment_saved", "opened"]
    assert all("secret" not in e["detail"] and "never stored" not in e["detail"]
               for e in kept)

    html = c.get("/users").get_data(as_text=True)
    assert "Opened \u201cInvoice 4411\u201d from acme@example.net" in html
    assert "Saved attachment invoice.pdf" in html
    assert "Sent \u201cRe: quote\u201d to bob@example.net" in html and "Started AMail" in html
    assert "secret" not in html
    html = c.get("/users?mailbox=sales@example.com").get_data(as_text=True)
    assert "Started AMail" not in html and "Invoice 4411" in html
    html = c.get(f"/users?pc={device_id}&day=2001-01-01").get_data(as_text=True)
    assert "Nothing recorded" in html
    assert "Invoice 4411" in c.get(f"/devices/{device_id}").get_data(as_text=True)

    store.prune_events(time.time() + 1)
    assert store.events() == []


# ----- hundreds of mailboxes -----

def test_import_list_parsing():
    from amail_admin.panel import parse_import
    entries, bad = parse_import(
        "sales@example.com:Str0ng:with:colons\n"
        "accounts@example.com, Pa ss, Accounts team, Ltd\n"
        "# a comment\n\n"
        "notanemail:pw\n"
        "nopassword@example.com\n"
        "UPPER@Example.com:x")
    assert entries == [
        {"email": "sales@example.com", "password": "Str0ng:with:colons", "display_name": ""},
        {"email": "accounts@example.com", "password": "Pa ss", "display_name": "Accounts team, Ltd"},
        {"email": "upper@example.com", "password": "x", "display_name": ""}]
    assert bad == ["notanemail:pw", "nopassword@example.com"]


def test_bulk_import_and_many_at_once_on_a_pc(env):
    c = lan(env["panel_app"])
    tok, _ = login(c, env)
    store = env["store"]
    lines = "\n".join(f"user{i}@example.com:pw{i}" for i in range(30))
    host = {"imap_host": "imap.zoho.com", "imap_port": "993",
            "smtp_host": "smtp.zoho.com", "smtp_port": "465"}
    # The first mailbox is checked against the host when asked; a failure
    # imports nothing.
    env["tested"].clear()
    env["panel_app"].view_functions  # noqa: B018 (keep flake quiet)
    r = c.post("/mailboxes/import", data={"csrf": tok, "lines": lines, "test": "1", **host})
    assert r.status_code == 302 and store.mailbox_count() == 30
    assert env["tested"] == [("user0@example.com", "pw0", "imap.zoho.com", 993,
                              "smtp.zoho.com", 465)]
    mb = store.mailbox_by_email("user7@example.com")
    assert crypto.decrypt_stored(env["master"], mb["password_enc"], f"mailbox:{mb['id']}") == "pw7"
    # Importing again adds nothing and says so; the server settings are remembered.
    c.post("/mailboxes/import", data={"csrf": tok, "lines": lines + "\nbad line", **host})
    assert store.mailbox_count() == 30
    page = c.get("/mailboxes").get_data(as_text=True)
    assert "30 company mailboxes, 30 on no PC" in page and 'value="imap.zoho.com"' in page
    assert "1 line(s) could not be read" in page
    # Search and the "on no PC" filter.
    page = c.get("/mailboxes?q=user2").get_data(as_text=True)
    assert "user2@example.com" in page and "user1@example.com" not in page

    # The overview switches from the grid to a PC list past 12 mailboxes,
    # and "find a mailbox" says where one is.
    _, device_id, ptoken, pub = enroll(env["dev"], "Desk 1")
    c.post(f"/devices/{device_id}/approve", data={"csrf": tok, "check_code": crypto.check_code(pub)})
    page = c.get("/").get_data(as_text=True)
    assert "grid-tbl" not in page and "30 mailboxes in all" in page

    # Tick many at once on the PC's page.
    ids = [store.mailbox_by_email(f"user{i}@example.com")["id"] for i in range(25)]
    from werkzeug.datastructures import MultiDict
    r = c.post(f"/devices/{device_id}/grant",
               data=MultiDict([("csrf", tok)] + [("mailbox_id", str(i)) for i in ids]))
    assert r.status_code == 302 and len(store.granted_mailboxes(device_id)) == 25
    page = c.get(f"/devices/{device_id}?q=user2").get_data(as_text=True)
    assert "user25@example.com" in page
    assert 'type="checkbox" name="mailbox_id" value="%d"' % ids[0] not in page   # already on it
    page = c.get("/?q=user7").get_data(as_text=True)
    assert "user7@example.com" in page and "Desk 1" in page
    assert "Only mailboxes on no PC" in c.get("/mailboxes?only=none").get_data(as_text=True)
    assert "user7@example.com" not in c.get("/mailboxes?only=none").get_data(as_text=True)

    # Copy everything to a second PC, then sign it out of all.
    _, d2, t2, pub2 = enroll(env["dev"], "Desk 2")
    c.post(f"/devices/{d2}/approve", data={"csrf": tok, "check_code": crypto.check_code(pub2)})
    c.post(f"/devices/{d2}/copy", data={"csrf": tok, "source": device_id})
    assert len(store.granted_mailboxes(d2)) == 25
    assert len(checkin(env["dev"], t2).get_json()["managed"]) == 25
    c.post(f"/devices/{d2}/ungrant-all", data={"csrf": tok})
    assert store.granted_mailboxes(d2) == []
    for path in ("/", "/mailboxes", f"/devices/{device_id}", "/audit"):
        assert "pw7" not in c.get(path).get_data(as_text=True)


def test_token_rotation_recovers_when_the_pc_never_took_the_new_token(env, monkeypatch):
    """A PC that keeps using its old token (the answer with the new one was
    lost, or it couldn't be stored) gets another one, instead of the old one
    staying valid for ever."""
    from amail_admin import device_api
    store, dev = env["store"], env["dev"]
    _, device_id, old, _ = enroll(dev)
    store._x("UPDATE devices SET token_issued_at=? WHERE id=?",
             (time.time() - 31 * 86400, device_id))
    first = checkin(dev, old).get_json()["new_token"]
    # Right away: the PC is given time to store it.
    assert "new_token" not in checkin(dev, old).get_json()
    # Still on the old token ten minutes later: a fresh one is issued.
    real = time.time
    monkeypatch.setattr(device_api.time, "time",
                        lambda: real() + device_api.TOKEN_RETRY_AFTER + 1)
    second = checkin(dev, old).get_json()["new_token"]
    assert second != first
    assert checkin(dev, first).status_code == 401           # the unused one is gone
    assert checkin(dev, old).status_code == 200             # the old still works...
    assert checkin(dev, second).status_code == 200
    assert checkin(dev, old).status_code == 401             # ...until the new is used


def test_rate_limiter_forgets_quiet_clients():
    from amail_admin import ratelimit
    now = [0.0]
    lim = RateLimiter(clock=lambda: now[0])
    for i in range(10_000):
        assert lim.allow(f"badtoken:10.0.{i // 256}.{i % 256}", limit=30, per_seconds=600)
    assert len(lim) == 10_000
    now[0] += 601 + ratelimit.SWEEP_EVERY
    lim.allow("device:1", limit=20, per_seconds=60)
    assert len(lim) == 1
    # A window still open is kept, and still enforced.
    for _ in range(19):
        assert lim.allow("device:1", limit=20, per_seconds=60)
    assert not lim.allow("device:1", limit=20, per_seconds=60)
    now[0] += ratelimit.SWEEP_EVERY
    lim.allow("other", limit=1, per_seconds=1)
    assert len(lim) == 2
