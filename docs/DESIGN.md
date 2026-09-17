# mu2edaq-file-reaper design

Code version 0.1.0. This document records what the reaper is meant to do,
the decisions taken where the specification (`FileReaper.md`) left room, the
algorithms as implemented, the safety analysis, the data model and the
testing strategy. Where the implementation plan and the code differ, the code
is described.

## 1. Goals

1. Monitor the used space of configured disk areas on a Mu2e DAQ node.
2. Alarm the DAQ (phone notifications, dashboard messages, Big Red Box,
   email, Slack) when used space crosses configurable `warning`, `critical`
   and `full` tiers.
3. Bring used space back under control by compressing or deleting files under
   a per-tier policy, with per-tier low and high water marks.
4. Keep a complete, queryable audit history of queue membership and actions.
5. Expose everything through a web UI, a bearer-token REST API and a CLI.
6. Advertise every instance through `mu2edaq-discovery` so the fleet is
   visible from the control room.

Non-goals for this version: remote (SSH) areas, SSO for the UI, schema
migrations, quota-style per-user accounting.

The guiding principle is that of a production DAQ tool: the daemon must keep
running on a partly bad configuration, must refuse to act whenever it is not
sure, and must leave enough evidence to reconstruct what it did.

## 2. Decisions

The four open questions in the specification were answered with the
recommended option (the user did not reply within the planning window). Each
is a configuration or small code change if a different choice is wanted;
they are also listed in `PROJECT-STATUS.md` as open issues.

| Topic | Decision | Rationale |
|---|---|---|
| Threshold semantics | `warning/critical/full` are **percent of capacity used** (defaults 80/90/95). Per tier `high_water` (default 0) is added to get the trigger; `low_water` (default 10) is subtracted to get the stop. Hysteresis: active at `used >= trigger`, stays active until `used <= stop`. | Matches the spec's water-mark wording ("low water mark 10 % below the threshold, high water mark 0 above"); compares directly with `df -h`. Diskwatcher's free-space percentages are the opposite sense, called out in every document. |
| Default policies | warning `LRU-Compress` 7 d, critical `LRU-Delete` 3 d, full `Age-Delete` 1 d; every area may override any tier or disable it with `null`. | Compress before delete; LRU for the tiers where access history matters; strict age at `full` where predictability matters more. |
| Cross-tier rule | Only the highest active tier (`full > critical > warning`, by name, not by threshold value) acts on a pass; passes repeat with fresh usage and enumeration up to `max_passes_per_scan` (3). | Running several tiers on one enumeration would act on stale queues; ordering by name keeps `full` meaning "most aggressive" even with misordered thresholds. |
| Compression | gzip (stdlib) default; bz2, xz (stdlib), zstd (optional). Output keeps atime/mtime/mode/owner and remains eligible for later delete tiers. Known compressed suffixes skipped. | Nothing to install on AL9; preserved timestamps keep the LRU/age semantics meaningful across tiers. |
| Eligibility and FTS safety | Regular files under the area, same filesystem, no symlinks; include/exclude globs; `min_age` on the policy's key; `settle_seconds`; temp names excluded; optional per-area `fts_db` gate accepting only `COMPLETED`/`DELETED`, failing closed. | Raw data must never leave the node before FTS has shipped and verified it; volumes without FTS stay usable. |
| UI authentication | Read-only pages open (control-room network). Writes need an admin token (YAML/env, bootstrap token logged when absent) or a scoped bearer token. | Same openness as every other dashboard; SSO can layer on later without changing the API model. |
| API port | Same Werkzeug listener as the UI by default; optional `api_port` starts a second server that serves `/api/*` only (anything else gets a JSON 404). | Lets the API be firewalled or proxied independently; both ports appear in discovery `meta`. |
| DAQ messages | ZMQ PUSH JSON to the dashboard (`tcp://host:5555`, `mu2edaq-dashboard/sender.py` schema). Big Red Box UDP 37020 as a separate, default-off channel. | The Big Red Box is for emergencies; keep it opt-in and `critical` only. |
| Storage | SQLAlchemy 2.x, SQLite WAL default at `data/file-reaper.db`, PostgreSQL via `database.url`. | Project convention; SQLite suffices per node. |
| Python and platforms | Python >= 3.9 (`Optional[X]` typing), bash 3.2 clean scripts, Windows foreground only. | AL9 system Python is 3.9.21; macOS bash is 3.2. |
| Local paths only | One reaper per host; no SSH areas. | Acting remotely would need remote identity re-verification; not worth the risk. |
| Port and discovery | HTTP 5004 (`CRS_PORT_HTTP`), `app=file-reaper`, label and `api_port` in `meta`. | 5002 is diskwatcher, 5003 is FTS. |

## 3. Water-mark math

Everything is percent of `used + free`, the `df` convention:

```
used_pct = 100 * used / (used + free)
```

(`shutil.disk_usage` reports `total`, `used`, `free`; `total` may exceed
`used + free` by the root-reserved blocks, which is why `df`'s `Use%`
denominator is used.) For a tier with threshold `T`, `high_water h`,
`low_water l`:

```
trigger(T) = min(T + h, 100)
stop(T)    = max(T - l, 0)

active(T)  = used >  stop(T)      if the tier was active on the previous evaluation
           = used >= trigger(T)   otherwise

policy_insufficient(T) = used > T  after the tier's queue is exhausted
```

Comparisons are inclusive on the bad side: reaching the trigger exactly
activates, reaching the stop exactly deactivates. Clamps to 100 and 0 are
recorded in `Marks.clamped` and reported as config issues (a tier that can
only trigger on a completely full disk, or a stop point of 0 that runs until
nothing eligible remains). Negative water marks and thresholds outside
`(0, 100]` drop the tier with an issue. Thresholds that do not ascend
`warning < critical < full` are reported, not corrected; `full` still runs
first when both are active.

The active set is persisted in `area_state.active_tiers` after every
evaluation, so hysteresis survives a restart: a tier that was acting when the
daemon stopped resumes acting at the first scan even if used space has
drifted below its trigger.

`bytes_to_stop = used - (used + free) * stop / 100` is published per tier so
operators can see how much the current pass must free.

## 4. Cross-tier rule

Per pass only the most severe active tier acts. After the pass the reaper
re-measures usage and, if a tier is still active and not marked exhausted,
runs another pass with a fresh enumeration, up to `max_passes_per_scan`.

Consequences:

- Queues are never stale: each pass enumerates the area as it is now, so a
  file compressed by the warning tier appears with its new size and name in
  the next pass's delete queue if that tier is active.
- The spec's "act on the compression queues, then the deletion queues"
  ordering falls out for free whenever the more severe tier is a delete
  policy and the lower one compresses: the delete pass runs first (it is the
  more urgent), and once it reaches its stop point the compress tier gets its
  own pass on what remains.
- Exhaustion falls through: if a tier's queue empties while used space is
  still above `T`, `policy_insufficient` is recorded and alarmed (`error`,
  `critical` for `full`), the tier is marked exhausted for this scan, and the
  next lower active tier gets a pass.

## 5. Scan and act algorithm

Implemented in `reaper.py` (`AreaScanner`). Pseudocode:

```
scan_area(area, rt, deps, S):
  return skipped if rt.disabled or not rt.try_acquire()      # one scan per area at a time
  rt.interrupt.clear()
  with deps.fs_lock(st_dev(area)):                           # areas on one filesystem never act together
    record scan_start
    if area path is not a directory: publish MISSING, alarm, return
    mode = atime_mode(area); warn if noatime and any LRU tier
    if not dry_run: sweep .reaper-tmp-* older than settle_seconds
    active = rt.active_tiers
    for pass in 1..max_passes_per_scan:
      usage = measure_usage(area)
      if usage is None: publish UNKNOWN, alarm "cannot measure", return    # never act blind
      active = evaluate_tiers(usage.used_pct, tiers, rt.active_tiers)
      record/notify tier_activate (severity by tier) and tier_deactivate (info)
      rt.active_tiers = active; persist to area_state
      tier = highest_active(active - exhausted) or (publish idle snapshot; break)
      policy, marks = POLICIES[tier.policy], resolve_marks(tier)
      fts = load_fts(area.fts_db) if configured; unreadable => EMPTY_SNAPSHOT + error alarm
      open_inodes = /proc scan if area.check_open_files
      infos = enumerate_files(area)                          # scandir, no symlinks, same st_dev, dir (dev,ino)
      eligible, rejects = partition(infos, EligibilityContext(...))
      drop entries remembered as incompressible (compress policies)
      queue = build_queue(eligible, policy, tier)            # (ordering key asc, path asc)
      publish snapshot(usage, tiers, marks, queue[:queue_publish_limit], rejects)
      record queue_add rows (all | new_only | none), with trigger/stop/key_ts
      if queue empty:
        if used > T: policy_insufficient (record + alarm) else queue_remove(no_eligible)
        exhausted.add(tier); continue
      if rt.paused: record queue_remove(paused); break       # paused = plan and publish, never act
      stop, usage = act_on_queue(queue, tier, marks, usage)
      stop_reached      -> info notification; continue
      exhausted         -> re-measure; policy_insufficient if used > T; exhausted.add(tier); continue
      paused|disabled|shutdown -> queue_remove(remaining); scan_abort on shutdown; break
      limit|stalled|error      -> break
    prune_empty_dirs(touched_dirs) if enabled and not dry_run
    publish final snapshot; record scan_end
  finally: rt.release(); area_state.mark_scan(); Session.remove()

act_on_queue(queue, tier, marks, usage):
  for entry in queue:
    stop on: shutdown | interrupt (paused/disabled) | not should_continue(used > stop) | max_actions_per_scan
    if is_excluded(entry.path, fresh exclusion rules): queue_remove(excluded); continue
    result = compress_file | delete_file (entry.info, ActionContext)  # identity re-verified inside
    skipped  -> queue_remove(reason); remember incompressible; return on "interrupted"
    failed   -> action_failed
    ok/dry_run -> acted++, freed += bytes_freed
              usage = simulate_after(usage, freed) if dry_run else measure_usage() every remeasure_every
              record action_compress|action_delete with used_pct_before/after, bytes_freed, dest_path
              throttled StateStore progress update (2 s or every 25 actions)
              every stall_window actions: if observed drop < 25 % of expected drop
                 -> space_not_reclaimed (record + error alarm); return stalled
  return exhausted
```

Ordering keys: LRU is `max(atime, mtime)`, Age is `mtime`; `min_age` is
applied to the same key. `settle_seconds` rejects anything modified recently
regardless of policy. Dry run uses `simulate_after` (delete frees `size`;
compress frees `size * (1 - compress_assumed_ratio)`) so the published plan
consumes exactly the files a live pass would.

Pause and disable differ: a paused area is still scanned and its queue is
published (operators see what would happen); a disabled area is never
scanned. Both set `AreaRuntime.interrupt`, which a running worker checks
between files and between 1 MiB chunks during compression.

## 6. Eligibility rules and rejection reasons

`eligibility.classify()` is pure; every input arrives in
`EligibilityContext`. Checks run cheapest first and the first failing check
names the reason. `REJECT_REASONS`:

| Reason | Check |
|---|---|
| `outside_area` | path does not start with the area realpath |
| `temp_name` | basename matches `*.tmp`, `*.part`, `*.partial`, `.*.swp`, `*~`, `.reaper-tmp-*`, `.~*` |
| `excluded_glob` | `exclude:` glob matches the relative path or basename |
| `not_included` | no `include:` glob matches |
| `protected` | path is in the protected set (FTS DB + journals, own DB + journals, `protected_paths`) |
| `manual_exclusion` | exact path, glob, or directory-prefix rule from the `exclusions` table (global or per area) |
| `hardlink` | `nlink > 1` and `allow_hardlinks` false |
| `already_compressed` | compress policies: name ends in a `skip_suffixes` entry |
| `compressed_copy_exists` | compress policies: `name + suffix` exists in the same directory for any configured suffix (a previous run compressed but did not unlink; the original stays delete-eligible) |
| `settling` | `now - mtime < settle_seconds` |
| `too_young` | `now - ordering_key < min_age` |
| `fts_not_complete` | FTS gate configured and neither the path nor the path minus a compression suffix is `COMPLETED`/`DELETED` |
| `open_file` | `(dev, ino)` appears in the `/proc/*/fd` scan (`check_open_files`) |

`incompressible` is applied after classification from the per-area memory of
`(path, size, mtime_ns)` triples whose last compression attempt did not shrink
the file below `compress_min_ratio`; it appears in `reject_counts` alongside
the others. Rejection counts are published per scan so an operator can see
why a full disk has an empty queue.

## 7. Safety analysis

**Identity re-verification.** The scan yields `FileInfo` (path, size,
`atime_ns`, `mtime_ns`, uid/gid/mode, `nlink`, `(dev, ino)` and the parent
directory's `(dir_dev, dir_ino)`). Minutes may pass before the action. The
action layer therefore opens the parent with `O_RDONLY|O_DIRECTORY|O_NOFOLLOW`
and requires `fstat` to match `(dir_dev, dir_ino)`; opens the file by name
relative to that `dir_fd` with `O_NOFOLLOW|O_NONBLOCK` (plus `O_NOATIME` when
permitted) and requires a regular file with matching `(dev, ino)`, size,
`mtime_ns`, `nlink == 1` (unless allowed) and an mtime older than the settle
window; and unlinks via `dir_fd` + name. Any mismatch, `ELOOP` (path became a
symlink), or `ENOENT` is a `skipped` result with reason `changed` or
`vanished`, never an error. The file is simply seen again on the next scan.

**Atomic compression.** `compress_file` refuses if the destination already
exists, `mkstemp`s `.reaper-tmp-*.<pid><suffix>` in the same directory,
streams 1 MiB chunks computing a CRC32 while checking the interrupt and
shutdown events, `fsync`s, re-`fstat`s the source (size, mtime, inode and
bytes copied must be unchanged), verifies the temp (`crc`: gzip trailer CRC
and ISIZE, or a full decompression pass for other algorithms; `size`: ISIZE
or decompressed length only; `none`), applies the ratio guard
(`out_size >= min_ratio * size` => `skipped/incompressible`, remembered),
`fchmod`/`fchown`/`utime` from the source stat, checks the destination once
more, `rename`s into place, `fsync`s the directory, and only then unlinks the
original. A crash leaves the original plus a temp file (swept next scan once
older than `settle_seconds`) or both files, never a half-written archive
under the final name.

**Containment.** `real_path` is resolved at config time. An area whose
realpath is in `SYSTEM_ROOTS` (POSIX roots plus the macOS `/private*`,
`/System`, `/Library`, `/Applications`) or whose depth is less than 2 is
dropped with an issue unless `reaper.allow_shallow_root`. The action layer
re-checks `os.path.commonpath` containment of the realpath and the protected
set before every action. `prune_empty_dirs` walks upward only while still
strictly inside the area root and stops at protected paths and symlinks.

**Symlinks and filesystem boundaries.** `enumerate_files` uses iterative
`os.scandir`, never follows symlinks (files or directories), counts them, and
skips any entry whose `st_dev` differs from the area root.

**Hard links.** Deleting one link of an `nlink > 1` file frees nothing and
may surprise whoever owns the other name; rejected unless `allow_hardlinks`.

**Open files.** Optional `check_open_files` scans `/proc/*/fd` (Linux only;
`None` on macOS skips the check). Because the check is expensive and racy,
the stall detector is the real backstop: every `stall_window` actions, if
used space fell by less than a quarter of what the freed bytes predict, the
pass stops with `space_not_reclaimed` and an `error` alarm ("files may be held
open by another process").

**atime modes.** `atime_mode()` reads `statvfs` flags and falls back to
`/proc/self/mounts` (longest mount-point prefix). Under `noatime` LRU degrades
to mtime because the key is `max(atime, mtime)`; the area publishes a
warning. Under `relatime` (the Linux default) atime updates at most daily,
which is adequate for `min_age` measured in days.

**FTS gate fails closed.** `load_fts_snapshot` opens the FTS SQLite database
with `mode=ro`, reads `filepath` and `compressed_path` for rows with status
`COMPLETED` or `DELETED`, and stores both `normpath` and `realpath` of each.
Missing file, locked database or any `sqlite3.Error` raises `FtsUnavailable`;
the scanner substitutes `EMPTY_SNAPSHOT`, so every file is
`fts_not_complete`, and raises an `error` notification. The gate is applied
per pass, so a recovered FTS database takes effect on the next pass.

**Never act blind.** A `None` usage sample (unmeasurable filesystem) publishes
`UNKNOWN`, alarms, and ends the scan. Dry-run mode is honoured at three
levels (global, per area, per request) and is the shipped default for host
configs.

**Process safety.** Every scan runs inside `try/except Exception`; a crash is
recorded as `scan_abort`, alarmed, and never kills the scheduler. Every
history and notification call is wrapped so a database hiccup cannot abort a
pass mid-file.

## 8. Concurrency model

Threads:

| Thread | Role |
|---|---|
| main | parses config, daemonizes, starts everything, handles `SIGTERM`/`SIGINT`, waits on a stop event, exits if the web thread dies |
| `reaper-scheduler` | first tick immediately, then every `scan_interval`; drains `request_scan` requests when woken; daily maintenance (history prune) |
| `reaper-worker-*` | `ThreadPoolExecutor(workers)` running `scan_area`; one future per area, never two for the same area |
| `reaper-notifier` | drains the notification queue; fan-out, rate limit, logging |
| `reaper-web`, `reaper-api` | Werkzeug `make_server(threaded=True)`; per-request threads |

Locks and signals:

- `StateStore._lock`: held only to swap a dict; readers receive copies.
- `AreaRuntime.lock`: protects `busy` and the pause/disable fields;
  `AreaRuntime.interrupt` (an `Event`) is set by pause, disable and shutdown
  and cleared at scan start (and by resume/enable when neither flag remains).
- `Deps.fs_locks[st_dev]`: serialises scans of areas on the same filesystem
  so two workers never fight over one disk's free space.
- `ExclusionRegistry._lock`: returns immutable `ExclusionRules`; the registry
  is reloaded from the database after every API change and consulted before
  every action.
- Notifier: `emit()` only enqueues; nothing on the scan path blocks on a
  network call.

Database: WAL, `synchronous=NORMAL`, `busy_timeout=5000`,
`check_same_thread=False`, a `scoped_session`, one short transaction per
repository method (`session_scope`), `Session.remove()` at scan end and on
Flask teardown. Repositories return plain dicts, never live ORM objects.

Shutdown: `SIGTERM` sets the stop event; `Scheduler.stop()` sets the shutdown
event and every area interrupt, joins the scheduler thread, waits up to
`shutdown_grace_s` for in-flight futures (which record `scan_abort`), then the
notifier is drained, the discovery responder and servers stopped, the engine
disposed and the pid file removed by the `atexit` hook. A second signal calls
`os._exit(1)`.

## 9. Database schema

SQLAlchemy 2.x declarative models in `db/models.py`; `create_all` at startup;
`meta.schema_version = 1`. Byte counts are `BigInteger`; `JSON` maps to TEXT
on SQLite; timestamps are timezone-aware UTC (`_aware()` repairs SQLite's
naive values).

| Table | Columns | Indexes |
|---|---|---|
| `history_events` | `id`, `ts`, `area`, `event_type`, `path`, `tier`, `policy`, `queue`, `outcome`, `reason`, `size_bytes`, `bytes_freed`, `duration_s`, `used_pct_before`, `used_pct_after`, `count`, `actor`, `scan_id`, `detail` JSON | `(area, ts)`, `(event_type, ts)`, `path`, `scan_id`, `ts` |
| `exclusions` | `id`, `area` (NULL = global), `pattern`, `kind` (`path` or `glob`), `reason`, `created_by`, `created_at`, `expires_at`, `active` | unique `(area, pattern)`, `active` |
| `api_tokens` | `id`, `name`, `prefix`, `token_hash` (`pbkdf2$iter$salt$hex`), `scopes` (comma list), `created_by`, `created_at`, `expires_at`, `revoked_at`, `last_used_at`, `last_used_ip` | `prefix` |
| `area_state` | `area` PK, `path`, `paused`, `paused_reason`, `paused_by`, `paused_at`, `disabled`, `disabled_reason`, `disabled_by`, `disabled_at`, `active_tiers` JSON, `last_scan_at`, `last_scan_id`, `updated_at` | |
| `notification_log` | `id`, `ts`, `channel`, `severity`, `area`, `event_key`, `title`, `body`, `outcome` (`sent`, `failed`, `suppressed_dup`, `suppressed_rate`, `disabled`), `error`, `detail` | `ts`, `(event_key, ts)`, `(channel, ts)` |
| `meta` | `key`, `value` | |

Event types (`EVENT_TYPES`): `scan_start`, `scan_end`, `scan_abort`,
`tier_activate`, `tier_deactivate`, `queue_add`, `queue_remove`,
`action_delete`, `action_compress`, `action_failed`, `policy_insufficient`,
`space_not_reclaimed`, `exclusion_add`, `exclusion_remove`, `area_pause`,
`area_resume`, `area_disable`, `area_enable`, `notification_sent`,
`token_create`, `token_revoke`, `maintenance`.

History path filters containing `*` or `?` are translated to SQL `LIKE` with
`%`, `_` and `\` escaped so the same query works on SQLite and PostgreSQL;
plain paths use equality. `history_log_queue_adds` controls how chatty
`queue_add` is (`all`, `new_only` against the previous scan's queue, `none`),
capped at 20 000 rows per pass. Retention pruning runs once a day from the
scheduler (`history_retention_days`, 0 keeps everything); the notification
log has a `prune_days` method but is not pruned automatically in this
version.

## 10. Notification routing and severity

`Notifier.emit(severity, title, message, key, area, meta)` enqueues an
`Event`; the worker thread offers it to each channel:

1. channel disabled -> `disabled`;
2. `severity_rank(event) < severity_rank(channel.min_severity)` ->
   `below_min_severity`;
3. `RateLimiter.allow(f"{channel}|{key}", severity, now)`: the same key within
   `rate_limit_seconds` is `suppressed_dup` unless the severity is higher than
   the last one sent; `max_per_hour` (0 = unlimited) yields `suppressed_rate`;
4. `send()`; success is `sent`, an exception is `failed` with the error.

Every non-`disabled` outcome is written to `notification_log`; when at least
one channel sent, a `notification_sent` history event records the per-channel
outcomes. Keys are `"<area>:<kind>"` (`tier:warning`, `insufficient:full`,
`stalled`, `fts_unavailable`, `missing`, `usage_unavailable`, `scan_crash`,
`compressor`, `stop:<tier>`, `tier_clear:<tier>`).

| Source event | Severity |
|---|---|
| `tier_activate` warning / critical / full | `warning` / `error` / `critical` (`TIER_SEVERITY`) |
| `policy_insufficient` | `error`; `critical` for the `full` tier |
| `space_not_reclaimed`, FTS unavailable, area missing, usage unavailable, scan crash, compressor unavailable | `error` |
| stop point reached, tier cleared | `info` |

Channel-specific mapping: DAQ messages `error_level` is `error` for
critical/error, `warning`, `normal` for info, `debug` for debug; the message
is `[file-reaper <label>] <title>: <message>` with `subsystem` and
`event_category` from config. Slack colours by severity. Email subject is
`[SEVERITY] [file-reaper <label>] <title>`. Big Red Box `system_id` defaults
to `FILE-REAPER-<LABEL>`. The `mu2e_notify` channel passes `instance` and
`area` in `meta`.

## 11. API authentication model

Two independent mechanisms, resolved once per request into a `Principal`
(`kind`, `name`, `scopes`, `actor`):

- **Bearer tokens** (`Authorization: Bearer rpr_...`). Tokens are `rpr_` plus
  `secrets.token_urlsafe(32)` (256 bits). The database stores a PBKDF2-SHA256
  hash (20 000 iterations, per-token salt, `pbkdf2$iter$salt$hex`) and the
  first 12 characters as an indexed prefix; a presented token is looked up by
  prefix and verified with `hmac.compare_digest`, then cached for 300 s so the
  PBKDF2 cost is paid once per token per window. Scopes `read < operate <
  admin`; a higher scope satisfies a lower requirement. Revocation
  invalidates the cache entry immediately; expiry is checked on every
  lookup. `actor` is `token:<name>`.
- **Admin secret** (`reaper.admin_token`). Presented as `X-Admin-Token` on
  any request (how the CLI mints the first token), or exchanged at `/login`
  for a signed session cookie (`HttpOnly`, `SameSite=Lax`); session-based
  mutating requests must also carry the CSRF token embedded in the page
  (`X-CSRF-Token` header or `csrf_token` form field). `actor` is `ui:admin`.
  When no secret is configured a random one is generated at start and logged
  once; it is not persisted, so it changes on every restart until a permanent
  one is set.

`require_scope(scope, allow_open_read)` denies anonymous callers with 401,
CSRF failures and insufficient scope with 403. With `api.open_read` true
(default) `GET` endpoints marked `allow_open_read` skip authentication
entirely. Endpoint scopes: `read` for every `GET` except tokens; `operate`
for pause/resume/rescan, exclusions and dry-run; `admin` for enable/disable,
tokens and notification tests. Every mutating call records a history event
with the actor.

Flask's `SECRET_KEY` comes from `MU2EDAQ_FILE_REAPER_SECRET_KEY` or is random
per process (sessions do not survive a restart unless it is set).

## 12. Discovery record

```
Responder(name = discovery.name or "File Reaper (<label>)",
          app  = discovery.app  ("file-reaper"),
          port = web_port, scheme = "http", version = __version__,
          meta = {"instance": label, "api_port": str(api_port or web_port),
                  "api_path": "/api/v1", "areas": str(len(areas)),
                  "dry_run": "true" | "false"})
```

Started after the sockets are bound, stopped in the shutdown path, wrapped in
`try/except` so a missing or failing `mu2edaq_discovery` never blocks
startup. `mu2edaq-reaper instances` filters a discovery scan on
`app=file-reaper` and reads `meta.instance` to resolve `--instance <label>`
to a URL.

## 13. Testing strategy

The pure modules carry the bulk of the tests because they contain every
decision: `watermarks` (truth tables at plus/minus epsilon of trigger and
stop, clamps, transitions, `bytes_to_stop`, `simulate_after`), `eligibility`
(one test per `REJECT_REASONS` entry, LRU versus Age keys, glob precedence,
FTS gate with and without suffix), `policies` (ordering, tie-break by path),
`units`, `auth`, `ratelimit`, `exclusions`.

I/O modules are tested against `tmp_path` trees built with `os.utime`-set
timestamps, symlinks, hardlinks and temp names: `scanner` (no symlink
following, same-device rule, sibling capture), `actions` (every refusal path,
atomic compress leaving no temp on abort, preserved timestamps and mode,
`dest_exists`, ratio guard, dry run touching nothing), `fts_gate` (built from
the FTS DDL; missing and locked database fail closed).

The orchestrator is tested with everything injected through `Deps`: a
`FakeDisk` whose free space grows as bytes are freed, a fake clock, a
recording notifier and history. Scenarios: single tier to low water,
hysteresis across scans and restarts, three-pass cascade, exhaustion
fall-through with `policy_insufficient`, pause and exclusion mid-queue,
shutdown mid-pass, stall detection, `AREA_STATE_KEYS` parity between success
and failure snapshots. `scheduler` tests cover tick, request_scan, pause and
disable state restore. `db` tests cover the WAL pragma, four concurrent
writers, history query filters and pruning. `notify` tests cover fan-out,
`min_severity`, dedup and escalation, and the DAQ and Big Red Box payload
shapes. `web` tests use the Flask test client over every page and every API
route including 401/403 paths and the token lifecycle; `cli` tests drive the
client and token cache (permission refusal) against a test server.
`test_scripts` runs the start and stop scripts against real processes
(stale pid naming a live stranger is not killed); `test_packaging` checks man
page `.TH` versions against `__version__`, the CHANGELOG heading and that nav
endpoints exist.

Manual end-to-end uses `config/mu2edaq-file-reaper-test.yaml`,
`tools/make_demo_tree.py` and the `reaper.fake_usage_file` hook to walk
warning -> critical -> full without filling a disk, first with `--dry-run`.

## 14. Open questions and future work

- **Confirm the four defaults** (percent-used thresholds, default policies,
  optional FTS gate, admin-token UI auth). All are configuration or one-line
  changes.
- **SSO for the UI** (Fermilab SSO, Google) layered on the admin session;
  the API token model would not change.
- **Schema migrations** (Alembic) once a column changes; `create_all` only
  adds tables.
- **Remote areas** over SSH were deliberately excluded; if ever needed the
  identity re-verification would have to run on the remote side.
- **Notification log pruning** from the scheduler (the repository method
  exists).
- **Daemon-mode file logging**: in `--daemon` mode stdout/stderr are
  redirected to `log_file` by `daemonize()` and the rotating handler is not
  installed, so `log_max_bytes`/`log_backup_count` apply only to foreground
  runs with `log_file` set; worth unifying.
- **Usage sparkline source**: `/areas/<name>` derives usage history from
  `scan_end` rows; a dedicated samples table would allow finer resolution.
- **Owner-based accounting** (bytes freed per uid) is available in
  `history_events.detail.owner` but not aggregated anywhere yet.
