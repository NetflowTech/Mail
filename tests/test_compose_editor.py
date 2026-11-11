"""Compose editor features: tables, table paste, format painter, Tab in cells.

Needs a QApplication (pytest-qt's qtbot) but no database or network.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


def _editor(qtbot):
    from emailclient.ui.compose import _RichBody
    ed = _RichBody()
    qtbot.addWidget(ed)
    return ed


class TestInsertTable:
    def test_inserts_bordered_table_with_caret_in_first_cell(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ed.textCursor().insertText("Before")
        table = table_tools.insert_table(ed, 3, 2)
        assert table.rows() == 3 and table.columns() == 2
        cell = table.cellAt(ed.textCursor())
        assert (cell.row(), cell.column()) == (0, 0)
        html = ed.toHtml()
        assert 'border-collapse:collapse' in html
        assert html.count("<td") == 6
        assert "border-top-style:solid" in html
        assert "border-top:1px" in html

    def test_can_type_after_table_at_end(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        table_tools.insert_table(ed, 1, 1)
        cur = ed.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        assert cur.currentTable() is None   # a paragraph exists after it

    def test_row_and_column_actions(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        table = table_tools.insert_table(ed, 2, 2)
        table_tools.insert_row(ed, table, above=False)
        assert table.rows() == 3
        table_tools.insert_column(ed, table, left=True)
        assert table.columns() == 3
        # New cells carry the same borders as their neighbours.
        fmt = table.cellAt(2, 0).format().toTableCellFormat()
        assert fmt.leftBorder() == 1.0
        table_tools.delete_row(ed, table)
        assert table.rows() == 2
        table_tools.delete_column(ed, table)
        assert table.columns() == 2

    def test_delete_table_removes_it(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ed.textCursor().insertText("keep me")
        table = table_tools.insert_table(ed, 2, 2)
        table_tools.delete_table(ed, table)
        assert "<table" not in ed.toHtml()
        assert "keep me" in ed.toPlainText()

    def test_context_menu_has_table_actions_only_inside_table(self, qtbot):
        from PySide6.QtWidgets import QMenu
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        menu = QMenu()
        assert table_tools.add_table_actions(menu, ed) is False
        table_tools.insert_table(ed, 1, 1)
        menu = QMenu()
        assert table_tools.add_table_actions(menu, ed) is True
        titles = [a.text() for a in menu.actions() if a.menu()]
        assert "Table" in titles


class TestTabInTable:
    def _tab(self, ed, shift=False):
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        key = Qt.Key.Key_Backtab if shift else Qt.Key.Key_Tab
        mods = (Qt.KeyboardModifier.ShiftModifier if shift
                else Qt.KeyboardModifier.NoModifier)
        ed.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods))

    def test_tab_moves_to_next_cell_and_adds_row_at_end(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        table = table_tools.insert_table(ed, 1, 2)
        self._tab(ed)
        cell = table.cellAt(ed.textCursor())
        assert (cell.row(), cell.column()) == (0, 1)
        self._tab(ed)
        assert table.rows() == 2
        cell = table.cellAt(ed.textCursor())
        assert (cell.row(), cell.column()) == (1, 0)
        self._tab(ed, shift=True)
        cell = table.cellAt(ed.textCursor())
        assert (cell.row(), cell.column()) == (0, 1)

    def test_tab_outside_table_is_not_swallowed(self, qtbot):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab,
                       Qt.KeyboardModifier.NoModifier)
        assert table_tools.handle_tab(ed, ev) is False


_EXCEL_HTML = """<html><head><style>
.xl65 { border: .5pt solid windowtext; }
</style></head><body>
<table border=0 cellpadding=0 cellspacing=0 width=160 style='border-collapse:collapse;width:120pt'>
<col width=80 span=2>
<tr height=21><td height=21 class=xl65 width=80 style='height:16pt;width:60pt;background:#FFFF00'>Q1</td><td class=xl65 width=80>100</td></tr>
<tr><td class=xl65>Q2</td><td class=xl65>200</td></tr>
</table></body></html>"""

_WORD_HTML = """<table class=MsoTableGrid border=1 cellspacing=0 cellpadding=0 style='border-collapse:collapse;border:none'>
<tr><td width=200 valign=top style='width:150pt;border:solid windowtext 1.0pt;padding:0in 5.4pt'><p class=MsoNormal><b>Name</b></p></td>
<td width=200 valign=top style='width:150pt;border:solid windowtext 1.0pt;border-left:none;padding:0in 5.4pt'><p class=MsoNormal>Val</p></td></tr>
<tr><td style='border:solid windowtext 1.0pt;border-top:none'><p class=MsoNormal>x</p></td><td style='border:solid windowtext 1.0pt;border-top:none'><p class=MsoNormal>y</p></td></tr></table>"""


class TestPasteTable:
    def _mime(self, html: str, with_image: bool):
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QImage
        md = QMimeData()
        md.setHtml(html)
        md.setText("Q1\t100\nQ2\t200")
        if with_image:
            img = QImage(10, 10, QImage.Format.Format_ARGB32)
            img.fill(0xFFFFFFFF)
            md.setImageData(img)
        return md

    def test_excel_clipboard_pastes_as_table_not_picture(self, qtbot):
        ed = _editor(qtbot)
        ed.insertFromMimeData(self._mime(_EXCEL_HTML, with_image=True))
        html = ed.toHtml()
        assert "<table" in html
        assert "<img" not in html
        assert "Q1" in ed.toPlainText() and "200" in ed.toPlainText()

    def test_pasted_cells_get_visible_borders(self, qtbot):
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ed.insertFromMimeData(self._mime(_WORD_HTML, with_image=False))
        doc = ed.document()
        tables = table_tools.tables_in_range(doc, 0, doc.characterCount())
        assert len(tables) == 1
        fmt = tables[0].cellAt(0, 0).format().toTableCellFormat()
        assert fmt.topBorder() == 1.0 and fmt.leftBorder() == 1.0
        # Word's 12px paragraph margins were stripped inside the cells.
        block = tables[0].cellAt(0, 0).firstCursorPosition().block()
        assert block.blockFormat().topMargin() == 0

    def test_pasted_table_round_trips_to_email(self, qtbot):
        from emailclient.outgoing_html import email_ready_html
        ed = _editor(qtbot)
        ed.insertFromMimeData(self._mime(_WORD_HTML, with_image=False))
        out = email_ready_html(ed.toHtml())
        assert "<table" in out
        assert "border-top-style:solid" in out
        assert "<b>" in out or "font-weight:700" in out

    def test_plain_image_still_pastes_as_image(self, qtbot):
        ed = _editor(qtbot)
        ed.insertFromMimeData(self._mime("<img src='x.png'>", with_image=True))
        assert "<img" in ed.toHtml()


class TestFormatPainter:
    def test_copies_look_and_applies_to_selection(self, qtbot):
        from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
        from emailclient.ui import format_painter
        ed = _editor(qtbot)
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        bold.setFontPointSize(18)
        bold.setForeground(QColor("#ff0000"))
        bold.setFontFamilies(["Georgia"])
        cur = ed.textCursor()
        cur.insertText("Fancy", bold)
        plain = QTextCharFormat()
        plain.setFontItalic(True)
        cur.insertText(" plain", plain)
        # Caret inside "Fancy".
        c = ed.textCursor()
        c.setPosition(2)
        ed.setTextCursor(c)
        char_fmt, block_fmt = format_painter.capture(ed)
        assert char_fmt.fontWeight() == QFont.Weight.Bold
        assert char_fmt.fontPointSize() == 18
        # Select " plain" and brush it.
        c.setPosition(5)
        c.setPosition(11, QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(c)
        assert format_painter.apply(ed, char_fmt, block_fmt)
        probe = QTextCursor(ed.document())
        probe.setPosition(8)
        got = probe.charFormat()
        assert got.fontWeight() == QFont.Weight.Bold
        assert got.fontPointSize() == 18
        assert got.foreground().color().name() == "#ff0000"
        assert got.fontFamilies() == ["Georgia"]
        assert not got.fontItalic()      # replaced, not merged

    def test_does_not_copy_links(self, qtbot):
        from PySide6.QtGui import QTextCharFormat
        from emailclient.ui import format_painter
        ed = _editor(qtbot)
        link = QTextCharFormat()
        link.setAnchor(True)
        link.setAnchorHref("https://example.com")
        link.setFontUnderline(True)
        ed.textCursor().insertText("link", link)
        c = ed.textCursor()
        c.setPosition(2)
        ed.setTextCursor(c)
        char_fmt, _ = format_painter.capture(ed)
        assert not char_fmt.isAnchor()
        assert char_fmt.anchorHref() == ""
        assert char_fmt.fontUnderline()

    def test_nothing_selected_is_a_noop(self, qtbot):
        from PySide6.QtGui import QTextCharFormat
        from emailclient.ui import format_painter
        ed = _editor(qtbot)
        ed.textCursor().insertText("abc")
        assert format_painter.apply(ed, QTextCharFormat(), None) is False

    def test_image_in_selection_survives(self, qtbot):
        from PySide6.QtGui import QImage, QTextCharFormat, QTextCursor
        from emailclient.ui import format_painter
        ed = _editor(qtbot)
        ed.textCursor().insertText("a")
        img = QImage(4, 4, QImage.Format.Format_ARGB32)
        img.fill(0xFF00FF00)
        ed.insert_qimage(img)
        ed.textCursor().insertText("b")
        c = QTextCursor(ed.document())
        c.select(QTextCursor.SelectionType.Document)
        ed.setTextCursor(c)
        fmt = QTextCharFormat()
        fmt.setFontPointSize(20)
        assert format_painter.apply(ed, fmt, None)
        assert "<img" in ed.toHtml()


class TestLinkify:
    def test_www_without_scheme_becomes_link(self):
        from emailclient.ui.textrender import linkify
        out = linkify("see www.example.com/page.")
        assert '<a href="http://www.example.com/page">www.example.com/page</a>.' in out

    def test_https_kept(self):
        from emailclient.ui.textrender import linkify
        assert ('<a href="https://x.io/a">https://x.io/a</a>'
                in linkify("go https://x.io/a"))


class TestExportFileImagesOnlyOpensAppFiles:
    """On send, file:// images are converted to cid parts, but only when the
    file is one the app wrote. A hostile quoted original must not get a
    network path opened (login-hash leak) or a private local image attached
    and mailed back."""

    @pytest.fixture
    def data_dir(self, tmp_path, monkeypatch):
        from emailclient import safe_paths
        d = tmp_path / "amail"
        d.mkdir()
        monkeypatch.setattr(safe_paths.config, "data_dir", lambda: d)
        return d

    @staticmethod
    def _png(path):
        from PySide6.QtGui import QImage
        img = QImage(4, 4, QImage.Format.Format_ARGB32)
        img.fill(0xFF0000FF)
        path.parent.mkdir(parents=True, exist_ok=True)
        assert img.save(str(path), "PNG")
        return path

    def test_signature_image_is_exported(self, qapp, data_dir):
        from PySide6.QtCore import QUrl
        from emailclient.ui.compose import export_file_images
        sig = self._png(data_dir / "sig_images" / "s.png")
        url = QUrl.fromLocalFile(str(sig)).toString()
        html, inline = export_file_images(f'<img src="{url}">', data_dir / "inline")
        assert len(inline) == 1 and 'src="cid:' in html

    def test_private_local_image_is_not_attached(self, qapp, data_dir, tmp_path):
        from PySide6.QtCore import QUrl
        from emailclient.ui.compose import export_file_images
        private = self._png(tmp_path / "Pictures" / "private.png")
        url = QUrl.fromLocalFile(str(private)).toString()
        src = f'<p>quoted</p><img src="{url}">'
        html, inline = export_file_images(src, data_dir / "inline")
        assert inline == [] and html == src

    def test_network_image_is_never_opened(self, qapp, data_dir, monkeypatch):
        from emailclient import safe_paths
        from emailclient.ui.compose import export_file_images

        def boom(_p):
            raise AssertionError("network path was opened")
        monkeypatch.setattr(safe_paths.os.path, "realpath", boom)
        src = '<img src="file://attacker-host/share/x.png">'
        html, inline = export_file_images(src, data_dir / "inline")
        assert inline == [] and html == src
