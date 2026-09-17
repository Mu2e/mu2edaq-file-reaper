#!/usr/bin/env bash
#
# file-reaper-proc.sh - process discovery shared by the start and stop scripts.
#
# Sourced, never executed. Provides:
#
#   fr_pid_from_file FILE     echo the live pid recorded in FILE, if any
#   fr_running_pids DIR       echo every file_reaper.py pid belonging to DIR
#   fr_kill_pids TIMEOUT ...  SIGTERM, then SIGKILL after TIMEOUT seconds
#
# A pid is only believed once its command line confirms it is this application
# (pid numbers are recycled) AND, for process-table candidates, its working
# directory is DIR (two checkouts on one host never stop each other).
# Kept bash-3.2 clean for macOS.

fr_is_reaper() {
  local pid="$1" args
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  args="$(ps -o args= -p "$pid" 2>/dev/null)"
  case "$args" in
    *file_reaper.py*|*mu2edaq_file_reaper*|*mu2edaq-file-reaper*) ;;
    *) return 1 ;;
  esac
  case "$args" in *[Pp]ython*) return 0 ;; esac
  return 1
}

fr_pid_from_file() {
  local pid_file="$1" pid
  [[ -f "$pid_file" ]] || return 0
  pid="$(tr -dc '0-9' < "$pid_file")"
  fr_is_reaper "$pid" && echo "$pid"
  return 0
}

fr_running_pids() {
  local dir="$1" pid cwd
  dir="$(cd "$dir" && pwd -P)"
  while read -r pid _rest; do
    [[ -n "$pid" ]] || continue
    [[ "$pid" == "$$" || "$pid" == "$PPID" ]] && continue
    fr_is_reaper "$pid" || continue
    if command -v lsof >/dev/null 2>&1; then
      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)"
      [[ "$cwd" == "$dir" ]] || continue
    else
      continue
    fi
    echo "$pid"
  done < <(ps -u "$(id -u)" -o pid=,args= 2>/dev/null)
  return 0
}

fr_kill_pids() {
  local timeout="$1"; shift
  local pid i alive
  for pid in "$@"; do kill -TERM "$pid" 2>/dev/null || true; done
  for ((i = 0; i < timeout; i++)); do
    alive=0
    for pid in "$@"; do kill -0 "$pid" 2>/dev/null && alive=1; done
    [[ "$alive" == 0 ]] && return 0
    sleep 1
  done
  for pid in "$@"; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "  pid $pid did not exit within ${timeout}s; sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  sleep 1
  return 0
}
