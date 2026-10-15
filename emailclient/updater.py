"""Self-update: check the server for a newer build and one-click install it.

GUI-independent core so it can be unit-tested without Qt. All network calls are
defensive — any failure returns None/False and never raises, so being offline
never breaks startup. Follows the codebase convention of stdlib urllib (see
license.py) and a per-module URL constant.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

MANIFEST_URL = "https://raw.githubusercontent.com/ahaarij/Mail/main/version.json"


def _parse(v: str) -> tuple[int, ...] | None:
    try:
        parts = [int(x) for x in str(v).strip().split(".")]
    except (ValueError, AttributeError):
        return None
    return tuple(parts) if parts else None


def is_newer(remote: str, local: str) -> bool:
    """True iff *remote* is a strictly higher dotted-numeric version than
    *local*. Malformed input on either side → False (never upgrade on garbage)."""
    r, l = _parse(remote), _parse(local)
    if r is None or l is None:
        return False
    return r > l


def fetch_manifest(url: str = MANIFEST_URL, timeout: float = 8.0) -> dict | None:
    """Fetch and parse the version manifest JSON. Returns None on any failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return None


def _current_platform() -> str:
    if sys.platform.startswith("win"):
        return "win"
    if sys.platform == "darwin":
        return "mac"
    return "other"


def platform_asset(manifest: dict, platform: str | None = None) -> tuple[str, str] | None:
    """(url, sha256) for the current OS's build, or None if absent."""
    plat = platform or _current_platform()
    url = str(manifest.get(f"{plat}_url", "")).strip()
    sha = str(manifest.get(f"{plat}_sha256", "")).strip()
    if not url:
        return None
    return url, sha


def check_only(local_version: str, url: str = MANIFEST_URL) -> dict | None:
    """Return the manifest iff a newer build exists for this platform, else
    None. Safe to call off the UI thread; never raises."""
    manifest = fetch_manifest(url)
    if manifest is None:
        return None
    if not is_newer(str(manifest.get("version", "")), local_version):
        return None
    if platform_asset(manifest) is None:
        return None
    return manifest


def download_and_verify(url: str, sha256: str, dest: Path,
                        timeout: float = 60.0, progress=None) -> bool:
    """Download *url* to *dest*, verify sha256, return True on success."""
    dest = Path(dest)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            h = hashlib.sha256()
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(done / total)
        if sha256 and h.hexdigest().lower() != sha256.lower():
            dest.unlink(missing_ok=True)
            return False
        return True
    except Exception:
        dest.unlink(missing_ok=True)
        return False


def apply_update(installer_path: Path) -> bool:
    """Launch the platform update, detached, so it can replace this app after we
    exit. Returns True if the update process started (caller should then quit)."""
    installer_path = Path(installer_path)
    plat = _current_platform()
    try:
        if plat == "win":
            # Inno Setup: close the running app, install silently, relaunch it.
            subprocess.Popen(
                [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES",
                 "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS", "/NORESTART"],
                close_fds=True)
            return True
        if plat == "mac":
            return _apply_update_mac(installer_path)
        return False
    except Exception:
        return False


def _apply_update_mac(zip_path: Path) -> bool:
    """Unzip the new .app next to the current one, then spawn a detached shell
    helper that waits for us to quit, swaps the bundle, and relaunches. A running
    .app cannot replace itself, hence the helper."""
    import sys as _sys
    # Current bundle: .../AMail.app/Contents/MacOS/<exe> -> the .app dir.
    exe = Path(_sys.executable).resolve()
    app_dir = exe
    while app_dir.suffix != ".app" and app_dir != app_dir.parent:
        app_dir = app_dir.parent
    if app_dir.suffix != ".app":
        return False
    staging = zip_path.parent / "osm_update_new"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        staging_resolved = staging.resolve()
        for member in z.namelist():
            target = (staging / member).resolve()
            # Path containment, not string prefix: startswith() would treat a
            # sibling like /tmp/staging_evil as inside /tmp/staging.
            if target != staging_resolved and not target.is_relative_to(staging_resolved):
                return False
        z.extractall(staging)
    new_app = next(staging.glob("*.app"), None)
    if new_app is None:
        return False
    pid = os.getpid()
    # Paths/PID are passed as positional args ($1/$2/$3), never interpolated
    # into the script text, so shell metacharacters in a server-supplied
    # bundle name can't be parsed as shell code.
    script = (
        'app_dir="$1"; new_app="$2"; pid="$3"; '
        'while kill -0 "$pid" 2>/dev/null; do sleep 0.5; done; '
        'if mv "$app_dir" "$app_dir.bak"; then '
        '  if mv "$new_app" "$app_dir"; then rm -rf "$app_dir.bak"; '
        '  else mv "$app_dir.bak" "$app_dir"; fi; '
        'fi; '
        'open "$app_dir"'
    )
    subprocess.Popen(
        ["/bin/sh", "-c", script, "sh", str(app_dir), str(new_app), str(pid)],
        close_fds=True, start_new_session=True)
    return True
