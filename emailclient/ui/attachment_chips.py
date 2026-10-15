"""Rich attachment chips for the compose window.

file_thumb()      — 34px thumbnail: real image preview for image files,
                    painted file-type badge (colored rounded rect + short
                    label) for everything else.
AttachmentChip    — QFrame card: thumb + name + human size + painted ✕.
AttachmentArea    — wrap-flowing, scrollable container of chips.
TYPE_COLORS /     — extension → color mapping shared with the reader's
type_color()        attachment chips (colored ● dots).
"""
from __future__ import annotations

import os

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLayout,
                               QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from . import editor_icons, theme

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_SCALE = 2  # devicePixelRatio for retina

# Extension (no dot, lower) → badge color. Reader imports this for its dots.
TYPE_COLORS = {
    "pdf": "#D93025",
    "doc": "#2B579A", "docx": "#2B579A",
    "xls": "#217346", "xlsx": "#217346", "csv": "#217346",
    "ppt": "#C43E1C", "pptx": "#C43E1C",
    "zip": "#6E7A80", "rar": "#6E7A80", "7z": "#6E7A80",
    "txt": "#70757D",
}


# Clean short labels for the document badge (kept to 3 chars so they fit).
_BADGE_LABELS = {
    "pdf": "PDF",
    "doc": "DOC", "docx": "DOC",
    "xls": "XLS", "xlsx": "XLS", "csv": "CSV",
    "ppt": "PPT", "pptx": "PPT",
    "zip": "ZIP", "rar": "RAR", "7z": "7Z",
    "txt": "TXT",
}


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lstrip(".").lower()


def type_color(path: str) -> str:
    """Badge/dot color for *path*'s extension (theme.TEXT_FAINT fallback)."""
    return TYPE_COLORS.get(_ext(path), theme.TEXT_FAINT or "#9AA0A8")


def human_size(n: int) -> str:
    """Bytes → '1.2 MB' style label."""
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024
    return ""  # pragma: no cover


def _rounded_clip(p: QPainter, size: int, radius: float = 6.0):
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)


def file_thumb(path: str, size: int = 34) -> QPixmap:
    """34px thumbnail pixmap for *path* (retina, 6px rounded corners).

    Image files that load → scaled-cropped preview. Everything else (including
    images that fail to load) → a colored type badge; the file does NOT need
    to exist for a badge.
    """
    out = QPixmap(size * _SCALE, size * _SCALE)
    out.setDevicePixelRatio(_SCALE)
    out.fill(Qt.GlobalColor.transparent)

    src = QPixmap(path) if _ext(path) in (e.lstrip(".") for e in _IMAGE_EXTS) else QPixmap()

    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    _rounded_clip(p, size)
    if not src.isNull():
        # Scale-crop: fill the square, center the overflow.
        scaled = src.scaled(size * _SCALE, size * _SCALE,
                            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(_SCALE)
        x = (size - scaled.width() / _SCALE) / 2
        y = (size - scaled.height() / _SCALE) / 2
        p.drawPixmap(QPoint(round(x), round(y)), scaled)
    else:
        _draw_doc_badge(p, size, _ext(path))
    p.end()
    return out


def _draw_doc_badge(p: QPainter, size: int, ext: str):
    """Draw a professional document-style file icon: a white page with a folded
    top-right corner and a colored band at the bottom showing the file type
    (PDF / XLS / DOC …). Scales cleanly from small reader chips to compose cards.
    """
    color = QColor(TYPE_COLORS.get(ext, theme.TEXT_FAINT or "#9AA0A8"))
    # Short, tidy 3-letter type labels that fit the band (xlsx→XLS, docx→DOC…).
    label = _BADGE_LABELS.get(ext, ext.upper()[:3] if ext else "FILE")

    # Page geometry (leave a little breathing room inside the square).
    m = size * 0.14
    left, right = m, size - m
    top, bottom = size * 0.06, size - size * 0.06
    fold = (right - left) * 0.34  # folded-corner size

    # Page outline with a folded top-right corner.
    page = QPainterPath()
    page.moveTo(left, top)
    page.lineTo(right - fold, top)
    page.lineTo(right, top + fold)
    page.lineTo(right, bottom)
    page.lineTo(left, bottom)
    page.closeSubpath()

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawPath(page)
    # Subtle page border in the type color.
    pen = QPen(color)
    pen.setWidthF(max(1.0, size * 0.05))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPath(page)

    # The folded corner (small triangle, lighter tint).
    corner = QPainterPath()
    corner.moveTo(right - fold, top)
    corner.lineTo(right - fold, top + fold)
    corner.lineTo(right, top + fold)
    corner.closeSubpath()
    p.setPen(Qt.PenStyle.NoPen)
    light = QColor(color)
    light.setAlpha(60)
    p.setBrush(light)
    p.drawPath(corner)

    # Colored band at the bottom carrying the extension label.
    band_h = (bottom - top) * 0.42
    band = QRectF(left, bottom - band_h, right - left, band_h)
    bandpath = QPainterPath()
    bandpath.addRoundedRect(band, size * 0.06, size * 0.06)
    # Square off the band's top so it sits flush inside the page.
    p.setBrush(color)
    p.drawRect(QRectF(left, bottom - band_h, right - left, band_h * 0.5))
    p.drawPath(bandpath)

    f = QFont()
    f.setPixelSize(max(6, int(size * 0.34)))
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#FFFFFF"))
    p.drawText(band, Qt.AlignmentFlag.AlignCenter, label)


class AttachmentChip(QFrame):
    """A single attachment card: thumb, elided name, size, remove button."""

    removed = Signal(str)
    opened = Signal(str)     # chip body clicked — preview it before sending

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("fileChip")
        self._path = path
        self.setFixedHeight(46)
        self.setMaximumWidth(210)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"{path}\n\nClick to preview before sending")

        thumb = QLabel()
        thumb.setPixmap(file_thumb(path))
        thumb.setFixedSize(34, 34)

        name = QLabel()
        name.setObjectName("chipName")
        fm = name.fontMetrics()
        name.setText(fm.elidedText(os.path.basename(path),
                                   Qt.TextElideMode.ElideMiddle, 140))

        size_label = QLabel(self._size_text(path))
        size_label.setStyleSheet(
            f"color: {theme.TEXT_DIM or '#70757D'}; font-size: 10px;")

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        col.addWidget(name)
        col.addWidget(size_label)

        close = QPushButton()
        close.setObjectName("chipClose")
        close.setFixedSize(18, 18)
        close.setIcon(editor_icons.icon("close", theme.TEXT_DIM or "#70757D"))
        close.setIconSize(QSize(12, 12))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Remove attachment")
        close.clicked.connect(lambda: self.removed.emit(self._path))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(8)
        lay.addWidget(thumb)
        lay.addLayout(col, 1)
        lay.addWidget(close)

    def mouseReleaseEvent(self, event):
        # Clicking the card previews the file; the ✕ is its own button and
        # never reaches here, so removing still works as before.
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.opened.emit(self._path)
        super().mouseReleaseEvent(event)

    @staticmethod
    def _size_text(path: str) -> str:
        try:
            return human_size(os.path.getsize(path))
        except OSError:
            return ""

    @property
    def path(self) -> str:
        return self._path


class FlowLayout(QLayout):
    """The classic Qt flow layout: children wrap into as many rows as needed."""

    def __init__(self, parent=None, margin: int = 0, spacing: int = 6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._spacing = spacing
        self._items = []

    def addItem(self, item):
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def spacing(self) -> int:
        return self._spacing

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect):
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y = effective.x(), effective.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y += line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + m.bottom()


class AttachmentArea(QWidget):
    """Scrollable, wrap-flowing host for AttachmentChips.

    `set_files(paths)` rebuilds; clicking a chip previews it via
    file_opened(path); clicking a chip's ✕ emits file_removed(path)
    — the owner stays the source of truth for the path list.
    """

    file_removed = Signal(str)
    file_opened = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._inner = QWidget()
        self._flow = FlowLayout(self._inner, margin=0, spacing=6)

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._inner)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet("background: transparent;")
        self._inner.setStyleSheet("background: transparent;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._scroll)

        self.setMaximumHeight(110)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Maximum)
        self._update_height()

    def set_files(self, paths: list[str]):
        while self._flow.count():
            item = self._flow.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for p in paths:
            chip = AttachmentChip(p)
            chip.removed.connect(self.file_removed.emit)
            chip.opened.connect(self.file_opened.emit)
            self._flow.addWidget(chip)
        self._inner.updateGeometry()
        self._update_height()

    def _update_height(self):
        if self._flow.count() == 0:
            self.setFixedHeight(0)
            return
        self.setMinimumHeight(0)
        self.setMaximumHeight(110)
        width = max(self.width(), 200)
        needed = self._flow.heightForWidth(width)
        self.setFixedHeight(min(needed, 110))
        self.setMaximumHeight(110)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()
