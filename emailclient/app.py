"""App entry point.

Heavy modules (MainWindow pulls in QtWebEngine etc.) are imported INSIDE
main(), after the splash screen is up — so users see the logo immediately
instead of a blank pause while everything loads.
"""
from __future__ import annotations

import os
import sys
import threading

# QtWebEngine in a frozen .app can fail to GPU-composite image-heavy pages,
# leaving the reader blank (the "Skia/GPU fallback" warnings). Force the
# software pipeline so the page always paints. Must be set before QtWebEngine
# initialises (MainWindow imports it lazily inside main()).
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-gpu-compositing --disable-software-rasterizer")

# Point all SSL users (urllib, requests/MSAL, smtplib) to the bundled certifi
# CA store — macOS Python does not use the system keychain by default, so
# connections to well-known servers fail with CERTIFICATE_VERIFY_FAILED.
try:
    import certifi as _certifi
    os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except Exception:
    pass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSplashScreen, QSystemTrayIcon

from .paths import asset_dir
from .ui import message_list, theme

ASSET_DIR = asset_dir()
LOGO_PATH = ASSET_DIR / "logo.png"


def _splash_pixmap() -> QPixmap:
    """Splash card: white rounded canvas with the logo + app name."""
    w, h = 420, 300
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(theme.BG))
    p.setPen(QColor(theme.BORDER))
    p.drawRoundedRect(0, 0, w - 1, h - 1, 18, 18)
    logo = QPixmap(str(LOGO_PATH))
    if not logo.isNull():
        logo = logo.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        p.drawPixmap((w - logo.width()) // 2, 42, logo)
    f = QFont()
    f.setPointSize(19)
    f.setWeight(QFont.Weight.Bold)
    p.setFont(f)
    p.setPen(QColor(theme.GOLD_DEEP))
    p.drawText(0, 180, w, 34, Qt.AlignmentFlag.AlignCenter, "AMail")
    f2 = QFont()
    f2.setPointSize(11)
    p.setFont(f2)
    p.setPen(QColor(theme.TEXT_DIM))
    p.drawText(0, 214, w, 22, Qt.AlignmentFlag.AlignCenter, "by AMail")
    p.end()
    return pm


def _raise_fd_limit(target: int = 4096) -> None:
    """Raise the open-file/socket limit (macOS default soft limit is 256 —
    too low for 100 IMAP connections + Qt + attachments)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE,
                               (min(target, hard), hard))
    except Exception:
        pass  # non-POSIX or restricted: workers degrade gracefully


def _fallback_tray_pixmap() -> QPixmap:
    """Gold ✦ drawn programmatically — used only if the logo asset is missing."""
    pm = QPixmap(22, 22)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    f = QFont()
    f.setPixelSize(18)
    p.setFont(f)
    p.setPen(QColor(theme.GOLD_DEEP))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "✦")
    p.end()
    return pm


def _tray_icon() -> QIcon:
    """Brand logo scaled for the menu bar.

    The logo is a full-color white disc (black crescent + red star), so it
    stays visible on both light and dark menu bars without a template mask.
    """
    pm = QPixmap(str(LOGO_PATH))
    if pm.isNull():
        return QIcon(_fallback_tray_pixmap())
    return QIcon(pm.scaled(22, 22, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation))


def _show_window(window: MainWindow) -> None:
    window.show()
    window.raise_()
    window.activateWindow()


def main():
    # Install crash/exception logging FIRST so anything that dies during startup
    # still leaves a diagnosable trail (local log + best-effort server upload).
    from . import crashlog
    crashlog.install()
    _raise_fd_limit()  # 100+ IMAP connections need more than macOS's 256 fds
    app = QApplication(sys.argv)
    crashlog.start_hang_watchdog()  # report a frozen UI (hangs throw no exception)
    app.setApplicationName("AMail")
    app.setStyle("Fusion")  # consistent QSS rendering across platforms
    # theme was applied at import time, before a QApplication existed — redo it
    # now that the OS light/dark hint is readable, so "System" starts correct.
    theme.apply_theme(theme.current_theme())
    message_list.apply_density()
    app.setStyleSheet(theme.build_qss())
    app.setWindowIcon(QIcon(str(LOGO_PATH)))  # window + Dock icon
    # Outlook-style background mode: closing the window hides it, the app
    # keeps syncing; only the tray menu's Quit really exits.
    app.setQuitOnLastWindowClosed(False)

    # Splash up FIRST — the heavy imports below (QtWebEngine via MainWindow)
    # take a few seconds on first launch.
    splash = QSplashScreen(_splash_pixmap())
    splash.showMessage("Loading…", Qt.AlignmentFlag.AlignBottom
                       | Qt.AlignmentFlag.AlignHCenter, QColor(theme.TEXT_FAINT))
    splash.show()
    app.processEvents()

    from .backup import run_backup  # noqa: E402  (deferred: splash first)
    from .config import db_path
    from .db import Database
    from .sync.engine import SyncEngine
    from .sync.outbox_worker import OutboxWorker
    from .ui.main_window import MainWindow
    app.processEvents()

    db = Database(db_path())
    engine = SyncEngine(db)
    outbox = OutboxWorker(db)
    window = MainWindow(db, engine, outbox_worker=outbox)

    tray = QSystemTrayIcon(_tray_icon(), app)
    tray.setToolTip("AMail — running in the background")
    tray_menu = QMenu()
    open_action = tray_menu.addAction("📬 Open AMail")
    open_action.triggered.connect(lambda: _show_window(window))
    compose_action = tray_menu.addAction("✉ Compose")
    compose_action.triggered.connect(window.compose_new)
    about_action = tray_menu.addAction("About AMail")

    def _show_about():
        from .ui.about_dialog import AboutDialog
        AboutDialog(window).exec()

    about_action.triggered.connect(_show_about)
    tray_menu.addSeparator()
    quit_action = tray_menu.addAction("Quit AMail")

    def _quit():
        window.shutdown()
        app.quit()

    quit_action.triggered.connect(_quit)
    tray.setContextMenu(tray_menu)
    tray.activated.connect(
        lambda reason: reason != QSystemTrayIcon.ActivationReason.Context
        and _show_window(window))
    tray.show()
    app._tray = tray  # keep the tray (and its menu) alive for the app's lifetime

    # Cmd+Q / external quit: make sure workers are stopped (shutdown is
    # idempotent, so the tray-Quit path doesn't double-stop).
    app.aboutToQuit.connect(window.shutdown)

    # Startup backup (always, regardless of frequency setting) — own thread +
    # own sqlite connections, so it never blocks or races the UI/WAL conn.
    def _startup_backup():
        if run_backup() is not None:
            import time

            from .settings import BackupSettings
            BackupSettings().last_run_epoch = time.time()

    threading.Thread(target=_startup_backup, daemon=True).start()

    window.show()
    splash.finish(window)  # auto-dismiss once the main window is up (no click)


    # Background update check: a few seconds after launch so it never
    # competes with startup work; marshals the prompt back to the UI thread.
    # check_only() never raises and returns None on any failure/no-update,
    # so a bad network or missing manifest silently does nothing.
    threading.Thread(target=crashlog.upload_pending, daemon=True).start()

    engine.start_all()
    outbox.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
