# CLAUDE.md - mu2edaq-file-reaper

## Project Overview

Policy-driven disk cleanup daemon for the Mu2e DAQ online systems. One
instance per node measures the used fraction of configured disk areas (df
convention), alarms the DAQ when the `warning` / `critical` / `full` tier is
reached, builds an ordered compression or deletion queue from the files in
the area under the tier's policy (`LRU-Delete`, `Age-Delete`, `LRU-Compress`,
`Age-Compress`), acts on it until the tier's low-water mark is reached, and
records every decision in an audit history exposed by a Flask web UI, a
bearer-token REST API (`/api/v1`) and the `mu2edaq-reaper` CLI. It is the
actuating counterpart of `mu2edaq-diskwatcher` and shares its layout and
conventions. Production infrastructure: correctness and refusal-to-act on
doubt matter more than convenience.

## Setup and Running

```bash
./bootstrap.sh                      # venv + deps + editable install + sibling libs (discovery, notify)
./bootstrap.sh --extras             # also pyzmq and zstandard
./start-mu2edaq-file-reaper.sh      # daemon on 5004 (CRS_PORT_HTTP), pid file ./file-reaper.pid
./stop-mu2edaq-file-reaper.sh
source venv/bin/activate
python file_reaper.py -c config/mu2edaq-file-reaper.yaml            # foreground
mu2edaq-file-reaper -c config/mu2e-file-reaper-dl-01.yaml --check-config
mu2edaq-reaper --url http://localhost:5004 --admin-token "$TOK" token create ops --scopes read,operate,admin --save
```

Windows: `bootstrap.ps1`, `start-mu2edaq-file-reaper.ps1` (foreground only,
no `fork`). CMake: `cmake -S . -B build && cmake --build build` bootstraps the
venv, `--target pytest` runs tests, `cmake --install build` installs man pages,
scripts and configs.

## Architecture

Package `src/mu2edaq_file_reaper/`:

- `settings.py` Settings singleton (`get_settings()`), `_ENV_MAP` of
  `MU2EDAQ_FILE_REAPER_*`, `.env` loader; `apply()` skips `None` so layers
  stack. `config.py` YAML -> `AreaConfig` list plus `issues` (never raises,
  never `sys.exit`; drops areas on system roots or shallower than depth 2).
  `cli.py` `build_parser`, `load_settings` (defaults -> YAML -> .env -> env ->
  CLI), `main` (wires everything, bootstrap admin token, signal handling).
- Pure modules (no I/O, no clock, no globals; they carry the unit tests):
  `units.py` (size/percent/duration), `watermarks.py` (`resolve_marks`,
  `tier_is_active`, `evaluate_tiers`, `highest_active`, `should_continue`,
  `policy_insufficient`, `bytes_to_stop`, `simulate_after`),
  `eligibility.py` (`EligibilityContext`, `classify`, `partition`,
  `REJECT_REASONS`), `policies.py` (`POLICIES`, `ordering_key`,
  `build_queue`), `auth.py` (token generate/hash/verify, scopes, `TokenCache`),
  `notify/ratelimit.py`.
- I/O modules: `usage.py` (df-style usage, `fake_usage_file` hook,
  `atime_mode`), `scanner.py` (iterative `scandir`, no symlinks, same `st_dev`,
  records parent `(dev, ino)`; temp sweep; `/proc` open-file scan),
  `fts_gate.py` (read-only FTS snapshot, `FtsUnavailable` => `EMPTY_SNAPSHOT`,
  fail closed), `exclusions.py` (`ExclusionRegistry`, immutable rules),
  `compressors.py`, `actions.py` (`delete_file`, `compress_file`,
  `prune_empty_dirs`; every action re-verifies identity via
  `open_parent_verified` / `open_file_verified` and returns `ActionResult`
  `ok|dry_run|skipped|failed`).
- `reaper.py` `AreaScanner` / `scan_area`: the orchestrator. `Deps` holds
  every injectable side effect (store, history, area_state, exclusions,
  notifier, measure_usage, enumerate_files, load_fts, delete, compress, clock,
  shutdown event, fs_locks). `AreaRuntime` is the mutable per-area state
  (paused/disabled, `active_tiers`, `interrupt` event, incompressible memory).
- `scheduler.py` `Scheduler`: restores `area_state`, ticks every
  `scan_interval`, `request_scan`, `run_now` (synchronous, used by
  `POST /dry-run`), pause/resume/disable/enable, daily history prune, `stop()`
  with grace. `state.py` `StateStore` and `null_area_state` /
  `AREA_STATE_KEYS`. `db/` models, repos (`HistoryRepo`, `ExclusionRepo`,
  `TokenRepo`, `AreaStateRepo`, `NotificationLogRepo`), `Database`
  (`session_scope`, WAL pragmas). `notify/` `Notifier` thread + five
  channels. `web/` Flask factory, `auth.py` (bearer + admin session + CSRF +
  `X-Admin-Token`), `api_v1.py`, `nav.py`, views and templates.
- `src/mu2edaq_reaper_cli/` stdlib CLI: `client.py` (`ReaperClient`),
  `tokencache.py` (0600 YAML cache), `cli.py`, `commands/`.

Threading model: main thread (signals, waits on a stop event, watches the
server threads); `reaper-scheduler` daemon thread; `ThreadPoolExecutor`
(`reaper.workers`, default 4) running `scan_area`; `reaper-notifier` daemon
thread draining an event queue; Werkzeug `make_server(threaded=True)` in a
`reaper-web` thread (plus `reaper-api` when `api_port` is set). Locks:
`StateStore._lock` (dict swap only), `AreaRuntime.lock` and its `interrupt`
`Event` (set by pause/disable/shutdown, honoured at the next file or 1 MiB
chunk), `Deps.fs_locks[st_dev]` so areas on one filesystem never act
concurrently, `ExclusionRegistry._lock`. SQLite: WAL, `busy_timeout=5000`,
`check_same_thread=False`, `scoped_session`, one short transaction per repo
call, `Session.remove()` at scan end and Flask teardown.

Data flow per scan: measure usage -> `evaluate_tiers` (hysteresis, persisted
`active_tiers`) -> pick `highest_active` minus exhausted -> load FTS snapshot
-> `enumerate_files` -> `partition` (eligibility) -> `build_queue` -> publish
snapshot to `StateStore` and `queue_add` history -> `_act` on the queue
(re-check exclusions and interrupt per file, act, re-measure, `action_*`
history, stall check) -> `stop_reached` / `exhausted` (`policy_insufficient`)
/ interrupt -> next pass (max `max_passes_per_scan`) -> final snapshot,
`scan_end`.

StateStore contract: writers install new dicts (`replace_area(name, snap)`,
`update_area(name, **changes)` builds `{**old, **changes}`); readers get
copies. Nobody mutates a published dict. Every area snapshot has exactly the
keys of `null_area_state()`.

Coding rules:

- Python 3.9 typing: `Optional[X]`, `Dict[str, Any]`, `Tuple[...]`; never
  `X | None` or `list[str]` in annotations evaluated at runtime.
- Call `get_settings()` inside function bodies; never bind it or its fields
  at module scope.
- Never mutate a dict obtained from `StateStore`; build a new one.
- Anything that modifies the filesystem lives in `actions.py` and must
  re-verify identity (`dir_fd`, `(dev, ino)`, size, `mtime_ns`, `nlink`,
  settle) before acting; a mismatch is a skip, never an error.
- Keep `watermarks`, `eligibility`, `policies`, `units`, `auth`, `ratelimit`
  pure.
- Add API-visible area keys to `null_area_state()`; `AREA_STATE_KEYS` parity
  is tested.
- Shell scripts stay bash 3.2 compatible (macOS); no `mapfile`, no `${var,,}`,
  no associative arrays.
- No em dashes in documentation.
- Update `CHANGELOG.md` under `[Unreleased]` (Compatibility for behavioural
  changes) and `PROJECT-STATUS.md` with every change.

## Configuration Schema

Precedence: `command line > environment (MU2EDAQ_FILE_REAPER_*) > .env >
YAML > defaults`. Full reference: `man 5 mu2edaq-file-reaper.conf`.

```yaml
reaper:
  label: null                    # instance label; default short hostname
  web_host: "0.0.0.0"
  web_port: 5004                 # CRS_PORT_HTTP
  api_port: null                 # optional second listener, /api/* only
  scan_interval: 600             # seconds
  dry_run: false                 # plan and record, never modify
  daemon: false                  # start script passes --daemon
  pid_file: "./file-reaper.pid"
  log_file: "./logs/mu2edaq-file-reaper.log"
  log_max_bytes: 10485760        # rotating handler
  log_backup_count: 5
  admin_token: ""                # empty => bootstrap token logged at start; prefer env / .env
  workers: 4
  max_passes_per_scan: 3         # cascade full -> critical -> warning within one scan
  remeasure_every: 1             # re-read usage every N actions
  stall_window: 20               # actions between space-reclaimed checks
  shutdown_grace_s: 30
  queue_publish_limit: 1000
  history_log_queue_adds: new_only   # all | new_only | none
  compress_verify: crc           # crc | size | none
  compress_min_ratio: 0.95
  compress_assumed_ratio: 0.5    # dry-run estimate
  protected_paths: []
  allow_shallow_root: false
  # fake_usage_file: ./data/demo-usage.json   # TEST HOOK ONLY: {realpath: [total, used, free]}
database:
  url: "sqlite:///./data/file-reaper.db"     # or postgresql+psycopg://user:pw@host/db
  history_retention_days: 365                # 0 = keep forever
discovery: {enabled: true, app: "file-reaper", name: null}
api: {open_read: true}
defaults:                        # inherited by every area
  thresholds: {warning: 80, critical: 90, full: 95}    # percent USED (df convention)
  tiers:                         # tier: null disables it; "threshold" may also be set per tier
    warning:  {policy: LRU-Compress, min_age: "7d", low_water: 10, high_water: 0}
    critical: {policy: LRU-Delete,   min_age: "3d", low_water: 10, high_water: 0}
    full:     {policy: Age-Delete,   min_age: "1d", low_water: 10, high_water: 0}
  include: ["*"]
  exclude: ["*.tmp", "*.part", ".~*", "*.pid", "*.db", "*.db-wal", "*.db-shm", "*.db-journal"]
  settle_seconds: 300
  compression: {algorithm: gzip, level: 6, skip_suffixes: [".gz", ".bz2", ".xz", ".zst", ".zip", ".7z", ".lz4", ".tgz", ".tbz2", ".txz"]}
  prune_empty_dirs: false
  max_actions_per_scan: 0        # 0 = unlimited
  check_open_files: false        # Linux /proc scan
  allow_hardlinks: false
areas:
  - path: /data/raw              # name defaults to "data-raw"
    label: "Raw data"
    fts_db: /home/mu2edaq/mu2edaq-main/mu2edaq-fts/fts_status.db   # only COMPLETED/DELETED eligible
    thresholds: {warning: 75, critical: 85, full: 92}
    tiers: {warning: {policy: LRU-Compress, min_age: "14d"}}
  - path: /scratch/mu2e
    enabled: false
notifications:
  rate_limit_seconds: 600
  max_per_hour: 0
  mu2e_notify:  {enabled: true,  min_severity: warning, source: file-reaper, category: DAQ, url: null, token: null}
  daq_messages: {enabled: true,  min_severity: warning, host: localhost, port: 5555, subsystem: DAQ, event_category: software}
  bigredbox:    {enabled: false, min_severity: critical, port: 37020, system_id: null}
  email:        {enabled: false, min_severity: critical, method: smtp, smtp_host: smtp.fnal.gov, smtp_port: 25, starttls: false, from: mu2edaq@fnal.gov, to: []}
  slack:        {enabled: false, min_severity: warning, webhook_url: null}
```

Water marks: `trigger = min(T + high_water, 100)`, `stop = max(T - low_water,
0)`; active when `used >= trigger`, stays active until `used <= stop`.

## Testing

```bash
source venv/bin/activate
pytest                                   # whole suite
pytest tests/test_watermarks.py -q       # one module
pytest -k "eligibility or actions" -q
python3.9 -W error -c "import mu2edaq_file_reaper"   # AL9 typing check when 3.9 is available
for f in man/man*/*; do man -l "$f" >/dev/null; done  # man pages render
```

Fixtures live in `tests/conftest.py`: autouse `reset_settings()` and store
clear, `fake_clock`, `fake_usage` (a FakeDisk whose free space grows as bytes
are freed), `tree` builder on `tmp_path` with `os.utime`-set atime/mtime,
symlinks, hardlinks and temp names, `mem_db` (SQLite StaticPool), `fts_db`
built from the FTS DDL, and `deps` with a recording notifier and history.
Engine tests inject everything through `Deps`; never touch a real disk's
usage in tests.

Manual end-to-end without filling a disk: the `reaper.fake_usage_file` hook
maps an area realpath to `[total, used, free]` and is re-read on every
measurement, so a running demo can be steered by editing the file.

```bash
python tools/make_demo_tree.py ./data/demo --files 300 --days 30 --used-pct 82
./start-mu2edaq-file-reaper.sh -c config/mu2edaq-file-reaper-test.yaml
# edit data/demo-usage.json: used across 80 / 90 / 95 %, watch /queues, /history, /notifications
```

Never set `fake_usage_file` in a production config; the daemon logs a warning
whenever it is active.
