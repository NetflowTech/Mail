"""Attachment preview dialog.

When the user clicks an attachment in the reader we no longer save it straight
away — we open this dialog first so they can *look* at the file and then decide
whether to download (save a copy) it.

Supported previews:
  • images (png/jpg/jpeg/gif/bmp/webp) — scaled pixmap
  • PDF                                — QtWebEngine's built-in PDF viewer
  • text-ish (txt/csv/log/md/json/xml/ini/py/...) — first chunk as plain text
Everything else falls back to a "no preview" card with the file-type badge.

The dialog never writes anything: the Download button emits `download_requested`
so the owner (main window) keeps the save-location logic and the security
check on the source path.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QMargins, QUrl, Qt, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPalette, QPixmap
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from . import theme
from .attachment_chips import file_thumb, human_size, type_color

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
_TEXT_EXTS = (".txt", ".csv", ".log", ".md", ".json", ".xml", ".ini",
              ".cfg", ".conf", ".py", ".js", ".html", ".htm", ".css",
              ".yml", ".yaml", ".tsv")
_TEXT_PREVIEW_BYTES = 200_000  # don't slurp a giant log into a text box


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _image_preview(path: str) -> QWidget:
    pix = QPixmap(path)
    label = QLabel()
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    if pix.isNull():
        return _fallback_preview(path, "Couldn't load this image")
    # Cap to a sane on-screen size; the scroll area handles anything bigger.
    if pix.width() > 900 or pix.height() > 640:
        pix = pix.scaled(900, 640, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    label.setPixmap(pix)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
    scroll.setWidget(label)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    return scroll


def _pdf_preview(path: str) -> QWidget:
    """Render the PDF with QtPdf (QPdfView + QPdfDocument).

    QtPdf ships with PySide6 on macOS AND Windows, renders pages directly with
    pdfium, and needs no Chromium/HTML viewer — so it's reliable cross-platform
    where the WebEngine built-in PDF viewer came up blank.
    """
    try:
        from PySide6.QtPdf import QPdfDocument
        from PySide6.QtPdfWidgets import QPdfView
    except Exception:
        return _fallback_preview(path, "PDF preview isn't available")
    doc = QPdfDocument()
    if doc.load(path) != QPdfDocument.Error.None_:
        return _fallback_preview(path, "Couldn't open this PDF")
    view = QPdfView()
    view.setDocument(doc)
    view.setPageMode(QPdfView.PageMode.MultiPage)
    view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
    # QPdfView paints its own sunken frame and a near-black backdrop. Both
    # fight the dialog's hairline frame, so drop the frame and sit the pages
    # on the theme's panel grey instead.
    view.setFrameShape(QPdfView.Shape.NoFrame)
    view.setDocumentMargins(QMargins(14, 14, 14, 14))
    # QPdfView paints its backdrop from the palette, not from QSS, so recolour
    # the roles it actually uses.
    pal = view.palette()
    backdrop = QColor(theme.PANEL or "#F5F6F8")
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Window,
                 QPalette.ColorRole.Dark, QPalette.ColorRole.Mid,
                 QPalette.ColorRole.Shadow):
        pal.setColor(role, backdrop)
    view.setPalette(pal)
    view.viewport().setPalette(pal)
    view.viewport().setAutoFillBackground(True)
    # Keep the document alive for the view's lifetime (no Python GC mid-scroll).
    view._doc = doc
    return view


def _text_preview(path: str) -> QWidget:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(_TEXT_PREVIEW_BYTES + 1)
    except OSError:
        return _fallback_preview(path, "Couldn't read this file")
    truncated = len(data) > _TEXT_PREVIEW_BYTES
    if truncated:
        data = data[:_TEXT_PREVIEW_BYTES] + "\n\n… (preview truncated)"
    box = QPlainTextEdit()
    box.setReadOnly(True)
    box.setPlainText(data)
    box.setFrameShape(QPlainTextEdit.Shape.NoFrame)
    # The dialog's own hairline frames this; a second border plus the focus
    # ring on top of it read as two nested boxes.
    box.setStyleSheet("QPlainTextEdit { font-family: 'SF Mono', Menlo, Consolas,"
                      " monospace; font-size: 12px; border: none;"
                      " background: transparent; padding: 10px 12px; }")
    return box


def _fallback_preview(path: str, note: str = "No preview available") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.setSpacing(10)

    badge = QLabel()
    badge.setPixmap(file_thumb(path, size=72))
    badge.setAlignment(Qt.AlignmentFlag.AlignCenter)

    text = QLabel(note)
    text.setAlignment(Qt.AlignmentFlag.AlignCenter)
    text.setStyleSheet(f"color: {theme.TEXT_DIM or '#70757D'}; font-size: 13px;")

    hint = QLabel("Open it in another app to check the contents.")
    hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
    hint.setStyleSheet(f"color: {theme.TEXT_FAINT or '#9AA0A8'}; font-size: 11px;")

    lay.addWidget(badge)
    lay.addWidget(text)
    lay.addWidget(hint)
    return w


def _build_preview(path: str) -> QWidget:
    ext = _ext(path)
    if not os.path.isfile(path):
        return _fallback_preview(path, "This attachment is no longer available")
    if ext in _IMAGE_EXTS:
        return _image_preview(path)
    if ext == ".pdf":
        return _pdf_preview(path)
    if ext in _TEXT_EXTS:
        return _text_preview(path)
    return _fallback_preview(path)


class AttachmentPreviewDialog(QDialog):
    """Preview an attachment; let the user download or open it.

    `download_requested(path)` fires when Download is clicked — the owner does
    the actual (validated) save. Open uses the OS default app on the cached copy.
    """

    download_requested = Signal(str)

    def __init__(self, path: str, parent=None, allow_download: bool = True):
        super().__init__(parent)
        self._path = path
        name = os.path.basename(path)
        self.setWindowTitle(name or "Attachment")
        self.resize(760, 620)

        # Header: the file's own thumbnail (same one the compose chips use, so
        # the vocabulary matches), its name, then type and size on a quieter
        # second line. The document itself is the hero; the chrome recedes.
        thumb = QLabel()
        thumb.setPixmap(file_thumb(path, 32))
        thumb.setFixedSize(32, 32)

        title = QLabel(name)
        title.setObjectName("previewName")

        try:
            size_text = human_size(os.path.getsize(path))
        except OSError:
            size_text = ""
        kind = _ext(path).lstrip(".").upper()
        meta = QLabel(" · ".join(t for t in (kind, size_text) if t))
        meta.setObjectName("previewMeta")

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        text_col.addWidget(title)
        text_col.addWidget(meta)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(thumb)
        header.addLayout(text_col, 1)

        self._body = body = _build_preview(path)
        frame = QFrame()
        frame.setObjectName("previewFrame")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(1, 1, 1, 1)
        frame_lay.addWidget(body)

        rule = QFrame()
        rule.setObjectName("previewRule")
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFixedHeight(1)

        # Actions on the file sit left; the dialog's own verbs sit right, which
        # is where the eye goes to leave.
        print_btn = QPushButton("Print")
        print_btn.setToolTip("Print this attachment")
        print_btn.clicked.connect(self._print)

        open_btn = QPushButton("Open")
        open_btn.setToolTip("Open in the default app")
        open_btn.clicked.connect(self._open_external)

        # An attachment the user just picked already lives on their disk, so
        # the composer opens this without a Download button.
        download_btn = QPushButton("Download")
        download_btn.setObjectName("primary")
        download_btn.setDefault(True)
        download_btn.clicked.connect(self._on_download)
        download_btn.setVisible(allow_download)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(print_btn)
        buttons.addWidget(open_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        buttons.addWidget(download_btn)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(0)
        lay.addLayout(header)
        lay.addSpacing(14)
        lay.addWidget(frame, 1)
        lay.addSpacing(14)
        lay.addWidget(rule)
        lay.addSpacing(12)
        lay.addLayout(buttons)

    def _on_download(self):
        self.download_requested.emit(self._path)

    def _open_external(self):
        if os.path.isfile(self._path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _print(self):
        """Show a print preview of the attachment (PDF pages / image / text),
        then let the user print. Unsupported types fall back to opening in the
        OS default app, which can then print."""
        from . import print_util
        ext = _ext(self._path)
        if ext == ".pdf":
            if print_util.show_pdf_preview(self, self._path, os.path.basename(self._path)):
                return
        elif ext in _IMAGE_EXTS:
            if print_util.show_image_preview(self, self._path, os.path.basename(self._path)):
                return
        elif ext in _TEXT_EXTS:
            from PySide6.QtWidgets import QPlainTextEdit
            if isinstance(self._body, QPlainTextEdit):
                print_util.show_text_preview(self, self._body.toPlainText(),
                                             os.path.basename(self._path))
                return
        # Unsupported / failed → hand off to the OS default app.
        self._open_external()
