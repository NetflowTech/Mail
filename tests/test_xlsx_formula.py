"""Exported spreadsheets never turn email text into live formulas."""
from __future__ import annotations

import zipfile

from emailclient.xlsx import csv_safe, write_xlsx


def _sheet(path):
    with zipfile.ZipFile(path) as z:
        return z.read("xl/worksheets/sheet1.xml").decode(), z.read("xl/styles.xml").decode()


def test_formula_like_text_gets_quote_prefix_style(tmp_path):
    p = tmp_path / "a.xlsx"
    write_xlsx(str(p), ["Name"], [["=HYPERLINK(\"http://x\",\"y\")"], ["@SUM(1)"],
                                  ["plain"], [-5]])
    sheet, styles = _sheet(p)
    assert 'r="A2" t="inlineStr" s="2"' in sheet
    assert 'r="A3" t="inlineStr" s="2"' in sheet
    assert 'r="A4" t="inlineStr">' in sheet          # ordinary text untouched
    assert '<c r="A5"><v>-5</v>' in sheet             # numbers stay numbers
    assert 'quotePrefix="1"' in styles
    # The text itself is unchanged (no stray apostrophe shown in the cell).
    assert "=HYPERLINK" in sheet and "'=HYPERLINK" not in sheet


def test_formula_like_header_stays_bold(tmp_path):
    p = tmp_path / "b.xlsx"
    write_xlsx(str(p), ["=X"], [])
    sheet, _ = _sheet(p)
    assert 'r="A1" t="inlineStr" s="3"' in sheet


def test_csv_safe():
    assert csv_safe("=1+1") == "'=1+1"
    assert csv_safe("+cmd") == "'+cmd"
    assert csv_safe("-x") == "'-x"
    assert csv_safe("@x") == "'@x"
    assert csv_safe("\t=x") == "'\t=x"
    assert csv_safe("alice@example.com") == "alice@example.com"
    assert csv_safe(5) == 5
    assert csv_safe("") == ""
