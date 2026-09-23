#!/usr/bin/env bash
# Build a native macOS .app and distributable .dmg.
# Run this script on macOS only: bash packaging/build-macos.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS. PyInstaller cannot cross-build a usable macOS app from Windows."
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.10+ is required. Set PYTHON_BIN if python3 is not on PATH."
  exit 1
fi

PYTHON_VERSION="$($PYTHON_BIN -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "Python 3.10+ is required; found $PYTHON_VERSION."
  exit 1
fi

ARCH="$(uname -m)"
case "$ARCH" in
  arm64|x86_64) ;;
  *) echo "Unsupported macOS architecture: $ARCH"; exit 1 ;;
esac

VENV="$ROOT/.venv-packaging-macos"
PYTHON="$VENV/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Creating packaging environment: $VENV"
  "$PYTHON_BIN" -m venv "$VENV"
fi

VENV_ARCH="$($PYTHON -c 'import platform; print(platform.machine())')"
if [[ "$VENV_ARCH" != "$ARCH" ]]; then
  echo "The packaging Python is $VENV_ARCH but this Mac is $ARCH. Recreate $VENV with a native Python."
  exit 1
fi

TOOLS="$ROOT/vendor/platform-tools-macos"
ADB="$TOOLS/adb"
if [[ ! -x "$ADB" ]]; then
  ZIP="$(mktemp -t perfpilot-platform-tools).zip"
  TMP="$(mktemp -d -t perfpilot-platform-tools)"
  trap 'rm -f "$ZIP"; rm -rf "$TMP"' EXIT
  echo "Downloading Android platform-tools for macOS..."
  curl --fail --location --retry 3 \
    --output "$ZIP" \
    "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
  ditto -x -k "$ZIP" "$TMP"
  rm -rf "$TOOLS"
  mkdir -p "$(dirname "$TOOLS")"
  mv "$TMP/platform-tools" "$TOOLS"
  rm -f "$ZIP"
  rm -rf "$TMP"
  trap - EXIT
fi
chmod +x "$ADB"

"$PYTHON" -m pip install --upgrade pip
"$PYTHON" -m pip install -r "$ROOT/packaging/requirements-build.txt"

rm -rf "$ROOT/build/perfpilot" "$ROOT/dist/PerfPilot" "$ROOT/dist/PerfPilot.app"
"$PYTHON" -m PyInstaller --noconfirm --clean "$ROOT/packaging/perfpilot.spec"

APP="$ROOT/dist/PerfPilot.app"
BIN="$APP/Contents/MacOS/PerfPilot"
if [[ ! -x "$BIN" ]]; then
  echo "PyInstaller finished but the app is missing: $BIN"
  exit 1
fi

# Optional signing for an internal or Developer ID distribution. Notarization
# remains a release step and is documented in packaging/BUILDING.md.
if [[ -n "${MACOS_CODESIGN_IDENTITY:-}" ]]; then
  echo "Signing app with: $MACOS_CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    --sign "$MACOS_CODESIGN_IDENTITY" "$APP"
fi

TEST_DATA="$(mktemp -d -t perfpilot-build-test)"
trap 'rm -rf "$TEST_DATA"' EXIT
PERFPILOT_DATA="$TEST_DATA" "$BIN" --doctor >/dev/null
PERFPILOT_DATA="$TEST_DATA" "$BIN" --collect android --help >/dev/null
PERFPILOT_DATA="$TEST_DATA" "$BIN" --collect ios --help >/dev/null

DMG="$ROOT/dist/PerfPilot-macos-${ARCH}.dmg"
rm -f "$DMG"
hdiutil create -volname "PerfPilot" -srcfolder "$APP" -ov -format UDZO "$DMG"

echo
echo "Build complete: $APP"
echo "DMG: $DMG"
echo "Target architecture: $ARCH"
