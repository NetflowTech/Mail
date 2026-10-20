"""Shared 'show dialog -> download -> apply' flow, used by the startup check and
the About dialog's manual 'Check for updates'."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .. import updater
from .update_dialog import UpdateDialog


def run_update_flow(parent, manifest: dict) -> None:
    dlg = UpdateDialog(manifest, parent)
    if not dlg.exec():          # user chose Later
        return
    asset = updater.platform_asset(manifest)
    if asset is None:
        return
    url, sha = asset
    sig_url = updater.platform_sig_url(manifest)
    dest = Path(tempfile.gettempdir()) / Path(url).name
    dlg.set_progress(None)
    dlg.show()

    def _progress(ratio):
        QTimer.singleShot(0, lambda: dlg.set_progress(ratio))

    def _worker():
        ok = updater.download_and_verify(url, sha, dest, progress=_progress,
                                         sig_url=sig_url)
        QTimer.singleShot(0, lambda: _finish(ok))

    def _finish(ok):
        dlg.close()
        if not ok:
            QMessageBox.warning(parent, "Update failed",
                                "The update could not be downloaded or verified. "
                                "Please try again later.")
            return
        if updater.apply_update(dest):
            # installer/helper takes over and relaunches, but quit() is a
            # no-op while any modal exec() loop (Settings/About/etc.) is
            # still on the stack, so wait for the whole chain to unwind.
            _quit_when_unwound()
        else:
            QMessageBox.warning(parent, "Update failed",
                                "Could not start the installer.")

    threading.Thread(target=_worker, daemon=True).start()


def _quit_when_unwound() -> None:
    """Defer QApplication.quit() until no modal exec() loop is active.

    Any still-open modal dialog (e.g. Settings -> About -> this flow) swallows
    quit() because Qt only actually exits once every nested event loop has
    returned. Re-post ourselves on a timer until the modal stack is clear.
    """
    if QApplication.activeModalWidget() is not None:
        QTimer.singleShot(50, _quit_when_unwound)
        return
    QApplication.quit()
