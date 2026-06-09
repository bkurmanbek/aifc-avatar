#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_DIR="$(cd "$PROJECT_DIR/../.." && pwd)"

cd "$PROJECT_DIR"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

SYNCTALK_DIR="${SYNCTALK_DIR:-$BASE_DIR/SyncTalk_2D}"
PYTHON_BIN="${SYNCTALK_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
PORT="${SYNCTALK_HTTP_PORT:-8005}"

exec env SYNCTALK_AVATAR="${SYNCTALK_AVATAR:-aifc-avatar-5-exp-5-v3}" \
  SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES:-64}" \
  SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S:-0.015}" \
  "$PYTHON_BIN" "$SYNCTALK_DIR/synctalk_server.py" --port "$PORT"
