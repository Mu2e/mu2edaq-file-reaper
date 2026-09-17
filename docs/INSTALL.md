# Installing, building and running mu2edaq-file-reaper

This document covers a fresh install on each supported platform, the optional
extras, the CMake targets, control-room integration, man pages, upgrading
and removal. The application is pure Python; "building" means creating the
virtual environment and installing the package into it in editable mode.

Requirements common to every platform:

| Component | Version | Notes |
|---|---|---|
| Python | 3.9 or newer | 3.9 is the production interpreter (AlmaLinux 9) |
| Flask | 3.0+ | web UI and API |
| PyYAML | 6.0+ | configuration |
| SQLAlchemy | 2.0+ | history, tokens, exclusions, area state |
| pytest | 7.0+ | development only |

Optional, each enabling one feature and nothing else:

| Package | Enables | Install |
|---|---|---|
| `pyzmq` | `daq_messages` channel (ZMQ PUSH to the dashboard) | `pip install pyzmq` or `./bootstrap.sh --extras` |
| `zstandard` | `compression.algorithm: zstd` | `pip install zstandard` or `./bootstrap.sh --extras` |
| `psycopg[binary]` | PostgreSQL backend | `pip install 'psycopg[binary]'` |
| `mu2edaq-discovery` | advertising on the DAQ network, `mu2edaq-reaper instances` | sibling checkout, installed by `bootstrap.sh` |
| `mu2edaq-notify` | `mu2e_notify` channel (phone notification system) | sibling checkout, installed by `bootstrap.sh` |

Every optional import is lazy: a missing package disables its channel or
algorithm and the daemon reports why on `/notifications` or `/config`.

## Linux (AlmaLinux 9, system Python 3.9)

```bash
sudo dnf install -y python3 python3-pip lsof git      # python3 is 3.9.21 on AL9
cd ~/mu2edaq-main                                     # or wherever the workspace lives
git clone https://github.com/Mu2e/mu2edaq-file-reaper.git   # skip when it is a submodule already
cd mu2edaq-file-reaper
MU2EDAQ_PYTHON=python3.9 ./bootstrap.sh               # venv/, deps, pip install -e ., sibling libs
./bootstrap.sh --extras                               # optional: pyzmq + zstandard
```

`bootstrap.sh` is idempotent. It creates `venv/` with `$MU2EDAQ_PYTHON`
(default `python3`), installs `requirements.txt` and `requirements-dev.txt`,
installs the package in editable mode (console scripts `mu2edaq-file-reaper`
and `mu2edaq-reaper`), then best-effort installs `mu2edaq-discovery` and
`mu2edaq-notify` from `../mu2edaq-discovery` and
`../mu2edaq-phone-notification-system` (falling back to
`../mu2edaq-install-discovery.sh` or GitHub), and creates `data/`, `logs/`,
`config/`. It exits non-zero if the core dependencies or the package fail to
install; the sibling libraries are optional and only produce a note.

`lsof` is used by the start and stop scripts to read another process's
working directory (so two checkouts on one host never stop each other); on a
host without it, process-table candidates are skipped and only the pid file
is consulted.

Configure and run:

```bash
cp config/.env.example config/.env && chmod 600 config/.env
$EDITOR config/.env                                   # MU2EDAQ_FILE_REAPER_ADMIN_TOKEN=...
cp config/mu2e-file-reaper-dl-01.yaml config/mu2e-file-reaper-$(hostname -s).yaml
$EDITOR config/mu2e-file-reaper-$(hostname -s).yaml   # areas, thresholds, notification hosts
source venv/bin/activate
mu2edaq-file-reaper -c config/mu2e-file-reaper-$(hostname -s).yaml --check-config
./start-mu2edaq-file-reaper.sh config/mu2e-file-reaper-$(hostname -s).yaml
tail -f logs/*.log
```

Host configs ship with `reaper.dry_run: true`. Watch `/queues` and
`/history` for at least one scan interval on real data, then set `dry_run:
false` and restart with the same start command (it replaces the running
copy).

The daemon needs read access to every area and the FTS database, and write
access to the areas it manages, to `data/` (its SQLite database) and to
`logs/`. Run it as the account that owns the data (`mu2edaq`); do not run it
as root.

## macOS (development)

```bash
brew install python@3.12 lsof                         # any 3.9+; python3.9 via pyenv to mirror AL9
cd mu2edaq-main/mu2edaq-file-reaper
./bootstrap.sh
source venv/bin/activate
pytest
python tools/make_demo_tree.py ./data/demo --files 300 --days 30 --used-pct 82
./start-mu2edaq-file-reaper.sh -c config/mu2edaq-file-reaper-test.yaml
open http://localhost:5004/
./stop-mu2edaq-file-reaper.sh
```

Notes: daemon mode works (fork is available). `/proc` is absent, so
`check_open_files` is skipped and `atime_mode` reports `unknown` unless
`statvfs` exposes the flag. The scripts are kept bash 3.2 clean because that
is what `/bin/bash` is on macOS. The demo config sets
`reaper.allow_shallow_root: true` because `./data/demo` under a home
directory is fine but a checkout directly under `/` would otherwise be
refused.

## Windows 11 (foreground only)

Install Python 3.9+ from python.org with "Add to PATH", then in PowerShell:

```powershell
cd mu2edaq-main\mu2edaq-file-reaper
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1            # add -Extras for pyzmq + zstandard
powershell -ExecutionPolicy Bypass -File .\start-mu2edaq-file-reaper.ps1 -Config config\mu2edaq-file-reaper.yaml -Port 5004
# ... Ctrl+C, or from another window:
powershell -ExecutionPolicy Bypass -File .\stop-mu2edaq-file-reaper.ps1
```

`start-mu2edaq-file-reaper.ps1` honours `CRS_PORT_HTTP` when `-Port` is not
given and forwards any extra arguments to `file_reaper.py`. There is no
`fork(2)`, so `--daemon` exits with an error; for background operation wrap
the start script in a service manager such as NSSM. `stop-*.ps1` finds the
process by command line and stops it with `Stop-Process -Force`. Hard-link
detection, `O_NOFOLLOW` and `dir_fd` semantics depend on the platform;
Windows is a development and UI-testing platform, not a deployment target.

## PostgreSQL backend

SQLite in `data/file-reaper.db` (WAL mode) is the default and is sufficient
for one node. To share a history database or to use an existing server:

```bash
source venv/bin/activate
pip install 'psycopg[binary]>=3.1'          # or: pip install -e '.[postgres]'
createdb reaper && psql -c "create user reaper password 'secret'" && psql -c "grant all on database reaper to reaper"
```

```yaml
database:
  url: "postgresql+psycopg://reaper:secret@dbhost.fnal.gov:5432/reaper"
  history_retention_days: 730
```

Prefer keeping the password out of the YAML:
`MU2EDAQ_FILE_REAPER_DATABASE_URL=postgresql+psycopg://reaper:secret@dbhost/reaper`
in `config/.env` (mode 0600). Tables are created with `create_all` on first
start; `meta.schema_version` records the schema (1). The URL is shown with
the password redacted on `/about`, `/config` and in `/api/v1/config`.

## CMake

`CMakeLists.txt` exists so the `mu2edaq-main` harness can treat this package
like the compiled ones.

```bash
cmake -S . -B build                    # finds Python3 >= 3.9
cmake --build build                    # target "venv" (ALL): runs bootstrap.sh / bootstrap.ps1
cmake --build build --target pytest    # runs the test suite in the venv
ctest --test-dir build                 # same suite through CTest
cmake --install build --prefix /usr/local
```

| Target | Effect |
|---|---|
| `venv` (default) | bootstraps `venv/` with the interpreter CMake found (`MU2EDAQ_PYTHON` is set for the script) |
| `pytest` | `venv/bin/python -m pytest -q` in the source directory |
| `test` / `ctest` | the same, registered with `add_test` |
| `install` | man pages to `share/man/man{1,3,5,7}`, the three shell scripts to `share/mu2edaq-file-reaper/`, YAML configs to `share/mu2edaq-file-reaper/config/` |

The application itself is not copied by `install`: it runs from its checkout
(the control-room convention), with the venv next to it.

## Control-room integration

The control room starts applications through `crs-app`, driven by
`mu2edaq-controlroom-setup/config/apps.yaml`. Add an entry alongside
`diskwatcher` (5002) and `fts` (5003):

```yaml
  - id: file-reaper
    title: "File Reaper"
    repo: mu2edaq-file-reaper
    start: start-mu2edaq-file-reaper.sh
    stop: stop-mu2edaq-file-reaper.sh
    install_path: ~/mu2edaq-main/mu2edaq-file-reaper
    ports: {http: 5004}
    sessions: [daq-main, daq-dl2]
    desktop: {icon: user-trash, terminal: false}
```

`crs-app start file-reaper` exports `CRS_PORT_HTTP=5004` and runs the start
script, which forwards it as `--port` and passes `--daemon --pid-file
./file-reaper.pid`; the first positional argument selects the config file.
`crs-app stop file-reaper` runs the stop script (`SIGTERM`, then `SIGKILL`
after `CRS_STOP_TIMEOUT` seconds, default 30). Host configs are kept in
`mu2edaq-config/config-main/mu2edaq-file-reaper/`; use
`scripts/mu2edaq-config-add.sh -p mu2edaq-file-reaper` there to register the
package and copy the YAML.

The daemon advertises itself through `mu2edaq-discovery` as
`app=file-reaper`, so it appears in `mu2edaq-discover` scans and in the
control-room browser without further registration.

## Man pages

The pages live under `man/` in section directories and render in place:

```bash
man -l man/man1/mu2edaq-file-reaper.1
man -l man/man5/mu2edaq-file-reaper.conf.5
man -l man/man7/mu2edaq-file-reaper-api.7
man -l man/man3/mu2edaq_file_reaper.3
```

To install them system-wide (or per user):

```bash
cmake --install build --prefix /usr/local             # -> /usr/local/share/man
# or without CMake:
sudo cp man/man1/*.1 /usr/local/share/man/man1/ && sudo cp man/man3/*.3 /usr/local/share/man/man3/
sudo cp man/man5/*.5 /usr/local/share/man/man5/ && sudo cp man/man7/*.7 /usr/local/share/man/man7/
sudo mandb 2>/dev/null || true
# per user:
mkdir -p ~/.local/share/man && cp -r man/* ~/.local/share/man/ && export MANPATH="$HOME/.local/share/man:$MANPATH"
```

Pages: `mu2edaq-file-reaper(1)`, `mu2edaq-reaper(1)`,
`mu2edaq-file-reaper-bootstrap(1)`, `mu2edaq-file-reaper-start(1)`,
`mu2edaq-file-reaper-stop(1)`, `make_demo_tree(1)`, `mu2edaq_file_reaper(3)`,
`mu2edaq-file-reaper.conf(5)`, `mu2edaq-file-reaper-api(7)`.

## Upgrading

```bash
cd ~/mu2edaq-main/mu2edaq-file-reaper
git pull                                   # or: cd .. && ./mu2edaq-update-submodules.sh
./bootstrap.sh                             # re-installs deps and the editable package
source venv/bin/activate
mu2edaq-file-reaper -c config/<host>.yaml --check-config
./start-mu2edaq-file-reaper.sh config/<host>.yaml     # replaces the running copy
```

Read `CHANGELOG.md` before restarting; anything under **Compatibility**
changes how an existing configuration or an API consumer behaves. Schema
changes are applied with `create_all`, which adds missing tables but never
alters existing ones; a release that changes a column will say so in the
changelog and ship a migration note. Back up `data/*.db` before upgrading a
production node:

```bash
sqlite3 data/file-reaper.db ".backup data/file-reaper-$(date +%F).db"
```

API tokens, exclusions, pause/disable state and the active-tier set live in
the database and survive an upgrade and a restart.

## Uninstalling

```bash
./stop-mu2edaq-file-reaper.sh
rm -rf venv build                          # environment and CMake output
rm -f file-reaper.pid
# keep or remove runtime state:
rm -rf data/*.db data/*.db-wal data/*.db-shm logs/*.log
rm -rf ~/.config/mu2edaq/file-reaper       # CLI token cache (per user, on the client host)
# man pages, if installed system-wide:
sudo rm -f /usr/local/share/man/man1/mu2edaq-file-reaper*.1 /usr/local/share/man/man1/mu2edaq-reaper.1 \
           /usr/local/share/man/man1/make_demo_tree.1 /usr/local/share/man/man3/mu2edaq_file_reaper.3 \
           /usr/local/share/man/man5/mu2edaq-file-reaper.conf.5 /usr/local/share/man/man7/mu2edaq-file-reaper-api.7
```

Then remove the `file-reaper` entry from `apps.yaml` and the checkout itself.
The reaper never writes outside its checkout, its configured areas, the
database URL and the log file, so nothing else needs cleaning.
