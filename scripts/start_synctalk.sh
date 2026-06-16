#!/usr/bin/env bash
# Start the SyncTalk inference server as a persistent background process.
#
# This script is idempotent: if SyncTalk is already responding on its port,
# it does nothing. Backend and frontend restarts do NOT need to touch this.
# Only restart SyncTalk when you change the checkpoint or server code.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# Load .env then config.env (config.env is the base; .env overrides)
if [ -f config.env ]; then
  set -a; . ./config.env; set +a
fi
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

SYNCTALK_DIR="${SYNCTALK_DIR:-/home/admin-aifc/SyncTalk_2D}"
PYTHON_BIN="${SYNCTALK_PYTHON:-/home/admin-aifc/miniforge3/envs/synctalk2d/bin/python}"
PORT="${SYNCTALK_HTTP_PORT:-8005}"
AVATAR="${SYNCTALK_AVATAR:-aifc-avatar-5-3min_exp_6}"
LOG_FILE="${PROJECT_DIR}/var/synctalk.log"
PID_FILE="${PROJECT_DIR}/var/synctalk.pid"

mkdir -p "$(dirname "$LOG_FILE")"

# ── Check if already running ───────────────────────────────────────────────────
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "SyncTalk already running (PID $OLD_PID, avatar=$AVATAR, port=$PORT) — nothing to do."
    exit 0
  else
    echo "Stale PID file ($OLD_PID) — cleaning up."
    rm -f "$PID_FILE"
  fi
fi

# Also check by port in case PID file is missing
if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
  echo "SyncTalk already responding on port $PORT — nothing to do."
  exit 0
fi

# ── Start SyncTalk ─────────────────────────────────────────────────────────────
echo "Starting SyncTalk server..."
echo "  avatar   : $AVATAR"
echo "  port     : $PORT"
echo "  log      : $LOG_FILE"
echo "  python   : $PYTHON_BIN"

env \
  SYNCTALK_AVATAR="$AVATAR" \
  SYNCTALK_MAX_FRAMES="${SYNCTALK_MAX_FRAMES:-64}" \
  SYNCTALK_MAX_WAIT_S="${SYNCTALK_MAX_WAIT_S:-0.015}" \
  SYNCTALK_CPU_WORKERS="${SYNCTALK_CPU_WORKERS:-4}" \
  SYNCTALK_GPU_WORKERS="${SYNCTALK_GPU_WORKERS:-4}" \
  SYNCTALK_BATCH_SIZE="${SYNCTALK_BATCH_SIZE:-32}" \
  "$PYTHON_BIN" "$SYNCTALK_DIR/synctalk_server.py" --port "$PORT" \
  >> "$LOG_FILE" 2>&1 &

SYNCTALK_PID=$!
echo "$SYNCTALK_PID" > "$PID_FILE"
echo "SyncTalk started (PID $SYNCTALK_PID) — waiting for readiness..."

# ── Wait until ready (up to 5 minutes — first load is slow) ───────────────────
WAITED=0
TIMEOUT=300
while [ "$WAITED" -lt "$TIMEOUT" ]; do
  if ! kill -0 "$SYNCTALK_PID" 2>/dev/null; then
    echo "ERROR: SyncTalk process ($SYNCTALK_PID) exited early. Check $LOG_FILE"
    rm -f "$PID_FILE"
    exit 1
  fi
  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "SyncTalk ready after ${WAITED}s (PID $SYNCTALK_PID)."
    exit 0
  fi
  sleep 5
  WAITED=$((WAITED + 5))
  echo "  still loading... ${WAITED}s"
done

echo "ERROR: SyncTalk did not become ready within ${TIMEOUT}s. Check $LOG_FILE"
exit 1
