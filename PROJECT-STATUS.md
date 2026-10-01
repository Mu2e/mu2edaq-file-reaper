# mu2edaq-file-reaper project status

Code version 0.1.0, released as repository tag `t00.01.00` (2026-09-17, commit 42aabbc; CHANGELOG `[t00.01.00] - 0.1.0`). Updated 2026-10-01.
Repository: https://github.com/Mu2e/mu2edaq-file-reaper (public).
Live tracker: docs/PROJECT-STATUS.html (published as a Claude artifact).

## Phases

| Phase | Scope | Status | Notes |
|---|---|---|---|
| 1 Scaffold | package layout, pyproject, settings/config/units, daemon, scripts (sh + ps1), CMake, reference/test/dl-01 configs | done | precedence CLI > env > .env > YAML > defaults |
| 2 Database | SQLAlchemy 2 models, SQLite WAL / Postgres URL, history, exclusions, tokens, area_state, notification_log repos | done | glob to LIKE with escaping, retention prune |
| 3 Engine | usage, water marks, eligibility, FTS gate, policies, scanner, atomic actions, orchestrator, scheduler | done | highest tier per pass, cascade up to 3 passes, dry-run simulation |
| 4 Notifications | mu2e notify, DAQ ZMQ, Big Red Box, email, Slack, rate limiter, log | done | failed sends retry on the next event |
| 5 Web | Flask factory, admin session + CSRF, scoped bearer tokens, REST API v1, 16 templates, JS/CSS | done | verified with the test client (see Test matrix) |
| 6 CLI | mu2edaq-reaper: 17 commands, token cache 0600, discovery lookup | done | 27 tests against a live Werkzeug server |
| 7 Docs | README, CHANGELOG, CLAUDE.md, INSTALL, DESIGN, man pages (1/3/5/7), demo tool | done | all man pages carry the code version (test-enforced) |
| 8 Registration | GitHub repo Mu2e/mu2edaq-file-reaper, submodule, testing/common.sh, apps.yaml (5004), mu2edaq-config | in progress | GitHub repo created and pushed 2026-10-01 (main, mu2e-sept-mega-review, t00.01.00); mu2edaq-main submodule, testing manifest, apps.yaml and mu2edaq-config pending |

## Test matrix

Last full run 2026-10-01, macOS arm64, Python 3.12.1: 199 passed, 0 failed (57.7 s).

| Suite | Covers | Status |
|---|---|---|
| test_units, test_watermarks | size/percent/duration parsing; trigger and stop at boundaries; clamps; transitions | passing |
| test_eligibility, test_policies, test_exclusions | one case per rejection reason; LRU vs Age keys; glob precedence; queue order; exclusion rules | passing |
| test_config, test_settings, test_cli_daemon | defaults and overrides; system-root refusal; env coercion; layer precedence; --check-config | passing |
| test_scanner, test_actions | symlinks, other filesystems, permission errors; every refusal path; atomic compress; interrupt; verify failure | passing |
| test_fts_gate | COMPLETED/DELETED only; suffix stripping; unreadable database raises | passing |
| test_reaper | idle, single tier to stop, hysteresis, cascade, exhaustion, pause, interrupt, shutdown, exclusion mid-queue, unmeasurable, missing, stall, dry run, FTS gate, history modes, prune | passing |
| test_scheduler | restore state, pause/resume/disable/enable, run_now, tick/stop, no overlapping scans | passing |
| test_db, test_auth, test_ratelimit, test_notify | WAL pragma, four concurrent writers, history filters and prune, token lifecycle; hashing and scopes; dedup; fan-out and payload shapes | passing |
| test_cli | token cache permissions, config precedence, end-to-end CLI against a live server | passing |
| test_web | every page and API route, 401/403/CSRF paths, token lifecycle | passing (9) |
| test_scripts, test_packaging | start/stop scripts against real processes; man page versions; changelog heading; nav endpoints; 3.9 syntax | passing (5 + 8) |

## Platform compatibility

| Platform | Python | Bootstrap | pytest | Daemon mode | Notes |
|---|---|---|---|---|---|
| macOS arm64 (development) | 3.12.1 | ok | 199 / 199 (2026-10-01) | fork | primary development host |
| AlmaLinux 9 (mu2e-mgr-01, mu2e-dl-01) | 3.9.21 | pending | pending | fork | production target; Optional[X] typing enforced by test |
| Windows 11 | 3.9+ | pending | pending | foreground only | bootstrap.ps1, start .ps1 |

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Threshold semantics | percent used (df convention), trigger = T + high_water, stop = T - low_water | matches the specification's water-mark wording and df -h |
| Cross-tier rule | most severe active tier per pass, re-enumerate, cascade up to 3 passes | queues never stale; compress-then-delete order falls out |
| Default policies | warning LRU-Compress, critical LRU-Delete, full Age-Delete, gzip | stdlib compressor available on AL9 Python 3.9 |
| FTS safety | optional per-area fts_db gate, fails closed | never delete untransferred raw data; non-FTS volumes stay usable |
| Action safety | dir_fd identity re-verification, atomic temp + verify + rename | a plan from the scan is never trusted at action time |
| UI auth | open reads, admin token for writes, scoped bearer tokens on the API | control-room network; SSO can be layered later |
| Storage | SQLAlchemy 2, SQLite WAL default, Postgres via URL | project convention |
| Port and discovery | HTTP 5004, app "file-reaper", label and api_port in meta | 5002 diskwatcher and 5003 fts are taken |

## Open items

- The four design questions (thresholds, default policies, FTS guard, UI auth) were answered with the recommended option because no reply arrived during planning. Each is a configuration or small code change if a different choice is wanted.
- Registration in mu2edaq-main (submodule, testing manifest, apps.yaml, mu2edaq-config) is still to be done. The GitHub repository exists as of 2026-10-01.
- `python3.9` is not installed on the development host, so the AL9 typing check (`python3.9 -W error -c "import mu2edaq_file_reaper"`) has only been covered by the syntax test in test_packaging.
- AlmaLinux 9 and Windows 11 runs of bootstrap and pytest have not been performed yet.
- SSO for the UI, Alembic migrations and remote (SSH) areas are future work.
