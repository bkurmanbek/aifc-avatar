#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/var/log/intro-cache"
RUN_DIR="$ROOT/var/run/intro-cache"
mkdir -p "$LOG_DIR" "$RUN_DIR"
SCRIPT_ARGS=("$@")

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi

SYNCTALK_DIR="${SYNCTALK_DIR:-/home/admin-aifc/SyncTalk_2D}"
SYNCTALK_PYTHON_BIN="${SYNCTALK_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
WS_BACKEND_PYTHON_BIN="${WS_BACKEND_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
SYNCTALK_CACHE_PORT="${SYNCTALK_CACHE_PORT:-8095}"
AVATAR_A_NAME="${AVATAR_A_NAME:-aifc-avatar-5-3min_exp_6}"
AVATAR_B_NAME="${AVATAR_B_NAME:-aifc-avatar-5-exp-3}"

wait_for_port() {
  local port="$1"
  for _ in $(seq 1 180); do
    if "$SYNCTALK_PYTHON_BIN" - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

with socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=1):
    pass
PY
    then
      return 0
    fi
    sleep 1
  done
  return 1
}

prebuild_avatar() {
  local label="$1"
  local avatar="$2"
  local synctalk_log="$LOG_DIR/${label}-synctalk.log"
  local prebuild_log="$LOG_DIR/${label}-prebuild.log"
  local pid=""

  : >"$synctalk_log"
  : >"$prebuild_log"
  echo "Starting SyncTalk for $label: $avatar"
  (
    cd "$ROOT"
    exec env \
      PYTHONUNBUFFERED=1 \
      SYNCTALK_AVATAR="$avatar" \
      SYNCTALK_BATCH_SIZE="${SYNCTALK_BATCH_SIZE:-32}" \
      SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES:-64}" \
      SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S:-0.015}" \
      SYNCTALK_CPU_WORKERS="${SYNCTALK_CPU_WORKERS:-4}" \
      SYNCTALK_GPU_WORKERS="${SYNCTALK_GPU_WORKERS:-4}" \
      "$SYNCTALK_PYTHON_BIN" "$SYNCTALK_DIR/synctalk_server.py" --port "$SYNCTALK_CACHE_PORT"
  ) >"$synctalk_log" 2>&1 &
  pid="$!"

  cleanup_avatar() {
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  }
  trap cleanup_avatar RETURN

  echo "Waiting for SyncTalk port $SYNCTALK_CACHE_PORT..."
  if ! wait_for_port "$SYNCTALK_CACHE_PORT"; then
    echo "SyncTalk failed to start for $label. Check $synctalk_log" >&2
    return 1
  fi

  echo "Prebuilding intro cache for $label..."
  (
    cd "$ROOT"
    exec env \
      INTRO_AVATAR_CACHE_KEY="$avatar" \
      PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
      PYTHONUNBUFFERED=1 \
      SYNCTALK_STREAM_URL="http://127.0.0.1:${SYNCTALK_CACHE_PORT}/infer_stream" \
      "$WS_BACKEND_PYTHON_BIN" scripts/prebuild_intro_cache.py "${SCRIPT_ARGS[@]}"
  ) >"$prebuild_log" 2>&1
  echo "$label intro cache ready. Logs: $prebuild_log"
}

prebuild_avatar "avatar-a" "$AVATAR_A_NAME" "$@"
prebuild_avatar "avatar-b" "$AVATAR_B_NAME" "$@"

echo "Intro caches are ready for:"
echo "  $AVATAR_A_NAME"
echo "  $AVATAR_B_NAME"
