#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/var/log/avatar-single"
RUN_DIR="$ROOT/var/run/avatar-single"
mkdir -p "$LOG_DIR" "$RUN_DIR"

LAUNCHER_PID_FILE="$RUN_DIR/launcher.pid"
PIDS_FILE="$RUN_DIR/pids"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

SYNCTALK_DIR="${SYNCTALK_DIR:-/home/admin-aifc/SyncTalk_2D}"
SYNCTALK_PYTHON_BIN="${SYNCTALK_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
WS_BACKEND_PYTHON_BIN="${WS_BACKEND_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
WS_BACKEND_HOST="${WS_BACKEND_HOST:-0.0.0.0}"

AVATAR_NAME="${SYNCTALK_AVATAR:-aifc-avatar-5-exp-5-v3}"
SYNCTALK_HTTP_PORT="${SYNCTALK_HTTP_PORT:-8005}"
BACKEND_PORT="${WS_BACKEND_PORT:-8080}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
IDLE_VIDEO_SRC="${VITE_IDLE_VIDEO_SRC:-/idle.mp4}"
AVATAR_LABEL="${VITE_AVATAR_LABEL:-$AVATAR_NAME}"
PIDS=()
NAMES=()

"$WS_BACKEND_PYTHON_BIN" -m backend.tools.reset_logs

kill_tree() {
  local pid="$1"
  local child
  while read -r child; do
    [ -n "${child:-}" ] || continue
    kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill "$pid" 2>/dev/null || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ "${#PIDS[@]}" -gt 0 ]; then
    local pid
    for pid in "${PIDS[@]}"; do
      kill_tree "$pid"
    done
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  if [ -f "$LAUNCHER_PID_FILE" ] && [ "$(cat "$LAUNCHER_PID_FILE" 2>/dev/null || true)" = "$$" ]; then
    rm -f "$LAUNCHER_PID_FILE"
  fi
  exit "$status"
}
trap cleanup EXIT INT TERM

start_synctalk() {
  local log_file="$LOG_DIR/synctalk.log"
  : >"$log_file"
  (
    cd "$ROOT"
    exec env \
      PYTHONUNBUFFERED=1 \
      OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
      MKL_NUM_THREADS="${MKL_NUM_THREADS:-4}" \
      OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}" \
      NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-4}" \
      SYNCTALK_AVATAR="$AVATAR_NAME" \
      SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES:-64}" \
      SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S:-0.015}" \
      SYNCTALK_CPU_WORKERS="${SYNCTALK_CPU_WORKERS:-2}" \
      SYNCTALK_GPU_WORKERS="${SYNCTALK_GPU_WORKERS:-2}" \
      "$SYNCTALK_PYTHON_BIN" "$SYNCTALK_DIR/synctalk_server.py" --port "$SYNCTALK_HTTP_PORT"
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("synctalk")
  echo "Started SyncTalk avatar=$AVATAR_NAME on http://localhost:$SYNCTALK_HTTP_PORT"
}

wait_for_synctalk() {
  local port="$1"
  local pid="$2"
  local log_file="$3"
  local attempt

  echo "Waiting for SyncTalk on port $port..."
  for attempt in $(seq 1 120); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "SyncTalk exited before becoming ready. Check $log_file" >&2
      tail -n 80 "$log_file" >&2 || true
      return 1
    fi
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

  echo "SyncTalk did not become ready on port $port. Check $log_file" >&2
  return 1
}

start_backend() {
  local log_file="$LOG_DIR/backend.log"
  : >"$log_file"
  (
    cd "$ROOT"
    exec env \
      WS_BACKEND_PORT="$BACKEND_PORT" \
      SYNCTALK_STREAM_URL="http://127.0.0.1:${SYNCTALK_HTTP_PORT}/infer_stream" \
      INTRO_AVATAR_CACHE_KEY="$AVATAR_NAME" \
      MEDIA_KEEPWARM_ENABLED="${MEDIA_KEEPWARM_ENABLED:-false}" \
      LOCAL_RAG_STARTUP_PREWARM="${LOCAL_RAG_STARTUP_PREWARM:-false}" \
      LOCAL_TTS_STARTUP_PREWARM="${LOCAL_TTS_STARTUP_PREWARM:-false}" \
      INTRO_AUDIO_CACHE_PREBUILD="${INTRO_AUDIO_CACHE_PREBUILD:-false}" \
      SONIOX_STT_PRECONNECT="${SONIOX_STT_PRECONNECT:-false}" \
      RESET_LOGS_ON_START=false \
      AVATAR_TTS_FIRST_SEGMENT_MS="${AVATAR_TTS_FIRST_SEGMENT_MS:-320}" \
      AVATAR_TTS_SEGMENT_MS="${AVATAR_TTS_SEGMENT_MS:-900}" \
      AVATAR_TTS_MIN_SEGMENT_MS="${AVATAR_TTS_MIN_SEGMENT_MS:-450}" \
      AVATAR_TTS_MAX_SEGMENT_MS="${AVATAR_TTS_MAX_SEGMENT_MS:-1400}" \
      FIRST_TTS_CHARS="${FIRST_TTS_CHARS:-48}" \
      MIN_TTS_CHARS="${MIN_TTS_CHARS:-80}" \
      MAX_TTS_CHARS="${MAX_TTS_CHARS:-220}" \
      "$WS_BACKEND_PYTHON_BIN" -m uvicorn backend.app.main:app --host "$WS_BACKEND_HOST" --port "$BACKEND_PORT"
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("backend")
  echo "Started ws-backend on http://localhost:$BACKEND_PORT"
}

wait_for_backend() {
  local port="$1"
  local pid="$2"
  local log_file="$3"
  local attempt

  echo "Waiting for ws-backend on port $port..."
  for attempt in $(seq 1 120); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "ws-backend exited before becoming ready. Check $log_file" >&2
      tail -n 80 "$log_file" >&2 || true
      return 1
    fi
    if "$WS_BACKEND_PYTHON_BIN" - "$port" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

with urllib.request.urlopen(f"http://127.0.0.1:{int(sys.argv[1])}/health", timeout=1) as response:
    if response.status != 200:
        raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 1
  done

  echo "ws-backend did not become ready on port $port. Check $log_file" >&2
  return 1
}

start_frontend() {
  local log_file="$LOG_DIR/frontend.log"
  : >"$log_file"
  (
    cd "$ROOT/frontend"
    exec env \
      WS_BACKEND_PORT="$BACKEND_PORT" \
      VITE_BACKEND_HTTP_URL="http://localhost:${BACKEND_PORT}" \
      VITE_BACKEND_WS_URL="ws://localhost:${BACKEND_PORT}" \
      VITE_IDLE_VIDEO_SRC="$IDLE_VIDEO_SRC" \
      VITE_AVATAR_LABEL="$AVATAR_LABEL" \
      RESET_LOGS_ON_START=false \
      npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT" --strictPort
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("frontend")
  echo "Started frontend on http://localhost:$FRONTEND_PORT"
}

write_pid_file() {
  : >"$PIDS_FILE"
  local i
  for i in "${!PIDS[@]}"; do
    printf '%s %s\n' "${PIDS[$i]}" "${NAMES[$i]}" >>"$PIDS_FILE"
  done
}

printf "%s\n" "$$" >"$LAUNCHER_PID_FILE"

start_synctalk
wait_for_synctalk "$SYNCTALK_HTTP_PORT" "${PIDS[-1]}" "$LOG_DIR/synctalk.log"
start_backend
wait_for_backend "$BACKEND_PORT" "${PIDS[-1]}" "$LOG_DIR/backend.log"
start_frontend
write_pid_file

echo

echo "Single avatar stack running"
echo "  Avatar: $AVATAR_NAME"
echo "  SyncTalk: http://localhost:${SYNCTALK_HTTP_PORT}"
echo "  Backend: http://localhost:${BACKEND_PORT}"
echo "  Frontend: http://localhost:${FRONTEND_PORT}"
echo "Logs: $LOG_DIR"
echo

echo "Press Ctrl+C to stop all"

sleep 4
for i in "${!PIDS[@]}"; do
  if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
    echo "${NAMES[$i]} exited during startup. Check logs in $LOG_DIR" >&2
    exit 1
  fi
done

while true; do
  sleep 2
  for i in "${!PIDS[@]}"; do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      echo "${NAMES[$i]} stopped. Stopping remaining processes." >&2
      exit 1
    fi
  done
done
