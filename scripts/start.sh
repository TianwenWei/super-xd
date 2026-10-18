#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
  printf 'Install uv before starting the Demo.\n' >&2
  exit 1
fi
extras=(--extra demo --extra manual-index)
# Preserve the optional local VAD adapter; WITH_LOCAL_VAD=1 also enables it explicitly.
if [ "${WITH_LOCAL_VAD:-0}" = 1 ] || { [ -x .venv/bin/python ] && .venv/bin/python -c 'import importlib.util; raise SystemExit(importlib.util.find_spec("onnxruntime") is None)'; }; then
  extras+=(--extra local-vad)
fi
uv sync --locked "${extras[@]}"
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
.venv/bin/python -m uvicorn super_xd.demo.api.app:app --host "${HOST:-127.0.0.1}" --port "$api_port" >data/server.log 2>&1 & children+=("$!")
.venv/bin/python -m super_xd.demo.tasks.worker >data/worker.log 2>&1 & children+=("$!")
(cd frontend && exec npm run dev -- --host 127.0.0.1) >data/frontend.log 2>&1 & children+=("$!")
printf '\n车机验证界面: %s\nAPI: %s/docs\n日志: data/{server,worker,frontend}.log\n按 Ctrl+C 停止全部三个进程。\n' "$FRONTEND_ORIGIN" "$API_ORIGIN"
wait "${children[@]}"
