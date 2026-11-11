"""Paths an email hands the app: only the app's own files are ever opened."""
from __future__ import annotations

import os

import pytest

from emailclient import safe_paths


class TestLocalPathFromSrc:
    def test_data_and_http_are_not_files(self):
        assert safe_paths.local_path_from_src("data:image/png;base64,AAAA") is None
        assert safe_paths.local_path_from_src("http://x/y.png") is None
        assert safe_paths.local_path_from_src("https://x/y.png") is None
        assert safe_paths.local_path_from_src("cid:abc") is None
        assert safe_paths.local_path_from_src("") is None

    def test_file_url_with_host_keeps_unc_prefix(self):
        # The host must survive so is_network_path can refuse it.
        p = safe_paths.local_path_from_src("file://attacker-host/share/x.png")
        assert p is not None and p.startswith("//attacker-host/")
        assert safe_paths.is_network_path(p)

    def test_unc_path_is_network(self):
        assert safe_paths.is_network_path("\\\\host\\share\\x.png")
        assert safe_paths.is_network_path("//host/share/x.png")
        assert not safe_paths.is_network_path("/Users/me/x.png")
        assert not safe_paths.is_network_path("C:\\x.png")

    def test_bare_and_drive_paths_pass_through(self):
        assert safe_paths.local_path_from_src("/tmp/a.png") == "/tmp/a.png"
        assert safe_paths.local_path_from_src("C:\\a.png") == "C:\\a.png"
        assert safe_paths.local_path_from_src("c:/a.png") == "c:/a.png"


class TestSafeAppFile:
    @pytest.fixture(autouse=True)
    def _data_dir(self, tmp_path, monkeypatch):
        self.data = tmp_path / "amail"
        self.data.mkdir()
        monkeypatch.setattr(safe_paths.config, "data_dir", lambda: self.data)

    def _png(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
        return path

    def test_network_paths_never_touch_the_filesystem(self, monkeypatch):
        def boom(_p):
            raise AssertionError("filesystem touched for a network path")
        monkeypatch.setattr(safe_paths.os.path, "realpath", boom)
        monkeypatch.setattr(safe_paths.os.path, "isfile", boom)
        assert safe_paths.safe_app_file("file://attacker/share/x.png") is None
        assert safe_paths.safe_app_file("\\\\attacker\\share\\x.png") is None
        assert safe_paths.safe_app_file("//attacker/share/x.png") is None

    def test_app_attachment_is_allowed(self):
        f = self._png(self.data / "attachments" / "abc" / "pic.png")
        assert safe_paths.safe_app_file(str(f)) == os.path.realpath(str(f))
        from PySide6.QtCore import QUrl
        assert safe_paths.safe_app_file(QUrl.fromLocalFile(str(f)).toString())

    @pytest.mark.parametrize("sub", ["copied", "inline", "sig_images"])
    def test_other_app_folders_are_allowed(self, sub):
        f = self._png(self.data / sub / "x.png")
        assert safe_paths.safe_app_file(str(f))

    def test_outside_the_data_dir_is_refused(self, tmp_path):
        f = self._png(tmp_path / "elsewhere" / "secret.png")
        assert safe_paths.safe_app_file(str(f)) is None
        assert safe_paths.safe_app_file("file://" + str(f)) is None

    def test_lookalike_folder_is_refused(self):
        f = self._png(self.data / "attachments_evil" / "x.png")
        assert safe_paths.safe_app_file(str(f)) is None

    def test_traversal_out_of_the_folder_is_refused(self, tmp_path):
        outside = self._png(tmp_path / "secret.png")
        (self.data / "attachments").mkdir()
        sneaky = self.data / "attachments" / ".." / ".." / "secret.png"
        assert os.path.isfile(sneaky)
        assert safe_paths.safe_app_file(str(sneaky)) is None
        assert outside.exists()

    def test_missing_file_is_refused(self):
        assert safe_paths.safe_app_file(str(self.data / "attachments" / "no.png")) is None
