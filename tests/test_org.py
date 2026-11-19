"""This PC's side of the organization admin backend.

The backend's own crypto module seals passwords and signs leases here, so these
tests prove the app and the server agree on the formats, not just that each is
self-consistent.
"""
from __future__ import annotations

import http.server
import json
import ssl
import sys
import threading
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from amail_admin import crypto as server_crypto   # noqa: E402

from emailclient import org                       # noqa: E402

SERVER_SIGN = Ed25519PrivateKey.generate()
SIGNPUB = server_crypto.signing_public(SERVER_SIGN)
HOST = "mail.example.com"


def servers(host=HOST):
    return org.server_tag(host, 993, host, 465)


def raw_pub(priv) -> bytes:
    return priv.public_key().public_bytes(serialization.Encoding.Raw,
                                          serialization.PublicFormat.Raw)


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    """Keep every test away from the real QSettings and keychain."""
    prefs, secrets = {}, {}
    monkeypatch.setattr(org, "_get", lambda k: prefs.get(k, ""))
    monkeypatch.setattr(org, "_set", lambda k, v: prefs.__setitem__(k, v))
    monkeypatch.setattr(org, "_secret_get", lambda n: secrets.get(n))

    def secret_set(n, v):
        if v is None:
            secrets.pop(n, None)
        else:
            secrets[n] = v
    monkeypatch.setattr(org, "_secret_set", secret_set)
    return prefs, secrets


def set_lease(storage, t, device_id="dev1"):
    storage[0][org._K_LEASE] = json.dumps(
        server_crypto.sign_lease(SERVER_SIGN, device_id, t))


def joined_device(storage, device_id="dev1"):
    """Join with a fresh key pair; returns the public key the server holds."""
    prefs, secrets = storage
    key = X25519PrivateKey.generate()
    secrets[org._S_KEY] = org._b64e(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()))
    secrets[org._S_TOKEN] = "tok"
    prefs[org._K_DEVICE] = device_id
    prefs[org._K_STATE] = "approved"
    prefs[org._K_SIGNPUB] = org._b64e(SIGNPUB)
    set_lease(storage, time.time(), device_id)
    return raw_pub(key)


def seal(pub, email, version=1, pw="pw", host=HOST):
    return server_crypto.seal_for_device(
        pub, "dev1", email, version, pw,
        server_crypto.server_tag(host, 993, host, 465))


def account(email, host=HOST):
    from emailclient.models import Account
    return Account(id=1, name=email, email=email, provider="custom",
                   imap_host=host, imap_port=993, smtp_host=host,
                   smtp_port=465, color="")


# ----- setup code -----

def code_for(url="https://srv:8443", fp="ab" * 32, enroll="ABCD-EFGH", signpub=SIGNPUB):
    return server_crypto.make_setup_code(url, fp, enroll, signpub)


def test_setup_code_from_the_panel_parses():
    c = code_for()
    info = org.parse_setup_code(c[:20] + "\n  " + c[20:])   # pasted across lines
    assert info == {"url": "https://srv:8443", "fp": "ab" * 32,
                    "code": "ABCD-EFGH", "signpub": org._b64e(SIGNPUB)}


@pytest.mark.parametrize("bad", [
    "", "hello", "AMAIL1.!!!",
    code_for(url="http://srv:8443"),        # not https
    code_for(fp="xyz"),                     # bad pin
    code_for(signpub=b"short"),             # bad signing key
])
def test_bad_setup_codes_are_refused(bad):
    with pytest.raises(org.OrgError):
        org.parse_setup_code(bad)


def test_a_build_pinned_to_one_server_refuses_others(monkeypatch):
    monkeypatch.setattr(org, "PINNED_SERVER_FINGERPRINTS", ("cd" * 32,))
    with pytest.raises(org.OrgError):
        org.parse_setup_code(code_for(fp="ab" * 32))
    with pytest.raises(org.OrgError):
        org.parse_setup_code(code_for(fp=""))           # unpinned code too
    assert org.parse_setup_code(code_for(fp="cd" * 32))["fp"] == "cd" * 32


def test_check_code_matches_the_panels(storage):
    pub = joined_device(storage)
    assert org.check_code() == server_crypto.check_code(pub)


# ----- sealed passwords -----

def test_password_opens_only_for_the_administrators_servers(storage):
    pub = joined_device(storage)
    org.store_grant("sales@example.com", 4, seal(pub, "Sales@Example.com", 4, "pw!"), {})
    assert org.is_managed("SALES@example.com")
    assert org.password_for("sales@example.com", servers()) == "pw!"
    # Someone edits the local database to point the account elsewhere:
    assert org.password_for("sales@example.com", servers("evil.example.net")) is None
    assert "pw!" not in json.dumps(storage[0])          # stored sealed only


def test_a_box_for_another_pc_is_refused(storage):
    joined_device(storage)
    other = raw_pub(X25519PrivateKey.generate())
    assert not org.validate_box("a@b.com", 1, seal(other, "a@b.com"), servers())


def test_credentials_use_the_accounts_own_servers(storage, monkeypatch):
    from emailclient import credentials
    pub = joined_device(storage)
    org.store_grant("a@b.com", 1, seal(pub, "a@b.com", pw="managed-pw"), {})
    calls = []
    monkeypatch.setattr(credentials.keyring, "set_password", lambda *a: calls.append(a))
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: "personal-pw")
    assert credentials.password_for_account(account("a@b.com")) == "managed-pw"
    assert credentials.password_for_account(account("a@b.com", "evil.net")) is None
    assert credentials.get_password("a@b.com") is None    # no email-only path
    credentials.set_password("a@b.com", "typed-by-user")  # refused silently
    assert calls == []
    assert credentials.password_for_account(account("me@b.com")) == "personal-pw"


def test_connection_test_wont_send_it_to_another_server(storage):
    from emailclient import conntest
    pub = joined_device(storage)
    org.store_grant("a@b.com", 1, seal(pub, "a@b.com", pw="pw"), {})
    acc = account("a@b.com")
    assert conntest._credentials(acc, None, HOST, 993) == (None, "pw")
    assert conntest._credentials(acc, None, "evil.net", 993) == (None, "")
    assert conntest._credentials(acc, None, HOST, 465, smtp=True) == (None, "pw")


def test_leave_forgets_everything(storage):
    pub = joined_device(storage)
    org.store_grant("a@b.com", 1, seal(pub, "a@b.com"), {})
    org.leave()
    assert not org.is_joined() and not org.is_managed("a@b.com")
    assert storage[1] == {}


def test_export_skips_managed_accounts(storage, tmp_path, monkeypatch):
    from emailclient import account_transfer, credentials
    from emailclient.db import Database
    from emailclient.models import Account
    pub = joined_device(storage)
    org.store_grant("work@b.com", 1, seal(pub, "work@b.com"), {})
    monkeypatch.setattr(credentials.keyring, "get_password", lambda *a: "")
    db = Database(tmp_path / "mail.db")
    for e in ("work@b.com", "me@b.com"):
        db.add_account(Account(id=None, name=e, email=e, provider="custom",
                               imap_host="h", imap_port=993, smtp_host="h",
                               smtp_port=465, color=""))
    blob = account_transfer.export_accounts(db, "pass-phrase-123")
    payload = json.loads(account_transfer.unseal(blob, "pass-phrase-123"))
    assert [a["email"] for a in payload["accounts"]] == ["me@b.com"]


# ----- the signed 7-day limit -----

def test_limit_counts_from_the_last_signed_server_time(storage):
    joined_device(storage)
    now = time.time()
    set_lease(storage, now - 6 * 86400)
    assert not org.lease_expired(now)
    set_lease(storage, now - 8 * 86400)
    assert org.lease_expired(now)
    set_lease(storage, now)
    assert org.lease_expired(now - 86400)        # clock set back
    assert not org.lease_expired(now + 60)


def test_editing_the_stored_time_counts_for_nothing(storage):
    joined_device(storage)
    lease = json.loads(storage[0][org._K_LEASE])
    lease["t"] = int(time.time()) + 86400 * 365          # "fresh for a year"
    storage[0][org._K_LEASE] = json.dumps(lease)
    assert org.lease_expired()
    forged = server_crypto.sign_lease(Ed25519PrivateKey.generate(), "dev1", time.time())
    storage[0][org._K_LEASE] = json.dumps(forged)        # signed by someone else
    assert org.lease_expired()


def test_limit_can_be_shortened_for_testing_never_lengthened(monkeypatch):
    monkeypatch.setenv("AMAIL_ORG_LEASE_SECONDS", "120")
    assert org.lease_seconds() == 120
    monkeypatch.setenv("AMAIL_ORG_LEASE_SECONDS", str(30 * 86400))
    assert org.lease_seconds() == 7 * 86400
    monkeypatch.setenv("AMAIL_ORG_LEASE_SECONDS", "junk")
    assert org.lease_seconds() == 7 * 86400


def test_not_joined_never_expires():
    assert not org.lease_expired()


def test_expired_pc_cannot_open_managed_passwords(storage):
    pub = joined_device(storage)
    org.store_grant("a@b.com", 1, seal(pub, "a@b.com"), {})
    assert org.password_for("a@b.com", servers()) == "pw"
    set_lease(storage, time.time() - 8 * 86400)
    assert org.password_for("a@b.com", servers()) is None


def test_a_signed_answer_renews_it_an_unsigned_one_does_not(storage, monkeypatch):
    joined_device(storage)
    set_lease(storage, time.time() - 8 * 86400)
    good = server_crypto.sign_lease(SERVER_SIGN, "dev1", time.time())
    monkeypatch.setattr(org, "_post", lambda *a, **k: (
        200, {"state": "approved", "managed": [], "lease": good}))
    assert org.checkin([], "t") is not None and not org.lease_expired()

    set_lease(storage, time.time() - 8 * 86400)
    bad = server_crypto.sign_lease(Ed25519PrivateKey.generate(), "dev1", time.time())
    monkeypatch.setattr(org, "_post", lambda *a, **k: (
        200, {"state": "approved", "managed": [], "lease": bad}))
    assert org.checkin([], "t") is None and org.lease_expired()

    monkeypatch.setattr(org, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert org.checkin([], "t") is None and org.lease_expired()


def test_a_new_token_from_the_server_is_kept(storage, monkeypatch):
    joined_device(storage)
    lease = server_crypto.sign_lease(SERVER_SIGN, "dev1", time.time())
    monkeypatch.setattr(org, "_post", lambda *a, **k: (
        200, {"state": "approved", "managed": [], "lease": lease,
              "new_token": "fresh"}))
    org.checkin([], "t")
    assert storage[1][org._S_TOKEN] == "fresh"


def test_engine_does_not_start_an_expired_managed_mailbox(storage, tmp_path):
    from emailclient.db import Database
    from emailclient.sync.engine import SyncEngine
    pub = joined_device(storage)
    org.store_grant("work@b.com", 1, seal(pub, "work@b.com"), {})
    set_lease(storage, time.time() - 8 * 86400)
    eng = SyncEngine.__new__(SyncEngine)
    eng.db, eng.workers, eng.auth_failed = Database(tmp_path / "mail.db"), {}, set()
    eng.start_account(account("work@b.com"))
    assert eng.workers == {}


# ----- certificate pinning -----

@pytest.fixture()
def tls_server(tmp_path):
    cert, key = tmp_path / "s.crt", tmp_path / "s.key"
    server_crypto.ensure_certificate(cert, key)
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps({"state": "pending", "managed": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield (f"https://127.0.0.1:{httpd.server_address[1]}",
           server_crypto.certificate_fingerprint(cert), seen)
    httpd.shutdown()


def test_pinned_server_is_trusted(tls_server):
    url, fp, seen = tls_server
    status, data = org._post(url, fp, "/api/v1/checkin", {}, token="secret")
    assert status == 200 and seen == [("/api/v1/checkin", "Bearer secret")]


def test_wrong_certificate_gets_nothing(tls_server):
    url, _fp, seen = tls_server
    with pytest.raises(org.OrgError):
        org._post(url, "00" * 32, "/api/v1/checkin", {}, token="secret")
    assert seen == []


def test_unpinned_self_signed_server_is_refused(tls_server):
    url, _fp, seen = tls_server
    with pytest.raises(ssl.SSLError):
        org._post(url, "", "/api/v1/checkin", {}, token="secret")
    assert seen == []


# ----- reconcile (the main window applying the server's answer) -----

class _Running:
    def isRunning(self):
        return True


class _Engine:
    def __init__(self):
        self.started, self.stopped, self.auth_failed = [], [], set()
        self.workers = {}

    def start_account(self, acc):
        self.started.append(acc.email)
        self.workers[acc.id] = _Running()

    def stop_account(self, acc_id):
        self.stopped.append(acc_id)
        self.workers.pop(acc_id, None)


class _Fake:
    """Just enough of MainWindow to run its organization methods."""
    from emailclient.ui.main_window import MainWindow as _MW
    _apply_org = _MW._apply_org
    _apply_org_inner = _MW._apply_org_inner
    _drop_managed_accounts = _MW._drop_managed_accounts
    _resume_after_lease = _MW._resume_after_lease
    _enforce_org_lease = _MW._enforce_org_lease
    _handle_collect = _MW._handle_collect
    _queue_declined = _MW._queue_declined
    _account_kind = _MW._account_kind

    def __init__(self, db, allow_take_over=True):
        self.db, self.engine, self.removed, self.msgs = db, _Engine(), [], []
        self._org_lease_paused = False
        self.asked = []
        self.asked_handover = []
        self._allow = allow_take_over
        import threading
        self._org_lock = threading.Lock()
        self._org_handover, self._org_declined = [], []
        self._org_collect_seen = set()

        class _T:
            def setInterval(self, *_):
                pass

            def stop(self):
                pass

        class _S:
            def refresh(self):
                pass
        self._org_timer, self.sidebar = _T(), _S()
        outer = self

        class _Bar:
            def showMessage(self, m, *_):
                outer.msgs.append(m)
        self._bar = _Bar()

    def _confirm_take_over(self, email):
        self.asked.append(email)
        return self._allow

    def _confirm_handover(self, emails):
        self.asked_handover.extend(emails)
        self.handover_dialogs = getattr(self, "handover_dialogs", 0) + 1
        return self._allow

    def _org_tick(self):
        pass

    def statusBar(self):
        return self._bar

    def _rebuild_tabs(self):
        pass

    def _remove_accounts(self, ids):
        self.removed += ids
        for i in ids:
            self.db.delete_account(i)


def _item(pub, email, version=1, pw="pw", host=HOST):
    return {"email": email, "name": "Sales", "imap_host": host, "imap_port": 993,
            "smtp_host": host, "smtp_port": 465, "version": version,
            "secret": seal(pub, email, version, pw, host)}


def test_reconcile_signs_in_updates_and_signs_out(storage, tmp_path, qtbot, monkeypatch):
    from emailclient import credentials
    from emailclient.db import Database
    monkeypatch.setattr(credentials.keyring, "delete_password", lambda *a: None)
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))

    w._apply_org({"state": "approved", "managed": [_item(pub, "sales@example.com")]})
    acc = w.db.list_accounts()[0]
    assert acc.email == "sales@example.com" and acc.imap_host == HOST
    assert w.engine.started == ["sales@example.com"]
    assert credentials.password_for_account(acc) == "pw"

    same = dict(_item(pub, "sales@example.com"))
    same.pop("secret")
    w._apply_org({"state": "approved", "managed": [same]})
    assert w.engine.stopped == []

    w._apply_org({"state": "approved", "managed": [
        _item(pub, "sales@example.com", version=2, pw="new", host="mx.example.com")]})
    acc = w.db.list_accounts()[0]
    assert acc.imap_host == "mx.example.com"
    assert credentials.password_for_account(acc) == "new"
    assert len(w.engine.stopped) == 1

    w._apply_org({"state": "approved", "managed": []})
    assert w.db.list_accounts() == [] and not org.is_managed("sales@example.com")


def test_personal_account_is_only_taken_over_with_the_users_ok(storage, tmp_path, qtbot):
    from emailclient.db import Database
    from emailclient.models import Account
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"), allow_take_over=False)
    w.db.add_account(Account(id=None, name="me", email="me@example.com",
                             provider="custom", imap_host="my.host", imap_port=993,
                             smtp_host="my.host", smtp_port=465, color=""))
    w._apply_org({"state": "approved", "managed": [_item(pub, "me@example.com")]})
    assert w.asked == ["me@example.com"] and not org.is_managed("me@example.com")
    assert w.db.list_accounts()[0].imap_host == "my.host"     # untouched
    w._apply_org({"state": "approved", "managed": [_item(pub, "me@example.com")]})
    assert w.asked == ["me@example.com"]                       # not asked again

    w2 = _Fake(w.db, allow_take_over=True)
    org.set_declined("me@example.com", False)
    w2._apply_org({"state": "approved", "managed": [_item(pub, "me@example.com")]})
    assert org.is_managed("me@example.com")
    assert w2.db.list_accounts()[0].imap_host == HOST


def test_reconcile_leaves_personal_accounts_alone(storage, tmp_path, qtbot):
    from emailclient.db import Database
    from emailclient.models import Account
    joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    w.db.add_account(Account(id=None, name="me", email="me@gmail.com",
                             provider="gmail", imap_host="h", imap_port=993,
                             smtp_host="h", smtp_port=465, color=""))
    w._apply_org({"state": "approved", "managed": []})
    assert [a.email for a in w.db.list_accounts()] == ["me@gmail.com"]


def test_blocked_pc_drops_managed_accounts_and_leaves(storage, tmp_path, qtbot, monkeypatch):
    from emailclient.db import Database
    from emailclient.ui import main_window
    monkeypatch.setattr(main_window.QMessageBox, "information", lambda *a: None)
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    w._apply_org({"state": "approved", "managed": [_item(pub, "a@example.com")]})
    w._apply_org({"state": "revoked", "managed": []})
    assert w.db.list_accounts() == [] and not org.is_joined()


def test_a_box_this_pc_cannot_open_is_ignored(storage, tmp_path, qtbot):
    from emailclient.db import Database
    joined_device(storage)
    other = raw_pub(X25519PrivateKey.generate())
    w = _Fake(Database(tmp_path / "mail.db"))
    w._apply_org({"state": "approved", "managed": [_item(other, "a@example.com")]})
    assert w.db.list_accounts() == [] and not org.is_managed("a@example.com")


def test_a_box_sealed_for_other_servers_is_ignored(storage, tmp_path, qtbot):
    from emailclient.db import Database
    pub = joined_device(storage)
    item = _item(pub, "a@example.com")
    item["imap_host"] = "evil.example.net"          # config and seal disagree
    w = _Fake(Database(tmp_path / "mail.db"))
    w._apply_org({"state": "approved", "managed": [item]})
    assert w.db.list_accounts() == []


def test_paused_mailboxes_resume_when_the_server_answers(storage, tmp_path, qtbot, monkeypatch):
    from emailclient.db import Database
    from emailclient.ui import main_window
    warned = []
    monkeypatch.setattr(main_window.QMessageBox, "warning",
                        lambda *a: warned.append(a[2]))
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    w._apply_org({"state": "approved", "managed": [_item(pub, "a@example.com")]})
    acc_id = w.db.list_accounts()[0].id

    set_lease(storage, time.time() - 8 * 86400)
    w._enforce_org_lease()
    assert acc_id in w.engine.stopped and "paused" in warned[0]
    w._enforce_org_lease()
    assert len(warned) == 1

    set_lease(storage, time.time())
    started = len(w.engine.started)
    same = dict(_item(pub, "a@example.com"))
    same.pop("secret")
    w._apply_org({"state": "approved", "managed": [same]})
    assert len(w.engine.started) == started + 1
    assert any("resumed" in m for m in w.msgs)


# ----- Google / Microsoft managed mailboxes -----

GMAIL = ("imap.gmail.com", 993, "smtp.gmail.com", 465)


def gmail_account(email="team@gmail.com", host="imap.gmail.com"):
    from emailclient.models import Account
    return Account(id=1, name=email, email=email, provider="gmail",
                   imap_host=host, imap_port=993, smtp_host="smtp.gmail.com",
                   smtp_port=465, color="")


def grant_oauth(storage, kind="google", email="team@gmail.com", rt="rt-1", version=1):
    pub = joined_device(storage)
    box = server_crypto.seal_for_device(pub, "dev1", email, version, rt,
                                        server_crypto.server_tag(*GMAIL))
    org.store_grant(email, version, box, {"auth": kind,
                                          "oauth_client": {"id": "cid", "secret": "cs"}})


def test_google_token_from_the_sealed_sign_in(storage, monkeypatch):
    from emailclient import google_oauth
    org._access_cache.clear()
    grant_oauth(storage)
    calls = []
    monkeypatch.setattr(org, "_token_request", lambda url, f: calls.append((url, f))
                        or {"access_token": "at-1", "expires_in": 3600})
    assert google_oauth.has_login("team@gmail.com")
    assert google_oauth.get_token("team@gmail.com", gmail_account()) == "at-1"
    url, fields = calls[0]
    assert "googleapis" in url and fields["refresh_token"] == "rt-1"
    assert fields["client_id"] == "cid" and fields["client_secret"] == "cs"
    assert google_oauth.get_token("team@gmail.com", gmail_account()) == "at-1"
    assert len(calls) == 1                               # cached for the hour
    # Pointed at another server: nothing.
    assert google_oauth.get_token("team@gmail.com", gmail_account(host="evil.net")) is None
    # Without the account (no server binding possible): nothing.
    assert google_oauth.get_token("team@gmail.com") is None


def test_microsoft_keeps_its_newest_refresh_token(storage, monkeypatch):
    from emailclient import oauth
    org._access_cache.clear()
    grant_oauth(storage, kind="microsoft", email="team@outlook.com")
    seen = []

    def fake(url, f):
        seen.append(f["refresh_token"])
        return {"access_token": f"at-{len(seen)}", "refresh_token": f"rt-{len(seen) + 1}",
                "expires_in": 1}
    monkeypatch.setattr(org, "_token_request", fake)
    acc = gmail_account("team@outlook.com")
    assert oauth.get_token("team@outlook.com", acc) == "at-1"
    assert oauth.get_token("team@outlook.com", acc) == "at-2"
    assert seen == ["rt-1", "rt-2"]                       # used the rotated one


def test_signing_out_drops_kept_tokens(storage, monkeypatch):
    org._access_cache.clear()
    grant_oauth(storage, kind="microsoft", email="team@outlook.com")
    monkeypatch.setattr(org, "_token_request", lambda u, f: {
        "access_token": "at", "refresh_token": "rotated", "expires_in": 3600})
    org.oauth_access_token(gmail_account("team@outlook.com"))
    assert any(k.startswith("org:rt:") for k in storage[1])
    org.forget("team@outlook.com")
    assert not any(k.startswith("org:rt:") for k in storage[1])
    assert org._access_cache == {}


def test_reconcile_makes_a_gmail_sign_in_account(storage, tmp_path, qtbot):
    from emailclient.db import Database
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    item = {"email": "team@gmail.com", "name": "Team", "imap_host": "imap.gmail.com",
            "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465,
            "version": 1, "auth": "google",
            "oauth_client": {"id": "cid", "secret": "cs"},
            "secret": server_crypto.seal_for_device(
                pub, "dev1", "team@gmail.com", 1, "rt",
                server_crypto.server_tag(*GMAIL))}
    w._apply_org({"state": "approved", "managed": [item]})
    acc = w.db.list_accounts()[0]
    assert acc.provider == "gmail" and org.auth_kind("team@gmail.com") == "google"


def test_stopping_a_busy_worker_keeps_it_alive_until_it_finishes(qtbot):
    """Removing an account whose worker is stuck in a network call used to
    drop the running QThread, which aborts the app."""
    import threading
    from PySide6.QtCore import QThread
    from emailclient.sync.engine import SyncEngine
    release = threading.Event()

    class Busy(QThread):
        def stop(self):
            pass                         # can't interrupt this call

        def run(self):
            release.wait(10)

    eng = SyncEngine.__new__(SyncEngine)
    SyncEngine.__init__(eng, None)
    w = Busy()
    eng.workers[1] = w
    w.start()
    orig = QThread.wait
    w.wait = lambda ms=0: orig(w, 50)    # don't sit through the 3 s here
    eng.stop_account(1)
    assert 1 not in eng.workers and w in eng._stopping and w.isRunning()
    release.set()
    qtbot.waitUntil(lambda: w not in eng._stopping, timeout=3000)


# ----- handing a personal account over to the organization -----

def _personal(db, email, provider="custom", host="my.host"):
    from emailclient.models import Account
    return db.add_account(Account(id=None, name="Me", email=email, provider=provider,
                                  imap_host=host, imap_port=993, smtp_host=host,
                                  smtp_port=465, color=""))


def test_handover_asks_once_then_sends_the_password(storage, tmp_path, qtbot, monkeypatch):
    from emailclient import credentials
    from emailclient.db import Database
    monkeypatch.setattr(credentials.keyring, "get_password",
                        lambda svc, key: "mypw" if key == "me@example.com" else None)
    monkeypatch.setattr(credentials.keyring, "delete_password", lambda *a: None)
    pub = joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    _personal(w.db, "me@example.com")

    w._apply_org({"state": "approved", "managed": [],
                  "collect": ["me@example.com", "nobody@example.com"]})
    assert w.asked_handover == ["me@example.com"]
    assert w._org_handover == [{"email": "me@example.com", "password": "mypw",
                                "display_name": "Me", "imap_host": "my.host",
                                "imap_port": 993, "smtp_host": "my.host",
                                "smtp_port": 465}]
    assert w._org_declined == [{"email": "nobody@example.com", "reason": "not on this PC"}]
    assert org.handed() == {"me@example.com"}
    # Still being asked for while the answer is on its way: not asked twice.
    w._apply_org({"state": "approved", "managed": [], "collect": ["me@example.com"]})
    assert w.asked_handover == ["me@example.com"]
    # The server now manages it: applied without the usual take-over question.
    w._apply_org({"state": "approved",
                  "managed": [_item(pub, "me@example.com", host="my.host")]})
    assert w.asked == [] and org.is_managed("me@example.com")
    assert org.handed() == set()
    assert w.db.list_accounts()[0].imap_host == "my.host"


def test_handover_declined_or_impossible_is_reported(storage, tmp_path, qtbot, monkeypatch):
    from emailclient import credentials, google_oauth
    from emailclient.db import Database
    monkeypatch.setattr(credentials.keyring, "get_password",
                        lambda svc, key: "mypw" if key == "me@example.com" else None)
    monkeypatch.setattr(google_oauth, "has_login", lambda e: e == "me@gmail.com")
    joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"), allow_take_over=False)
    _personal(w.db, "me@example.com")
    _personal(w.db, "me@gmail.com", provider="gmail")
    _personal(w.db, "me@outlook.com", provider="outlook")

    w._apply_org({"state": "approved", "managed": [],
                  "collect": ["me@example.com", "me@gmail.com", "me@outlook.com"]})
    assert w.asked_handover == ["me@example.com"]        # only a password account is asked
    assert w._org_handover == [] and org.handed() == set()
    assert {d["email"]: d["reason"] for d in w._org_declined} == {
        "me@example.com": "the user said no",
        "me@gmail.com": "signs in with Google",
        "me@outlook.com": "signs in with Microsoft"}
    # The server stops asking, then the admin asks again later: asked again.
    w._apply_org({"state": "approved", "managed": [], "collect": []})
    w._apply_org({"state": "approved", "managed": [], "collect": ["me@example.com"]})
    assert w.asked_handover == ["me@example.com", "me@example.com"]


def test_a_forgotten_handover_is_dropped_and_leave_clears_it(storage, tmp_path, qtbot):
    joined_device(storage)
    org.set_handed("gone@example.com", True)
    from emailclient.db import Database
    w = _Fake(Database(tmp_path / "mail.db"))
    w._apply_org({"state": "approved", "managed": [], "collect": []})
    assert org.handed() == set()                         # the server never confirmed it
    org.set_handed("x@example.com", True)
    org.leave()
    assert org.handed() == set()


# ----- recording what the user does with company mailboxes -----

@pytest.fixture()
def queue(tmp_path, monkeypatch):
    from emailclient import activity
    monkeypatch.setattr(activity, "_path", lambda: tmp_path / "q.jsonl")
    activity.bind(None)
    return tmp_path / "q.jsonl"


def test_only_managed_mailboxes_are_recorded(storage, queue):
    from emailclient import activity
    activity.record("me@example.com", "opened", subject="x")     # not joined
    assert not queue.exists()
    joined_device(storage)
    org.store_grant("sales@example.com", 1, {}, {})
    activity.record("personal@gmail.com", "opened", subject="private")
    activity.record("", "opened", subject="no mailbox")
    activity.record("Sales@Example.com", "opened", subject="Invoice", who="acme",
                    body="never")
    activity.record("", "app_start")
    events, n = activity.take()
    assert n == 2
    assert events[0]["email"] == "sales@example.com"
    assert events[0]["detail"] == {"subject": "Invoice", "who": "acme", "body": "never"}
    assert events[1]["kind"] == "app_start"
    assert "private" not in queue.read_text()


def test_queue_is_acked_only_after_the_server_took_it(storage, queue, monkeypatch):
    from emailclient import activity
    joined_device(storage)
    org.store_grant("sales@example.com", 1, {}, {})
    for i in range(3):
        activity.record("sales@example.com", "opened", subject=str(i))
    events, n = activity.take()
    activity.record("sales@example.com", "opened", subject="late")   # after take
    activity.ack(n)
    left, _ = activity.take()
    assert [e["detail"]["subject"] for e in left] == ["late"]

    sent = {}
    monkeypatch.setattr(org, "_post", lambda url, fp, path, body, token=None: (
        sent.update(body) or (200, {"state": "approved", "managed": [],
                                    "lease": json.loads(storage[0][org._K_LEASE])})))
    org.checkin([], "2.0.14", events=left)
    assert sent["events"] == left


def test_discard_records_on_managed_mail_only(storage, queue, tmp_path):
    from emailclient import activity
    from emailclient.db import Database
    from emailclient.models import EmailMessage
    from emailclient.ui.mail_actions import bulk_discard
    joined_device(storage)
    db = Database(tmp_path / "mail.db")
    activity.bind(db)
    company = _personal(db, "sales@example.com")
    private = _personal(db, "me@gmail.com", provider="gmail")
    org.store_grant("sales@example.com", 1, {}, {})
    for aid, subj in ((company, "Invoice"), (private, "Holiday")):
        db.save_message(EmailMessage(account_id=aid, folder="INBOX", uid=1,
                                     subject=subj, sender="x@y", recipients="",
                                     date="2026-01-01", body_text="", body_html="",
                                     is_read=True))
    ids = [r["id"] for r in db.conn.execute("SELECT id FROM messages")]
    bulk_discard(db, ids, "delete")
    events, _ = activity.take()
    assert [(e["kind"], e["detail"]["subject"]) for e in events] == [("deleted", "Invoice")]


def test_many_accounts_are_asked_for_in_one_dialog(storage, tmp_path, qtbot, monkeypatch):
    from emailclient import credentials
    from emailclient.db import Database
    monkeypatch.setattr(credentials.keyring, "get_password", lambda svc, key: "pw")
    joined_device(storage)
    w = _Fake(Database(tmp_path / "mail.db"))
    emails = [f"user{i}@example.com" for i in range(30)]
    for e in emails:
        _personal(w.db, e)
    w._apply_org({"state": "approved", "managed": [], "collect": emails})
    assert w.handover_dialogs == 1 and w.asked_handover == emails
    assert [h["email"] for h in w._org_handover] == emails
