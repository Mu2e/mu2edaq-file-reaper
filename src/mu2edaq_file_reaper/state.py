"""Latest engine results per area, shared between worker threads and Flask.

Thread-safety contract (inherited from diskwatcher): writers install **new**
dicts (``{**old, **changes}``); readers get copies.  Nobody mutates a published
dict in place.
"""

import threading
import time
from typing import Any, Dict, List, Optional

from .domain import AreaConfig
from .formatting import DASH, fmt_bytes, fmt_duration

#: States shown on the dashboard, in display order.
AREA_STATES = ("GOOD", "WARNING", "CRITICAL", "FULL", "PAUSED", "DISABLED", "UNKNOWN", "MISSING")
AREA_SEVERITY = {"GOOD": 0, "PAUSED": 1, "DISABLED": 1, "WARNING": 2, "CRITICAL": 3,
                 "FULL": 4, "UNKNOWN": 5, "MISSING": 6}


def null_area_state(area: AreaConfig) -> Dict[str, Any]:
    """Every key the API can emit, at its "nothing known yet" value."""
    return {
        "name": area.name, "path": area.path, "real_path": area.real_path, "label": area.label,
        "enabled": area.enabled, "dry_run": area.dry_run,
        "state": "UNKNOWN", "rank": AREA_SEVERITY["UNKNOWN"],
        "paused": False, "paused_reason": None, "paused_by": None, "paused_at": None,
        "disabled": not area.enabled, "disabled_reason": None, "disabled_by": None, "disabled_at": None,
        "usage": None, "usage_str": {"total": DASH, "used": DASH, "free": DASH, "used_pct": DASH},
        "atime_mode": "unknown",
        "tiers": {}, "active_tiers": [], "selected_tier": None,
        "scan": {"scan_id": None, "pass_no": 0, "started": None, "finished": None,
                 "duration_s": None, "outcome": None, "reason": None, "acted": 0,
                 "bytes_freed": 0, "bytes_freed_str": "0.0 B", "files_seen": 0},
        "queues": {"compress": {"total": 0, "shown": 0, "entries": []},
                   "delete":   {"total": 0, "shown": 0, "entries": []}},
        "progress": {"acting": False, "current_path": None, "acted": 0, "remaining": 0,
                     "bytes_freed": 0},
        "reject_counts": {}, "scan_errors": 0, "warnings": [],
        "config_errors": list(area.config_errors),
        "last_scan_at": None, "next_scan_in_s": None,
    }


AREA_STATE_KEYS = frozenset(null_area_state(
    AreaConfig(name="x", path="/x/y", real_path="/x/y", label="x", tiers={})))


def usage_strings(usage) -> Dict[str, str]:
    if usage is None:
        return {"total": DASH, "used": DASH, "free": DASH, "used_pct": DASH}
    return {"total": fmt_bytes(usage.total), "used": fmt_bytes(usage.used),
            "free": fmt_bytes(usage.free), "used_pct": f"{usage.used_pct:.1f}%"}


def usage_dict(usage) -> Optional[Dict[str, Any]]:
    if usage is None:
        return None
    return {"total": usage.total, "used": usage.used, "free": usage.free,
            "used_pct": round(usage.used_pct, 2), "ts": usage.ts}


def entry_dict(entry, status: str = "queued", reason: Optional[str] = None) -> Dict[str, Any]:
    info = entry.info
    return {
        "id": f"{info.dev}:{info.ino}", "path": info.path, "rel": info.rel, "name": info.name,
        "size": info.size, "size_str": fmt_bytes(info.size),
        "atime": info.atime, "mtime": info.mtime, "key_ts": entry.key_ts,
        "owner": entry.owner, "group": entry.group, "mode": oct(info.mode & 0o7777),
        "queue": entry.queue, "tier": entry.tier, "policy": entry.policy,
        "status": status, "reason": reason,
    }


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._areas: Dict[str, Dict[str, Any]] = {}
        self._order: List[str] = []
        self._last_tick: Optional[float] = None
        self._scheduler_alive = False

    def init_areas(self, areas: List[AreaConfig]) -> None:
        with self._lock:
            self._areas = {a.name: null_area_state(a) for a in areas}
            self._order = [a.name for a in areas]

    def replace_area(self, name: str, snapshot: Dict[str, Any]) -> None:
        with self._lock:
            self._areas[name] = dict(snapshot)
            if name not in self._order:
                self._order.append(name)

    def update_area(self, name: str, **changes: Any) -> None:
        with self._lock:
            old = self._areas.get(name, {})
            self._areas[name] = {**old, **changes}
            if name not in self._order:
                self._order.append(name)

    def area(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            snap = self._areas.get(name)
            return None if snap is None else dict(snap)

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(self._areas[n]) for n in self._order if n in self._areas]

    def tick(self, alive: bool = True) -> None:
        with self._lock:
            self._last_tick = time.time()
            self._scheduler_alive = alive

    def meta(self) -> Dict[str, Any]:
        with self._lock:
            last = self._last_tick
            alive = self._scheduler_alive
        return {"last_tick": last,
                "tick_age_s": None if last is None else round(time.time() - last, 1),
                "scheduler_alive": alive}

    def __len__(self) -> int:
        with self._lock:
            return len(self._areas)


def area_summary(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {s.lower(): 0 for s in AREA_STATES}
    for e in entries:
        st = (e.get("state") or "UNKNOWN").lower()
        if st in counts:
            counts[st] += 1
    counts["total"] = len(entries)
    counts["queued_compress"] = sum(e["queues"]["compress"]["total"] for e in entries)
    counts["queued_delete"] = sum(e["queues"]["delete"]["total"] for e in entries)
    counts["acting"] = sum(1 for e in entries if e["progress"]["acting"])
    return counts


STORE = StateStore()
