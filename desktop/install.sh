#!/usr/bin/env bash
# Install the LizardKing desktop app for this user: Electron (npm), the icon,
# and a menu entry pointing at this checkout. Run again after moving the repo.
set -euo pipefail

DESKTOP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$DESKTOP")"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

command -v npm >/dev/null || { echo "npm not found (brew install node)" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "No .venv: run 'uv sync' in $ROOT first" >&2; exit 1; }

(cd "$DESKTOP" && npm ci --no-audit --no-fund)
# Newer npm skips dependency install scripts unless allowed; Electron's
# downloads the binary, so run it if the binary is missing.
[ -x "$DESKTOP/node_modules/electron/dist/electron" ] || node "$DESKTOP/node_modules/electron/install.js"

mkdir -p "$APPS" "$ICONS"
install -m 644 "$DESKTOP/icon.png" "$ICONS/lizardking.png"
cat > "$APPS/lizardking.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=LizardKing
GenericName=Media Player
Comment=Play any local video or music file
Exec="$DESKTOP/node_modules/electron/dist/electron" "$DESKTOP" %F
Icon=lizardking
Terminal=false
Categories=AudioVideo;Player;Video;Audio;
MimeType=video/mp4;video/x-matroska;video/webm;video/x-msvideo;video/quicktime;audio/mpeg;audio/flac;audio/ogg;audio/mp4;audio/x-wav;audio/x-mpegurl;
StartupWMClass=lizardking
EOF
update-desktop-database "$APPS" 2>/dev/null || true
echo "Installed: LizardKing is in the application menu ($APPS/lizardking.desktop)"
