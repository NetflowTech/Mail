"""Quoted emails can't carry scripts back out to the recipients."""
from __future__ import annotations

import pytest

from emailclient.html_sanitize import sanitize
from emailclient.ui.compose import sanitize_email_html


def _bad(out: str) -> bool:
    low = out.lower()
    return any(x in low for x in ("<script", "javascript:", "vbscript:",
                                  "onerror", "onclick", "onload", "<iframe",
                                  "<object", "<embed", "<svg", "srcdoc",
                                  "expression(", "<form", "<base", "<meta"))


@pytest.mark.parametrize("attack", [
    "<a href=javascript:alert(1)>x</a>",                      # unquoted
    "<a href='javascript:alert(1)'>x</a>",
    '<a href="  JaVaScRiPt:alert(1)">x</a>',
    '<a href="java\tscript:alert(1)">x</a>',                  # browser ignores tab
    '<a href="&#106;avascript:alert(1)">x</a>',               # entity-encoded
    '<a href="&#x6A;avascript&colon;alert(1)">x</a>',
    "<scr<script>ipt>alert(1)</script>",                      # nested rebuild
    "<<script>script>alert(1)<</script>/script>",
    "<img src=x onerror=alert(1)>",
    "<img src=x OnError='alert(1)'>",
    '<img src="x" /onerror=alert(1)>',
    "<body onload=alert(1)>hi</body>",
    "<iframe srcdoc='<script>alert(1)</script>'></iframe>",
    "<object data=javascript:alert(1)></object>",
    "<svg><script>alert(1)</script></svg>",
    "<svg onload=alert(1)>",
    "<math><mtext><script>alert(1)</script></mtext></math>",
    '<form action="javascript:alert(1)"><button>go</button></form>',
    '<button formaction="javascript:alert(1)">x</button>',
    '<div style="width: expression(alert(1))">x</div>',
    '<div style="background:url(javascript:alert(1))">x</div>',
    '<div style="background:url(\\6a avascript:alert(1))">x</div>',
    '<base href="javascript:alert(1)//">',
    '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">',
    '<a href="vbscript:msgbox(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<img src="data:text/html;base64,PHNjcmlwdD4=">',
    "<!--<script>alert(1)</script>-->",
    "<style>body{background:url(javascript:alert(1))}</style>",
    "<scrIPT>alert(1)</scRipt>",
    "<script\n>alert(1)</script\n>",
])
def test_attack_removed(attack):
    out = sanitize(attack)
    assert not _bad(out), out


def test_ordinary_email_kept():
    src = ('<table width="600" style="color:#333"><tr><td>'
           '<p>Hello <b>Alice</b> &amp; Bob</p>'
           '<a href="https://example.com/?a=1&amp;b=2">link</a>'
           '<img src="cid:logo123" alt="logo">'
           '<img src="data:image/png;base64,iVBORw0KGgo=">'
           '<a href="mailto:x@example.com">mail</a>'
           '</td></tr></table>')
    out = sanitize(src)
    assert '<table width="600" style="color:#333">' in out
    assert "Hello <b>Alice</b> &amp; Bob" in out
    assert 'href="https://example.com/?a=1&amp;b=2"' in out
    assert 'src="cid:logo123"' in out
    assert 'src="data:image/png;base64,iVBORw0KGgo="' in out
    assert 'href="mailto:x@example.com"' in out


def test_text_that_looks_like_markup_stays_text():
    assert sanitize("1 < 2 and 3 > 2") == "1 &lt; 2 and 3 &gt; 2"


def test_unclosed_head_does_not_eat_the_email():
    out = sanitize("<html><head><title>t</title><body><p>Body text</p>")
    assert "Body text" in out and "<title" not in out


def test_relative_and_anchor_links_kept():
    assert 'href="#top"' in sanitize('<a href="#top">x</a>')
    assert 'href="page.html"' in sanitize('<a href="page.html">x</a>')


def test_compose_uses_it():
    assert not _bad(sanitize_email_html("<a href=javascript:alert(1)>x</a>"))
