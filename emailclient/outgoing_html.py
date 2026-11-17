"""Make the compose editor's HTML render the same in real mail clients.

QTextEdit.toHtml() is written for Qt's own renderer. Most of it is fine for
Gmail, Outlook and Apple Mail, but a few details are not — and the worst one
silently swallows blank lines the user typed. No Qt imports: unit-testable.
"""
from __future__ import annotations

import re

from .pasted_html import strip_white_backgrounds

# A paragraph the user left empty. Qt writes it as "<p ...><br /></p>".
# Outlook's renderer (Word) drops a <br> that is the only thing in a
# paragraph, then drops the now-empty paragraph — so every blank line the
# user typed vanished from the delivered mail. "&nbsp;" is the one form all
# clients keep as a blank line.
_EMPTY_BLOCK = re.compile(
    r"(<(?:p|li)\b[^>]*-qt-paragraph-type:empty[^>]*>)\s*<br\s*/?>\s*(</(?:p|li)>)",
    re.IGNORECASE)

# An empty table cell renders at zero height in Outlook unless it holds
# something; the ones the user left blank in a table are exactly those.
_EMPTY_CELL = re.compile(r"(<t[dh]\b[^>]*>)\s*(</t[dh]>)", re.IGNORECASE)

# Qt writes cell padding without a unit ("padding-left:4"), which browsers
# ignore outside quirks mode. "0" needs no unit.
_UNITLESS_PAD = re.compile(
    r"(padding-(?:left|right|top|bottom)\s*:\s*)(\d*\.?\d+)(?=\s*[;\"'])",
    re.IGNORECASE)

_CELL_TAG = re.compile(r"<t[dh]\b[^>]*>", re.IGNORECASE)
_STYLE_ATTR = re.compile(r"""\bstyle\s*=\s*(["'])(.*?)\1""",
                         re.IGNORECASE | re.DOTALL)


def _fix_cell_tag(tag: str) -> str:
    """Give a cell with solid borders but no border width a 1px width.

    A table pasted from Word or Excel keeps its border *style* and *colour*
    through Qt but loses the width. Browsers then fall back to "medium"
    (3px) — the recipient sees fat borders the sender never had."""
    m = _STYLE_ATTR.search(tag)
    if not m:
        return tag
    style = m.group(2)
    low = style.lower()
    if "border" not in low or "-style:" not in low:
        return tag
    if re.search(r"border(?:-(?:left|right|top|bottom))?(?:-width)?\s*:\s*\d", low):
        return tag
    new_style = style.rstrip().rstrip(";") + "; border-width:1px;"
    return tag[:m.start(2)] + new_style + tag[m.end(2):]


# A line break that is the last thing in its paragraph (Shift+Enter, or the
# <br> text copied from a web page ends with). Qt shows the empty line after
# it; browsers and Outlook drop a trailing <br>, so the mail arrived one line
# shorter than the editor showed. "&nbsp;" after it keeps the line.
_TRAILING_BR = re.compile(
    r"(<br\s*/?>)((?:\s*</span>)*\s*</(?:p|li|td|th|div)>)", re.IGNORECASE)

# Qt writes a list item's two formats as two style attributes on one <li>.
# Browsers keep the first and ignore the second, so the margins were lost.
_ANY_TAG = re.compile(r"<[a-zA-Z][^>]*>")

# QTextEdit puts the default font on <body style>. Gmail throws the body's
# attributes away, so text the user typed without picking a font arrived in
# the recipient's default. A <div> carrying the same style survives.
_BODY_STYLE = re.compile(r"<body\s+style=\"([^\"]*)\"\s*>", re.IGNORECASE)


def _merge_duplicate_styles(tag: str) -> str:
    styles = list(_STYLE_ATTR.finditer(tag))
    if len(styles) < 2:
        return tag
    merged = "; ".join(s.group(2).strip().rstrip(";") for s in styles
                       if s.group(2).strip())
    out = tag
    for s in reversed(styles[1:]):
        out = out[:s.start()].rstrip() + out[s.end():]
    first = _STYLE_ATTR.search(out)
    return out[:first.start(2)] + merged + ";" + out[first.end(2):]


def _wrap_body_style(html: str) -> str:
    m = _BODY_STYLE.search(html)
    if not m or "</body>" not in html:
        return html
    head = html[:m.start()] + f'<body><div style="{m.group(1)}">'
    rest = html[m.end():]
    i = rest.rfind("</body>")
    return head + rest[:i] + "</div>" + rest[i:]


def email_ready_html(html: str) -> str:
    """Normalise *html* from QTextEdit.toHtml() for delivery to any client."""
    if not html:
        return html
    out = _EMPTY_BLOCK.sub(r"\1&nbsp;\2", html)
    out = _TRAILING_BR.sub(r"\1&nbsp;\2", out)
    out = _EMPTY_CELL.sub(r"\1&nbsp;\2", out)
    out = _UNITLESS_PAD.sub(r"\1\2px", out)
    out = _CELL_TAG.sub(lambda m: _fix_cell_tag(m.group(0)), out)
    out = _ANY_TAG.sub(lambda m: _merge_duplicate_styles(m.group(0)), out)
    out = strip_white_backgrounds(out)
    out = _wrap_body_style(out)
    return out
