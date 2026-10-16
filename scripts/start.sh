#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then uv venv --python 3.12 .venv; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null 2>&1; then uv pip sync --python .venv/bin/python requirements.lock.txt; else .venv/bin/python -m pip install -r requirements.lock.txt; fi
if [ ! -d frontend/node_modules ]; then (cd frontend && npm ci); fi
mkdir -p data
api_port="${PORT:-8000}"
frontend_port="${FRONTEND_PORT:-5173}"
export FRONTEND_ORIGIN="${FRONTEND_ORIGIN:-http://127.0.0.1:$frontend_port}"
export API_ORIGIN="${API_ORIGIN:-http://127.0.0.1:$api_port}"
export VITE_PORT="$frontend_port"
export VITE_API_TARGET="$API_ORIGIN"
children=()
cleanup() { trap - EXIT INT TERM; kill "${children[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn backend.app:app --host "${HOST:-127.0.0.1}" --port "$api_port" >data/server.log 2>&1 & children+=("$!")
.venv/bin/python -m backend.worker >data/worker.log 2>&1 & children+=("$!")
(cd frontend && exec npm run dev -- --host 127.0.0.1) >data/frontend.log 2>&1 & children+=("$!")
printf '\n车机验证界面: %s\nAPI: %s/docs\n日志: data/{server,worker,frontend}.log\n按 Ctrl+C 停止全部三个进程。\n' "$FRONTEND_ORIGIN" "$API_ORIGIN"
wait "${children[@]}"
