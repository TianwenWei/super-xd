#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null 2>&1; then uv venv --python 3.12 .venv; else python3 -m venv .venv; fi
fi
if command -v uv >/dev/null 2>&1; then uv pip sync --python .venv/bin/python requirements.lock.txt; else .venv/bin/python -m pip install -r requirements.lock.txt; fi
if [ ! -d frontend/node_modules ]; then (cd frontend && npm ci); fi
mkdir -p data
children=()
cleanup() { trap - EXIT INT TERM; kill "${children[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM
.venv/bin/python -m uvicorn backend.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" >data/server.log 2>&1 & children+=("$!")
.venv/bin/python -m backend.worker >data/worker.log 2>&1 & children+=("$!")
(cd frontend && exec npm run dev -- --host 127.0.0.1) >data/frontend.log 2>&1 & children+=("$!")
printf '\n车机验证界面: http://127.0.0.1:5173\nAPI: http://127.0.0.1:8000/docs\n日志: data/{server,worker,frontend}.log\n按 Ctrl+C 停止全部三个进程。\n'
wait "${children[@]}"
