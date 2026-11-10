"""Outlook / Word "Format Painter" for the compose editor.

Pick up the look of the text under the caret (font, size, bold, colour,
highlight, alignment …), then brush it onto whatever is selected next.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import (QCursor, QPixmap, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextFormat)

from . import editor_icons

# Everything that makes text *look* a certain way. Copying property by
# property (rather than the whole format) keeps links, images and the
# spell-checker's own marks out of the brush.
_COPIED_PROPS = (
    QTextFormat.Property.FontFamilies,
    QTextFormat.Property.FontPointSize,
    QTextFormat.Property.FontPixelSize,
    QTextFormat.Property.FontSizeAdjustment,
    QTextFormat.Property.FontWeight,
    QTextFormat.Property.FontItalic,
    QTextFormat.Property.FontUnderline,
    QTextFormat.Property.TextUnderlineStyle,
    QTextFormat.Property.TextUnderlineColor,
    QTextFormat.Property.FontStrikeOut,
    QTextFormat.Property.FontOverline,
    QTextFormat.Property.FontCapitalization,
    QTextFormat.Property.FontLetterSpacing,
    QTextFormat.Property.FontWordSpacing,
    QTextFormat.Property.FontStretch,
    QTextFormat.Property.ForegroundBrush,
    QTextFormat.Property.BackgroundBrush,
    QTextFormat.Property.TextVerticalAlignment,
)


def capture(editor) -> tuple[QTextCharFormat, QTextBlockFormat]:
    """The look at the caret (or at the start of the selection)."""
    cursor = editor.textCursor()
    if cursor.hasSelection():
        probe = QTextCursor(editor.document())
        # The char format "at" a position is that of the character before
        # it, so step one past the selection start to read the first
        # selected character.
        probe.setPosition(min(cursor.selectionStart() + 1,
                              editor.document().characterCount() - 1))
        src = probe.charFormat()
        block_src = probe.blockFormat()
    else:
        src = editor.currentCharFormat()
        block_src = cursor.blockFormat()
    if src.isImageFormat():
        src = QTextCharFormat()
    char_fmt = QTextCharFormat()
    for prop in _COPIED_PROPS:
        if src.hasProperty(prop):
            char_fmt.setProperty(prop, src.property(prop))
    block_fmt = QTextBlockFormat()
    block_fmt.setAlignment(block_src.alignment())
    return char_fmt, block_fmt


def _text_ranges(cursor: QTextCursor) -> list[tuple[int, int]]:
    """[start, end) spans of the selection that are text — images skipped."""
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    doc = cursor.document()
    ranges: list[tuple[int, int]] = []
    block = doc.findBlock(start)
    while block.isValid() and block.position() < end:
        it = block.begin()
        while not it.atEnd():
            frag = it.fragment()
            if frag.isValid():
                s = max(frag.position(), start)
                e = min(frag.position() + frag.length(), end)
                if s < e and not frag.charFormat().isImageFormat():
                    ranges.append((s, e))
            it += 1
        block = block.next()
    return ranges


def apply(editor, char_fmt: QTextCharFormat,
          block_fmt: QTextBlockFormat | None = None) -> bool:
    """Replace the look of the selected text with the captured one. Returns
    False when nothing was selected."""
    cursor = editor.textCursor()
    if not cursor.hasSelection():
        return False
    cursor.beginEditBlock()
    for s, e in _text_ranges(cursor):
        c = QTextCursor(editor.document())
        c.setPosition(s)
        c.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
        # Replace, don't merge: the point is to make the target *match*,
        # so a bold target loses its bold when the source is regular.
        c.setCharFormat(char_fmt)
    if block_fmt is not None:
        cursor.mergeBlockFormat(block_fmt)
    cursor.endEditBlock()
    editor.setTextCursor(cursor)
    return True


def brush_cursor() -> QCursor:
    """A paintbrush mouse cursor, shown while the painter is armed."""
    pix: QPixmap = editor_icons.icon("brush").pixmap(22, 22)
    return QCursor(pix, 2, 20)
