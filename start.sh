#!/usr/bin/env bash

set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

cleanup() {
  kill "${backend_pid:-}" "${frontend_pid:-}" 2>/dev/null || true
}

trap cleanup EXIT INT TERM

(
  cd "$repo_dir/backend"
  uv run uvicorn viberadio.main:app --reload
) &
backend_pid=$!

(
  cd "$repo_dir/frontend"
  npm run dev
) &
frontend_pid=$!

wait -n "$backend_pid" "$frontend_pid"