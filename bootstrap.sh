#!/usr/bin/env bash
#
# bootstrap.sh - set up (or refresh) the mu2edaq-file-reaper install directory.
#
# Creates ./venv, installs the runtime and development dependencies, installs
# the package in editable mode (console scripts mu2edaq-file-reaper and
# mu2edaq-reaper), and best-effort installs the two sibling Mu2e libraries that
# are not on PyPI: mu2edaq-discovery and mu2edaq-notify.  Re-running is safe.
#
#   ./bootstrap.sh              # runtime + dev deps
#   ./bootstrap.sh --extras     # also pyzmq (DAQ messages) and zstandard (zstd)
#   MU2EDAQ_PYTHON=python3.9 ./bootstrap.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
PY="${MU2EDAQ_PYTHON:-python3}"
EXTRAS=0
for arg in "$@"; do
  case "$arg" in
    --extras) EXTRAS=1 ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -x venv/bin/python ]]; then
  echo "Creating virtual environment with $PY"
  "$PY" -m venv venv
fi
# shellcheck disable=SC1091
source venv/bin/activate
python -m pip install --upgrade pip >/dev/null 2>&1 || true

if ! pip install -r requirements.txt -r requirements-dev.txt; then
  echo "ERROR: dependency installation failed; environment is NOT usable." >&2
  exit 1
fi
if ! pip install -e .; then
  echo "ERROR: could not install mu2edaq-file-reaper; environment is NOT usable." >&2
  exit 1
fi
if [[ "$EXTRAS" == 1 ]]; then
  pip install pyzmq zstandard || echo "note: optional extras failed to install"
fi

# Sibling Mu2e libraries (not on PyPI): prefer the checkout next door, fall back to GitHub.
install_sibling() {
  local module="$1" dir="$2" repo="$3"
  if python -c "import $module" 2>/dev/null; then return 0; fi
  if [[ -d "$HERE/../$dir" ]]; then
    pip install -e "$HERE/../$dir" && echo "Installed $dir from sibling checkout" && return 0
  fi
  if [[ -x "$HERE/../mu2edaq-install-discovery.sh" && "$module" == "mu2edaq_discovery" ]]; then
    "$HERE/../mu2edaq-install-discovery.sh" && return 0
  fi
  pip install "git+https://github.com/Mu2e/$repo" 2>/dev/null \
    && echo "Installed $dir from GitHub" \
    || echo "note: $dir not installed; the reaper runs without it"
}
install_sibling mu2edaq_discovery mu2edaq-discovery mu2edaq-discovery
install_sibling mu2edaq_notify mu2edaq-phone-notification-system mu2edaq-phone-notification-system

mkdir -p data logs config
echo "mu2edaq-file-reaper environment ready.  Next: ./start-mu2edaq-file-reaper.sh"
