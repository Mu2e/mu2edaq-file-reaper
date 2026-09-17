# mu2edaq-file-reaper

Policy-driven disk cleanup daemon for the Mu2e DAQ online systems. It watches
the used fraction of configured disk areas, alarms the DAQ when a `warning`,
`critical` or `full` tier is reached, builds ordered compression and deletion
queues from the files in each area, acts on them until the tier's low-water
mark is reached, and records every decision in a queryable audit history. One
reaper runs per node; each carries a label and advertises itself through
`mu2edaq-discovery`.

It is the actuating counterpart of `mu2edaq-diskwatcher`, which only measures.
Same web layout, same configuration conventions, same control-room scripts;
the difference is that this application is allowed to modify the filesystem.

| Monitor | Alarm | Act |
|---|---|---|
| Used percent of every configured area, measured the way `df` does, every `scan_interval` (default 10 min) | Tier transitions and failures go to the Mu2e phone notification system, the DAQ message dashboard (ZMQ), the Big Red Box, email and Slack, each channel independently configured | The most severe active tier's policy (`LRU-Delete`, `Age-Delete`, `LRU-Compress`, `Age-Compress`) runs on an ordered queue until used space is back at the tier's stop point |

## Quick start

```bash
./bootstrap.sh                          # venv, deps, editable install, sibling libs
./start-mu2edaq-file-reaper.sh          # daemon on port 5004 with a pid file
./stop-mu2edaq-file-reaper.sh
```

Then open <http://localhost:5004/>.

Starting is idempotent: a copy already running out of this directory is
stopped before the new one launches. See [Start and stop](#start-and-stop).

To run in the foreground against a specific config, or to check a config
without starting anything:

```bash
source venv/bin/activate
python file_reaper.py --config config/mu2edaq-file-reaper.yaml --port 5004
mu2edaq-file-reaper --config config/mu2e-file-reaper-dl-01.yaml --check-config
```

Three equivalent entry points exist: `python file_reaper.py`,
`python -m mu2edaq_file_reaper`, and the `mu2edaq-file-reaper` console script
installed by `pip install -e .`.

### First run: the admin token and the first API token

Read-only pages and `GET` API calls are open on the control-room network.
Anything that changes state (pause, disable, exclude, token management) needs
either the **admin token** or a scoped **bearer token**.

If `reaper.admin_token` is empty and `MU2EDAQ_FILE_REAPER_ADMIN_TOKEN` is not
set, the daemon generates a bootstrap admin token at every start and logs it
once as a warning:

```
WARNING reaper: no reaper.admin_token configured; one-time bootstrap admin token: k3...
```

Use it to mint the first real API token, then put a permanent admin secret in
`config/.env` (copy `config/.env.example`) so the bootstrap token stops
changing on every restart:

```bash
mu2edaq-reaper --url http://localhost:5004 --admin-token 'k3...' \
    token create shifter --scopes read,operate --save
# token is printed once and cached in ~/.config/mu2edaq/file-reaper/tokens.yaml (chmod 600)
mu2edaq-reaper --url http://localhost:5004 status
```

## Configuration

Precedence, highest first:

```
command line  >  environment (MU2EDAQ_FILE_REAPER_*)  >  .env  >  config file  >  built-in defaults
```

The `.env` file is looked for next to the config file, then in the current
directory, or named with `--env-file` / `MU2EDAQ_FILE_REAPER_ENV_FILE`. Values
already present in the real environment win over the file.

The YAML has seven top-level keys. Full reference in
`man 5 mu2edaq-file-reaper.conf`; `config/mu2edaq-file-reaper.yaml` is
heavily commented and doubles as a worked example.

```yaml
reaper:
  label: "dl-01"                 # instance label; default = short hostname
  web_host: "0.0.0.0"
  web_port: 5004                 # CRS_PORT_HTTP
  api_port: null                 # optional second listener serving only /api/v1
  scan_interval: 600             # seconds
  dry_run: false                 # plan, publish queues, write history; never modify files
  admin_token: ""                # prefer MU2EDAQ_FILE_REAPER_ADMIN_TOKEN or config/.env
  workers: 4                     # areas scanned concurrently

database:
  url: "sqlite:///./data/file-reaper.db"   # or postgresql+psycopg://user:pw@host/db
  history_retention_days: 365

discovery:
  enabled: true
  app: "file-reaper"

api:
  open_read: true                # GET needs no token; every write always does

defaults:                        # inherited by every area
  thresholds: {warning: 80, critical: 90, full: 95}      # percent USED
  tiers:
    warning:  {policy: LRU-Compress, min_age: "7d", low_water: 10, high_water: 0}
    critical: {policy: LRU-Delete,   min_age: "3d", low_water: 10, high_water: 0}
    full:     {policy: Age-Delete,   min_age: "1d", low_water: 10, high_water: 0}
  include: ["*"]
  exclude: ["*.tmp", "*.part", ".~*", "*.pid", "*.db", "*.db-wal", "*.db-shm", "*.db-journal"]
  settle_seconds: 300
  compression: {algorithm: gzip, level: 6}

areas:
  - path: /data/raw
    label: "Raw data"
    fts_db: /home/mu2edaq/mu2edaq-main/mu2edaq-fts/fts_status.db   # FTS gate
    thresholds: {warning: 75, critical: 85, full: 92}
  - path: /daqlogs
    include: ["*.log", "*.log.*"]
    tiers:
      warning: {policy: Age-Compress, min_age: "2d"}
  - path: /scratch/mu2e
    enabled: false               # monitor-only until enabled from the UI

notifications:
  rate_limit_seconds: 600
  mu2e_notify:  {enabled: true,  min_severity: warning}
  daq_messages: {enabled: true,  min_severity: warning, host: localhost, port: 5555}
  bigredbox:    {enabled: false, min_severity: critical}
  email:        {enabled: false, min_severity: critical, to: []}
  slack:        {enabled: false, min_severity: warning, webhook_url: null}
```

Durations accept `"90s"`, `"10m"`, `"6h"`, `"7d"`, `"2w"` or bare seconds.
Percentages accept `80` or `"80%"`. Configuration problems never stop the
daemon: they accumulate as issues shown on `/config` and `/api/v1/config`, and
an area with a fatal problem is dropped and listed rather than silently
accepted. `--check-config` prints the parsed result and exits 1 if any area
was dropped.

### Thresholds are percent USED

Every threshold is **percent of capacity used**, computed the way `df` does
(`100 * used / (used + free)`), so the numbers compare directly with `df -h`
on the node. This is the opposite sense from diskwatcher's `space:` blocks,
which state how much must remain free.

For a tier with threshold `T`, `high_water h` (default 0) and `low_water l`
(default 10):

```
trigger = min(T + h, 100)        tier becomes ACTIVE when used >= trigger
stop    = max(T - l, 0)          tier stays active until used <= stop
```

The tier keeps acting on every scan while it is active (hysteresis), and the
active set survives a restart (it is persisted in the database). If a tier's
queue empties while used space is still above `T`, the reaper records
`policy_insufficient`, raises an alarm, and falls through to the next lower
active tier.

Worked example, 10 TB volume (used + free), defaults for `warning`:

| Step | Used | Result |
|---|---|---|
| Scan 1 | 79.5 % | below trigger 80 %: GOOD, nothing queued |
| Scan 2 | 81.0 % | at or above 80 %: `warning` activates, `LRU-Compress` runs; to reach stop 70 % it must free `8.1 TB - 7.0 TB = 1.1 TB` |
| Scan 2, after acting | 72.0 % | eligible files exhausted above the stop point but below `T`: tier stays active, no alarm |
| Scan 3 | 71.5 % | still above stop 70 %: tier still active (hysteresis), queue re-built and acted on |
| Scan 4 | 69.8 % | at or below 70 %: `warning` deactivates, `info` notification "cleared" |

With `critical: {threshold: 90, high_water: 2, low_water: 5}` the trigger
would be 92 % and the stop 85 %.

### Tier policies

Each tier runs exactly one policy. `min_age` applies to the policy's own
ordering key.

| Policy | Ordering key (oldest first) | Action | Default for |
|---|---|---|---|
| `LRU-Delete` | `max(atime, mtime)` | unlink | `critical` |
| `Age-Delete` | `mtime` | unlink | `full` |
| `LRU-Compress` | `max(atime, mtime)` | compress in place (`X` -> `X.gz`) | `warning` |
| `Age-Compress` | `mtime` | compress in place | - |

Only the **most severe active tier** (`full > critical > warning`) acts on a
pass. After the pass the reaper re-measures usage and re-enumerates the area
and, if a tier is still active and not exhausted, runs another pass, up to
`reaper.max_passes_per_scan` (default 3). Queues are therefore never stale,
and "compress first, then delete" falls out naturally when both tiers are
active. Compressed output keeps the original's mode, owner, atime and mtime,
so it stays eligible for a later delete tier. Compressors: `gzip` (default),
`bz2`, `xz` from the standard library; `zstd` with the optional `zstandard`
package. On a `noatime` mount LRU degrades to mtime and the area shows a
warning.

### Eligibility

A file enters a queue only if none of these rejects it (the counts per reason
appear on the area page and in the API as `reject_counts`):

| Reason | Meaning |
|---|---|
| `outside_area` | realpath is not under the area root |
| `temp_name` | `*.tmp`, `*.part`, `*.partial`, `.*.swp`, `*~`, `.reaper-tmp-*`, `.~*` |
| `excluded_glob` / `not_included` | area `exclude:` / `include:` globs, matched on the relative path or the basename |
| `protected` | the FTS database and its `-wal/-shm/-journal` files, the reaper's own SQLite database, `reaper.protected_paths` |
| `manual_exclusion` | a rule added on `/exclusions` or via the API (exact path, glob, or directory prefix; optional expiry) |
| `hardlink` | `nlink > 1` unless `allow_hardlinks: true` (deleting one link frees nothing) |
| `already_compressed` / `compressed_copy_exists` | compress policies only: name ends in a `skip_suffixes` entry, or `X.gz` already sits next to `X` |
| `settling` | modified within `settle_seconds` |
| `too_young` | ordering key newer than the tier's `min_age` |
| `fts_not_complete` | see the FTS gate below |
| `open_file` | held open by a process (`check_open_files: true`, Linux `/proc` scan) |
| `incompressible` | a previous attempt did not shrink the file below `compress_min_ratio`; remembered per (path, size, mtime) |

Symlinks are never followed or listed; scanning never crosses a filesystem
boundary.

### FTS gate

An area with `fts_db:` pointing at a `mu2edaq-fts` SQLite database becomes
eligible-only-if-transferred: a file is a candidate only when its path (or its
path minus a compression suffix) appears in the FTS `files` table with status
`COMPLETED` or `DELETED`. The database is opened read-only. If it cannot be
read, **nothing** in the area is eligible, an `error` notification is sent and
the area shows a warning: the gate fails closed.

### dry_run

`reaper.dry_run: true`, a per-area `dry_run: true`, `--dry-run`, or
`POST /api/v1/dry-run/<area>` runs the full scan, publishes the queues and
writes history rows with outcome `dry_run`, but never touches a file. Usage is
simulated (delete frees `size`; compress frees
`size * (1 - compress_assumed_ratio)`) so the report shows exactly which files
a live pass would consume. Host configs ship with `dry_run: true`; flip it
after watching `/queues` and `/history` for a day.

## Pages

| Path | Contents |
|---|---|
| `/` | Areas: cards per state (GOOD, WARNING, CRITICAL, FULL, PAUSED, DISABLED, UNKNOWN, MISSING), table with used %, thresholds and water marks, active tier, last scan, queue sizes; pause/resume/disable/enable (admin) |
| `/areas/<name>` | Area detail: usage history, tier configuration, current queues, recent actions |
| `/queues` | Compression and deletion queues across areas, filter by area; row detail (size, atime, mtime, owner, path, ordering key, status); Exclude button (admin) |
| `/exclusions` | Persisted exclusions: add by path or glob with optional expiry, remove |
| `/history` | Audit query: area, path glob, event type, tier, time range, paging; CSV export |
| `/notifications` | Channel status, last sent, recent log, test send (admin) |
| `/tokens` | API tokens: create (shown once), list (prefix, name, scopes, created, last used), revoke |
| `/config` | Settings in effect, parsed areas, issues, raw YAML |
| `/api` | API documentation with `curl` and `mu2edaq-reaper` examples |
| `/about` | Version, label, host, uptime, dependencies, database URL (redacted) |
| `/sitemap` | Structure of the application |
| `/login`, `/logout` | Admin token to session cookie for UI writes |

Area names are derived from the path (`/data/raw` -> `data-raw`) unless the
area sets `name:`.

## REST API

All routes live under `/api/v1`. Authentication is `Authorization: Bearer
rpr_...` with scopes `read` < `operate` < `admin` (a higher scope implies the
lower ones). `GET` routes marked *read* need no token while `api.open_read`
is true. The admin secret may also be presented as `X-Admin-Token`, which is
how the first token is minted. Full reference: `man 7 mu2edaq-file-reaper-api`.

| Method and path | Scope | Purpose |
|---|---|---|
| `GET /health`, `GET /version`, `GET /whoami` | none | liveness (`degraded` if the scheduler is dead or stalled, or the DB does not answer); version; who the caller is |
| `GET /state` | read | everything: areas, summary counts, scheduler health |
| `GET /areas`, `GET /areas/<name>` | read | area snapshots; the single-area form adds the parsed config and usage history |
| `POST /areas/<name>/pause`, `/resume`, `/rescan` | operate | body `{"reason": ...}`; rescan accepts `{"dry_run": true}` |
| `POST /areas/<name>/disable`, `/enable` | admin | stop or restart scanning an area |
| `POST /dry-run/<name>` | operate | run one dry-run scan synchronously and return the plan (409 if a scan is running) |
| `GET /queues?area=&kind=compress\|delete&status=` | read | queue entries across areas |
| `GET /queues/<id>` | read | one entry by `dev:ino` id or path |
| `GET /exclusions?area=&all=1` | read | active (or all) exclusion rules |
| `POST /exclusions`, `DELETE /exclusions/<id>` | operate | body `pattern` (or `path`), `area`, `kind` (path or glob), `reason`, `expires_in` (`"7d"`) or `expires_at` |
| `GET /history?area=&path=&type=&tier=&scan_id=&since=&until=&limit=&offset=&order=asc&format=csv` | read | audit query; `since`/`until` accept ISO 8601 or relative (`24h`) |
| `GET /history/types` | read | the event-type vocabulary |
| `GET /notifications?limit=` | read | channel status, last sent, recent log |
| `POST /notifications/test` | admin | body `channel` (optional), `severity`, `title`, `message` |
| `GET /tokens?all=1`, `POST /tokens`, `DELETE /tokens/<id>` | admin | body `name`, `scopes`, `expires_in`; plaintext token returned once |
| `GET /config` | read | settings (secrets redacted), areas, issues, notification config |

Errors are `{"error": "message"}` with 400, 401, 403, 404, 405 or 409. Every
mutating call writes a history event whose `actor` is `token:<name>` or
`ui:admin`.

```bash
# every area in CRITICAL or FULL
curl -s localhost:5004/api/v1/areas | python3 -c \
  'import json,sys; print([a["name"] for a in json.load(sys.stdin)["areas"]
   if a["state"] in ("CRITICAL","FULL")])'

# pause raw data cleanup while a run is being debugged
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"reason": "run 12345 investigation"}' localhost:5004/api/v1/areas/data-raw/pause

# what did the reaper delete in the last day?
curl -s "localhost:5004/api/v1/history?type=action_delete&since=24h&limit=500"

# CSV export of one area's history
curl -s "localhost:5004/api/v1/history?area=daqlogs&format=csv" -o daqlogs-history.csv

# mint the first token with the admin secret
curl -s -X POST -H "X-Admin-Token: $ADMIN" -H "Content-Type: application/json" \
  -d '{"name": "shifter", "scopes": ["read", "operate"]}' localhost:5004/api/v1/tokens
```

## Command line

`mu2edaq-reaper` talks only to the API (stdlib `urllib`), finds instances via
discovery, and caches tokens per URL. See `man 1 mu2edaq-reaper`.

```bash
mu2edaq-reaper instances                          # discovery scan for app=file-reaper
mu2edaq-reaper --instance dl-01 status
mu2edaq-reaper --url http://mu2e-dl-01:5004 areas
mu2edaq-reaper area data-raw
mu2edaq-reaper pause data-raw --reason "debugging run 12345"
mu2edaq-reaper plan data-raw                      # dry-run scan, prints the queue
mu2edaq-reaper queues --area data-raw --kind delete --limit 50
mu2edaq-reaper exclude add /data/raw/keep/ --reason "calibration set"
mu2edaq-reaper history --type action_delete --since 24h --csv > deleted.csv
mu2edaq-reaper notify test slack
mu2edaq-reaper token create ops --scopes read,operate,admin --expires-in 90d --save
mu2edaq-reaper --json status | jq .summary
```

Endpoint resolution: `--url` > `MU2EDAQ_REAPER_URL` > `--instance` /
`MU2EDAQ_REAPER_INSTANCE` via discovery. Token resolution: `--token` >
`MU2EDAQ_REAPER_TOKEN` > the cache entry for the URL.

## Notifications

Five independent channels. Each has `enabled` and `min_severity`
(`debug < info < warning < error < critical`). Identical
`(channel, area, key)` events within `rate_limit_seconds` are suppressed
unless the severity rose; `max_per_hour` caps total sends. Every attempt is
recorded in the `notification_log` table and shown on `/notifications`.

| Channel | Transport | Needs | Default |
|---|---|---|---|
| `mu2e_notify` | `mu2edaq_notify.NotifyPublisher` (phone notification system) | sibling package `mu2edaq-notify`, optional `url`/`token` | on, `warning` |
| `daq_messages` | ZMQ PUSH JSON to the dashboard (`tcp://host:5555`) | `pyzmq` | on, `warning` |
| `bigredbox` | UDP broadcast 37020 (`daq_alert` if installed, raw JSON otherwise) | nothing | off, `critical` |
| `email` | `smtplib` or `mail(1)` | `to:` list | off, `critical` |
| `slack` | incoming webhook | `webhook_url` | off, `warning` |

Severity mapping: tier activation `warning -> warning`, `critical -> error`,
`full -> critical`; a policy that cannot clear its tier is `error` (`critical`
for `full`); stall, FTS unavailable, missing area, unmeasurable usage and scan
crashes are `error`; stop point reached and tier cleared are `info`. A channel
whose import fails disables itself and reports why, never blocking startup.

## Discovery

When `mu2edaq-discovery` is installed and `discovery.enabled` is true, the
daemon answers scans as `app=file-reaper`, `name="File Reaper (<label>)"`,
with `meta` carrying `instance`, `api_port`, `api_path=/api/v1`, `areas` and
`dry_run`. `mu2edaq-reaper instances` lists them. The package is not on PyPI;
`bootstrap.sh` installs it from the sibling checkout. Its absence is harmless.

## Safety

The scan produces a *plan*; the action layer trusts none of it. The reaper
will never:

- manage a system root (`/`, `/bin`, `/boot`, `/dev`, `/etc`, `/home`,
  `/lib*`, `/opt`, `/proc`, `/root`, `/run`, `/sbin`, `/srv`, `/sys`, `/usr`,
  `/var`, and the macOS `/private*`, `/System`, `/Library`, `/Applications`),
  or a directory directly under `/` unless `reaper.allow_shallow_root` is set;
- follow a symlink, list one as a candidate, or cross a filesystem boundary;
- delete a hard-linked file unless `allow_hardlinks` is set;
- act on a file whose identity changed since the scan: the parent directory is
  opened with `O_DIRECTORY|O_NOFOLLOW` and its `(dev, ino)` compared with the
  scan, the file is opened by `dir_fd` + name with `O_NOFOLLOW` and its
  `(dev, ino)`, size, `mtime_ns`, link count and settle window re-checked;
  any mismatch is a skip, never an error;
- replace a file with an unverified archive: compression streams to a
  `.reaper-tmp-*` file in the same directory, verifies it (gzip trailer CRC and
  ISIZE, or a full decompression pass for other algorithms), refuses if the
  destination exists, copies mode/owner/atime/mtime, `rename`s into place,
  syncs the directory, and only then unlinks the original;
- delete raw data that FTS has not marked `COMPLETED`/`DELETED` when an
  `fts_db` gate is configured; an unreadable FTS database makes nothing
  eligible;
- touch a protected path: the FTS database, its own database and their
  journal files, or anything in `reaper.protected_paths`;
- act without a usage measurement: an area whose filesystem cannot be measured
  is `UNKNOWN` and skipped with an alarm;
- keep going when space is not coming back: every `stall_window` actions it
  checks that used space dropped by at least a quarter of what was freed, and
  otherwise stops the pass with a `space_not_reclaimed` alarm (files held open
  elsewhere).

Areas on the same filesystem never act concurrently. Pause and disable set an
interrupt that a worker honours at the next file or 1 MiB chunk. Manual
exclusions are re-read before every single action.

## Start and stop

```bash
./start-mu2edaq-file-reaper.sh                                # default config
./start-mu2edaq-file-reaper.sh config/mu2e-file-reaper-dl-01.yaml
./start-mu2edaq-file-reaper.sh -c config/test.yaml -p 5010    # or --config= / --port=
./start-mu2edaq-file-reaper.sh --no-replace                   # refuse if already running
./start-mu2edaq-file-reaper.sh --dry-run --verbose            # forwarded to the daemon
./stop-mu2edaq-file-reaper.sh
```

`crs-app start file-reaper` runs the start script, which reads `CRS_PORT_HTTP`
(default 5004) and forwards it as `--port`, then runs the daemon with
`--pid-file ./file-reaper.pid`. Unrecognised options are forwarded to
`file_reaper.py` unchanged.

**Only one copy runs at a time.** Before starting, the script looks for a copy
already running out of its own directory (pid file, plus the process table
filtered by command line and working directory via `lsof`) and stops it;
`--no-replace` exits 1 instead. Shutdown is `SIGTERM`, which lets the daemon
finish the file it is working on, then `SIGKILL` after `CRS_STOP_TIMEOUT`
seconds (default 30). The shared logic lives in `lib/file-reaper-proc.sh`.

On `SIGTERM` the daemon sets the shutdown flag and every area interrupt, waits
up to `reaper.shutdown_grace_s` (30 s) for in-flight scans, flushes the
notifier, stops the web server and discovery responder, disposes the database
and removes the pid file. A second signal exits immediately.

## Development

```bash
./bootstrap.sh
source venv/bin/activate
pytest
python3.9 -W error -c "import mu2edaq_file_reaper"   # AL9 compatibility check
```

A laptop walk-through that never fills a disk:

```bash
python tools/make_demo_tree.py ./data/demo --files 300 --days 30 --used-pct 82
./start-mu2edaq-file-reaper.sh -c config/mu2edaq-file-reaper-test.yaml
# edit data/demo-usage.json to move "used" across 80 / 90 / 95 and watch /queues and /history
```

### Layout

```
file_reaper.py                       entry-point shim (crs-app runs this)
pyproject.toml  requirements*.txt    packaging; console scripts mu2edaq-file-reaper, mu2edaq-reaper
CMakeLists.txt                       venv / pytest / install targets for the mu2edaq-main harness
bootstrap.{sh,ps1}  start-*  stop-*  platform scripts;  lib/file-reaper-proc.sh shared by start/stop
config/                              reference, test and host YAML; .env.example
docs/INSTALL.md  docs/DESIGN.md      install guide; design document
PROJECT-STATUS.md  CHANGELOG.md      phase/test/compat matrices; release notes
man/man1 man3 man5 man7              daemon, CLI, scripts, Python API, config, REST API
tools/make_demo_tree.py              demo tree + fake usage file
src/mu2edaq_file_reaper/
    settings.py    Settings singleton, MU2EDAQ_FILE_REAPER_* map, .env loader
    config.py      YAML -> AreaConfig list + issues (never raises)
    domain.py      frozen dataclasses shared by the engine
    units.py       size / percent / duration parsing (pure)
    watermarks.py  trigger, stop, hysteresis, tier selection (pure)
    eligibility.py per-file verdicts and REJECT_REASONS (pure)
    policies.py    policy registry, ordering keys, build_queue (pure)
    scanner.py     safe scandir enumeration, temp sweep, /proc open-file scan
    usage.py       df-style usage, fake_usage_file hook, atime-mode detection
    fts_gate.py    read-only FTS snapshot (fails closed)
    exclusions.py  thread-safe mirror of the exclusions table
    compressors.py gzip / bz2 / xz / zstd registry
    actions.py     verified delete, atomic verified compress, prune_empty_dirs
    reaper.py      AreaScanner: scan -> eligibility -> queue -> act -> history
    scheduler.py   scheduler thread, worker pool, pause/disable, shutdown
    state.py       StateStore (copy-on-write snapshots), AREA_STATE_KEYS
    auth.py        token generate / hash / verify, scopes, TokenCache (pure)
    db/            models.py, repo.py, __init__.py (engine, WAL, scoped_session)
    notify/        Notifier thread, channels.py (5 channels), ratelimit.py (pure)
    web/           Flask factory, auth.py (bearer + admin session), api_v1.py, nav.py, views, templates
src/mu2edaq_reaper_cli/              CLI client: client.py, tokencache.py, cli.py, commands/
tests/                               pytest suite
```

Notes for anyone extending it:

- Add user-visible changes to `CHANGELOG.md` under `[Unreleased]` as you make
  them. Anything that changes how an existing config behaves or what the API
  returns goes under **Compatibility**. Keep `PROJECT-STATUS.md` current.
- `settings.get_settings()` must be called *inside* functions, never bound at
  module scope: `cli.main()` mutates the singleton after import.
- Python 3.9 is production (AlmaLinux 9 system interpreter): `Optional[X]`,
  `Dict[...]`, never `X | None` or `list[str]` at runtime.
- `state.null_area_state()` defines every key the API can emit for an area.
  Add new fields there so success and failure paths stay identical;
  `AREA_STATE_KEYS` and a test enforce parity.
- Never mutate a dict obtained from `StateStore`; install a new one with
  `replace_area()` or `update_area()`.
- Anything that modifies the filesystem belongs in `actions.py` and must
  re-verify identity through `open_parent_verified()` and
  `open_file_verified()` before acting.
- `watermarks`, `eligibility`, `policies`, `units`, `auth`, `ratelimit` are
  pure: no I/O, no clock, no globals. Keep them that way; they carry the tests.
- Scripts stay bash 3.2 clean (macOS ships 3.2).

## Requirements

Python 3.9+, Flask 3.0+, PyYAML 6.0+, SQLAlchemy 2.0+. Optional: `pyzmq`
(DAQ messages), `zstandard` (zstd), `psycopg[binary]` (PostgreSQL),
`mu2edaq-discovery` and `mu2edaq-notify` (sibling checkouts, installed by
`bootstrap.sh`). Bootstrap 5 and Bootstrap Icons load from jsDelivr; without
outbound internet the pages work but render unstyled.

## Platforms

| Platform | Role | Notes |
|---|---|---|
| AlmaLinux 9, Python 3.9.21 | production (mu2e-dl-01, mgr, dcs nodes) | daemon mode, `/proc` open-file check, `relatime` detection via `/proc/self/mounts` |
| macOS | development | daemon mode works; `/proc` checks are skipped; `lsof` used by the scripts |
| Windows 11 | foreground only | `bootstrap.ps1`, `start-mu2edaq-file-reaper.ps1`, `stop-mu2edaq-file-reaper.ps1`; no `fork(2)`, so use a service wrapper such as NSSM for background use |

## Changes

[CHANGELOG.md](CHANGELOG.md) tracks what changed in each release, naming both
the code version and the repository tag.

## License

MIT. See [LICENSE](LICENSE).
