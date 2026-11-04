#!/usr/bin/env bash
# Installs PhotoFrame for the current user on Ubuntu.
set -e
cd "$(dirname "$0")"

echo "Installing dependencies (needs your password)…"
sudo apt-get update
sudo apt-get install -y python3 python3-tk python3-pil python3-pil.imagetk zenity x11-xserver-utils

DEST="$HOME/.local/share/photoframe"
mkdir -p "$DEST" "$HOME/.local/share/applications"
install -m 755 photoframe.py "$DEST/photoframe.py"

cat > "$HOME/.local/share/applications/photoframe.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=PhotoFrame
Comment=Full-screen shuffling photo gallery
Exec=python3 "$DEST/photoframe.py"
Icon=image-x-generic
Categories=Graphics;Viewer;
EOF

echo
echo "Done. Open 'PhotoFrame' from the app menu, or run:"
echo "  python3 $DEST/photoframe.py"
