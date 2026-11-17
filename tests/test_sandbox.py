"""Chromium's sandbox must stay on in every build.

It is what contains a renderer exploit from a hostile email. It was once
switched off in frozen builds over a crash the GPU flags actually caused; this
test stops that switch from coming back.
"""
import io
import pathlib
import tokenize

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "emailclient"
FORBIDDEN = ("QTWEBENGINE_DISABLE_SANDBOX", "--no-sandbox")


def _code_strings(path: pathlib.Path):
    """String literals in the file's code; comments don't count."""
    src = path.read_text(encoding="utf-8")
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.STRING:
            yield tok.start[0], tok.string


def test_no_code_disables_the_chromium_sandbox():
    hits = [
        f"{path.relative_to(APP_DIR.parent)}:{line}: {text}"
        for path in sorted(APP_DIR.rglob("*.py"))
        for line, text in _code_strings(path)
        if any(f in text for f in FORBIDDEN)
    ]
    assert not hits, "Chromium sandbox disabled in:\n" + "\n".join(hits)
