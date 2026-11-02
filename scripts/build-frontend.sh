#!/usr/bin/env bash
# Production build of the web UI, copied into the Python package so `agenttape serve`
# serves it (backend/src/agenttape/web_dist is git-ignored build output).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pnpm --dir "$ROOT/frontend" build
rm -rf "$ROOT/backend/src/agenttape/web_dist"
cp -r "$ROOT/frontend/dist" "$ROOT/backend/src/agenttape/web_dist"
echo "web UI built into backend/src/agenttape/web_dist"
