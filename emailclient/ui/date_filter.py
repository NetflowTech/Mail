"""Date filter shared by the main window and group windows.

Easy flow: one-click presets (Today / Last 7 days / ...) apply instantly;
custom ranges use two side-by-side calendars labelled From and To.
"""
from __future__ import annotations

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCalendarWidget, QDialog, QHBoxLayout, QLabel,
                               QPushButton, QVBoxLayout)

from . import theme


class DateFilterDialog(QDialog):
    """Pick a date range via presets or two From/To calendars.

    After exec() returns accepted: `cleared` is True if the user pressed
    Clear; otherwise `date_from()` / `date_to()` return "YYYY-MM-DD" strings.
    """

    def __init__(self, parent=None, date_from: str | None = None,
                 date_to: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Filter by date")
        self.cleared = False
        today = QDate.currentDate()

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 14, 16, 14)

        # -- one-click presets (apply instantly) --
        presets = QHBoxLayout()
        presets.setSpacing(6)
        for label, days in (("Today", 0), ("Yesterday", -1),
                            ("Last 7 days", 6), ("Last 30 days", 29),
                            ("This month", "month")):
            b = QPushButton(label)
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, d=days: self._preset(d))
            presets.addWidget(b)
        presets.addStretch(1)
        layout.addLayout(presets)

        # -- custom range: two calendars side by side --
        cals = QHBoxLayout()
        cals.setSpacing(14)
        for which in ("From", "To"):
            col = QVBoxLayout()
            head = QLabel(which)
            f = head.font()
            f.setWeight(QFont.Weight.DemiBold)
            head.setFont(f)
            head.setStyleSheet(f"color: {theme.GOLD_DEEP};")
            col.addWidget(head)
            cal = QCalendarWidget()
            cal.setGridVisible(False)
            cal.setVerticalHeaderFormat(
                QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
            cal.setMaximumDate(today)
            cal.clicked.connect(self._update_label)
            cal.selectionChanged.connect(self._update_label)
            col.addWidget(cal)
            cals.addLayout(col)
            if which == "From":
                self.from_cal = cal
            else:
                self.to_cal = cal
        layout.addLayout(cals)

        self.from_cal.setSelectedDate(
            QDate.fromString(date_from, "yyyy-MM-dd") if date_from
            else today.addDays(-6))
        self.to_cal.setSelectedDate(
            QDate.fromString(date_to, "yyyy-MM-dd") if date_to else today)

        self.range_label = QLabel()
        f = self.range_label.font()
        f.setBold(True)
        self.range_label.setFont(f)
        self.range_label.setStyleSheet(f"color: {theme.GOLD_DEEP};")
        layout.addWidget(self.range_label)

        buttons = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("primary")
        apply_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_btn.clicked.connect(self._apply)
        clear_btn = QPushButton("Clear filter")
        clear_btn.clicked.connect(self._clear)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(apply_btn)
        buttons.addWidget(clear_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        self._update_label()

    # -- presets apply instantly: one click and done --
    def _preset(self, days):
        today = QDate.currentDate()
        if days == "month":
            start = QDate(today.year(), today.month(), 1)
            end = today
        elif days == -1:  # yesterday
            start = end = today.addDays(-1)
        else:
            start, end = today.addDays(-days), today
        self.from_cal.setSelectedDate(start)
        self.to_cal.setSelectedDate(end)
        self._apply()

    def _update_label(self, *_):
        # setSelectedDate() during construction fires selectionChanged before
        # range_label exists — ignore those early calls.
        if not hasattr(self, "range_label"):
            return
        f, t = self.from_cal.selectedDate(), self.to_cal.selectedDate()
        if f == t:
            self.range_label.setText(f"Selected: {f.toString('dd MMM yyyy')}")
        else:
            self.range_label.setText(
                f"Selected: {f.toString('dd MMM yyyy')} – "
                f"{t.toString('dd MMM yyyy')}")

    def _apply(self):
        # Keep From <= To (swap silently rather than nagging the user).
        if self.from_cal.selectedDate() > self.to_cal.selectedDate():
            f = self.from_cal.selectedDate()
            self.from_cal.setSelectedDate(self.to_cal.selectedDate())
            self.to_cal.setSelectedDate(f)
        self.cleared = False
        self.accept()

    def _clear(self):
        self.cleared = True
        self.accept()

    def date_from(self) -> str:
        return self.from_cal.selectedDate().toString("yyyy-MM-dd")

    def date_to(self) -> str:
        return self.to_cal.selectedDate().toString("yyyy-MM-dd")

    def label(self) -> str:
        """Short human label for the toolbar button, e.g. '12 May – 06 Jun'."""
        f, t = self.from_cal.selectedDate(), self.to_cal.selectedDate()
        if f == t:
            return f.toString("dd MMM yyyy")
        return f"{f.toString('dd MMM')} – {t.toString('dd MMM')}"


def show_date_filter(parent, button, msg_list) -> None:
    """Open the date-filter dialog and apply the result to *msg_list*,
    updating *button*'s text/tooltip to reflect the active filter."""
    dlg = DateFilterDialog(parent, msg_list.date_from, msg_list.date_to)
    if not dlg.exec():
        # A checkable button toggles itself on click; restore the checked
        # state to whatever the current filter actually is on cancel.
        button.setChecked(bool(msg_list.date_from or msg_list.date_to))
        return
    if dlg.cleared:
        msg_list.set_date_range(None, None)
        button.setChecked(False)
        button.setToolTip("Filter by date")
    else:
        msg_list.set_date_range(dlg.date_from(), dlg.date_to())
        # Icon-only toolbar button: highlight when a filter is active and
        # show the exact range in the tooltip (no clipped inline text).
        button.setChecked(True)
        button.setToolTip(f"Showing emails: {dlg.label()}")
