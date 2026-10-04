#!/usr/bin/env bash
# Installs or updates Inpaint Studio from the latest GitHub release:
#   curl -fsSL https://raw.githubusercontent.com/leoxnard/inpaint-studio/main/install.sh | bash
# curl does not set the quarantine attribute, so Gatekeeper does not block the unsigned app.
# Everything runs inside main() so a cut-off download never runs half a script.
set -euo pipefail

main() {
  local url="https://github.com/leoxnard/inpaint-studio/releases/latest/download/InpaintStudio.zip"
  local name="Inpaint Studio.app"

  if [ "$(uname -s)" != Darwin ] || [ "$(uname -m)" != arm64 ]; then
    echo "Inpaint Studio needs a Mac with Apple Silicon." >&2
    exit 1
  fi

  local dir="/Applications"
  [ -w "$dir" ] || { dir="$HOME/Applications"; mkdir -p "$dir"; }
  local app="$dir/$name"

  tmp="$(mktemp -d)"  # global: the EXIT trap runs after main has returned
  trap 'rm -rf "$tmp"' EXIT

  echo "Downloading Inpaint Studio…"
  curl -fL --progress-bar "$url" -o "$tmp/app.zip"
  ditto -x -k "$tmp/app.zip" "$tmp"

  if pgrep -f "$app/Contents/MacOS/" >/dev/null; then
    echo "Quitting the running copy…"
    osascript -e "tell application \"$app\" to quit" 2>/dev/null || true
    for _ in $(seq 1 40); do pgrep -f "$app/Contents/MacOS/" >/dev/null || break; sleep 0.5; done
    if pgrep -f "$app/Contents/MacOS/" >/dev/null; then
      echo "Inpaint Studio is still running (jobs left?). Quit it and run this again." >&2
      exit 1
    fi
  fi

  rm -rf "$app"
  mv "$tmp/$name" "$app"
  echo "Installed: $app"
  open "$app"
}

main "$@"
