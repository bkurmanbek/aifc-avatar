#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND_DIR="$ROOT/frontend"
LOG_DIR="$ROOT/var/log/avatar-comparison"
RUN_DIR="$ROOT/var/run/avatar-comparison"
mkdir -p "$LOG_DIR" "$RUN_DIR"
LAUNCHER_PID_FILE="$RUN_DIR/launcher.pid"

OVERRIDE_SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES-}"
OVERRIDE_SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S-}"
OVERRIDE_SYNCTALK_BATCH_SIZE="${SYNCTALK_BATCH_SIZE-}"
OVERRIDE_SYNCTALK_CPU_WORKERS="${SYNCTALK_CPU_WORKERS-}"
OVERRIDE_SYNCTALK_GPU_WORKERS="${SYNCTALK_GPU_WORKERS-}"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi

if [ -n "$OVERRIDE_SYNCTALK_BATCH_SIZE" ]; then
  SYNCTALK_BATCH_SIZE="$OVERRIDE_SYNCTALK_BATCH_SIZE"
else
  SYNCTALK_BATCH_SIZE="${SYNCTALK_COMPARISON_BATCH_SIZE:-32}"
fi
if [ -n "$OVERRIDE_SYNCTALK_MAX_FRAMES" ]; then
  SYNCTALK_MAX_FRAMES="$OVERRIDE_SYNCTALK_MAX_FRAMES"
else
  SYNCTALK_MAX_FRAMES="${SYNCTALK_COMPARISON_MAX_FRAMES:-64}"
fi
if [ -n "$OVERRIDE_SYNCTALK_MAX_WAIT_S" ]; then
  SYNCTALK_MAX_WAIT_S="$OVERRIDE_SYNCTALK_MAX_WAIT_S"
else
  SYNCTALK_MAX_WAIT_S="${SYNCTALK_COMPARISON_MAX_WAIT_S:-0.015}"
fi
if [ -n "$OVERRIDE_SYNCTALK_CPU_WORKERS" ]; then
  SYNCTALK_CPU_WORKERS="$OVERRIDE_SYNCTALK_CPU_WORKERS"
else
  SYNCTALK_CPU_WORKERS="${SYNCTALK_COMPARISON_CPU_WORKERS:-4}"
fi
if [ -n "$OVERRIDE_SYNCTALK_GPU_WORKERS" ]; then
  SYNCTALK_GPU_WORKERS="$OVERRIDE_SYNCTALK_GPU_WORKERS"
else
  SYNCTALK_GPU_WORKERS="${SYNCTALK_COMPARISON_GPU_WORKERS:-4}"
fi

SYNCTALK_DIR="${SYNCTALK_DIR:-/home/admin-aifc/SyncTalk_2D}"
SYNCTALK_PYTHON_BIN="${SYNCTALK_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
WS_BACKEND_PYTHON_BIN="${WS_BACKEND_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
WS_BACKEND_HOST="${WS_BACKEND_HOST:-0.0.0.0}"

AVATAR_A_NAME="${AVATAR_A_NAME:-aifc-avatar-5-3min_exp_6}"
AVATAR_B_NAME="${AVATAR_B_NAME:-aifc-avatar-5-exp-3}"

IDLE_A_SRC="${IDLE_A_SRC:-/idle.mp4?v=compare-a}"
IDLE_B_SRC="${IDLE_B_SRC:-/idle-2.mp4?v=compare-b}"

SYNCTALK_A_PORT="${SYNCTALK_A_PORT:-8105}"
SYNCTALK_B_PORT="${SYNCTALK_B_PORT:-8106}"
BACKEND_A_PORT="${BACKEND_A_PORT:-8180}"
BACKEND_B_PORT="${BACKEND_B_PORT:-8181}"
FRONTEND_A_PORT="${FRONTEND_A_PORT:-15273}"
FRONTEND_B_PORT="${FRONTEND_B_PORT:-15274}"
SYNCTALK_READY_TIMEOUT_S="${SYNCTALK_READY_TIMEOUT_S:-900}"

PIDS=()
NAMES=()

"$WS_BACKEND_PYTHON_BIN" -m backend.tools.reset_logs

start_synctalk() {
  local label="$1"
  local avatar="$2"
  local port="$3"
  local log_file="$LOG_DIR/${label}-synctalk.log"
  : >"$log_file"

  (
    cd "$ROOT"
    exec env \
      PYTHONUNBUFFERED=1 \
      OMP_NUM_THREADS="${OMP_NUM_THREADS:-4}" \
      MKL_NUM_THREADS="${MKL_NUM_THREADS:-4}" \
      OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-4}" \
      NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-4}" \
      SYNCTALK_AVATAR="$avatar" \
      SYNCTALK_BATCH_SIZE="${SYNCTALK_BATCH_SIZE:-32}" \
      SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES:-64}" \
      SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S:-0.015}" \
      SYNCTALK_CPU_WORKERS="${SYNCTALK_CPU_WORKERS:-2}" \
      SYNCTALK_GPU_WORKERS="${SYNCTALK_GPU_WORKERS:-2}" \
      "$SYNCTALK_PYTHON_BIN" "$SYNCTALK_DIR/synctalk_server.py" --port "$port"
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("$label synctalk")
  echo "$label SyncTalk: avatar=$avatar port=$port log=$log_file"
}

wait_for_synctalk() {
  local label="$1"
  local port="$2"
  local pid="$3"
  local log_file="$LOG_DIR/${label}-synctalk.log"

  echo "Waiting for $label SyncTalk on port $port..."
  local attempt
  for attempt in $(seq 1 "$SYNCTALK_READY_TIMEOUT_S"); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$label SyncTalk exited before becoming ready. Check $log_file" >&2
      tail -n 80 "$log_file" >&2 || true
      return 1
    fi
    if "$SYNCTALK_PYTHON_BIN" - "$port" <<'PY' >/dev/null 2>&1
import socket
import sys

port = int(sys.argv[1])
with socket.create_connection(("127.0.0.1", port), timeout=1):
    pass
PY
    then
      echo "$label SyncTalk ready on port $port"
      return 0
    fi
    sleep 1
  done

  echo "$label SyncTalk did not become ready on port $port. Check $log_file" >&2
  return 1
}

start_backend() {
  local label="$1"
  local backend_port="$2"
  local synctalk_port="$3"
  local avatar="$4"
  local log_file="$LOG_DIR/${label}-backend.log"
  : >"$log_file"

  (
    cd "$ROOT"
    exec env \
      WS_BACKEND_PORT="$backend_port" \
      SYNCTALK_STREAM_URL="http://127.0.0.1:${synctalk_port}/infer_stream" \
      INTRO_AVATAR_CACHE_KEY="$avatar" \
      LOCAL_RAG_STARTUP_PREWARM=false \
      LOCAL_TTS_STARTUP_PREWARM=false \
      INTRO_AUDIO_CACHE_PREBUILD=false \
      MEDIA_KEEPWARM_ENABLED=false \
      SONIOX_STT_PRECONNECT=false \
      RESET_LOGS_ON_START=false \
      AVATAR_TTS_FIRST_SEGMENT_MS="${AVATAR_TTS_FIRST_SEGMENT_MS:-320}" \
      AVATAR_TTS_SEGMENT_MS="${AVATAR_TTS_SEGMENT_MS:-900}" \
      AVATAR_TTS_MIN_SEGMENT_MS="${AVATAR_TTS_MIN_SEGMENT_MS:-450}" \
      AVATAR_TTS_MAX_SEGMENT_MS="${AVATAR_TTS_MAX_SEGMENT_MS:-1400}" \
      FIRST_TTS_CHARS="${FIRST_TTS_CHARS:-48}" \
      MIN_TTS_CHARS="${MIN_TTS_CHARS:-80}" \
      MAX_TTS_CHARS="${MAX_TTS_CHARS:-220}" \
      GEMINI_MAX_OUTPUT_TOKENS="${GEMINI_MAX_OUTPUT_TOKENS:-650}" \
      ANSWER_DETAIL_MAX_POINTS="${ANSWER_DETAIL_MAX_POINTS:-5}" \
      ANSWER_DETAIL_MAX_SECTIONS="${ANSWER_DETAIL_MAX_SECTIONS:-2}" \
      ANSWER_DETAIL_MAX_SECTION_ITEMS="${ANSWER_DETAIL_MAX_SECTION_ITEMS:-4}" \
      ANSWER_VOICE_MAX_CHARS="${ANSWER_VOICE_MAX_CHARS:-900}" \
      "$WS_BACKEND_PYTHON_BIN" -m uvicorn backend.app.main:app --host "$WS_BACKEND_HOST" --port "$backend_port"
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("$label backend")
  echo "$label backend: port=$backend_port synctalk=$synctalk_port log=$log_file"
}

start_frontend() {
  local label="$1"
  local display_name="$2"
  local frontend_port="$3"
  local backend_port="$4"
  local idle_src="$5"
  local log_file="$LOG_DIR/${label}-frontend.log"
  : >"$log_file"

  (
    cd "$FRONTEND_DIR"
    exec env \
      WS_BACKEND_PORT="$backend_port" \
      VITE_BACKEND_HTTP_URL="http://localhost:${backend_port}" \
      VITE_BACKEND_WS_URL="ws://localhost:${backend_port}" \
      VITE_IDLE_VIDEO_SRC="$idle_src" \
      VITE_AVATAR_LABEL="$display_name" \
      RESET_LOGS_ON_START=false \
      npm run dev -- --host 0.0.0.0 --port "$frontend_port" --strictPort
  ) >"$log_file" 2>&1 &

  PIDS+=("$!")
  NAMES+=("$label frontend")
  echo "$label frontend: http://localhost:$frontend_port backend=$backend_port idle=$idle_src log=$log_file"
}

write_pid_file() {
  local pid_file="$RUN_DIR/pids"
  : >"$pid_file"
  local i
  for i in "${!PIDS[@]}"; do
    printf "%s %s\n" "${PIDS[$i]}" "${NAMES[$i]}" >>"$pid_file"
  done
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ "${#PIDS[@]}" -gt 0 ]; then
    echo
    echo "Stopping avatar comparison processes..."
    kill "${PIDS[@]}" 2>/dev/null || true
    wait "${PIDS[@]}" 2>/dev/null || true
  fi
  rm -f "$LAUNCHER_PID_FILE"
  exit "$status"
}
trap cleanup EXIT INT TERM

printf "%s\n" "$$" >"$LAUNCHER_PID_FILE"
: >"$RUN_DIR/pids"

echo "Starting avatar comparison stack..."
start_synctalk "avatar-a" "$AVATAR_A_NAME" "$SYNCTALK_A_PORT"
wait_for_synctalk "avatar-a" "$SYNCTALK_A_PORT" "${PIDS[$((${#PIDS[@]} - 1))]}"
start_backend "avatar-a" "$BACKEND_A_PORT" "$SYNCTALK_A_PORT" "$AVATAR_A_NAME"
start_frontend "avatar-a" "$AVATAR_A_NAME" "$FRONTEND_A_PORT" "$BACKEND_A_PORT" "$IDLE_A_SRC"

start_synctalk "avatar-b" "$AVATAR_B_NAME" "$SYNCTALK_B_PORT"
wait_for_synctalk "avatar-b" "$SYNCTALK_B_PORT" "${PIDS[$((${#PIDS[@]} - 1))]}"
start_backend "avatar-b" "$BACKEND_B_PORT" "$SYNCTALK_B_PORT" "$AVATAR_B_NAME"
start_frontend "avatar-b" "$AVATAR_B_NAME" "$FRONTEND_B_PORT" "$BACKEND_B_PORT" "$IDLE_B_SRC"

write_pid_file

echo
echo "Open these URLs for the comparison:"
echo "  Avatar A ($AVATAR_A_NAME): http://localhost:$FRONTEND_A_PORT"
echo "  Avatar B ($AVATAR_B_NAME): http://localhost:$FRONTEND_B_PORT"
echo
echo "Logs are in $LOG_DIR"
echo "Press Ctrl+C in this terminal to stop all comparison processes."

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
      echo "${NAMES[$i]} stopped. Check logs in $LOG_DIR" >&2
      exit 1
    fi
  done
done
