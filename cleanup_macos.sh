#!/usr/bin/env bash
# AMail migration cleanup — removes all OroStarMail traces from this Mac.
# Run this BEFORE installing AMail.
set -euo pipefail

echo "============================================"
echo " AMail Cleanup — removing OroStarMail"
echo "============================================"
echo

# -- 1. Quit OroStarMail if running ---------------------------------------
if pgrep -x "OroStarMail" > /dev/null 2>&1; then
    echo "Quitting OroStarMail..."
    pkill -x "OroStarMail" || true
    sleep 2
fi

# -- 2. Remove app bundle -------------------------------------------------
if [ -d "/Applications/OroStarMail.app" ]; then
    echo "Removing /Applications/OroStarMail.app ..."
    rm -rf "/Applications/OroStarMail.app"
fi

# -- 3. Remove user data folder (mail cache) ------------------------------
USERDATA="$HOME/.orostarmail"
if [ -d "$USERDATA" ]; then
    echo "Removing user data: $USERDATA ..."
    rm -rf "$USERDATA"
fi

# -- 4. Remove macOS preferences (QSettings plist) -----------------------
PLIST="$HOME/Library/Preferences/com.orostar.orostarmail.plist"
if [ -f "$PLIST" ]; then
    echo "Removing preferences: $PLIST ..."
    rm -f "$PLIST"
fi
# Also clear from defaults system
defaults delete com.orostar.orostarmail 2>/dev/null || true

# -- 5. Remove keychain entries -------------------------------------------
echo "Removing keychain entries..."
# Delete all keychain items with service name OroStarMail
security find-generic-password -s "OroStarMail" 2>/dev/null | grep "acct" | \
    awk -F'"' '{print $4}' | while read -r account; do
    security delete-generic-password -s "OroStarMail" -a "$account" 2>/dev/null || true
done

# -- 6. Remove app support folder if it exists ----------------------------
APPSUPPORT="$HOME/Library/Application Support/OroStarMail"
if [ -d "$APPSUPPORT" ]; then
    echo "Removing Application Support folder..."
    rm -rf "$APPSUPPORT"
fi

# -- 7. Remove logs -------------------------------------------------------
LOGS="$HOME/Library/Logs/OroStarMail"
if [ -d "$LOGS" ]; then
    echo "Removing logs..."
    rm -rf "$LOGS"
fi

echo
echo "============================================"
echo " Cleanup complete. You can now install AMail."
echo "============================================"
