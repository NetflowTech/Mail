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


class TestPasteFromExcel:
    """The real shape of Excel's clipboard HTML (see tests/test_pasted_html)."""

    def _paste(self, qtbot, html, text="Item\tQty\nWidget\t4"):
        from PySide6.QtCore import QMimeData
        from PySide6.QtGui import QImage
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ed.resize(700, 400)
        ed.show()
        qtbot.waitExposed(ed)
        md = QMimeData()
        if html:
            md.setHtml(html)
        md.setText(text)
        img = QImage(10, 10, QImage.Format.Format_ARGB32)
        img.fill(0xFFFFFFFF)
        md.setImageData(img)
        ed.insertFromMimeData(md)
        doc = ed.document()
        tables = table_tools.tables_in_range(doc, 0, doc.characterCount())
        assert len(tables) == 1
        return ed, tables[0]

    def test_header_bold_and_grid_borders(self, qtbot):
        from tests.test_pasted_html import EXCEL
        ed, t = self._paste(qtbot, EXCEL)
        assert "<img" not in ed.toHtml()
        assert t.cellAt(0, 0).firstCursorPosition().charFormat().fontWeight() == 700
        assert t.cellAt(1, 0).firstCursorPosition().charFormat().fontWeight() < 700
        fmt = t.cellAt(1, 1).format().toTableCellFormat()
        assert fmt.leftBorder() == 1.0 and fmt.bottomBorder() == 1.0
        assert t.format().cellSpacing() == 0 and t.format().borderCollapse()

    def test_grid_is_only_for_spreadsheets(self, qtbot):
        # A borderless Word / web table was borderless on purpose.
        ed, t = self._paste(qtbot, "<table><tr><td>a</td><td>b</td></tr></table>")
        fmt = t.cellAt(0, 0).format().toTableCellFormat()
        assert fmt.leftBorder() == 0

    def test_tab_separated_text_becomes_a_table(self, qtbot):
        ed, t = self._paste(qtbot, None, text="Item\tQty\nWidget\t4\nGadget\t10")
        assert (t.rows(), t.columns()) == (3, 2)
        assert t.cellAt(2, 1).firstCursorPosition().block().text() == "10"
        assert t.cellAt(0, 0).format().toTableCellFormat().topBorder() == 1.0

    def test_plain_prose_with_a_tab_stays_text(self, qtbot):
        from PySide6.QtCore import QMimeData
        ed = _editor(qtbot)
        md = QMimeData()
        md.setText("a\tb\nc")
        ed.insertFromMimeData(md)
        assert "<table" not in ed.toHtml()
        assert ed.toPlainText() == "a\tb\nc"


class TestPasteFromWeb:
    def test_page_background_is_not_pasted(self, qtbot):
        from PySide6.QtCore import QMimeData
        from PySide6.QtCore import Qt
        ed = _editor(qtbot)
        md = QMimeData()
        md.setHtml('<meta charset="utf-8"><p style="margin:0; background-color: '
                   'rgb(255, 255, 255);"><span style="color: rgb(31, 33, 37); '
                   'background-color: rgb(255, 255, 255);">Dear team,</span></p>')
        ed.insertFromMimeData(md)
        block = ed.document().firstBlock()
        assert block.blockFormat().background().style() == Qt.BrushStyle.NoBrush
        frag = block.begin().fragment().charFormat()
        assert frag.background().style() == Qt.BrushStyle.NoBrush
        assert frag.foreground().color().name() == "#1f2125"   # text colour kept
        assert ed.toPlainText() == "Dear team,"

    def test_yellow_highlight_is_kept(self, qtbot):
        from PySide6.QtCore import QMimeData
        ed = _editor(qtbot)
        md = QMimeData()
        md.setHtml('<span style="background-color:#ffff00">hot</span>')
        ed.insertFromMimeData(md)
        frag = ed.document().firstBlock().begin().fragment().charFormat()
        assert frag.background().color().name() == "#ffff00"


class TestResizeByDragging:
    def _table(self, qtbot, cols=3):
        from PySide6.QtCore import QPoint
        from emailclient.ui import table_tools
        ed = _editor(qtbot)
        ed.resize(700, 400)
        ed.show()
        qtbot.waitExposed(ed)
        ed.textCursor().insertText("above\n")
        t = table_tools.insert_table(ed, 2, cols)
        for c in range(cols):
            t.cellAt(0, c).firstCursorPosition().insertText(f"c{c}")
        xs, top, bottom = ed._dragger._boundaries(t)
        y = int((top + bottom) / 2)
        return ed, t, xs, y, QPoint

    def test_grip_only_on_boundaries(self, qtbot):
        ed, t, xs, y, P = self._table(qtbot)
        assert ed._dragger.hover(P(int(xs[1]), y))
        assert ed._dragger.hover(P(int(xs[-1]) - 2, y))         # right edge
        assert not ed._dragger.hover(P(int((xs[0] + xs[1]) / 2), y))
        assert not ed._dragger.hover(P(int(xs[1]), y + 300))    # below the table
        assert not ed._dragger.press(P(int((xs[0] + xs[1]) / 2), y))

    def test_right_edge_shrinks_the_whole_table(self, qtbot):
        ed, t, xs, y, P = self._table(qtbot)
        width0 = xs[-1] - xs[0]
        assert ed._dragger.press(P(int(xs[-1]), y))
        assert ed._dragger.move(P(int(xs[-1]) - 200, y))
        assert ed._dragger.release()
        xs2, _, _ = ed._dragger._boundaries(t)
        assert xs2[-1] - xs2[0] < width0 - 150
        cols = t.format().columnWidthConstraints()
        assert len(cols) == 3 and all(c.type() == c.Type.FixedLength for c in cols)
        # Proportions kept: each column keeps its share of the width.
        w0 = [xs[i + 1] - xs[i] for i in range(3)]
        total = sum(c.rawValue() for c in cols)
        for c, w in zip(cols, w0):
            assert c.rawValue() / total == pytest.approx(w / sum(w0), abs=0.02)
        assert t.format().width().type() == t.format().width().Type.FixedLength

    def test_inner_boundary_trades_width_between_neighbours(self, qtbot):
        ed, t, xs, y, P = self._table(qtbot)
        total0 = xs[-1] - xs[0]
        assert ed._dragger.press(P(int(xs[1]), y))
        ed._dragger.move(P(int(xs[1]) + 60, y))
        ed._dragger.release()
        cols = [c.rawValue() for c in t.format().columnWidthConstraints()]
        assert cols[0] > cols[1] + 100
        assert abs(sum(cols) - total0) <= 4
        assert cols[2] == pytest.approx((xs[3] - xs[2]), abs=3)

    def test_columns_never_vanish(self, qtbot):
        ed, t, xs, y, P = self._table(qtbot)
        ed._dragger.press(P(int(xs[1]), y))
        ed._dragger.move(P(int(xs[1]) + 5000, y))
        ed._dragger.release()
        cols = [c.rawValue() for c in t.format().columnWidthConstraints()]
        assert min(cols) >= ed._dragger.MIN_COL

    def test_resized_table_exports_fixed_widths(self, qtbot):
        from emailclient.outgoing_html import email_ready_html
        ed, t, xs, y, P = self._table(qtbot)
        ed._dragger.press(P(int(xs[-1]), y))
        ed._dragger.move(P(int(xs[-1]) - 200, y))
        ed._dragger.release()
        out = email_ready_html(ed.toHtml())
        import re
        assert re.search(r'<table[^>]* width="\d+"', out)
        assert len(re.findall(r'<td width="\d+"', out)) == 3

    def test_mouse_press_on_grip_does_not_move_the_caret(self, qtbot):
        from PySide6.QtCore import Qt
        ed, t, xs, y, P = self._table(qtbot)
        before = ed.textCursor().position()
        qtbot.mousePress(ed.viewport(), Qt.MouseButton.LeftButton, pos=P(int(xs[1]), y))
        assert ed._dragger.dragging
        qtbot.mouseMove(ed.viewport(), P(int(xs[1]) + 40, y))
        qtbot.mouseRelease(ed.viewport(), Qt.MouseButton.LeftButton, pos=P(int(xs[1]) + 40, y))
        assert not ed._dragger.dragging
        assert ed.textCursor().position() == before
        assert t.format().columnWidthConstraints()

    def test_fit_to_contents_and_full_width(self, qtbot):
        from emailclient.ui import table_tools
        ed, t, xs, y, P = self._table(qtbot)
        table_tools.fit_to_contents(ed, t)
        xs2, _, _ = ed._dragger._boundaries(t)
        assert xs2[-1] - xs2[0] < (xs[-1] - xs[0]) / 2
        assert not t.format().columnWidthConstraints()
        table_tools.full_width(ed, t)
        xs3, _, _ = ed._dragger._boundaries(t)
        assert xs3[-1] - xs3[0] == pytest.approx(xs[-1] - xs[0], abs=2)


class TestToggleList:
    def _editor_html(self, qtbot, html):
        ed = _editor(qtbot)
        ed.setHtml(html)
        return ed

    def _select(self, ed, text_from, text_to):
        from PySide6.QtCore import QRegularExpression
        from PySide6.QtGui import QTextCursor
        plain = ed.toPlainText()
        s = plain.index(text_from)
        e = plain.index(text_to) + len(text_to)
        cur = ed.textCursor()
        cur.setPosition(s)
        cur.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(cur)

    def _blocks(self, ed):
        doc = ed.document()
        return [doc.findBlockByNumber(i) for i in range(doc.blockCount())]

    def test_only_the_selected_lines_of_a_soft_break_paragraph(self, qtbot):
        from PySide6.QtGui import QTextListFormat
        from emailclient.ui.compose import toggle_list
        # Pasted text: one paragraph, lines separated by <br>.
        ed = self._editor_html(qtbot, "<p>intro<br/>one<br/>two<br/>three<br/>outro</p>")
        assert ed.document().blockCount() == 1
        self._select(ed, "one", "two")
        toggle_list(ed, QTextListFormat.Style.ListDisc)
        listed = [b.text() for b in self._blocks(ed) if b.textList()]
        assert listed == ["one", "two"]
        assert ed.toPlainText() == "intro\none\ntwo\nthree\noutro"

    def test_loaded_paragraph_margins_do_not_spread_the_items(self, qtbot):
        from PySide6.QtGui import QTextListFormat
        from emailclient.ui.compose import toggle_list
        ed = self._editor_html(
            qtbot, '<p style="margin-top:12px;margin-bottom:12px">a</p>'
                   '<p style="margin-top:12px;margin-bottom:12px">b</p>')
        self._select(ed, "a", "b")
        toggle_list(ed, QTextListFormat.Style.ListDecimal)
        for b in self._blocks(ed):
            assert b.textList() is not None
            assert b.blockFormat().topMargin() == 0
            assert b.blockFormat().bottomMargin() == 0
        assert "margin-top:12px" not in ed.toHtml()

    def test_toggle_off_covers_the_whole_selection(self, qtbot):
        from PySide6.QtGui import QTextListFormat
        from emailclient.ui.compose import toggle_list
        ed = _editor(qtbot)
        ed.setPlainText("a\nb\nc")
        self._select(ed, "a", "c")
        toggle_list(ed, QTextListFormat.Style.ListDisc)
        assert all(b.textList() for b in self._blocks(ed))
        self._select(ed, "a", "c")
        toggle_list(ed, QTextListFormat.Style.ListDisc)
        assert not any(b.textList() for b in self._blocks(ed))
        assert ed.toPlainText() == "a\nb\nc"

    def test_caret_only_bullets_its_own_line(self, qtbot):
        from PySide6.QtGui import QTextCursor, QTextListFormat
        from emailclient.ui.compose import toggle_list
        ed = self._editor_html(qtbot, "<p>one<br/>two<br/>three</p>")
        cur = ed.textCursor()
        cur.setPosition(ed.toPlainText().index("two") + 1)
        ed.setTextCursor(cur)
        toggle_list(ed, QTextListFormat.Style.ListDisc)
        assert [b.text() for b in self._blocks(ed) if b.textList()] == ["two"]
        assert ed.textCursor().block().text() == "two"


class TestSignatureAndPainter:
    def _signature_html(self, qtbot, chosen_font=False):
        from PySide6.QtGui import QTextCharFormat
        from emailclient.ui.account_settings import SignatureBody
        sig = SignatureBody()
        qtbot.addWidget(sig)
        fmt = QTextCharFormat()
        if chosen_font:
            fmt.setFontFamilies(["Georgia"])
            fmt.setFontPointSize(10)
        sig.textCursor().insertText("Best regards,", fmt)
        return sig.toHtml()

    def test_signature_editor_uses_the_mail_font_size(self, qtbot):
        from emailclient.ui.account_settings import SignatureBody
        sig = SignatureBody()
        qtbot.addWidget(sig)
        assert sig.document().defaultFont().pointSize() == 13

    def test_without_body_font_only_touches_the_body_tag(self, qtbot):
        from emailclient.ui.compose import without_body_font
        html = self._signature_html(qtbot)
        assert 'style="' in html.split("<body")[1].split(">")[0]
        out = without_body_font(html)
        assert out.split("<body")[1].split(">")[0].strip() == ""
        assert "Best regards," in out
        assert without_body_font("") == ""

    def test_plain_signature_inherits_the_mail_font(self, qtbot):
        from PySide6.QtGui import QTextFormat
        from emailclient.ui.compose import insert_paragraph_html, without_body_font
        ed = _editor(qtbot)
        ed.textCursor().insertText("Hello")
        insert_paragraph_html(ed.textCursor(), without_body_font(self._signature_html(qtbot)))
        sig_block = ed.document().lastBlock()
        assert sig_block.text() == "Best regards,"
        fmt = sig_block.begin().fragment().charFormat()
        assert not fmt.hasProperty(QTextFormat.Property.FontFamilies)
        assert not fmt.hasProperty(QTextFormat.Property.FontPointSize)
        assert not fmt.hasProperty(QTextFormat.Property.FontPixelSize)

    def test_chosen_signature_font_still_travels(self, qtbot):
        from emailclient.ui.compose import insert_paragraph_html, without_body_font
        ed = _editor(qtbot)
        insert_paragraph_html(
            ed.textCursor(),
            without_body_font(self._signature_html(qtbot, chosen_font=True)))
        fmt = ed.document().lastBlock().begin().fragment().charFormat()
        assert fmt.fontFamilies() == ["Georgia"] and fmt.fontPointSize() == 10

    def test_painter_from_the_signature_does_not_shrink_the_text(self, qtbot):
        from PySide6.QtGui import QTextCharFormat, QTextCursor
        from emailclient.ui import format_painter
        from emailclient.ui.compose import insert_paragraph_html, without_body_font
        ed = _editor(qtbot)
        bold = QTextCharFormat()
        bold.setFontWeight(700)
        ed.textCursor().insertText("Hello team", bold)
        insert_paragraph_html(ed.textCursor(), without_body_font(self._signature_html(qtbot)))
        sig_block = ed.document().lastBlock()
        cur = ed.textCursor()
        cur.setPosition(sig_block.position() + 4)
        ed.setTextCursor(cur)
        char_fmt, block_fmt = format_painter.capture(ed)
        assert not char_fmt.properties()            # plain = the mail's own font
        sel = ed.textCursor()
        sel.setPosition(0)
        sel.setPosition(5, QTextCursor.MoveMode.KeepAnchor)
        ed.setTextCursor(sel)
        assert format_painter.apply(ed, char_fmt, block_fmt)
        frag = ed.document().firstBlock().begin().fragment().charFormat()
        assert frag.fontWeight() < 700                 # matches the signature
        assert frag.fontPointSize() == 0               # no size forced on it
