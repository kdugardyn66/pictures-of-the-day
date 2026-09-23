#!/usr/bin/env bash
# Build potd.app and install it in /Applications.
#   ./build.sh               build + install + open
#   ./build.sh --no-install  build only (dist/potd.app)
#   PYTHON=/path/to/python3 ./build.sh   use a specific Python
set -euo pipefail
cd "$(dirname "$0")"

# Pick a Python: py2app is most reliable on 3.10-3.13, so prefer those.
if [ -z "${PYTHON:-}" ]; then
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then PYTHON=$(command -v "$c"); break; fi
  done
fi
[ -n "${PYTHON:-}" ] || { echo "No python3 found. Install one, e.g.: brew install python@3.13"; exit 1; }
PYVER=$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "Using $PYTHON (Python $PYVER)"

# (Re)create the virtualenv if it's missing or was made with another Python version.
if [ -d .venv ] && [ "$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" != "$PYVER" ]; then
  echo "Recreating .venv for Python $PYVER"
  rm -rf .venv
fi
[ -d .venv ] || "$PYTHON" -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel setuptools >/dev/null
pip install -r requirements.txt

# App icon: assets/icon.png -> assets/potd.icns
if [ ! -f assets/potd.icns ]; then
  set=assets/potd.iconset; rm -rf "$set"; mkdir -p "$set"
  for s in 16 32 128 256 512; do
    sips -z $s $s assets/icon.png --out "$set/icon_${s}x${s}.png" >/dev/null
    sips -z $((s*2)) $((s*2)) assets/icon.png --out "$set/icon_${s}x${s}@2x.png" >/dev/null
  done
  iconutil -c icns "$set" -o assets/potd.icns && rm -rf "$set"
fi

rm -rf build dist
python setup.py py2app

if [ "${1:-}" != "--no-install" ]; then
  osascript -e 'tell application "potd" to quit' 2>/dev/null || true
  sleep 1
  rm -rf /Applications/potd.app
  cp -R dist/potd.app /Applications/
  echo "Installed /Applications/potd.app"
  open /Applications/potd.app
fi
