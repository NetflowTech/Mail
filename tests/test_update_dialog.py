"""The update prompt shows server text as plain text only."""
from __future__ import annotations

from PySide6.QtCore import Qt

from emailclient.ui.update_dialog import UpdateDialog


def test_notes_render_as_plain_text_with_no_links(qtbot):
    dlg = UpdateDialog({"version": "2.0.99",
                        "notes": '<a href="https://evil.example">Click</a>'})
    qtbot.addWidget(dlg)
    lab = dlg.notes_label
    assert lab.textFormat() == Qt.TextFormat.PlainText
    assert lab.openExternalLinks() is False
    assert lab.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction
    assert lab.text() == '<a href="https://evil.example">Click</a>'


def test_default_line_when_no_notes(qtbot):
    dlg = UpdateDialog({"version": "2.0.99"})
    qtbot.addWidget(dlg)
    assert dlg.notes_label.text() == "Bug fixes and improvements."
