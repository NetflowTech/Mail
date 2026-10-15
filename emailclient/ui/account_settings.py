from __future__ import annotations

import os
import uuid

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import (QColor, QImage, QIntValidator, QPixmap,
                           QTextCharFormat, QTextCursor, QTextImageFormat)
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSlider,
                               QTabWidget, QVBoxLayout, QWidget)

from .. import config, credentials, google_oauth, oauth
from ..db import Database
from . import editor_icons, theme
from .add_account_dialog import SYNC_DEPTH_OPTIONS
from .compose import ImageContextBar, _MAX_INLINE_WIDTH, _RichBody

_PREVIEW_MAX = 360   # max preview width (px) shown inside the dialog
_SIG_IMG_DEFAULT_W = 320   # default insertion width when slider is at 0


class SignatureBody(_RichBody):
    """Rich signature editor: paste/drop images + double-click resize.

    Unlike the compose body, pasted images are saved to disk and inserted as
    file:// references — the stored signature_html must survive across compose
    documents, where img:// resources would dangle.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Rendered-image URL -> full-resolution source file, so resizing can
        # re-render crisply from the original instead of upscaling a thumbnail.
        self._img_sources: dict[str, str] = {}

    def _dpr(self) -> float:
        """The screen's device-pixel ratio (2.0 on Retina)."""
        try:
            scr = self.screen()
            return max(1.0, float(scr.devicePixelRatio())) if scr else 2.0
        except Exception:
            return 2.0

    def _render_display(self, src_image: QImage, src_path: str,
                        logical_w: int) -> tuple[str, int]:
        """Save a display-resolution PNG (logical_w × the screen's DPR pixels,
        smoothly scaled) and return its file URL + the logical width. Drawing it
        1:1 at the device resolution keeps it crisp instead of QTextEdit having
        to up-scale a low-res image (the cause of blur on Retina)."""
        logical_w = max(16, int(logical_w))
        target_px = max(1, round(logical_w * self._dpr()))
        render = (src_image.scaledToWidth(
                      target_px, Qt.TransformationMode.SmoothTransformation)
                  if src_image.width() > target_px else src_image)
        sig_dir = config.data_dir() / "sig_images"
        sig_dir.mkdir(parents=True, exist_ok=True)
        out = sig_dir / f"{uuid.uuid4().hex}.png"
        if not render.save(str(out), "PNG"):
            return "", logical_w
        url = QUrl.fromLocalFile(str(out)).toString()
        self._img_sources[url] = src_path
        return url, logical_w

    def insert_qimage(self, image: QImage, cursor: QTextCursor | None = None,
                      target_width: int | None = None):
        sig_dir = config.data_dir() / "sig_images"
        sig_dir.mkdir(parents=True, exist_ok=True)
        # Keep the full-resolution original so later resizes stay sharp.
        src_path = sig_dir / f"{uuid.uuid4().hex}_src.png"
        if not image.save(str(src_path), "PNG"):
            return
        logical = int(target_width) if target_width else min(
            image.width(), _MAX_INLINE_WIDTH)
        url, logical = self._render_display(image, str(src_path), logical)
        if not url:
            return
        (cursor if cursor is not None else self.textCursor()).insertHtml(
            f'<img src="{url}" width="{logical}">')

    def _source_image_at(self, img_pos: int):
        """Crop/operate on the full-resolution original, not the display render."""
        fmt = self.image_format_at(img_pos)
        if fmt is not None:
            src = self._img_sources.get(fmt.name())
            if src and os.path.isfile(src):
                img = QImage(src)
                if not img.isNull():
                    return img
        return super()._source_image_at(img_pos)

    def resize_image_at(self, doc_pos: int, width: int) -> bool:
        """Re-render the image from its full-res source at the new size (crisp),
        falling back to the base behaviour for images we have no source for."""
        fmt = self.image_format_at(doc_pos)
        if fmt is not None:
            src = self._img_sources.get(fmt.name())
            if src and os.path.isfile(src):
                img = QImage(src)
                if not img.isNull():
                    url, logical = self._render_display(img, src, width)
                    if url:
                        new_fmt = QTextImageFormat()
                        new_fmt.setName(url)
                        new_fmt.setWidth(float(logical))
                        if img.width() > 0 and img.height() > 0:
                            new_fmt.setHeight(round(logical * img.height() / img.width()))
                        cur = QTextCursor(self.document())
                        cur.setPosition(doc_pos)
                        cur.movePosition(QTextCursor.MoveOperation.Right,
                                         QTextCursor.MoveMode.KeepAnchor)
                        cur.setCharFormat(new_fmt)
                        return True
        return super().resize_image_at(doc_pos, width)


class AccountSettingsDialog(QDialog):
    """Tabbed per-account settings: General / Connection / Sync & Notifications."""

    # The connection test runs in a thread; results come back through this.
    _test_done = Signal(dict)

    def __init__(self, db: Database, account_id: int, parent=None):
        super().__init__(parent)
        self._test_done.connect(self._on_test_done)
        self.db = db
        self.account_id = account_id
        # Flags read by MainWindow after exec()
        self.password_changed = False
        self.sync_depth_changed = False
        self.connection_changed = False
        self.pause_changed = False
        self.pause_value = False
        self.color_changed = False

        # Load account
        accounts = {a.id: a for a in self.db.list_accounts()}
        acc = accounts[account_id]
        self._acc = acc
        self._email = acc.email
        self._provider = acc.provider

        self.setWindowTitle(f"Account Settings — {acc.email}")
        # Resizable; the Save/Cancel bar stays pinned outside the scroll area so
        # it is always reachable. Start small enough to fit short screens and
        # never open taller than the available screen height.
        self.setMinimumSize(520, 380)
        screen = self.screen()
        avail = screen.availableGeometry() if screen else None
        max_h = avail.height() - 80 if avail else 640
        # Wider by default so signatures with wide images/banners fit without
        # horizontal scrolling; capped to the screen width.
        max_w = avail.width() - 80 if avail else 820
        self.resize(min(820, max_w), max(380, min(640, max_h)))

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(16, 16, 16, 16)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scrollable(self._build_general_tab(acc)),
                         "General")
        self.tabs.addTab(self._scrollable(self._build_connection_tab(acc)),
                         "Connection")
        # "&&" because Qt treats a single "&" as a mnemonic underline
        self.tabs.addTab(self._scrollable(self._build_sync_tab(acc)),
                         "Sync && Notifications")
        layout.addWidget(self.tabs, 1)

        # Buttons
        buttons = QDialogButtonBox()
        save_btn = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save_btn.setObjectName("primary")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        """Wrap a tab so its content scrolls on short screens instead of
        pushing the dialog (and the Save button) off-screen."""
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        sa.setWidget(inner)
        return sa

    # ----- Tab 1: General ---------------------------------------------------

    def _build_general_tab(self, acc) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(acc.name)
        layout.addWidget(self.name_edit)

        email_label = QLabel(acc.email)
        email_label.setEnabled(False)
        layout.addWidget(email_label)

        # Per-account accent color (the account's dot / label / chip). Empty =
        # the app theme accent, so all accounts look consistent by default;
        # pick a custom color here to colour-code an account.
        self._acc_color = getattr(acc, "color", "") or ""
        color_row = QHBoxLayout()
        color_row.addWidget(QLabel("Account color:"))
        self._color_swatch = QPushButton()
        self._color_swatch.setFixedSize(80, 26)
        self._color_swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_swatch.setToolTip("Pick a custom color for this account")
        self._color_swatch.clicked.connect(self._pick_account_color)
        color_row.addWidget(self._color_swatch)
        self._color_state = QLabel("")
        self._color_state.setObjectName("guide")
        color_row.addWidget(self._color_state)
        self._color_reset = QPushButton("Use theme default")
        self._color_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_reset.clicked.connect(self._reset_account_color)
        color_row.addWidget(self._color_reset)
        color_row.addStretch(1)
        layout.addLayout(color_row)
        self._update_color_swatch()

        # Rich signature editor + mini toolbar
        layout.addWidget(QLabel("Signature:"))
        bar = QHBoxLayout()
        bar.setSpacing(4)
        for icon_name, tip, slot, checkable in (
                ("bold", "Bold", self._sig_bold, True),
                ("italic", "Italic", self._sig_italic, True),
                ("underline", "Underline", self._sig_underline, True),
                ("link", "Link (select text first)", self._sig_link, False),
                ("image", "Insert image", self._sig_insert_image, False),
                ("color", "Text color", self._sig_text_color, False)):
            b = QPushButton()
            b.setIcon(editor_icons.icon(icon_name))
            b.setFixedWidth(34)
            b.setCheckable(checkable)
            b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            if checkable:
                b.toggled.connect(slot)
            else:
                b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        clear_btn = QPushButton("🗑 Clear")
        clear_btn.setToolTip("Remove the entire signature")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.clicked.connect(self._clear_signature)
        bar.addWidget(clear_btn)
        layout.addLayout(bar)

        # Contextual image controls (layout + size) — appears under the toolbar
        # only while an image in the signature is selected.
        self.sig_img_bar = ImageContextBar()
        layout.addWidget(self.sig_img_bar)

        self.sig_edit = SignatureBody()
        self.sig_edit.setAcceptRichText(True)
        self.sig_edit.setPlaceholderText("Your name\nCompany | phone")
        self.sig_edit.setMinimumHeight(120)
        sig_html = getattr(acc, "signature_html", "") or ""
        if sig_html:
            self.sig_edit.setHtml(sig_html)
        else:
            self.sig_edit.setPlainText(acc.signature)
        self.sig_img_bar.bind(self.sig_edit)
        layout.addWidget(self.sig_edit)

        guide = QLabel("This signature is added automatically at the bottom of every email you send.")
        guide.setObjectName("guide")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        layout.addSpacing(6)

        # Legacy signature image row (used when rich signature is plain text)
        layout.addWidget(QLabel("Signature image:"))
        self._sig_image = getattr(acc, "signature_image", "") or ""
        stored_w = getattr(acc, "signature_image_width", 0) or 0
        self._sig_img_width = stored_w if stored_w > 0 else _SIG_IMG_DEFAULT_W

        self.sig_img_label = QLabel()
        choose_btn = QPushButton("📷 Choose…")
        choose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        choose_btn.clicked.connect(self._choose_sig_image)
        remove_btn = QPushButton("❌ Remove")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(self._remove_sig_image)
        img_row = QHBoxLayout()
        img_row.addWidget(self.sig_img_label, 1)
        img_row.addWidget(choose_btn)
        img_row.addWidget(remove_btn)
        layout.addLayout(img_row)

        # Live preview of the signature image
        self.sig_img_preview = QLabel()
        self.sig_img_preview.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.sig_img_preview.setMinimumHeight(60)
        self.sig_img_preview.setWordWrap(False)
        layout.addWidget(self.sig_img_preview)

        # Width slider row (also feeds the 🖼 insert width)
        layout.addWidget(QLabel("Image width in email:"))
        slider_row = QHBoxLayout()
        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setMinimum(80)
        self.width_slider.setMaximum(640)
        self.width_slider.setValue(self._sig_img_width)
        self.width_px_label = QLabel(f"{self._sig_img_width}px")
        self.width_px_label.setMinimumWidth(50)
        self.width_slider.valueChanged.connect(self._on_width_changed)
        slider_row.addWidget(self.width_slider, 1)
        slider_row.addWidget(self.width_px_label)
        layout.addLayout(slider_row)

        self._refresh_sig_img_label()
        layout.addStretch(1)
        return tab

    # -- rich signature toolbar slots --

    def _sig_merge(self, fmt: QTextCharFormat):
        cursor = self.sig_edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        self.sig_edit.mergeCurrentCharFormat(fmt)
        self.sig_edit.setFocus()

    def _sig_bold(self, on: bool):
        from PySide6.QtGui import QFont
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self._sig_merge(fmt)

    def _sig_italic(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontItalic(on)
        self._sig_merge(fmt)

    def _sig_underline(self, on: bool):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(on)
        self._sig_merge(fmt)

    def _sig_link(self):
        url, ok = QInputDialog.getText(self, "Link", "URL:")
        if not ok or not url.strip():
            return
        url = url.strip()
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setFontUnderline(True)
        fmt.setForeground(QColor(theme.GOLD_DEEP))
        cursor = self.sig_edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
        else:
            cursor.insertText(url, fmt)
        self.sig_edit.setFocus()

    def _sig_insert_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Signature image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Image", "Could not load that image.")
            return
        # Go through insert_qimage so the picture is stored at the screen's pixel
        # density (crisp on Retina) and stays sharp when resized.
        self.sig_edit.insert_qimage(image, target_width=self.width_slider.value())
        self.sig_edit.setFocus()

    # ----- per-account color -------------------------------------------------

    def _update_color_swatch(self):
        is_default = not self._acc_color
        eff = self._acc_color or theme.GOLD
        self._color_swatch.setStyleSheet(
            f"background:{eff}; border:1px solid {theme.BORDER}; border-radius:5px;")
        self._color_state.setText("Theme default" if is_default else eff)
        self._color_reset.setEnabled(not is_default)

    def _pick_account_color(self):
        initial = QColor(self._acc_color or theme.GOLD)
        color = QColorDialog.getColor(initial, self, "Account color")
        if color.isValid():
            self._acc_color = color.name()
            self._update_color_swatch()

    def _reset_account_color(self):
        self._acc_color = ""
        self._update_color_swatch()

    def _clear_signature(self):
        """Wipe the whole signature (text + images) after confirmation."""
        has_content = (self.sig_edit.toPlainText().strip()
                       or "<img" in self.sig_edit.toHtml())
        if has_content:
            ans = QMessageBox.question(
                self, "Clear signature",
                "Remove the entire signature?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        self.sig_edit.clear()
        if hasattr(self.sig_edit, "_img_sources"):
            self.sig_edit._img_sources.clear()
        self.sig_img_bar.hide()
        self.sig_edit.setFocus()

    def _sig_text_color(self):
        color = QColorDialog.getColor(parent=self)
        if not color.isValid():
            return
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        self._sig_merge(fmt)

    # ----- Tab 2: Connection -------------------------------------------------

    def _build_connection_tab(self, acc) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        self._orig_conn = (acc.imap_host, str(acc.imap_port),
                           acc.smtp_host, str(acc.smtp_port))
        port_validator = QIntValidator(1, 65535, self)

        layout.addWidget(QLabel("IMAP server:"))
        self.imap_host_edit = QLineEdit(acc.imap_host)
        self.imap_port_edit = QLineEdit(str(acc.imap_port))
        self.imap_port_edit.setValidator(port_validator)
        self.imap_port_edit.setFixedWidth(80)
        imap_row = QHBoxLayout()
        imap_row.addWidget(self.imap_host_edit, 1)
        imap_row.addWidget(QLabel("Port:"))
        imap_row.addWidget(self.imap_port_edit)
        layout.addLayout(imap_row)

        layout.addWidget(QLabel("SMTP server:"))
        self.smtp_host_edit = QLineEdit(acc.smtp_host)
        self.smtp_port_edit = QLineEdit(str(acc.smtp_port))
        self.smtp_port_edit.setValidator(port_validator)
        self.smtp_port_edit.setFixedWidth(80)
        smtp_row = QHBoxLayout()
        smtp_row.addWidget(self.smtp_host_edit, 1)
        smtp_row.addWidget(QLabel("Port:"))
        smtp_row.addWidget(self.smtp_port_edit)
        layout.addLayout(smtp_row)

        layout.addSpacing(8)

        self.pw_edit = None
        if acc.provider == "outlook":
            # Microsoft servers are fixed — show them but don't allow edits.
            for w in (self.imap_host_edit, self.imap_port_edit,
                      self.smtp_host_edit, self.smtp_port_edit):
                w.setReadOnly(True)
                w.setEnabled(False)
            relogin_btn = QPushButton("🔁 Sign in to Microsoft again")
            relogin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            relogin_btn.clicked.connect(self._relogin_outlook)
            layout.addWidget(relogin_btn)
            pw_guide = QLabel("Login expired? Sign in again here —"
                              " the account reconnects automatically.")
        else:
            if acc.provider == "gmail":
                # Always offered for gmail: switches the account to (or renews)
                # browser sign-in; app-password accounts can keep using the
                # password field below instead.
                grelogin_btn = QPushButton("🔁 Sign in with Google again")
                grelogin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
                grelogin_btn.clicked.connect(self._relogin_google)
                layout.addWidget(grelogin_btn)
                g_guide = QLabel("Login expired? Sign in with Google here — or"
                                 " enter your password below (optional).")
                g_guide.setObjectName("guide")
                g_guide.setWordWrap(True)
                layout.addWidget(g_guide)
                layout.addSpacing(8)
            # Direct account password for every provider — the app just hands
            # it to IMAP/SMTP LOGIN; no special "app password" wording.
            pw_label = "Password:"
            pw_word = "password"
            layout.addWidget(QLabel(pw_label))
            self.pw_edit = QLineEdit()
            self.pw_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self.pw_edit.setPlaceholderText(f"Enter new {pw_word} to change it")
            show_pw = QPushButton("👁")
            show_pw.setCheckable(True)
            show_pw.setFixedWidth(40)
            show_pw.setCursor(Qt.CursorShape.PointingHandCursor)
            show_pw.setToolTip("Show / hide password")
            show_pw.toggled.connect(
                lambda on: self.pw_edit.setEchoMode(
                    QLineEdit.EchoMode.Normal if on
                    else QLineEdit.EchoMode.Password))
            pw_row = QHBoxLayout()
            pw_row.addWidget(self.pw_edit, 1)
            pw_row.addWidget(show_pw)
            layout.addLayout(pw_row)
            pw_guide = QLabel("Wrong or expired password? Enter the new one here"
                              " and Save — the account reconnects automatically.")
        pw_guide.setObjectName("guide")
        pw_guide.setWordWrap(True)
        layout.addWidget(pw_guide)

        # Try the settings BEFORE saving them: a wrong host, port or password
        # otherwise only shows up as a red dot in the sidebar minutes later.
        layout.addSpacing(10)
        self.test_btn = QPushButton("🔌 Test connection")
        self.test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.test_btn.setToolTip(
            "Log in with the settings above (nothing is sent or saved)")
        self.test_btn.clicked.connect(self._test_connection)
        layout.addWidget(self.test_btn)
        self.test_result = QLabel("")
        self.test_result.setObjectName("guide")
        self.test_result.setWordWrap(True)
        self.test_result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.test_result)

        layout.addStretch(1)
        return tab

    # ----- connection test ---------------------------------------------------

    def _test_connection(self):
        """Check IMAP + SMTP with what is typed in right now, off the GUI
        thread so the window keeps painting while servers are slow."""
        import threading
        from .. import conntest
        acc = next((a for a in self.db.list_accounts()
                    if a.id == self.account_id), None)
        if acc is None:
            return
        try:
            imap_port = int(self.imap_port_edit.text() or 0)
            smtp_port = int(self.smtp_port_edit.text() or 0)
        except ValueError:
            self.test_result.setText("⚠ Ports must be numbers.")
            return
        imap_host = self.imap_host_edit.text().strip()
        smtp_host = self.smtp_host_edit.text().strip()
        if not imap_host or not smtp_host or not imap_port or not smtp_port:
            self.test_result.setText("⚠ Fill in both servers and their ports.")
            return
        password = self.pw_edit.text() if self.pw_edit is not None else ""

        self.test_btn.setEnabled(False)
        self.test_btn.setText("🔌 Testing…")
        self.test_result.setText("Connecting…")

        def work():
            try:
                res = conntest.check_account(acc, imap_host, imap_port,
                                             smtp_host, smtp_port,
                                             password or None)
            except Exception as exc:  # noqa: BLE001
                res = {"imap": (False, str(exc)), "smtp": (False, str(exc))}
            try:
                self._test_done.emit(res)
            except RuntimeError:
                pass          # dialog closed while the test was running

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, res: dict):
        self.test_btn.setEnabled(True)
        self.test_btn.setText("🔌 Test connection")
        imap_ok, imap_msg = res.get("imap", (False, "not run"))
        smtp_ok, smtp_msg = res.get("smtp", (False, "not run"))
        lines = [f"{'✅' if imap_ok else '❌'} IMAP: {imap_msg}",
                 f"{'✅' if smtp_ok else '❌'} SMTP: {smtp_msg}"]
        if imap_ok and smtp_ok:
            lines.append("Both work — press Save to keep these settings.")
        self.test_result.setText("\n".join(lines))

    # ----- Tab 3: Sync & Notifications ----------------------------------------

    def _build_sync_tab(self, acc) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        layout.addWidget(QLabel("How far back to sync emails:"))
        self._orig_sync_depth = getattr(acc, "sync_depth", "50") or "50"
        self.sync_depth_combo = QComboBox()
        for label, value in SYNC_DEPTH_OPTIONS:
            self.sync_depth_combo.addItem(label, value)
        idx = self.sync_depth_combo.findData(self._orig_sync_depth)
        if idx >= 0:
            self.sync_depth_combo.setCurrentIndex(idx)
        layout.addWidget(self.sync_depth_combo)

        layout.addSpacing(8)

        self._orig_attachments_only = bool(getattr(acc, "attachments_only", False))
        self.attachments_only_check = QCheckBox(
            "📎 Only save emails that have attachments")
        self.attachments_only_check.setChecked(self._orig_attachments_only)
        layout.addWidget(self.attachments_only_check)
        att_guide = QLabel("Note: the filter only applies to newly arriving emails;"
                           " bandwidth is still used.")
        att_guide.setObjectName("guide")
        att_guide.setWordWrap(True)
        layout.addWidget(att_guide)

        layout.addSpacing(8)

        self._orig_notify_enabled = bool(getattr(acc, "notify_enabled", True))
        self.notify_check = QCheckBox("🔔 Notifications for this account")
        self.notify_check.setChecked(self._orig_notify_enabled)
        layout.addWidget(self.notify_check)

        layout.addSpacing(8)

        self._orig_paused = bool(getattr(acc, "paused", False))
        self.pause_check = QCheckBox("⏸ Account paused")
        self.pause_check.setChecked(self._orig_paused)
        layout.addWidget(self.pause_check)

        layout.addStretch(1)
        return tab

    # ----- shared helpers (signature image / preview) -------------------------

    def _refresh_sig_img_label(self):
        self.sig_img_label.setText(
            os.path.basename(self._sig_image) if self._sig_image
            else "(no image)")
        self._refresh_preview()

    def _refresh_preview(self):
        """Update the live preview pixmap to match current image + slider width."""
        if self._sig_image and os.path.isfile(self._sig_image):
            px = QPixmap(self._sig_image)
            if not px.isNull():
                preview_w = min(self._sig_img_width, _PREVIEW_MAX)
                if px.width() > preview_w:
                    px = px.scaledToWidth(
                        preview_w,
                        Qt.TransformationMode.SmoothTransformation)
                self.sig_img_preview.setPixmap(px)
                return
        # No image or file missing
        self.sig_img_preview.setPixmap(QPixmap())
        self.sig_img_preview.setText("")

    def _on_width_changed(self, value: int):
        self._sig_img_width = value
        self.width_px_label.setText(f"{value}px")
        self._refresh_preview()

    def _choose_sig_image(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Signature image", "",
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if path:
            self._sig_image = path
            self._refresh_sig_img_label()

    def _remove_sig_image(self):
        self._sig_image = ""
        self._refresh_sig_img_label()

    def _relogin_google(self):
        try:
            google_oauth.login_interactive(self._email)
        except Exception as e:
            QMessageBox.warning(self, "Google Login",
                                f"Could not sign in: {e}")
            return
        self.password_changed = True
        QMessageBox.information(self, "Google Login",
                                "Signed in ✅ — the account reconnects"
                                " as soon as you Save.")

    def _relogin_outlook(self):
        try:
            oauth.login_interactive(self._email)
        except Exception as e:
            QMessageBox.warning(self, "Microsoft Login",
                                f"Could not sign in: {e}")
            return
        self.password_changed = True
        QMessageBox.information(self, "Microsoft Login",
                                "Signed in ✅ — the account reconnects"
                                " as soon as you Save.")

    # ----- save ----------------------------------------------------------------

    def _save(self):
        name = self.name_edit.text().strip() or None
        # Rich signature: HTML for compose insertion, plain text for the
        # text/plain part + legacy fallback.
        sig_plain = self.sig_edit.toPlainText()
        sig_html = self.sig_edit.toHtml() if sig_plain.strip() or "<img" in self.sig_edit.toHtml() else ""
        self.db.update_account(self.account_id, name=name, signature=sig_plain,
                               signature_html=sig_html,
                               signature_image=self._sig_image,
                               signature_image_width=self._sig_img_width,
                               color=self._acc_color)
        if self._acc_color != (getattr(self._acc, "color", "") or ""):
            self.color_changed = True

        # Connection changes (gmail/zoho only — outlook fields are read-only)
        if self._provider != "outlook":
            new_conn = (self.imap_host_edit.text().strip(),
                        self.imap_port_edit.text().strip(),
                        self.smtp_host_edit.text().strip(),
                        self.smtp_port_edit.text().strip())
            if all(new_conn) and new_conn != self._orig_conn:
                self.db.update_account(self.account_id,
                                       imap_host=new_conn[0],
                                       imap_port=int(new_conn[1]),
                                       smtp_host=new_conn[2],
                                       smtp_port=int(new_conn[3]))
                self.connection_changed = True

        new_depth = self.sync_depth_combo.currentData() or "50"
        if new_depth != self._orig_sync_depth:
            self.db.update_account(self.account_id, sync_depth=new_depth)
            self.sync_depth_changed = True

        new_att_only = self.attachments_only_check.isChecked()
        if new_att_only != self._orig_attachments_only:
            self.db.update_account(self.account_id, attachments_only=new_att_only)
            # Reuse the resync path so the worker restarts with the new filter.
            self.sync_depth_changed = True

        new_notify = self.notify_check.isChecked()
        if new_notify != self._orig_notify_enabled:
            self.db.update_account(self.account_id, notify_enabled=new_notify)

        new_paused = self.pause_check.isChecked()
        if new_paused != self._orig_paused:
            self.pause_changed = True
            self.pause_value = new_paused

        if self.pw_edit is not None and self.pw_edit.text().strip():
            # Strip all whitespace (incl. non-breaking spaces from copy-pasted
            # Gmail app passwords) — they break IMAP LOGIN with ascii encoding.
            credentials.set_password(self._email,
                                     "".join(self.pw_edit.text().split()))
            self.password_changed = True
        self.accept()
