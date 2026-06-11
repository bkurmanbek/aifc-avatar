#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT/var/log/avatar-comparison"
RUN_DIR="$ROOT/var/run/avatar-comparison"
mkdir -p "$LOG_DIR" "$RUN_DIR"

WATCHER_PID_FILE="$RUN_DIR/keepwarm.pid"
STACK_LOG_FILE="$LOG_DIR/keep-warm-stack.log"
WATCHER_LOG_FILE="$LOG_DIR/keep-warm-watcher.log"

CHECK_INTERVAL_SECONDS="${KEEPWARM_CHECK_INTERVAL_SECONDS:-5}"
STACK_READY_TIMEOUT_SECONDS="${KEEPWARM_STACK_READY_TIMEOUT_SECONDS:-120}"
RESTART_COOLDOWN_SECONDS="${KEEPWARM_RESTART_COOLDOWN_SECONDS:-5}"
SHUTDOWN_STACK_ON_EXIT="${KEEPWARM_SHUTDOWN_STACK_ON_EXIT:-1}"

if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT/.env"
  set +a
fi

SYNCTALK_A_PORT="${SYNCTALK_A_PORT:-8105}"
SYNCTALK_B_PORT="${SYNCTALK_B_PORT:-8106}"
BACKEND_A_PORT="${BACKEND_A_PORT:-8180}"
BACKEND_B_PORT="${BACKEND_B_PORT:-8181}"
FRONTEND_A_PORT="${FRONTEND_A_PORT:-15273}"
FRONTEND_B_PORT="${FRONTEND_B_PORT:-15274}"
PIDS_FILE="$RUN_DIR/pids"

log_msg() {
  printf "[%s] %s\n" "$(date '+%F %T')" "$*" | tee -a "$WATCHER_LOG_FILE"
}

is_port_open() {
  local port="$1"
  (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
}

check_processes_and_ports() {
  local missing=0
  for port in \
    "$SYNCTALK_A_PORT" "$SYNCTALK_B_PORT" \
    "$BACKEND_A_PORT" "$BACKEND_B_PORT" \
    "$FRONTEND_A_PORT" "$FRONTEND_B_PORT"
  do
    if ! is_port_open "$port"; then
      missing=1
      break
    fi
  done

  if [ "$missing" -ne 0 ]; then
    return 1
  fi

  if [ -f "$PIDS_FILE" ]; then
    local line pid
    while read -r pid _; do
      [ -n "${pid:-}" ] || continue
      if ! kill -0 "$pid" 2>/dev/null; then
        return 1
      fi
    done <"$PIDS_FILE"
  fi

  return 0
}

start_stack() {
  bash "$ROOT/scripts/stop_avatar_comparison.sh" >/dev/null 2>&1 || true
  rm -f "$PIDS_FILE"

  log_msg "Starting avatar comparison stack..."
  (cd "$ROOT" && bash scripts/run_avatar_comparison.sh >>"$STACK_LOG_FILE" 2>&1) &
  local launcher_pid="$!"
  log_msg "Started launcher pid=$launcher_pid (log tail: $STACK_LOG_FILE)"

  local waited=0
  while [ "$waited" -lt "$STACK_READY_TIMEOUT_SECONDS" ]; do
    if check_processes_and_ports; then
      log_msg "Avatar comparison stack is ready."
      return 0
    fi
    sleep 1
    waited=$((waited + 1))
  done

  log_msg "Stack did not become healthy in ${STACK_READY_TIMEOUT_SECONDS}s. Restarting..."
  bash "$ROOT/scripts/stop_avatar_comparison.sh" >/dev/null 2>&1 || true
  return 1
}

stop_stack() {
  bash "$ROOT/scripts/stop_avatar_comparison.sh" >/dev/null 2>&1 || true
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [ "$SHUTDOWN_STACK_ON_EXIT" = "1" ]; then
    log_msg "Shutting down warm watcher and comparison stack."
    stop_stack
  else
    log_msg "Stopping warm watcher only (comparison stack left running)."
  fi
  rm -f "$WATCHER_PID_FILE"
  exit "$status"
}

if [ "${1:-}" = "--stop" ]; then
  if [ -f "$WATCHER_PID_FILE" ] && kill -0 "$(cat "$WATCHER_PID_FILE")" 2>/dev/null; then
    kill "$(cat "$WATCHER_PID_FILE")"
    log_msg "Stopped keep-warm watcher $(cat "$WATCHER_PID_FILE")"
  else
    log_msg "No keep-warm watcher running."
  fi
  exit 0
fi

if [ -f "$WATCHER_PID_FILE" ] && kill -0 "$(cat "$WATCHER_PID_FILE")" 2>/dev/null; then
  log_msg "Keep-warm watcher already running with pid $(cat "$WATCHER_PID_FILE")."
  exit 1
fi
printf "%s\n" "$$" >"$WATCHER_PID_FILE"
trap cleanup EXIT INT TERM

log_msg "Keep-warm watcher started (pid=$$). Press Ctrl+C to stop."

if ! check_processes_and_ports; then
  start_stack || true
fi

while true; do
  if ! check_processes_and_ports; then
    log_msg "Detected missing process/port; restarting comparison stack."
    sleep "$RESTART_COOLDOWN_SECONDS"
    start_stack || true
  fi
  sleep "$CHECK_INTERVAL_SECONDS"
done
