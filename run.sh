#!/usr/bin/env bash
# Inpaint Studio launcher: syncs deps with uv, opens the browser, serves the UI.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-7380}"
uv sync -q
( sleep 1.5; open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
exec uv run uvicorn server:app --host 127.0.0.1 --port "$PORT"
