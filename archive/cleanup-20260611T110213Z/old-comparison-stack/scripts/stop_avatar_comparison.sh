#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/var/run/avatar-comparison"
LAUNCHER_PID_FILE="$RUN_DIR/launcher.pid"
PIDS_FILE="$RUN_DIR/pids"

if [ -f "$LAUNCHER_PID_FILE" ]; then
  launcher_pid="$(cat "$LAUNCHER_PID_FILE")"
  if kill -0 "$launcher_pid" 2>/dev/null; then
    kill "$launcher_pid"
    echo "Stopped avatar comparison launcher $launcher_pid"
    exit 0
  fi
fi

if [ -f "$PIDS_FILE" ]; then
  while read -r pid _; do
    [ -n "${pid:-}" ] || continue
    kill "$pid" 2>/dev/null || true
  done <"$PIDS_FILE"
  echo "Stopped remaining avatar comparison processes from $PIDS_FILE"
  exit 0
fi

echo "No avatar comparison processes found."
