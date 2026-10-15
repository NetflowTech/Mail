"""Programmatically painted toolbar icons for the rich-text editor.

The compose / signature toolbars used unicode glyphs (🅰 🖍 ⇤ ⯇ …) which
render as garbled boxes on some macOS font stacks.  Instead we paint small
crisp icons with QPainter at 2x for retina displays.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)

from . import theme

_SIZE = 18          # logical size of the icon
_SCALE = 2          # devicePixelRatio for retina
_CACHE: dict = {}


def dot_icon(color: str, size: int = 12) -> QIcon:
    """A filled circle in *color* — used as the colored account dot on chips,
    keeping the chip's text in the neutral default color."""
    s = size * _SCALE
    pix = QPixmap(s, s)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0, 0))
    p.setBrush(QColor(color))
    p.drawEllipse(_SCALE, _SCALE, s - 2 * _SCALE, s - 2 * _SCALE)
    p.end()
    pix.setDevicePixelRatio(_SCALE)
    return QIcon(pix)


def _pen(color: str, width: float = 1.6) -> QPen:
    pen = QPen(QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _letter_font(px: int = 12, *, bold: bool = False, italic: bool = False,
                 underline: bool = False, strike: bool = False) -> QFont:
    f = QFont()
    f.setPixelSize(px)
    f.setBold(bold)
    f.setItalic(italic)
    f.setUnderline(underline)
    f.setStrikeOut(strike)
    return f


def _draw_letter(p: QPainter, color: str, text: str, font: QFont,
                 rect: QRectF = None):
    p.setPen(QColor(color))
    p.setFont(font)
    p.drawText(rect or QRectF(0, 0, _SIZE, _SIZE),
               Qt.AlignmentFlag.AlignCenter, text)


def _lines(p: QPainter, color: str, widths, x0: float = 3.0,
           y0: float = 4.0, step: float = 3.2):
    """Draw horizontal text-lines; widths is a list of (start, end) pairs."""
    p.setPen(_pen(color, 1.4))
    y = y0
    for start, end in widths:
        p.drawLine(QPointF(x0 + start, y), QPointF(x0 + end, y))
        y += step


def _paint(name: str, p: QPainter, color: str):
    if name == "bold":
        _draw_letter(p, color, "B", _letter_font(bold=True))
    elif name == "italic":
        _draw_letter(p, color, "I", _letter_font(italic=True))
    elif name == "underline":
        _draw_letter(p, color, "U", _letter_font(underline=True))
    elif name == "strike":
        _draw_letter(p, color, "S", _letter_font(strike=True))
    elif name == "color":
        _draw_letter(p, color, "A", _letter_font(11),
                     QRectF(0, 0, _SIZE, _SIZE - 4))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.GOLD_DEEP or "#A4831B"))
        p.drawRect(QRectF(3, _SIZE - 4.5, _SIZE - 6, 3))
    elif name == "highlight":
        p.setPen(Qt.PenStyle.NoPen)
        soft = QColor(theme.GOLD or "#C29A3B")
        soft.setAlpha(110)
        p.setBrush(soft)
        p.drawRoundedRect(QRectF(2, 2.5, _SIZE - 4, _SIZE - 5), 3, 3)
        _draw_letter(p, color, "A", _letter_font(11))
    elif name == "bullets":
        p.setPen(_pen(color, 1.4))
        for i, y in enumerate((4.5, 9.0, 13.5)):
            p.drawLine(QPointF(7.5, y), QPointF(15.5, y))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        for y in (4.5, 9.0, 13.5):
            p.drawEllipse(QPointF(3.6, y), 1.4, 1.4)
    elif name == "numbers":
        p.setPen(_pen(color, 1.4))
        for y in (4.5, 9.0, 13.5):
            p.drawLine(QPointF(8.0, y), QPointF(15.5, y))
        f = _letter_font(6)
        p.setFont(f)
        p.setPen(QColor(color))
        for digit, y in (("1", 1.0), ("2", 5.5), ("3", 10.0)):
            p.drawText(QRectF(1.0, y, 5.5, 7),
                       Qt.AlignmentFlag.AlignCenter, digit)
    elif name in ("outdent", "indent"):
        p.setPen(_pen(color, 1.4))
        for y in (4.0, 9.0, 14.0):
            p.drawLine(QPointF(8.0, y), QPointF(15.5, y))
        # arrow in the left gutter, at mid height
        if name == "indent":
            p.drawLine(QPointF(1.5, 9.0), QPointF(6.0, 9.0))
            p.drawLine(QPointF(4.0, 6.8), QPointF(6.0, 9.0))
            p.drawLine(QPointF(4.0, 11.2), QPointF(6.0, 9.0))
        else:
            p.drawLine(QPointF(6.0, 9.0), QPointF(1.5, 9.0))
            p.drawLine(QPointF(3.5, 6.8), QPointF(1.5, 9.0))
            p.drawLine(QPointF(3.5, 11.2), QPointF(1.5, 9.0))
    elif name == "align_left":
        _lines(p, color, [(0, 12), (0, 8), (0, 12), (0, 8)], y0=4.0)
    elif name == "align_center":
        _lines(p, color, [(0, 12), (2, 10), (0, 12), (2, 10)], y0=4.0)
    elif name == "align_right":
        _lines(p, color, [(0, 12), (4, 12), (0, 12), (4, 12)], y0=4.0)
    elif name == "link":
        p.setPen(_pen(color, 1.6))
        p.save()
        p.translate(_SIZE / 2, _SIZE / 2)
        p.rotate(-45)
        # two chain segments along the (now horizontal) axis
        p.drawRoundedRect(QRectF(-7.5, -2.6, 8.5, 5.2), 2.6, 2.6)
        p.drawRoundedRect(QRectF(-1.0, -2.6, 8.5, 5.2), 2.6, 2.6)
        p.drawLine(QPointF(-2.5, 0), QPointF(2.5, 0))
        p.restore()
    elif name == "image":
        p.setPen(_pen(color, 1.4))
        p.drawRoundedRect(QRectF(2, 3, 14, 12), 2, 2)
        p.drawEllipse(QPointF(6.2, 7.0), 1.5, 1.5)
        path = QPainterPath(QPointF(3.5, 13.5))
        path.lineTo(8.0, 9.0)
        path.lineTo(11.0, 12.0)
        path.lineTo(13.5, 9.5)
        path.lineTo(15.5, 11.5)
        p.drawPath(path)
    elif name == "clear":
        _draw_letter(p, color, "Tx", _letter_font(10))
        p.setPen(_pen(color, 1.2))
        p.drawLine(QPointF(3.5, 15.0), QPointF(14.5, 3.0))
    elif name == "sign":
        p.setPen(_pen(color, 1.6))
        path = QPainterPath(QPointF(3.0, 12.5))
        path.cubicTo(QPointF(5.5, 7.0), QPointF(8.0, 7.0),
                     QPointF(9.0, 10.0))
        path.cubicTo(QPointF(10.0, 13.0), QPointF(12.0, 12.5),
                     QPointF(15.0, 7.5))
        p.drawPath(path)
        p.setPen(_pen(color, 1.2))
        p.drawLine(QPointF(3.0, 15.0), QPointF(15.0, 15.0))
    elif name == "close":
        # small ✕ cross — used by attachment chip remove buttons
        p.setPen(_pen(color, 2.0))
        p.drawLine(QPointF(5.0, 5.0), QPointF(13.0, 13.0))
        p.drawLine(QPointF(13.0, 5.0), QPointF(5.0, 13.0))
    elif name == "attach":
        # paperclip: outer U + inner return
        p.setPen(_pen(color, 1.5))
        path = QPainterPath(QPointF(6.0, 4.5))
        path.lineTo(6.0, 12.0)
        path.arcTo(QRectF(6.0, 9.0, 6.0, 6.0), 180, 180)
        path.lineTo(12.0, 5.5)
        path.arcTo(QRectF(8.5, 3.5, 3.5, 4.0), 0, 180)
        path.lineTo(8.5, 11.0)
        p.drawPath(path)
    elif name == "inbox":
        # open tray (U-shape with inner lip) + incoming arrow
        p.setPen(_pen(color, 1.6))
        path = QPainterPath(QPointF(2.5, 9.5))
        path.lineTo(2.5, 14.5)
        path.lineTo(15.5, 14.5)
        path.lineTo(15.5, 9.5)
        p.drawPath(path)
        lip = QPainterPath(QPointF(2.5, 9.5))
        lip.lineTo(5.8, 9.5)
        lip.lineTo(7.2, 11.5)
        lip.lineTo(10.8, 11.5)
        lip.lineTo(12.2, 9.5)
        lip.lineTo(15.5, 9.5)
        p.drawPath(lip)
        p.drawLine(QPointF(9.0, 2.5), QPointF(9.0, 7.0))
        p.drawLine(QPointF(6.9, 5.0), QPointF(9.0, 7.0))
        p.drawLine(QPointF(11.1, 5.0), QPointF(9.0, 7.0))
    elif name == "outbox":
        # tray + outgoing (up) arrow
        p.setPen(_pen(color, 1.6))
        path = QPainterPath(QPointF(2.5, 9.5))
        path.lineTo(2.5, 14.5)
        path.lineTo(15.5, 14.5)
        path.lineTo(15.5, 9.5)
        p.drawPath(path)
        lip = QPainterPath(QPointF(2.5, 9.5))
        lip.lineTo(5.8, 9.5)
        lip.lineTo(7.2, 11.5)
        lip.lineTo(10.8, 11.5)
        lip.lineTo(12.2, 9.5)
        lip.lineTo(15.5, 9.5)
        p.drawPath(lip)
        p.drawLine(QPointF(9.0, 7.0), QPointF(9.0, 2.5))
        p.drawLine(QPointF(6.9, 4.5), QPointF(9.0, 2.5))
        p.drawLine(QPointF(11.1, 4.5), QPointF(9.0, 2.5))
    elif name == "sent_folder":
        # paper plane
        p.setPen(_pen(color, 1.5))
        plane = QPainterPath(QPointF(2.2, 8.8))
        plane.lineTo(15.8, 2.8)
        plane.lineTo(10.2, 15.2)
        plane.lineTo(7.6, 10.6)
        plane.closeSubpath()
        p.drawPath(plane)
        p.drawLine(QPointF(7.6, 10.6), QPointF(15.8, 2.8))
    elif name == "trash":
        # bin with lid + handle + ribs
        p.setPen(_pen(color, 1.5))
        p.drawLine(QPointF(3.0, 5.0), QPointF(15.0, 5.0))
        p.drawLine(QPointF(7.0, 5.0), QPointF(7.6, 3.0))
        p.drawLine(QPointF(7.6, 3.0), QPointF(10.4, 3.0))
        p.drawLine(QPointF(10.4, 3.0), QPointF(11.0, 5.0))
        body = QPainterPath(QPointF(4.4, 5.0))
        body.lineTo(5.4, 15.0)
        body.lineTo(12.6, 15.0)
        body.lineTo(13.6, 5.0)
        p.drawPath(body)
        p.setPen(_pen(color, 1.2))
        p.drawLine(QPointF(7.5, 7.5), QPointF(7.5, 12.5))
        p.drawLine(QPointF(10.5, 7.5), QPointF(10.5, 12.5))
    elif name == "spam":
        # warning triangle with exclamation mark
        p.setPen(_pen(color, 1.5))
        tri = QPainterPath(QPointF(9.0, 2.8))
        tri.lineTo(16.0, 14.8)
        tri.lineTo(2.0, 14.8)
        tri.closeSubpath()
        p.drawPath(tri)
        p.drawLine(QPointF(9.0, 7.0), QPointF(9.0, 10.6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(9.0, 12.8), 1.0, 1.0)
    elif name == "folder":
        # classic folder silhouette: translucent fill + crisp outline
        path = QPainterPath(QPointF(2.5, 14.5))
        path.lineTo(2.5, 4.8)
        path.lineTo(7.2, 4.8)
        path.lineTo(8.8, 6.8)
        path.lineTo(15.5, 6.8)
        path.lineTo(15.5, 14.5)
        path.closeSubpath()
        fill = QColor(color)
        fill.setAlpha(60)
        p.setPen(_pen(color, 1.5))
        p.setBrush(fill)
        p.drawPath(path)
    elif name == "dot":
        # small filled status dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(_SIZE / 2, _SIZE / 2), 3.2, 3.2)
    elif name == "compose":
        # pencil writing
        p.setPen(_pen(color, 1.5))
        body = QPainterPath(QPointF(3.4, 14.6))   # nib tip
        body.lineTo(4.6, 10.8)
        body.lineTo(11.4, 4.0)
        body.lineTo(14.0, 6.6)
        body.lineTo(7.2, 13.4)
        body.closeSubpath()
        p.drawPath(body)
        p.drawLine(QPointF(4.6, 10.8), QPointF(7.2, 13.4))
        p.drawLine(QPointF(10.0, 5.4), QPointF(12.6, 8.0))   # eraser band
    elif name == "reply":
        p.setPen(_pen(color, 1.6))
        p.drawLine(QPointF(6.6, 4.8), QPointF(2.8, 8.4))
        p.drawLine(QPointF(2.8, 8.4), QPointF(6.6, 12.0))
        tail = QPainterPath(QPointF(2.8, 8.4))
        tail.lineTo(10.5, 8.4)
        tail.quadTo(QPointF(14.6, 8.4), QPointF(14.6, 12.2))
        tail.lineTo(14.6, 14.4)
        p.drawPath(tail)
    elif name == "reply_all":
        p.setPen(_pen(color, 1.5))
        p.drawLine(QPointF(5.4, 4.4), QPointF(2.0, 8.0))
        p.drawLine(QPointF(2.0, 8.0), QPointF(5.4, 11.6))
        p.drawLine(QPointF(9.0, 4.4), QPointF(5.6, 8.0))
        p.drawLine(QPointF(5.6, 8.0), QPointF(9.0, 11.6))
        tail = QPainterPath(QPointF(5.6, 8.0))
        tail.lineTo(11.6, 8.0)
        tail.quadTo(QPointF(15.2, 8.0), QPointF(15.2, 11.4))
        tail.lineTo(15.2, 13.8)
        p.drawPath(tail)
    elif name == "forward":
        p.setPen(_pen(color, 1.6))
        p.drawLine(QPointF(11.4, 4.8), QPointF(15.2, 8.4))
        p.drawLine(QPointF(15.2, 8.4), QPointF(11.4, 12.0))
        tail = QPainterPath(QPointF(15.2, 8.4))
        tail.lineTo(7.5, 8.4)
        tail.quadTo(QPointF(3.4, 8.4), QPointF(3.4, 12.2))
        tail.lineTo(3.4, 14.4)
        p.drawPath(tail)
    elif name == "bell":
        p.setPen(_pen(color, 1.5))
        dome = QPainterPath(QPointF(5.4, 12.8))
        dome.lineTo(5.4, 9.0)
        dome.cubicTo(QPointF(5.4, 5.4), QPointF(12.6, 5.4), QPointF(12.6, 9.0))
        dome.lineTo(12.6, 12.8)
        p.drawPath(dome)
        p.drawLine(QPointF(3.8, 12.8), QPointF(14.2, 12.8))
        p.drawLine(QPointF(7.9, 14.6), QPointF(9.0, 15.6))
        p.drawLine(QPointF(9.0, 15.6), QPointF(10.1, 14.6))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(9.0, 4.7), 1.0, 1.0)
    elif name == "hourglass":
        p.setPen(_pen(color, 1.5))
        p.drawLine(QPointF(4.6, 3.0), QPointF(13.4, 3.0))
        p.drawLine(QPointF(4.6, 15.0), QPointF(13.4, 15.0))
        top = QPainterPath(QPointF(5.4, 3.0))
        top.lineTo(12.6, 3.0)
        top.lineTo(9.0, 8.8)
        top.closeSubpath()
        bot = QPainterPath(QPointF(9.0, 9.2))
        bot.lineTo(5.4, 15.0)
        bot.lineTo(12.6, 15.0)
        bot.closeSubpath()
        fill = QColor(color)
        fill.setAlpha(55)
        p.setBrush(fill)
        p.drawPath(top)
        p.drawPath(bot)
    elif name == "gear":
        p.setPen(_pen(color, 1.4))
        teeth = ((9.0, 4.8, 9.0, 2.8), (11.97, 6.03, 13.24, 4.76),
                 (13.2, 9.0, 15.2, 9.0), (11.97, 11.97, 13.24, 13.24),
                 (9.0, 13.2, 9.0, 15.2), (6.03, 11.97, 4.76, 13.24),
                 (4.8, 9.0, 2.8, 9.0), (6.03, 6.03, 4.76, 4.76))
        for x0, y0, x1, y1 in teeth:
            p.drawLine(QPointF(x0, y0), QPointF(x1, y1))
        p.drawEllipse(QPointF(9.0, 9.0), 4.0, 4.0)
        p.drawEllipse(QPointF(9.0, 9.0), 1.7, 1.7)
    elif name == "power":
        p.setPen(_pen(color, 1.7))
        p.drawArc(QRectF(4.0, 4.6, 10.0, 10.0), 122 * 16, 296 * 16)
        p.drawLine(QPointF(9.0, 2.8), QPointF(9.0, 9.0))
    elif name == "grid":
        p.setPen(_pen(color, 1.4))
        p.drawRoundedRect(QRectF(3.2, 4.0, 4.8, 10.0), 1.2, 1.2)
        p.drawRoundedRect(QRectF(10.0, 4.0, 4.8, 10.0), 1.2, 1.2)
    elif name == "plus":
        p.setPen(_pen(color, 1.8))
        p.drawLine(QPointF(9.0, 3.8), QPointF(9.0, 14.2))
        p.drawLine(QPointF(3.8, 9.0), QPointF(14.2, 9.0))
    elif name == "bulk":
        p.setPen(_pen(color, 1.6))
        for y in (5.4, 9.0, 12.6):
            p.drawLine(QPointF(3.4, y), QPointF(10.6, y))
        p.drawLine(QPointF(13.4, 9.8), QPointF(13.4, 14.2))
        p.drawLine(QPointF(11.2, 12.0), QPointF(15.6, 12.0))
    elif name == "calendar":
        p.setPen(_pen(color, 1.5))
        p.drawRoundedRect(QRectF(3.0, 4.2, 12.0, 10.8), 1.6, 1.6)
        p.drawLine(QPointF(3.0, 7.6), QPointF(15.0, 7.6))
        p.drawLine(QPointF(6.2, 2.8), QPointF(6.2, 5.4))
        p.drawLine(QPointF(11.8, 2.8), QPointF(11.8, 5.4))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        for cx, cy in ((6.4, 10.4), (9.0, 10.4), (11.6, 10.4),
                       (6.4, 12.6), (9.0, 12.6)):
            p.drawEllipse(QPointF(cx, cy), 0.7, 0.7)
    elif name == "search":
        p.setPen(_pen(color, 1.6))
        p.drawEllipse(QPointF(7.6, 7.6), 3.9, 3.9)
        p.drawLine(QPointF(10.5, 10.5), QPointF(14.6, 14.6))
    elif name == "print":
        # printer: top paper stub, body, output sheet, LED dot
        p.setPen(_pen(color, 1.5))
        top = QPainterPath(QPointF(4.5, 6.0))
        top.lineTo(4.5, 3.0); top.lineTo(13.5, 3.0); top.lineTo(13.5, 6.0)
        p.drawPath(top)
        body = QPainterPath()
        body.addRoundedRect(QRectF(2.5, 6.0, 13.0, 6.0), 1.2, 1.2)
        p.drawPath(body)
        out = QPainterPath(QPointF(5.0, 10.5))
        out.lineTo(5.0, 15.0); out.lineTo(13.0, 15.0); out.lineTo(13.0, 10.5)
        out.closeSubpath()
        p.setBrush(QColor(theme.PANEL or "#FFFFFF"))
        p.drawPath(out)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(13.0, 8.3), 0.8, 0.8)
    elif name == "flag":
        # flag on a pole (filled banner) — used for the message flag action
        p.setPen(_pen(color, 1.6))
        p.drawLine(QPointF(4.6, 2.6), QPointF(4.6, 15.4))          # pole
        banner = QPainterPath(QPointF(4.6, 3.2))
        banner.lineTo(13.6, 3.2)
        banner.lineTo(11.0, 6.2)
        banner.lineTo(13.6, 9.2)
        banner.lineTo(4.6, 9.2)
        banner.closeSubpath()
        fill = QColor(color); fill.setAlpha(75)
        p.setBrush(fill)
        p.drawPath(banner)
    elif name == "pin":
        # thumbtack / pin — pin-to-top action
        p.setPen(_pen(color, 1.5))
        p.drawLine(QPointF(9.0, 10.6), QPointF(9.0, 15.2))         # needle
        head = QPainterPath()
        head.addRoundedRect(QRectF(5.4, 3.0, 7.2, 4.6), 1.6, 1.6)  # head
        fill = QColor(color); fill.setAlpha(75)
        p.setBrush(fill)
        p.drawPath(head)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(4.3, 7.6), QPointF(13.7, 7.6))          # crossbar
    elif name == "mail":
        # envelope — mark read / unread action
        p.setPen(_pen(color, 1.5))
        body = QPainterPath()
        body.addRoundedRect(QRectF(2.5, 4.6, 13.0, 8.8), 1.2, 1.2)
        p.drawPath(body)
        p.drawLine(QPointF(3.2, 5.6), QPointF(9.0, 9.9))
        p.drawLine(QPointF(14.8, 5.6), QPointF(9.0, 9.9))
    elif name == "check":
        # tick — follow-up 'marked complete' marker
        pen = _pen(color, 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        tick = QPainterPath(QPointF(3.6, 9.2))
        tick.lineTo(7.2, 12.8)
        tick.lineTo(14.4, 5.0)
        p.drawPath(tick)
    else:  # pragma: no cover - unknown name fallback: dot
        p.setBrush(QColor(color))
        p.drawEllipse(QPointF(_SIZE / 2, _SIZE / 2), 3, 3)


def icon(name: str, color: str = None) -> QIcon:
    """Return a cached 18x18 (2x retina) painted toolbar icon."""
    col = color or theme.TEXT or "#1F2125"
    key = (name, col, theme.GOLD_DEEP, theme.GOLD)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    img = QImage(_SIZE * _SCALE, _SIZE * _SCALE,
                 QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    img.setDevicePixelRatio(_SCALE)

    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    _paint(name, p, col)
    p.end()

    ico = QIcon(QPixmap.fromImage(img))
    _CACHE[key] = ico
    return ico
