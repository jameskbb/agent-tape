#!/usr/bin/env bash
# Starts an isolated AgentTape server for Playwright: fresh temp data dir, migrated and
# seeded database, built UI, in-process worker. Never touches ~/.agenttape. On exit (or
# SIGTERM/SIGINT from Playwright) it stops the server and the fake Herdr and removes both
# temp dirs.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${E2E_PORT:-8799}"
if [[ ! -f "$ROOT/backend/src/agenttape/web_dist/index.html" ]]; then
  echo "e2e-server: the web UI is not built. Run 'make build' first." >&2
  exit 1
fi
DATA_DIR="$(mktemp -d -t agenttape-e2e-XXXXXX)"
HERDR_DIR="$(mktemp -d -t ath-XXXXXX)"
HERDR_PID=""
SERVER_PID=""
cleanup() {
  for pid in $SERVER_PID $HERDR_PID; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  rm -rf "$DATA_DIR" "$HERDR_DIR"
}
trap cleanup EXIT
trap 'exit 143' TERM INT
export AGENTTAPE_DATA_DIR="$DATA_DIR"
export AGENTTAPE_ALLOWED_ROOTS="$DATA_DIR"
export AGENTTAPE_DEMO_STEP_DELAY_MS="${AGENTTAPE_DEMO_STEP_DELAY_MS:-150}"
cd "$ROOT/backend"
# A fake Herdr server (never the user's real Herdr) for the Herdr runtime specs; its panes
# run the real evidence demo agent.
uv run --frozen python tests/fixtures/fake_herdr.py --socket "$HERDR_DIR/h.sock" >/dev/null &
HERDR_PID=$!
export AGENTTAPE_HERDR_SOCKET="$HERDR_DIR/h.sock"
export AGENTTAPE_HERDR_EXECUTABLE="$HERDR_DIR/no-herdr-cli"
for _ in $(seq 1 100); do [[ -S "$AGENTTAPE_HERDR_SOCKET" ]] && break; sleep 0.1; done
uv run --frozen agenttape init >/dev/null
uv run --frozen agenttape seed >/dev/null
# Not exec: the traps above must stay installed to clean up.
uv run --frozen agenttape serve --host 127.0.0.1 --port "$PORT" &
SERVER_PID=$!
wait "$SERVER_PID"
