#!/usr/bin/env bash
# Development: API + worker (+ any registered services) on :8787 and Vite on :5173 with
# /api proxied to the backend. Ctrl+C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${AGENTTAPE_PORT:-8787}"
cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM
(cd "$ROOT/backend" && uv run agenttape serve --port "$PORT") &
(cd "$ROOT/frontend" && AGENTTAPE_API_URL="http://127.0.0.1:$PORT" pnpm dev --host 127.0.0.1) &
wait
