#!/usr/bin/env bash
# Builds "Inpaint Studio.app" (stay-open AppleScript applet with the app code bundled inside).
# The .app is self-contained: send it to someone and the setup page installs the rest.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$HOME/Applications/Inpaint Studio.app}"
mkdir -p "$(dirname "$OUT")"
rm -rf "$OUT"
osacompile -s -o "$OUT" macos/InpaintStudio.applescript
APP="$OUT/Contents/Resources/app"
mkdir -p "$APP"
cp *.py pyproject.toml uv.lock "$APP/"
cp -R web "$APP/web"
codesign --force --deep -s - "$OUT" 2>/dev/null  # ad-hoc signature again after adding files
echo "Built: $OUT"
