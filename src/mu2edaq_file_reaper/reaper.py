"""The scan/act orchestrator — the only place where hysteresis, queues,
history and notifications meet.  See docs/DESIGN.md for the algorithm."""

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from . import actions
from .compressors import Compressor, CompressorError, get_compressor
from .domain import AreaConfig, PassStop, QueueEntry, TierConfig, UsageSample
from .eligibility import EligibilityContext, partition
from .exclusions import ExclusionRegistry, is_excluded
from .formatting import fmt_bytes
from .fts_gate import EMPTY_SNAPSHOT, FtsSnapshot, FtsUnavailable, load_fts_snapshot
from .notify import TIER_SEVERITY
from .policies import POLICIES, Policy, build_queue, diff_new_paths, owner_names
from .scanner import ScanStats, enumerate_files, open_inodes_proc, sweep_temp_files
from .state import (
    AREA_SEVERITY,
    StateStore,
    entry_dict,
    null_area_state,
    usage_dict,
    usage_strings,
)
from .usage import atime_mode, measure_usage
from .watermarks import (
    bytes_to_stop,
    evaluate_tiers,
    highest_active,
    policy_insufficient,
    resolve_marks,
    should_continue,
    simulate_after,
    transitions,
)

log = logging.getLogger("reaper.engine")


@dataclass
class AreaRuntime:
    """Mutable per-area state owned by the scheduler; shared with workers."""

    area: AreaConfig
    lock: threading.Lock = field(default_factory=threading.Lock)
    busy: bool = False
    paused: bool = False
    paused_reason: Optional[str] = None
    paused_by: Optional[str] = None
    paused_at: Optional[str] = None
    disabled: bool = False
    disabled_reason: Optional[str] = None
    disabled_by: Optional[str] = None
    disabled_at: Optional[str] = None
    active_tiers: FrozenSet[str] = frozenset()
    prev_queued_paths: FrozenSet[str] = frozenset()
    interrupt: threading.Event = field(default_factory=threading.Event)
    incompressible: Dict[Tuple[str, int, int], float] = field(default_factory=dict)
    last_scan_at: Optional[float] = None
    force_dry_run: bool = False          # set by POST /dry-run

    def try_acquire(self) -> bool:
        with self.lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def release(self) -> None:
        with self.lock:
            self.busy = False

    def is_incompressible(self, info) -> bool:
        return (info.path, info.size, info.mtime_ns) in self.incompressible

    def remember_incompressible(self, info, now: float) -> None:
        self.incompressible[(info.path, info.size, info.mtime_ns)] = now
        if len(self.incompressible) > 50000:
            oldest = sorted(self.incompressible.items(), key=lambda kv: kv[1])[:10000]
            for k, _ in oldest:
                self.incompressible.pop(k, None)


@dataclass
class Deps:
    """Everything scan_area touches outside pure code — injectable for tests."""

    store: StateStore
    history: object
    area_state: object
    exclusions: ExclusionRegistry
    notifier: object
    measure_usage: Callable[[str], Optional[UsageSample]]
    enumerate_files: Callable = enumerate_files
    load_fts: Callable[[str], FtsSnapshot] = load_fts_snapshot
    delete: Callable = actions.delete_file
    compress: Callable = actions.compress_file
    clock: Callable[[], float] = time.time
    shutdown: threading.Event = field(default_factory=threading.Event)
    open_inodes: Callable = open_inodes_proc
    atime_mode: Callable[[str], str] = atime_mode
    sweep_temp: Callable = sweep_temp_files
    prune_dirs: Callable = actions.prune_empty_dirs
    owner_lookup: Callable = owner_names
    fs_locks: Dict[int, threading.Lock] = field(default_factory=dict)
    fs_locks_guard: threading.Lock = field(default_factory=threading.Lock)

    def fs_lock(self, dev: Optional[int]) -> threading.Lock:
        with self.fs_locks_guard:
            key = dev if dev is not None else -1
            lock = self.fs_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self.fs_locks[key] = lock
            return lock


@dataclass
class ScanReport:
    scan_id: str
    outcome: str
    passes: int = 0
    acted: int = 0
    bytes_freed: int = 0
    reason: Optional[str] = None
    duration_s: float = 0.0


# ---------------------------------------------------------------------------
def protected_paths(area: AreaConfig, settings) -> FrozenSet[str]:
    prot: Set[str] = set()
    if area.fts_db:
        for suffix in ("", "-wal", "-shm", "-journal"):
            prot.add(area.fts_db + suffix)
    url = getattr(settings, "database_url", "") or ""
    if url.startswith("sqlite:///"):
        db_path = os.path.realpath(url[len("sqlite:///"):])
        for suffix in ("", "-wal", "-shm", "-journal"):
            prot.add(db_path + suffix)
    for p in getattr(settings, "protected_paths", []) or []:
        prot.add(os.path.realpath(os.path.expanduser(p)))
    return frozenset(prot)


def _tier_view(area: AreaConfig, active: FrozenSet[str], usage: Optional[UsageSample]) -> Dict:
    out = {}
    for name, cfg in area.tiers.items():
        marks = resolve_marks(cfg)
        out[name] = {"threshold": cfg.threshold, "trigger": marks.trigger, "stop": marks.stop,
                     "policy": cfg.policy, "min_age": cfg.min_age, "active": name in active,
                     "low_water": cfg.low_water, "high_water": cfg.high_water,
                     "bytes_to_stop": bytes_to_stop(usage, marks) if usage else None,
                     "bytes_to_stop_str": fmt_bytes(bytes_to_stop(usage, marks)) if usage else None}
    return out


def _area_state_name(rt: AreaRuntime, active: FrozenSet[str], usage: Optional[UsageSample],
                     missing: bool) -> str:
    if rt.disabled:
        return "DISABLED"
    if missing:
        return "MISSING"
    if usage is None:
        return "UNKNOWN"
    top = highest_active(active)
    if rt.paused:
        return "PAUSED" if top is None else top.upper()
    return top.upper() if top else "GOOD"


class AreaScanner:
    """Runs one scan of one area.  Instantiated per scan by the scheduler."""

    def __init__(self, area: AreaConfig, rt: AreaRuntime, deps: Deps, settings) -> None:
        self.area = area
        self.rt = rt
        self.deps = deps
        self.settings = settings
        self.scan_id = str(uuid.uuid4())
        self.started = deps.clock()
        self.dry_run = bool(settings.dry_run or area.dry_run or rt.force_dry_run)
        self.exhausted: Set[str] = set()
        self.touched_dirs: Set[str] = set()
        self.acted_total = 0
        self.freed_total = 0
        self.warnings: List[str] = []
        self.protected = protected_paths(area, settings)
        self.stats = ScanStats()
        self.mode = "unknown"
        self.last_publish = 0.0
        self.sim_usage: Optional[UsageSample] = None      # dry run: carried across passes
        self.dry_acted: Set[str] = set()                  # dry run: paths already "acted" this scan

    # ---- history / notify helpers ----
    def _hist(self, event_type: str, **fields) -> None:
        try:
            self.deps.history.record(event_type, self.area.name, scan_id=self.scan_id, **fields)
        except Exception as exc:
            log.error("history write failed (%s): %s", event_type, exc)

    def _notify(self, severity: str, title: str, message: str, key: str, **meta) -> None:
        try:
            self.deps.notifier.emit(severity, title, message, key=f"{self.area.name}:{key}",
                                    area=self.area.name, meta=meta)
        except Exception as exc:
            log.error("notify failed: %s", exc)

    # ---- publication ----
    def _base_snapshot(self, usage: Optional[UsageSample], active: FrozenSet[str],
                       missing: bool = False) -> Dict:
        snap = null_area_state(self.area)
        rt = self.rt
        snap.update({
            "state": _area_state_name(rt, active, usage, missing),
            "paused": rt.paused, "paused_reason": rt.paused_reason, "paused_by": rt.paused_by,
            "paused_at": rt.paused_at,
            "disabled": rt.disabled, "disabled_reason": rt.disabled_reason,
            "disabled_by": rt.disabled_by, "disabled_at": rt.disabled_at,
            "dry_run": self.dry_run,
            "usage": usage_dict(usage), "usage_str": usage_strings(usage),
            "atime_mode": self.mode,
            "tiers": _tier_view(self.area, active, usage),
            "active_tiers": sorted(active),
            "warnings": list(self.warnings),
            "last_scan_at": rt.last_scan_at,
        })
        snap["rank"] = AREA_SEVERITY.get(snap["state"], 5)
        snap["scan"] = {**snap["scan"], "scan_id": self.scan_id, "started": self.started,
                        "acted": self.acted_total, "bytes_freed": self.freed_total,
                        "bytes_freed_str": fmt_bytes(self.freed_total),
                        "files_seen": self.stats.files}
        snap["reject_counts"] = {}
        snap["scan_errors"] = self.stats.errors
        return snap

    def _publish(self, snap: Dict) -> None:
        self.deps.store.replace_area(self.area.name, snap)

    def _publish_queue(self, snap: Dict, queue: List[QueueEntry], tier: str, policy: Policy,
                       pass_no: int, rejects: Dict[str, int]) -> Dict:
        limit = int(getattr(self.settings, "queue_publish_limit", 1000))
        entries = [entry_dict(e) for e in queue[:limit]]
        q = {"compress": {"total": 0, "shown": 0, "entries": []},
             "delete":   {"total": 0, "shown": 0, "entries": []}}
        q[policy.kind] = {"total": len(queue), "shown": len(entries), "entries": entries}
        if not queue and pass_no > 1:
            # Keep the previous pass's (already acted-on, status-annotated) queue on
            # display rather than wiping it with an empty one.
            prev = self.deps.store.area(self.area.name) or {}
            if prev.get("queues"):
                q = prev["queues"]
        snap = {**snap, "queues": q, "selected_tier": tier, "reject_counts": dict(rejects),
                "scan": {**snap["scan"], "pass_no": pass_no}}
        self._publish(snap)
        return snap

    def _progress(self, snap: Dict, entry: Optional[QueueEntry], acted: int, remaining: int,
                  freed: int, usage: UsageSample, statuses: Dict[str, Tuple[str, Optional[str]]],
                  force: bool = False) -> None:
        now = self.deps.clock()
        if not force and now - self.last_publish < 2.0 and acted % 25 != 0:
            return
        self.last_publish = now
        queues = {}
        for kind, q in snap["queues"].items():
            ents = []
            for e in q["entries"]:
                st = statuses.get(e["path"])
                ents.append({**e, "status": st[0], "reason": st[1]} if st else e)
            queues[kind] = {**q, "entries": ents}
        self.deps.store.update_area(self.area.name,
            queues=queues,
            usage=usage_dict(usage), usage_str=usage_strings(usage),
            progress={"acting": entry is not None, "current_path": entry.info.path if entry else None,
                      "acted": acted, "remaining": remaining, "bytes_freed": freed},
            scan={**snap["scan"], "acted": self.acted_total + acted,
                  "bytes_freed": self.freed_total + freed,
                  "bytes_freed_str": fmt_bytes(self.freed_total + freed)})

    # ---- the scan ----
    def run(self) -> ScanReport:
        area, rt, deps, S = self.area, self.rt, self.deps, self.settings
        report = ScanReport(scan_id=self.scan_id, outcome="ok")
        if rt.disabled:
            report.outcome = "skipped"
            report.reason = "disabled"
            return report
        if not rt.try_acquire():
            report.outcome = "skipped"
            report.reason = "busy"
            return report
        rt.interrupt.clear()
        try:
            dev = None
            try:
                dev = os.stat(area.real_path).st_dev
            except OSError:
                pass
            with deps.fs_lock(dev):
                self._run_locked(report)
        except Exception as exc:                                  # never kill the scheduler
            log.exception("scan of %s crashed", area.name)
            report.outcome = "error"
            report.reason = str(exc)
            self._hist("scan_abort", outcome="error", reason="exception", detail={"error": str(exc)})
            self._notify("error", f"Scan of {area.label} crashed", str(exc), key="scan_crash")
        finally:
            rt.last_scan_at = deps.clock()
            rt.release()
            try:
                deps.area_state.mark_scan(area.name, self.scan_id)
            except Exception:
                pass
            try:
                getattr(getattr(deps.history, "db", None), "remove", lambda: None)()
            except Exception:
                pass
        report.duration_s = deps.clock() - self.started
        return report

    def _run_locked(self, report: ScanReport) -> None:
        area, rt, deps, S = self.area, self.rt, self.deps, self.settings
        self._hist("scan_start", detail={"dry_run": self.dry_run})
        missing = not os.path.isdir(area.real_path)
        if missing:
            snap = self._base_snapshot(None, rt.active_tiers, missing=True)
            snap["scan"].update({"finished": deps.clock(), "outcome": "skipped", "reason": "missing"})
            self._publish(snap)
            self._hist("scan_end", outcome="skipped", reason="missing")
            self._notify("error", f"Area {area.label} is missing", f"{area.real_path} is not a directory",
                         key="missing")
            report.outcome, report.reason = "skipped", "missing"
            return
        self.mode = deps.atime_mode(area.real_path)
        if any(POLICIES[t.policy].key == "lru" for t in area.tiers.values()) and self.mode == "noatime":
            self.warnings.append("filesystem mounted noatime: LRU ordering degrades to mtime")
        if not self.dry_run:
            try:
                swept = deps.sweep_temp(area, area.settle_seconds, deps.clock)
                if swept:
                    self._hist("maintenance", reason="temp_sweep", count=swept)
            except Exception as exc:
                log.warning("temp sweep failed: %s", exc)

        max_passes = max(1, int(getattr(S, "max_passes_per_scan", 3)))
        usage = None
        active = rt.active_tiers
        for pass_no in range(1, max_passes + 1):
            if self.dry_run and self.sim_usage is not None:
                usage = self.sim_usage
            else:
                usage = deps.measure_usage(area.real_path)
            if usage is None:
                snap = self._base_snapshot(None, active)
                snap["scan"].update({"finished": deps.clock(), "outcome": "skipped",
                                     "reason": "usage_unavailable"})
                self._publish(snap)
                self._hist("scan_end", outcome="skipped", reason="usage_unavailable")
                self._notify("error", f"Cannot measure {area.label}",
                             f"disk usage unavailable for {area.real_path}; nothing will be deleted",
                             key="usage_unavailable")
                report.outcome, report.reason = "skipped", "usage_unavailable"
                return
            active = evaluate_tiers(usage.used_pct, area.tiers, rt.active_tiers)
            up, down = transitions(rt.active_tiers, active)
            for t in up:
                marks = resolve_marks(area.tiers[t])
                self._hist("tier_activate", tier=t, used_pct_before=usage.used_pct,
                           detail={"trigger": marks.trigger, "stop": marks.stop,
                                   "policy": area.tiers[t].policy})
                self._notify(TIER_SEVERITY.get(t, "warning"),
                             f"{area.label}: {t.upper()} — {usage.used_pct:.1f}% used",
                             f"{area.real_path} reached the {t} trigger ({marks.trigger:g}% used); "
                             f"policy {area.tiers[t].policy} will run until {marks.stop:g}%",
                             key=f"tier:{t}", tier=t, used_pct=f"{usage.used_pct:.1f}",
                             policy=area.tiers[t].policy)
            for t in down:
                self._hist("tier_deactivate", tier=t, used_pct_before=usage.used_pct)
                self._notify("info", f"{area.label}: {t} cleared — {usage.used_pct:.1f}% used",
                             f"{area.real_path} is below the {t} stop point", key=f"tier_clear:{t}")
            rt.active_tiers = active
            try:
                deps.area_state.save_active_tiers(area.name, active)
            except Exception as exc:
                log.warning("area_state save failed: %s", exc)

            tier = highest_active(active, exclude=frozenset(self.exhausted))
            if tier is None:
                snap = self._base_snapshot(usage, active)
                if pass_no > 1:
                    prev = self.deps.store.area(area.name) or {}
                    for key in ("queues", "reject_counts", "selected_tier"):
                        if key in prev:
                            snap[key] = prev[key]
                snap["scan"].update({"finished": deps.clock(), "outcome": "ok", "pass_no": pass_no})
                self._publish(snap)
                break

            tcfg = area.tiers[tier]
            policy = POLICIES[tcfg.policy]
            marks = resolve_marks(tcfg)
            report.passes = pass_no

            # ---- plan ----
            fts = None
            if area.fts_db:
                try:
                    fts = deps.load_fts(area.fts_db)
                except FtsUnavailable as exc:
                    fts = EMPTY_SNAPSHOT
                    msg = f"FTS database unreadable ({exc}); nothing is eligible"
                    if msg not in self.warnings:
                        self.warnings.append(msg)
                    self._notify("error", f"{area.label}: FTS gate unavailable", str(exc),
                                 key="fts_unavailable")
            open_inodes = None
            if area.check_open_files:
                try:
                    open_inodes = deps.open_inodes()
                except Exception:
                    open_inodes = None
            self.stats = ScanStats()
            infos = list(deps.enumerate_files(area, None, self.protected, self.stats))
            now = deps.clock()
            ctx = EligibilityContext(
                now=now, policy=policy, min_age=tcfg.min_age, settle_seconds=area.settle_seconds,
                include=area.include, exclude=area.exclude, protected=self.protected,
                manual_exclusions=deps.exclusions.rules_for(area.name), fts=fts,
                open_inodes=open_inodes, allow_hardlinks=area.allow_hardlinks,
                compressed_suffixes=tuple(area.skip_suffixes), area_root=area.real_path,
                siblings=self.stats.siblings)
            eligible, rejects = partition(infos, ctx)
            if self.dry_run and self.dry_acted:
                before = len(eligible)
                eligible = [i for i in eligible if i.path not in self.dry_acted]
                if before != len(eligible):
                    rejects["dry_run_acted"] = before - len(eligible)
            if policy.kind == "compress":
                before = len(eligible)
                eligible = [i for i in eligible if not rt.is_incompressible(i)]
                if before != len(eligible):
                    rejects["incompressible"] = before - len(eligible)
            queue = build_queue(eligible, policy, tier, deps.owner_lookup)

            # ---- publish ----
            snap = self._base_snapshot(usage, active)
            snap = self._publish_queue(snap, queue, tier, policy, pass_no, rejects)
            mode = getattr(S, "history_log_queue_adds", "new_only")
            if mode != "none" and queue:
                paths = [q.info.path for q in queue]
                to_log = paths if mode == "all" else diff_new_paths(paths, rt.prev_queued_paths)
                by_path = {q.info.path: q for q in queue}
                rows = []
                for p in to_log[:20000]:
                    q = by_path[p]
                    rows.append({"area": area.name, "event_type": "queue_add", "path": p,
                                 "tier": tier, "policy": policy.name, "queue": policy.kind,
                                 "size_bytes": q.info.size, "used_pct_before": usage.used_pct,
                                 "scan_id": self.scan_id, "actor": "system",
                                 "detail": {"trigger": marks.trigger, "stop": marks.stop,
                                            "key_ts": q.key_ts, "atime": q.info.atime,
                                            "mtime": q.info.mtime, "owner": q.owner}})
                try:
                    deps.history.record_many(rows)
                except Exception as exc:
                    log.error("queue_add history write failed: %s", exc)
            rt.prev_queued_paths = frozenset(q.info.path for q in queue)

            if not queue:
                if policy_insufficient(usage.used_pct, tcfg):
                    self._insufficient(tier, tcfg, usage, rejects)
                else:
                    self._hist("queue_remove", tier=tier, queue=policy.kind, reason="no_eligible",
                               count=0, detail={"rejects": rejects})
                self.exhausted.add(tier)
                continue
            if rt.paused:
                self._hist("queue_remove", tier=tier, queue=policy.kind, reason="paused",
                           count=len(queue), detail={"sample": [q.info.path for q in queue[:10]]})
                snap["scan"].update({"finished": deps.clock(), "outcome": "paused", "pass_no": pass_no})
                self._publish(snap)
                report.outcome, report.reason = "paused", "paused"
                break

            # ---- act ----
            stop, usage = self._act(snap, queue, tier, tcfg, policy, marks, usage)
            self.acted_total += stop.acted
            self.freed_total += stop.bytes_freed
            report.acted, report.bytes_freed = self.acted_total, self.freed_total
            if stop.reason == "stop_reached":
                self._notify("info", f"{area.label}: {tier} stop point reached",
                             f"{stop.acted} file(s), {fmt_bytes(stop.bytes_freed)} freed; "
                             f"now {usage.used_pct:.1f}% used", key=f"stop:{tier}",
                             acted=stop.acted, bytes_freed=stop.bytes_freed)
                continue
            if stop.reason == "exhausted":
                fresh = deps.measure_usage(area.real_path) or usage
                usage = fresh
                if policy_insufficient(usage.used_pct, tcfg):
                    self._insufficient(tier, tcfg, usage, rejects, acted=stop.acted)
                self.exhausted.add(tier)
                continue
            if stop.reason in ("paused", "disabled", "shutdown"):
                self._hist("queue_remove", tier=tier, queue=policy.kind, reason=stop.reason,
                           count=stop.remaining)
                report.outcome, report.reason = stop.reason, stop.reason
                if stop.reason == "shutdown":
                    self._hist("scan_abort", outcome="aborted", reason="shutdown")
                break
            if stop.reason in ("limit", "stalled", "error"):
                report.outcome, report.reason = "stopped", stop.reason
                break

        # ---- wrap up ----
        if area.prune_empty_dirs and not self.dry_run and self.touched_dirs:
            try:
                removed = deps.prune_dirs(self.touched_dirs, area.real_path, self.protected)
                if removed:
                    self._hist("maintenance", reason="prune_empty_dirs", count=removed)
            except Exception as exc:
                log.warning("prune_empty_dirs failed: %s", exc)
        final = self.deps.store.area(area.name) or self._base_snapshot(usage, active)
        finished = deps.clock()
        final = {**final,
                 "state": _area_state_name(rt, active, usage, False),
                 "active_tiers": sorted(active),
                 "tiers": _tier_view(area, active, usage),
                 "usage": usage_dict(usage), "usage_str": usage_strings(usage),
                 "warnings": list(self.warnings),
                 "progress": {"acting": False, "current_path": None, "acted": self.acted_total,
                              "remaining": 0, "bytes_freed": self.freed_total},
                 "scan": {**final["scan"], "finished": finished,
                          "duration_s": round(finished - self.started, 3),
                          "outcome": report.outcome, "reason": report.reason,
                          "acted": self.acted_total, "bytes_freed": self.freed_total,
                          "bytes_freed_str": fmt_bytes(self.freed_total),
                          "files_seen": self.stats.files},
                 "scan_errors": self.stats.errors,
                 "last_scan_at": finished}
        final["rank"] = AREA_SEVERITY.get(final["state"], 5)
        self._publish(final)
        self._hist("scan_end", outcome=report.outcome, reason=report.reason,
                   count=self.acted_total, bytes_freed=self.freed_total,
                   used_pct_after=usage.used_pct if usage else None,
                   duration_s=round(finished - self.started, 3),
                   detail={"passes": report.passes, "files_seen": self.stats.files,
                           "scan_errors": self.stats.errors, "dry_run": self.dry_run,
                           "active_tiers": sorted(active)})

    def _insufficient(self, tier: str, tcfg: TierConfig, usage: UsageSample,
                      rejects: Dict[str, int], acted: int = 0) -> None:
        self._hist("policy_insufficient", tier=tier, policy=tcfg.policy,
                   used_pct_after=usage.used_pct, count=acted, detail={"rejects": rejects})
        sev = "critical" if tier == "full" else "error"
        self._notify(sev, f"{self.area.label}: {tcfg.policy} could not clear {tier}",
                     f"still {usage.used_pct:.1f}% used (threshold {tcfg.threshold:g}%) after "
                     f"{acted} action(s); no more eligible files (min_age {tcfg.min_age}s). "
                     f"Rejections: {rejects}", key=f"insufficient:{tier}",
                     tier=tier, policy=tcfg.policy, used_pct=f"{usage.used_pct:.1f}")

    def _act(self, snap: Dict, queue: List[QueueEntry], tier: str, tcfg: TierConfig,
             policy: Policy, marks, usage: UsageSample) -> Tuple[PassStop, UsageSample]:
        area, rt, deps, S = self.area, self.rt, self.deps, self.settings
        comp: Optional[Compressor] = None
        if policy.kind == "compress":
            try:
                comp = get_compressor(tcfg.compressor or area.compressor)
            except CompressorError as exc:
                self.warnings.append(str(exc))
                self._notify("error", f"{area.label}: compression unavailable", str(exc), key="compressor")
                return PassStop("error", 0, len(queue)), usage
        actx = actions.ActionContext(
            area_root=area.real_path, dry_run=self.dry_run, compressor=comp,
            level=area.compress_level, verify=getattr(S, "compress_verify", "crc"),
            min_ratio=float(getattr(S, "compress_min_ratio", 0.95)),
            assumed_ratio=float(getattr(S, "compress_assumed_ratio", 0.5)),
            protected=self.protected, clock=deps.clock, interrupt=rt.interrupt,
            shutdown=deps.shutdown)
        acted = freed = 0
        statuses: Dict[str, Tuple[str, Optional[str]]] = {}
        remeasure_every = max(1, int(getattr(S, "remeasure_every", 1)))
        stall_window = max(5, int(getattr(S, "stall_window", 20)))
        max_actions = area.max_actions_per_scan or 0
        last_check_used = usage.used_pct
        freed_since_check = 0
        total = len(queue)
        for i, entry in enumerate(queue):
            remaining = total - i
            if deps.shutdown.is_set():
                self._progress(snap, None, acted, remaining, freed, usage, statuses, force=True)
                return PassStop("shutdown", acted, remaining, freed), usage
            if rt.interrupt.is_set():
                self._progress(snap, None, acted, remaining, freed, usage, statuses, force=True)
                return PassStop("disabled" if rt.disabled else "paused", acted, remaining, freed), usage
            if not should_continue(usage.used_pct, marks):
                self._progress(snap, None, acted, remaining, freed, usage, statuses, force=True)
                return PassStop("stop_reached", acted, remaining, freed), usage
            if max_actions and acted >= max_actions:
                self._hist("queue_remove", tier=tier, queue=policy.kind, reason="limit", count=remaining)
                self._progress(snap, None, acted, remaining, freed, usage, statuses, force=True)
                return PassStop("limit", acted, remaining, freed), usage
            path = entry.info.path
            if is_excluded(path, deps.exclusions.rules_for(area.name)):
                statuses[path] = ("excluded", "manual_exclusion")
                self._hist("queue_remove", path=path, tier=tier, queue=policy.kind, reason="excluded")
                continue
            self._progress(snap, entry, acted, remaining, freed, usage, statuses)
            before = usage.used_pct
            if policy.kind == "compress":
                res = deps.compress(entry.info, actx, area.settle_seconds, area.allow_hardlinks)
            else:
                res = deps.delete(entry.info, actx, area.settle_seconds, area.allow_hardlinks)
            if res.outcome == "skipped":
                statuses[path] = ("skipped", res.reason)
                self._hist("queue_remove", path=path, tier=tier, queue=policy.kind,
                           reason=res.reason, detail={"error": res.error})
                if res.reason == "incompressible":
                    rt.remember_incompressible(entry.info, deps.clock())
                if res.reason == "interrupted":
                    return PassStop("shutdown" if deps.shutdown.is_set() else
                                    ("disabled" if rt.disabled else "paused"), acted, remaining, freed), usage
                continue
            if res.outcome == "failed":
                statuses[path] = ("failed", res.reason)
                self._hist("action_failed", path=path, tier=tier, policy=policy.name,
                           queue=policy.kind, reason=res.reason, size_bytes=entry.info.size,
                           detail={"error": res.error})
                continue
            acted += 1
            freed += res.bytes_freed
            freed_since_check += res.bytes_freed
            self.touched_dirs.add(os.path.dirname(path))
            statuses[path] = (res.outcome, None)
            if self.dry_run:
                usage = simulate_after(usage, res.bytes_freed)
                self.sim_usage = usage
                self.dry_acted.add(path)
            elif acted % remeasure_every == 0:
                usage = deps.measure_usage(area.real_path) or usage
            self._hist("action_compress" if policy.kind == "compress" else "action_delete",
                       path=path, tier=tier, policy=policy.name, queue=policy.kind,
                       outcome=res.outcome, size_bytes=entry.info.size, bytes_freed=res.bytes_freed,
                       duration_s=round(res.duration_s, 4), used_pct_before=before,
                       used_pct_after=usage.used_pct,
                       detail={"dest_path": res.dest_path, "atime": entry.info.atime,
                               "mtime": entry.info.mtime, "owner": entry.owner, "key_ts": entry.key_ts})
            if not self.dry_run and acted % stall_window == 0:
                denom = usage.used + usage.free
                expected = 100.0 * freed_since_check / denom if denom else 0.0
                if expected > 0.1 and (last_check_used - usage.used_pct) < 0.25 * expected:
                    self._hist("space_not_reclaimed", tier=tier, policy=policy.name, count=acted,
                               bytes_freed=freed, used_pct_after=usage.used_pct,
                               detail={"expected_drop_pct": expected,
                                       "observed_drop_pct": last_check_used - usage.used_pct})
                    self._notify("error", f"{area.label}: space not reclaimed",
                                 f"freed {fmt_bytes(freed_since_check)} but used space only moved "
                                 f"{last_check_used - usage.used_pct:.2f} pts (expected {expected:.2f}); "
                                 f"files may be held open by another process", key="stalled")
                    self._progress(snap, None, acted, remaining - 1, freed, usage, statuses, force=True)
                    return PassStop("stalled", acted, remaining - 1, freed), usage
                last_check_used = usage.used_pct
                freed_since_check = 0
        self._progress(snap, None, acted, 0, freed, usage, statuses, force=True)
        return PassStop("exhausted", acted, 0, freed), usage


def scan_area(area: AreaConfig, rt: AreaRuntime, deps: Deps, settings) -> ScanReport:
    return AreaScanner(area, rt, deps, settings).run()
