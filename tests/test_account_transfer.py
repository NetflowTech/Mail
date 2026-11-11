"""Account backup file: key stretching and format compatibility."""
from __future__ import annotations

import os
import struct

import pytest

from emailclient import account_transfer as at


class TestSeal:
    def test_roundtrip(self):
        blob = at.seal("hunter2", b'{"version": 1}')
        assert blob.startswith(b"OSMACCT3\n")
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
