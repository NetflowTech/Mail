#!/usr/bin/env bash
# Build OroStarMail.dmg for macOS.
# Run from the repo root: ./build_dmg.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO/.venv"
DIST="$REPO/dist"
APP="$DIST/OroStarMail.app"
DMG="$DIST/OroStarMail.dmg"

echo "==> Checking virtual environment..."
if [ ! -f "$VENV/bin/python" ]; then
    echo "Creating .venv..."
    python3 -m venv "$VENV"
fi

echo "==> Installing dependencies..."
"$VENV/bin/pip" install --quiet -r "$REPO/requirements.txt"
"$VENV/bin/pip" install --quiet pyinstaller

echo "==> Building app bundle..."
cd "$REPO"
"$VENV/bin/pyinstaller" OroStarMail.spec --noconfirm

if [ ! -d "$APP" ]; then
    echo "ERROR: app bundle not found at $APP"
    exit 1
fi

echo "==> Creating DMG..."
rm -f "$DMG"
hdiutil create \
    -volname "OroStarMail" \
    -srcfolder "$APP" \
    -ov \
    -format UDZO \
    "$DMG"

echo ""
echo "Done: $DMG"
ls -lh "$DMG"
