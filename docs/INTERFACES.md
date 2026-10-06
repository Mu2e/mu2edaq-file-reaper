---
title: mu2edaq-file-reaper interfaces
group: data-movement
tier: A
audience: expert
summary: >
  Policy-driven disk cleanup daemon that measures configured DAQ disk areas, alarms at
  warning/critical/full tiers, and compresses or deletes files in LRU or age order (raw
  data gated on mu2edaq-fts transfer status) down to a low-water mark, with an audit
  history, web UI, REST API and CLI
---

# mu2edaq-file-reaper interfaces

Five interfaces, most consequential first:

1. [The mu2edaq-fts read contract](#3-the-mu2edaq-fts-read-contract), the delete gate (section 3)
2. [HTTP: REST API v1 and pages](#1-http) (section 1)
3. [Notification channels](#2-notification-channels) (section 2)
4. [Discovery](#4-discovery) (section 4)
5. [Files and environment](#5-files-and-environment) (section 5)

Line references are to `b4524a1` on `mu2e-sept-mega-review`. The man pages
`mu2edaq-file-reaper-api(7)` and `mu2edaq-file-reaper.conf(5)` are the user
reference; this page is the contract view, with the review's caveats.

---

## 1. HTTP

### 1.1 Listeners

| Listener | Bind | Serves | Source |
|---|---|---|---|
| web | `reaper.web_host:web_port`, default `0.0.0.0:5004` | pages and `/api/*` | `web/__init__.py:110-113`, `settings.py:72-73` |
| api (optional) | `reaper.web_host:api_port`, default off | `/api/*` only; anything else 404 JSON | `web/__init__.py:96-104,114-118` |

`start-mu2edaq-file-reaper.sh:20` sets `CRS_PORT_HTTP=5004` when unset and
passes it as `--port`, so the web port is 5004 on every scripted start.
Werkzeug `make_server(threaded=True)`, plain HTTP, no access log above
WARNING. 5004 is not registered in any `apps.yaml` yet (FR-016 b); fts's
`laptop_config.yaml` also uses 5004 (FTS-007).

### 1.2 Authentication

Resolved per request into a principal (`web/auth.py:68-82`), first match wins:

| Credential | Principal | Scopes |
|---|---|---|
| `Authorization: Bearer rpr_...` | `token:<name>` | as minted (`read`, `operate`, `admin`; higher implies lower, `auth.py:22-26`) |
| `X-Admin-Token: <reaper.admin_token>` | `ui:admin` | `admin` |
| admin session cookie (from `POST /login`) | `ui:admin` | `admin`; mutations also need `X-CSRF-Token` or `csrf_token` (`web/auth.py:129-130`) |
| none | `anonymous` | none |

`api.open_read: true` (default) waives authentication for `GET` routes marked
`allow_open_read` (`web/auth.py:123-125`). Tokens: `rpr_` + 256 bits, stored as
PBKDF2-SHA256 with a 12-character lookup prefix, verified results cached 300 s,
revocation evicts the cache entry (`auth.py`, `web/api_v1.py:439`). If no
`admin_token` is configured a random one is generated per start and logged
(FR-010). Full model: `DESIGN.md` section 11.

### 1.3 REST API v1

All under `/api/v1`, JSON in and out (`web/api_v1.py`). "open" means no
credential needed when `api.open_read` is true.

| Method | Path | Scope | Effect | Line |
|---|---|---|---|---|
| GET | `/health` | none, always | liveness: scheduler alive/stalled, DB ping, config issue count; `status` `ok` or `degraded` | `:81` |
| GET | `/version` | none, always | name, version, label, `api: v1` | `:100` |
| GET | `/whoami` | none, always | the resolved principal | `:445` |
| GET | `/state` | read, open | every area snapshot, summary, scheduler health | `:109` |
| GET | `/areas` | read, open | area snapshots and summary | `:123` |
| GET | `/areas/<name>` | read, open | one snapshot, its config, last 288 `scan_end` usage points | `:131` |
| POST | `/areas/<name>/pause` | operate | set paused, interrupt the running pass at the next file or 1 MiB | `:161` |
| POST | `/areas/<name>/resume` | operate | clear paused, request a scan | `:167` |
| POST | `/areas/<name>/rescan` | operate | request a scan; body `{"dry_run": true}` makes it a dry run (shared flag, FR-016 f) | `:173` |
| POST | `/areas/<name>/disable` | admin | disable the area, interrupt | `:179` |
| POST | `/areas/<name>/enable` | admin | enable, including an area disabled in YAML; requests a scan | `:185` |
| POST | `/dry-run/<name>` | operate | synchronous dry-run scan on the request thread; 409 if busy | `:191` |
| GET | `/queues` | read, open | queue entries; `?area= &kind=compress|delete &status=` | `:217` |
| GET | `/queues/<id or path>` | read, open | one entry (`id` is `<dev>:<ino>`) | `:239` |
| GET | `/exclusions` | read, open | active rules; `?area= &all=1` | `:253` |
| POST | `/exclusions` | operate | add `{pattern|path, area?, kind?, reason?, expires_in?|expires_at?}`; `kind` is not validated (FR-004) | `:262` |
| DELETE | `/exclusions/<id>` | operate | deactivate a rule | `:292` |
| GET | `/history` | read, open | audit events; `?area= path=<glob> type=a,b tier= scan_id= since= until= limit= offset= order=asc format=csv` | `:309` |
| GET | `/history/types` | read, open | the 22 event types (`db/models.py:37-46`) | `:341` |
| GET | `/notifications` | read, open | channel status, last sent, recent `notification_log` rows | `:351` |
| POST | `/notifications/test` | admin | send a test event to one channel or all | `:368` |
| GET | `/tokens` | admin | list tokens (`?all=1` includes revoked) | `:398` |
| POST | `/tokens` | admin | mint `{name, scopes, expires_in?}`; plaintext returned once | `:405` |
| DELETE | `/tokens/<id>` | admin | revoke | `:433` |
| GET | `/config` | read, open | scalar settings (redacted), areas, config issues, notification config (redacted) | `:454` |

No route deletes or compresses a named file, edits an area, or changes a
threshold. The destructive reach of an `admin` credential is: run the
configured policy now, enable a config-disabled area, remove exclusions,
disable every area (disk then fills).

Errors are `{"error": "<message>"}` with 400, 401, 403, 404 or 409.

**Response shapes.** An area snapshot has exactly the keys of
`null_area_state()` (`state.py:21-43`): `name path real_path label enabled
dry_run state rank paused* disabled* usage usage_str atime_mode tiers
active_tiers selected_tier scan queues progress reject_counts scan_errors
warnings config_errors last_scan_at next_scan_in_s`. `state` is one of
`GOOD PAUSED DISABLED WARNING CRITICAL FULL UNKNOWN MISSING` (`state.py:17-18`).
A queue entry (`state.py:63-72`): `id ("<dev>:<ino>") path rel name size
size_str atime mtime key_ts owner group mode queue tier policy status reason`.
A history event mirrors `history_events` (`db/models.py:56-78`): `id ts area
event_type path tier policy queue outcome reason size_bytes bytes_freed
duration_s used_pct_before used_pct_after count actor scan_id detail`.

### 1.4 Server-rendered pages

| Path | Auth | Notes |
|---|---|---|
| `/`, `/areas/<name>`, `/queues`, `/history`, `/exclusions`, `/notifications` | none | data loaded by JavaScript from `/api/v1`, so `open_read: false` empties them |
| `/tokens` | admin session, else redirect to `/login` | |
| `/config` | **none, even with `open_read: false`** | renders the raw config file (FR-003) |
| `/api`, `/sitemap`, `/about` | none | `/about` shows host, PID, versions and which optional integrations import (`daq_alert` included, relevant to FR-011) |
| `POST /login`, `POST /logout` | admin secret / none | `next=` restricted to same-site relative paths (`web/views.py:63-70`) |

---

## 2. Notification channels

`Notifier.emit()` enqueues; a daemon thread offers each event to every
channel, applying `min_severity` and the rate limiter (same
`channel|area:key` within `rate_limit_seconds` suppressed unless severity rose;
`max_per_hour`), and records the outcome in `notification_log`
(`notify/__init__.py:85-127`). Event keys and severities: `DESIGN.md`
section 10. All `meta` values are stringified (`notify/__init__.py:54`).

| Channel | Default | Transport | Wire payload | Source |
|---|---|---|---|---|
| `mu2e_notify` | on, `warning` | `mu2edaq_notify.NotifyPublisher.publish()`: `POST <url>/api/events`, `Authorization: Bearer <token>` if set, 5 s timeout | `{"severity", "title": "[file-reaper <label>] <title>", "message", "source": "file-reaper", "category": "DAQ", "host", "meta": {..., "instance", "area"}}` after the library's `normalize_event` | `notify/channels.py:55-81`; `mu2edaq-phone-notification-system/src/mu2edaq_notify/publisher.py:97-198` |
| `daq_messages` | on, `warning` | ZMQ `PUSH`, `connect("tcp://<host>:<port>")`, default `localhost:5555`; `LINGER 500`, `SNDTIMEO 1000`, `SNDHWM 100` | `{"subsystem": "DAQ", "error_level": "error"|"warning"|"normal"|"debug", "event_category": "software", "timestamp": "<UTC ISO-8601>", "message": "[file-reaper <label>] <title>: <message>"}` | `notify/channels.py:85-129` |
| `bigredbox` | **off**, `critical` | `daq_alert.send_alert` if importable (call is broken, FR-011), else raw UDP broadcast to `ip:port`, default `255.255.255.255:37020` | `{"system_id": "FILE-REAPER-<LABEL>", "timestamp": "<local naive ISO, seconds>", "message": "<title>: <message>"}` | `notify/channels.py:133-162` |
| `email` | off, `critical` | `smtplib` to `smtp_host:smtp_port` (default `localhost:25` in code, `smtp.fnal.gov` in the reference config), optional STARTTLS and login; or `mail -s` | Subject `[SEVERITY] [file-reaper <label>] <title>`; body = message, `instance:`, `area:`, `meta` lines | `notify/channels.py:166-204` |
| `slack` | off, `warning` | `POST webhook_url`, 10 s timeout | `{"text": "[SEV] [file-reaper <label>] <title>", "attachments": [{"color", "text": message, "fields": [...]}]}` | `notify/channels.py:208-236` |

Conformance with the receivers:

- **Dashboard**: the five keys and the PUSH-connect pattern match
  `mu2edaq-dashboard/docs/INTERFACES.md`, which lists the reaper as the
  conformant reference producer. The dashboard binds `ZMQ_PULL` on 5555; the
  reaper connects a `ZMQ_PUSH` (`channels.py:107,111`), so the socket pair is
  correct, and it sets `ZMQ_SNDTIMEO` (`:109`, config key `timeout_ms`, default
  1000). `ZMQ_IMMEDIATE` is not set: while the dashboard is down the first
  `SNDHWM` (100) messages are queued in the socket and recorded `sent`, then
  each further send blocks the notifier thread for 1 s and is recorded
  `failed`. Queued alarms are delivered, stale, when the dashboard returns.
- **Big Red Box**: the fallback payload is byte-for-byte the shape
  `daq_alert.send_alert` produces (`mu2edaq-bigredbox/libs/python/daq_alert/_sender.py:30-36`).
- **Phone notifications**: the server URL comes from config `url`, else
  `MU2EDAQ_NOTIFY_URL`, else discovery `app=notify`, resolved **once** when the
  channel is built at startup (`publisher.py:100-105`); a server that moves
  later is not followed until the reaper restarts. The token comes from config
  `token` or the real environment; `.env` does not work (FR-012). The server's
  port 8095 is its own config (`mu2edaq-phone-notification-system/config/notify-server.yaml:8`).

---

## 3. The mu2edaq-fts read contract

The fts side is specified in `mu2edaq-fts/docs/INTERFACES.md`, "The SQLite
`files` table as a public interface". This section is the reaper side and the
cross-check.

### 3.1 What the reaper reads

| Item | Value | Source |
|---|---|---|
| Configuration | per-area `fts_db: <path>`; `realpath`ed at config load; absent or empty disables the gate for that area | `config.py:221-226` |
| Open | `sqlite3.connect("file:<path>?mode=ro", uri=True, timeout=5.0)`; path not percent-encoded (FR-016 e) | `fts_gate.py:48-50` |
| Query | `SELECT filepath, compressed_path FROM files WHERE status IN ('COMPLETED','DELETED')` | `fts_gate.py:16,54-57` |
| Columns used | `filepath`, `compressed_path`, `status`. Nothing else: not `file_size`, `detected_at`, `transfer_end`, `checksum_verified`, `checksum_match`, `ready_for_deletion` | `fts_gate.py:56` |
| Result | immutable set of `normpath(p)` and `realpath(p)` for each non-null path | `fts_gate.py:58-68` |
| Frequency | once per pass of each gated area, before enumeration; never re-read during the act loop | `reaper.py:410-420` |
| Protection | the DB and its `-wal`, `-shm`, `-journal` are in the area's protected set | `reaper.py:137-139` |

### 3.2 Path matching

A file is eligible when its scanned path, or that path minus one of the
area's `compression.skip_suffixes`, is in the set (`fts_gate.py:30-38`).
Scanned paths are built from the area's realpath plus directory entries
(`scanner.py:39,88`), so they are canonical. fts records
`os.scandir(source_directory)` entries (`entry.path`), **top level only**,
with `source_directory` passed through `expanduser` but not `abspath`
(`mu2edaq-fts/fts/poller.py:95-98`, `fts/config.py:163`). Consequences:

- Only files directly in an fts `source_directory` can ever be eligible.
  Subdirectories never are. One fts instance covers one directory (FR-007).
- If fts's `source_directory` is relative, its recorded paths are relative;
  the reaper's `realpath` resolves them against the **reaper's** working
  directory, nothing matches, and the area fails closed.
- If fts's `source_directory` is a symlink, the reaper resolves it at snapshot
  time, not at record time; repointing it aliases old rows onto new files
  (FR-001).
- A reaper-compressed `X.gz` is accepted through the suffix rule when `X` is in
  the set; fts's own compressed copy is accepted through `compressed_path`.
- fts follows symlinks when scanning (`poller.py:96`) and records the link's
  path. The reaper never acts on a symlink, but its `realpath` alias puts the
  link's target in the set, so a target lying inside a gated area is eligible.
  fts shipped the target's bytes, so this is consistent, but it means an fts
  row can make a file outside fts's `source_directory` eligible.

### 3.3 Agreement with mu2edaq-fts/docs/INTERFACES.md

| Item | fts side | Reaper side | Agree |
|---|---|---|---|
| Table and columns | `files`; `status`, `filepath`, `compressed_path` load-bearing | reads exactly these | yes |
| Eligible statuses | `COMPLETED` (`database.py:15`), `DELETED` (`database.py:18`), exact spelling | `{"COMPLETED", "DELETED"}` | yes |
| Meaning of `COMPLETED` | transfer exited 0; **remote checksum not yet compared**, and stays `COMPLETED` on mismatch | treated as "bytes are safely elsewhere" | **no**: FR-002, FTS-001 |
| `filepath` identity | `UNIQUE`; a `DELETED` or `COMPLETED` row suppresses re-transfer of a new file at that path (FTS-002) | path is the only identity key | **no**: FR-001 |
| `filepath` form | described as "absolute source path" | assumes absolute | only if fts's `source_directory` is absolute (see 3.2) |
| WAL and `mode=ro` | WAL enabled; a `mode=ro` reader cannot replay a hot WAL | any `sqlite3.Error` fails closed | yes |
| `clear_all_history` | empties the table | empty set allows nothing; no FTS alarm, shows as `policy_insufficient` | yes |
| Already-absent file | fts doc: "the reaper likewise" treats it as success | reaper records a **skip**, reason `vanished` (`actions.py:122-123`) | benign difference; noted for the fts doc |

### 3.4 Failure modes of the gate

| Condition | Result | Alarm |
|---|---|---|
| `fts_db` path missing at startup | area kept, `config_errors` lists it; every pass fails closed until it appears | `config_errors` on `/config`, then per pass |
| DB missing, unreadable, locked past 5 s, corrupt, or `no such column` (old schema) | `FtsUnavailable` -> `EMPTY_SNAPSHOT` -> every file `fts_not_complete` | `error` "FTS gate unavailable", area warning |
| Error part-way through the result set | same; the partial set is discarded | same |
| fts killed with a hot WAL | `mode=ro` cannot recover -> same; clears when fts (any writer) opens the DB once | same |
| WAL DB with no `-shm` and the reaper cannot create one in fts's directory (different user, fts stopped cleanly) | open fails -> same | same |
| Readable, zero eligible rows (fresh DB, history cleared, wrong DB) | nothing eligible | none from the gate; `policy_insufficient` with `fts_not_complete` counts |
| Row `COMPLETED` with `checksum_match = 0` | **eligible** | none (FR-002) |
| New file at a path with a `COMPLETED`/`DELETED` row | **eligible** once past `min_age` | none (FR-001) |

### 3.5 Delete authority boundary

Both daemons unlink files in the same directories and nothing arbitrates
between them: no lock, lease or flag. They agree only by keying off the same
rows.

| Deleter | Guard | Code |
|---|---|---|
| fts cleaner | `ready_for_deletion = 1`, set only after a remote checksum match (or by the manual `mark-ready`, FTS-004) | `mu2edaq-fts/fts/cleaner.py:151-181` |
| fts web single / bulk delete | `ready_for_deletion` | `mu2edaq-fts/fts/web.py:1672-1697` |
| fts pipeline, `keep_original: false` | transfer exit status | `mu2edaq-fts/fts/poller.py:335-340` |
| reaper | tier active, policy eligibility, `status IN ('COMPLETED','DELETED')` by path, identity re-check | `actions.py:155-176` |

Intended division: fts removes what it shipped **and verified**; the reaper
removes older material under space pressure and uses fts as a veto on what is
still in flight. In practice the reaper's veto is weaker than fts's own
deletion rule: fts's cleaner waits for a checksum match, the reaper does not.

Races, both benign: fts deletes `X` after the reaper's snapshot, the reaper's
open fails, `vanished` skip. The reaper deletes `X` first, fts's cleaner finds
no file (`cleaner.py:165`), marks the row `DELETED` anyway. The reaper
compresses `X` to `X.gz` before fts's verifier runs: verification reads only
the remote copy, so it is unaffected.

**FTS-002 exposure (direct answer).** The reaper is exposed. Its gate compares
nothing beyond path and status: the query selects only `filepath` and
`compressed_path` (`fts_gate.py:56`) and the decision is set membership
(`fts_gate.py:33-34`). There is no inode, size, mtime or transfer-time
comparison. The action layer's identity check (`actions.py:136-145`) compares
against the reaper's own scan, so it cannot detect that the bytes on disk are
not the bytes fts shipped. `settle_seconds` and `min_age` delay deletion of a
freshly written file by at most `min_age` (1 to 7 days for the delete tiers of
the shipped gated areas) and do not delay an mtime-preserving copy at all. fts's poller skips `COMPLETED`
rows as well as `DELETED` ones (`poller.py:127-128,149-150`), so the hazard
covers both. Filed as FR-001.

**FTS-004 exposure (direct answer).** The reaper does not read
`checksum_verified`, `checksum_match` or `ready_for_deletion`; it looks only at
`status`. `mark-ready` refuses rows that are not `COMPLETED`
(`mu2edaq-fts/fts/web.py:1655-1656`) and changes no status, so it cannot make
anything reaper-eligible that was not already. FTS-004 therefore adds no
exposure. What does matter is the status-only test itself: a `COMPLETED` row is
reaper-eligible while unverified and, worse, after a failed verification,
because fts never moves a mismatched row out of `COMPLETED`
(`poller.py:477-478,500-506`). `DELETED` rows are harmless except through
FTS-002, since the local copy is already gone. Filed as FR-002.

**Constraint for fts configuration.** If a reaper area overlaps an fts
`source_directory` and uses a compress policy, fts's `include_patterns` must
not match the reaper's suffixes or `.reaper-tmp-*`, or fts ships the reaper's
outputs. fts's code default is `["*"]` (FR-016 i).

---

## 4. Discovery

| Role | Call | Source |
|---|---|---|
| Daemon announces | `Responder(name="File Reaper (<label>)", app="file-reaper", port=web_port, scheme="http", version, meta={instance, api_port, api_path="/api/v1", areas, dry_run})`, all `meta` values strings | `discovery.py:17-33` |
| CLI finds daemons | `discover(filter={"app": "file-reaper"})`, `--instance <label>` matched on `meta.instance` | `mu2edaq_reaper_cli/config.py:196-235` |
| Notify channel finds the notify server | `discover(filter={"app": "notify"})` inside `NotifyPublisher` | `publisher.py:66-91` |

Conformance with `mu2edaq-discovery/doc/INTERFACES.md` section 6: `meta`
values are strings (`discovery.py:24-28`), the record is small, and the
Responder signature matches (`mu2edaq-discovery/src/mu2edaq_discovery/responder.py:33`).
A failed import or start is logged at INFO and ignored (`discovery.py:36-38`).
Discovery is unauthenticated; the CLI sends its bearer token, and the notify
channel its token, to whichever host answered. Pin `--url` and
`notifications.mu2e_notify.url` where that matters.

---

## 5. Files and environment

| Path | Purpose | Notes |
|---|---|---|
| `config/*.yaml` | configuration (`man 5 mu2edaq-file-reaper.conf`) | reference, dl-01 node, test, sandbox |
| `config/.env` (or `.env` next to the config, or `./.env`) | secrets below the real environment | only `MU2EDAQ_FILE_REAPER_*` keys are honoured |
| `data/file-reaper.db` | SQLite: `history_events`, `exclusions`, `api_tokens`, `area_state`, `notification_log`, `meta` | schema `DESIGN.md` section 9; any SQLAlchemy URL, PostgreSQL via `psycopg` |
| `logs/mu2edaq-file-reaper.log` | log | daemon mode: WARNING+ only, no timestamps, no rotation (FR-009) |
| `file-reaper.pid` | pid file of the daemon | removed at exit |
| `~/.config/mu2edaq/file-reaper/tokens.yaml` | CLI token cache, refused unless `0600` | `tokencache.py:45-53` |
| `~/.config/mu2edaq/file-reaper/cli.yaml` | CLI config | `mu2edaq_reaper_cli/config.py:58-67` |

Environment, daemon (`settings.py:29-46`, `cli.py:117`,
`web/__init__.py:29`): `MU2EDAQ_FILE_REAPER_{CONFIG, ENV_FILE, LABEL,
WEB_HOST, WEB_PORT, API_PORT, SCAN_INTERVAL, DRY_RUN, DAEMON, PID_FILE,
LOG_FILE, VERBOSE, ADMIN_TOKEN, WORKERS, DATABASE_URL, OPEN_READ, DISCOVERY,
SECRET_KEY}`. `SECRET_KEY` (Flask session signing) is read from the real
environment only (`web/__init__.py:29`), not `.env`; unset, a random key is
generated per start and admin sessions end at every restart. Booleans accept
`1 true yes on` / `0 false no off` and the empty string as false
(`settings.py:48-49`). Read by libraries in the process: `MU2EDAQ_NOTIFY_URL`,
`MU2EDAQ_NOTIFY_TOKEN`, `MU2EDAQ_NOTIFY_FALLBACK_URL`,
`MU2EDAQ_NOTIFY_CATEGORY`. Scripts: `CRS_PORT_HTTP`, `CRS_STOP_TIMEOUT`,
`FR_DRY_RUN` (test hook, start script only), `MU2EDAQ_PYTHON` (bootstrap).
CLI (`mu2edaq_reaper_cli/config.py:31-37`, `tokencache.py:24`):
`MU2EDAQ_REAPER_{URL, INSTANCE, TOKEN, ADMIN_TOKEN, TIMEOUT, CONFIG,
TOKEN_FILE}`.
