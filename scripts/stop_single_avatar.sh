#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/var/run/avatar-single"
LAUNCHER_PID_FILE="$RUN_DIR/launcher.pid"
PIDS_FILE="$RUN_DIR/pids"
STOPPED_PIDS=()
STOPPED_LAUNCHERS=()

kill_tree() {
  local pid="$1"
  local child
  while read -r child; do
    [ -n "${child:-}" ] || continue
    kill_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  if kill "$pid" 2>/dev/null; then
    STOPPED_PIDS+=("$pid")
  fi
}

pid_exited() {
  local pid="$1"
  local _stat_pid _stat_comm stat_state _rest
  if ! kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if [ -r "/proc/$pid/stat" ]; then
    read -r _stat_pid _stat_comm stat_state _rest <"/proc/$pid/stat" || true
    if [ "${stat_state:-}" = "Z" ]; then
      return 0
    fi
  fi
  return 1
}

wait_for_stopped_pids() {
  local pid attempt
  for pid in "${STOPPED_PIDS[@]}"; do
    for attempt in $(seq 1 40); do
      if pid_exited "$pid"; then
        break
      fi
      sleep 0.25
    done
    if ! pid_exited "$pid"; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
}

stop_launcher_from_child_parents() {
  local pid _name parent args
  [ -f "$PIDS_FILE" ] || return 0
  while read -r pid _name; do
    [ -n "${pid:-}" ] || continue
    parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    [ -n "${parent:-}" ] || continue
    [ "$parent" != "1" ] || continue
    [ "$parent" != "$$" ] || continue
    args="$(ps -o args= -p "$parent" 2>/dev/null || true)"
    case "$args" in
      *run_single_avatar.sh*)
        case " ${STOPPED_LAUNCHERS[*]} " in
          *" $parent "*) continue ;;
        esac
        if kill -0 "$parent" 2>/dev/null; then
          kill_tree "$parent"
          STOPPED_LAUNCHERS+=("$parent")
          echo "Stopped single avatar launcher $parent"
        fi
        ;;
    esac
  done <"$PIDS_FILE"
}

if [ -f "$LAUNCHER_PID_FILE" ]; then
  launcher_pid="$(cat "$LAUNCHER_PID_FILE")"
  if kill -0 "$launcher_pid" 2>/dev/null; then
    kill_tree "$launcher_pid"
    STOPPED_LAUNCHERS+=("$launcher_pid")
    echo "Stopped single avatar launcher $launcher_pid"
  fi
fi

stop_launcher_from_child_parents

if [ -f "$PIDS_FILE" ]; then
  while read -r pid _; do
    [ -n "${pid:-}" ] || continue
    kill_tree "$pid"
  done <"$PIDS_FILE"
  echo "Stopped single avatar stack processes from $PIDS_FILE"
fi

wait_for_stopped_pids

if [ -f "$PIDS_FILE" ]; then
  rm -f "$PIDS_FILE"
fi

if [ -f "$LAUNCHER_PID_FILE" ]; then
  rm -f "$LAUNCHER_PID_FILE"
fi

echo "Done."
