#!/usr/bin/env bash
# Builds the app and publishes it as a GitHub release: InpaintStudio.zip (used by install.sh)
# and InpaintStudio.dmg (manual download, needs "Open Anyway" once).
#   macos/release.sh 1.1.0           # build + upload
#   macos/release.sh 1.1.0 --dry-run # build only, files in dist/
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="${1:?usage: macos/release.sh <version> [--dry-run]}"
DRY="${2:-}"
[ "$(uname -m)" = arm64 ] || { echo "Build on Apple Silicon." >&2; exit 1; }

DIST="dist"
rm -rf "$DIST" && mkdir -p "$DIST/dmg"
APP="$DIST/dmg/Inpaint Studio.app"
macos/build-app.sh "$PWD/$APP"

/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
codesign --force --deep -s - "$APP"  # the plist changed, sign again

ditto -c -k --keepParent "$APP" "$DIST/InpaintStudio.zip"
ln -s /Applications "$DIST/dmg/Applications"
hdiutil create -quiet -volname "Inpaint Studio" -srcfolder "$DIST/dmg" -format UDZO -ov "$DIST/InpaintStudio.dmg"
echo "Built: $DIST/InpaintStudio.zip, $DIST/InpaintStudio.dmg"
[ "$DRY" = --dry-run ] && exit 0

NOTES=$(cat <<'EOF'
**Install (recommended):** run this in Terminal. It also updates an existing install.

```bash
curl -fsSL https://raw.githubusercontent.com/leoxnard/inpaint-studio/main/install.sh | bash
```

**Or download the DMG** and drag the app into Applications. The app is not notarized, so macOS blocks
the first start:

1. Open the app and click **Done** in the warning.
2. Go to **System Settings → Privacy & Security** and click **Open Anyway** next to "Inpaint Studio".
3. Confirm with your password. After that it opens normally.

Needs a Mac with Apple Silicon (32 GB RAM recommended).
EOF
)
gh release create "v$VERSION" "$DIST/InpaintStudio.zip" "$DIST/InpaintStudio.dmg" \
  --title "Inpaint Studio $VERSION" --notes "$NOTES"
