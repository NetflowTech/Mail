"""Word-style tables for the compose editor.

The grid picker (hover to choose rows × columns), table insertion with real
cell borders that survive the trip to the recipient's mail client, the Table
context-menu actions, Tab navigation between cells, and the clean-up pass a
pasted Word / Excel / web table needs to look right inside QTextEdit.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QKeyEvent, QPainter, QPen,
                           QTextCursor, QTextFrameFormat, QTextLength,
                           QTextTable, QTextTableCellFormat, QTextTableFormat)
from PySide6.QtWidgets import (QColorDialog, QLabel, QMenu, QVBoxLayout,
                               QWidget, QWidgetAction)

from . import theme

GRID_COLS = 10
GRID_ROWS = 8
_CELL = 20          # picker cell size in px
_GAP = 3


def border_color() -> str:
    """Black borders, like Word — unless the editor is themed dark, where
    black would vanish; a mid grey reads on both dark and the white the
    recipient sees."""
    bg = QColor(theme.BG or "#ffffff")
    return "#000000" if bg.lightness() > 128 else "#9a9a9a"


def cell_format(color: str | None = None,
                padding: float = 4.0) -> QTextTableCellFormat:
    fmt = QTextTableCellFormat()
    fmt.setBorder(1.0)
    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    fmt.setBorderBrush(QBrush(QColor(color or border_color())))
    fmt.setPadding(padding)
    return fmt


def table_format() -> QTextTableFormat:
    fmt = QTextTableFormat()
    fmt.setBorder(0)
    fmt.setCellSpacing(0)
    fmt.setCellPadding(4)
    fmt.setBorderCollapse(True)
    fmt.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
    return fmt


def insert_table(editor, rows: int, cols: int) -> QTextTable:
    """Insert a rows × cols bordered table at the cursor and put the caret in
    its first cell."""
    rows, cols = max(1, rows), max(1, cols)
    cursor = editor.textCursor()
    cursor.beginEditBlock()
    table = cursor.insertTable(rows, cols, table_format())
    fmt = cell_format()
    for r in range(rows):
        for c in range(cols):
            table.cellAt(r, c).setFormat(fmt)
    cursor.endEditBlock()
    editor.setTextCursor(table.cellAt(0, 0).firstCursorPosition())
    editor.setFocus()
    return table


# ----- pasted tables ------------------------------------------------------

def tables_in_range(document, start: int, end: int) -> list[QTextTable]:
    """Every table whose start lies in [start, end], nested ones included."""
    found: list[QTextTable] = []

    def walk(frame):
        for child in frame.childFrames():
            if isinstance(child, QTextTable) and start <= child.firstPosition() <= end:
                found.append(child)
            walk(child)

    walk(document.rootFrame())
    return found


def fix_pasted_tables(document, start: int, end: int) -> int:
    """Make tables that arrived through insertHtml look like they did in the
    source. Word and Excel give cells a border *style* and colour, but Qt
    reads no width for them, so it draws nothing. Give every styled side a
    1px width. Returns the number of tables touched."""
    tables = tables_in_range(document, start, end)
    for table in tables:
        for r in range(table.rows()):
            for c in range(table.columns()):
                cell = table.cellAt(r, c)
                if cell.row() != r or cell.column() != c:
                    continue   # part of a merged span; its origin handles it
                fmt = cell.format().toTableCellFormat()
                changed = False
                none = QTextFrameFormat.BorderStyle.BorderStyle_None
                for side in ("Left", "Right", "Top", "Bottom"):
                    style = getattr(fmt, f"{side.lower()}BorderStyle")()
                    width = getattr(fmt, f"{side.lower()}Border")()
                    if style == none:
                        continue
                    if not width:
                        getattr(fmt, f"set{side}Border")(1.0)
                        changed = True
                    # Word's "windowtext" colour comes through as no colour
                    # at all; give the side a real one so it draws and sends.
                    brush = getattr(fmt, f"{side.lower()}BorderBrush")()
                    if not brush.color().isValid():
                        getattr(fmt, f"set{side}BorderBrush")(
                            QBrush(QColor(border_color())))
                        changed = True
                if changed:
                    cell.setFormat(fmt)
    return len(tables)


def has_table(html: str) -> bool:
    return "<table" in (html or "").lower()


# ----- context-menu actions ------------------------------------------------

def _copy_row_formats(table: QTextTable, src_row: int, dst_row: int) -> None:
    for c in range(table.columns()):
        src = table.cellAt(src_row, c)
        dst = table.cellAt(dst_row, c)
        if src.isValid() and dst.isValid():
            dst.setFormat(src.format().toTableCellFormat())


def _copy_col_formats(table: QTextTable, src_col: int, dst_col: int) -> None:
    for r in range(table.rows()):
        src = table.cellAt(r, src_col)
        dst = table.cellAt(r, dst_col)
        if src.isValid() and dst.isValid():
            dst.setFormat(src.format().toTableCellFormat())


def insert_row(editor, table: QTextTable, above: bool) -> None:
    cell = table.cellAt(editor.textCursor())
    if not cell.isValid():
        return
    row = cell.row()
    new = row if above else row + cell.rowSpan()
    table.insertRows(new, 1)
    _copy_row_formats(table, row + (1 if above else 0), new)
    editor.setTextCursor(table.cellAt(new, cell.column()).firstCursorPosition())


def insert_column(editor, table: QTextTable, left: bool) -> None:
    cell = table.cellAt(editor.textCursor())
    if not cell.isValid():
        return
    col = cell.column()
    new = col if left else col + cell.columnSpan()
    table.insertColumns(new, 1)
    _copy_col_formats(table, col + (1 if left else 0), new)
    editor.setTextCursor(table.cellAt(cell.row(), new).firstCursorPosition())


def delete_table(editor, table: QTextTable) -> None:
    cursor = QTextCursor(editor.document())
    cursor.setPosition(table.firstPosition() - 1)
    cursor.setPosition(table.lastPosition() + 1, QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    editor.setTextCursor(cursor)


def delete_row(editor, table: QTextTable) -> None:
    cell = table.cellAt(editor.textCursor())
    if not cell.isValid():
        return
    if table.rows() <= cell.rowSpan():
        delete_table(editor, table)
        return
    table.removeRows(cell.row(), cell.rowSpan())


def delete_column(editor, table: QTextTable) -> None:
    cell = table.cellAt(editor.textCursor())
    if not cell.isValid():
        return
    if table.columns() <= cell.columnSpan():
        delete_table(editor, table)
        return
    table.removeColumns(cell.column(), cell.columnSpan())


def merge_cells(editor, table: QTextTable) -> None:
    cursor = editor.textCursor()
    if cursor.hasComplexSelection():
        table.mergeCells(cursor)


def split_cell(editor, table: QTextTable) -> None:
    cell = table.cellAt(editor.textCursor())
    if cell.isValid() and (cell.rowSpan() > 1 or cell.columnSpan() > 1):
        table.splitCell(cell.row(), cell.column(), 1, 1)


def shade_cells(editor, table: QTextTable) -> None:
    color = QColorDialog.getColor(parent=editor)
    if not color.isValid():
        return
    cursor = editor.textCursor()
    cells = []
    if cursor.hasComplexSelection():
        r0, nr, c0, nc = cursor.selectedTableCells()
        for r in range(r0, r0 + nr):
            for c in range(c0, c0 + nc):
                cells.append(table.cellAt(r, c))
    else:
        cells.append(table.cellAt(cursor))
    for cell in cells:
        if cell.isValid():
            fmt = cell.format().toTableCellFormat()
            fmt.setBackground(QBrush(color))
            cell.setFormat(fmt)


def toggle_borders(editor, table: QTextTable) -> None:
    """Switch every cell border on or off (off is what a layout table wants)."""
    first = table.cellAt(0, 0).format().toTableCellFormat()
    turn_on = first.leftBorderStyle() == QTextFrameFormat.BorderStyle.BorderStyle_None
    for r in range(table.rows()):
        for c in range(table.columns()):
            cell = table.cellAt(r, c)
            if cell.row() != r or cell.column() != c:
                continue
            fmt = cell.format().toTableCellFormat()
            if turn_on:
                fmt.setBorder(1.0)
                fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                fmt.setBorderBrush(QBrush(QColor(border_color())))
            else:
                fmt.setBorder(0.0)
                fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_None)
            cell.setFormat(fmt)


def add_table_actions(menu: QMenu, editor) -> bool:
    """Append a "Table" submenu to *menu* when the cursor is inside a table.
    Returns whether anything was added."""
    cursor = editor.textCursor()
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    menu.addSeparator()
    sub = menu.addMenu("Table")
    sub.addAction("Insert row above",
                  lambda: insert_row(editor, table, above=True))
    sub.addAction("Insert row below",
                  lambda: insert_row(editor, table, above=False))
    sub.addAction("Insert column left",
                  lambda: insert_column(editor, table, left=True))
    sub.addAction("Insert column right",
                  lambda: insert_column(editor, table, left=False))
    sub.addSeparator()
    sub.addAction("Delete row", lambda: delete_row(editor, table))
    sub.addAction("Delete column", lambda: delete_column(editor, table))
    sub.addAction("Delete table", lambda: delete_table(editor, table))
    sub.addSeparator()
    merge = sub.addAction("Merge cells", lambda: merge_cells(editor, table))
    merge.setEnabled(cursor.hasComplexSelection())
    split = sub.addAction("Split cell", lambda: split_cell(editor, table))
    split.setEnabled(cell.isValid()
                     and (cell.rowSpan() > 1 or cell.columnSpan() > 1))
    sub.addSeparator()
    sub.addAction("Cell shading…", lambda: shade_cells(editor, table))
    sub.addAction("Toggle borders", lambda: toggle_borders(editor, table))
    return True


# ----- keyboard -------------------------------------------------------------

def handle_tab(editor, event: QKeyEvent) -> bool:
    """Tab / Shift+Tab inside a table move between cells, the way Word does;
    Tab in the last cell adds a row. Returns True when the key was used."""
    if event.key() not in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
        return False
    cursor = editor.textCursor()
    table = cursor.currentTable()
    if table is None:
        return False
    cell = table.cellAt(cursor)
    if not cell.isValid():
        return False
    backwards = (event.key() == Qt.Key.Key_Backtab
                 or event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
    rows, cols = table.rows(), table.columns()
    r, c = cell.row(), cell.column()
    if backwards:
        if r == 0 and c == 0:
            return True
        c -= 1
        if c < 0:
            c = cols - 1
            r -= 1
    else:
        c += cell.columnSpan()
        if c >= cols:
            c = 0
            r += cell.rowSpan()
            if r >= rows:
                table.appendRows(1)
                _copy_row_formats(table, rows - 1, rows)
    target = table.cellAt(r, c)
    if not target.isValid():
        return True
    new = target.firstCursorPosition()
    new.setPosition(target.lastCursorPosition().position(),
                    QTextCursor.MoveMode.KeepAnchor)
    editor.setTextCursor(new)
    return True


# ----- the grid picker -----------------------------------------------------

class TableGrid(QWidget):
    """A GRID_COLS × GRID_ROWS field of little squares; hovering highlights
    the rows × columns the click will insert."""

    hovered = Signal(int, int)   # rows, cols (0, 0 when off the grid)
    picked = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self._hover = (0, 0)
        w = GRID_COLS * (_CELL + _GAP) + _GAP
        h = GRID_ROWS * (_CELL + _GAP) + _GAP
        self.setFixedSize(w, h)

    def sizeHint(self) -> QSize:
        return self.size()

    def _cell_at(self, pos) -> tuple[int, int]:
        col = int(pos.x() - _GAP) // (_CELL + _GAP) + 1
        row = int(pos.y() - _GAP) // (_CELL + _GAP) + 1
        if 1 <= col <= GRID_COLS and 1 <= row <= GRID_ROWS:
            return row, col
        return 0, 0

    def mouseMoveEvent(self, event):
        hover = self._cell_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.hovered.emit(*hover)
            self.update()

    def leaveEvent(self, event):
        self._hover = (0, 0)
        self.hovered.emit(0, 0)
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        rows, cols = self._cell_at(event.position())
        if rows and cols:
            self.picked.emit(rows, cols)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rows, cols = self._hover
        line = QColor(theme.BORDER or "#c8c8c8")
        fill = QColor(theme.PANEL or "#f4f4f4")
        hot = QColor(theme.GOLD or "#c9a227")
        hot_fill = QColor(hot)
        hot_fill.setAlpha(70)
        for r in range(GRID_ROWS):
            for c in range(GRID_COLS):
                x = _GAP + c * (_CELL + _GAP)
                y = _GAP + r * (_CELL + _GAP)
                rect = QRectF(x, y, _CELL, _CELL)
                lit = r < rows and c < cols
                p.setPen(QPen(hot if lit else line, 1))
                p.setBrush(QBrush(hot_fill if lit else fill))
                p.drawRoundedRect(rect, 2, 2)
        p.end()


class TableMenu(QMenu):
    """Popup under the toolbar's Table button: the grid plus a caption."""

    picked = Signal(int, int)   # rows, cols

    def __init__(self, parent=None):
        super().__init__(parent)
        host = QWidget(self)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        self._grid = TableGrid(host)
        self._label = QLabel("Insert table")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._grid)
        lay.addWidget(self._label)
        action = QWidgetAction(self)
        action.setDefaultWidget(host)
        self.addAction(action)
        self._grid.hovered.connect(self._on_hover)
        self._grid.picked.connect(self._on_pick)
        self.aboutToShow.connect(lambda: self._on_hover(0, 0))

    def _on_hover(self, rows: int, cols: int):
        self._label.setText(f"{cols} × {rows} table" if rows and cols
                            else "Insert table")

    def _on_pick(self, rows: int, cols: int):
        self.close()
        self.picked.emit(rows, cols)
