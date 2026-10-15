"""Red-squiggle spell checking inside the message editor.

A QSyntaxHighlighter marks unknown words as you type; right-clicking one offers
corrections, "Add to dictionary" and "Ignore once". The dictionary itself lives
in emailclient/spell.py — this module is only the Qt wiring.
"""
from __future__ import annotations

from PySide6.QtGui import QSyntaxHighlighter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QMenu

from .. import spell


class SpellHighlighter(QSyntaxHighlighter):
    """Underlines misspelled words in a QTextDocument."""

    def __init__(self, document):
        super().__init__(document)
        self._enabled = True
        self._ignored: set[str] = set()      # "ignore once" words, this window
        fmt = QTextCharFormat()
        fmt.setUnderlineStyle(
            QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
        fmt.setUnderlineColor("#D93025")     # the usual red squiggle
        self._format = fmt

    def set_enabled(self, on: bool) -> None:
        if on == self._enabled:
            return
        self._enabled = on
        self.rehighlight()                   # paint or clear the squiggles

    def is_enabled(self) -> bool:
        return self._enabled

    def ignore_word(self, word: str) -> None:
        """Stop flagging *word* in this message only."""
        self._ignored.add((word or "").lower())
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        if not self._enabled or not spell.available():
            return
        for match in spell.WORD_RE.finditer(text or ""):
            word = match.group(0)
            if word.lower() in self._ignored:
                continue
            if spell.is_misspelled(word):
                self.setFormat(match.start(), len(word), self._format)


def word_at(cursor: QTextCursor) -> tuple[str, int, int] | None:
    """The word under *cursor* as (word, start, end) document positions.

    Qt's own WordUnderCursor grabs punctuation too, so the text is re-scanned
    with the same word rule the highlighter uses — the menu must act on exactly
    what got underlined.
    """
    block = cursor.block()
    if not block.isValid():
        return None
    offset = cursor.position() - block.position()
    for match in spell.WORD_RE.finditer(block.text()):
        if match.start() <= offset <= match.end():
            return (match.group(0),
                    block.position() + match.start(),
                    block.position() + match.end())
    return None


def build_menu(editor, highlighter: SpellHighlighter, position) -> QMenu:
    """The editor's context menu for a right-click at *position* (widget
    coordinates), with spelling entries on top when it landed on a typo."""
    return menu_for_cursor(editor, highlighter,
                           editor.cursorForPosition(position))


def menu_for_cursor(editor, highlighter: SpellHighlighter,
                    cursor: QTextCursor) -> QMenu:
    """Same menu, built for an explicit cursor."""
    menu = editor.createStandardContextMenu()
    if not highlighter.is_enabled() or not spell.available():
        return menu
    found = word_at(cursor)
    if found is None:
        return menu
    word, start, end = found
    if word.lower() in highlighter._ignored or not spell.is_misspelled(word):
        return menu

    first = menu.actions()[0] if menu.actions() else None
    spell_actions = []
    for suggestion in spell.suggestions(word):
        act = menu.addAction(suggestion)
        act.triggered.connect(
            lambda _=False, s=suggestion: _replace(editor, start, end, s))
        spell_actions.append(act)
    if not spell_actions:
        none_act = menu.addAction("(no suggestions)")
        none_act.setEnabled(False)
        spell_actions.append(none_act)
    add_act = menu.addAction(f'➕ Add "{word}" to dictionary')
    add_act.triggered.connect(lambda: _learn(word, highlighter))
    ignore_act = menu.addAction("Ignore once")
    ignore_act.triggered.connect(lambda: highlighter.ignore_word(word))
    sep = menu.addSeparator()
    spell_actions += [add_act, ignore_act, sep]
    # Put them at the top, above Cut/Copy/Paste.
    if first is not None:
        menu.insertActions(first, spell_actions)
    return menu


def _replace(editor, start: int, end: int, word: str) -> None:
    cursor = editor.textCursor()
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    cursor.insertText(word)


def _learn(word: str, highlighter: SpellHighlighter) -> None:
    spell.add_word(word)
    highlighter.rehighlight()
