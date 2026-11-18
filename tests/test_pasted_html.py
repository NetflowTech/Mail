"""pasted_html: what clipboard HTML gets before the compose editor parses it."""
from __future__ import annotations

from emailclient import pasted_html as ph

# Trimmed from what Excel for Mac puts on the clipboard (Windows is the same
# shape): the CSS sits inside an HTML comment, cells reference it by class.
EXCEL = """<html xmlns:x="urn:schemas-microsoft-com:office:excel">
<head><meta name=ProgId content=Excel.Sheet>
<style>
<!--table
\t{mso-displayed-decimal-separator:"\\.";}
@page
\t{margin:.75in .7in .75in .7in;}
td
\t{padding-top:1px;
\tcolor:black;
\tfont-size:12.0pt;
\tfont-weight:400;
\tfont-family:"Aptos Narrow", sans-serif;
\tmso-number-format:General;
\tborder:none;
\twhite-space:nowrap;}
.xl63
\t{font-weight:700;
\tfont-family:"Aptos Narrow";
\tmso-generic-font-family:auto;}
-->
</style></head>
<body link="#467886" vlink="#96607D">
<table border=0 cellpadding=0 cellspacing=0 width=261 style='border-collapse:
 collapse;width:195pt'>
<!--StartFragment-->
 <col width=87 span=3 style='width:65pt'>
 <tr height=21 style='height:16.0pt'>
  <td height=21 class=xl63 width=87 style='height:16.0pt;width:65pt'>Item</td>
  <td class=xl63 width=87 style='width:65pt'>Qty</td>
 </tr>
 <tr><td>Widget</td><td align=right>4</td></tr>
<!--EndFragment-->
</table></body></html>"""


class TestInlineClassStyles:
    def test_comment_wrapped_class_rule_lands_on_the_cell(self):
        out = ph.inline_class_styles(EXCEL)
        header = out[out.index("<td height=21 class=xl63"):out.index("Item")]
        assert "font-weight:700" in header
        assert "<style" not in out

    def test_element_rule_applies_and_inline_style_wins(self):
        out = ph.inline_class_styles(EXCEL)
        header = out[out.index("<td height=21 class=xl63"):out.index("Item")]
        # td rule, then the class, then the tag's own style — later wins.
        assert header.index("font-weight:400") < header.index("font-weight:700")
        assert header.rstrip(">").endswith("width:65pt\"")
        assert header.count("style=") == 1

    def test_office_only_and_harmful_declarations_dropped(self):
        out = ph.inline_class_styles(EXCEL)
        assert "mso-" not in out
        assert "white-space" not in out
        header = out[out.index("<td height=21 class=xl63"):out.index("Item")]
        assert "height:16.0pt" not in header

    def test_quotes_in_values_do_not_break_the_attribute(self):
        out = ph.inline_class_styles(EXCEL)
        assert "font-family:'Aptos Narrow'" in out
        assert '"Aptos Narrow"' not in out

    def test_element_dot_class_and_grouped_selectors(self):
        html = ("<style>p.MsoNormal, li.MsoNormal {margin:0in} "
                ".red{color:red} span.red{font-weight:700}</style>"
                '<p class=MsoNormal>a</p><span class="red big">b</span>')
        out = ph.inline_class_styles(html)
        assert '<p class=MsoNormal style="margin:0in">' in out
        assert '<span class="red big" style="color:red; font-weight:700">' in out

    def test_complex_selectors_are_ignored(self):
        html = "<style>td p {color:red} a:hover{color:blue} #x{color:green}</style><p>a</p>"
        assert ph.inline_class_styles(html) == "<p>a</p>"

    def test_no_style_block_is_untouched(self):
        html = '<table><tr><td class=x>1</td></tr></table>'
        assert ph.inline_class_styles(html) == html


class TestWhiteBackgrounds:
    def test_chrome_copy_loses_its_page_background(self):
        html = ('<p style="margin:0; background-color: rgb(255, 255, 255);">'
                '<span style="color: rgb(31, 33, 37); background-color: rgb(255, 255, 255);">'
                "x</span></p>")
        out = ph.strip_white_backgrounds(html)
        assert "background" not in out
        assert 'style="margin:0;"' in out
        assert "color: rgb(31, 33, 37)" in out

    def test_qt_form_and_bgcolor_attribute(self):
        html = ('<p style=" margin-top:0px; background-color:#ffffff;">x</p>'
                '<td bgcolor="#ffffff" style="x">y</td><td bgcolor=white>z</td>')
        out = ph.strip_white_backgrounds(html)
        assert "#ffffff" not in out and "bgcolor" not in out
        assert '<td style="x">y</td><td>z</td>' in out

    def test_real_highlights_stay(self):
        html = ('<span style="background-color:#ffff00;">a</span>'
                '<td bgcolor="#d9d9d9">b</td>'
                '<p style="background:#fafafa">c</p>')
        assert ph.strip_white_backgrounds(html) == html

    def test_transparent_and_shorthand(self):
        html = '<p style="background: transparent; color:red">a</p><p style="background:white">b</p>'
        out = ph.strip_white_backgrounds(html)
        assert out == '<p style="color:red">a</p><p style="">b</p>'


class TestSource:
    def test_excel_is_a_spreadsheet(self):
        assert ph.is_spreadsheet(EXCEL)
        assert ph.is_spreadsheet('<meta name=Generator content="Microsoft Excel 15">')

    def test_word_and_web_are_not(self):
        assert not ph.is_spreadsheet('<meta name=Generator content="Microsoft Word 15">')
        assert not ph.is_spreadsheet("<table><tr><td>a</td></tr></table>")
        assert not ph.is_spreadsheet("")


class TestTableFromTsv:
    def test_grid_becomes_a_table(self):
        out = ph.table_from_tsv("Item\tQty\r\nWidget\t4\r\n")
        assert out.count("<tr>") == 2 and out.count("<td>") == 4
        assert "<td>Widget</td><td>4</td>" in out

    def test_cells_are_escaped(self):
        assert "&lt;b&gt;" in ph.table_from_tsv("<b>\t1\n2\t3")

    def test_not_a_grid(self):
        assert ph.table_from_tsv("just text") is None
        assert ph.table_from_tsv("a\tb") is None               # one row
        assert ph.table_from_tsv("a\nb") is None               # one column
        assert ph.table_from_tsv("a\tb\nc") is None            # ragged
        assert ph.table_from_tsv("") is None


class TestLimits:
    def test_absurdly_wide_grid_is_not_a_table(self):
        row = "\t".join(["x"] * 201)
        assert ph.table_from_tsv(f"{row}\n{row}") is None
        row = "\t".join(["x"] * 200)
        assert ph.table_from_tsv(f"{row}\n{row}") is not None


class TestLengthsToPx:
    """Qt applies margins, padding and indents only in px; Word writes pt/cm."""

    def test_words_gap_after_a_paragraph_survives(self):
        # Word's Normal style, as it arrives after the class is inlined.
        out = ph.lengths_to_px(
            '<p style="margin-top:0cm; margin-bottom:8.0pt; line-height:115%; '
            'font-size:12.0pt">x</p>')
        assert "margin-top:0px" in out and "margin-bottom:11px" in out
        # Font size and line height are Qt-readable as they are.
        assert "font-size:12.0pt" in out and "line-height:115%" in out

    def test_indents_shorthand_and_units(self):
        out = ph.lengths_to_px(
            "<p style='margin:0cm 0cm .0001pt 36.0pt;text-indent:-18.0pt;"
            "padding:0in 5.4pt'>x</p>")
        assert "margin:0px 0px 0px 48px" in out
        assert "text-indent:-24px" in out
        assert "padding:0px 7px" in out

    def test_quotes_inside_stay_valid(self):
        out = ph.lengths_to_px(
            """<p style='margin-left:1cm;font-family:"Aptos"'>x</p>""")
        assert out == """<p style="margin-left:38px;font-family:'Aptos'">x</p>"""

    def test_px_and_unrelated_styles_are_untouched(self):
        html = '<p style="margin-bottom:11px; width:5cm">x</p>'
        assert ph.lengths_to_px(html) == html

    def test_clean_runs_it_on_word_clipboard_html(self):
        word = ('<style><!-- p.MsoNormal {margin-top:0cm; margin-bottom:8.0pt;'
                ' mso-pagination:widow-orphan} --></style>'
                '<p class=MsoNormal>One</p>')
        assert "margin-bottom:11px" in ph.clean(word)


def test_pasted_word_paragraphs_keep_their_gap(qtbot):
    from PySide6.QtCore import QMimeData
    from emailclient.ui import theme
    from emailclient.ui.compose import _RichBody
    from emailclient.outgoing_html import email_ready_html
    theme.apply_theme("AMail Light")
    ed = _RichBody()
    qtbot.addWidget(ed)
    md = QMimeData()
    md.setHtml('<style><!-- p.MsoNormal {margin:0cm 0cm 8.0pt 0cm; '
               'line-height:115%} --></style>'
               '<p class=MsoNormal>One</p><p class=MsoNormal>Two</p>')
    ed.insertFromMimeData(md)
    gaps = []
    b = ed.document().begin()
    while b.isValid():
        if b.text().strip():
            gaps.append(b.blockFormat().bottomMargin())
        b = b.next()
    assert gaps == [11.0, 11.0]
    assert "margin-bottom:11px" in email_ready_html(ed.toHtml())
