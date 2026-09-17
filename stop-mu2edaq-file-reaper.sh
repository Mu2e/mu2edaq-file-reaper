#!/usr/bin/env bash
#
# stop-mu2edaq-file-reaper.sh - standardized Mu2e control-room stop script.
# Launched as `crs-app stop file-reaper`. SIGTERM (the daemon finishes the file
# it is working on, at most CRS_STOP_TIMEOUT seconds) then SIGKILL.
#
# Stops every copy running out of this directory, not just the one in the pid
# file, so a hand-started copy cannot survive a "stop" and hold the port.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/file-reaper-proc.sh
source "$SCRIPT_DIR/lib/file-reaper-proc.sh"

PID_FILE="${1:-$SCRIPT_DIR/file-reaper.pid}"
TIMEOUT="${CRS_STOP_TIMEOUT:-30}"

RUNNING=()
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ fr_pid_from_file "$PID_FILE"; fr_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -eq 0 ]]; then
  if [[ -f "$PID_FILE" ]]; then
    echo "File Reaper not running (stale pid file); cleaning up"
    rm -f "$PID_FILE"
  else
    echo "File Reaper not running (no pid file: $PID_FILE)"
  fi
  exit 0
fi

echo "Stopping File Reaper (pid ${RUNNING[*]})..."
fr_kill_pids "$TIMEOUT" "${RUNNING[@]}"
rm -f "$PID_FILE"
echo "File Reaper stopped"
