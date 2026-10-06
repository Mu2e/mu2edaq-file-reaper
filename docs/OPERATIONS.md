---
title: mu2edaq-file-reaper operations
group: data-movement
tier: A
audience: expert
summary: >
  Policy-driven disk cleanup daemon that measures configured DAQ disk areas, alarms at
  warning/critical/full tiers, and compresses or deletes files in LRU or age order (raw
  data gated on mu2edaq-fts transfer status) down to a low-water mark, with an audit
  history, web UI, REST API and CLI
---

# mu2edaq-file-reaper operations

Day-2 operation of a reaper that is already installed. Installation, CMake,
control-room registration, upgrade and removal are in [`INSTALL.md`](INSTALL.md);
start/stop script behaviour is in the README section "Start and stop" and
`mu2edaq-file-reaper-start(1)`. The examples assume the checkout is the working
directory, the web port is 5004 and the CLI has a token cached
(`mu2edaq-reaper token create ops --scopes read,operate,admin --save`, see the
README "First run").

This daemon deletes files. Every procedure below is written so that the
safe action comes first.

## 1. Going live on a node

1. Validate the node config. Exit status 1 means an area was dropped.

   ```bash
   source venv/bin/activate
   mu2edaq-file-reaper -c config/mu2e-file-reaper-dl-01.yaml --check-config
   ```

2. Confirm nothing turns dry run off or fakes the disk:

   ```bash
   grep -n 'dry_run\|fake_usage_file' config/mu2e-file-reaper-dl-01.yaml
   env | grep '^MU2EDAQ_FILE_REAPER_DRY_RUN'      # must be absent, or 1/true
   ```

   An exported but **empty** `MU2EDAQ_FILE_REAPER_DRY_RUN=` counts as false and
   overrides a YAML `dry_run: true` (`settings.py:49`). `fake_usage_file` must
   never appear in a production config; the daemon accepts it with one log
   line (FR-016 a).

3. Start with an explicit config. A bare `./start-mu2edaq-file-reaper.sh`
   loads `config/mu2edaq-file-reaper.yaml`, which is **live** against
   `/data/raw` and `/daqlogs` (FR-008).

   ```bash
   ./start-mu2edaq-file-reaper.sh -c config/mu2e-file-reaper-dl-01.yaml
   curl -s http://localhost:5004/api/v1/health | python3 -m json.tool   # "dry_run": true
   ```

4. For each `fts_db`-gated area, check the pairing with mu2edaq-fts before
   trusting the gate (INTERFACES.md section 3.2):
   - the area path is exactly one fts instance's `source_directory`, and that
     `source_directory` is absolute; only files directly in it can ever be
     eligible (FR-007);
   - fts runs with `checksum.enabled: true`;
   - fts `include_patterns` does not match `*.gz` (or the area's other
     compression suffixes) or `.reaper-tmp-*` if the area has a compress tier
     (FR-016 i).

5. Watch one day of plans, then read what a live pass would have done:

   ```bash
   mu2edaq-reaper plan data-raw                 # synchronous dry-run scan, top of the queue
   mu2edaq-reaper history --type action_delete,action_compress --since 24h
   ```

6. Before the first live pass on a gated area, exclude every file whose
   transfer is unverified or failed verification. The gate accepts these
   (FR-002):

   ```bash
   sqlite3 -readonly /home/mu2edaq/mu2edaq-main/mu2edaq-fts/fts_status.db \
     "SELECT filepath FROM files WHERE status='COMPLETED'
        AND (checksum_verified=0 OR checksum_match IS NOT 1);"
   mu2edaq-reaper exclude add /data/raw/run_001234.dat --area data-raw \
     --reason "fts checksum mismatch"
   ```

7. Set `dry_run: false` in the node YAML and restart with the same command as
   step 3.

## 2. Rules that keep raw data safe

- **Never re-stage a file under a name fts has already shipped.** fts will
  not transfer it again, and the reaper treats it as shipped (FR-001). Use a
  new name, or add an exclusion first.
- **Exclusions:** use an absolute *real* path (no symlinked mount point), a
  directory, or a glob containing `*`, `?` or `[`. `--kind regex` and
  `--kind prefix` are stored and listed but do not protect anything (FR-004).
  Confirm with `mu2edaq-reaper plan <area>` that the file is gone from the
  queue.
- An exclusion's expiry takes effect at the next exclusion add or remove or
  daemon restart (`api_v1.py:284,299`, `cli.py:212`); until then the expired
  rule keeps protecting.
- Do not nest areas or bind-mount another directory into an area on the same
  device: the outer area's policy and gate then apply to the inner files
  (FR-006).
- One daemon per checkout. The start script replaces a running copy; the stop
  script also signals any same-user Python process whose arguments name the
  package and whose working directory is the checkout (FR-015).

## 3. Stopping deletion now

In order of scope:

| Scope | Command | Effect |
|---|---|---|
| one file or directory | `mu2edaq-reaper exclude add <realpath or glob> --area <area>` | re-checked before every action (`reaper.py:608-611`) |
| one area | `mu2edaq-reaper pause <area> --reason "..."` | interrupt at the next file or 1 MiB compression chunk; scans and alarms continue; persisted across restarts |
| one area, harder | `mu2edaq-reaper disable <area>` (admin) | no scans at all; the area fills silently except for the diskwatcher |
| whole node, keep monitoring | restart with `--dry-run` appended to the start command | plans and alarms, no file is touched |
| whole node | `./stop-mu2edaq-file-reaper.sh` | `SIGTERM`, current file finishes, `SIGKILL` after `CRS_STOP_TIMEOUT` (30 s) |

Without the CLI, the same calls over HTTP:

```bash
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"reason":"debugging run 1234"}' \
     http://localhost:5004/api/v1/areas/data-raw/pause
```

## 4. Alarm response

Titles are prefixed `[file-reaper <instance label>]`; `<area>` below is the
area's `label`. Severities and routing: `DESIGN.md` section 10. Source of every
title: the `_notify` calls in `reaper.py` (lines 311 to 663).

| Alarm (title) | Meaning | Action |
|---|---|---|
| `<area>: WARNING` (or `CRITICAL`, `FULL`) followed by `n% used` | tier activated; its policy is running | look at `/queues`; nothing to do if usage falls |
| `<area>: <policy> could not clear <tier>` | queue exhausted above the threshold (`policy_insufficient`) | read the rejection counts in the message. `fts_not_complete` dominant: fts is behind or the pairing is wrong (section 1 step 4). `too_young`: everything is newer than `min_age`. Free space by hand or move data; do not lower `min_age` on a gated area without checking fts |
| `<area>: FTS gate unavailable` | fts database unreadable; nothing in the area is eligible | check the path, permissions and that fts is running. After a hard kill of fts a hot WAL blocks `mode=ro` readers until fts (any writer) opens the database once |
| `Cannot measure <area>` | `statvfs` failed; nothing done | check the mount |
| `Area <area> is missing` | the area path is not a directory | check the mount; the area resumes by itself |
| `<area>: space not reclaimed` | freed bytes did not show up in used space; pass stopped | deleted files held open: `lsof +L1` on the node |
| `<area>: compression unavailable` | the configured compressor cannot be loaded | install it (`./bootstrap.sh --extras` for zstd) or change the algorithm |
| `Scan of <area> crashed` | exception in the engine; scheduler keeps running | read the traceback in the log, then file an issue |

A failed notification is recorded on `/notifications`, not raised. The Big Red
Box channel fails on every send if `daq_alert` is installed (FR-011), and the
phone channel fails when the notification server enforces tokens and
`MU2EDAQ_NOTIFY_TOKEN` was put in `.env` instead of the real environment
(FR-012).

## 5. Audit: what did it do

```bash
mu2edaq-reaper history --type action_delete --since 24h --csv deleted.csv
mu2edaq-reaper history --area data-raw --type policy_insufficient,space_not_reclaimed --since 7d
curl -s 'http://localhost:5004/api/v1/history?type=action_delete&since=24h&format=csv'
```

Every action, skip (`queue_remove` with a reason such as `changed` or
`vanished`), pause, exclusion and token change is a row in `history_events`,
kept `database.history_retention_days` (365) days. The history is the
authoritative record; the log is not (next section).

## 6. Logs, state and secrets

| Item | Where | Caveat |
|---|---|---|
| log | `reaper.log_file` (dl-01: `logs/mu2e-file-reaper-dl-01.log`) | in daemon mode only WARNING and above, no timestamps, no rotation (FR-009) |
| database | `data/file-reaper.db` (+ `-wal`, `-shm`) | history, exclusions, tokens, pause/disable and active-tier state; back up with `sqlite3 data/file-reaper.db ".backup <file>"` |
| pid | `file-reaper.pid` | removed at clean exit |
| admin secret | `MU2EDAQ_FILE_REAPER_ADMIN_TOKEN` in `config/.env`, mode 0600 | if unset, a new one is generated and written to the log at every start (FR-010) |
| YAML | `config/*.yaml` | readable by anyone who can reach port 5004 via `/config` (FR-003): keep secrets out of it |

## 7. Coexistence with mu2edaq-fts

Both daemons delete files in the same directories with no lock between
them. fts's cleaner removes what it shipped **and verified**; the reaper
removes older eligible files under space pressure using fts's `status` column
as a veto. Races between them end as a `vanished` skip on the reaper side or a
`DELETED` row with no file on the fts side; both are benign. The contract, its
failure modes and the cases where the reaper's veto is weaker than fts's own
rule are in [`INTERFACES.md`](INTERFACES.md) section 3 and in
`mu2edaq-fts/docs/INTERFACES.md`.

Open review findings: [`ISSUES-PENDING.md`](ISSUES-PENDING.md).
