from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

from . import theme

PRESET_COLORS = ["#8A6D1F", "#B33A3A", "#2E7D52", "#2B5DA8",
                 "#7A4FA3", "#C26A1B", "#3A8FA3", "#6B7FA3"]

PRESET_ICONS = ["📁", "💼", "🏦", "🛒", "🏠", "✈️", "⚙️", "📈",
                "🎯", "🛠", "🌐", "⭐", "📧", "🔥", "💎", "🎨",
                "🏢", "📦", "🔔", "🧾", "💰", "🖥️", "📱", "🍀"]


class IconGrid(QWidget):
    """Grid of selectable emoji buttons for picking a group icon.

    NOTE: every button stylesheet explicitly resets `padding: 0` — the
    app-wide QSS gives QPushButton `padding: 5px 14px`, which on a small
    fixed-size button squeezes the emoji text out entirely (the old picker
    rendered as blank swatches because of exactly this).
    """

    icon_changed = Signal(str)
    COLUMNS = 6

    def __init__(self, selected: str = "📁", parent=None):
        super().__init__(parent)
        self._icon = selected if selected in PRESET_ICONS else PRESET_ICONS[0]
        self._buttons: list[QPushButton] = []
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        for i, ic in enumerate(PRESET_ICONS):
            btn = QPushButton(ic)
            btn.setFixedSize(36, 36)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("icon_value", ic)
            btn.clicked.connect(lambda _=False, i=ic: self._select(i))
            grid.addWidget(btn, i // self.COLUMNS, i % self.COLUMNS)
            self._buttons.append(btn)
        grid.setColumnStretch(self.COLUMNS, 1)
        self._update()

    def _select(self, icon: str):
        self._icon = icon
        self._update()
        self.icon_changed.emit(icon)

    def _update(self):
        for btn in self._buttons:
            ic = btn.property("icon_value")
            if ic == self._icon:
                state = (f"background: {theme.GOLD_SOFT}; "
                         f"border: 2px solid {theme.GOLD_DEEP};")
                hover = f"border: 2px solid {theme.GOLD_DEEP};"
            else:
                state = ("background: transparent; "
                         "border: 1px solid transparent;")
                hover = f"border: 1px solid {theme.GOLD};"
            btn.setStyleSheet(
                f"QPushButton {{ font-size: 18px; padding: 0; "
                f"border-radius: 8px; {state} }} "
                f"QPushButton:hover {{ {hover} }}")

    def icon(self) -> str:
        return self._icon


class IconPickerDialog(QDialog):
    """Small dialog wrapping IconGrid — used by 'Change icon…' on groups."""

    def __init__(self, current: str = "📁", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Change Icon")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Group icon:"))
        self.icon_row = IconGrid(current)
        layout.addWidget(self.icon_row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def icon(self) -> str:
        return self.icon_row.icon()


class ColorPickerDialog(QDialog):
    """Pick a group's color on its own — teams working different markets tell
    their groups apart by color at a glance, so it must be changeable after the
    group was created, not only while creating it.

    The presets cover the common cases; "Custom…" opens the full color wheel.
    """

    def __init__(self, current: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Group color")
        self._color = current or PRESET_COLORS[0]
        self._swatches: list[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick a color for this group:"))
        row = QHBoxLayout()
        row.setSpacing(6)
        for c in PRESET_COLORS:
            btn = QPushButton()
            btn.setFixedSize(28, 28)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(c)
            btn.setProperty("swatch_color", c)
            btn.clicked.connect(lambda _=False, col=c: self._select(col))
            row.addWidget(btn)
            self._swatches.append(btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.preview = QLabel()
        self.preview.setMinimumHeight(28)
        layout.addWidget(self.preview)

        custom = QPushButton("🎨 Custom…")
        custom.setCursor(Qt.CursorShape.PointingHandCursor)
        custom.clicked.connect(self._pick_custom)
        layout.addWidget(custom)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    def _select(self, color: str):
        self._color = color
        self._refresh()

    def _pick_custom(self):
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        chosen = QColorDialog.getColor(QColor(self._color), self,
                                       "Group color")
        if chosen.isValid():
            self._select(chosen.name())

    def _refresh(self):
        for btn in self._swatches:
            c = btn.property("swatch_color")
            border = ("border: 2px solid #2B2823;" if c == self._color
                      else "border: 1px solid transparent;")
            btn.setStyleSheet(
                f"QPushButton {{ background: {c}; {border} border-radius: 4px; }}")
        self.preview.setText(f"  Selected: {self._color}")
        self.preview.setStyleSheet(
            f"QLabel {{ color: {self._color}; font-weight: 600; }}")

    def color(self) -> str:
        return self._color


class NewGroupDialog(QDialog):
    """Dialog for creating a new account group: choose a name + icon + color."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New Group")
        self._color = PRESET_COLORS[0]  # gold preselected
        self._swatches: list[QPushButton] = []

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Group name:"))
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("e.g. Finance")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Icon:"))
        self.icon_row = IconGrid()
        layout.addWidget(self.icon_row)

        layout.addWidget(QLabel("Color:"))
        row = QHBoxLayout()
        row.setSpacing(6)
        for c in PRESET_COLORS:
            btn = QPushButton()
            btn.setFixedSize(24, 24)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setProperty("swatch_color", c)
            btn.clicked.connect(lambda _=False, col=c: self._select_color(col))
            row.addWidget(btn)
            self._swatches.append(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self._update_swatches()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _select_color(self, color: str):
        self._color = color
        self._update_swatches()

    def _update_swatches(self):
        for btn in self._swatches:
            c = btn.property("swatch_color")
            border = ("border: 2px solid #2B2823;" if c == self._color
                      else "border: 1px solid transparent;")
            btn.setStyleSheet(
                f"QPushButton {{ background: {c}; {border} border-radius: 4px; }}")

    def name(self) -> str:
        return self.name_edit.text().strip()

    def color(self) -> str:
        return self._color

    def icon(self) -> str:
        return self.icon_row.icon()

    def accept(self):
        if not self.name():
            QMessageBox.warning(self, "New Group", "Enter a group name")
            return
        super().accept()
