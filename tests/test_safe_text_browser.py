"""The previews that show quoted mail must never open a file the mail names."""
from __future__ import annotations

import base64

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QImage, QPixmap, QTextDocument
from PySide6.QtWidgets import QTextBrowser

from emailclient import safe_paths
from emailclient.ui.safe_text_browser import SafeTextBrowser

IMG = QTextDocument.ResourceType.ImageResource


def _png_file(path, w=8, h=5):
    img = QImage(w, h, QImage.Format.Format_ARGB32)
    img.fill(0xFFFF0000)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert img.save(str(path), "PNG")
    return path


def _as_image(res) -> QImage:
    if isinstance(res, QImage):
        return res
    if isinstance(res, QPixmap):
        return res.toImage()
    img = QImage()
    if res is not None:
        img.loadFromData(bytes(res))
    return img


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "amail"
    d.mkdir()
    monkeypatch.setattr(safe_paths.config, "data_dir", lambda: d)
    return d


class TestSafeTextBrowser:
    def test_plain_browser_would_open_the_file(self, qtbot, tmp_path):
        # Documents the hazard the safe viewer closes: a stock QTextBrowser
        # reads any local file an <img> points at.
        f = _png_file(tmp_path / "outside.png")
        view = QTextBrowser()
        qtbot.addWidget(view)
        view.setHtml(f'<img src="{QUrl.fromLocalFile(str(f)).toString()}">')
        got = _as_image(view.document().resource(IMG, QUrl.fromLocalFile(str(f))))
        assert got.width() == 8

    def test_file_outside_the_app_is_not_read(self, qtbot, tmp_path, data_dir):
        f = _png_file(tmp_path / "outside.png")
        view = SafeTextBrowser()
        qtbot.addWidget(view)
        url = QUrl.fromLocalFile(str(f))
        view.setHtml(f'<p>hi</p><img src="{url.toString()}">')
        got = _as_image(view.document().resource(IMG, url))
        assert got.width() == 1 and got.height() == 1   # blank stand-in

    def test_network_image_never_touches_the_filesystem(self, qtbot, monkeypatch):
        def boom(_p):
            raise AssertionError("network path was opened")
        monkeypatch.setattr(safe_paths.os.path, "realpath", boom)
        monkeypatch.setattr(safe_paths.os.path, "isfile", boom)
        view = SafeTextBrowser()
        qtbot.addWidget(view)
        view.setHtml('<img src="file://attacker-host/share/x.png">'
                     '<img src="\\\\attacker-host\\share\\y.png">'
                     '<table background="file://attacker-host/share/z.png">'
                     '<tr><td>x</td></tr></table>')
        got = _as_image(view.document().resource(
            IMG, QUrl("file://attacker-host/share/x.png")))
        assert got.width() == 1

    def test_app_file_and_data_uri_still_render(self, qtbot, data_dir):
        f = _png_file(data_dir / "attachments" / "a" / "pic.png", 8, 5)
        view = SafeTextBrowser()
        qtbot.addWidget(view)
        url = QUrl.fromLocalFile(str(f))
        b64 = base64.b64encode(f.read_bytes()).decode()
        data_url = QUrl(f"data:image/png;base64,{b64}")
        view.setHtml(f'<img src="{url.toString()}"><img src="{data_url.toString()}">')
        assert _as_image(view.document().resource(IMG, url)).width() == 8
        assert _as_image(view.document().resource(IMG, data_url)).width() == 8

    def test_links_do_not_open(self, qtbot):
        view = SafeTextBrowser()
        qtbot.addWidget(view)
        assert view.openExternalLinks() is False
        assert view.openLinks() is False
