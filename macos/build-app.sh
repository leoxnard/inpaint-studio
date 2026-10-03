#!/usr/bin/env bash
# Builds "Inpaint Studio.app" (native window around the local server, app code bundled inside).
# The .app is self-contained: send it to someone and the setup page installs the rest.
# If the app is running, it is quit first (that also restarts the server with the new code) and
# opened again after the build, so a rebuild just updates the one app window.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-$HOME/Applications/Inpaint Studio.app}"
BUILD="$(mktemp -d)/Inpaint Studio.app"
C="$BUILD/Contents"
mkdir -p "$C/MacOS" "$C/Resources/app"
swiftc -O -target "$(uname -m)-apple-macos12" -o "$C/MacOS/InpaintStudio" macos/InpaintStudio.swift
cat > "$C/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Inpaint Studio</string>
  <key>CFBundleDisplayName</key><string>Inpaint Studio</string>
  <key>CFBundleIdentifier</key><string>de.leonardsima.inpaintstudio</string>
  <key>CFBundleExecutable</key><string>InpaintStudio</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSPrincipalClass</key><string>NSApplication</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
cp *.py pyproject.toml uv.lock "$C/Resources/app/"
cp -R web "$C/Resources/app/web"
codesign --force --deep -s - "$BUILD" 2>/dev/null

# Quit a running copy (old applet or this app); it asks first when jobs are still running
WAS_RUNNING=0
if pgrep -f "$OUT/Contents/MacOS/" >/dev/null; then
  WAS_RUNNING=1
  osascript -e "tell application \"$OUT\" to quit" 2>/dev/null || true  # by path: a copy elsewhere stays
  for _ in $(seq 1 40); do pgrep -f "$OUT/Contents/MacOS/" >/dev/null || break; sleep 0.5; done
  if pgrep -f "$OUT/Contents/MacOS/" >/dev/null; then
    echo "Inpaint Studio is still running (jobs kept?), not replaced. New build: $BUILD" >&2
    exit 1
  fi
fi
mkdir -p "$(dirname "$OUT")"
rm -rf "$OUT"
mv "$BUILD" "$OUT"
echo "Built: $OUT"
[ "$WAS_RUNNING" = 1 ] && open "$OUT"
true
