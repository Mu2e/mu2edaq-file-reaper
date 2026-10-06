# Staged issues: mu2edaq-file-reaper

Findings from the September 2026 DAQ mega-review (`mu2e-sept-mega-review`),
reviewed 2026-10-06 against `b4524a1`.

**Why these are staged rather than filed.** Per CONTRACT section 9.1 no GitHub
write was attempted. Each block below is in the section 6 issue format and is
parsed by `docs/mega-review/tools/stage_issues.py` into the filing script.
Route: `Mu2e/mu2edaq-file-reaper` (public, issues enabled).

**Dedup.** `gh issue list -R Mu2e/mu2edaq-file-reaper --state all` returned
**0 issues** on 2026-10-06, so there is nothing to match by fingerprint or by
title tokens. Three findings are the reaper side of defects already staged
against `Mu2e/mu2edaq-fts` (FTS-001 `b36928f6`, FTS-002 `b6517e10`, FTS-004
`a9628446`). They are filed separately on purpose: the fts fix and the reaper
fix are independent changes in different repositories, and either one alone
closes only half of the hazard.

**Fingerprints** are `sha1("Mu2e/mu2edaq-file-reaper:<path>:<rule_id>")[:8]`
with no line number. **Labels:** `mega-review` plus the category shown.

16 entries: 4 x S2, 9 x S3, 3 x S4 (FR-016 is the rollup of minor items that
would otherwise exceed the 15-issue cap).

| ID | Sev | Category | rule_id | Fingerprint | Title |
|---|---|---|---|---|---|
| FR-001 | S2 | bug | `fts-gate-path-only-identity` | `87e72aad` | [fts-gate] eligibility keys on path alone, so a new file at a previously shipped path is deletable |
| FR-002 | S2 | bug | `fts-gate-ignores-checksum` | `1883f984` | [fts-gate] COMPLETED rows whose remote checksum mismatched or was never compared are deletable |
| FR-003 | S2 | bug | `unauth-raw-config-secrets` | `cbd85439` | [web] /config is unauthenticated and prints the raw YAML, undoing secret redaction |
| FR-004 | S2 | bug | `exclusion-silent-nonmatch` | `6d69c110` | [exclusions] regex-kind and non-canonical-path exclusions silently protect nothing |
| FR-005 | S3 | bug | `system-root-exact-match` | `2a52d439` | [config] system-root refusal is exact-match only; /usr/lib, /var/lib or /home/<user> are accepted as areas |
| FR-006 | S3 | bug | `area-overlap-unchecked` | `53c93e40` | [config] nested areas and same-device bind mounts let one area's policy reach another area's files |
| FR-007 | S3 | bug | `fts-gate-unreachable-area` | `925f4d16` | [config] dl-01 gates two areas on one FTS database, but FTS records only the top level of one directory |
| FR-008 | S3 | bug | `reference-config-live` | `22c4bb2f` | [config] the start script's default config runs live (dry_run: false) against production paths |
| FR-009 | S3 | bug | `daemon-mode-no-log-handler` | `de9732ec` | [logging] daemon mode installs no log handler: INFO dropped, no timestamps, no rotation |
| FR-010 | S3 | bug | `bootstrap-token-in-log` | `2d27b2ed` | [auth] the bootstrap admin token is written to a world-readable log on every start |
| FR-011 | S3 | bug | `bigredbox-daq-alert-kwarg` | `b84df732` | [notify] Big Red Box channel fails on every send whenever daq_alert is installed |
| FR-012 | S3 | bug | `dotenv-not-exported` | `841022a1` | [config] MU2EDAQ_NOTIFY_TOKEN in .env never reaches the phone-notification publisher |
| FR-013 | S3 | enhancement | `no-ci` | `35047b31` | [ci] 199 tests and no CI workflow |
| FR-014 | S4 | bug | `stale-usage-keeps-acting` | `a8e7cfe7` | [engine] a failed mid-pass usage measurement reuses the stale sample and keeps acting |
| FR-015 | S4 | bug | `proc-matcher-too-broad` | `2089f155` | [scripts] running-copy matcher signals any same-user process naming the package and python |
| FR-016 | S4 | documentation | `minor-rollup` | `2e07a8f0` | [hygiene] minor findings from the 2026-10 review (rollup) |

---

## FR-001

**Title:** `[fts-gate] eligibility keys on path alone, so a new file at a previously shipped path is deletable`

**Severity:** S2 — production breakage or silent wrong behaviour; workaround exists
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/fts_gate.py:56`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The FTS gate reduces every `COMPLETED`/`DELETED` row of the mu2edaq-fts
`files` table to a path string. Nothing that identifies *which bytes* FTS
shipped (`file_size`, `detected_at`, `transfer_start`) is read or compared with
the file now on disk. mu2edaq-fts never re-ships a path it already holds as
`COMPLETED` or `DELETED` (FTS-002), so a new file appearing at such a path is
never transferred **and** is deletable by the reaper once it passes the tier's
`min_age`.

## Evidence

```
$ sed -n '55,57p' src/mu2edaq_file_reaper/fts_gate.py
        cur = conn.execute(
            f"SELECT filepath, compressed_path FROM files WHERE status IN ({placeholders})",
            tuple(sorted(ELIGIBLE_STATUSES)))

$ sed -n '33,34p' src/mu2edaq_file_reaper/fts_gate.py      # FtsSnapshot.allows()
        if path in self.paths:
            return True

$ sed -n '97,103p' src/mu2edaq_file_reaper/eligibility.py
    mtime = info.mtime_ns / 1e9
    if ctx.now - mtime < ctx.settle_seconds:
        return Verdict(False, "settling")
    if ctx.now - ordering_key(info, ctx.policy) < ctx.min_age:
        return Verdict(False, "too_young")
    if ctx.fts is not None and not ctx.fts.allows(info.path, ctx.compressed_suffixes):
        return Verdict(False, "fts_not_complete")
```

The fts side (read-only reference, `../mu2edaq-fts/fts/poller.py:124-128`,
`:149-150`): a tracked path whose row is `COMPLETED` is skipped, and every other
non-`ERROR` status, `DELETED` included, falls through to
`# Already tracked and in-progress — skip` / `continue`.

- The action layer's re-verification (`actions.py:136-145`) compares the file
  against the reaper's own scan-time `stat`, not against anything FTS recorded,
  so it cannot detect that the bytes are not the shipped bytes.
- `FileInfo.ctime_ns` is collected (`scanner.py:96`) but no rule reads it.
- `settle_seconds` and `min_age` are both on mtime (Age policies) or
  `max(atime, mtime)` (LRU) (`policies.py:24-28`). An mtime-preserving copy
  (`cp -p`, `rsync -a`, `tar x`) satisfies both immediately; a fresh write is
  only delayed by `min_age` (1 d for `full` by default; in the dl-01 config
  2 d for `/data/raw` and 1 d for `/data/staging`).
- A second path to the same outcome: the snapshot stores `realpath()` of each
  recorded path as resolved *when the reaper loads it* (`fts_gate.py:66`). If
  FTS's `source_directory` is a symlink that is later repointed, every recorded
  path aliases a file in the new target.

## Why it matters

Any producer that reuses a filename (run-number reuse, a recovered run
rewritten in place, an operator re-staging a file with `rsync -a` after an
offline-side loss) produces a file FTS will never ship and the reaper will
delete as soon as its tier activates. That is loss of unreplicated raw data
with no alarm on either side. It requires a path collision, which is why this
is S2 rather than S1, consistent with FTS-002. The re-staging case is the
uncomfortable one: it is exactly the recovery action an expert takes after a
transfer problem.

## Suggested fix

Defence in depth on the reaper side, independent of the fts fix:

1. Select `file_size` and `detected_at` (guard with `PRAGMA table_info(files)`
   and fail closed if absent) and keep a `path -> (size, detected_at)` map.
2. For an exact `filepath` match, require `st_size == file_size` and
   `st_ctime <= detected_at` (UTC, naive in fts). ctime cannot be preserved by
   `cp -p`/`rsync -a`, so a re-staged file is rejected as `fts_identity_changed`.
   Both tests err closed: ctime also moves on `chmod`, `chown`, `rename` and
   link-count changes, and `file_size` is sampled at detection, so a file fts
   registered while still growing never matches. Those files accumulate and
   surface as `policy_insufficient`, which is the right direction.
3. Keep the compression-suffix path for the reaper's own `X.gz`, but only when
   `X` itself passed check 2 at compression time (record the produced
   `(dev, ino)` in history and accept the suffix match only for that inode).
4. Resolve FTS paths with `realpath` only if the recorded path's directory has
   not changed identity, or drop the realpath alias and require FTS to record
   canonical paths.

If FTS-002 is fixed by re-arming the row to `PENDING`, note that the reaper
reads the snapshot once per pass (`reaper.py:411-420`), so a long pass can still
act on a row that was re-armed during the pass; step 2 covers that window.

## Related

- Mu2e/mu2edaq-fts FTS-002 (staged, fingerprint `b6517e10`), the root cause
- Mu2e/mu2edaq-fts FTS-001 (staged, `b36928f6`)
- `mu2edaq-fts/docs/INTERFACES.md`, section "The SQLite files table as a public interface"

<!-- mega-review-fingerprint: 87e72aad -->

---

## FR-002

**Title:** `[fts-gate] COMPLETED rows whose remote checksum mismatched or was never compared are deletable`

**Severity:** S1 — data loss: deletes the only good copy of raw data in exactly the case the checksum check exists to catch

> Severity raised from S2 by the review orchestrator (2026-10-06) after reading both sides:
> `mu2edaq-fts/fts/poller.py:477-506` records a mismatch as `checksum_match=False`,
> `ready_for_deletion=False` but leaves `status='COMPLETED'`, so fts's own cleaner refuses to
> delete the source; this gate keys on `status` alone (`fts_gate.py:56`), so the reaper deletes it
> once the area crosses a delete tier, and the corrupt remote copy becomes the only one. A rare
> trigger is not a mitigation here: the corrupted transfer is the very event the check exists for.
> Either side can close it: the gate requires `ready_for_deletion = 1` (or `status = 'DELETED'`),
> or fts moves mismatched rows out of `COMPLETED`. Cross-reference FTS-001 in `Mu2e/mu2edaq-fts`.
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/fts_gate.py:16`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The gate accepts any row whose `status` is `COMPLETED`, without reading
`checksum_verified` or `checksum_match`. In mu2edaq-fts, `COMPLETED` is written
when the transfer command exits 0, before any remote comparison, and **stays
`COMPLETED` when the later comparison fails**. A file whose only intact copy is
the local one is therefore reaper-eligible indefinitely.

## Evidence

```
$ sed -n '16p' src/mu2edaq_file_reaper/fts_gate.py
ELIGIBLE_STATUSES = frozenset({"COMPLETED", "DELETED"})

$ sed -n '477,478p;500,506p' ../mu2edaq-fts/fts/poller.py
        match = remote_checksum.lower() == transfer_checksum.lower()
        ready = match
        self.db.set_deletion_status(
            file_id,
            remote_checksum=remote_checksum,
            checksum_verified=True,
            checksum_match=match,
            ready_for_deletion=ready,
        )
```

On a mismatch fts sets `checksum_match = 0`, `ready_for_deletion = 0` and logs
`CHECKSUM_MISMATCH`; it never changes `status`. fts's own cleaner therefore
will not delete the file, but the reaper will.

The project's own documents state the stronger contract the code does not
implement:

```
$ grep -n -o 'Raw data must never[^;]*;' docs/DESIGN.md
42:Raw data must never leave the node before FTS has shipped and verified it;

$ sed -n '93,95p' config/mu2edaq-file-reaper.yaml
  # Raw data on a data-logger node.  The fts_db gate makes a file eligible only
  # when mu2edaq-fts has marked it COMPLETED (transferred and checksummed) or
  # DELETED.  If the FTS database cannot be read, nothing is eligible.
```

## Why it matters

A checksum mismatch is exactly the event the checksum exists to catch. When it
fires, the remote copy is bad and the local file is the only good copy; the
reaper deletes it the next time the area crosses `critical` or `full` and the
file is older than `min_age`. No alarm connects fts's `CHECKSUM_MISMATCH` to
the reaper's queue. The unverified window (`COMPLETED`, `checksum_verified = 0`)
is wider than it looks: with `deletion.delay_seconds` it lasts at least that
long, and with fts `checksum.enabled: false` it lasts forever, in which case
the reaper is deleting on `scp`'s exit status alone. Data loss of the only
intact copy is S1-shaped; it is S2 here because it needs a mismatch or an
unverified transfer **and** disk pressure, and an operator can exclude the file
(subject to FR-004).

On FTS-004 (manual `mark-ready`): it does **not** widen the reaper's exposure.
`mark-ready` only accepts `COMPLETED` rows (`../mu2edaq-fts/fts/web.py:1655-1656`)
and only changes `ready_for_deletion`/checksum columns, none of which the reaper
reads; those rows were already reaper-eligible. After fts's cleaner deletes the
file the row becomes `DELETED` and there is nothing local left to act on.

## Suggested fix

1. Always exclude `checksum_match = 0` rows, whatever else is configured.
2. Add a per-area `fts_require_verified` (default `true` for gated areas)
   meaning `checksum_verified = 1 AND checksum_match = 1`; document that an fts
   deployment with `checksum.enabled: false` must set it `false` knowingly.
3. Probe `PRAGMA table_info(files)` and fail closed, with an alarm, if the
   checksum columns are missing.
4. Correct the comment at `config/mu2edaq-file-reaper.yaml:94` and the README
   "FTS gate" section to say what the gate actually checks.

## Related

- Mu2e/mu2edaq-fts FTS-001 (staged, `b36928f6`) suggested fix item 3
- Mu2e/mu2edaq-fts FTS-004 (staged, `a9628446`)
- `mu2edaq-fts/docs/INTERFACES.md` "Query recipes for consumers"

<!-- mega-review-fingerprint: 1883f984 -->

---

## FR-003

**Title:** `[web] /config is unauthenticated and prints the raw YAML, undoing secret redaction`

**Severity:** S2 — production breakage or silent wrong behaviour; workaround exists
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/web/views.py:142`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The HTML `/config` route has no authentication decorator and renders the
configuration file verbatim. The same view, and `settings.as_dict()`, carefully
redact `admin_token`, notification `token`/`webhook_url`/`password` and the
database URL password; the raw dump next to them shows all of it.

## Evidence

```
$ sed -n '142,150p' src/mu2edaq_file_reaper/web/views.py
@bp.route("/config")
def config():
    settings = get_settings()
    raw_yaml = None
    if settings.config_path:
        try:
            with open(settings.config_path, encoding="utf-8") as fh:
                raw_yaml = fh.read()
        except OSError as exc:

$ sed -n '155p;167p' src/mu2edaq_file_reaper/web/templates/config.html
{% if raw_yaml is not none %}
         style="background:#f8f9fa;border-radius:0 0 .375rem .375rem;font-size:.85em;overflow-x:auto">{{ raw_yaml }}</pre>
```

Redaction that is bypassed: `views.py:153-159`, `settings.py:155-162`. The
route is not covered by `api.open_read`; it stays open when an operator sets
`open_read: false` (contrast `web/auth.py:116-135`, which only the API uses).
Every secret the page would leak is a documented YAML key
(`man 5 mu2edaq-file-reaper.conf`: `reaper.admin_token`,
`notifications.mu2e_notify.token`, `notifications.slack.webhook_url`,
`notifications.email.password`, `database.url`). The shipped test configs
already carry `admin_token: "demo-admin"` in YAML.

## Why it matters

Anyone who can reach port 5004 reads the admin secret if it is in YAML, logs in
as admin, and can then mint tokens, remove exclusions (making operator-protected
files eligible again), enable areas that were disabled in config, and disable
every area so the disk fills. The phone-notification token and SMTP password
leak the same way. The production configs ship `admin_token: ""` and say to use
`.env`, which is the workaround and the reason this is S2, not S1; but the
redaction code shows the authors expected secrets in YAML, and FR-012 pushes
operators toward YAML for the notify token.

## Suggested fix

Render the raw YAML only to an admin session, or pass it through the same
key-based redaction (`admin_token`, `token`, `webhook_url`, `password`, and the
password component of any `*url`) before display. Apply `require_scope("read",
allow_open_read=True)` to the server-rendered pages so `open_read: false`
means what it says.

## Related

- FR-012 (where the notify token can actually be configured)

<!-- mega-review-fingerprint: cbd85439 -->

---

## FR-004

**Title:** `[exclusions] regex-kind and non-canonical-path exclusions silently protect nothing`

**Severity:** S2 — production breakage or silent wrong behaviour; workaround exists
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/exclusions.py:51`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

Manual exclusions are the operator's emergency brake ("do not touch this
run"). Two ways to add one are accepted, stored, listed as active, and match
nothing:

1. `mu2edaq-reaper exclude add --kind regex` (and `POST /api/v1/exclusions`
   with `"kind": "regex"`). The kind is stored verbatim; the matcher never
   interprets regular expressions.
2. A path written with the area's *configured* path when that differs from its
   realpath (symlinked mount point). Candidates carry realpath-based paths, and
   exclusions are compared as strings.

## Evidence

```
$ grep -n '"--kind"' src/mu2edaq_reaper_cli/cli.py
438:    a.add_argument("--kind", choices=["path", "glob", "prefix", "regex"], help="pattern kind (auto)")

$ sed -n '51p;54,56p' src/mu2edaq_file_reaper/exclusions.py
        if row.get("kind") == "glob" or any(ch in pattern for ch in "*?["):
            exact.setdefault(area, set()).add(os.path.normpath(pattern))
            # also match as a directory prefix
            globs.setdefault(area, []).append(os.path.normpath(pattern))
```

`rules_from_rows` + `is_excluded` run on three regex-kind rows (excerpt of the
output; every one of the nine pattern/path pairs printed `excluded=False`):

```
'^/data/raw/keep_.*\\.dat$'      /data/raw/keep_001.dat       excluded=False
'/data/raw/(keep|hold)_run'      /data/raw/keep_run7.dat      excluded=False
'.*\\.calib$'                    /data/raw/x.calib            excluded=False
```

A regex containing `*`, `?` or `[` is applied as an `fnmatch` glob (where `\.`
and `^`/`$` are literals); one without them is applied as an exact path and
directory prefix. `--kind prefix` gets the same treatment, so it works only at
a `/` boundary: `prefix /data/raw/run12` protects `/data/raw/run12/a.dat` but
not `/data/raw/run12_001.dat` (checked with the same two functions). The API does not validate `kind` (`web/api_v1.py:282`), the
model documents `path | glob` only (`db/models.py:93`), and the man page
documents `--kind path|glob` while the parser offers four values.

For case 2: scanned paths are built from `realpath(area)` (`config.py:189`,
`scanner.py:39,88`); `rules_from_rows` stores `normpath(pattern)` without
`realpath` (`exclusions.py:54`), and `is_excluded` compares strings
(`exclusions.py:30-38`).

## Why it matters

The operator sees the exclusion listed as active and stops worrying; the reaper
deletes the files on its next pass. In an ungated area (`/daqlogs`, scratch)
that is unrecoverable. S2 rather than S1 because glob and canonical-path
exclusions work and are the documented forms.

## Suggested fix

Reject unknown kinds with 400 in `exclusions_add` and drop `prefix`/`regex` from
the CLI choices (or implement `regex` with `re.search` and `prefix` explicitly).
Canonicalise exact and prefix patterns with `os.path.realpath` when stored, and
show the canonical form back to the operator.

## Related

- `man/man1/mu2edaq-reaper.1` documented `--kind path|glob`; corrected on the
  review branch to list all four choices and state that only two work.

<!-- mega-review-fingerprint: 6d69c110 -->

---

## FR-005

**Title:** `[config] system-root refusal is exact-match only; /usr/lib, /var/lib or /home/<user> are accepted as areas`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/config.py:190`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

Area validation refuses a path whose realpath *equals* a member of
`SYSTEM_ROOTS`, and refuses depth < 2. Any depth-2 child of a system root
passes: `/usr/lib`, `/usr/local`, `/var/lib`, `/var/log`, `/etc/sysconfig`,
`/boot/efi`, `/home/mu2edaq`.

## Evidence

```
$ sed -n '190,197p' src/mu2edaq_file_reaper/config.py
        if real in SYSTEM_ROOTS or expanded.rstrip("/") in SYSTEM_ROOTS:
            issues.append(f"{prefix}: refusing to manage system root {real}; area dropped")
            continue
        depth = len([p for p in real.split(os.sep) if p])
        if depth < 2 and not allow_shallow_root:
            issues.append(f"{prefix}: {real} is a top-level directory; set "
                          f"reaper.allow_shallow_root to manage it; area dropped")
            continue
```

README "Safety" and the daemon man page describe this as refusing "system
roots", which a reader takes to include their subtrees.

## Why it matters

A typo or copy-paste (`path: /home/mu2edaq` instead of
`/home/mu2edaq/scratch`) gives an LRU-Delete policy the DAQ account's home
directory: the `mu2edaq-main` checkout, venvs, `.ssh`, Kerberos files. The
default excludes only cover temp and SQLite names. No outage today because no
shipped config does this.

## Suggested fix

Refuse any area whose realpath is a system root **or lies under one**, with an
explicit allow-list (`reaper.allowed_roots`, e.g. `/data`, `/scratch`,
`/daqlogs`) or an `allow_system_subtree: true` per area for the rare
legitimate case such as `/var/log/mu2e`.

## Related

- FR-006

<!-- mega-review-fingerprint: 2a52d439 -->

---

## FR-006

**Title:** `[config] nested areas and same-device bind mounts let one area's policy reach another area's files`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/config.py:155`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

`build_areas` checks duplicate *names* but not overlapping *paths*. If one
area contains another on the same filesystem, the outer area's scan enumerates
the inner area's files and applies the outer area's policy and gate to them.
The same happens with a bind mount of another directory from the same device
inside an area: the scanner's filesystem-boundary test is `st_dev`, which a
same-device bind mount does not change, and `realpath` does not see bind mounts.

## Evidence

```
$ grep -n 'overlap\|nested\|commonpath' src/mu2edaq_file_reaper/config.py
$ echo $?
1
$ sed -n '60,62p;85,87p' src/mu2edaq_file_reaper/scanner.py
        if dst.st_dev != root_dev:
            stats.other_fs += 1
            continue
                    if st.st_dev != root_dev:
                        stats.other_fs += 1
                        continue
```

The outer area's protected set contains only its own `fts_db` (`reaper.py:135-147`),
and its `EligibilityContext.fts` is its own gate or `None` (`reaper.py:410-420`).

## Why it matters

The dangerous combination is an ungated outer area around a gated inner one
(for example a catch-all `/data/daq` plus `/data/daq/raw` with `fts_db`): the
outer area deletes untransferred raw data that the inner area would refuse. It
needs a misconfiguration, hence S3, but nothing warns about it, and per-area
locks do not help because the two areas share an `fs_lock` and simply take
turns.

## Suggested fix

In `build_areas`, after resolving real paths, report and drop any area whose
realpath is a prefix of another's (or require an explicit `nested_ok: true`).
In the scanner, record the area roots of all configured areas and stop
descending at another area's root. For bind mounts, compare `st_dev` **and**
check `/proc/self/mountinfo` for mount points under the area on Linux.

## Related

- FR-005

<!-- mega-review-fingerprint: 53c93e40 -->

---

## FR-007

**Title:** `[config] dl-01 gates two areas on one FTS database, but FTS records only the top level of one directory`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/mu2e-file-reaper-dl-01.yaml:41`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The node config gates both `/data/raw` and `/data/staging` on the same
`fts_status.db`. One mu2edaq-fts instance watches one `source_directory` and
records only its top-level entries (`os.scandir`, no recursion). With one FTS
instance, at most one of the two areas can ever have an eligible file, and in
that area only files directly in the watched directory.

## Evidence

```
$ grep -n 'path:\|fts_db:' config/mu2e-file-reaper-dl-01.yaml | sed -n '1,4p'
30:  - path: /data/raw
32:    fts_db: /home/mu2edaq/mu2edaq-main/mu2edaq-fts/fts_status.db
39:  - path: /data/staging
41:    fts_db: /home/mu2edaq/mu2edaq-main/mu2edaq-fts/fts_status.db

$ sed -n '95p;98p' ../mu2edaq-fts/fts/poller.py
        for entry in os.scandir(src):
            filepath = entry.path
```

`source_directory` is a single string (`../mu2edaq-fts/fts/config.py:136,163`),
and no fts config in that repository points under `/data` (all use
`/Users/anorman/transfer`), so the intended pairing cannot be verified from the
repositories.

## Why it matters

The ungatable area fails closed: nothing in it is ever compressed or deleted,
its `critical`/`full` tiers raise `policy_insufficient` alarms with every file
in `fts_not_complete`, and the disk fills. That is an accumulation outage, not
data loss, hence S3. The same applies to any data logger that writes into
per-run subdirectories under the watched directory.

## Suggested fix

Document in `man 5 mu2edaq-file-reaper.conf` that a gated area must be exactly
one FTS instance's `source_directory` and that only depth-1 files are ever
eligible. Either point `/data/staging` at its own FTS instance's database or
remove its `fts_db`. Have `--check-config` warn when two areas share an
`fts_db`.

## Related

- Mu2e/mu2edaq-fts FTS-001 (staged, `b36928f6`)

<!-- mega-review-fingerprint: 925f4d16 -->

---

## FR-008

**Title:** `[config] the start script's default config runs live (dry_run: false) against production paths`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/mu2edaq-file-reaper.yaml:21`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

`start-mu2edaq-file-reaper.sh` with no config argument loads
`config/mu2edaq-file-reaper.yaml`, which sets `dry_run: false` and defines
enabled areas `/data/raw` and `/daqlogs`. The project's own go-live advice
(start in dry run, watch for a day) is in the node config and README, not in the
config a bare start uses.

## Evidence

```
$ grep -n 'CONFIG_FILE:-' start-mu2edaq-file-reaper.sh
51:CONFIG_FILE="${CONFIG_FILE:-./config/mu2edaq-file-reaper.yaml}"
$ grep -n 'dry_run:\|  - path:' config/mu2edaq-file-reaper.yaml
21:  dry_run: false               # true = plan, publish queues and write history, but never modify files
96:  - path: /data/raw
106:  - path: /daqlogs
116:  - path: /scratch/mu2e
$ sed -n '243,244p' README.md
a live pass would consume. Host configs ship with `dry_run: true`; flip it
after watching `/queues` and `/history` for a day.
```

The documented control-room entry (`docs/INSTALL.md:174-185`) passes no config
argument, so `crs-app start file-reaper` would use this file.

## Why it matters

The first start on a DAQ node acts immediately: `/daqlogs` is ungated and its
`critical` tier deletes logs older than 30 days. The dry-run guard exists
(`actions.py:160-162`), so this is not the S1 "no dry-run guard" case; the
problem is that the default path skips the review step the docs prescribe.

## Suggested fix

Ship the reference config with `dry_run: true`, or make the start script refuse
to run the reference config without an explicit `-c`. Correct README line 243
("Host configs") to name the files that actually ship dry.

## Related

- FR-016 item (b) (no `apps.yaml` registration yet)

<!-- mega-review-fingerprint: 22c4bb2f -->

---

## FR-009

**Title:** `[logging] daemon mode installs no log handler: INFO dropped, no timestamps, no rotation`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/logsetup.py:19`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

In daemon mode `main()` passes `log_file=None, daemon=True` to
`configure_logging`, which then adds neither the console handler (skipped when
`daemon`) nor the file handler (no file). The root logger has no handlers, so
Python's `logging.lastResort` handles records: WARNING and above only, written
as the bare message with no timestamp or level, to `sys.stderr`, which
`daemonize()` pointed at the log file. `log_max_bytes`/`log_backup_count` are
never applied. The start script always passes `--daemon`.

## Evidence

```
$ sed -n '186,187p' src/mu2edaq_file_reaper/cli.py
    configure_logging(settings.log_file if not settings.daemon else None, settings.verbose,
                      settings.log_max_bytes, settings.log_backup_count, settings.daemon)
$ sed -n '19,23p' src/mu2edaq_file_reaper/logsetup.py
    if not daemon:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)
    if log_file:
```

Calling `configure_logging(None, False, 10485760, 5, True)` and then logging:

```
WARNING line (bootstrap admin token ...)
root handlers: [] | lastResort: <_StderrHandler <stderr> (WARNING)>
```

The INFO line logged before it does not appear.

`docs/DESIGN.md` section 14 already lists the rotation half of this as future
work, on the assumption that records still reach the file through the
redirected stderr. That holds only for WARNING and above, and only as bare
messages.

## Why it matters

In production the log has no timestamps, so a `scan of <area> crashed`
traceback cannot be correlated with the history database or with fts's log;
startup, discovery and configuration lines are absent; and the file grows
without rotation. The audit history in SQLite is unaffected, which is why this
is S3.

## Suggested fix

In daemon mode add a `RotatingFileHandler` on `settings.log_file` (and keep the
stdout/stderr redirect for stray prints), i.e. pass `settings.log_file`
unconditionally and only suppress the console handler when daemonised. Add a
test that asserts a handler exists after `configure_logging(..., daemon=True)`.

## Related

- FR-010

<!-- mega-review-fingerprint: de9732ec -->

---

## FR-010

**Title:** `[auth] the bootstrap admin token is written to a world-readable log on every start`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/cli.py:231`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

With `admin_token` unset (the shipped default, `admin_token: ""`), every start
generates a fresh admin secret and logs it at WARNING. The log file is created
under umask `022` (`daemon.py:36`, or the user's umask in the foreground), so it
is readable by every local account, and each restart appends another valid
secret.

## Evidence

```
$ sed -n '189,192p;230,233p' src/mu2edaq_file_reaper/cli.py
    bootstrap_token = None
    if not settings.admin_token:
        bootstrap_token = secrets.token_urlsafe(18)
        settings.admin_token = bootstrap_token
    if bootstrap_token:
        log.warning("no reaper.admin_token configured; one-time bootstrap admin token: %s "
                    "(set %sADMIN_TOKEN or reaper.admin_token to make it permanent)",
                    bootstrap_token, ENV_PREFIX)
```

## Why it matters

The admin secret grants every API scope (`web/auth.py:77-80`): token minting,
exclusion removal, enabling config-disabled areas, disabling every area. Any
local user on a node can read the current one. S3 because it needs a local
account and the documented workaround (set the token in `.env`) avoids it.

## Suggested fix

Do not log the secret. Write it once to a `0600` file next to the pid file (or
print it to the controlling terminal only when not daemonised), and log the
file's path. Create the log file with `0640`.

## Related

- FR-003, FR-009

<!-- mega-review-fingerprint: 2d27b2ed -->

---

## FR-011

**Title:** `[notify] Big Red Box channel fails on every send whenever daq_alert is installed`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/notify/channels.py:150`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

`BigRedBoxChannel.send` prefers `daq_alert.send_alert` and calls it with
`ip=`. The library's parameter is `broadcast_ip`. The resulting `TypeError` is
not caught (only `ImportError` is), so when `daq_alert` is importable every
critical alert to the Big Red Box fails; the raw-UDP fallback is only used when
the library is absent.

## Evidence

```
$ sed -n '148,154p' src/mu2edaq_file_reaper/notify/channels.py
        try:
            from daq_alert import send_alert
            send_alert(self.payload(ev)["system_id"], self.payload(ev)["message"],
                       ip=self.cfg.get("ip", "255.255.255.255"), port=int(self.cfg.get("port", 37020)))
            return
        except ImportError:
            pass

$ sed -n '13,18p' ../mu2edaq-bigredbox/libs/python/daq_alert/_sender.py
def send_alert(
    system_id: str,
    message: str,
    broadcast_ip: str = DEFAULT_BROADCAST_IP,
    port: int = DEFAULT_PORT,
) -> None:
```

Binding the reaper's call against that signature:
`TypeError: got an unexpected keyword argument 'ip'`. No test covers the
`daq_alert` path (`tests/test_notify.py` only checks the channel's config).

## Why it matters

The Big Red Box is the control room's critical-alarm path; the reaper sends it
`full`-tier activations and `policy_insufficient` on `full`. The channel is off
by default and `bootstrap.sh` does not install `daq_alert`, so nothing is broken
today, hence S3. The day someone installs the bigredbox library into the
reaper's environment to "do it properly", disk-full alarms stop reaching the
box, visible only as `failed` rows on `/notifications`.

## Suggested fix

Call `send_alert(system_id, message, broadcast_ip=..., port=...)` (positional
also works), and add a test that monkeypatches a fake `daq_alert` module and
asserts the call binds.

## Related

- `mu2edaq-bigredbox/libs/python/daq_alert/_sender.py`

<!-- mega-review-fingerprint: b84df732 -->

---

## FR-012

**Title:** `[config] MU2EDAQ_NOTIFY_TOKEN in .env never reaches the phone-notification publisher`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/settings.py:180`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

`config/.env.example` and `man 5 mu2edaq-file-reaper.conf` (FILES) present
`.env` as the place for `MU2EDAQ_NOTIFY_TOKEN`. The reaper's `.env` loader
returns a dict that is only applied to its own `MU2EDAQ_FILE_REAPER_*` settings;
nothing is exported to `os.environ`. `NotifyPublisher` reads the token from
`os.environ` only.

## Evidence

```
$ sed -n '4p' config/.env.example
# MU2EDAQ_NOTIFY_TOKEN=phone-notification-api-token
$ sed -n '126,128p' src/mu2edaq_file_reaper/cli.py
        dotenv = load_env_file(env_file)
        if dotenv:
            issues += settings.apply_env({**dotenv})
$ grep -n 'os.environ\[\|putenv\|environ.update\|environ.setdefault' -r src/
$ echo $?
1
$ sed -n '106p' ../mu2edaq-phone-notification-system/src/mu2edaq_notify/publisher.py
        self.token = token or os.environ.get(ENV_TOKEN)
```

`apply_env` only looks up keys in `_ENV_MAP` (`settings.py:137-152`); the start
script does not source `.env` either.

## Why it matters

When the notification server enforces `api_tokens`
(`mu2edaq-phone-notification-system/config/notify-server.yaml:44-49`), every
phone notification from the reaper is rejected and shows as `failed`, including
`full`-tier alarms. The operator who followed `.env.example` has no reason to
suspect the token. The workarounds are the real environment or YAML, and YAML
is exposed by FR-003. S3 because the server ships with `api_tokens: []`.

## Suggested fix

Export keys from the `.env` file into `os.environ` when they are not already
set (the documented precedence still holds), or map
`notifications.mu2e_notify.token`/`url` from `MU2EDAQ_NOTIFY_TOKEN`/`URL` in
`apply_env`. Add a test that a `.env` token reaches `Mu2eNotifyChannel`.

## Related

- FR-003

<!-- mega-review-fingerprint: 841022a1 -->

---

## FR-013

**Title:** `[ci] 199 tests and no CI workflow`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** `.github/workflows`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The package has a 199-test pytest suite (all passing on 2026-10-06, macOS,
Python 3.9 venv) and no `.github/workflows`. Nothing runs the suite on push or
on the AlmaLinux 9 / Python 3.9 production target.

## Evidence

```
$ ls .github
ls: .github: No such file or directory
$ venv/bin/python -m pytest --co -q | tail -1
199 tests collected in 0.14s
```

## Why it matters

The package deletes files; its safety properties live in the unit tests
(`test_actions.py`, `test_eligibility.py`, `test_fts_gate.py`). Without CI a
regression in those is caught only if a developer runs the suite locally, and
FR-015 shows the suite can kill its own launcher in some invocations.

## Suggested fix

A workflow on `ubuntu-latest` with Python 3.9 and 3.12: `pip install -r
requirements.txt -r requirements-dev.txt && pip install -e . && pytest -q`,
plus `mandoc -T lint man/man*/*`.

## Related

- FR-015

<!-- mega-review-fingerprint: 35047b31 -->

---

## FR-014

**Title:** `[engine] a failed mid-pass usage measurement reuses the stale sample and keeps acting`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `src/mu2edaq_file_reaper/reaper.py:644`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

The first measurement of a pass refuses to act on `None`, but the
re-measurement after each action falls back to the previous sample with
`or usage`. If `statvfs` starts failing mid-pass, `should_continue` keeps seeing
the pre-failure usage and the loop keeps acting.

## Evidence

```
$ sed -n '643,644p' src/mu2edaq_file_reaper/reaper.py
            elif acted % remeasure_every == 0:
                usage = deps.measure_usage(area.real_path) or usage
$ sed -n '23,26p' src/mu2edaq_file_reaper/usage.py
    """A :class:`UsageSample`, or ``None`` when the filesystem cannot be measured.

    ``None`` is a first-class answer: the engine never acts on an area it
    cannot measure.
```

The stall check (`reaper.py:652-665`) stops the pass after `stall_window`
actions only if the expected drop exceeds 0.1 percentage points; on a large
volume with files smaller than about 0.1 % of capacity per window it never
fires, and the pass consumes the whole queue.

## Why it matters

Over-deletion below the low-water mark, of files that were eligible anyway
(gated, old enough). It contradicts a documented guarantee
(README "Safety": never "act without a usage measurement"). Requires `statvfs`
to fail while `unlink` still works, which is rare, hence S4.

## Suggested fix

Treat `None` from a re-measurement as a pass stop (`usage_unavailable`) with
the same alarm the first measurement raises.

## Related

- README.md "Safety"

<!-- mega-review-fingerprint: a8e7cfe7 -->

---

## FR-015

**Title:** `[scripts] running-copy matcher signals any same-user process naming the package and python`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `lib/file-reaper-proc.sh:21`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

`fr_is_reaper` accepts any process whose argv contains `file_reaper.py`,
`mu2edaq_file_reaper` or `mu2edaq-file-reaper` **and** `python`/`Python`;
`fr_running_pids` adds a cwd-equals-repo test. The start script (which replaces
a running copy by default) and the stop script send `SIGTERM` then `SIGKILL` to
every match.

## Evidence

```
$ sed -n '21,25p' lib/file-reaper-proc.sh
  case "$args" in
    *file_reaper.py*|*mu2edaq_file_reaper*|*mu2edaq-file-reaper*) ;;
    *) return 1 ;;
  esac
  case "$args" in *[Pp]ython*) return 0 ;; esac
```

Observed during this review: running
`cd .../mu2edaq-file-reaper && ./venv/bin/python -m pytest` from a shell killed
that shell (exit 144) when `tests/test_scripts.py::test_running_copy_is_found_and_stopped`
ran the real stop script. The same suite passes (199/199) when launched from
another directory. Any absolute `.../mu2edaq-file-reaper/venv/bin/python`
process with cwd in the checkout matches, and so does the `sh -c "cd <src> &&
<src>/venv/bin/python -m pytest -q"` wrapper that a CMake custom target such as
`pytest` typically generates.

## Why it matters

An operator's interactive `venv/bin/python` session or tmux pane started in the
checkout is killed by a routine restart. Narrow, hence S4.

## Suggested fix

Match the daemon precisely: argv must contain `file_reaper.py` or
`-m mu2edaq_file_reaper` or the `mu2edaq-file-reaper` console script **as an
argument**, not as a path substring; better, rely on the pid file plus a
`/proc/<pid>/cmdline` check of the exact entry point. Make the test run the stop
script against a temporary directory, not the repository root.

## Related

- FR-013

<!-- mega-review-fingerprint: 2089f155 -->

---

## FR-016

**Title:** `[hygiene] minor findings from the 2026-10 review (rollup)`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `docs/ISSUES-PENDING.md`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What

Minor items, each too small for its own issue.

## Evidence

- **(a) `fake_usage_file` guards.** The test hook that replaces real disk usage
  can be set in any YAML; it cannot come from the environment or CLI. Its
  guards are one startup WARNING (`cli.py:216-218`, which in daemon mode lands
  without a timestamp, FR-009) and a badge on `/config` (`config.html:54-55`).
  It is not in `/api/v1/health`, discovery `meta` or `--check-config` output,
  and it is not refused when `dry_run` is false. `CLAUDE.md` says the daemon
  "logs a warning whenever it is active"; it logs once. With it set, deletions
  are driven by fabricated usage (still limited to gate- and age-eligible
  files). Suggest: refuse unless `dry_run` or an explicit `--allow-test-hooks`,
  and expose it in health and discovery.
- **(b) No control-room registration.** `start-mu2edaq-file-reaper.sh:5-6` and
  `man/man1/mu2edaq-file-reaper-start.1` describe launch via
  `crs-app start file-reaper`; neither `mu2edaq-controlroom-setup/config/apps.yaml`
  nor `mu2edaq-desktop/config/apps.yaml` has an entry, and 5004 appears in
  neither. `docs/INSTALL.md:168-185` correctly says to add one. fts's
  `config/laptop_config.yaml:99` also uses 5004 (FTS-007).
- **(c) `config/sandbox.yaml`** tracks a personal area
  `/Users/anorman/novarwgt_python` and its header calls itself
  `mu2edaq-file-reaper-test.yaml`.
- **(d) `RateLimiter._sent_times`** is appended on every send but only pruned
  when `max_per_hour` is non-zero (`notify/ratelimit.py:33-38`); with the
  default `0` it grows for the life of the daemon.
- **(e) SQLite URI not encoded.** `fts_gate.py:48` builds
  `"file:%s?mode=ro" % db_path`. A `#` in the path starts a fragment, dropping
  `mode=ro`, so SQLite opens (and can create) the truncated path read-write;
  `?` or `%` misparse. Use `pathlib.Path(db_path).as_uri() + "?mode=ro"`.
- **(f) Dry run is a shared flag.** `rt.force_dry_run` is set by
  `request_scan`/`run_now` and cleared by whichever job finishes
  (`scheduler.py:106,116,120,208`). Concurrent rescans can turn a requested dry
  run live (a policy-conformant live scan) or a scheduled live scan dry. Pass
  `dry_run` into `scan_area` instead.
- **(g) Temp sweep crosses filesystems.** `sweep_temp_files`
  (`scanner.py:109-135`) has no `st_dev` test, unlike `enumerate_files`, so it
  unlinks `.reaper-tmp-*` files on other filesystems mounted under an area.
- **(h) Config loader can raise.** `config.py:3-7` promises nothing raises, but
  `int(comp.get("level", 6))` and `int(spec.get("max_actions_per_scan", 0) or 0)`
  (`config.py:239,242`) raise `ValueError` on a non-numeric YAML value and stop
  the daemon at startup (fail-safe, but not as documented).
- **(i) Compression inside an fts source directory.** The reaper writes `X.gz`
  and a long-lived `.reaper-tmp-*` next to `X`. mu2edaq-fts's code-default
  `include_patterns` is `["*"]` (`../mu2edaq-fts/fts/config.py:59`), so such an
  fts instance would register and ship the reaper's outputs (duplicate or
  partial transfers). Shipped fts configs include only `*.dat`, `*.raw`,
  `*.root`, `*.jpg`, `*.pdf`. Document the constraint in both packages.
- **(j) Windows actions.** `_HAS_DIR_FD` (`actions.py:34`) is computed and never
  used. On Windows `os.open()` of a directory fails, so `open_parent_verified`
  turns every action into a `changed` skip; the README platform table lists
  Windows 11 without saying the reaper cannot act there. (By inspection; not run
  on Windows.)
- **(k) Windows scripts.** `start-mu2edaq-file-reaper.ps1` and
  `stop-mu2edaq-file-reaper.ps1` have no man page coverage (`bootstrap.ps1` is
  covered by `mu2edaq-file-reaper-bootstrap.1`).
- **(l) CLAUDE.md drift.** It says the `fts_db` fixture is "built from the FTS
  DDL"; `tests/conftest.py:240-241` is a 7-column hand-written subset without
  `file_size`, `detected_at` or any checksum column. It lists
  `src/mu2edaq_reaper_cli/commands/`, which does not exist.
- **(m) Empty environment variable disables dry run.** `_FALSE` contains the
  empty string (`settings.py:49`), so an exported but empty
  `MU2EDAQ_FILE_REAPER_DRY_RUN=` (a blank line in a wrapper or unit file)
  silently overrides a YAML `dry_run: true`. Treat the empty string as "not
  set" for every boolean.
- **(n) Dashboard channel records undelivered alarms as sent.** The ZMQ `PUSH`
  socket does not set `ZMQ_IMMEDIATE` (`notify/channels.py:107-111`). With the
  dashboard down, the first 100 (`SNDHWM`) messages queue in the socket and are
  logged `sent`; they arrive stale when the dashboard returns. Set
  `zmq.IMMEDIATE = 1` so a missing peer is reported as `failed`.

## Why it matters

Individually cosmetic or latent. (a), (e), (i) and (m) are the ones worth
doing first because they sit next to the delete path.

## Suggested fix

As stated per item.

## Related

- FR-008, FR-009

<!-- mega-review-fingerprint: 2e07a8f0 -->

---

## Resolved during the review, not filed

- **Man page EXAMPLES lines silently dropped.** `man/man1/mu2edaq-file-reaper.1:275-276`
  and `man/man1/make_demo_tree.1:62` began with `./start-...`/`./stop-...`, which
  roff parses as an unknown request and discards; the rendered EXAMPLES omitted
  exactly the start/stop commands. Escaped with `\&`. Also removed a `.PP` after
  `.SH` (mandoc WARNING), normalised `EXAMPLE` to `EXAMPLES` and `AUTHORS` to
  `AUTHOR`, added an `AUTHOR` section to the eight pages that lacked one, and
  added the path-only/checksum caveat to the `fts_db` entries in
  `mu2edaq-file-reaper.conf.5` and `mu2edaq-file-reaper.1`. All nine pages now
  pass `mandoc -T lint` (no ERROR/UNSUPP/WARNING) and
  `groff -mandoc -Tutf8 -ww -z` with no output.
- **Man pages that described behaviour the code does not have.**
  `mu2edaq-reaper.1` listed `--kind path|glob` (parser: four choices, two
  implemented; FR-004); `mu2edaq-file-reaper.1` called `--log-file` a rotating
  log in every mode (not with `--daemon`; FR-009); `mu2edaq-file-reaper.conf.5`
  offered `.env` for `MU2EDAQ_NOTIFY_TOKEN` (not applied; FR-012). Corrected to
  describe the code; the defects themselves remain staged.

## Note for the orchestrator: one statement in mu2edaq-fts/docs/INTERFACES.md

"Delete authority boundary" says that when fts's cleaner has already removed a
file, "both sides treat an already-absent file as success (... the reaper
likewise)". The reaper treats it as a **skip** with reason `vanished`
(`actions.py:122-123`), recorded as a `queue_remove` history row, not as a
successful action. The conclusion (benign) is unchanged. The same document
describes `filepath` as an "absolute source path"; it is absolute only if
fts's `source_directory` is, since `fts/config.py:163` applies `expanduser` but
not `abspath`. A relative `source_directory` makes every gated reaper area fail
closed, because the reaper resolves those paths against its own working
directory (`fts_gate.py:64-66`).
