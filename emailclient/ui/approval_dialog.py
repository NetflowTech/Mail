"""Admin 'Waiting for Approval' queue.

Lists emails other devices submitted from approval-required accounts. The admin
approves (this device then actually sends via its own SMTP credentials) or
rejects with an optional reason. Only shown when this device is the admin.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading

import html as _html
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QMessageBox,
                               QPushButton, QScrollArea, QTextBrowser,
                               QVBoxLayout, QWidget)

from .. import approval, smtp_sender
from . import theme


_ACTION_LABEL = {
    "approve": "✅ Approved",
    "cloud_send": "☁️ Approved + sent (cloud)",
    "sent": "📤 Sent (admin device)",
    "reject": "⛔ Rejected",
    "send_failed": "⚠️ Send failed",
}


class _HistoryDialog(QDialog):
    """Read-only audit history: which admin approved/rejected/sent which email."""

    _loaded = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Approval History")
        self.resize(760, 540)
        lay = QVBoxLayout(self)
        title = QLabel("📜 Approval History")
        tf = title.font(); tf.setWeight(QFont.Weight.DemiBold)
        tf.setPointSize(tf.pointSize() + 3); title.setFont(tf)
        lay.addWidget(title)
        lay.addWidget(QLabel("Who approved, rejected or sent which email "
                             "(newest first, this license)."))
        self._view = QTextBrowser()
        self._view.setOpenExternalLinks(False)
        lay.addWidget(self._view, 1)
        row = QHBoxLayout(); row.addStretch(1)
        refresh = QPushButton("↻ Refresh"); refresh.clicked.connect(self._load)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        row.addWidget(refresh); row.addWidget(close)
        lay.addLayout(row)
        self._loaded.connect(self._render)
        self._load()

    def _load(self):
        self._view.setHtml("<p style='color:#888;padding:12px'>Loading…</p>")
        import threading

        def work():
            entries = approval.fetch_audit_log()
            try:
                self._loaded.emit(entries)
            except RuntimeError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _fmt_time(self, ts: str) -> str:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%d %b %Y · %H:%M")
        except (ValueError, AttributeError):
            return _html.escape(str(ts))

    def _render(self, entries):
        if not entries:
            self._view.setHtml(
                "<p style='color:#888;padding:12px'>No history yet.</p>")
            return
        rows = []
        for e in entries:
            when = self._fmt_time(e.get("ts", ""))
            admin = _html.escape(str(e.get("admin", "")))
            act = _ACTION_LABEL.get(e.get("action", ""),
                                    _html.escape(str(e.get("action", ""))))
            subj = _html.escape(str(e.get("subject", "") or "(no subject)"))
            acct = _html.escape(str(e.get("account", "")))
            to = _html.escape(str(e.get("to", "")))
            sender = _html.escape(str(e.get("sender", "")))
            detail = _html.escape(str(e.get("detail", "")))
            sender_html = (f"<br><span style='color:#888'>sent by {sender}</span>"
                           if sender else "")
            detail_html = (f"<br><span style='color:#888'>{detail}</span>"
                           if detail else "")
            rows.append(
                f"<tr><td style='white-space:nowrap;color:#666;padding:6px 10px;"
                f"border-bottom:1px solid #eee'>{when}</td>"
                f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>"
                f"<b>{act}</b> &nbsp; <span style='color:#a07c00'>{admin}</span>"
                f"<br><b>{subj}</b>"
                f"<br><span style='color:#666'>from {acct} → {to}</span>"
                f"{sender_html}{detail_html}</td></tr>")
        self._view.setHtml(
            "<table style='width:100%;border-collapse:collapse;"
            "font-family:-apple-system,Segoe UI,sans-serif;font-size:13px'>"
            + "".join(rows) + "</table>")

# Explicit button styles — the approval dialogs set their own stylesheet, so we
# don't rely on the global #primary rule cascading in (it sometimes doesn't,
# which left the Approve button invisible).
_APPROVE_QSS = (
    f"QPushButton {{ background: {theme.GOLD_DEEP}; color: #FFFFFF;"
    f" border: none; border-radius: 8px; padding: 6px 16px; font-weight: 600; }}"
    f" QPushButton:hover {{ background: {theme.GOLD}; }}")
_REJECT_QSS = (
    f"QPushButton {{ background: {theme.BG}; color: {theme.TEXT};"
    f" border: 1px solid {theme.BORDER}; border-radius: 8px; padding: 6px 14px; }}"
    f" QPushButton:hover {{ background: {theme.PANEL}; }}")


class _ItemCard(QFrame):
    def __init__(self, item, on_approve, on_reject, on_open,
                 approve_label="✓ Approve & send"):
        super().__init__()
        self._item = item
        self._on_open = on_open
        self.setObjectName("approvalCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QFrame#approvalCard {{
                background: {theme.BG};
                border: 1px solid {theme.BORDER};
                border-radius: 10px;
            }}
            QFrame#approvalCard:hover {{ border-color: {theme.GOLD}; }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(3)

        subj = QLabel(item.get("subject") or "(no subject)")
        f = subj.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSize(f.pointSize() + 1)
        subj.setFont(f)
        subj.setWordWrap(True)
        lay.addWidget(subj)

        meta = QLabel(f"From account: {item.get('account_email','')}   ·   "
                      f"To: {item.get('to','')}")
        meta.setStyleSheet(f"color: {theme.TEXT_DIM};")
        meta.setWordWrap(True)
        lay.addWidget(meta)

        atts = item.get("attachments") or []
        sender = item.get("sender_label", "")
        extra = f"Sent by: {sender}"
        if item.get("ip"):
            extra += f"   ·   🌐 IP: {item['ip']}"
        if atts:
            extra += f"   ·   📎 {len(atts)} attachment(s)"
        sub = QLabel(extra)
        sub.setStyleSheet(f"color: {theme.TEXT_FAINT}; font-size: 11px;")
        lay.addWidget(sub)

        preview = (item.get("body_text") or "").strip().replace("\n", " ")
        if preview:
            body = QLabel(preview[:240] + ("…" if len(preview) > 240 else ""))
            body.setWordWrap(True)
            body.setStyleSheet(f"color: {theme.TEXT};")
            lay.addWidget(body)

        btns = QHBoxLayout()
        hint = QLabel("Click to open ↗")
        hint.setStyleSheet(f"color: {theme.GOLD_DEEP}; font-size: 11px;")
        btns.addWidget(hint)
        btns.addStretch(1)
        reject = QPushButton("✕ Reject")
        reject.setStyleSheet(_REJECT_QSS)
        reject.setCursor(Qt.CursorShape.PointingHandCursor)
        reject.clicked.connect(lambda: on_reject(item))
        approve = QPushButton(approve_label)
        approve.setStyleSheet(_APPROVE_QSS)   # explicit — don't rely on #primary
        approve.setCursor(Qt.CursorShape.PointingHandCursor)
        approve.clicked.connect(lambda: on_approve(item))
        btns.addWidget(reject)
        btns.addWidget(approve)
        lay.addLayout(btns)

    def mousePressEvent(self, event):
        # Clicks on the buttons are consumed by them; a click anywhere else on
        # the card opens the full email.
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_open(self._item)
        super().mousePressEvent(event)


class ApprovalQueueDialog(QDialog):
    """Admin-only: review and release/reject pending outgoing emails."""

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Waiting for Approval")
        self.resize(620, 600)
        # Force a light surface — unstyled containers fall through to the OS
        # dark palette otherwise.
        self.setStyleSheet(
            f"QDialog {{ background: {theme.BG}; }}"
            f" QScrollArea {{ background: {theme.BG}; border: none; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("⏳ Waiting for Approval")
        f = title.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSize(f.pointSize() + 3)
        title.setFont(f)
        header.addWidget(title, 1)
        history_btn = QPushButton("📜 History")
        history_btn.setToolTip("Who approved/rejected/sent which email")
        history_btn.clicked.connect(self._show_history)
        header.addWidget(history_btn)
        refresh = QPushButton("↻ Refresh")
        refresh.clicked.connect(self.reload)
        header.addWidget(refresh)
        outer.addLayout(header)

        # Admin toggle: send approved emails from the original sender's device
        # (recipient sees the sender's IP) instead of from this admin device.
        _settings = approval.get_settings()
        self._sender_mode = QCheckBox(
            "Send approved emails from the sender's own device "
            "(recipient sees the sender's IP, not yours)")
        self._sender_mode.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self._sender_mode.setChecked(bool(_settings.get("send_from_sender")))
        self._sender_mode.toggled.connect(self._on_sender_mode_toggled)
        outer.addWidget(self._sender_mode)

        # Cloud-send fallback: when the sender's device is offline at approval
        # time, let the SERVER send the approved email using SMTP credentials the
        # sender uploaded. Convenience vs. security trade-off — uploads each
        # account's email password to the server (encrypted at rest).
        self._cloud_send = QCheckBox(
            "If the sender is offline, let the server (cloud) send it — "
            "uploads accounts' email passwords to the server")
        self._cloud_send.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self._cloud_send.setChecked(bool(_settings.get("cloud_send_enabled")))
        self._cloud_send.toggled.connect(self._on_cloud_send_toggled)
        outer.addWidget(self._cloud_send)
        # (Email-deletion lock lives in Settings → Security, set by the admin.)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._inner = QWidget()
        self._inner.setStyleSheet(f"background: {theme.BG};")
        self._scroll.setStyleSheet(f"background: {theme.BG};")
        self._list = QVBoxLayout(self._inner)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(8)
        self._list.addStretch(1)
        self._scroll.setWidget(self._inner)
        outer.addWidget(self._scroll, 1)

        self._empty = QLabel("Nothing waiting — all caught up. ✅")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        outer.addWidget(self._empty)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        outer.addLayout(row)

        self.reload()

    def reload(self):
        # clear existing cards (keep the trailing stretch)
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        items = approval.pending()
        self._empty.setVisible(not items)
        label = self._approve_label()
        for it in items:
            card = _ItemCard(it, self._approve, self._reject, self._open,
                             approve_label=label)
            self._list.insertWidget(self._list.count() - 1, card)

    def _admin_save_failed_msg(self):
        """Show a precise reason an admin save failed: a stale session (admin
        password changed on the server) vs the server being unreachable."""
        if approval.admin_session_state() == "invalid":
            approval.clear_admin()
            QMessageBox.warning(
                self, "Admin session expired",
                "Your admin session is no longer valid — the admin password "
                "was likely changed on the server. Open Settings ▸ Security and "
                "log in again as admin.")
        else:
            QMessageBox.warning(
                self, "Setting",
                "Could not save the setting — the approval server is "
                "unreachable. Check your internet connection and try again.")

    def _on_sender_mode_toggled(self, checked: bool):
        if not approval.set_send_from_sender(checked):
            # revert the checkbox without re-firing the network call
            self._sender_mode.blockSignals(True)
            self._sender_mode.setChecked(not checked)
            self._sender_mode.blockSignals(False)
            self._admin_save_failed_msg()
            return
        self.reload()  # refresh button labels

    def _on_cloud_send_toggled(self, checked: bool):
        if checked:
            ok = QMessageBox.question(
                self, "Enable cloud send",
                "This uploads each account's email password to the server so it "
                "can send approved mail when the sender is offline.\n\n"
                "Passwords are encrypted at rest, but a server compromise could "
                "still expose them. Turning this off later wipes them.\n\n"
                "Enable cloud send?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ok != QMessageBox.StandardButton.Yes:
                self._cloud_send.blockSignals(True)
                self._cloud_send.setChecked(False)
                self._cloud_send.blockSignals(False)
                return
        if not approval.set_cloud_send(checked):
            self._cloud_send.blockSignals(True)
            self._cloud_send.setChecked(not checked)
            self._cloud_send.blockSignals(False)
            self._admin_save_failed_msg()
            return
        if checked:
            # Push every password-based account's creds up now so the feature
            # works immediately (not only for accounts added later).
            self._upload_all_creds()

    def _show_history(self):
        _HistoryDialog(self).exec()

    def _upload_all_creds(self):
        db = getattr(self, "db", None)
        if db is None:
            return
        accts = [a for a in db.list_accounts()
                 if approval.is_password_account(a)]

        def work():
            for a in accts:
                approval.save_creds(a)
        threading.Thread(target=work, daemon=True).start()

    def _approve_label(self) -> str:
        return ("✓ Approve" if self._sender_mode.isChecked()
                else "✓ Approve & send")

    def _open(self, item):
        _ItemDetailDialog(item, self._approve, self._reject, self,
                          approve_label=self._approve_label()).exec()

    # -- actions --
    def _approve(self, item):
        fresh = approval.approve(item["id"])
        if not fresh:
            QMessageBox.warning(self, "Approve",
                                "Could not approve (already handled, or server "
                                "unreachable).")
            self.reload()
            return
        if fresh.get("status") == "sent":
            # Cloud-send: the server already sent it (sender was offline).
            QMessageBox.information(
                self, "Approved",
                "Approved and sent ✅\n\nThe sender was offline, so the server "
                "(cloud) sent it for you.")
            self.reload()
            return
        if fresh.get("status") == "released":
            # "Send from sender" mode: the original sender's device sends it
            # (so the recipient sees the sender's IP). Nothing to send here.
            note = ""
            if fresh.get("error"):
                note = ("\n\nNote: cloud send was tried but failed "
                        f"({fresh['error']}), so it falls back to the sender's "
                        "device.")
            QMessageBox.information(
                self, "Approved",
                "Approved ✅\n\nIt will be sent from the sender's own device, "
                "so the recipient sees the sender's IP — not yours. This happens "
                "the next time the sender's app is online." + note)
            self.reload()
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, err = self._send_approved(fresh)
        finally:
            QApplication.restoreOverrideCursor()
        approval.mark_sent(item["id"], error="" if ok else err)
        if ok:
            QMessageBox.information(self, "Approved",
                                    "Email approved and sent.")
        else:
            QMessageBox.warning(self, "Send failed",
                                f"Approved, but sending failed:\n{err}\n\n"
                                "It stays in the queue to try again.")
        self.reload()

    def _reject(self, item):
        reason, ok = QInputDialog.getText(
            self, "Reject email",
            f"Reject “{item.get('subject') or '(no subject)'}”?\n"
            "Optional reason for the sender:")
        if not ok:
            return
        if approval.reject(item["id"], reason.strip()):
            self.reload()
        else:
            QMessageBox.warning(self, "Reject",
                                "Could not reject (already handled?).")

    def _send_approved(self, item) -> tuple[bool, str]:
        account = next((a for a in self.db.list_accounts()
                        if a.email.lower() == item.get("account_email", "").lower()),
                       None)
        if account is None:
            return False, ("This account isn't added on the admin device, "
                           "so it can't be sent from here.")
        tmpdir = tempfile.mkdtemp(prefix="amail-approve-")
        try:
            paths = []
            for i, a in enumerate(item.get("attachments") or []):
                data = approval.fetch_attachment(item["id"], i)
                if data is None:
                    continue
                # Sanitize to a bare filename — never trust the name for a path.
                import pathlib
                safe = pathlib.Path(a.get("name") or f"file{i}").name
                if not safe or safe.startswith("."):
                    safe = f"file{i}"
                p = os.path.join(tmpdir, safe)
                if not os.path.realpath(p).startswith(
                        os.path.realpath(tmpdir) + os.sep):
                    continue  # path escaped tmpdir — skip
                with open(p, "wb") as fh:
                    fh.write(data)
                paths.append(p)
            try:
                inline_meta = json.loads(item.get("inline_meta") or "[]")
            except (TypeError, ValueError):
                inline_meta = []
            inline_idx = {m["idx"]: m["cid"] for m in inline_meta
                          if "idx" in m and "cid" in m}
            files = [p for i, p in enumerate(paths) if i not in inline_idx]
            inline_images = [(inline_idx[i], p) for i, p in enumerate(paths)
                             if i in inline_idx]
            try:
                msg = smtp_sender.build_mime(
                    account=account, to_addrs=item.get("to", ""),
                    cc=item.get("cc", ""), subject=item.get("subject", ""),
                    body_text=item.get("body_text", ""), attachments=files,
                    body_html=item.get("body_html", ""),
                    inline_images=inline_images)
                rcpts = smtp_sender.parse_rcpts(
                    item.get("to", ""), item.get("cc", ""), item.get("bcc", ""))
                smtp_sender.send(account, msg, rcpts)
                return True, ""
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class _ItemDetailDialog(QDialog):
    """Full view of one pending email: headers + body + attachments, with the
    same Approve/Reject actions. Opened by clicking a card."""

    def __init__(self, item, on_approve, on_reject, parent=None,
                 approve_label="✓ Approve & send"):
        super().__init__(parent)
        self._item = item
        self._on_approve = on_approve
        self._on_reject = on_reject
        self.setWindowTitle("Pending email")
        self.resize(680, 640)
        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(8)

        subj = QLabel(item.get("subject") or "(no subject)")
        f = subj.font()
        f.setWeight(QFont.Weight.DemiBold)
        f.setPointSize(f.pointSize() + 4)
        subj.setFont(f)
        subj.setWordWrap(True)
        lay.addWidget(subj)

        def hdr(text):
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
            lay.addWidget(lbl)
            return lbl

        hdr(f"<b>From account:</b> {item.get('account_email','')}")
        hdr(f"<b>To:</b> {item.get('to','')}")
        if item.get("cc"):
            hdr(f"<b>Cc:</b> {item['cc']}")
        if item.get("bcc"):
            hdr(f"<b>Bcc:</b> {item['bcc']}")
        hdr(f"<b>Submitted by:</b> {item.get('sender_label','')}")
        if item.get("ip"):
            hdr(f"<b>From IP:</b> {item.get('ip')}")
        atts = item.get("attachments") or []
        if atts:
            names = ", ".join(a.get("name", "?") for a in atts)
            hdr(f"📎 <b>{len(atts)} attachment(s):</b> {names}")

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {theme.BORDER};")
        lay.addWidget(sep)

        outgoing = QLabel("✉️ The email that will be sent "
                          "(includes any quoted original below):")
        outgoing.setStyleSheet(f"color: {theme.GOLD_DEEP}; font-weight: 600;")
        lay.addWidget(outgoing)

        body = QTextBrowser()
        body.setOpenExternalLinks(False)
        body.setOpenLinks(False)
        body.setStyleSheet(
            f"QTextBrowser {{ background: {theme.BG}; color: {theme.TEXT};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px; }}")
        html = item.get("body_html") or ""
        if html.strip():
            body.setHtml(html)
        else:
            body.setPlainText(item.get("body_text") or "")
        lay.addWidget(body, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        reject = QPushButton("✕ Reject")
        reject.setStyleSheet(_REJECT_QSS)
        reject.setCursor(Qt.CursorShape.PointingHandCursor)
        reject.clicked.connect(self._do_reject)
        approve = QPushButton(approve_label)
        approve.setStyleSheet(_APPROVE_QSS)
        approve.setCursor(Qt.CursorShape.PointingHandCursor)
        approve.clicked.connect(self._do_approve)
        btns.addWidget(close)
        btns.addWidget(reject)
        btns.addWidget(approve)
        lay.addLayout(btns)

    def _do_approve(self):
        self.accept()
        self._on_approve(self._item)

    def _do_reject(self):
        self.accept()
        self._on_reject(self._item)
