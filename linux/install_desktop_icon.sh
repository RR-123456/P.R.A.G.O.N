#!/bin/bash
# install_desktop_icon.sh
# ------------------------
# Creates a "P.R.A.G.O.N" launcher icon (with the dragon logo) on your
# Linux Desktop and in your app menu.
#
# Usage:  chmod +x install_desktop_icon.sh && ./install_desktop_icon.sh

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$SCRIPT_DIR/pragon.desktop.template"
OUT_NAME="pragon.desktop"

DESKTOP_DIR="$HOME/Desktop"
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

render() {
    sed "s#__PROJECT_DIR__#$PROJECT_DIR#g" "$TEMPLATE" > "$1"
    chmod +x "$1"
}

render "$APPS_DIR/$OUT_NAME"
echo "Installed to app menu: $APPS_DIR/$OUT_NAME"

if [ -d "$DESKTOP_DIR" ]; then
    render "$DESKTOP_DIR/$OUT_NAME"
    # Most desktop environments require the file to be marked "trusted"
    gio set "$DESKTOP_DIR/$OUT_NAME" metadata::trusted true 2>/dev/null || true
    echo "Installed to Desktop: $DESKTOP_DIR/$OUT_NAME"
else
    echo "No ~/Desktop folder found - skipped desktop icon, app-menu entry is enough."
fi

echo "Done. Look for the P.R.A.G.O.N dragon icon."
