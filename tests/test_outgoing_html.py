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


class TestLineBreaks:
    def test_trailing_br_keeps_its_line(self):
        # Shift+Enter at the end of a paragraph: Qt shows an empty line after
        # it; browsers drop a trailing <br>. &nbsp; after it keeps the line.
        html = f"{_QT_P}<span>Regards,<br /></span></p>"
        out = email_ready_html(html)
        assert "<br />&nbsp;</span></p>" in out

    def test_only_the_last_br_in_a_run(self):
        html = f"{_QT_P}a<br /><br /></p>"
        assert email_ready_html(html) == f"{_QT_P}a<br /><br />&nbsp;</p>"

    def test_paragraph_holding_only_a_br_is_two_lines(self):
        html = f'{_QT_P}<span style=" font-size:14px;"><br /></span></p>'
        assert "<br />&nbsp;</span></p>" in email_ready_html(html)


class TestClientQuirks:
    def test_duplicate_style_attributes_are_merged(self):
        html = ('<li style=" font-size:14px; color:#1f2125;" '
                'style=" margin-top:0px; margin-bottom:0px;">x</li>')
        out = email_ready_html(html)
        assert out.count("style=") == 1
        assert ('style="font-size:14px; color:#1f2125; margin-top:0px; '
                'margin-bottom:0px;">x</li>') in out

    def test_white_backgrounds_are_dropped(self):
        html = (f'<p style=" margin-top:0px; background-color:#ffffff;">'
                '<span style=" color:#1f2125; background-color:#ffffff;">x</span></p>'
                '<td bgcolor="#ffffff" style=" padding-left:4px;">y</td>')
        out = email_ready_html(html)
        assert "#ffffff" not in out and "bgcolor" not in out
        assert "color:#1f2125" in out

    def test_body_font_moves_onto_a_div(self):
        html = ('<html><head></head><body style=" font-family:\'Segoe UI\'; '
                'font-size:13px; font-weight:400; font-style:normal;">\n'
                f'{_QT_P}hi</p></body></html>')
        out = email_ready_html(html)
        assert ('<body><div style=" font-family:\'Segoe UI\'; font-size:13px; '
                'font-weight:400; font-style:normal;">') in out
        assert out.endswith("hi</p></div></body></html>")

    def test_fragment_without_body_is_fine(self):
        assert email_ready_html(f"{_QT_P}hi</p>") == f"{_QT_P}hi</p>"
