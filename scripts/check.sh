#!/usr/bin/env bash
# The full quality gate. Every gate runs; the summary lists PASS / FAIL /
# SKIPPED-EXTERNAL for each, and the script exits non-zero if any gate failed.
# SKIPPED-EXTERNAL means the gate could not run because of the environment (for example
# Docker is not installed); it is never reported as PASS, the reason is printed, and
# AGENTTAPE_STRICT_EXTERNAL=1 (use it in CI) turns any skip into a failure.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

declare -a NAMES=() RESULTS=() NOTES=()
record() { NAMES+=("$1"); RESULTS+=("$2"); NOTES+=("${3:-}"); }

gate() {
  local name="$1"; shift
  echo
  echo "==> $name"
  if "$@"; then record "$name" PASS; else record "$name" FAIL; fi
}

in_backend() { (cd "$ROOT/backend" && "$@"); }
in_frontend() { (cd "$ROOT/frontend" && "$@"); }
in_e2e() { (cd "$ROOT/e2e" && "$@"); }

# CONTRIBUTING.md forbids the em dash (U+2014) everywhere in the repository. The character is
# built from its UTF-8 bytes so this script never contains it. git grep searches tracked
# files only; -I skips binary files (the images). Exit status 0 is a match, 1 is no
# match, anything else is a git error, which must never read as clean.
no_em_dash() {
  local em_dash=$'\xe2\x80\x94' files status
  files="$(git grep -IlF -e "$em_dash" -- . ':(exclude).planning')"
  status=$?
  if [[ $status -eq 0 ]]; then
    echo "em dash (U+2014) found in:"
    printf '%s\n' "$files"
    return 1
  fi
  if [[ $status -eq 1 ]]; then
    return 0
  fi
  echo "git grep failed (exit $status); the em dash check did not run"
  return "$status"
}

gate "no em dashes in tracked files" no_em_dash
gate "CLAUDE.md and AGENTS.md identical" cmp CLAUDE.md AGENTS.md
gate "backend deps in sync" in_backend uv sync --frozen --quiet
gate "python format (ruff format --check)" in_backend uv run --frozen ruff format --check .
gate "python lint (ruff check)" in_backend uv run --frozen ruff check .
gate "python types (mypy --strict)" in_backend uv run --frozen mypy
gate "python tests (pytest)" in_backend uv run --frozen pytest -q
gate "migration smoke (upgrade, downgrade, upgrade, drift)" in_backend uv run --frozen python ../scripts/migration_smoke.py
gate "frontend deps in sync" pnpm install --frozen-lockfile --silent
gate "frontend lint (eslint)" in_frontend pnpm exec eslint .
gate "frontend format (prettier --check)" in_frontend pnpm exec prettier --check .
gate "frontend types (tsc --noEmit)" in_frontend pnpm exec tsc --noEmit -p .
gate "e2e types (tsc --noEmit)" in_e2e pnpm exec tsc --noEmit -p .
gate "frontend unit tests (vitest)" in_frontend pnpm exec vitest run
gate "frontend production build (vite build + package copy)" "$ROOT/scripts/build-frontend.sh"
gate "secret scan (gitleaks when installed, else built-in patterns)" python3 scripts/secret_scan.py
gate "container config static checks (Dockerfile, compose, .dockerignore)" python3 scripts/dockerfile_check.py

echo
echo "==> docker build"
if command -v docker >/dev/null 2>&1; then
  if docker build --quiet -t agenttape:check "$ROOT" >/dev/null; then
    record "docker build" PASS
  else
    record "docker build" FAIL
  fi
else
  echo "docker is not installed on this machine; the image was not built."
  record "docker build" SKIPPED-EXTERNAL "docker not installed (static checks above still ran; see docs/DEPLOYMENT.md)"
fi

echo
echo "==> end-to-end tests (Playwright)"
if (cd "$ROOT/e2e" && pnpm exec playwright install chromium >/dev/null 2>&1); then
  if (cd "$ROOT/e2e" && pnpm exec playwright test); then
    record "end-to-end tests (Playwright)" PASS
  else
    record "end-to-end tests (Playwright)" FAIL
  fi
else
  record "end-to-end tests (Playwright)" SKIPPED-EXTERNAL \
    "Chromium for Playwright could not be installed (network or system libraries). See docs/DEVELOPMENT.md."
fi

echo
echo "================ quality gate summary ================"
failed=0
skipped=0
for i in "${!NAMES[@]}"; do
  printf '%-18s %s\n' "${RESULTS[$i]}" "${NAMES[$i]}"
  [[ -n "${NOTES[$i]}" ]] && printf '%-18s   %s\n' "" "${NOTES[$i]}"
  [[ "${RESULTS[$i]}" == FAIL ]] && failed=1
  [[ "${RESULTS[$i]}" == SKIPPED-EXTERNAL ]] && skipped=1
done
echo "======================================================"
if [[ $failed -ne 0 ]]; then
  echo "check: FAILED"
  exit 1
fi
if [[ $skipped -ne 0 ]]; then
  if [[ "${AGENTTAPE_STRICT_EXTERNAL:-0}" == "1" ]]; then
    echo "check: INCOMPLETE - gates were SKIPPED-EXTERNAL and AGENTTAPE_STRICT_EXTERNAL=1"
    exit 3
  fi
  echo "check: every runnable gate passed; SKIPPED-EXTERNAL gates listed above did NOT run"
  exit 0
fi
echo "check: all gates passed"
