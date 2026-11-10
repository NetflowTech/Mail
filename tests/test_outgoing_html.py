"""email_ready_html: QTextEdit.toHtml() → something Outlook/Gmail render right."""
from __future__ import annotations

from emailclient.outgoing_html import email_ready_html

_QT_P = ('<p style=" margin-top:0px; margin-bottom:0px; margin-left:0px; '
         'margin-right:0px; -qt-block-indent:0; text-indent:0px;">')
_QT_EMPTY = ('<p style="-qt-paragraph-type:empty; margin-top:0px; '
             'margin-bottom:0px; margin-left:0px; margin-right:0px; '
             '-qt-block-indent:0; text-indent:0px;"><br /></p>')


class TestBlankLines:
    def test_empty_paragraph_becomes_nbsp(self):
        html = f"{_QT_P}Hey Aarij,</p>\n{_QT_EMPTY}\n{_QT_P}So and So</p>"
        out = email_ready_html(html)
        assert "<br />" not in out
        assert ('-qt-paragraph-type:empty; margin-top:0px; margin-bottom:0px; '
                'margin-left:0px; margin-right:0px; -qt-block-indent:0; '
                'text-indent:0px;">&nbsp;</p>') in out
        # Surrounding text untouched.
        assert f"{_QT_P}Hey Aarij,</p>" in out
        assert f"{_QT_P}So and So</p>" in out

    def test_several_blank_lines_all_kept(self):
        html = f"{_QT_P}a</p>{_QT_EMPTY}{_QT_EMPTY}{_QT_EMPTY}{_QT_P}b</p>"
        assert email_ready_html(html).count("&nbsp;</p>") == 3

    def test_br_inside_a_real_paragraph_is_left_alone(self):
        html = f"{_QT_P}line one<br />line two</p>"
        assert email_ready_html(html) == html

    def test_empty_list_item(self):
        html = ('<li style="-qt-paragraph-type:empty; margin-top:0px;">'
                '<br /></li>')
        assert email_ready_html(html).endswith(">&nbsp;</li>")

    def test_empty_input(self):
        assert email_ready_html("") == ""


class TestTables:
    def test_empty_cells_get_nbsp(self):
        html = '<table><tr><td style="x"></td><th></th></tr></table>'
        out = email_ready_html(html)
        assert '<td style="x">&nbsp;</td>' in out
        assert "<th>&nbsp;</th>" in out

    def test_unitless_padding_gets_px(self):
        html = ('<td style=" padding-left:4; padding-right:4; padding-top:0; '
                'padding-bottom:4.5;">x</td>')
        out = email_ready_html(html)
        assert "padding-left:4px;" in out
        assert "padding-top:0px;" in out
        assert "padding-bottom:4.5px;" in out

    def test_styled_border_without_width_gets_1px(self):
        # What Qt writes for a cell pasted from Word: style + colour, no width.
        html = ('<td style=" border-top-color:#000000; border-top-style:solid; '
                'border-left-style:solid;">x</td>')
        out = email_ready_html(html)
        assert "border-width:1px;" in out

    def test_border_with_width_untouched(self):
        html = ('<td style=" border-top:1px; border-top-style:solid; '
                'border-top-color:#000000;">x</td>')
        assert "border-width:1px" not in email_ready_html(html)

    def test_cell_without_borders_untouched(self):
        html = '<td style=" vertical-align:top;">x</td>'
        assert email_ready_html(html) == html

    def test_own_table_export_is_email_ready(self):
        # The exact shape QTextEdit.toHtml() gives our inserted tables.
        html = ('<table border="0" style=" border-collapse:collapse;" '
                'width="100%" cellspacing="0" cellpadding="4">\n<tr>\n'
                '<td style=" padding-left:4; padding-right:4; padding-top:4; '
                'padding-bottom:4; border-top:1px; border-right:1px; '
                'border-bottom:1px; border-left:1px; border-top-color:#000000; '
                'border-top-style:solid;">\n' + _QT_P + 'A1</p></td>\n'
                '<td style=" padding-left:4;"></td></tr></table>')
        out = email_ready_html(html)
        assert 'padding-left:4px; padding-right:4px;' in out
        assert '<td style=" padding-left:4px;">&nbsp;</td>' in out
        assert "border-width:1px" not in out   # width already explicit
