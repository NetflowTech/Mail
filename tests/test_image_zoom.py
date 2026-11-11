"""Click-to-zoom only ever loads data: images or the app's own files."""
from __future__ import annotations

import base64

import pytest
from PySide6.QtGui import QImage

from emailclient import safe_paths
from emailclient.ui.image_zoom import pixmap_from_src


def _png_bytes(w=6, h=4) -> bytes:
    from PySide6.QtCore import QBuffer, QIODevice
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(0xFF00FF00)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "amail"
    d.mkdir()
    monkeypatch.setattr(safe_paths.config, "data_dir", lambda: d)
    return d


class TestPixmapFromSrc:
    def test_data_uri_loads(self, qapp):
        src = "data:image/png;base64," + base64.b64encode(_png_bytes()).decode()
        assert pixmap_from_src(src).width() == 6

    def test_app_attachment_loads(self, qapp, data_dir):
        f = data_dir / "attachments" / "x" / "pic.png"
        f.parent.mkdir(parents=True)
        f.write_bytes(_png_bytes())
        assert pixmap_from_src(str(f)).width() == 6
        from PySide6.QtCore import QUrl
        assert pixmap_from_src(QUrl.fromLocalFile(str(f)).toString()).width() == 6

    @pytest.mark.parametrize("src", [
        "file://attacker-host/share/x.png",
        "\\\\attacker-host\\share\\x.png",
        "//attacker-host/share/x.png",
    ])
    def test_network_sources_are_never_touched(self, qapp, monkeypatch, src):
        def boom(_p):
            raise AssertionError("network path was opened")
        monkeypatch.setattr(safe_paths.os.path, "realpath", boom)
        monkeypatch.setattr(safe_paths.os.path, "isfile", boom)
        assert pixmap_from_src(src).isNull()

    def test_local_file_outside_the_app_is_refused(self, qapp, tmp_path, data_dir):
        f = tmp_path / "private.png"
        f.write_bytes(_png_bytes())
        assert pixmap_from_src(str(f)).isNull()
        assert pixmap_from_src("file://" + str(f)).isNull()

    def test_remote_images_are_not_fetched(self, qapp):
        assert pixmap_from_src("http://example.com/x.png").isNull()
        assert pixmap_from_src("https://example.com/x.png").isNull()
