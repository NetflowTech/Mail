"""Minimal .xlsx writer — standard library only.

An .xlsx file is a zip of a handful of XML parts. Writing just those parts keeps
the Excel export free of a third-party dependency, which matters for the frozen
build (no new PyInstaller hidden import, installer size unchanged, nothing new
for the signing server to vet). Supports what a list export needs: one sheet,
a bold frozen header row, an auto-filter, column widths, text and numbers.
"""
from __future__ import annotations

import re
import zipfile
from xml.sax.saxutils import escape

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

# Characters XML 1.0 forbids even when escaped (control chars except \t \n \r).
_BAD_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


def col_letter(n: int) -> str:
    """1 → A, 26 → Z, 27 → AA."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value, style: int = 0) -> str:
    s_attr = f' s="{style}"' if style else ""
    if value is None or value == "":
        return f'<c r="{ref}"{s_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{s_attr}><v>{int(value)}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"{s_attr}><v>{value}</v></c>'
    text = escape(_BAD_XML_CHARS.sub("", str(value)))
    return f'<c r="{ref}" t="inlineStr"{s_attr}><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(headers: list, rows: list[list], col_widths: list[int] | None) -> str:
    ncols = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 1
    nrows = len(rows) + 1
    parts = [f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
             f'<dimension ref="A1:{col_letter(ncols)}{nrows}"/>'
             '<sheetViews><sheetView workbookViewId="0">'
             '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
             '</sheetView></sheetViews>']
    if col_widths:
        parts.append("<cols>")
        for i, w in enumerate(col_widths, 1):
            parts.append(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>')
        parts.append("</cols>")
    parts.append("<sheetData>")
    parts.append('<row r="1">' + "".join(
        _cell(f"{col_letter(c)}1", h, style=1) for c, h in enumerate(headers, 1)) + "</row>")
    for r, row in enumerate(rows, 2):
        parts.append(f'<row r="{r}">' + "".join(
            _cell(f"{col_letter(c)}{r}", v) for c, v in enumerate(row, 1)) + "</row>")
    parts.append("</sheetData>")
    if rows:
        parts.append(f'<autoFilter ref="A1:{col_letter(ncols)}{nrows}"/>')
    parts.append("</worksheet>")
    return "".join(parts)


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<styleSheet xmlns="{_NS_MAIN}">'
    '<fonts count="2">'
    '<font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font>'
    '</fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2">'
    '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
    '</cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    '</styleSheet>'
)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
    '</Types>'
)

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_NS_PKG_REL}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)

_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_NS_PKG_REL}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    '</Relationships>'
)


def _workbook_xml(sheet_name: str) -> str:
    # Excel: max 31 chars, none of []:*?/\ .
    name = re.sub(r'[\[\]:*?/\\]', "_", sheet_name).strip()[:31] or "Sheet1"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
            f'<sheets><sheet name="{escape(name, {chr(34): "&quot;"})}" sheetId="1" r:id="rId1"/></sheets>'
            '</workbook>')


def write_xlsx(path: str, headers: list, rows: list[list], *,
               sheet_name: str = "Sheet1",
               col_widths: list[int] | None = None) -> None:
    """Write *headers* + *rows* to *path* as a single-sheet workbook.

    Header row is bold and frozen, with an auto-filter over the data. Cell
    values may be str, int, float, bool or None/'' (blank)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _workbook_xml(sheet_name))
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", _sheet_xml(headers, rows, col_widths))
