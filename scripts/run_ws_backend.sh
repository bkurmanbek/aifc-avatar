#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

PYTHON_BIN="${WS_BACKEND_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
exec "$PYTHON_BIN" -m uvicorn ws_backend.app:app --host 0.0.0.0 --port "${WS_BACKEND_PORT:-8080}"
