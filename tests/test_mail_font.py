"""One default font for new mail and for the signature, chosen in Settings."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QSettings

from emailclient.settings import ComposeSettings


@pytest.fixture
def cs(tmp_path):
    return ComposeSettings(QSettings(str(tmp_path / "ui.ini"),
                                     QSettings.Format.IniFormat))


class TestSetting:
    def test_defaults(self, cs):
        assert cs.font_family == "Aptos"     # Outlook's own default
        assert cs.font_size == 11

    def test_round_trip(self, cs):
        cs.font_family = "Georgia"
        cs.font_size = 11
        assert (cs.font_family, cs.font_size) == ("Georgia", 11)

    def test_nonsense_falls_back(self, cs):
        cs.font_family = "   "
        cs.font_size = 0
        assert cs.font_family == "Aptos" and cs.font_size == 11
        cs._qs.setValue("compose_font_size", "huge")
        assert cs.font_size == 11
        cs._qs.setValue("compose_font_size", 400)
        assert cs.font_size == 11


class TestEditorsShareIt:
    def test_compose_body_and_signature_editor_use_the_mail_font(self, qtbot):
        from emailclient.ui import theme
        from emailclient.ui.account_settings import SignatureBody
        from emailclient.ui.compose import _RichBody
        theme.apply_theme("AMail Light")
        want = ComposeSettings()
        body = _RichBody()
        qtbot.addWidget(body)
        body.use_mail_font()
        sig = SignatureBody()
        qtbot.addWidget(sig)
        for w in (body, sig):
            w.show()
            qtbot.waitExposed(w)
            f = w.document().defaultFont()
            assert f.family() == want.font_family
            assert f.pointSize() == want.font_size

    def test_the_sent_mail_carries_it(self, qtbot):
        from emailclient.outgoing_html import email_ready_html
        from emailclient.ui.compose import _RichBody
        want = ComposeSettings()
        body = _RichBody()
        qtbot.addWidget(body)
        body.use_mail_font()
        body.textCursor().insertText("Hello")
        out = email_ready_html(body.toHtml())
        assert f"'{want.font_family}'" in out
        assert f"font-size:{want.font_size}pt;" in out
        assert "<body><div style=" in out
