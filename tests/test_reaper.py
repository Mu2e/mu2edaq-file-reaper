"""Orchestrator tests: real pure code, real tmp tree, fake disk, in-memory DB."""

import os
import threading
from collections import Counter

import pytest

from mu2edaq_file_reaper.domain import PassStop
from mu2edaq_file_reaper.reaper import AreaRuntime, scan_area
from mu2edaq_file_reaper.settings import get_settings
from mu2edaq_file_reaper.state import AREA_STATE_KEYS, STORE

MiB = 1024 * 1024


def populate(tree, n=20, size=MiB):
    """n files of *size* bytes, ages 1..n days, alternating root/sub."""
    paths = []
    for i in range(n):
        rel = f"{'sub/' if i % 2 else ''}f{i:02d}.dat"
        paths.append(tree.file(rel, content=os.urandom(size // 2) + b"\0" * (size // 2), age_days=i + 1))
    return paths


def std_area(area_factory, tree, **kw):
    spec = dict(tiers={"warning": {"policy": "LRU-Compress", "min_age": "5d"},
                       "critical": {"policy": "LRU-Delete", "min_age": "10d"},
                       "full": {"policy": "Age-Delete", "min_age": "1d"}})
    spec.update(kw)
    area, issues = area_factory(tree.root, **spec)
    return area


def run(area, deps, **settings_over):
    S = get_settings()
    S.apply(areas=[area], **settings_over)
    rt = AreaRuntime(area=area)
    STORE.init_areas([area])
    return rt, scan_area(area, rt, deps, S)


def events(repos, *types):
    rows, _ = repos["history"].query(limit=10000, newest_first=False, event_types=types or None)
    return rows


def test_idle_area_publishes_full_snapshot(tree, area_factory, deps_factory, fake_disk, repos):
    populate(tree, 3)
    fake_disk.set_used_pct(50)
    area = std_area(area_factory, tree)
    rt, report = run(area, deps_factory())
    assert report.outcome == "ok" and report.acted == 0
    snap = STORE.area(area.name)
    assert set(snap) == AREA_STATE_KEYS
    assert snap["state"] == "GOOD" and snap["active_tiers"] == [] and snap["usage"]["used_pct"] == 50.0
    assert [e["event_type"] for e in events(repos)] == ["scan_start", "scan_end"]


def test_single_tier_runs_to_low_water_and_hysteresis_persists(tree, area_factory, deps_factory, fake_disk, repos, notifier):
    populate(tree, 20)
    fake_disk.set_used_pct(92)                        # critical (90) active; stop at 80
    area = std_area(area_factory, tree, tiers={"critical": {"policy": "LRU-Delete", "min_age": "1d"}})
    deps = deps_factory()
    rt, report = run(area, deps)
    assert report.outcome == "ok" and report.passes == 1
    assert 78.0 < fake_disk.used_pct <= 80.0           # stopped as soon as <= stop
    snap = STORE.area(area.name)
    assert set(snap) == AREA_STATE_KEYS
    # reaching the stop point deactivates the tier on the next evaluation
    assert snap["state"] == "GOOD" and snap["active_tiers"] == []
    deleted = [e["path"] for e in events(repos, "action_delete")]
    assert deleted == sorted(deleted, key=lambda p: -int(os.path.basename(p)[1:3]))   # oldest first
    assert report.acted == len(deleted)
    types = Counter(e["event_type"] for e in events(repos))
    assert types["tier_activate"] == 1 and types["tier_deactivate"] == 1
    assert types["queue_add"] >= types["action_delete"]
    assert any("CRITICAL" in t for t in notifier.titles())
    assert any(k.endswith("stop:critical") for k in notifier.keys())
    # 85 % with no memory of being active: below the 90 trigger -> nothing happens
    fake_disk.set_used_pct(85)
    before = report.acted
    rt2 = AreaRuntime(area=area)
    r2 = scan_area(area, rt2, deps, get_settings())
    assert r2.acted == 0 and rt2.active_tiers == frozenset()
    # 85 % but the tier was active (e.g. restored after a restart): hysteresis keeps acting
    rt3 = AreaRuntime(area=area, active_tiers=frozenset({"critical"}))
    r3 = scan_area(area, rt3, deps, get_settings())
    assert r3.acted > 0 and fake_disk.used_pct <= 80.0


def test_cascade_full_critical_warning_in_one_scan(tree, area_factory, deps_factory, fake_disk, repos):
    populate(tree, 30, size=MiB)
    fake_disk.total = 100 * MiB
    fake_disk.set_used_pct(97)
    area = std_area(area_factory, tree, tiers={
        "warning": {"policy": "LRU-Compress", "min_age": "2d", "low_water": 5},
        "critical": {"policy": "LRU-Delete", "min_age": "15d", "low_water": 5},
        "full": {"policy": "Age-Delete", "min_age": "25d", "low_water": 3}})
    rt, report = run(area, deps_factory(), max_passes_per_scan=3)
    kinds = [(e["event_type"], e["tier"]) for e in events(repos, "action_delete", "action_compress")]
    tiers_in_order = [t for _, t in kinds]
    # full acted first, then critical, then warning; never interleaved backwards
    order = {"full": 0, "critical": 1, "warning": 2}
    assert tiers_in_order == sorted(tiers_in_order, key=order.get)
    assert "full" in tiers_in_order and "critical" in tiers_in_order and "warning" in tiers_in_order
    assert report.passes == 3
    scan_end = events(repos, "scan_end")[-1]
    assert scan_end["detail"]["passes"] == 3


def test_max_passes_one_stops_after_highest_tier(tree, area_factory, deps_factory, fake_disk, repos):
    populate(tree, 30)
    fake_disk.set_used_pct(97)
    area = std_area(area_factory, tree, tiers={
        "critical": {"policy": "LRU-Delete", "min_age": "15d", "low_water": 5},
        "full": {"policy": "Age-Delete", "min_age": "25d", "low_water": 3}})
    rt, report = run(area, deps_factory(), max_passes_per_scan=1)
    assert report.passes == 1
    assert {e["tier"] for e in events(repos, "action_delete")} == {"full"}


def test_exhaustion_falls_through_and_flags_insufficient(tree, area_factory, deps_factory, fake_disk, repos, notifier):
    populate(tree, 6)                                  # ages 1..6 d, 6 MiB total
    fake_disk.total = 100 * MiB
    fake_disk.set_used_pct(97)
    area = std_area(area_factory, tree, tiers={
        "critical": {"policy": "LRU-Delete", "min_age": "4d"},   # only f03..f05 eligible (3 MiB) -> cannot reach 90
        "full": {"policy": "Age-Delete", "min_age": "30d"}})     # nothing eligible
    rt, report = run(area, deps_factory())
    ins = events(repos, "policy_insufficient")
    assert [e["tier"] for e in ins] == ["full", "critical"]
    assert report.acted == 3
    assert any("could not clear" in t for t in notifier.titles())
    sev = {e["meta"].get("tier"): e["severity"] for e in notifier.events if "could not clear" in e["title"]}
    assert sev["full"] == "critical" and sev["critical"] == "error"


def test_paused_publishes_queue_but_never_acts(tree, area_factory, deps_factory, fake_disk, repos):
    paths = populate(tree, 10)
    fake_disk.set_used_pct(92)
    area = std_area(area_factory, tree, tiers={"critical": {"policy": "LRU-Delete", "min_age": "1d"}})
    S = get_settings()
    S.apply(areas=[area])
    rt = AreaRuntime(area=area, paused=True)
    STORE.init_areas([area])
    report = scan_area(area, rt, deps_factory(), S)
    assert report.outcome == "paused" and report.acted == 0
    assert all(os.path.exists(p) for p in paths)
    snap = STORE.area(area.name)
    assert snap["queues"]["delete"]["total"] == 10 and snap["state"] == "CRITICAL" and snap["paused"]
    assert events(repos, "queue_remove")[-1]["reason"] == "paused"


def test_disabled_runtime_skips(tree, area_factory, deps_factory, fake_disk):
    populate(tree, 2)
    area = std_area(area_factory, tree)
    S = get_settings()
    rt = AreaRuntime(area=area, disabled=True)
    assert scan_area(area, rt, deps_factory(), S).reason == "disabled"
    rt2 = AreaRuntime(area=area, busy=True)
    assert scan_area(area, rt2, deps_factory(), S).reason == "busy"


def test_interrupt_mid_queue_stops_at_next_file(tree, area_factory, deps_factory, fake_disk, repos):
    paths = populate(tree, 10)
    fake_disk.set_used_pct(99)
    area = std_area(area_factory, tree, tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    S = get_settings()
    S.apply(areas=[area])
    rt = AreaRuntime(area=area)
    STORE.init_areas([area])
    deps = deps_factory()
    real_delete = deps.delete
    count = {"n": 0}

    def delete_then_pause(info, ctx, settle, hl=False):
        count["n"] += 1
        if count["n"] == 3:
            rt.paused = True
            rt.interrupt.set()
        return real_delete(info, ctx, settle, hl)
    deps.delete = delete_then_pause
    report = scan_area(area, rt, deps, S)
    assert report.outcome == "paused" and report.acted == 3
    assert sum(os.path.exists(p) for p in paths) == 7
    qr = events(repos, "queue_remove")[-1]
    assert qr["reason"] == "paused" and qr["count"] == 7


def test_shutdown_event_aborts(tree, area_factory, deps_factory, fake_disk, repos):
    populate(tree, 5)
    fake_disk.set_used_pct(99)
    area = std_area(area_factory, tree, tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    deps = deps_factory()
    real_delete = deps.delete

    def delete_then_shutdown(info, ctx, settle, hl=False):
        deps.shutdown.set()
        return real_delete(info, ctx, settle, hl)
    deps.delete = delete_then_shutdown
    rt, report = run(area, deps)
    assert report.outcome == "shutdown" and report.acted == 1
    assert any(e["event_type"] == "scan_abort" for e in events(repos))


def test_exclusion_added_mid_queue_is_honoured(tree, area_factory, deps_factory, fake_disk, repos):
    paths = populate(tree, 6)
    fake_disk.set_used_pct(99)
    area = std_area(area_factory, tree, tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    deps = deps_factory()
    real_delete = deps.delete
    target = paths[0]                              # youngest: acted on last

    def delete_and_exclude(info, ctx, settle, hl=False):
        r = real_delete(info, ctx, settle, hl)
        repos["exclusions"].add(target, area=area.name)
        deps.exclusions.reload()
        return r
    deps.delete = delete_and_exclude
    rt, report = run(area, deps)
    assert os.path.exists(target) and report.acted == 5
    assert any(e["reason"] == "excluded" and e["path"] == target for e in events(repos, "queue_remove"))
    entries = STORE.area(area.name)["queues"]["delete"]["entries"]
    assert entries and entries[-1]["path"] == target and entries[-1]["status"] == "excluded"


def test_unmeasurable_usage_never_acts(tree, area_factory, deps_factory, fake_disk, repos, notifier):
    paths = populate(tree, 3)
    area = std_area(area_factory, tree)
    deps = deps_factory(measure_usage=lambda p: None)
    rt, report = run(area, deps)
    assert report.reason == "usage_unavailable" and all(os.path.exists(p) for p in paths)
    snap = STORE.area(area.name)
    assert snap["state"] == "UNKNOWN" and set(snap) == AREA_STATE_KEYS
    assert any("Cannot measure" in t for t in notifier.titles())


def test_missing_area_is_reported(tmp_path, area_factory, deps_factory, notifier):
    root = tmp_path / "gone" / "x"
    root.mkdir(parents=True)
    area, _ = area_factory(str(root))
    root.rmdir()
    rt, report = run(area, deps_factory())
    assert report.reason == "missing" and STORE.area(area.name)["state"] == "MISSING"


def test_stall_detector_fires_when_space_is_not_reclaimed(tree, area_factory, deps_factory, fake_disk, repos, notifier):
    populate(tree, 30, size=64 * 1024)
    fake_disk.total = 4 * MiB
    fake_disk.set_used_pct(99)
    area = std_area(area_factory, tree, tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    from mu2edaq_file_reaper import actions
    deps = deps_factory(delete=lambda info, ctx, settle, hl=False: actions.delete_file(info, ctx, settle, hl))  # disk never learns
    rt, report = run(area, deps, stall_window=5)
    assert report.reason == "stalled"
    assert events(repos, "space_not_reclaimed")
    assert any("not reclaimed" in t for t in notifier.titles())


def test_dry_run_simulates_and_touches_nothing(tree, area_factory, deps_factory, fake_disk, repos):
    paths = populate(tree, 20)
    fake_disk.set_used_pct(92)
    area = std_area(area_factory, tree, tiers={"critical": {"policy": "LRU-Delete", "min_age": "1d"}})
    rt, report = run(area, deps_factory(), dry_run=True)
    assert report.outcome == "ok" and 0 < report.acted < 20
    assert all(os.path.exists(p) for p in paths)
    acts = events(repos, "action_delete")
    assert acts and all(e["outcome"] == "dry_run" for e in acts)
    assert acts[-1]["used_pct_after"] <= 80.0
    assert STORE.area(area.name)["dry_run"] is True


def test_fts_gate_blocks_untransferred_and_fails_closed(tree, area_factory, deps_factory, fake_disk, repos, fts_db, notifier):
    paths = populate(tree, 6)
    fake_disk.set_used_pct(99)
    fts_db["add"](os.path.realpath(paths[5]), "COMPLETED")
    fts_db["add"](os.path.realpath(paths[4]), "PENDING")
    area = std_area(area_factory, tree, fts_db=fts_db["path"],
                    tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    rt, report = run(area, deps_factory())
    assert report.acted == 1 and not os.path.exists(paths[5]) and os.path.exists(paths[4])
    snap = STORE.area(area.name)
    assert snap["reject_counts"].get("fts_not_complete") == 5
    os.unlink(fts_db["path"])
    rt, report = run(area, deps_factory())
    assert report.acted == 0 and any("FTS" in w for w in STORE.area(area.name)["warnings"])
    assert any("FTS gate unavailable" in t for t in notifier.titles())


def test_queue_add_history_modes(tree, area_factory, deps_factory, fake_disk, repos):
    populate(tree, 5)
    fake_disk.set_used_pct(92)
    area = std_area(area_factory, tree, tiers={"critical": {"policy": "LRU-Delete", "min_age": "1d", "low_water": 0}})
    deps = deps_factory(delete=lambda *a, **k: PassStopStub())   # never acts -> queue stays
    rt, _ = run(area, deps, history_log_queue_adds="new_only")
    first = len(events(repos, "queue_add"))
    scan_area(area, rt, deps, get_settings())
    assert len(events(repos, "queue_add")) == first            # nothing new the second time


class PassStopStub:
    outcome = "skipped"
    reason = "test"
    error = None
    bytes_freed = 0
    duration_s = 0.0
    dest_path = None


def test_prune_empty_dirs_after_deletion(tree, area_factory, deps_factory, fake_disk):
    tree.file("sub/only.dat", age_days=5, content=b"x" * MiB)
    fake_disk.set_used_pct(99)
    area = std_area(area_factory, tree, prune_empty_dirs=True,
                    tiers={"full": {"policy": "Age-Delete", "min_age": "1d", "low_water": 90}})
    rt, report = run(area, deps_factory())
    assert report.acted == 1 and not os.path.exists(tree.path("sub")) and os.path.isdir(tree.root)
