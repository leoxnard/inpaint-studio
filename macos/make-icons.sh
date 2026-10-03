#!/usr/bin/env bash
# Renders the app icon from design/app-icon.svg (light) and design/app-icon-dark.svg (dark):
#   macos/AppIcon.icns  light icon (fallback, older macOS)
#   macos/Assets.car    light + dark icon, macOS switches with the system appearance
#   web/apple-touch-icon.png
# Needs Xcode (actool). Run after changing the SVGs and commit the outputs; build-app.sh only copies them.
set -euo pipefail
cd "$(dirname "$0")/.."
T="$(mktemp -d)"
swiftc -O -o "$T/svg2png" macos/tools/svg2png.swift
A="$T/Assets.xcassets/AppIcon.appiconset"
mkdir -p "$A" "$T/AppIcon.iconset" "$T/out"
echo '{"info":{"author":"xcode","version":1}}' > "$T/Assets.xcassets/Contents.json"
images=""
for s in 16 32 128 256 512; do
  for scale in 1 2; do
    px=$((s * scale)); suffix=$([ $scale = 2 ] && echo "@2x" || true)
    "$T/svg2png" design/app-icon.svg "$A/${s}@${scale}x.png" $px
    "$T/svg2png" design/app-icon-dark.svg "$A/${s}@${scale}x-dark.png" $px
    cp "$A/${s}@${scale}x.png" "$T/AppIcon.iconset/icon_${s}x${s}${suffix}.png"
    e="\"idiom\":\"mac\",\"scale\":\"${scale}x\",\"size\":\"${s}x${s}\""
    images+="{\"filename\":\"${s}@${scale}x.png\",$e},"
    images+="{\"filename\":\"${s}@${scale}x-dark.png\",$e,\"appearances\":[{\"appearance\":\"luminosity\",\"value\":\"dark\"}]},"
  done
done
echo "{\"images\":[${images%,}],\"info\":{\"author\":\"xcode\",\"version\":1}}" > "$A/Contents.json"
xcrun actool "$T/Assets.xcassets" --compile "$T/out" --platform macosx --minimum-deployment-target 12.0 \
  --app-icon AppIcon --output-partial-info-plist "$T/out/partial.plist" > /dev/null
cp "$T/out/Assets.car" macos/Assets.car
iconutil -c icns -o macos/AppIcon.icns "$T/AppIcon.iconset"
sed 's/width="1024" height="1024" viewBox="0 0 512 512"/viewBox="50 50 412 412"/' design/app-icon.svg > "$T/touch.svg"
"$T/svg2png" "$T/touch.svg" web/apple-touch-icon.png 180
rm -rf "$T"
echo "Icons written: macos/AppIcon.icns macos/Assets.car web/apple-touch-icon.png"
