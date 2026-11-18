"""Make an untrusted email's HTML safe to quote in a reply or forward.

The quoted original goes back out to the recipients, whose mail program may
run what a hostile sender hid in it. The old regex filter missed anything it
didn't expect: an unquoted ``href=javascript:...``, or ``<scr<script>ipt>``,
which turned back into ``<script>`` once the inner tag was removed.

This reads the HTML with a real parser and writes a fresh document from what
it understood, so text that wasn't parsed as markup can only come out as text.
It removes:

* elements that run code or embed other documents (script, iframe, object,
  svg, ...), with their content, and form controls;
* event handler attributes (``onclick=``...) and ``srcdoc``/``formaction``;
* links and sources whose scheme isn't http(s), mailto, tel or cid (images may
  also be ``data:image/...``), however they are spelt or encoded;
* styles that can run code (``expression()``, ``url(javascript:...)``...).

Everything else (tables, inline styles, images) is kept, so a newsletter
still looks like itself in the quote. Standard library only.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Dropped together with everything inside them.
_DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "frame", "frameset", "object", "embed",
    "applet", "svg", "math", "template", "noscript", "noembed", "noframes",
    "form", "xml", "title", "head",
})
# Dropped, but their content stays (as text/other markup).
_DROP_TAG_ONLY = frozenset({
    "link", "meta", "base", "input", "button", "textarea", "select", "option",
    "optgroup", "keygen", "portal", "param", "source", "track", "html", "body",
    "audio", "video", "dialog",
})
_VOID = frozenset({
    "area", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr", "base", "keygen",
})
_TAG_NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]{0,40}$")
_ATTR_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:.-]{0,60}$")

_URL_ATTRS = frozenset({
    "href", "src", "action", "background", "poster", "cite", "longdesc",
    "lowsrc", "dynsrc", "codebase", "data", "usemap", "xlink:href", "ping",
    "manifest", "profile", "classid", "archive", "icon",
})
_DROP_ATTRS = frozenset({"srcdoc", "formaction", "srcset", "imagesrcset",
                         "http-equiv", "is"})
_SAFE_SCHEMES = ("http", "https", "mailto", "tel", "cid")
_DATA_IMAGE = re.compile(r"^data:image/(?:png|gif|jpe?g|webp|bmp);", re.I)
# Characters a browser ignores inside a URL scheme ("java\tscript:").
_IGNORED_IN_URL = re.compile(r"[\x00-\x20\x7f-\x9f]+")
_STYLE_BAD = re.compile(
    r"expression\s*\(|javascript\s*:|vbscript\s*:|behavior\s*:|-moz-binding"
    r"|@import|url\s*\(\s*['\"]?\s*(?!https?:|cid:|data:image/)[a-z0-9+.-]+:",
    re.I)
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})\s?|\\(.)")


def _url_ok(value: str, attr: str) -> bool:
    v = _IGNORED_IN_URL.sub("", html.unescape(value)).lower()
    if ":" not in v.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]:
        return True                                  # relative URL, #anchor
    scheme = v.split(":", 1)[0]
    if scheme in _SAFE_SCHEMES:
        return True
    return attr in ("src", "background") and bool(_DATA_IMAGE.match(v))


def _style_ok(value: str) -> bool:
    # CSS escapes ("\6a avascript:") spell the same words: decode them first.
    def dec(m):
        if m.group(1):
            try:
                return chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return m.group(2)
    decoded = _CSS_ESCAPE.sub(dec, value)
    decoded = re.sub(r"/\*.*?\*/", "", decoded, flags=re.S)
    return not _STYLE_BAD.search(decoded)


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip: list[str] = []      # stack of dropped elements we're inside

    # -- tags --------------------------------------------------------------
    def _open(self, tag: str, attrs, self_closing: bool) -> None:
        tag = tag.lower()
        if tag == "body" and "head" in self._skip:
            # <head> left unclosed: a browser ends it at <body>, so do we
            # (otherwise the whole email would be dropped with it).
            while self._skip and self._skip.pop() != "head":
                pass
        if self._skip:
            if tag in _DROP_WITH_CONTENT and not self_closing and tag not in _VOID:
                self._skip.append(tag)
            return
        if tag in _DROP_WITH_CONTENT:
            if not self_closing and tag not in _VOID:
                self._skip.append(tag)
            return
        if tag in _DROP_TAG_ONLY or not _TAG_NAME.match(tag):
            return
        parts = [tag]
        for name, value in attrs:
            name = (name or "").lower()
            if not _ATTR_NAME.match(name) or name.startswith("on"):
                continue
            if name in _DROP_ATTRS:
                continue
            value = "" if value is None else value
            if name in _URL_ATTRS and not _url_ok(value, name):
                continue
            if name == "style" and not _style_ok(value):
                continue
            parts.append(f'{name}="{html.escape(value, quote=True)}"')
        end = " /" if self_closing and tag in _VOID else ""
        self.out.append("<" + " ".join(parts) + end + ">")

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, True)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            elif tag in self._skip:
                # Unbalanced markup: close back to that element.
                while self._skip and self._skip.pop() != tag:
                    pass
            return
        if (tag in _DROP_WITH_CONTENT or tag in _DROP_TAG_ONLY
                or tag in _VOID or not _TAG_NAME.match(tag)):
            return
        self.out.append(f"</{tag}>")

    # -- content -----------------------------------------------------------
    def handle_data(self, data):
        if not self._skip:
            self.out.append(html.escape(data, quote=False))

    # Comments (including Outlook's <!--[if mso]> blocks), doctype, processing
    # instructions and CDATA are not content: drop them.
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


def sanitize(raw: str) -> str:
    """Safe HTML for *raw* (an untrusted email body)."""
    if not raw:
        return ""
    p = _Sanitizer()
    try:
        p.feed(raw)
        p.close()
    except Exception:
        # The parser doesn't raise on bad HTML, but if anything goes wrong
        # the email is quoted as plain text rather than unchecked.
        return html.escape(re.sub(r"<[^>]*>", " ", raw), quote=False)
    return "".join(p.out)
