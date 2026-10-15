from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
import uuid
from email.utils import parseaddr
from pathlib import Path

from PySide6.QtCore import (QBuffer, QByteArray, QIODevice, QSize, Qt, QUrl,
                            Signal)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ..models import EmailMessage
from . import editor_icons, theme
from .attachment_chips import file_thumb, type_color
from .textrender import plain_to_html


def _thumb_data_uri(path: str, size: int = 22) -> str:
    """A base64 PNG data URI of the file-type badge / image thumbnail for *path*,
    so the reader's HTML attachment chips can show a real PDF/Excel/Word icon
    (or an image preview) instead of a plain colored dot.

    Returns "" when no GUI QApplication exists (e.g. headless unit tests): QPixmap
    needs a QGuiApplication and would otherwise abort the process. The chip then
    falls back to the colored dot.
    """
    from PySide6.QtGui import QGuiApplication
    if not isinstance(QGuiApplication.instance(), QGuiApplication):
        return ""
    pm = file_thumb(path, size=size)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pm.save(buf, "PNG")
    buf.close()
    b64 = bytes(ba.toBase64()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def _reader_css() -> str:
    """Per-message CSS from the CURRENT theme — theme switches apply to
    newly opened emails."""
    return f"""
body {{ margin:0; background:{theme.CONTENT_BG}; }}
/* The header sits on the same white as the body so the message reads as one
   sheet; a hairline is all that separates them. */
.mail-header {{
  font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  background:{theme.CONTENT_BG}; border-bottom:1px solid {theme.CONTENT_BORDER}; padding:20px 24px 16px;
}}
.mail-header .subject {{
  font-size:19px; font-weight:650; color:{theme.CONTENT_TEXT};
  line-height:1.3; letter-spacing:-0.01em;
}}
.mail-header .meta-row {{ margin-top:14px; display:flex; align-items:flex-start; }}
.mail-header .avatar {{
  width:36px; height:36px; border-radius:18px; margin-right:12px;
  background:{theme.CONTENT_PANEL}; color:#ffffff;
  font-size:15px; font-weight:600; line-height:36px; text-align:center;
  flex:none;
}}
.mail-header .meta {{ flex:1; min-width:0; }}
/* Sender left, date right on one baseline: the eye reads who, then when. */
.mail-header .from {{
  display:flex; justify-content:space-between; align-items:baseline;
  gap:14px; font-size:13px; color:{theme.CONTENT_TEXT}; font-weight:600;
}}
.mail-header .from .who {{ overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.mail-header .from .addr {{
  color:{theme.CONTENT_DIM}; font-weight:400; margin-left:6px;
}}
.mail-header .from .when {{
  font-size:11px; color:{theme.CONTENT_FAINT}; font-weight:400; white-space:nowrap;
}}
.mail-header .line {{
  font-size:12px; color:{theme.CONTENT_DIM}; margin-top:3px;
  overflow:hidden; text-overflow:ellipsis;
}}
.mail-header .line .lbl {{ color:{theme.CONTENT_FAINT}; }}
.mail-body {{
  font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;
  font-size:14px; line-height:1.6; color:{theme.CONTENT_TEXT};
  padding:20px 24px 32px; max-width:760px;
  word-wrap:break-word; overflow-wrap:break-word;
}}
.mail-body blockquote {{
  margin:8px 0 8px 0; padding:2px 0 2px 14px;
  border-left:3px solid {theme.CONTENT_BORDER}; color:{theme.CONTENT_DIM};
}}
.mail-body a {{ color:{theme.CONTENT_ACCENT}; }}
/* max-width keeps big images from overflowing, but do NOT force height:auto —
   that overrides emails' explicit height="..." (e.g. App Store / Google Play
   badges set height="40px") and blows small images up to full size. */
.mail-body img {{ max-width:100%; }}
.mail-body pre {{ white-space:pre-wrap; font-family:inherit; }}
.mail-body table {{ max-width:100%; }}
.mail-body p {{ margin:0 0 12px; }}
"""


_CID_MAX_BYTES = 5 * 1024 * 1024  # skip huge inline images: avoid giant HTML

# Only non-scriptable raster types may be embedded as data URIs.
# image/svg+xml is intentionally excluded — SVG can contain scripts.
_SAFE_IMAGE_MIMES = frozenset({
    "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/bmp", "image/tiff",
})


def resolve_cid_images(body_html: str, attachments_json: str) -> str:
    """Replace <img src="cid:..."> references with embedded data: URIs.

    The webview blocks file:// loads (LocalContentCanAccessFileUrls is off),
    so inline images must be inlined as base64 data URIs."""
    try:
        items = json.loads(attachments_json)
        assert isinstance(items, list)
    except Exception:
        return body_html

    def data_uri_for(item) -> str | None:
        path = Path(str(item["path"]))
        if not path.is_file() or path.stat().st_size > _CID_MAX_BYTES:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        if mime not in _SAFE_IMAGE_MIMES:
            return None  # never embed scriptable/non-image content
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'src="data:{mime};base64,{b64}"'

    leftover_images = []  # image items with no cid (pre-cid-era rows)
    for item in items:
        try:
            cid = str(item.get("cid", ""))
            if not cid:
                leftover_images.append(item)
                continue
            uri = data_uri_for(item)
            if not uri:
                continue
            body_html = body_html.replace(f'src="cid:{cid}"', uri)
            body_html = body_html.replace(f"src='cid:{cid}'", uri)
        except Exception:
            continue  # one bad item must not break rendering

    # Fallback for messages synced before cid tracking existed: map any
    # remaining cid: references to this message's own image attachments
    # in order — the common signature-image case resolves correctly.
    remaining = re.findall(r"src=[\"\'](cid:[^\"\']+)[\"\']", body_html)
    for ref, item in zip(remaining, leftover_images):
        try:
            uri = data_uri_for(item)
            if not uri:
                continue
            body_html = body_html.replace(f'src="{ref}"', uri)
            body_html = body_html.replace(f"src='{ref}'", uri)
        except Exception:
            continue
    return body_html


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_IMG_SRC_RE = re.compile(r"""src\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)


def wrap_body_images(body_html: str) -> tuple[str, list[str]]:
    """Wrap each <img> in the body with a link the reader intercepts, so clicking
    an image opens the native zoom viewer (JS is disabled, so this is the only
    way to make images expandable). Returns (wrapped_html, [src, ...]) where the
    link index maps into the src list."""
    srcs: list[str] = []

    def repl(mtch):
        tag = mtch.group(0)
        sm = _IMG_SRC_RE.search(tag)
        if not sm:
            return tag
        idx = len(srcs)
        srcs.append(sm.group(2))
        return (f'<a href="orostarimg:{idx}" '
                f'style="cursor:zoom-in;text-decoration:none">{tag}</a>')

    return _IMG_TAG_RE.sub(repl, body_html), srcs


def build_message_document(m: EmailMessage, header: str) -> tuple[str, list[str]]:
    """Full HTML document for a message: shell + styles + header + body.
    Returns (html, image_srcs) — image_srcs backs click-to-zoom in the reader."""
    body = m.body_html or plain_to_html(m.body_text or "")
    if m.body_html and m.attachments:
        body = resolve_cid_images(body, m.attachments)
    body, image_srcs = wrap_body_images(body)
    doc = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        f"<style>{_reader_css()}</style></head><body>"
        f"{header}<div class=\"mail-body\">{body}</div></body></html>")
    return doc, image_srcs


def message_header_html(m: EmailMessage, accent: str = "") -> str:
    """The reader's message header block (subject, avatar, from/to, date,
    attachment chips) — shared by the webview reader and the lightweight
    original-message viewer.

    *accent* colours the avatar. Passing the account's own colour makes the
    reader's circle match the one on the message list row, so "which account
    is this" stays readable without re-reading the address."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(m.date)
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # user's local timezone
        when = dt.strftime("%d %b %Y &nbsp;·&nbsp; %H:%M")
    except (ValueError, TypeError):
        when = str(m.date or "")[:16].replace("T", " &nbsp;·&nbsp; ")
    # Every field can be None for some senders — html.escape(None) raises
    # (NoneType has no .replace), which used to leave the reader blank. Coerce
    # to "" everywhere.
    name, addr = parseaddr(m.sender or "")
    display = name or addr or (m.sender or "")
    initial = next((ch.upper() for ch in display
                    if ch.isalpha() or ch.isdigit()), "?")
    avatar_bg = (f" style=\"background:{html.escape(accent)}\""
                 if accent else "")

    def line(label: str, value: str) -> str:
        return (f"<div class=\"line\"><span class=\"lbl\">{label}</span> "
                f"{html.escape(value)}</div>")

    lines = line("to", m.recipients or "")
    if getattr(m, "cc", ""):
        lines += line("cc", m.cc)
    if getattr(m, "bcc", ""):
        lines += line("bcc", m.bcc)
    return (
        "<div class=\"mail-header\">"
        f"<div class=\"subject\">{html.escape(m.subject or '(no subject)')}</div>"
        "<div class=\"meta-row\">"
        f"<div class=\"avatar\"{avatar_bg}>{html.escape(initial)}</div>"
        "<div class=\"meta\">"
        f"<div class=\"from\"><span class=\"who\">{html.escape(display or '')}"
        + (f"<span class=\"addr\">{html.escape(addr)}</span>"
           if addr and addr != display else "")
        + f"</span><span class=\"when\">{when}</span></div>"
        f"{lines}"
        "</div>"
        "</div>"
        f"{_attachment_chips_html(m.attachments) if m.attachments else ''}"
        "</div>")


def _size_human(n: int) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


def _attachment_chips_html(attachments_json: str) -> str:
    """Render attachment chips for the reader header; '' if none/invalid."""
    try:
        items = json.loads(attachments_json)
        assert isinstance(items, list)
    except Exception:
        return ""
    chips = []
    for item in items:
        try:
            # Inline images (referenced in the body) get no chip. Uses the parse
            # -time `inline` flag; older records fall back to 'has a Content-ID'.
            if item.get("inline", bool(item.get("cid"))):
                continue
            name = str(item["name"])
            path = str(item["path"])
            size = _size_human(item.get("size", 0))
        except (TypeError, KeyError):
            continue
        # Build a proper file URL via QUrl so Windows paths ("C:\\...") encode
        # correctly. The old "file://" + quote(path) produced a malformed URL on
        # Windows (the drive became the host), so the click navigated to a broken
        # page (blank reader) instead of firing the save handler.
        href = html.escape(bytes(QUrl.fromLocalFile(path).toEncoded()).decode())
        # File-type icon (PDF / Excel / Word badge, or image thumbnail) by
        # extension, instead of a plain colored dot. Falls back to the dot when
        # no GUI app is available (headless tests).
        uri = _thumb_data_uri(path)
        if uri:
            icon = (f'<img src="{uri}" width="22" height="22" '
                    'style="vertical-align:-6px;margin-right:6px">')
        else:
            icon = f'<span style="color:{type_color(name)}">&#9679;</span> '
        label = f"{icon}{html.escape(name)}" + (f" ({size})" if size else "")
        chips.append(
            f'<a href="{href}" style="display:inline-block;background:{theme.CONTENT_ACCENT_SOFT};'
            f'color:{theme.CONTENT_ACCENT};border-radius:8px;padding:3px 10px;margin:6px 6px 0 0;'
            f'text-decoration:none;font-size:12px">{label}</a>')
    return "<div>" + "".join(chips) + "</div>" if chips else ""


class _MailPage(QWebEnginePage):
    link_activated = Signal(QUrl)   # any link the user clicked
    image_activated = Signal(int)   # a body image the user clicked (zoom index)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame):
        if nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            if url.scheme() == "orostarimg":
                # href="orostarimg:N" — N indexes the reader's image list.
                try:
                    self.image_activated.emit(int(url.toString().split(":", 1)[1]))
                except (ValueError, IndexError):
                    pass
                return False
            self.link_activated.emit(url)
            return False
        return True


class Reader(QWebEngineView):
    attachment_clicked = Signal(str)   # local attachment path the user clicked

    def __init__(self):
        super().__init__()
        self._images: list[str] = []   # body image srcs, for click-to-zoom
        page = _MailPage(self)
        # QueuedConnection is essential: these signals fire from inside
        # QWebEnginePage.acceptNavigationRequest (a Chromium navigation
        # callback). Opening a modal dialog (image zoom / attachment save)
        # synchronously there re-enters the event loop while Chromium's UI
        # thread is blocked waiting for the callback to return — a documented
        # deadlock that froze the whole app when clicking an image or
        # attachment (notably in the reply "View original" viewer). Queuing
        # lets the callback return first; the dialog then opens on the next
        # event-loop tick.
        page.link_activated.connect(self._on_link, Qt.ConnectionType.QueuedConnection)
        page.image_activated.connect(self._on_image, Qt.ConnectionType.QueuedConnection)
        self.setPage(page)
        # Emails are untrusted HTML: never run their scripts. Remote (http) image
        # loading is gated by the user's "Load remote images" setting (default
        # on) — see _apply_remote_images_setting. JavaScript stays disabled, so
        # even with remote images on, only passive sub-resource fetches happen.
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        # Keep local file:// exfiltration blocked (inline images are embedded as
        # data: URIs, so nothing legitimate needs file:// sub-resource access).
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
        self._apply_remote_images_setting()
        # Per-instance temp file: the main pane and any standalone message
        # windows must NOT share one file, or load() (which is async) can read a
        # page another Reader just overwrote — that raced into blank / broken
        # images.
        self._render_id = uuid.uuid4().hex
        self.clear_view()

    def _render(self, html_str: str):
        # Both setHtml and setContent fail to load very large pages (image-heavy
        # emails inline their images as base64 data: URIs, easily 2 MB+ →
        # loadFinished ok=False → blank). Writing the page to a temp file and
        # load()-ing it has no size limit. data: images are self-contained and
        # render fine; remote/file sub-resource access stays disabled.
        try:
            from ..config import data_dir
            path = data_dir() / f"reader_view_{self._render_id}.html"
            path.write_text(html_str, encoding="utf-8")
            self.load(QUrl.fromLocalFile(str(path)))
        except Exception:
            self.setContent(html_str.encode("utf-8"), "text/html;charset=utf-8")

    def _on_link(self, url: QUrl):
        # Attachments are local file:// links → let the app save them; web links
        # open in the system browser.
        if url.isLocalFile():
            self.attachment_clicked.emit(url.toLocalFile())
        else:
            QDesktopServices.openUrl(url)

    def _on_image(self, index: int):
        # A body image was clicked — open it full-size in the zoom viewer.
        if not (0 <= index < len(self._images)):
            return
        from .image_zoom import ImageZoomDialog, pixmap_from_src
        pm = pixmap_from_src(self._images[index])
        if pm.isNull():
            return
        ImageZoomDialog(pm, self).exec()

    def clear_view(self):
        self._render(
            "<html><body style=\"margin:0;height:95vh;display:flex;"
            "align-items:center;justify-content:center;"
            f"background:{theme.PANEL};"
            "font-family:-apple-system,sans-serif\">"
            f"<div style='text-align:center;color:{theme.TEXT_FAINT}'>"
            "<div style='font-size:34px'>&#9993;</div>"
            "<div style='font-size:13px;margin-top:6px'>Select an email to read</div>"
            "</div></body></html>")

    def _apply_remote_images_setting(self) -> None:
        # The page renders from a file:// temp file, so LocalContentCanAccess
        # RemoteUrls actually gates hosted <img> loads. Read the user's pref each
        # render so toggling it in Settings takes effect on the next email.
        try:
            from ..settings import ReaderSettings
            allow = ReaderSettings().load_remote_images
        except Exception:
            allow = True
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, allow)

    def show_message(self, m: EmailMessage, accent: str = ""):
        self._apply_remote_images_setting()
        header = message_header_html(m, accent)
        try:
            doc, self._images = build_message_document(m, header)
            self._render(doc)
        except Exception:
            # Never leave the pane blank: fall back to a minimal safe render.
            self._images = []
            safe = html.escape(m.body_text or "").replace("\n", "<br>")
            self._render(f"<html><body>{header}<div class='mail-body'>{safe}"
                         "</div></body></html>")


class ReaderPane(QWidget):
    """Reader webview with an action bar (reply / forward / delete / spam)."""

    reply_requested = Signal(object)
    reply_all_requested = Signal(object)
    forward_requested = Signal(object)
    delete_requested = Signal(object)
    spam_requested = Signal(object)
    attachment_clicked = Signal(str)

    def __init__(self):
        super().__init__()
        self._current: EmailMessage | None = None
        self._accent = ""
        self.webview = Reader()
        self.webview.attachment_clicked.connect(self.attachment_clicked)

        self._bar = QWidget()
        self._bar.setObjectName("readerBar")
        bar_lay = QHBoxLayout(self._bar)
        bar_lay.setContentsMargins(8, 6, 8, 6)
        bar_lay.setSpacing(6)
        self._buttons = []

        def _btn(text, icon_name, signal):
            b = QPushButton(f" {text}")
            b.setIcon(editor_icons.icon(icon_name))
            b.setIconSize(QSize(16, 16))
            b.clicked.connect(lambda: self._current is not None
                              and signal.emit(self._current))
            self._buttons.append(b)
            return b

        bar_lay.addWidget(_btn("Reply", "reply", self.reply_requested))
        bar_lay.addWidget(_btn("Reply All", "reply_all", self.reply_all_requested))
        bar_lay.addWidget(_btn("Forward", "forward", self.forward_requested))
        bar_lay.addStretch(1)
        print_btn = QPushButton(" Print")
        print_btn.setIcon(editor_icons.icon("print"))
        print_btn.setIconSize(QSize(16, 16))
        print_btn.setToolTip("Print this email (Ctrl+P)")
        print_btn.clicked.connect(self._print_message)
        self._buttons.append(print_btn)
        bar_lay.addWidget(print_btn)
        bar_lay.addWidget(_btn("Delete", "trash", self.delete_requested))
        bar_lay.addWidget(_btn("Spam", "spam", self.spam_requested))
        # The action bar is meaningless with nothing open — hide it until a
        # message is selected (instead of greying the buttons out).
        self._bar.setVisible(False)
        # Remember each label so the bar can go icon-only when it gets narrow.
        # In a standalone message window the bar ran out of room and the last
        # buttons (Print / Delete / Spam) were simply clipped off the edge.
        self._btn_labels = {b: b.text() for b in self._buttons}
        for b in self._buttons:
            if not b.toolTip():
                b.setToolTip(b.text().strip())
        self._compact = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._bar)
        lay.addWidget(self.webview, 1)

    def _full_bar_width(self) -> int:
        """Width the bar needs with every label showing."""
        lay = self._bar.layout()
        m = lay.contentsMargins()
        wide = 0
        for b in self._buttons:
            label = self._btn_labels.get(b, b.text())
            hint = b.sizeHint().width()
            if self._compact:
                # sizeHint is the icon-only width right now — add the label back.
                hint += b.fontMetrics().horizontalAdvance(label) + 8
            wide += hint
        return wide + m.left() + m.right() + lay.spacing() * len(self._buttons)

    def _set_compact(self, compact: bool) -> None:
        if compact == self._compact:
            return
        self._compact = compact
        for b in self._buttons:
            b.setText("" if compact else self._btn_labels.get(b, b.text()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Drop to icons when the labels no longer fit, so every action stays
        # reachable instead of being cut off at the right edge.
        self._set_compact(self.width() < self._full_bar_width() + 24)

    def current_message(self) -> EmailMessage | None:
        """The email on screen, or None — what the keyboard shortcuts act on."""
        return self._current

    def show_message(self, m: EmailMessage, accent: str = ""):
        self._current = m
        self._accent = accent
        self._bar.setVisible(True)
        self.webview.show_message(m, accent)

    def repaint_theme(self) -> None:
        """Re-render with the current palette after a theme change."""
        if self._current is None:
            self.webview.clear_view()
        else:
            self.webview.show_message(self._current, self._accent)

    def print_current(self) -> None:
        """Show a print preview of the open email, then let the user print.

        Public because Ctrl+P (and the standalone message window) reach print
        without going through the toolbar button."""
        if self._current is None:
            return
        from . import print_util
        print_util.show_email_preview(self, self.webview, "Print email")

    # Kept as the button's slot name; both call the same thing.
    _print_message = print_current

    def clear_view(self):
        self._current = None
        self._bar.setVisible(False)
        self.webview.clear_view()


class MessageWindow(QWidget):
    """Standalone window showing a single email — opened by double-clicking a
    message (Outlook-style). Reuses ReaderPane; its reply/forward/delete actions
    are forwarded to the main window so they behave exactly like the main pane."""

    reply_requested = Signal(object)
    reply_all_requested = Signal(object)
    forward_requested = Signal(object)
    delete_requested = Signal(object)
    spam_requested = Signal(object)
    attachment_clicked = Signal(str)

    def __init__(self, message: EmailMessage):
        super().__init__()
        self.setWindowTitle(message.subject or "(no subject)")
        self.resize(840, 660)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.pane = ReaderPane()
        self.pane.reply_requested.connect(self.reply_requested)
        self.pane.reply_all_requested.connect(self.reply_all_requested)
        self.pane.forward_requested.connect(self.forward_requested)
        self.pane.delete_requested.connect(self._forward_delete)
        self.pane.spam_requested.connect(self._forward_spam)
        self.pane.attachment_clicked.connect(self.attachment_clicked)
        lay.addWidget(self.pane)
        self.pane.show_message(message)
        self._install_shortcuts()

    # Same keys as the main window, so a mail opened in its own window behaves
    # identically (Ctrl+P was the one people reached for first).
    def _install_shortcuts(self) -> None:
        from PySide6.QtGui import QKeySequence, QShortcut
        keys = (("Ctrl+P", self.pane.print_current),
                ("Ctrl+R", lambda: self._emit_current(self.reply_requested)),
                ("Ctrl+Shift+R", lambda: self._emit_current(self.reply_all_requested)),
                ("Ctrl+Shift+F", lambda: self._emit_current(self.forward_requested)),
                ("Esc", self.close))
        for seq, slot in keys:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(slot)

    def _emit_current(self, signal) -> None:
        m = self.pane.current_message()
        if m is not None:
            signal.emit(m)

    def _forward_delete(self, m):
        self.delete_requested.emit(m)
        self.close()

    def _forward_spam(self, m):
        self.spam_requested.emit(m)
        self.close()
