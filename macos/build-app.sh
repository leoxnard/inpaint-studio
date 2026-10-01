#!/usr/bin/env bash
# Builds "Inpaint Studio.app" (stay-open AppleScript applet) into ~/Applications.
set -euo pipefail
cd "$(dirname "$0")"
OUT="$HOME/Applications/Inpaint Studio.app"
mkdir -p "$HOME/Applications"
rm -rf "$OUT"
osacompile -s -o "$OUT" InpaintStudio.applescript
echo "Built: $OUT"
