#!/usr/bin/env bash
# Remove a per-user Music Organizer install (undoes install.sh).
set -euo pipefail
APP="music-organizer"

rm -f "$HOME/.local/bin/$APP"
rm -f "$HOME/.local/share/applications/$APP.desktop"
find "$HOME/.local/share/icons/hicolor" -name "$APP.png" -delete 2>/dev/null || true

command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true

echo "Music Organizer removed."
