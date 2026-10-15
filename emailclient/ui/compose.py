"""Compose window: new mail, reply, reply-all, forward.

Pure prefill helpers (make_reply_subject / make_quote / make_forward_body)
live at module level so they stay testable without constructing widgets.
The quote date is preformatted by the caller (see format_local_date) so the
helpers stay pure.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import uuid
from datetime import datetime
from email.utils import getaddresses, parseaddr

from PySide6.QtCore import (QEvent, QPoint, QRect, QRectF, Qt, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication,
                           QKeySequence, QShortcut,
                           QImage, QPainter,
                           QPen, QPixmap, QTextBlockFormat, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextImageFormat,
                           QTextLength, QTextListFormat, QTextTableFormat)
from PySide6.QtWidgets import (QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFontComboBox,
                               QFrame,
                               QFormLayout, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMenu, QMessageBox, QPushButton,
                               QSlider, QSplitter, QTextBrowser,
                               QTextEdit, QVBoxLayout, QWidget)

from .. import config
from ..models import EmailMessage
from ..settings import ComposeSettings

log = logging.getLogger(__name__)
from ..smtp_sender import parse_rcpts
from . import editor_icons, theme
from .attachment_chips import AttachmentArea
from .recipient_edit import RecipientDropButton, RecipientEdit

# Most providers cap a message at ~25 MB. Enforce it here so the user gets a
# clean message up front instead of an SMTP failure after sending.
MAX_ATTACH_BYTES = 25 * 1024 * 1024


def make_reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def make_forward_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}"


def reply_all_recipients(sender: str, recipients: str, cc: str,
                         own: str = "") -> tuple[str, str]:
    """Outlook-style Reply-All split of an original message's headers.

    Returns (to, cc) strings where:
      To = original sender + original To recipients (minus our own address)
      Cc = original Cc recipients (minus our own / anyone already in To)
    Duplicates are dropped; ordering is preserved. *own* is our account email.
    """
    seen = {own.lower()} if own else set()
    to = []
    sender_addr = parseaddr(sender or "")[1]
    if sender_addr and sender_addr.lower() not in seen:
        seen.add(sender_addr.lower())
        to.append(sender_addr)
    for _, addr in getaddresses([recipients or ""]):
        a = (addr or "").lower()
        if addr and a not in seen:
            seen.add(a)
            to.append(addr)
    cc_out = []
    for _, addr in getaddresses([cc or ""]):
        a = (addr or "").lower()
        if addr and a not in seen:
            seen.add(a)
            cc_out.append(addr)
    return ", ".join(to), ", ".join(cc_out)


def format_local_date(iso_date: str) -> str:
    """ISO date string → local '%d %b %Y %H:%M'; falls back to the raw value."""
    try:
        dt = datetime.fromisoformat((iso_date or "").strip())
    except ValueError:
        return iso_date or ""
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    return dt.strftime("%d %b %Y %H:%M")


def make_quote(original: EmailMessage, date_str: str) -> str:
    quoted = "\n".join(f"> {line}" for line in (original.body_text or "").splitlines())
    return f"\n\nOn {date_str}, {original.sender} wrote:\n{quoted}"


def make_forward_body(original: EmailMessage, date_str: str) -> str:
    return ("\n\n---------- Forwarded message ----------\n"
            f"From: {original.sender}\n"
            f"Date: {date_str}\n"
            f"Subject: {original.subject}\n"
            f"To: {original.recipients}\n\n"
            f"{original.body_text or ''}")


# Strip the genuinely dangerous bits from an untrusted email before re-embedding
# it in a reply/forward (and re-sending it). The compose editor is a QTextEdit
# (no JS engine), but the quoted HTML also goes back out to the recipient, so we
# remove script/style/iframe/etc., inline event handlers and javascript: URLs
# while keeping ordinary formatting. Denylist + Qt's non-scriptable renderer; no
# extra dependency (the reader already renders mail with JavaScript disabled).
_UNSAFE_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed|form|svg|math)\b[^>]*>.*?"
    r"<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)
_UNSAFE_VOID = re.compile(
    r"<\s*/?\s*(script|style|iframe|object|embed|link|meta|base|form|svg|math)"
    r"\b[^>]*>", re.IGNORECASE)
_EVENT_ATTR = re.compile(
    r"""\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL = re.compile(
    r"""((?:href|src|action)\s*=\s*)("|')\s*javascript:[^"']*\2""",
    re.IGNORECASE)


def sanitize_email_html(raw: str) -> str:
    """Remove scripts/handlers/dangerous tags from untrusted email HTML, keeping
    ordinary formatting. Not a full sanitizer, but the renderer runs no JS."""
    s = _UNSAFE_BLOCK.sub("", raw)
    s = _UNSAFE_VOID.sub("", s)
    s = _EVENT_ATTR.sub("", s)
    s = _JS_URL.sub(r"\1\2#\2", s)
    return s


_STYLE_ATTR = re.compile(r"""\sstyle\s*=\s*("[^"]*"|'[^']*')""", re.IGNORECASE)
_LAYOUT_ATTR = re.compile(
    r"""\s(?:width|height|align|valign|bgcolor|background|border|cellpadding|"""
    r"""cellspacing|color)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_TABLE_TAGS = re.compile(r"</?(?:table|tbody|thead|tfoot|tr|colgroup|col)\b[^>]*>",
                         re.IGNORECASE)
_CELL_OPEN = re.compile(r"<(?:td|th)\b[^>]*>", re.IGNORECASE)
_CELL_CLOSE = re.compile(r"</(?:td|th)>", re.IGNORECASE)


def simplify_email_html(raw: str) -> str:
    """Flatten a complex email template into something QTextEdit can lay out.

    QTextEdit is not a browser: marketing templates (background images, absolute
    positioning, nested layout tables, fixed widths) render as an overlapping
    mess. Strip the styling/layout so the quoted original becomes clean, linear
    text + inline images. Image widths are re-applied later by
    _embed_original_images so they stay constrained."""
    s = sanitize_email_html(raw)
    s = _STYLE_ATTR.sub("", s)
    s = _LAYOUT_ATTR.sub("", s)
    s = _TABLE_TAGS.sub("", s)
    s = _CELL_OPEN.sub(" ", s)
    s = _CELL_CLOSE.sub("<br>", s)
    return s


def insert_paragraph_html(cursor, fragment: str) -> None:
    """Insert *fragment* as its own paragraph, starting a real block first.

    Inserting "<br>" through insertHtml does NOT start a paragraph: Qt turns
    it into a Unicode LINE SEPARATOR inside whatever block the cursor is in.
    Everything typed afterwards then shares that block, and one press of Enter
    splits it and carries the separator along — which is what the user sees as
    an extra blank line between every pair of lines.

    So a new paragraph is made with insertBlock(), which is the only thing in
    Qt that actually starts one.
    """
    # Go to the END of the current block before starting a new one.
    # insertBlock() at the START of a paragraph that already has text splits
    # it and leaves the cursor in front of that text, so the fragment would be
    # glued onto the beginning of the user's own line.
    if cursor.block().text():
        cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock)
        cursor.insertBlock()
    elif not cursor.atBlockStart():
        cursor.insertBlock()
    cursor.insertHtml(fragment)


def normalise_block_spacing(document) -> None:
    """Strip inherited paragraph margins from a document loaded as HTML.

    Qt gives a bare "<p>" its CSS default margins — 12px above and below — and
    pressing Enter copies the block format into the new paragraph. So a draft
    or queued message saved by an older build reopens double-spaced, and keeps
    spreading as the user types, even though composing a fresh one no longer
    does. Reset the margins once, on load.
    """
    from PySide6.QtGui import QTextCursor as _Cursor

    cursor = _Cursor(document)
    cursor.beginEditBlock()
    block = document.begin()
    while block.isValid():
        fmt = block.blockFormat()
        if fmt.topMargin() or fmt.bottomMargin():
            fmt.setTopMargin(0)
            fmt.setBottomMargin(0)
            cursor.setPosition(block.position())
            cursor.setBlockFormat(fmt)
        block = block.next()
    cursor.endEditBlock()


def build_typing_room(cursor, lines: int = 2) -> None:
    """Empty paragraphs at the top of a reply, for the user to write in.

    Real blocks, not "<p><br></p>": the old template left two line separators
    inside the first paragraph the user typed into.
    """
    for _ in range(lines):
        cursor.insertBlock()


def _orig_body_html(original: EmailMessage) -> str:
    """Original message as HTML — prefer its own HTML (sanitized + flattened so
    it renders cleanly in the editor), else escape the plain text."""
    if original.body_html:
        return simplify_email_html(original.body_html)
    text = html.escape(original.body_text or "")
    return text.replace("\n", "<br>")


def _quote_header_block(original: EmailMessage, date_str: str, body: str) -> str:
    """Outlook-style quoted block: a divider, a From/Sent/To/Subject header, then
    the original message — clear and professional, not '>' prefixes."""
    def esc(s):
        return html.escape(s or "")
    return (
        '<div style="border-top:1px solid #C2C8D0; margin-top:14px; padding-top:12px;">'
        '<p style="margin:0 0 10px 0; font-size:13px;">'
        f'<b>From:</b> {esc(original.sender)}<br>'
        f'<b>Sent:</b> {esc(date_str)}<br>'
        f'<b>To:</b> {esc(original.recipients)}<br>'
        f'<b>Subject:</b> {esc(original.subject)}</p>'
        f'{body}'
        '</div>')


_CID_SRC = re.compile(r"""(src\s*=\s*)("|')\s*cid:([^"']+)\2""", re.IGNORECASE)


def inline_image_paths(attachments_json: str | None) -> dict[str, str]:
    """{content-id: on-disk path} for a message's inline images."""
    try:
        items = json.loads(attachments_json or "[]")
    except Exception:
        return {}
    out: dict[str, str] = {}
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        cid = str(item.get("cid", "")).strip().strip("<>").lower()
        path = str(item.get("path", ""))
        if cid and path and os.path.isfile(path):
            out[cid] = path
        # Some mail carries an inline image whose cid never appears in the HTML;
        # those simply stay unmatched below, which is fine.
    return out


def embed_cid_images(raw_html: str, cid_map: dict[str, str]) -> str:
    """Rewrite src="cid:…" to local file URLs so a widget with no mail context
    (the trail preview) can actually show the original's inline images."""
    if not cid_map:
        return raw_html

    def sub(m):
        path = cid_map.get(m.group(3).strip().strip("<>").lower())
        if not path:
            return m.group(0)
        return f'{m.group(1)}{m.group(2)}{QUrl.fromLocalFile(path).toString()}{m.group(2)}'

    return _CID_SRC.sub(sub, raw_html)


def trail_preview_html(original: EmailMessage, date_str: str) -> str:
    """The reply trail as shown in the compose window: the same Outlook-style
    quoted block that goes out with the reply, flattened for widget rendering
    and with inline images resolved to local files."""
    body = embed_cid_images(_orig_body_html(original),
                            inline_image_paths(original.attachments))
    return _quote_header_block(original, date_str, body)


def make_quote_html(original: EmailMessage, date_str: str) -> str:
    """Reply quote as a clean Outlook-style HTML block."""
    return _quote_header_block(original, date_str, _orig_body_html(original))


def make_forward_html(original: EmailMessage, date_str: str) -> str:
    """Forward body as a clean Outlook-style HTML block."""
    return _quote_header_block(original, date_str, _orig_body_html(original))


def forwarded_attachment_paths(attachments_json: str | None) -> list[str]:
    """Real (non-inline) attachment file paths from a message's attachments JSON
    that still exist on disk — what a forward should carry along. Inline cid
    images are skipped (they ride inside the quoted body instead)."""
    try:
        items = json.loads(attachments_json or "[]")
    except Exception:
        return []
    out: list[str] = []
    for item in (items if isinstance(items, list) else []):
        if not isinstance(item, dict) or str(item.get("cid", "")):
            continue
        p = str(item.get("path", ""))
        if p and os.path.isfile(p) and p not in out:
            out.append(p)
    return out


def _bulk_rcpts(text: str) -> list[str]:
    """Split a bulk recipient list on commas/semicolons/whitespace.

    Keeps only tokens that look like addresses (contain '@'), so pasted lists
    with newlines, stray words or trailing separators are handled gracefully.
    """
    parts = re.split(r"[,;\s]+", text or "")
    return [p for p in (part.strip() for part in parts) if p and "@" in p]


def export_file_images(html: str, inline_dir) -> tuple[str, list[dict]]:
    """Convert <img src="file://..."> refs in *html* to cid: parts.

    Each referenced local file is loaded as a QImage, saved as PNG into
    *inline_dir* under a fresh cid, and the src rewritten to cid:.
    Unloadable/missing files are left untouched. String-only (no QTextDocument
    needed) so it stays unit-testable.

    Returns (rewritten_html, [{"cid": hex, "path": str}, ...]).
    """
    inline: list[dict] = []
    for ref in sorted(set(re.findall(r'src="(file://[^"]+)"', html))):
        local = QUrl(ref).toLocalFile()
        image = QImage(local)
        if image.isNull():
            continue
        hexid = uuid.uuid4().hex
        inline_dir.mkdir(parents=True, exist_ok=True)
        path = inline_dir / f"{hexid}.png"
        if not image.save(str(path), "PNG"):
            continue
        html = html.replace(f'src="{ref}"', f'src="cid:{hexid}"')
        inline.append({"cid": hexid, "path": str(path)})
    return html, inline


_TITLES = {"new": "New Message", "reply": "Reply",
           "reply_all": "Reply", "forward": "Forward"}

_SIZES = ["10", "11", "12", "13", "14", "16", "18", "24", "32"]
# Email-safe families every mail client renders correctly.
_FONTS = ["Helvetica", "Arial", "Georgia", "Times New Roman", "Verdana",
          "Tahoma", "Trebuchet MS", "Courier New", "Palatino", "Comic Sans MS"]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_MAX_INLINE_WIDTH = 640


class _CropArea(QWidget):
    """Shows an image; the user drags a rectangle to choose what to keep."""

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self._image = image
        max_w, max_h = 560, 420
        self._scale = 1.0
        if image.width() and image.height():
            self._scale = min(max_w / image.width(), max_h / image.height(), 1.0)
        dw = max(1, int(image.width() * self._scale))
        dh = max(1, int(image.height() * self._scale))
        self._disp = QPixmap.fromImage(image).scaled(
            dw, dh, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.setFixedSize(self._disp.width(), self._disp.height())
        self._sel = QRect(0, 0, self._disp.width(), self._disp.height())
        self._dragging = False
        self._start = QPoint()

    def mousePressEvent(self, e):
        self._dragging = True
        self._start = e.position().toPoint()
        self._sel = QRect(self._start, self._start)
        self.update()

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._sel = QRect(self._start, e.position().toPoint()).normalized() \
                .intersected(QRect(0, 0, self._disp.width(), self._disp.height()))
            self.update()

    def mouseReleaseEvent(self, e):
        self._dragging = False
        if self._sel.width() < 8 or self._sel.height() < 8:
            self._sel = QRect(0, 0, self._disp.width(), self._disp.height())
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._disp)
        full = QRect(0, 0, self._disp.width(), self._disp.height())
        s = self._sel
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 110))   # dim everything outside the selection
        p.drawRect(QRect(0, 0, full.width(), s.top()))
        p.drawRect(QRect(0, s.bottom(), full.width(), full.height() - s.bottom()))
        p.drawRect(QRect(0, s.top(), s.left(), s.height()))
        p.drawRect(QRect(s.right(), s.top(), full.width() - s.right(), s.height()))
        pen = QPen(QColor(theme.GOLD))
        pen.setWidth(2)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(s)
        p.end()

    def cropped_image(self) -> QImage:
        if self._scale <= 0:
            return self._image
        s = self._sel
        rect = QRect(int(s.x() / self._scale), int(s.y() / self._scale),
                     max(1, int(s.width() / self._scale)),
                     max(1, int(s.height() / self._scale))).intersected(self._image.rect())
        return self._image.copy(rect)


class _CropDialog(QDialog):
    """Crop dialog: drag a box over the image, OK returns the cropped image."""

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Crop image")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Drag to select the area to keep."))
        self._area = _CropArea(image, self)
        lay.addWidget(self._area, 0, Qt.AlignmentFlag.AlignCenter)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def cropped_image(self) -> QImage:
        return self._area.cropped_image()


class ImageContextBar(QWidget):
    """Contextual image controls (layout + size). Lives in the editor's chrome
    just under the format toolbar and is shown only while an image is selected,
    so it never floats over — or hides — the signature text.

    Call ``bind(editor)``: it listens to the editor's ``imageSelected`` signal,
    shows/updates itself on selection and hides on deselection.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._editor = None
        self.setObjectName("imageBar")
        self.setStyleSheet(
            "#imageBar { background: %s; border: 1px solid %s; border-radius: 8px; }"
            " QPushButton { border: 0; background: transparent; padding: 4px 9px;"
            " border-radius: 5px; font-size: 13px; }"
            " QPushButton:hover { background: %s; }"
            " QPushButton:checked { background: %s; color: white; }"
            " QLabel { padding: 0 4px; font-size: 12px; color: %s; }"
            % (theme.GOLD_SOFT, theme.GOLD, theme.GOLD_HOVER, theme.GOLD_DEEP,
               theme.TEXT if hasattr(theme, "TEXT") else "#333"))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        lay.setSpacing(2)
        tag = QLabel("Image:")
        tag.setStyleSheet("color:%s; font-weight:600;" % theme.GOLD_DEEP)
        lay.addWidget(tag)

        def _btn(text, tip, slot, checkable=False):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setCheckable(checkable)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(slot)
            lay.addWidget(b)
            return b

        self._inline = _btn("Inline", "Image in the text line",
                            lambda: self._call("inline"), True)
        self._left = _btn("◧ Left", "Image left, text on the right",
                          lambda: self._call("left"), True)
        self._right = _btn("Right ◨", "Image right, text on the left",
                           lambda: self._call("right"), True)
        sep = QLabel("│")
        sep.setStyleSheet("color:#ccc;")
        lay.addWidget(sep)
        _btn("−", "Smaller", lambda: self._size(-30))
        self._size_lbl = QLabel("")
        lay.addWidget(self._size_lbl)
        _btn("+", "Bigger", lambda: self._size(30))
        sep2 = QLabel("│")
        sep2.setStyleSheet("color:#ccc;")
        lay.addWidget(sep2)
        _btn("✂ Crop", "Crop this image", self._crop)
        lay.addStretch(1)
        self.hide()

    def bind(self, editor):
        self._editor = editor
        editor.imageSelected.connect(self._on_selection)

    def _call(self, layout):
        if self._editor is not None:
            self._editor.set_image_layout(layout)

    def _crop(self):
        if self._editor is not None:
            self._editor.crop_selected_image()

    def _size(self, delta):
        if self._editor is not None:
            self._editor.step_image_size(delta)

    def _on_selection(self, info):
        if not info:
            self.hide()
            return
        self._inline.setChecked(info["layout"] == "inline")
        self._left.setChecked(info["layout"] == "left")
        self._right.setChecked(info["layout"] == "right")
        self._size_lbl.setText(f"{int(info['width'])}px")
        self.show()


class _ImageResizeDialog(QDialog):
    """Small slider dialog used by the double-click image resize. Emits
    value_changed live as the slider moves so the image previews in real time."""

    value_changed = Signal(int)

    def __init__(self, current_width: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resize image")
        lay = QVBoxLayout(self)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(80)
        self.slider.setMaximum(800)
        self.slider.setValue(max(80, min(800, current_width)))
        self.value_label = QLabel(f"{self.slider.value()}px")
        self.slider.valueChanged.connect(
            lambda v: (self.value_label.setText(f"{v}px"),
                       self.value_changed.emit(v)))
        row = QHBoxLayout()
        row.addWidget(self.slider, 1)
        row.addWidget(self.value_label)
        lay.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def width_value(self) -> int:
        return self.slider.value()


class _RichBody(QTextEdit):
    """QTextEdit that accepts pasted / dropped images.

    Images are stored as document resources under unique img:// URLs; on send,
    ComposeDialog rewrites them to cid: references with the image data saved
    to disk so the outbox worker can embed them as multipart/related parts.

    Clicking an inserted image emits ``imageSelected`` with the image's layout
    info (or None on deselect); an ImageContextBar in the editor chrome shows
    the layout (Inline / image-left-text-right / image-right-text-left, built as
    borderless tables so they render the same in every email client) and size
    controls. Double-click still opens the exact-width dialog.
    """

    imageSelected = Signal(object)   # dict(name, width, layout, side_text) | None
    emailsPasted = Signal(list)      # [.eml path, ...] -> attach, don't inline
    filesDropped = Signal(list)      # [path, ...] -> attach, don't inline

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMouseTracking(True)   # fire mouseMove without a button held
        self._sel_img_pos = None      # doc pos of the selected image

    @staticmethod
    def _is_image_url(url: QUrl) -> bool:
        return (url.isLocalFile()
                and url.toLocalFile().lower().endswith(_IMAGE_EXTS))

    @staticmethod
    def _local_files(source) -> list:
        if source is None or not source.hasUrls():
            return []
        return [u.toLocalFile() for u in source.urls()
                if u.isLocalFile() and os.path.isfile(u.toLocalFile())]

    def canInsertFromMimeData(self, source) -> bool:
        from .. import eml
        if eml.eml_paths_from_mime(source):
            return True
        if self._local_files(source):
            return True
        if source.hasImage():
            return True
        if source.hasUrls() and any(self._is_image_url(u) for u in source.urls()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source):
        # A copied email pastes as an ATTACHMENT, the way Outlook does it —
        # dumping its text into the body would lose the headers and files.
        from .. import eml
        paths = eml.eml_paths_from_mime(source)
        if paths:
            self.emailsPasted.emit(paths)
            return
        if source.hasImage():
            image = QImage(source.imageData())
            if not image.isNull():
                self.insert_qimage(image)
                return
        if source.hasUrls():
            handled = False
            for url in source.urls():
                if self._is_image_url(url):
                    image = QImage(url.toLocalFile())
                    if not image.isNull():
                        self.insert_qimage(image)
                        handled = True
            # Anything else that came in as a real file is an attachment —
            # dropping a PDF on the body used to insert its path as text.
            others = [p for p in self._local_files(source)
                      if not p.lower().endswith(_IMAGE_EXTS)]
            if others:
                self.filesDropped.emit(others)
                handled = True
            if handled:
                return
        super().insertFromMimeData(source)

    def set_spell_highlighter(self, highlighter) -> None:
        self._spell_highlighter = highlighter

    def contextMenuEvent(self, event):
        highlighter = getattr(self, "_spell_highlighter", None)
        if highlighter is None:
            super().contextMenuEvent(event)
            return
        from .spellcheck import build_menu
        menu = build_menu(self, highlighter, event.pos())
        menu.exec(event.globalPos())

    def insert_qimage(self, image: QImage, cursor: QTextCursor | None = None,
                      target_width: int | None = None):
        """Register *image* as a document resource and insert it at the cursor.

        *target_width*: if given and image is wider, scale down to that width.
        Regardless, never exceed _MAX_INLINE_WIDTH.  If image is narrower than
        *target_width* it is left at its natural size (no upscaling).
        """
        effective_max = min(target_width, _MAX_INLINE_WIDTH) if target_width else _MAX_INLINE_WIDTH
        if image.width() > effective_max:
            image = image.scaledToWidth(
                effective_max, Qt.TransformationMode.SmoothTransformation)
        name = f"img://{uuid.uuid4().hex}"
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(name), image)
        (cursor if cursor is not None else self.textCursor()).insertImage(name)

    # ----- double-click image resize ---------------------------------------

    def image_format_at(self, doc_pos: int) -> QTextImageFormat | None:
        """QTextImageFormat of the image char at *doc_pos*, or None.

        In Qt the char format of the character at position N is reported by a
        cursor sitting at N+1 (charFormat() describes the char to the left).
        """
        cursor = QTextCursor(self.document())
        cursor.setPosition(doc_pos)
        if not cursor.movePosition(QTextCursor.MoveOperation.Right):
            return None
        fmt = cursor.charFormat()
        return fmt.toImageFormat() if fmt.isImageFormat() else None

    def _image_pos_for_cursor(self, cursor: QTextCursor) -> int | None:
        """Document position of the image char at/next to *cursor*, or None."""
        pos = cursor.position()
        # Char to the left of the cursor
        if pos > 0 and self.image_format_at(pos - 1) is not None:
            return pos - 1
        # Char to the right of the cursor
        if self.image_format_at(pos) is not None:
            return pos
        return None

    def _natural_image_size(self, fmt: QTextImageFormat) -> tuple[int, int]:
        """(width, height) of the underlying image resource, or (0, 0)."""
        url = QUrl(fmt.name())
        res = self.document().resource(
            QTextDocument.ResourceType.ImageResource, url)
        image = res if isinstance(res, QImage) else QImage()
        if image.isNull() and url.isLocalFile():
            image = QImage(url.toLocalFile())
        if image.isNull():
            return 0, 0
        return image.width(), image.height()

    def resize_image_at(self, doc_pos: int, width: int) -> bool:
        """Set the display width of the image char at *doc_pos*. True if done.

        Height is recomputed from the resource's aspect ratio so the image
        always scales proportionally.
        """
        existing = self.image_format_at(doc_pos)
        if existing is None:
            return False
        fmt = QTextImageFormat(existing)
        fmt.setWidth(float(width))
        nat_w, nat_h = self._natural_image_size(existing)
        if nat_w > 0 and nat_h > 0:
            fmt.setHeight(round(width * nat_h / nat_w))
        cursor = QTextCursor(self.document())
        cursor.setPosition(doc_pos)
        cursor.movePosition(QTextCursor.MoveOperation.Right,
                            QTextCursor.MoveMode.KeepAnchor)
        cursor.setCharFormat(fmt)
        return True

    def mouseDoubleClickEvent(self, event):
        cursor = self.cursorForPosition(event.position().toPoint())
        img_pos = self._image_pos_for_cursor(cursor)
        if img_pos is None:
            super().mouseDoubleClickEvent(event)
            return
        fmt = self.image_format_at(img_pos)
        current = int(fmt.width()) if fmt.width() > 0 else (
            self._natural_image_size(fmt)[0] or 320)
        dlg = _ImageResizeDialog(current, self)
        # Live preview while dragging — use the fast format-only resize so it
        # stays responsive; commit the crisp re-render only on OK.
        dlg.value_changed.connect(
            lambda w: _RichBody.resize_image_at(self, img_pos, w))
        if dlg.exec():
            self.resize_image_at(img_pos, dlg.width_value())
        else:
            self.resize_image_at(img_pos, current)   # revert on Cancel

    # ----- image layout + size (driven by ImageContextBar) -----------------

    def _image_rect(self, img_pos: int) -> QRectF | None:
        """Display rectangle (viewport coords) of the image char at *img_pos*."""
        fmt = self.image_format_at(img_pos)
        if fmt is None:
            return None
        nat_w, nat_h = self._natural_image_size(fmt)
        w = fmt.width() or nat_w
        h = fmt.height() or nat_h
        if w <= 0 or h <= 0:
            return None
        cur = QTextCursor(self.document())
        cur.setPosition(img_pos + 1)        # caret after the image = right edge
        r = self.cursorRect(cur)
        return QRectF(r.left() - w, r.bottom() - h, w, h)

    def _img_pos_under(self, pt):
        """Image doc-pos only if *pt* is genuinely INSIDE the image box.

        cursorForPosition snaps to the nearest character, so on its own it would
        report an image when the mouse is merely near one (or in empty space next
        to it) — that made the hand cursor appear everywhere. Verify containment
        in the image's actual rectangle."""
        pos = self._image_pos_for_cursor(self.cursorForPosition(pt))
        if pos is None:
            return None
        rect = self._image_rect(pos)
        return pos if (rect is not None and rect.contains(pt)) else None

    def _image_table(self, img_pos: int):
        """The QTextTable wrapping the image (our left/right layout), or None."""
        cur = QTextCursor(self.document())
        # The cell context reads firmly only just AFTER the image char.
        cur.setPosition(min(img_pos + 1, self.document().characterCount() - 1))
        return cur.currentTable()

    def _image_info(self, img_pos: int) -> dict | None:
        """Describe the image at *img_pos*: name, width, layout, side text."""
        fmt = self.image_format_at(img_pos)
        if fmt is None:
            return None
        width = int(fmt.width()) if fmt.width() > 0 else (
            self._natural_image_size(fmt)[0] or 320)
        table = self._image_table(img_pos)
        layout, side_text = "inline", ""
        if table is not None:
            cur = QTextCursor(self.document())
            cur.setPosition(min(img_pos + 1, self.document().characterCount() - 1))
            cell = table.cellAt(cur)
            col = cell.column()
            layout = "left" if col == 0 else "right"
            other = table.cellAt(0, 1 - col)
            tc = QTextCursor(self.document())
            tc.setPosition(other.firstCursorPosition().position())
            tc.setPosition(other.lastCursorPosition().position(),
                           QTextCursor.MoveMode.KeepAnchor)
            side_text = tc.selectedText().replace(" ", "\n").strip()
        return {"name": fmt.name(), "width": width,
                "layout": layout, "side_text": side_text}

    def _remove_image_struct(self, img_pos: int) -> int:
        """Remove the image (and its layout table). Returns the freed position."""
        table = self._image_table(img_pos)
        if table is not None:
            start = table.firstPosition() - 1
            cur = QTextCursor(self.document())
            cur.setPosition(max(0, start))
            cur.setPosition(table.lastPosition() + 1,
                            QTextCursor.MoveMode.KeepAnchor)
            cur.removeSelectedText()
            return max(0, start)
        cur = QTextCursor(self.document())
        cur.setPosition(img_pos)
        cur.movePosition(QTextCursor.MoveOperation.Right,
                         QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        return img_pos

    def _find_image_pos(self, name: str) -> int | None:
        for i in range(self.document().characterCount()):
            f = self.image_format_at(i)
            if f is not None and f.name() == name:
                return i
        return None

    def _insert_image_struct(self, at_pos: int, name: str, width: int,
                             layout: str, side_text: str):
        """(Re)insert the image at *at_pos* in the given layout."""
        nat_w, nat_h = self._natural_image_size_by_name(name)
        ifmt = QTextImageFormat()
        ifmt.setName(name)
        ifmt.setWidth(float(width))
        if nat_w > 0 and nat_h > 0:
            ifmt.setHeight(round(width * nat_h / nat_w))
        cur = QTextCursor(self.document())
        cur.setPosition(min(at_pos, self.document().characterCount() - 1))
        if layout == "inline":
            cur.insertImage(ifmt)
            return
        tfmt = QTextTableFormat()
        tfmt.setBorder(0)
        tfmt.setCellSpacing(0)
        tfmt.setCellPadding(4)
        tfmt.setColumnWidthConstraints([
            QTextLength(QTextLength.Type.FixedLength, width + 8),
            QTextLength(QTextLength.Type.VariableLength, 0)])
        table = cur.insertTable(1, 2, tfmt)
        img_col, txt_col = (0, 1) if layout == "left" else (1, 0)
        table.cellAt(0, img_col).firstCursorPosition().insertImage(ifmt)
        txt_cur = table.cellAt(0, txt_col).firstCursorPosition()
        txt_cur.insertText(side_text or "Your text here")

    def _natural_image_size_by_name(self, name: str) -> tuple[int, int]:
        url = QUrl(name)
        res = self.document().resource(
            QTextDocument.ResourceType.ImageResource, url)
        image = res if isinstance(res, QImage) else QImage()
        if image.isNull() and url.isLocalFile():
            image = QImage(url.toLocalFile())
        return (image.width(), image.height()) if not image.isNull() else (0, 0)

    def _purge_image(self, name: str) -> int:
        """Remove EVERY structure that holds the image *name*; return the first
        freed document position. Removing by name (not a tracked position) makes
        layout changes idempotent and immune to table-boundary position quirks,
        so an image can never be left behind as a duplicate."""
        first_at = None
        for _ in range(12):                 # safety bound; only ever 1 in practice
            pos = self._find_image_pos(name)
            if pos is None:
                break
            at = self._remove_image_struct(pos)
            if first_at is None:
                first_at = at
        return first_at if first_at is not None else self.textCursor().position()

    def set_image_layout(self, layout: str):
        """Switch the selected image between inline / left / right."""
        if self._sel_img_pos is None:
            return
        info = self._image_info(self._sel_img_pos)
        if info is None or info["layout"] == layout:
            return
        at = self._purge_image(info["name"])
        self._insert_image_struct(at, info["name"], info["width"],
                                  layout, info["side_text"])
        self._sel_img_pos = self._find_image_pos(info["name"])
        self._emit_selection()

    def step_image_size(self, delta: int):
        """Grow/shrink the selected image by *delta* px (aspect kept)."""
        if self._sel_img_pos is None:
            return
        info = self._image_info(self._sel_img_pos)
        if info is None:
            return
        new_w = max(32, min(info["width"] + delta, _MAX_INLINE_WIDTH))
        self.resize_image_at(self._sel_img_pos, new_w)
        # keep the layout table's column matched to the new image width
        table = self._image_table(self._sel_img_pos)
        if table is not None:
            tfmt = table.format()
            col = 0 if info["layout"] == "left" else 1
            cons = [QTextLength(QTextLength.Type.VariableLength, 0),
                    QTextLength(QTextLength.Type.VariableLength, 0)]
            cons[col] = QTextLength(QTextLength.Type.FixedLength, new_w + 8)
            tfmt.setColumnWidthConstraints(cons)
            table.setFormat(tfmt)
        self._emit_selection()

    def _source_image_at(self, img_pos: int):
        """Best-resolution QImage backing the image at *img_pos* (or None)."""
        fmt = self.image_format_at(img_pos)
        if fmt is None:
            return None
        name = fmt.name()
        res = self.document().resource(
            QTextDocument.ResourceType.ImageResource, QUrl(name))
        img = res if isinstance(res, QImage) else QImage()
        if img.isNull() and QUrl(name).isLocalFile():
            img = QImage(QUrl(name).toLocalFile())
        return img if not img.isNull() else None

    def crop_selected_image(self):
        """Open the crop dialog for the selected image and replace it with the
        cropped result (kept at the same display width)."""
        if self._sel_img_pos is None:
            return
        info = self._image_info(self._sel_img_pos)
        src = self._source_image_at(self._sel_img_pos)
        if not info or src is None or src.isNull():
            return
        dlg = _CropDialog(src, self)
        if not dlg.exec():
            return
        cropped = dlg.cropped_image()
        if cropped is None or cropped.isNull():
            return
        at = self._purge_image(info["name"])
        cur = QTextCursor(self.document())
        cur.setPosition(min(at, self.document().characterCount() - 1))
        self.insert_qimage(cropped, cursor=cur, target_width=info["width"])
        self._sel_img_pos = None
        self._emit_selection()

    def _emit_selection(self):
        """Tell the ImageContextBar what (if anything) is selected."""
        info = (self._image_info(self._sel_img_pos)
                if self._sel_img_pos is not None else None)
        if info is None:
            self._sel_img_pos = None
        self.imageSelected.emit(info)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._sel_img_pos = self._img_pos_under(event.position().toPoint())
            self._emit_selection()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        over_image = self._img_pos_under(event.position().toPoint()) is not None
        # Hand only over an actual image; restore the text caret everywhere else
        # so it never sticks as a hand over plain text.
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if over_image
                                  else Qt.CursorShape.IBeamCursor)


class ComposeDialog(QDialog):
    draft_saved = Signal()          # a draft was written — refresh the folder
    state_changed = Signal()        # minimised/restored — the footer dock listens
    def __init__(self, db, preset_account_id=None,
                 original: EmailMessage | None = None, mode: str = "new",
                 edit_outbox_id: int | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        # When set, Send updates this existing outbox row instead of queuing a new
        # one — used to edit a queued/failed/rejected email and re-send it.
        self.edit_outbox_id = edit_outbox_id
        # Row this window's draft lives in (the edited row, or one created the
        # first time the draft is saved).
        self._draft_id = edit_outbox_id
        self._sent = False        # True once _send queued it: no draft then
        # Editing a QUEUED outbox row is different: cancelling must leave the
        # queued message exactly as it was, so no draft is written for it.
        self.autosave_draft = True
        self._attachments: list[str] = []
        self.queued_outbox_ids: list[int] = []  # rows queued by _send (for Undo)
        self._quote = None          # (original EmailMessage, date_str) for reply/fwd
        self._quote_label = None
        self._sig_block = ""
        # Tracked extent of the auto-appended signature (text + optional image):
        # _sig_start_pos = doc position where it starts; _sig_snapshot = the
        # plain-text it rendered to, used to detect user edits before removal.
        self._sig_start_pos: int | None = None
        self._sig_snapshot = ""
        self.setWindowTitle("Edit Email" if edit_outbox_id is not None
                            else _TITLES.get(mode, "New Message"))
        # A dialog gets only a close button by default; compose is a real window
        # the user wants to minimise out of the way or maximise while writing.
        # Replace (not OR) the flags: Qt.Dialog must be cleared, or macOS keeps
        # building a panel-style NSWindow whose minimise button stays greyed out.
        self.setWindowFlags(Qt.WindowType.Window
                            | Qt.WindowType.WindowTitleHint
                            | Qt.WindowType.WindowSystemMenuHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowMaximizeButtonHint
                            | Qt.WindowType.WindowCloseButtonHint)
        self.setSizeGripEnabled(True)
        self.resize(*self._preferred_size())
        # macOS greys out minimise on a window that is a transient child of
        # another; clearing the transient parent (done once the native window
        # exists, in showEvent) makes it a first-class window that minimises to
        # the Dock and gets its own taskbar entry on Windows.
        self._transient_cleared = False

        self.from_box = QComboBox()
        for a in db.list_accounts():
            self.from_box.addItem(f"{a.name} — {a.email}", a.id)
        want = preset_account_id if preset_account_id is not None else (
            original.account_id if original else None)
        if want is not None:
            idx = self.from_box.findData(want)
            if idx >= 0:
                self.from_box.setCurrentIndex(idx)

        self.to_edit = RecipientEdit()
        self.cc_edit = RecipientEdit()
        self.bcc_edit = RecipientEdit()
        for w in (self.to_edit, self.cc_edit, self.bcc_edit):
            w.setPlaceholderText("separate with commas")
        self.subject_edit = QLineEdit()

        # Outlook-style: Cc/Bcc are hidden until the user reveals them via these
        # small toggles sitting at the right of the To row.
        # These also accept a dragged recipient: the row they reveal is hidden,
        # and a hidden widget receives no drops, so without this a move to Cc
        # would still need the reveal-then-drag dance.
        self.cc_toggle = RecipientDropButton("Cc")
        self.bcc_toggle = RecipientDropButton("Bcc")
        for b in (self.cc_toggle, self.bcc_toggle):
            b.setObjectName("ccBccToggle")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.cc_toggle.clicked.connect(lambda: self._show_cc(True, focus=True))
        self.bcc_toggle.clicked.connect(lambda: self._show_bcc(True, focus=True))
        self.cc_toggle.recipientDropped.connect(
            lambda d: self._drop_into(self.cc_edit, self._show_cc, d))
        self.bcc_toggle.recipientDropped.connect(
            lambda d: self._drop_into(self.bcc_edit, self._show_bcc, d))
        to_row = QHBoxLayout()
        to_row.setContentsMargins(0, 0, 0, 0)
        to_row.setSpacing(6)
        to_row.addWidget(self.to_edit, 1)
        to_row.addWidget(self.cc_toggle)
        to_row.addWidget(self.bcc_toggle)
        self._to_row_widget = QWidget()
        self._to_row_widget.setLayout(to_row)

        self.body_edit = _RichBody()
        self.body_edit.setAcceptRichText(True)
        # Keep this modest: the body has stretch=1 so it grows to fill the
        # window, but a large *minimum* forces the dialog's minimumSizeHint past
        # the screen height — which pushed the Send button row off the bottom on
        # smaller screens / when an attachment expands the chip area.
        self.body_edit.setMinimumHeight(120)
        # Spell check: red squiggles as you type, corrections on right-click.
        from .spellcheck import SpellHighlighter
        self._spell = SpellHighlighter(self.body_edit.document())
        self._spell.set_enabled(ComposeSettings().spell_check)
        self.body_edit.set_spell_highlighter(self._spell)
        body_font = self.body_edit.font()
        body_font.setPointSize(13)
        self.body_edit.setFont(body_font)
        self.body_edit.document().setDefaultFont(body_font)

        editor_bar = self._build_editor_bar()

        self.body_edit.cursorPositionChanged.connect(self._sync_toolbar)
        self.body_edit.currentCharFormatChanged.connect(
            lambda _fmt: self._sync_toolbar())

        # Attach lives in the top action bar (Outlook-style); the chips sit
        # directly under Subject, above the body, where attachments belong.
        self.attach_btn = QPushButton("📎 Attach")
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.clicked.connect(self._pick_files)
        self.attach_area = AttachmentArea()
        self.attach_area.file_removed.connect(self._remove_attachment)
        self.attach_area.file_opened.connect(self._preview_attachment)

        self.bulk_hint = QLabel(
            "Bulk mode ON — every address in To (comma/newline separated)"
            " gets its own separate email. CC/BCC are ignored.")
        self.bulk_hint.setObjectName("guide")
        self.bulk_hint.setWordWrap(True)
        self.bulk_hint.hide()

        form = QFormLayout()
        form.setVerticalSpacing(10)
        form.addRow("From", self.from_box)
        form.addRow("To", self._to_row_widget)
        form.addRow("", self.bulk_hint)
        form.addRow("Cc", self.cc_edit)
        form.addRow("Bcc", self.bcc_edit)
        form.addRow("Subject", self.subject_edit)
        self._form = form
        # Hidden by default; toggles (or prefilled content) reveal them.
        form.setRowVisible(self.cc_edit, False)
        form.setRowVisible(self.bcc_edit, False)

        self.bulk_btn = QPushButton("📨 Bulk mode")
        self.bulk_btn.setCheckable(True)
        self.bulk_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.bulk_btn.setToolTip(
            "Each recipient gets a separate email"
            " (recipients can't see each other)")
        self.bulk_btn.toggled.connect(self.bulk_hint.setVisible)

        self.send_btn = send_btn = QPushButton("Send")
        send_btn.setObjectName("primary")
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setToolTip("Send (Ctrl+Enter)")
        send_btn.clicked.connect(self._send)
        # Ctrl+Enter sends from anywhere in the window, including mid-typing —
        # the editor swallows a plain Enter, so it needs its own shortcut.
        for seq in ("Ctrl+Return", "Ctrl+Enter"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(self._send)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        # One action bar across the top, the way Outlook does it: Send first,
        # then everything you do to the message. Nothing hides below the fold
        # on a small window any more.
        action_bar = QWidget()
        action_bar.setObjectName("composeActions")
        action_row = QHBoxLayout(action_bar)
        action_row.setContentsMargins(0, 0, 0, 4)
        action_row.setSpacing(8)
        action_row.addWidget(send_btn)
        action_row.addWidget(cancel_btn)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        action_row.addWidget(sep)
        action_row.addWidget(self.attach_btn)
        action_row.addWidget(self.bulk_btn)
        action_row.addStretch(1)

        root = QVBoxLayout(self)
        root.addWidget(action_bar)
        root.addLayout(form)
        root.addWidget(self.attach_area)   # attachments under Subject
        root.addWidget(editor_bar)
        self._img_bar = ImageContextBar()
        self._img_bar.bind(self.body_edit)
        self.body_edit.emailsPasted.connect(self._attach_pasted_emails)
        self.body_edit.filesDropped.connect(self._attach_dropped_files)
        # Files can be dropped anywhere on the window, not just on the body —
        # people aim at the header or the attachment strip just as often.
        self.setAcceptDrops(True)
        root.addWidget(self._img_bar)
        # Editor and quoted trail share the vertical space through a splitter,
        # the way Outlook lets you drag the divider: the trail used to be capped
        # at 240px, which showed barely three lines of the original.
        self._body_split = QSplitter(Qt.Orientation.Vertical)
        self._body_split.setChildrenCollapsible(False)
        self._body_split.setHandleWidth(6)
        self._body_split.addWidget(self.body_edit)
        root.addWidget(self._body_split, 1)
        self._quote_label = QLabel()
        self._quote_label.setObjectName("guide")
        self._quote_label.setWordWrap(True)
        self._quote_label.setTextFormat(Qt.TextFormat.RichText)
        self._view_orig_btn = QPushButton("👁  View original")
        self._view_orig_btn.setObjectName("ccBccToggle")
        self._view_orig_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._view_orig_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._view_orig_btn.setToolTip("Show the original message you're replying to")
        self._view_orig_btn.clicked.connect(self._open_original)
        # Gmail-style trail toggle: the quoted conversation is right there in the
        # compose window, not only behind the full-fidelity viewer.
        self._trail_btn = QPushButton("⌄  Show trail")
        self._trail_btn.setObjectName("ccBccToggle")
        self._trail_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._trail_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._trail_btn.setToolTip(
            "Show the quoted conversation that goes out below your reply")
        self._trail_btn.clicked.connect(self._toggle_trail)
        quote_row = QHBoxLayout()
        quote_row.setContentsMargins(0, 0, 0, 0)
        quote_row.setSpacing(8)
        quote_row.addWidget(self._quote_label, 1,
                            Qt.AlignmentFlag.AlignVCenter)
        quote_row.addWidget(self._trail_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        quote_row.addWidget(self._view_orig_btn, 0,
                            Qt.AlignmentFlag.AlignVCenter)
        self._quote_row_widget = QWidget()
        self._quote_row_widget.setLayout(quote_row)
        self._quote_row_widget.hide()
        # The trail renders through the same engine as the reading pane, created
        # on first use. A QTextBrowser could not lay out real mail — remote and
        # inline images came out as empty boxes and templates collapsed — which
        # is exactly what the quoted conversation is made of.
        self._trail_view = None
        self._trail_host = QWidget()
        trail_host_lay = QVBoxLayout(self._trail_host)
        trail_host_lay.setContentsMargins(0, 0, 0, 0)
        trail_host_lay.setSpacing(0)
        self._trail_host.hide()
        self._trail_open = False
        # Lower pane of the splitter: the "replying to…" bar with its buttons,
        # and under it the trail itself. With the trail hidden the pane is just
        # the thin bar, so the editor keeps the whole window.
        trail_pane = QWidget()
        trail_lay = QVBoxLayout(trail_pane)
        trail_lay.setContentsMargins(0, 0, 0, 0)
        trail_lay.setSpacing(6)
        trail_lay.addWidget(self._quote_row_widget)
        trail_lay.addWidget(self._trail_host, 1)
        self._trail_pane = trail_pane
        self._body_split.addWidget(trail_pane)
        self._body_split.setStretchFactor(0, 3)
        self._body_split.setStretchFactor(1, 2)
        trail_pane.setMaximumHeight(0)   # nothing to show until _set_quote_indicator


        if edit_outbox_id is not None:
            # Editing an existing outbox row: load its content verbatim. The body
            # already carries whatever signature it was composed with, so don't
            # auto-append another one. Bulk mode is meaningless for a single row.
            self._prefill_outbox(edit_outbox_id)
            self.bulk_btn.hide()
        else:
            self._prefill(original, mode)
            # Reply/forward already embed the signature above the quote inside
            # _prefill; only plain "new" mail needs the auto-appended signature.
            if not (original and mode in ("reply", "reply_all", "forward")):
                self._apply_signature(initial=True)
        # Replies embed a fixed signature above the quote; don't re-run the
        # tail-based signature swap on From-account change (it can't track a
        # signature that sits above quoted text — it would duplicate it).
        self._reply_mode = bool(original) and mode in (
            "reply", "reply_all", "forward")
        self.from_box.currentIndexChanged.connect(self._on_from_changed)
        # Autosave: a crash, a power cut or a stray ⌘Q must not cost the user
        # what they typed. Cheap — it only writes when something changed.
        self._draft_timer = QTimer(self)
        self._draft_timer.setInterval(20_000)
        self._draft_timer.timeout.connect(self._autosave)
        self._draft_timer.start()

        # Recipient autocomplete: one capped query per dialog, shared by all
        # three fields (compose opens often — the UI caches, not the db).
        addrs = db.known_addresses()
        for w in (self.to_edit, self.cc_edit, self.bcc_edit):
            w.set_addresses(addrs)

        # Reveal Cc/Bcc when a reply/edit prefilled them, so existing recipients
        # are never hidden.
        if self.cc_edit.text().strip():
            self._show_cc(True)
        if self.bcc_edit.text().strip():
            self._show_bcc(True)

        self._fit_to_screen()

    def _drop_into(self, field, reveal, display: str) -> None:
        """A recipient dropped on the Cc/Bcc button: show the row, add it there.

        Removing it from the field it came from is the source chip's job — it
        only lets go once the drop is reported as a completed move, which it
        now is.
        """
        reveal(True)
        if not field.has_recipient(display):
            field.setText(", ".join(
                [t for t in [field.text().strip().rstrip(","), display] if t]))

    def _show_cc(self, show: bool = True, focus: bool = False):
        self._form.setRowVisible(self.cc_edit, show)
        self.cc_toggle.setVisible(not show)
        if show and focus:
            self.cc_edit.setFocus()

    def _show_bcc(self, show: bool = True, focus: bool = False):
        self._form.setRowVisible(self.bcc_edit, show)
        self.bcc_toggle.setVisible(not show)
        if show and focus:
            self.bcc_edit.setFocus()

    def _fit_to_screen(self):
        """Clamp the dialog to the available screen and center it, so the Send
        button row at the bottom is always on-screen (Windows taskbar / small
        laptop displays were cropping it)."""
        from PySide6.QtWidgets import QApplication
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        w = min(self.width(), avail.width() - 40)
        h = min(self.height(), avail.height() - 40)
        self.resize(max(w, 480), max(h, 420))
        self.move(avail.center().x() - self.width() // 2,
                  avail.center().y() - self.height() // 2)

    # ----- editor toolbar ---------------------------------------------------

    def _tool_btn(self, icon_name: str, slot, checkable: bool = False,
                  tip: str = "") -> QPushButton:
        btn = QPushButton()
        btn.setIcon(editor_icons.icon(icon_name))
        btn.setFixedSize(28, 26)
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            btn.setToolTip(tip)
        btn.clicked.connect(slot)
        return btn

    def _toggle_spell_check(self, on: bool) -> None:
        """Remembered across windows and restarts — people who compose in
        another language want it off for good, not per message."""
        self._spell.set_enabled(on)
        ComposeSettings().spell_check = on

    def _build_editor_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("editorBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(3)

        # Plain QComboBox with email-safe fonts: QFontComboBox's popup is
        # unreliable under Fusion+QSS on macOS (user could not open it).
        self.font_box = QComboBox()
        self.font_box.addItems(_FONTS)
        self.font_box.setMaximumWidth(150)
        self.font_box.setToolTip("Font family")
        self.font_box.textActivated.connect(self._set_font_family)
        lay.addWidget(self.font_box)

        self.size_box = QComboBox()
        self.size_box.setEditable(True)
        self.size_box.addItems(_SIZES)
        self.size_box.setCurrentText("13")
        self.size_box.setMaximumWidth(58)
        self.size_box.textActivated.connect(self._set_font_size)
        self.size_box.lineEdit().returnPressed.connect(
            lambda: self._set_font_size(self.size_box.currentText()))
        lay.addWidget(self.size_box)

        self.bold_btn = self._tool_btn("bold", self._toggle_bold, True, "Bold")
        self.italic_btn = self._tool_btn("italic", self._toggle_italic, True,
                                         "Italic")
        self.underline_btn = self._tool_btn("underline",
                                            self._toggle_underline, True,
                                            "Underline")
        self.strike_btn = self._tool_btn("strike", self._toggle_strike, True,
                                         "Strikethrough")
        for b in (self.bold_btn, self.italic_btn, self.underline_btn,
                  self.strike_btn):
            lay.addWidget(b)

        lay.addWidget(self._tool_btn("color", self._pick_text_color,
                                     tip="Text color"))
        lay.addWidget(self._tool_btn("highlight", self._pick_highlight,
                                     tip="Highlight"))

        lay.addWidget(self._tool_btn(
            "bullets",
            lambda: self._toggle_list(QTextListFormat.Style.ListDisc),
            tip="Bullet list"))
        lay.addWidget(self._tool_btn(
            "numbers",
            lambda: self._toggle_list(QTextListFormat.Style.ListDecimal),
            tip="Numbered list"))
        lay.addWidget(self._tool_btn("outdent",
                                     lambda: self._change_indent(-1),
                                     tip="Outdent"))
        lay.addWidget(self._tool_btn("indent", lambda: self._change_indent(1),
                                     tip="Indent"))

        self.align_left_btn = self._tool_btn(
            "align_left",
            lambda: self._set_alignment(Qt.AlignmentFlag.AlignLeft),
            True, "Align left")
        self.align_center_btn = self._tool_btn(
            "align_center",
            lambda: self._set_alignment(Qt.AlignmentFlag.AlignCenter),
            True, "Align center")
        self.align_right_btn = self._tool_btn(
            "align_right",
            lambda: self._set_alignment(Qt.AlignmentFlag.AlignRight),
            True, "Align right")
        for b in (self.align_left_btn, self.align_center_btn,
                  self.align_right_btn):
            lay.addWidget(b)

        lay.addWidget(self._tool_btn("link", self._insert_link, tip="Link"))
        lay.addWidget(self._tool_btn("image", self._insert_image_dialog,
                                     tip="Insert image"))
        lay.addWidget(self._tool_btn("clear", self._clear_formatting,
                                     tip="Clear formatting"))

        lay.addStretch(1)

        self.spell_btn = QPushButton("ABC✓")
        self.spell_btn.setCheckable(True)
        self.spell_btn.setChecked(self._spell.is_enabled())
        self.spell_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spell_btn.setToolTip("Check spelling while you type")
        self.spell_btn.toggled.connect(self._toggle_spell_check)
        lay.addWidget(self.spell_btn)

        sig_btn = QPushButton("Signature")
        sig_btn.setIcon(editor_icons.icon("sign"))
        sig_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sig_menu = QMenu(sig_btn)
        sig_menu.addAction("Insert signature", self._insert_signature)
        sig_menu.addAction("Edit signature…", self._edit_signature)
        sig_btn.setMenu(sig_menu)
        lay.addWidget(sig_btn)

        return bar

    # ----- formatting actions ----------------------------------------------

    def _merge_format(self, fmt: QTextCharFormat):
        """Apply *fmt* to the selection, or to the current insert position."""
        cursor = self.body_edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self.body_edit.mergeCurrentCharFormat(fmt)
        self.body_edit.setFocus()

    def _set_font_family(self, family: str):
        fmt = QTextCharFormat()
        fmt.setFontFamilies([family])
        self._merge_format(fmt)

    def _set_font_size(self, text: str):
        try:
            size = float(text)
        except ValueError:
            return
        if size <= 0:
            return
        fmt = QTextCharFormat()
        fmt.setFontPointSize(size)
        self._merge_format(fmt)

    def _toggle_bold(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self._merge_format(fmt)

    def _toggle_italic(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontItalic(on)
        self._merge_format(fmt)

    def _toggle_underline(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(on)
        self._merge_format(fmt)

    def _toggle_strike(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(on)
        self._merge_format(fmt)

    def _pick_text_color(self):
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setForeground(color)
            self._merge_format(fmt)

    def _pick_highlight(self):
        color = QColorDialog.getColor(parent=self)
        if color.isValid():
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            self._merge_format(fmt)

    def _toggle_list(self, style: "QTextListFormat.Style"):
        cursor = self.body_edit.textCursor()
        current = cursor.currentList()
        if current and current.format().style() == style:
            current.remove(cursor.block())
            bf = cursor.blockFormat()
            bf.setIndent(0)
            cursor.setBlockFormat(bf)
        else:
            cursor.createList(style)
        self.body_edit.setFocus()

    def _change_indent(self, delta: int):
        cursor = self.body_edit.textCursor()
        bf = cursor.blockFormat()
        bf.setIndent(max(0, bf.indent() + delta))
        cursor.setBlockFormat(bf)
        self.body_edit.setFocus()

    def _set_alignment(self, align: Qt.AlignmentFlag):
        self.body_edit.setAlignment(align)
        self._sync_toolbar()
        self.body_edit.setFocus()

    def _insert_link(self):
        url, ok = QInputDialog.getText(self, "Link", "URL:")
        if not ok or not url.strip():
            return
        url = url.strip()
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setFontUnderline(True)
        fmt.setForeground(QColor(theme.GOLD_DEEP))
        cursor = self.body_edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(url, fmt)
        self.body_edit.setFocus()

    def _clear_formatting(self):
        cursor = self.body_edit.textCursor()
        if cursor.hasSelection():
            cursor.setCharFormat(QTextCharFormat())
        self.body_edit.setCurrentCharFormat(QTextCharFormat())
        self.body_edit.setFocus()

    def _insert_image_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Insert image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Image", "Could not load the image")
            return
        self.body_edit.insert_qimage(image)
        self.body_edit.setFocus()

    def _sync_toolbar(self):
        fmt = self.body_edit.currentCharFormat()
        # Reflect the font under the cursor in the family combo (signals
        # blocked so syncing never re-applies the format).
        families = fmt.fontFamilies()
        family = (families[0] if families else "") or self.body_edit.font().family()
        self.font_box.blockSignals(True)
        idx = self.font_box.findText(family, Qt.MatchFlag.MatchFixedString)
        self.font_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.font_box.blockSignals(False)
        self.bold_btn.setChecked(fmt.fontWeight() >= QFont.Weight.Bold)
        self.italic_btn.setChecked(fmt.fontItalic())
        self.underline_btn.setChecked(fmt.fontUnderline())
        self.strike_btn.setChecked(fmt.fontStrikeOut())
        align = self.body_edit.alignment()
        self.align_left_btn.setChecked(
            bool(align & Qt.AlignmentFlag.AlignLeft))
        self.align_center_btn.setChecked(
            bool(align & Qt.AlignmentFlag.AlignHCenter))
        self.align_right_btn.setChecked(
            bool(align & Qt.AlignmentFlag.AlignRight))

    # ----- prefill ---------------------------------------------------------

    # Fixed 720x560 was cramped on a large screen: the editor got a couple of
    # visible lines once the toolbar, recipients and signature took their share.
    # Scale with the main window instead (fall back to the screen), floored so a
    # small display still gets a usable box and capped so it never goes silly.
    MIN_SIZE = (820, 640)
    MAX_SIZE = (1700, 1250)

    def _preferred_size(self) -> tuple[int, int]:
        base = self.parent().size() if self.parent() is not None else None
        if base is None or base.width() < 400 or base.height() < 300:
            screen = self.screen() or QGuiApplication.primaryScreen()
            geo = screen.availableGeometry()
            base_w, base_h = geo.width(), geo.height()
        else:
            base_w, base_h = base.width(), base.height()
        # ~72% of the app window — clearly bigger than half, still not a
        # full-screen takeover of what is meant to be a message on top.
        w = int(base_w * 0.88)
        h = int(base_h * 0.90)
        w = max(self.MIN_SIZE[0], min(w, self.MAX_SIZE[0]))
        h = max(self.MIN_SIZE[1], min(h, self.MAX_SIZE[1]))
        return w, h

    def _prefill(self, original: EmailMessage | None, mode: str):
        if not original or mode == "new":
            return
        date_str = format_local_date(original.date)
        # The original is quoted in FULL at send time (template + images intact)
        # — the QTextEdit editor can't render complex marketing templates, so we
        # keep the editor clean and let the recipient's real mail client render
        # the original below the reply.
        self._quote = (original, date_str)
        if mode in ("reply", "reply_all"):
            self.to_edit.setText(parseaddr(original.sender)[1])
            self.subject_edit.setText(make_reply_subject(original.subject))
            if mode == "reply_all":
                own = ""
                acc_id = self.from_box.currentData()
                for a in self.db.list_accounts():
                    if a.id == acc_id:
                        own = a.email
                        break
                # Reply-All (Outlook-style): keep the original To/Cc split.
                to, cc = reply_all_recipients(
                    original.sender, original.recipients, original.cc, own)
                self.to_edit.setText(to)
                self.cc_edit.setText(cc)
        elif mode == "forward":
            self.subject_edit.setText(make_forward_subject(original.subject))
            self._attach_forwarded_files(original)
        self._build_reply_body()
        self._set_quote_indicator(original, date_str)

    def _build_reply_body(self):
        """Clean reply/forward body: an empty typing area at the top (cursor
        lands here) then the signature. The quoted original is appended in full
        on send, not shown in the editor (QTextEdit mangles rich templates)."""
        cursor = self.body_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        build_typing_room(cursor)
        sig_html = self._current_signature_html()
        if sig_html:
            insert_paragraph_html(cursor, "-- ")
            insert_paragraph_html(cursor, sig_html)
        else:
            sig = self._current_signature()
            if sig:
                insert_paragraph_html(cursor, "-- ")
                insert_paragraph_html(
                    cursor, html.escape(sig).replace("\n", "<br>"))
            img_path = self._current_sig_image()
            if img_path:
                image = QImage(img_path)
                if not image.isNull():
                    self.body_edit.insert_qimage(
                        image, cursor, target_width=self._current_sig_image_width())
                    # A real paragraph break, for the same reason as above.
                    cursor.insertBlock()
        top = self.body_edit.textCursor()
        top.movePosition(QTextCursor.MoveOperation.Start)
        self.body_edit.setTextCursor(top)

    def _set_quote_indicator(self, original: EmailMessage, date_str: str):
        if self._quote_label is None:
            return
        sender = html.escape(original.sender or "the original sender")
        self._quote_label.setText(
            f"↪  Replying to <b>{sender}</b> — their message "
            f"({html.escape(date_str)}) is quoted in full below your reply.")
        self._quote_row_widget.show()
        # Reply/forward: the pane now holds the bar, so let it be exactly that
        # tall — no empty slab under the editor while the trail is collapsed.
        self._trail_pane.setMaximumHeight(
            self._quote_row_widget.sizeHint().height())

    def _make_trail_view(self):
        """The widget that renders the quoted conversation — the reading pane's
        own reader, so the trail looks exactly like the email did. Created only
        when the user opens the trail (a web view is expensive), and isolated in
        this one method so tests can swap it for a plain widget."""
        from .reader import Reader
        view = Reader()
        view.setMinimumHeight(160)
        return view

    def _toggle_trail(self):
        """Show/hide the quoted conversation inline under the reply editor."""
        # Track open/closed ourselves: isVisible() is False for every child of a
        # dialog that hasn't been shown yet, which would break the first toggle.
        if self._trail_open:
            # Remember how the user had dragged the divider, then give ALL the
            # space back to the editor. Without this the splitter kept the old
            # 55/45 split and left a big empty gap where the trail had been.
            self._trail_sizes = self._body_split.sizes()
            self._trail_open = False
            self._trail_host.hide()
            # Clamp the pane to just the "replying to" bar, otherwise the
            # splitter still reserves a slab of empty space under the editor.
            self._trail_pane.setMaximumHeight(
                self._quote_row_widget.sizeHint().height())
            self._body_split.setSizes([self._body_split.height() or 1000, 0])
            self._trail_btn.setText("⌄  Show trail")
            return
        if self._trail_view is None and getattr(self, "_quote", None):
            original, _date_str = self._quote
            self._trail_view = self._make_trail_view()
            self._trail_host.layout().addWidget(self._trail_view)
            self._trail_view.show_message(original)
        self._trail_open = True
        self._trail_pane.setMaximumHeight(16777215)   # QWIDGETSIZE_MAX
        self._trail_host.show()
        self._trail_btn.setText("⌃  Hide trail")
        # Give the trail a real share of the window (roughly 45%), draggable
        # from there like Outlook's reading pane. Qt reads these as ratios, so
        # they hold whatever the current window height is. Reopening restores
        # whatever split the user last dragged to.
        self._body_split.setSizes(getattr(self, "_trail_sizes", None) or [55, 45])

    def _open_original(self):
        """Show the original message (the one being replied to / forwarded) in a
        read-only viewer, so the user can re-read it while composing.

        Full-fidelity QWebEngine reader (renders marketing templates exactly like
        the main pane). This is safe because reply/forward compose is opened
        NON-modally (see MainWindow._compose_from): a second QWebEngineView only
        deadlocks QtWebEngine on macOS when created inside a modal dialog's nested
        exec() loop; with a non-modal parent it initialises on the normal main
        event loop."""
        if not getattr(self, "_quote", None):
            return
        original, _ = self._quote
        # Reuse the viewer if it's still open; WA_DeleteOnClose may have deleted
        # the C++ object while this attribute still points at it, so guard the
        # stale wrapper and recreate.
        existing = getattr(self, "_orig_dlg", None)
        if existing is not None:
            try:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                self._orig_dlg = None
        from .reader import Reader
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Original — {original.subject or ''}".strip(" —"))
        dlg.resize(720, 600)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        reader = Reader()
        reader.attachment_clicked.connect(self._save_quoted_attachment)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(reader)
        reader.show_message(original)
        dlg.destroyed.connect(self._on_orig_dlg_closed)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        # Keep a reference so the (parentless-for-GC) Python wrapper survives.
        self._orig_dlg = dlg

    def _on_orig_dlg_closed(self, *_):
        self._orig_dlg = None

    # ----- drafts -----------------------------------------------------------

    def _draft_fields(self) -> dict | None:
        """What would be saved, or None when there is nothing worth keeping.

        A reply starts life with a signature and a quote already in the box, so
        "has the user written anything?" is judged on the recipients, the
        subject, attachments, and the text ABOVE the signature.
        """
        acc_id = self.from_box.currentData()
        if acc_id is None:
            return None
        to = self.to_edit.text().strip()
        cc = self.cc_edit.text().strip()
        bcc = self.bcc_edit.text().strip()
        subject = self.subject_edit.text().strip()
        typed = self.body_edit.toPlainText()
        sig = self._current_signature() or ""
        if sig:
            typed = typed.replace(sig, "")
        typed = typed.replace("--", "").strip()
        if not (to or cc or bcc or subject or typed or self._attachments):
            return None
        html_body, inline = self._export_inline_images(self.body_edit.toHtml())
        if inline:
            attachments = json.dumps({"files": self._attachments,
                                      "inline": inline})
        else:
            attachments = json.dumps(self._attachments)
        return {"account_id": acc_id, "to_addrs": self.to_edit.text(),
                "cc": cc, "bcc": bcc, "subject": self.subject_edit.text(),
                "body": self.body_edit.toPlainText(),
                "attachments": attachments, "body_html": html_body}

    def save_draft(self) -> int | None:
        """Store the message as a draft. Returns the draft's outbox id."""
        if self._sent or not self.autosave_draft:
            return None
        fields = self._draft_fields()
        if fields is None:
            return None
        try:
            self._draft_id = self.db.save_draft(self._draft_id, **fields)
        except Exception:  # noqa: BLE001
            log.exception("compose: could not save the draft")
            return None
        return self._draft_id

    def _autosave(self):
        """Periodic save so a crash or a power cut costs nothing.

        The timer is stopped in done(), so reaching here means the window is
        still open — no need to ask the widget whether it is on screen (a
        minimised or not-yet-shown window still holds the user's text).
        """
        if self._sent:
            return
        if self.save_draft() is not None:
            self.draft_saved.emit()

    def changeEvent(self, event):
        # Minimising is what sends this window out of sight, so tell the main
        # window each time the state flips: its footer chip is the only way back.
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.state_changed.emit()

    def showEvent(self, event):
        # The native window only exists once shown, so detach it from its parent
        # here: without this macOS keeps compose as a child sheet and disables
        # the minimise button. Harmless on Windows/Linux.
        super().showEvent(event)
        if not self._transient_cleared:
            self._transient_cleared = True
            handle = self.windowHandle()
            if handle is not None:
                handle.setTransientParent(None)

    def done(self, result):
        # Both Cancel (reject) and the window's X route through done(). Close the
        # original-message viewer here — while this dialog's modal event loop is
        # still running — so its Chromium-backed Reader is torn down cleanly
        # instead of during modal unwinding (which froze the whole app).
        dlg = getattr(self, "_orig_dlg", None)
        if dlg is not None:
            self._orig_dlg = None
            dlg.close()
        self._draft_timer.stop()
        # Closing a half-written message must not throw it away — that is the
        # whole point of a Drafts folder.
        if not self._sent and self.save_draft() is not None:
            self.draft_saved.emit()
        super().done(result)

    def _save_quoted_attachment(self, path: str):
        """The original-message viewer (opened during a reply) had an attachment
        clicked — open it through the main window's preview/save flow if available,
        else fall back to the OS default app."""
        mw = self.parent()
        while mw is not None and not hasattr(mw, "_open_attachment"):
            mw = mw.parent()
        if mw is not None:
            mw._open_attachment(path)
        elif os.path.isfile(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _attach_forwarded_files(self, original: EmailMessage):
        """Carry the original message's real attachments into the forward so they
        go out with it. They show as removable chips, so the user can drop any
        they don't want before sending."""
        files = forwarded_attachment_paths(original.attachments)
        if not files:
            return
        for p in files:
            if p not in self._attachments:
                self._attachments.append(p)
        self.attach_area.set_files(self._attachments)

    def _quote_for_send(self) -> tuple[str, str, list[dict]]:
        """Build the quoted-original block for the OUTGOING message: (html, plain,
        inline_images). The recipient's client renders the original template
        faithfully, so we keep its HTML intact (only sanitized)."""
        if not getattr(self, "_quote", None):
            return "", "", []
        original, date_str = self._quote
        if original.body_html:
            body = sanitize_email_html(original.body_html)
        else:
            body = html.escape(original.body_text or "").replace("\n", "<br>")
        q_html = _quote_header_block(original, date_str, body)
        q_plain = make_quote(original, date_str)
        inline: list[dict] = []
        try:
            items = json.loads(original.attachments or "[]")
        except Exception:
            items = []
        for item in (items if isinstance(items, list) else []):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("cid", ""))
            path = str(item.get("path", ""))
            if cid and path and os.path.isfile(path):
                inline.append({"cid": cid, "path": path})
        return q_html, q_plain, inline

    def _prefill_outbox(self, outbox_id: int):
        """Load an existing outbox row into the editor for re-editing."""
        row = self.db.get_outbox(outbox_id)
        if row is None:
            return
        idx = self.from_box.findData(row["account_id"])
        if idx >= 0:
            self.from_box.setCurrentIndex(idx)
        self.to_edit.setText(row["to_addrs"] or "")
        self.cc_edit.setText(row["cc"] or "")
        self.bcc_edit.setText(row["bcc"] or "")
        self.subject_edit.setText(row["subject"] or "")

        parsed = json.loads(row["attachments"] or "[]")
        if isinstance(parsed, dict):
            files = parsed.get("files", [])
            inline = parsed.get("inline", [])
        else:
            files, inline = parsed, []
        self._attachments = [f for f in files if os.path.isfile(f)]
        self.attach_area.set_files(self._attachments)

        html = row["body_html"] or ""
        if html:
            # Restore inline images: re-register each cid as an img:// document
            # resource and rewrite the refs, so they both display now and get
            # re-exported correctly on send.
            for meta in inline:
                cid = meta.get("cid")
                path = meta.get("path")
                if cid and path and os.path.isfile(path):
                    image = QImage(path)
                    if not image.isNull():
                        self.body_edit.document().addResource(
                            QTextDocument.ResourceType.ImageResource,
                            QUrl(f"img://{cid}"), image)
                        html = html.replace(f"cid:{cid}", f"img://{cid}")
            self.body_edit.setHtml(html)
            # Drafts and outbox rows written before the paragraph fix carry
            # 12px margins that would come straight back on reopening.
            normalise_block_spacing(self.body_edit.document())
        else:
            self.body_edit.setPlainText(row["body"] or "")

    # ----- signature -------------------------------------------------------

    def _current_account(self):
        acc_id = self.from_box.currentData()
        for a in self.db.list_accounts():
            if a.id == acc_id:
                return a
        return None

    def _current_signature(self) -> str:
        acc = self._current_account()
        return (acc.signature or "") if acc else ""

    def _current_signature_html(self) -> str:
        """Rich (HTML) signature for the current account, or '' if unset."""
        acc = self._current_account()
        return (getattr(acc, "signature_html", "") or "") if acc else ""

    def _current_sig_image(self) -> str:
        """Path to the current account's signature image, or '' if unset/missing."""
        acc = self._current_account()
        path = getattr(acc, "signature_image", "") if acc else ""
        return path if path and os.path.isfile(path) else ""

    def _append_plain(self, text: str):
        cursor = self.body_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.setCharFormat(QTextCharFormat())
        cursor.insertText(text)

    def _remove_tail(self, n_chars: int):
        cursor = self.body_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.movePosition(QTextCursor.MoveOperation.Left,
                            QTextCursor.MoveMode.KeepAnchor, n_chars)
        cursor.removeSelectedText()

    def _current_sig_image_width(self) -> int:
        """Return the configured insertion width for the current account's signature image.

        0 stored means use the default (320px).
        """
        acc = self._current_account()
        if acc is None:
            return 320
        w = getattr(acc, "signature_image_width", 0) or 0
        return w if w > 0 else 320

    def _insert_sig_content(self, text_block: str):
        """Append the signature text block and, if set, the signature image.

        The image is inserted through the same img:// resource mechanism as
        pasted images, so it flows through the cid: pipeline on send.
        The image is scaled to account.signature_image_width (default 320) but
        never upscaled beyond its natural size.
        """
        cursor = self.body_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.setCharFormat(QTextCharFormat())
        sig_html = self._current_signature_html()
        if sig_html:
            # Rich signature: insert as HTML (file:// images inside it are
            # converted to cid: parts on send by _export_inline_images).
            # Each part gets its own paragraph, so the line the user is
            # writing never ends up sharing a block with the signature.
            insert_paragraph_html(cursor, "-- ")
            insert_paragraph_html(cursor, sig_html)
            return
        cursor.insertText(text_block)
        img_path = self._current_sig_image()
        if img_path:
            image = QImage(img_path)
            if not image.isNull():
                cursor.insertText("\n")
                target_w = self._current_sig_image_width()
                self.body_edit.insert_qimage(image, cursor, target_width=target_w)

    def _insert_tracked_signature(self, text_block: str):
        cursor = self.body_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._sig_start_pos = cursor.position()
        self._insert_sig_content(text_block)
        self._sig_snapshot = self.body_edit.toPlainText()[self._sig_start_pos:]
        self._sig_block = text_block

    def _clear_sig_tracking(self):
        self._sig_start_pos = None
        self._sig_snapshot = ""
        self._sig_block = ""

    def _remove_tracked_signature(self) -> bool:
        """Remove the tracked signature (text + image). True if removed.

        If the tail of the document no longer matches what we inserted, the
        user edited at/below the signature — leave their text alone. (A
        signature image edited around may then be left behind; accepted edge
        case.)
        """
        if self._sig_start_pos is None:
            return False
        body = self.body_edit.toPlainText()
        if body[self._sig_start_pos:] != self._sig_snapshot:
            return False
        cursor = self.body_edit.textCursor()
        cursor.setPosition(self._sig_start_pos)
        cursor.movePosition(QTextCursor.MoveOperation.End,
                            QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        return True

    def _apply_signature(self, initial=False):
        sig = self._current_signature()
        has_img = bool(self._current_sig_image())
        has_rich = bool(self._current_signature_html())
        new_block = f"\n\n-- \n{sig}" if (sig or has_img or has_rich) else ""
        if initial:
            if new_block:
                self._insert_tracked_signature(new_block)
            return
        if self._sig_start_pos is not None:
            if self._remove_tracked_signature():
                if new_block:
                    self._insert_tracked_signature(new_block)
                else:
                    self._clear_sig_tracking()
            else:
                # User edited the signature region — stop tracking it.
                self._clear_sig_tracking()
            return
        # No tracked position (legacy path): plain-text endswith heuristic.
        body = self.body_edit.toPlainText()
        if self._sig_block and body.endswith(self._sig_block):
            self._remove_tail(len(self._sig_block))
            if new_block:
                self._insert_tracked_signature(new_block)
            else:
                self._clear_sig_tracking()
        elif not self._sig_block and new_block:
            self._insert_tracked_signature(new_block)
        else:
            self._clear_sig_tracking()

    def _on_from_changed(self, _idx):
        if getattr(self, "_reply_mode", False):
            return
        self._apply_signature()

    def _insert_signature(self):
        sig = self._current_signature()
        has_img = bool(self._current_sig_image())
        if not sig and not has_img and not self._current_signature_html():
            QMessageBox.warning(
                self, "Signature",
                "No signature set for this account — create one via 'Edit signature…'")
            return
        self._insert_sig_content(f"\n-- \n{sig}")
        self.body_edit.setFocus()

    def _edit_signature(self):
        acc_id = self.from_box.currentData()
        if acc_id is None:
            QMessageBox.warning(self, "Signature", "Add an account first")
            return
        from .account_settings import AccountSettingsDialog
        AccountSettingsDialog(self.db, acc_id, self).exec()

    # ----- attachments -----------------------------------------------------

    def _files_total_bytes(self) -> int:
        total = 0
        for p in self._attachments:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        return total

    def _pick_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        self._add_attachments(paths)

    # -- drag & drop --------------------------------------------------
    @staticmethod
    def _dropped_files(event) -> list:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls()
                if u.isLocalFile() and os.path.isfile(u.toLocalFile())]

    def dragEnterEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._dropped_files(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = self._dropped_files(event)
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        self._attach_dropped_files(paths)

    def _attach_dropped_files(self, paths: list):
        added = self._add_attachments(paths)
        if added:
            self._flash_status(f"📎 {added} file(s) attached")

    def _attach_pasted_emails(self, paths: list):
        """A copied email was pasted — attach the .eml instead of inlining it."""
        added = self._add_attachments(paths)
        if added:
            self._flash_status(f"📎 {added} email(s) attached")

    def _flash_status(self, text: str) -> None:
        win = self.parent()
        while win is not None and not hasattr(win, "statusBar"):
            win = win.parent()
        if win is not None:
            win.statusBar().showMessage(text, 4000)

    def _add_attachments(self, paths) -> int:
        """Attach *paths*, honouring the size cap. Returns how many were added."""
        total = self._files_total_bytes()
        added = 0
        for p in paths:
            if not p or p in self._attachments:
                continue
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            if total + size > MAX_ATTACH_BYTES:
                QMessageBox.warning(
                    self, "Attachment too large",
                    f"Attachments can total at most "
                    f"{MAX_ATTACH_BYTES // (1024 * 1024)} MB per email.\n\n"
                    f"“{os.path.basename(p)}” would go over the limit, so it "
                    f"wasn't added.\n\nFor bigger files, share a Google Drive / "
                    f"WeTransfer link in the email instead.")
                continue
            self._attachments.append(p)
            total += size
            added += 1
        self.attach_area.set_files(self._attachments)
        return added

    def _remove_attachment(self, path: str):
        if path in self._attachments:
            self._attachments.remove(path)
        self.attach_area.set_files(self._attachments)

    def _preview_attachment(self, path: str):
        """Chip clicked — show the file so the user can confirm they picked the
        right one before sending. No path validation here (unlike the reader):
        these are files the user chose themselves, not ones an email supplied."""
        if not os.path.isfile(path):
            QMessageBox.warning(
                self, "Attachment",
                "That file is no longer on disk — remove it and attach again.")
            return
        from .attachment_preview import AttachmentPreviewDialog
        AttachmentPreviewDialog(path, self, allow_download=False).exec()

    # ----- send ------------------------------------------------------------

    def _send(self):
        if self.from_box.currentData() is None:
            QMessageBox.warning(self, "Send", "Add an account first")
            return
        if self._files_total_bytes() > MAX_ATTACH_BYTES:
            QMessageBox.warning(
                self, "Attachments too large",
                f"Total attachments exceed "
                f"{MAX_ATTACH_BYTES // (1024 * 1024)} MB. Remove some files or "
                f"share a download link instead.")
            return
        bulk = self.bulk_btn.isChecked()
        if bulk:
            rcpts = _bulk_rcpts(self.to_edit.text())
        else:
            rcpts = parse_rcpts(self.to_edit.text(), self.cc_edit.text(),
                                self.bcc_edit.text())
        if not rcpts:
            QMessageBox.warning(self, "Send", "Add at least one recipient")
            return
        if not self.subject_edit.text().strip():
            ans = QMessageBox.question(self, "Send",
                                       "Send without a subject?",
                                       QMessageBox.StandardButton.Yes
                                       | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        if bulk:
            ans = QMessageBox.question(
                self, "Bulk send",
                f"{len(rcpts)} separate emails will be queued — send?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        html, inline = self._export_inline_images(self.body_edit.toHtml())
        body_text = self.body_edit.toPlainText()
        # Append the quoted original in full (template + inline images intact) so
        # the recipient's client renders it faithfully.
        q_html, q_plain, q_inline = self._quote_for_send()
        if q_html:
            html += q_html
            body_text += q_plain
            seen = {i.get("cid") for i in inline if isinstance(i, dict)}
            inline.extend(i for i in q_inline if i.get("cid") not in seen)
        if inline:
            attachments_json = json.dumps(
                {"files": self._attachments, "inline": inline})
        else:
            # Backward-compatible plain-list format when no inline images.
            attachments_json = json.dumps(self._attachments)
        acc_id = self.from_box.currentData()
        # Gmail-style Undo Send: hold the message in the Outbox for a few seconds
        # so it can be recalled. The queued row id(s) are exposed on the dialog so
        # the caller can offer an Undo toast (see MainWindow).
        from ..settings import SendSettings
        delay = SendSettings().undo_send_seconds
        self.queued_outbox_ids = []
        if self.edit_outbox_id is not None:
            # Editing an existing row: overwrite it and re-queue (clears any
            # prior failure/rejection so it sends — or re-enters approval — anew).
            self.db.update_outbox(self.edit_outbox_id, acc_id,
                                  self.to_edit.text(), self.cc_edit.text(),
                                  self.bcc_edit.text(), self.subject_edit.text(),
                                  body_text,
                                  attachments_json, body_html=html,
                                  delay_seconds=delay)
            self.queued_outbox_ids.append(self.edit_outbox_id)
        elif bulk:
            # Each recipient gets their own separate email — CC/BCC ignored.
            for rcpt in rcpts:
                self.queued_outbox_ids.append(self.db.queue_outbox(
                    acc_id, rcpt, "", "", self.subject_edit.text(),
                    body_text, attachments_json, body_html=html,
                    delay_seconds=delay))
        else:
            self.queued_outbox_ids.append(self.db.queue_outbox(
                acc_id, self.to_edit.text(), self.cc_edit.text(),
                self.bcc_edit.text(), self.subject_edit.text(),
                body_text, attachments_json, body_html=html,
                delay_seconds=delay))
        self._sent = True
        if self.edit_outbox_id is None and self._draft_id is not None:
            # This window's draft became a real send — drop the draft row.
            self.db.delete_outbox(self._draft_id)
            self._draft_id = None
        self.accept()

    def _export_inline_images(self, html: str) -> tuple[str, list[dict]]:
        """Save img:// document resources to disk and rewrite them to cid: refs.

        Returns (rewritten_html, [{"cid": hex, "path": str}, ...]).
        """
        inline: list[dict] = []
        doc = self.body_edit.document()
        for hexid in sorted(set(re.findall(r"img://([0-9a-f]{32})", html))):
            res = doc.resource(QTextDocument.ResourceType.ImageResource,
                               QUrl(f"img://{hexid}"))
            image = res if isinstance(res, QImage) else QImage()
            if image.isNull():
                continue
            inline_dir = config.data_dir() / "inline"
            inline_dir.mkdir(parents=True, exist_ok=True)
            path = inline_dir / f"{hexid}.png"
            if not image.save(str(path), "PNG"):
                continue
            html = html.replace(f'src="img://{hexid}"', f'src="cid:{hexid}"')
            inline.append({"cid": hexid, "path": str(path)})
        # Local file refs (e.g. images embedded in a rich signature) → cid too.
        html, file_inline = export_file_images(html, config.data_dir() / "inline")
        inline.extend(file_inline)
        return html, inline
