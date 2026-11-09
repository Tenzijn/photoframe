#!/usr/bin/env bash
# Installs PhotoFrame for the current user on Ubuntu.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo (it asks for your password when needed)." >&2
    exit 1
fi

echo "Installing dependencies (needs your password)…"
sudo apt-get update
sudo apt-get install -y python3 python3-tk python3-pil python3-pil.imagetk python3-gi \
    zenity x11-xserver-utils

# Check the script is valid before replacing a working install.
python3 -m py_compile photoframe.py

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
echo "If something goes wrong, the log is in ~/.cache/photoframe/"
