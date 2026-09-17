"""Scheduler thread: periodic scans, manual scan requests, pause/resume,
enable/disable, daily maintenance and graceful shutdown."""

import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .domain import AreaConfig
from .reaper import AreaRuntime, Deps, ScanReport, scan_area

log = logging.getLogger("reaper.scheduler")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Scheduler:
    def __init__(self, deps: Deps, settings, history=None) -> None:
        self.deps = deps
        self.settings = settings
        self.history = history if history is not None else deps.history
        self.runtimes: Dict[str, AreaRuntime] = {}
        self._requests: "queue.Queue[str]" = queue.Queue()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._futures: Dict[str, Future] = {}
        self._last_maintenance = 0.0
        self.last_tick: Optional[float] = None
        self.next_tick: Optional[float] = None
        self.reports: Dict[str, ScanReport] = {}
        self._restore_state()

    # ---- setup ----
    def _restore_state(self) -> None:
        saved = {}
        try:
            saved = self.deps.area_state.all()
        except Exception as exc:
            log.warning("could not load area_state: %s", exc)
        for area in self.settings.areas:
            rt = AreaRuntime(area=area)
            st = saved.get(area.name) or {}
            rt.paused = bool(st.get("paused"))
            rt.paused_reason, rt.paused_by, rt.paused_at = st.get("paused_reason"), st.get("paused_by"), st.get("paused_at")
            rt.disabled = bool(st.get("disabled")) or not area.enabled
            rt.disabled_reason = st.get("disabled_reason") or (None if area.enabled else "disabled in config")
            rt.disabled_by, rt.disabled_at = st.get("disabled_by"), st.get("disabled_at")
            rt.active_tiers = frozenset(t for t in (st.get("active_tiers") or []) if t in area.tiers)
            self.runtimes[area.name] = rt
            try:
                self.deps.area_state.load(area.name, area.real_path)
            except Exception:
                pass
        self.deps.store.init_areas(self.settings.areas)
        for name, rt in self.runtimes.items():
            self.deps.store.update_area(name, paused=rt.paused, paused_reason=rt.paused_reason,
                                        paused_by=rt.paused_by, paused_at=rt.paused_at,
                                        disabled=rt.disabled, disabled_reason=rt.disabled_reason,
                                        disabled_by=rt.disabled_by, disabled_at=rt.disabled_at,
                                        active_tiers=sorted(rt.active_tiers),
                                        state="DISABLED" if rt.disabled else "UNKNOWN")

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread is not None:
            return
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(self.settings.workers)),
                                        thread_name_prefix="reaper-worker")
        self._thread = threading.Thread(target=self._run, name="reaper-scheduler", daemon=True)
        self._thread.start()

    def stop(self, grace_s: Optional[float] = None) -> None:
        grace = float(grace_s if grace_s is not None else self.settings.shutdown_grace_s)
        self.deps.shutdown.set()
        for rt in self.runtimes.values():
            rt.interrupt.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(min(grace, 5.0))
        deadline = time.time() + grace
        for fut in list(self._futures.values()):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                fut.result(timeout=remaining)
            except Exception:
                pass
        if self._pool is not None:
            self._pool.shutdown(wait=False)

    # ---- API used by web / CLI ----
    def runtime(self, area: str) -> Optional[AreaRuntime]:
        return self.runtimes.get(area)

    def request_scan(self, area: str, dry_run: bool = False) -> bool:
        rt = self.runtimes.get(area)
        if rt is None:
            return False
        rt.force_dry_run = bool(dry_run)
        self._requests.put(area)
        self._wake.set()
        return True

    def run_now(self, area: str, dry_run: bool = False) -> Optional[ScanReport]:
        """Synchronous scan on the calling thread (used by POST /dry-run)."""
        rt = self.runtimes.get(area)
        if rt is None:
            return None
        rt.force_dry_run = bool(dry_run)
        try:
            report = scan_area(rt.area, rt, self.deps, self.settings)
        finally:
            rt.force_dry_run = False
        self.reports[area] = report
        return report

    def _record(self, event: str, area: str, by: str, reason: Optional[str]) -> None:
        try:
            self.history.record(event, area, actor=by, reason=(reason or "")[:64],
                                detail={"reason": reason})
        except Exception as exc:
            log.warning("history write failed: %s", exc)

    def pause(self, area: str, by: str = "system", reason: Optional[str] = None) -> bool:
        rt = self.runtimes.get(area)
        if rt is None:
            return False
        with rt.lock:
            rt.paused, rt.paused_reason, rt.paused_by, rt.paused_at = True, reason, by, _now_iso()
        rt.interrupt.set()
        self.deps.area_state.set_paused(area, True, by, reason)
        self._record("area_pause", area, by, reason)
        self.deps.store.update_area(area, paused=True, paused_reason=reason, paused_by=by,
                                    paused_at=rt.paused_at)
        return True

    def resume(self, area: str, by: str = "system", reason: Optional[str] = None) -> bool:
        rt = self.runtimes.get(area)
        if rt is None:
            return False
        with rt.lock:
            rt.paused, rt.paused_reason, rt.paused_by, rt.paused_at = False, None, None, None
        if not rt.disabled:
            rt.interrupt.clear()
        self.deps.area_state.set_paused(area, False, by, reason)
        self._record("area_resume", area, by, reason)
        self.deps.store.update_area(area, paused=False, paused_reason=None, paused_by=None, paused_at=None)
        self.request_scan(area)
        return True

    def disable(self, area: str, by: str = "system", reason: Optional[str] = None) -> bool:
        rt = self.runtimes.get(area)
        if rt is None:
            return False
        with rt.lock:
            rt.disabled, rt.disabled_reason, rt.disabled_by, rt.disabled_at = True, reason, by, _now_iso()
        rt.interrupt.set()
        self.deps.area_state.set_disabled(area, True, by, reason)
        self._record("area_disable", area, by, reason)
        self.deps.store.update_area(area, disabled=True, disabled_reason=reason, disabled_by=by,
                                    disabled_at=rt.disabled_at, state="DISABLED", rank=1,
                                    progress={"acting": False, "current_path": None, "acted": 0,
                                              "remaining": 0, "bytes_freed": 0})
        return True

    def enable(self, area: str, by: str = "system", reason: Optional[str] = None) -> bool:
        rt = self.runtimes.get(area)
        if rt is None:
            return False
        with rt.lock:
            rt.disabled, rt.disabled_reason, rt.disabled_by, rt.disabled_at = False, None, None, None
        if not rt.paused:
            rt.interrupt.clear()
        self.deps.area_state.set_disabled(area, False, by, reason)
        self._record("area_enable", area, by, reason)
        self.deps.store.update_area(area, disabled=False, disabled_reason=None, disabled_by=None,
                                    disabled_at=None, state="UNKNOWN", rank=5)
        self.request_scan(area)
        return True

    def health(self) -> Dict[str, object]:
        interval = max(1, int(self.settings.scan_interval))
        age = None if self.last_tick is None else time.time() - self.last_tick
        return {"alive": self._thread is not None and self._thread.is_alive(),
                "last_tick": self.last_tick, "tick_age_s": None if age is None else round(age, 1),
                "next_tick_in_s": None if self.next_tick is None else max(0, round(self.next_tick - time.time(), 1)),
                "stalled": age is not None and age > 3 * interval,
                "busy": sorted(n for n, rt in self.runtimes.items() if rt.busy)}

    # ---- loop ----
    def _submit(self, name: str) -> None:
        rt = self.runtimes.get(name)
        if rt is None or rt.disabled or rt.busy:
            return
        fut = self._futures.get(name)
        if fut is not None and not fut.done():
            return
        def _job(rt=rt):
            report = scan_area(rt.area, rt, self.deps, self.settings)
            self.reports[rt.area.name] = report
            rt.force_dry_run = False
            return report
        self._futures[name] = self._pool.submit(_job)

    def tick(self) -> None:
        self.last_tick = time.time()
        self.deps.store.tick(alive=True)
        for name in self.runtimes:
            self._submit(name)
        self._maintenance()

    def _drain_requests(self) -> None:
        while True:
            try:
                name = self._requests.get_nowait()
            except queue.Empty:
                return
            self._submit(name)

    def _maintenance(self) -> None:
        now = time.time()
        if now - self._last_maintenance < 86400:
            return
        self._last_maintenance = now
        try:
            days = int(self.settings.history_retention_days)
            pruned = self.history.prune_days(days) if hasattr(self.history, "prune_days") else 0
            if pruned:
                self.history.record("maintenance", "", reason="history_prune", count=pruned)
        except Exception as exc:
            log.warning("maintenance failed: %s", exc)

    def _run(self) -> None:
        interval = max(1, int(self.settings.scan_interval))
        self.tick()
        while not self.deps.shutdown.is_set():
            self.next_tick = time.time() + interval
            woke = self._wake.wait(interval)
            if self.deps.shutdown.is_set():
                break
            if woke:
                self._wake.clear()
                self._drain_requests()
                continue
            try:
                self.tick()
            except Exception as exc:
                log.exception("scheduler tick failed: %s", exc)
        self.deps.store.tick(alive=False)
