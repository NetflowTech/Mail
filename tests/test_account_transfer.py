"""Account backup file: key stretching and format compatibility."""
from __future__ import annotations

import os
import struct

import pytest

from emailclient import account_transfer as at


class TestSeal:
    def test_roundtrip(self):
        blob = at.seal("hunter2", b'{"version": 1}')
        assert blob.startswith(b"OSMACCT4\n")
        assert at.unseal(blob, "hunter2") == b'{"version": 1}'

    def test_uses_600k_rounds_and_records_them(self):
        assert at._PBKDF2_ITERS >= 600_000
        blob = at.seal("pw", b"x")
        (iters,) = struct.unpack(">I", blob[9:13])
        assert iters == at._PBKDF2_ITERS

    def test_wrong_password_is_rejected(self):
        blob = at.seal("right", b"secret")
        with pytest.raises(ValueError):
            at.unseal(blob, "wrong")

    def test_legacy_200k_file_still_imports(self):
        # A file written by the previous release (OSMACCT2, fixed 200k rounds).
        salt = os.urandom(16)
        legacy = (b"OSMACCT2\n" + salt
                  + at._encrypt("pw", salt, b"old payload", 200_000))
        assert at.unseal(legacy, "pw") == b"old payload"

    def test_previous_release_file_still_imports(self):
        # OSMACCT3: the stdlib scheme v2.0.13 and earlier wrote.
        salt = os.urandom(16)
        v3 = (b"OSMACCT3\n" + struct.pack(">I", 1000) + salt
              + at._encrypt("pw", salt, b"v3 payload", 1000))
        assert at.unseal(v3, "pw") == b"v3 payload"

    def test_tampered_header_or_body_is_rejected(self):
        blob = bytearray(at.seal("pw", b"secret"))
        for i in (12, len(blob) - 1):      # round count byte, last tag byte
            bad = bytearray(blob)
            bad[i] ^= 1
            with pytest.raises(ValueError):
                at.unseal(bytes(bad), "pw")

    def test_new_file_truncated_is_rejected(self):
        with pytest.raises(ValueError):
            at.unseal(b"OSMACCT4\n" + b"\x00" * 40, "pw")

    def test_blocked_library_gives_clear_message(self, monkeypatch):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name.startswith("cryptography"):
                raise ImportError("DLL load failed")
            return real(name, *a, **k)
        monkeypatch.setattr(builtins, "__import__", fake)
        with pytest.raises(RuntimeError, match="encryption component"):
            at.seal("pw", b"x")

    def test_foreign_file_is_rejected(self):
        with pytest.raises(ValueError):
            at.unseal(b"PK\x03\x04 not ours", "pw")

    def test_absurd_round_count_is_rejected_before_deriving(self, monkeypatch):
        salt = os.urandom(16)
        body = at._encrypt("pw", salt, b"x", 1)
        blob = b"OSMACCT3\n" + struct.pack(">I", 2**32 - 1) + salt + body
        monkeypatch.setattr(at.hashlib, "pbkdf2_hmac",
                            lambda *a, **k: pytest.fail("derived anyway"))
        with pytest.raises(ValueError):
            at.unseal(blob, "pw")

    def test_truncated_file_is_rejected(self):
        with pytest.raises(ValueError):
            at.unseal(b"OSMACCT3\n" + b"\x00" * 10, "pw")
