# Changelog

All notable changes to `mu2edaq-file-reaper`.

Entries are grouped by release. Each release names both the code version
(`__version__` in `src/mu2edaq_file_reaper/__init__.py`, reported by
`--version`, `/about` and `/api/v1/version`) and the repository tag the DAQ
release process uses (`tNN.NN.NN`, see `mu2edaq-main/TAGGING-HOWTO.md`),
since the two number differently.

Changes that alter behaviour an operator or an integration depends on are
called out as **Breaking** or **Compatibility**; anything not so marked is
additive.

## [Unreleased] - 0.1.0

First version. Not yet tagged; the first repository tag will follow the
`tNN.NN.NN` convention (proposed `t00.01.00-rc`).

### Added

- **Engine.** Per-area scan every `scan_interval` (default 600 s). Used space
  is measured the way `df` does; `warning` / `critical` / `full` tiers are
  percent-used thresholds (defaults 80 / 90 / 95) with `high_water` (trigger
  offset, default 0) and `low_water` (stop offset, default 10) and hysteresis
  that survives a restart. Only the most severe active tier acts on a pass;
  up to `max_passes_per_scan` (3) passes re-measure and re-enumerate so the
  cascade `full -> critical -> warning` runs within one scan with fresh
  queues. `policy_insufficient` is recorded and alarmed when a tier's queue
  empties above its threshold.
- **Four policies**: `LRU-Delete`, `Age-Delete`, `LRU-Compress`,
  `Age-Compress`. LRU orders by `max(atime, mtime)`, Age by `mtime`; `min_age`
  applies to the policy's own key. Defaults: warning `LRU-Compress` 7 d,
  critical `LRU-Delete` 3 d, full `Age-Delete` 1 d.
- **Compression** in place with `gzip` (default), `bz2`, `xz` (stdlib) or
  `zstd` (optional `zstandard`). Atomic temp + verify + rename + unlink;
  output preserves mode, owner, atime and mtime; `compress_verify`
  (`crc` | `size` | `none`), `compress_min_ratio` (0.95) with an
  incompressible-file memory, `compress_assumed_ratio` for dry-run estimates.
- **Eligibility rules** with named rejection reasons: `temp_name`,
  `excluded_glob`, `not_included`, `protected`, `manual_exclusion`,
  `hardlink`, `already_compressed`, `compressed_copy_exists`, `settling`,
  `too_young`, `fts_not_complete`, `open_file`, `outside_area`, plus
  `incompressible`. Include/exclude globs match the area-relative path or the
  basename; `settle_seconds` guards recent writes; symlinks are never followed
  and filesystem boundaries never crossed.
- **FTS gate.** Optional per-area `fts_db:`; only files whose `mu2edaq-fts`
  status is `COMPLETED` or `DELETED` are eligible. Read-only; an unreadable
  database makes nothing eligible (fails closed) and raises an `error`
  notification.
- **Action safety.** Identity re-verification at action time through
  `dir_fd` (`O_DIRECTORY|O_NOFOLLOW` on the parent, `O_NOFOLLOW` on the file,
  `(dev, ino)`, size, `mtime_ns`, `nlink`, settle window all re-checked);
  containment via `commonpath`; refusal to manage system roots or top-level
  directories (`reaper.allow_shallow_root` to override); protected set covering
  the FTS database, the reaper's own database, their journal files and
  `reaper.protected_paths`; `.reaper-tmp-*` sweep of crashed compressions;
  optional `prune_empty_dirs`; stall detector (`stall_window`) raising
  `space_not_reclaimed` when used space does not follow the bytes freed.
- **Dry run** globally (`reaper.dry_run`, `--dry-run`), per area
  (`dry_run: true`) and per request (`POST /api/v1/dry-run/<area>`, `rescan`
  with `dry_run`), with simulated usage so the plan is exact.
- **Manual exclusions** by exact path, glob or directory prefix, per area or
  global, with optional expiry; re-read before every action so an exclusion
  added mid-queue lands before the next file.
- **Pause / resume / disable / enable** per area from the UI, API and CLI,
  persisted in `area_state`, with an interrupt honoured at the next file or
  1 MiB chunk. Paused areas still scan and publish their queues; disabled
  areas are not scanned.
- **Audit history** (`history_events`): `scan_start/end/abort`,
  `tier_activate/deactivate`, `queue_add/remove`, `action_delete/compress/
  failed`, `policy_insufficient`, `space_not_reclaimed`,
  `exclusion_add/remove`, `area_pause/resume/disable/enable`,
  `notification_sent`, `token_create/revoke`, `maintenance`; queryable by area,
  path glob, type, tier, scan id and time range; CSV export; daily retention
  pruning (`database.history_retention_days`, 0 = keep).
- **Database** via SQLAlchemy 2.x: SQLite (WAL, `busy_timeout`) at
  `data/file-reaper.db` by default, PostgreSQL through `database.url`
  (`postgresql+psycopg://`). Tables `history_events`, `exclusions`,
  `api_tokens`, `area_state`, `notification_log`, `meta`.
- **Notifications** through five independently configured channels:
  `mu2e_notify` (phone notification system), `daq_messages` (ZMQ PUSH to the
  dashboard), `bigredbox` (UDP), `email` (SMTP or `mail`), `slack` (webhook).
  Per-channel `min_severity`, duplicate suppression within
  `rate_limit_seconds` with severity escalation, `max_per_hour` cap, every
  outcome logged to `notification_log`. Missing optional packages disable only
  their channel.
- **Web UI** (Flask 3, Bootstrap 5, diskwatcher layout): Areas, area detail,
  Queues, Exclusions, History, Notifications, Tokens, Config, API, About,
  Sitemap, login/logout. Read-only pages open; writes need the admin session
  (`reaper.admin_token`, CSRF-protected) or a bearer token.
- **REST API `/api/v1`** with bearer tokens (`rpr_` prefix, PBKDF2-SHA256
  hashes, 12-character prefix lookup, constant-time verify, 300 s cache) and
  scopes `read < operate < admin`; `api.open_read` waives tokens on `GET`.
  `X-Admin-Token` accepted for bootstrapping. Optional `reaper.api_port`
  starts a second listener serving `/api/*` only.
- **Bootstrap admin token**: when no admin secret is configured a random one
  is generated at start and logged once.
- **Discovery** responder (`app=file-reaper`) with `instance`, `api_port`,
  `api_path`, `areas` and `dry_run` in `meta`.
- **CLI `mu2edaq-reaper`**: `status`, `areas`, `area`, `pause`, `resume`,
  `disable`, `enable`, `rescan`, `plan`, `queues`, `exclude add|rm|list`,
  `history`, `notify test|status`, `token create|list|revoke|login|logout|whoami`,
  `instances`, `config`, `version`; `--json` everywhere; endpoint from `--url`,
  `MU2EDAQ_REAPER_URL` or discovery by `--instance`; token cache at
  `~/.config/mu2edaq/file-reaper/tokens.yaml` (0600, refused if readable by
  others).
- **Configuration** with precedence `command line > environment
  (MU2EDAQ_FILE_REAPER_*) > .env > YAML > defaults`; a `.env` next to the
  config or in the working directory; `--check-config`; problems reported as
  issues, never fatal except a named config file that does not exist.
- **Control-room scripts** `start-mu2edaq-file-reaper.sh` (`CRS_PORT_HTTP`,
  single-instance replace, `--no-replace`, `--pid-file`, pass-through of
  daemon options), `stop-mu2edaq-file-reaper.sh` (`SIGTERM` then `SIGKILL`
  after `CRS_STOP_TIMEOUT`, default 30 s), `lib/file-reaper-proc.sh`,
  `bootstrap.sh` (`--extras`), and Windows `bootstrap.ps1`, `start-*.ps1`,
  `stop-*.ps1`.
- **`config/mu2edaq-file-reaper.yaml`** (reference, commented),
  **`config/mu2edaq-file-reaper-test.yaml`** (demo tree with the
  `reaper.fake_usage_file` hook), **`config/mu2e-file-reaper-dl-01.yaml`**
  (data logger host config, ships with `dry_run: true`), `config/.env.example`.
- **`tools/make_demo_tree.py`** to build a demo tree with spread-out
  atimes/mtimes and write `data/demo-usage.json`.
- **Packaging**: `pyproject.toml` (console scripts, optional extras `zmq`,
  `zstd`, `postgres`), `CMakeLists.txt` (venv, pytest, install of man pages,
  scripts and configs), MIT `LICENSE`, `.gitignore`.
- **Documentation**: `README.md`, `CLAUDE.md`, `docs/INSTALL.md`,
  `docs/DESIGN.md`, `PROJECT-STATUS.md`, man pages `mu2edaq-file-reaper(1)`,
  `mu2edaq-reaper(1)`, `mu2edaq-file-reaper-bootstrap(1)`,
  `mu2edaq-file-reaper-start(1)`, `mu2edaq-file-reaper-stop(1)`,
  `make_demo_tree(1)`, `mu2edaq_file_reaper(3)`,
  `mu2edaq-file-reaper.conf(5)`, `mu2edaq-file-reaper-api(7)`, and the
  in-app `/api`, `/about` and `/sitemap` pages.

### Compatibility

- Thresholds are **percent of capacity used**. This is the opposite sense from
  `mu2edaq-diskwatcher`, whose `space:` percentages state how much must remain
  free. Do not copy numbers between the two configs unchanged.
- HTTP port **5004** (`CRS_PORT_HTTP`); 5002 is diskwatcher and 5003 is FTS.
  Discovery class `app=file-reaper`.
- `start-mu2edaq-file-reaper.sh` passes `--pid-file ./file-reaper.pid`, which
  overrides `reaper.pid_file` from the YAML.
- Host configs ship with `reaper.dry_run: true`. Nothing is modified until an
  operator flips it.
- Python 3.9 is the minimum and the production interpreter.
- The database schema is created with `create_all` (`meta.schema_version = 1`);
  there is no migration tooling in this version.
