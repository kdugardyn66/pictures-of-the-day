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
# Only go to the internet when something is missing; a failed download is not fatal
# if everything potd needs is already installed in .venv.
have_all() { python -c "import objc, AppKit, Photos, Quartz, CoreLocation, ServiceManagement, certifi, py2app" 2>/dev/null; }
if have_all; then
  echo "All Python packages already installed (no download needed)."
else
  pip install --upgrade pip wheel setuptools >/dev/null 2>&1 || echo "Note: could not update pip/wheel/setuptools (offline?) - continuing."
  if ! pip install -r requirements.txt; then
    if have_all; then
      echo "Note: pip had network trouble, but all packages are present - continuing."
    else
      echo "ERROR: could not download the Python packages potd needs."
      echo "       Check the internet connection (VPN/proxy/DNS) and run ./build.sh again."
      exit 1
    fi
  fi
fi

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
