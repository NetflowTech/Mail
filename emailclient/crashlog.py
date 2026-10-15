"""Global crash / exception logging.

The app was dying silently on intermittent unhandled exceptions, leaving nothing
to diagnose. This installs file logging plus hooks for the three places an
exception can escape: the main interpreter, background threads, and Qt's own
message stream. Everything lands in a small rotating log under the data dir so a
user can send it to us after a crash.

Install once, as early as possible, from ``main()``.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

from .config import data_dir

# CUSTOMER EDITION: crash reports are NOT sent anywhere. Logging stays local only.

_installed = False


def context() -> dict:
    """Best-effort identity for a crash report (version, device, license key,
    platform). Never raises — a crash report must go out even if some field is
    missing."""
    ctx = {"version": "?", "platform": sys.platform,
           "device": "", "name": "", "key": ""}
    try:
        from . import __version__
        ctx["version"] = __version__
    except Exception:
        pass
    try:
        from . import approval
        ctx["device"] = approval._device()
        try:
            ctx["name"] = approval._device_name()
        except Exception:
            pass
    except Exception:
        pass
    try:
        from PySide6.QtCore import QSettings
        ctx["key"] = str(QSettings("OroStarMail", "ui").value("lic_key") or "")
    except Exception:
        pass
    return ctx


def _upload(text: str, timeout: int = 10) -> bool:
    """CUSTOMER EDITION: disabled. Crash logs are kept locally and never sent."""
    return False


def _upload_async(text: str) -> None:
    """CUSTOMER EDITION: disabled (no crash upload)."""
    return


def _offset_file() -> Path:
    return log_dir() / ".uploaded_offset"


def upload_pending() -> None:
    """CUSTOMER EDITION: disabled. Crash logs stay on the user's own machine."""
    return


def log_dir() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_path() -> Path:
    return log_dir() / "orostarmail.log"


def install() -> None:
    """Wire up file logging + excepthooks. Idempotent and never raises."""
    global _installed
    if _installed:
        return
    _installed = True
    try:
        handler = logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception:
        return  # if we can't even open the log, don't break startup

    log = logging.getLogger("crash")

    def _excepthook(exc_type, exc, tb):
        # Let Ctrl+C behave normally; log everything else with a full traceback.
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("Uncaught exception:\n%s", text)
        _upload_async("MAIN-THREAD CRASH:\n" + text)

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        text = "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))
        log.error("Uncaught exception in thread %s:\n%s", name, text)
        _upload_async(f"THREAD CRASH ({name}):\n" + text)

    threading.excepthook = _thread_excepthook

    # Qt's own warnings/criticals (render-process issues, layout warnings, …)
    # go through here, not Python's excepthook — capture warning-and-above.
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler

        qt_log = logging.getLogger("qt")
        _levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }

        def _qt_handler(mode, ctx, message):
            level = _levels.get(mode, logging.INFO)
            if level >= logging.WARNING:      # skip chatty debug/info noise
                qt_log.log(level, "%s", message)

        qInstallMessageHandler(_qt_handler)
    except Exception:
        pass

    log.info("crash logging installed (log: %s)", log_path())


def start_hang_watchdog(threshold: float = 8.0) -> None:
    """Detect a frozen UI thread and report it (a hang throws no exception, so
    excepthook never sees it). A QTimer on the UI thread bumps a heartbeat every
    second; a daemon thread notices when the heartbeat goes silent for
    *threshold* seconds, grabs the main thread's stack, and uploads it once per
    hang episode. Call AFTER the QApplication exists. Never raises."""
    try:
        import time

        from PySide6.QtCore import QTimer

        state = {"beat": time.monotonic(), "reported": False}

        def _bump():
            state["beat"] = time.monotonic()
            state["reported"] = False   # UI is alive again → arm for next hang

        timer = QTimer()
        timer.timeout.connect(_bump)
        timer.start(1000)
        # Keep a reference so the timer isn't garbage-collected.
        globals()["_hang_timer"] = timer

        log = logging.getLogger("crash")
        main_id = threading.main_thread().ident

        def _watch():
            while True:
                time.sleep(2.0)
                stuck = time.monotonic() - state["beat"]
                if stuck < threshold or state["reported"]:
                    continue
                state["reported"] = True   # one report per freeze, not per tick
                frames = sys._current_frames()
                frame = frames.get(main_id)
                stack = ("".join(traceback.format_stack(frame))
                         if frame is not None else "(main-thread stack unavailable)")
                text = (f"UI HANG: main thread unresponsive for "
                        f"~{stuck:.0f}s. Stack at detection:\n{stack}")
                log.error(text)
                _upload_async("UI HANG:\n" + text)

        threading.Thread(target=_watch, name="hang-watchdog",
                         daemon=True).start()
    except Exception:
        pass  # watchdog is best-effort; never break startup over it
