#!/usr/bin/env bash
# Dev workflow: runs the Python server on 8099 AND the Vite dev server on
# 5173 with HMR. Vite proxies /api/* to the Python server so SSE works
# end to end. Ctrl+C cleans up both.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  echo "error: .venv not found. Create it with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

if [[ ! -d ui/node_modules ]]; then
  echo "Installing UI deps..."
  (cd ui && npm install --no-audit --no-fund)
fi

cleanup() {
  echo
  echo "Stopping dev servers..."
  kill "$PY_PID" "$UI_PID" 2>/dev/null || true
  wait "$PY_PID" "$UI_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Starting Python server on :8099 …"
.venv/bin/python -m mylo &
PY_PID=$!

echo "Starting Vite on :5173 …"
(cd ui && npx vite) &
UI_PID=$!

echo
echo "  UI:     http://localhost:5173"
echo "  API:    http://localhost:8099"
echo

# Poll for either process exiting — `wait -n` needs bash 4.3+ which
# macOS doesn't ship. Sleep loop is cheap and pid-safe.
while kill -0 "$PY_PID" 2>/dev/null && kill -0 "$UI_PID" 2>/dev/null; do
  sleep 1
done
