"""Tests for emailclient.updater — version comparison and manifest logic."""
from __future__ import annotations

import pytest

from emailclient.updater import is_newer, _parse, platform_asset, check_only


class TestParse:
    def test_simple(self):
        assert _parse("1.2.3") == (1, 2, 3)

    def test_single(self):
        assert _parse("5") == (5,)

    def test_four_parts(self):
        assert _parse("1.2.3.4") == (1, 2, 3, 4)

    def test_empty_string_returns_none(self):
        assert _parse("") is None

    def test_letters_return_none(self):
        assert _parse("abc") is None

    def test_mixed_returns_none(self):
        assert _parse("1.x.3") is None

    def test_none_returns_none(self):
        assert _parse(None) is None

    def test_leading_whitespace(self):
        assert _parse("  2.0.0") == (2, 0, 0)


class TestIsNewer:
    def test_remote_newer_patch(self):
        assert is_newer("1.0.1", "1.0.0") is True

    def test_remote_newer_minor(self):
        assert is_newer("1.1.0", "1.0.9") is True

    def test_remote_newer_major(self):
        assert is_newer("2.0.0", "1.9.9") is True

    def test_equal_not_newer(self):
        assert is_newer("1.0.0", "1.0.0") is False

    def test_local_newer_returns_false(self):
        assert is_newer("1.0.0", "2.0.0") is False

    def test_garbage_remote_returns_false(self):
        assert is_newer("garbage", "1.0.0") is False

    def test_garbage_local_returns_false(self):
        assert is_newer("2.0.0", "garbage") is False

    def test_empty_remote_returns_false(self):
        assert is_newer("", "1.0.0") is False

    def test_empty_both_returns_false(self):
        assert is_newer("", "") is False

    def test_different_length_tuples(self):
        # (2,) > (1, 9, 9) in Python tuple comparison
        assert is_newer("2", "1.9.9") is True

    def test_build_version_2_0_0(self):
        assert is_newer("2.0.1", "2.0.0") is True
        assert is_newer("2.0.0", "2.0.1") is False


class TestPlatformAsset:
    def test_mac_asset_present(self):
        manifest = {"version": "2.0.1",
                    "mac_url": "https://example.com/AMail.zip",
                    "mac_sha256": "abc123"}
        result = platform_asset(manifest, "mac")
        assert result == ("https://example.com/AMail.zip", "abc123")

    def test_win_asset_present(self):
        manifest = {"version": "2.0.1",
                    "win_url": "https://example.com/AMail-setup.exe",
                    "win_sha256": "def456"}
        result = platform_asset(manifest, "win")
        assert result == ("https://example.com/AMail-setup.exe", "def456")

    def test_missing_url_returns_none(self):
        manifest = {"version": "2.0.1"}
        assert platform_asset(manifest, "mac") is None

    def test_empty_url_returns_none(self):
        manifest = {"version": "2.0.1", "mac_url": "   ", "mac_sha256": "x"}
        assert platform_asset(manifest, "mac") is None

    def test_sha_may_be_empty_string(self):
        manifest = {"mac_url": "https://example.com/a.zip", "mac_sha256": ""}
        result = platform_asset(manifest, "mac")
        assert result == ("https://example.com/a.zip", "")


class TestCheckOnly:
    def test_returns_none_when_fetch_returns_none(self, monkeypatch):
        import emailclient.updater as u
        monkeypatch.setattr(u, "fetch_manifest", lambda url: None)
        assert check_only("1.0.0") is None

    def test_returns_none_when_not_newer(self, monkeypatch):
        import emailclient.updater as u
        manifest = {"version": "1.0.0", "mac_url": "http://x/a.zip",
                    "mac_sha256": "x"}
        monkeypatch.setattr(u, "fetch_manifest", lambda url: manifest)
        assert check_only("1.0.0") is None

    def test_returns_manifest_when_newer(self, monkeypatch):
        import emailclient.updater as u
        import sys
        manifest = {"version": "2.0.0",
                    f"{u._current_platform()}_url": "http://x/a.zip",
                    f"{u._current_platform()}_sha256": "x",
                    f"{u._current_platform()}_sig_url": "https://x/a.zip.minisig"}
        monkeypatch.setattr(u, "fetch_manifest", lambda url: manifest)
        result = check_only("1.0.0")
        assert result is not None
        assert result["version"] == "2.0.0"

    def test_returns_none_when_signature_missing(self, monkeypatch):
        import emailclient.updater as u
        manifest = {"version": "2.0.0",
                    f"{u._current_platform()}_url": "https://x/a.zip",
                    f"{u._current_platform()}_sha256": "a" * 64}
        monkeypatch.setattr(u, "fetch_manifest", lambda url: manifest)
        assert check_only("1.0.0") is None

    def test_download_refuses_without_signature(self, tmp_path, monkeypatch):
        import emailclient.updater as u
        def _no_network(*a, **k):
            raise AssertionError("must not download without a signature URL")
        monkeypatch.setattr(u.urllib.request, "urlopen", _no_network)
        dest = tmp_path / "a.exe"
        assert u.download_and_verify("https://x/a.exe", "a" * 64, dest) is False
        assert u.download_and_verify("https://x/a.exe", "a" * 64, dest,
                                     sig_url="http://x/a.exe.minisig") is False
        assert not dest.exists()

    def test_returns_none_when_no_asset_for_platform(self, monkeypatch):
        import emailclient.updater as u
        # Provide an asset only for the other platform
        other = "win" if u._current_platform() == "mac" else "mac"
        manifest = {"version": "9.9.9",
                    f"{other}_url": "http://x/a.zip",
                    f"{other}_sha256": "x"}
        monkeypatch.setattr(u, "fetch_manifest", lambda url: manifest)
        assert check_only("1.0.0") is None


class TestCurrentPlatform:
    def test_returns_known_value(self):
        from emailclient.updater import _current_platform
        plat = _current_platform()
        assert plat in ("win", "mac", "other")

    def test_fetch_manifest_is_disabled(self):
        from emailclient.updater import fetch_manifest
        # Customer edition stubs this out — must not hit the network.
        result = fetch_manifest()
        assert result is None

    def test_download_and_verify_is_disabled(self, tmp_path):
        from emailclient.updater import download_and_verify
        result = download_and_verify("http://example.com/x.zip", "sha", tmp_path)
        assert result is False


class TestEveryUpdatePathIsSigned:
    """Every caller of download_and_verify must pass sig_url. The startup
    prompt once called it without one, so with signatures mandatory it failed
    instantly on every machine while the About-dialog path worked."""

    def test_all_callers_pass_sig_url(self):
        import ast
        from pathlib import Path
        import emailclient
        root = Path(emailclient.__file__).parent
        callers = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name == "download_and_verify":
                    callers.append(path.name)
                    kws = {k.arg for k in node.keywords}
                    assert "sig_url" in kws, f"{path} calls download_and_verify without sig_url"
        assert callers, "expected at least one caller"


class TestApplyUpdateWindowsFlags:
    def test_installer_runs_visibly_and_without_suppressed_errors(self, tmp_path, monkeypatch):
        """Regression: /VERYSILENT + /SUPPRESSMSGBOXES hid the install and
        auto-aborted on file-in-use errors, leaving half-copied installs."""
        from emailclient import updater
        calls = []
        monkeypatch.setattr(updater.subprocess, "Popen",
                            lambda args, **kw: calls.append(args))
        monkeypatch.setattr(updater, "_current_platform", lambda: "win")
        installer = tmp_path / "AMail-Setup-9.9.9.exe"
        installer.write_bytes(b"x")
        assert updater.apply_update(installer) is True
        (args,) = calls
        assert args[0] == str(installer)
        assert "/SILENT" in args
        assert "/VERYSILENT" not in args
        assert "/SUPPRESSMSGBOXES" not in args
        assert "/CLOSEAPPLICATIONS" in args
        assert any(a.startswith("/LOG=") for a in args)


class TestReleaseNotes:
    """Notes come from version.json on the server: display-only, plain text,
    cleaned and capped."""

    def test_plain_notes_pass_through(self):
        from emailclient.updater import release_notes
        m = {"notes": "Read status is per computer.\nTables in compose."}
        assert release_notes(m) == "Read status is per computer.\nTables in compose."

    def test_missing_or_non_string_is_empty(self):
        from emailclient.updater import release_notes
        assert release_notes({}) == ""
        assert release_notes({"notes": None}) == ""
        assert release_notes({"notes": ["a", "b"]}) == ""
        assert release_notes({"notes": 42}) == ""
        assert release_notes("not a dict") == ""

    def test_markup_is_kept_as_literal_text(self):
        # Not stripped: the labels render PlainText, so this shows as typed
        # instead of becoming a link.
        from emailclient.updater import release_notes
        m = {"notes": '<a href="https://evil.example">Click to update</a>'}
        assert release_notes(m) == '<a href="https://evil.example">Click to update</a>'

    def test_control_and_invisible_characters_are_dropped(self):
        from emailclient.updater import release_notes
        # bell, NUL, RTL override, zero-width space, soft hyphen
        m = {"notes": "Up\x07da\x00te ‮now​­ please"}
        assert release_notes(m) == "Update now please"

    def test_crlf_tabs_and_blank_runs_are_normalised(self):
        from emailclient.updater import release_notes
        m = {"notes": "\r\n\r\nOne\ttwo\r\n\r\n\r\n\r\nThree   four\r\n\r\n"}
        assert release_notes(m) == "One two\n\nThree four"

    def test_capped_lines_and_chars(self):
        from emailclient import updater
        many = "\n".join(f"line {i}" for i in range(50))
        assert updater.release_notes({"notes": many}).count("\n") == updater.NOTES_MAX_LINES - 1
        long = "x" * 5000
        out = updater.release_notes({"notes": long})
        assert len(out) == updater.NOTES_MAX_CHARS + 1 and out.endswith("…")


class TestFetchManifestShape:
    def _serve(self, monkeypatch, payload: bytes):
        import io
        from emailclient import updater

        class Resp(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(updater.urllib.request, "urlopen",
                            lambda *a, **k: Resp(payload))
        return updater

    def test_non_object_json_is_rejected(self, monkeypatch):
        u = self._serve(monkeypatch, b'["2.0.99"]')
        assert u.fetch_manifest("https://x/version.json") is None

    def test_oversized_manifest_is_rejected(self, monkeypatch):
        u = self._serve(monkeypatch, b'{"notes": "' + b"a" * 70_000 + b'"}')
        assert u.fetch_manifest("https://x/version.json") is None

    def test_normal_manifest_parses(self, monkeypatch):
        u = self._serve(monkeypatch, b'{"version": "2.0.99", "notes": "hi"}')
        assert u.fetch_manifest("https://x/version.json") == {"version": "2.0.99", "notes": "hi"}
