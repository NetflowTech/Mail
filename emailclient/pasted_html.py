"""Clean up HTML from the clipboard before the compose editor sees it.

Word and Excel wrap the CSS in their clipboard HTML in an HTML comment
("<style><!-- ... --></style>"). Qt's CSS parser gives up at the comment, so
every class-based rule is lost: the bold header row of an Excel range, the
borders Excel puts in ".xl65 {border: .5pt solid windowtext}", Word's
paragraph styles. Inlining the rules into each tag's style attribute gets
them back without depending on Qt's parser.

Text copied from a web page (or from the reader) arrives with the page's
white background on every paragraph and span. Invisible on a white page,
but the recipient's dark-mode client shows white blocks behind the text —
so white and transparent backgrounds are dropped on the way in.

No Qt imports: unit-testable.
"""
from __future__ import annotations

import html as _html
import re

_STYLE_BLOCK = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_CSS_COMMENT = re.compile(r"<!--|-->|/\*.*?\*/", re.DOTALL)
_AT_RULE = re.compile(r"@[\w-]+[^{;]*(?:\{[^{}]*\}|;)")
_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_SIMPLE_SELECTOR = re.compile(r"([a-zA-Z][\w-]*)?(?:\.([\w-]+))?")
_START_TAG = re.compile(r"<([a-zA-Z][\w:-]*)((?:\s[^<>]*)?)(/?)>")
_CLASS_ATTR = re.compile(
    r"""\sclass\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)
_STYLE_ATTR = re.compile(
    r"""\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.IGNORECASE)

# Declarations that only mean something to Office, or that hurt in an email
# (nowrap keeps a long cell from wrapping and pushes the table off-screen).
_DROP_PROPS = ("mso-", "white-space", "height", "page-break", "tab-stops",
               "layout-grid")

_WHITE = (r"(?:white|#fff|#ffffff|rgb\(\s*255\s*,\s*255\s*,\s*255\s*\)|"
          r"rgba\(\s*255\s*,\s*255\s*,\s*255\s*,\s*1(?:\.0*)?\s*\)|"
          r"transparent|rgba\(\s*0\s*,\s*0\s*,\s*0\s*,\s*0(?:\.0*)?\s*\))")
_WHITE_BG_DECL = re.compile(
    r"(?:^|;)\s*background(?:-color)?\s*:\s*" + _WHITE + r"\s*(?=;|$)",
    re.IGNORECASE)
_WHITE_BGCOLOR_ATTR = re.compile(
    r"""\sbgcolor\s*=\s*(?:"\s*""" + _WHITE + r"""\s*"|'\s*""" + _WHITE
    + r"""\s*'|""" + _WHITE + r")(?=[\s/>])",
    re.IGNORECASE)

_SPREADSHEET = re.compile(
    r"schemas-microsoft-com:office:excel|content=\"?Excel\.Sheet|"
    r"content=\"?Microsoft Excel", re.IGNORECASE)


# ----- class → inline style -------------------------------------------------

def _clean_declarations(decls: str) -> list[str]:
    out = []
    for d in decls.split(";"):
        # The result lands inside style="..."; Excel quotes font names.
        d = " ".join(d.split()).replace('"', "'")
        if ":" not in d:
            continue
        prop = d.split(":", 1)[0].strip().lower()
        if prop.startswith(_DROP_PROPS):
            continue
        out.append(d)
    return out


def _rules(css: str) -> list[tuple[str | None, str | None, list[str]]]:
    """(element, class, declarations) for every simple selector in *css*.
    Complex selectors (descendants, pseudo-classes, ids) are skipped."""
    css = _CSS_COMMENT.sub("", css)
    css = _AT_RULE.sub("", css)
    rules = []
    for selectors, decls in _RULE.findall(css):
        cleaned = _clean_declarations(decls)
        if not cleaned:
            continue
        for sel in selectors.split(","):
            sel = sel.strip()
            m = _SIMPLE_SELECTOR.fullmatch(sel)
            if not sel or not m or not (m.group(1) or m.group(2)):
                continue
            elem = m.group(1).lower() if m.group(1) else None
            rules.append((elem, m.group(2), cleaned))
    return rules


def _attr_value(match: re.Match | None) -> str:
    if match is None:
        return ""
    return next((g for g in match.groups() if g is not None), "")


def inline_class_styles(html: str) -> str:
    """Move the rules in <style> blocks onto the tags they select, then drop
    the blocks. Inline styles already on a tag keep precedence."""
    blocks = _STYLE_BLOCK.findall(html)
    if not blocks:
        return html
    rules = []
    for css in blocks:
        rules.extend(_rules(css))
    html = _STYLE_BLOCK.sub("", html)
    if not rules:
        return html
    by_elem: dict[str, list[str]] = {}
    by_class: dict[str, list[str]] = {}
    by_both: dict[tuple[str, str], list[str]] = {}
    for elem, cls, decls in rules:
        if elem and cls:
            by_both.setdefault((elem, cls), []).extend(decls)
        elif cls:
            by_class.setdefault(cls, []).extend(decls)
        else:
            by_elem.setdefault(elem, []).extend(decls)

    def fix(m: re.Match) -> str:
        name, attrs, slash = m.group(1).lower(), m.group(2), m.group(3)
        classes = _attr_value(_CLASS_ATTR.search(attrs)).split()
        decls: list[str] = list(by_elem.get(name, ()))
        for cls in classes:
            decls += by_class.get(cls, ())
        for cls in classes:
            decls += by_both.get((name, cls), ())
        if not decls:
            return m.group(0)
        sm = _STYLE_ATTR.search(attrs)
        own = _clean_declarations(_attr_value(sm)) if sm else []
        style = "; ".join(decls + own)
        if sm:
            attrs = attrs[:sm.start()] + attrs[sm.end():]
        attrs = attrs.rstrip()
        return f'<{m.group(1)}{attrs} style="{style}"{slash}>'

    return _START_TAG.sub(fix, html)


# ----- backgrounds ------------------------------------------------------------

def strip_white_backgrounds(html: str) -> str:
    """Drop white / transparent backgrounds (style declarations and bgcolor
    attributes). They are the paper colour the text was copied from, not a
    highlight the user chose, and they turn into white boxes in dark mode."""
    if not html:
        return html

    def fix_style(m: re.Match) -> str:
        value = _attr_value(m)
        new = _WHITE_BG_DECL.sub("", value)
        new = re.sub(r"^\s*;\s*", "", new)
        if new == value:
            return m.group(0)
        return f' style="{new.strip()}"'

    out = _STYLE_ATTR.sub(fix_style, html)
    return _WHITE_BGCOLOR_ATTR.sub("", out)


# ----- lengths ----------------------------------------------------------------

# Qt reads margins, padding and indents only in px: Word's "margin-bottom:8.0pt"
# (the gap after every paragraph) and "text-indent:-18.0pt" became 0, and the
# pasted paragraphs ran together. Font sizes in pt are fine and left alone.
_BOX_PROPS = ("margin", "padding", "text-indent")
_PX_PER = {"pt": 96 / 72, "pc": 16.0, "in": 96.0, "cm": 96 / 2.54,
           "mm": 96 / 25.4}
_LENGTH = re.compile(r"(-?(?:\d+\.?\d*|\.\d+))(pt|pc|in|cm|mm)\b",
                     re.IGNORECASE)


def _to_px(m: re.Match) -> str:
    px = round(float(m.group(1)) * _PX_PER[m.group(2).lower()])
    return f"{px or 0}px"   # "or 0": no "-0px"


def lengths_to_px(html: str) -> str:
    """Rewrite margin / padding / text-indent lengths in style attributes as
    px, the only unit QTextEdit's HTML parser applies to them."""
    if not html:
        return html

    def fix_style(m: re.Match) -> str:
        value = _attr_value(m)
        decls = []
        changed = False
        for d in value.split(";"):
            prop, sep, val = d.partition(":")
            if sep and prop.strip().lower().startswith(_BOX_PROPS):
                new = _LENGTH.sub(_to_px, val)
                changed |= new != val
                d = prop + sep + new
            decls.append(d)
        if not changed:
            return m.group(0)
        return ' style="' + ";".join(decls).replace('"', "'") + '"'

    return _STYLE_ATTR.sub(fix_style, html)


# ----- what came from where -----------------------------------------------------

def is_spreadsheet(html: str) -> bool:
    """True for the HTML Excel puts on the clipboard (Windows and Mac)."""
    return bool(html) and bool(_SPREADSHEET.search(html[:4000]))


def table_from_tsv(text: str) -> str | None:
    """A cell grid copied as tab-separated text (what a spreadsheet gives when
    it has no HTML on the clipboard) → table HTML. None unless it really looks
    like a grid: at least 2 rows and 2 columns, every row the same width."""
    if not text or "\t" not in text:
        return None
    rows = [r.split("\t") for r in text.replace("\r\n", "\n").replace("\r", "\n")
            .strip("\n").split("\n")]
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    if any(len(r) != len(rows[0]) for r in rows):
        return None
    if len(rows) > 2000 or len(rows[0]) > 200:
        return None   # not a grid anyone meant to mail
    body = "".join(
        "<tr>" + "".join(f"<td>{_html.escape(c.strip())}</td>" for c in r) + "</tr>"
        for r in rows)
    return ('<table border="0" cellspacing="0" cellpadding="0" '
            f'style="border-collapse:collapse">{body}</table>')


def clean(html: str) -> str:
    """Everything a pasted HTML fragment gets before QTextEdit parses it."""
    if not html:
        return html
    return strip_white_backgrounds(lengths_to_px(inline_class_styles(html)))
