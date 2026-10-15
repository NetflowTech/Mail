"""Spell checking for the message editor.

Pure logic (no Qt) so it is unit-testable and can be reused. The dictionary is
loaded lazily on first use — a compose window must never wait for it — and the
words the user adds are kept in a small file next to the mail database.
"""
from __future__ import annotations

import logging
import re
import threading

from .config import data_dir

log = logging.getLogger(__name__)

# Words are what a person would consider a word: letters plus the apostrophes
# and hyphens that live inside them ("don't", "well-known"). Numbers, URLs and
# addresses are handled by _is_checkable below.
WORD_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*")

MAX_SUGGESTIONS = 5
_MIN_LENGTH = 3          # "ok", "hi", initials — not worth flagging

_lock = threading.Lock()
_checker = None          # lazily built SpellChecker
_user_words: set[str] | None = None


def user_dict_path():
    return data_dir() / "user_dictionary.txt"


def _load_user_words() -> set[str]:
    global _user_words
    if _user_words is None:
        words = set()
        try:
            with open(user_dict_path(), encoding="utf-8") as fh:
                words = {line.strip().lower() for line in fh if line.strip()}
        except OSError:
            pass                     # no personal dictionary yet
        _user_words = words
    return _user_words


def add_word(word: str) -> None:
    """Remember *word* as correct, for this and every future session."""
    word = (word or "").strip()
    if not word:
        return
    words = _load_user_words()
    if word.lower() in words:
        return
    words.add(word.lower())
    try:
        path = user_dict_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(word + "\n")
    except OSError as exc:
        log.warning("spell: could not save '%s' to the user dictionary: %s",
                    word, exc)


def known_words() -> set[str]:
    """The user's own dictionary (lowercased)."""
    return set(_load_user_words())


def _get_checker():
    """The shared SpellChecker, built on first use. None if unavailable."""
    global _checker
    if _checker is None:
        with _lock:
            if _checker is None:
                try:
                    from spellchecker import SpellChecker
                    _checker = SpellChecker(distance=2)
                except Exception:  # noqa: BLE001
                    log.warning("spell: dictionary unavailable — spell check off")
                    _checker = False        # remember the failure, don't retry
    return _checker or None


def available() -> bool:
    """False when the dictionary could not be loaded (spell check just stays
    off rather than breaking the editor)."""
    return _get_checker() is not None


def _is_checkable(word: str) -> bool:
    """Skip what a spell checker has no business flagging."""
    if len(word) < _MIN_LENGTH:
        return False
    if any(ch.isdigit() for ch in word):
        return False
    if word.isupper():               # acronyms: IMAP, SMTP, VAT
        return False
    # MixedCase in the middle of a word is a product name (AMail, iPhone)
    if any(ch.isupper() for ch in word[1:]):
        return False
    return True


def is_misspelled(word: str) -> bool:
    """True when *word* is worth underlining."""
    word = (word or "").strip("'’-")
    if not word or not _is_checkable(word):
        return False
    if word.lower() in _load_user_words():
        return False
    checker = _get_checker()
    if checker is None:
        return False
    return bool(checker.unknown([word.lower()]))


def suggestions(word: str, limit: int = MAX_SUGGESTIONS) -> list[str]:
    """Best corrections for *word*, most likely first, matching its case."""
    checker = _get_checker()
    word = (word or "").strip("'’-")
    if checker is None or not word:
        return []
    try:
        cands = checker.candidates(word.lower()) or set()
    except Exception:  # noqa: BLE001
        return []
    cands = {c for c in cands if c != word.lower()}
    ranked = sorted(cands, key=lambda w: -checker.word_frequency[w])[:limit]
    if word[:1].isupper():
        ranked = [w.capitalize() for w in ranked]
    return ranked


def misspelled_spans(text: str) -> list[tuple[int, int]]:
    """(start, length) of every misspelled word in *text* — what the editor
    underlines."""
    spans = []
    for match in WORD_RE.finditer(text or ""):
        if is_misspelled(match.group(0)):
            spans.append((match.start(), match.end() - match.start()))
    return spans


def reset_cache() -> None:
    """Drop the cached dictionary/user words (tests, and after an edit)."""
    global _checker, _user_words
    _checker = None
    _user_words = None
