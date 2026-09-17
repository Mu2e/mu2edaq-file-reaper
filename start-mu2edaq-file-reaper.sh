#!/usr/bin/env bash
#
# start-mu2edaq-file-reaper.sh - standardized Mu2e control-room start script.
#
# Launched by the control room as `crs-app start file-reaper`, which exports
# CRS_PORT_HTTP from apps.yaml. Forwards it as --port and runs the daemon with
# a pid file. Any copy already running out of this directory is stopped first
# so a restart never leaves two daemons competing for the port; --no-replace
# refuses to start instead.
#
# usage: start-mu2edaq-file-reaper.sh [CONFIG | -c CONFIG] [-p PORT] [--no-replace]
#                                     [--pid-file FILE] [--dry-run] [reaper options...]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck source=lib/file-reaper-proc.sh
source "$SCRIPT_DIR/lib/file-reaper-proc.sh"

CRS_PORT_HTTP="${CRS_PORT_HTTP:-5004}"
PID_FILE="$SCRIPT_DIR/file-reaper.pid"
STOP_TIMEOUT="${CRS_STOP_TIMEOUT:-30}"
REPLACE=1
CONFIG_FILE=""
EXTRA=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -c|--config)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a file argument" >&2; exit 2; }
      CONFIG_FILE="$2"; shift 2 ;;
    --config=*) CONFIG_FILE="${1#*=}"; shift ;;
    -p|--port)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a port argument" >&2; exit 2; }
      CRS_PORT_HTTP="$2"; shift 2 ;;
    --port=*) CRS_PORT_HTTP="${1#*=}"; shift ;;
    --no-replace) REPLACE=0; shift ;;
    --pid-file)
      [[ $# -ge 2 ]] || { echo "error: $1 requires a file argument" >&2; exit 2; }
      PID_FILE="$2"; shift 2 ;;
    --pid-file=*) PID_FILE="${1#*=}"; shift ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*) EXTRA+=("$1"); shift ;;
    *)
      if [[ -z "$CONFIG_FILE" ]]; then CONFIG_FILE="$1"; else EXTRA+=("$1"); fi
      shift ;;
  esac
done
CONFIG_FILE="${CONFIG_FILE:-./config/mu2edaq-file-reaper.yaml}"

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "error: config file not found: $CONFIG_FILE" >&2
  exit 1
fi

RUNNING=()
while read -r _pid; do
  [[ -n "$_pid" ]] && RUNNING+=("$_pid")
done < <({ fr_pid_from_file "$PID_FILE"; fr_running_pids "$SCRIPT_DIR"; } | sort -un)

if [[ ${#RUNNING[@]} -gt 0 ]]; then
  if [[ "$REPLACE" == 0 ]]; then
    echo "error: File Reaper already running (pid ${RUNNING[*]}); not starting (--no-replace)" >&2
    exit 1
  fi
  echo "Found running File Reaper (pid ${RUNNING[*]}); stopping it first"
  if [[ -z "${FR_DRY_RUN:-}" ]]; then
    fr_kill_pids "$STOP_TIMEOUT" "${RUNNING[@]}"
    rm -f "$PID_FILE"
    echo "Previous instance stopped"
  fi
fi

# FR_DRY_RUN is a test hook: report what would start, without a venv or a daemon.
if [[ -n "${FR_DRY_RUN:-}" ]]; then
  echo "Starting File Reaper (http=$CRS_PORT_HTTP, config: $CONFIG_FILE)"
  exit 0
fi

if [[ ! -x ./venv/bin/python ]]; then
  echo "error: virtual environment not found; run ./bootstrap.sh first" >&2
  exit 1
fi
# shellcheck disable=SC1091
source ./venv/bin/activate
export PYTHONPATH="./src:${PYTHONPATH:-}"
mkdir -p data logs

echo "Starting File Reaper (http=$CRS_PORT_HTTP, config: $CONFIG_FILE)"
exec python file_reaper.py --config "$CONFIG_FILE" --port "$CRS_PORT_HTTP" \
  --daemon --pid-file "$PID_FILE" ${EXTRA[@]+"${EXTRA[@]}"}
