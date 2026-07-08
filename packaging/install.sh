#!/usr/bin/env bash
#
# Install Music Organizer for the current user (no root needed):
#   - binary  -> ~/.local/bin/music-organizer
#   - icons   -> ~/.local/share/icons/hicolor/<size>/apps/music-organizer.png
#   - launcher-> ~/.local/share/applications/music-organizer.desktop
#
# Run it from the extracted release folder:  ./install.sh
#
set -euo pipefail

APP="music-organizer"
HERE="$(cd "$(dirname "$0")" && pwd)"

BIN_SRC="$HERE/$APP"
BIN_DIR="$HOME/.local/bin"
ICON_BASE="$HOME/.local/share/icons/hicolor"
APP_DIR="$HOME/.local/share/applications"

if [ ! -f "$BIN_SRC" ]; then
    echo "Error: '$APP' binary not found next to this script." >&2
    echo "Run install.sh from inside the extracted release folder." >&2
    exit 1
fi

echo "Installing $APP…"
mkdir -p "$BIN_DIR" "$APP_DIR"
install -m 755 "$BIN_SRC" "$BIN_DIR/$APP"

# Icons into the hicolor theme (size parsed from music-organizer-<size>.png)
if [ -d "$HERE/icons" ]; then
    for png in "$HERE"/icons/$APP-*.png; do
        [ -e "$png" ] || continue
        size="$(basename "$png" | sed -E "s/^$APP-([0-9]+)\.png$/\1/")"
        dest="$ICON_BASE/${size}x${size}/apps"
        mkdir -p "$dest"
        install -m 644 "$png" "$dest/$APP.png"
    done
fi

# Desktop entry, with Exec pointed at the absolute installed path
sed -e "s|^Exec=.*|Exec=$BIN_DIR/$APP|" \
    "$HERE/$APP.desktop" > "$APP_DIR/$APP.desktop"
chmod 644 "$APP_DIR/$APP.desktop"

# Refresh caches if the tools are present (harmless if they aren't)
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APP_DIR" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$ICON_BASE" >/dev/null 2>&1 || true

echo "Done. Look for \"Music Organizer\" in your application menu."
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "Note: $BIN_DIR is not on your PATH — the menu launcher still works, "
       echo "      but to run '$APP' from a terminal add it to PATH." ;;
esac
