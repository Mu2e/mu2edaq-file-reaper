import threading
import time

from mu2edaq_file_reaper.scheduler import Scheduler
from mu2edaq_file_reaper.settings import get_settings
from mu2edaq_file_reaper.state import STORE


def make(area_factory, tree, deps_factory, repos, **over):
    tree.file("a.dat", age_days=3)
    area, _ = area_factory(tree.root)
    S = get_settings()
    S.apply(areas=[area], scan_interval=3600, workers=2, **over)
    return area, Scheduler(deps_factory(), S, history=repos["history"])


def test_restore_state_and_pause_resume_disable_enable(tree, area_factory, deps_factory, repos):
    tree.file("a.dat", age_days=3)
    area, _ = area_factory(tree.root)
    repos["area_state"].set_paused(area.name, True, "op", "maintenance")
    repos["area_state"].save_active_tiers(area.name, ["critical", "bogus"])
    S = get_settings()
    S.apply(areas=[area], scan_interval=3600)
    sched = Scheduler(deps_factory(), S, history=repos["history"])
    rt = sched.runtime(area.name)
    assert rt.paused and rt.paused_by == "op" and rt.active_tiers == {"critical"}
    assert STORE.area(area.name)["paused"] is True
    assert sched.resume(area.name, by="token:cli", reason="done")
    assert not rt.paused and STORE.area(area.name)["paused"] is False
    assert sched.disable(area.name, by="ui:admin", reason="broken disk")
    assert rt.disabled and rt.interrupt.is_set() and STORE.area(area.name)["state"] == "DISABLED"
    assert sched.enable(area.name, by="ui:admin")
    assert not rt.disabled and not rt.interrupt.is_set()
    assert sched.pause(area.name, by="x")
    rows, _ = repos["history"].query(area=area.name, newest_first=False)
    assert [r["event_type"] for r in rows if r["event_type"].startswith("area_")] == \
        ["area_resume", "area_disable", "area_enable", "area_pause"]
    assert rows[0]["actor"] == "token:cli"
    assert not sched.pause("nope")


def test_run_now_populates_state_and_dry_run_flag_resets(tree, area_factory, deps_factory, repos, fake_disk):
    area, sched = make(area_factory, tree, deps_factory, repos)
    fake_disk.set_used_pct(10)
    report = sched.run_now(area.name, dry_run=True)
    assert report.outcome == "ok" and STORE.area(area.name)["dry_run"] is True
    assert sched.runtime(area.name).force_dry_run is False
    assert sched.reports[area.name] is report
    assert sched.run_now("nope") is None


def test_start_tick_and_stop(tree, area_factory, deps_factory, repos, fake_disk):
    area, sched = make(area_factory, tree, deps_factory, repos)
    fake_disk.set_used_pct(10)
    sched.start()
    deadline = time.time() + 5
    while time.time() < deadline and STORE.area(area.name)["scan"]["finished"] is None:
        time.sleep(0.05)
    assert STORE.area(area.name)["scan"]["outcome"] == "ok"
    h = sched.health()
    assert h["alive"] and not h["stalled"] and h["tick_age_s"] is not None
    assert sched.request_scan(area.name)
    sched.stop(grace_s=5)
    assert not sched.health()["alive"]


def test_no_overlapping_scans_and_fs_lock_serialises(tree, area_factory, deps_factory, repos, fake_disk, monkeypatch):
    area, sched = make(area_factory, tree, deps_factory, repos)
    fake_disk.set_used_pct(10)
    active = {"n": 0, "max": 0}
    lock = threading.Lock()
    import mu2edaq_file_reaper.scheduler as sm

    def slow_scan(a, rt, deps, S):
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.2)
        with lock:
            active["n"] -= 1
        from mu2edaq_file_reaper.reaper import ScanReport
        return ScanReport(scan_id="x", outcome="ok")
    real = sm.scan_area
    monkeypatch.setattr(sm, "scan_area", lambda a, rt, deps, S: (rt.try_acquire() and (slow_scan(a, rt, deps, S), rt.release())[0]) or real(a, rt, deps, S))
    sched.start()
    for _ in range(5):
        sched.request_scan(area.name)
        sched.tick()
    time.sleep(0.6)
    sched.stop(grace_s=3)
    assert active["max"] == 1
