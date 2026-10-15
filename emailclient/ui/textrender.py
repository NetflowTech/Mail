"""Plain-text email -> presentable HTML (no Qt imports; unit-testable)."""
from __future__ import annotations

import html
import re

_URL_RE = re.compile(r"https?://\S+")


def linkify(escaped: str) -> str:
    """Wrap URLs in already-HTML-escaped text with anchors."""
    def _repl(m: re.Match) -> str:
        url = m.group(0)
        # Don't swallow trailing punctuation that's likely sentence-level.
        trailing = ""
        while url and url[-1] in ".,;:!?)\"'":
            trailing = url[-1] + trailing
            url = url[:-1]
        return f'<a href="{url}">{url}</a>{trailing}'
    return _URL_RE.sub(_repl, escaped)


def plain_to_html(text: str) -> str:
    """Convert plain-text email body to HTML.

    Escapes, linkifies URLs, groups consecutive '>'-quoted lines into
    blockquotes (stripping one level of '> ' prefix), and preserves line
    breaks with <br>.
    """
    out: list[str] = []
    quote_lines: list[str] = []

    def _flush_quote():
        if quote_lines:
            out.append("<blockquote>" + "<br>".join(quote_lines) + "</blockquote>")
            quote_lines.clear()

    for line in (text or "").splitlines():
        if line.startswith(">"):
            inner = line[1:]
            if inner.startswith(" "):
                inner = inner[1:]
            quote_lines.append(linkify(html.escape(inner)))
        else:
            _flush_quote()
            out.append(linkify(html.escape(line)) + "<br>")
    _flush_quote()
    return "<div>" + "".join(out) + "</div>"
