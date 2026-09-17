"""Notification fan-out with a worker thread, severity filtering and dedup.

``Notifier.emit()`` never blocks the reaper: it enqueues, and a daemon thread
delivers to every channel that accepts the event, recording the outcome in the
``notification_log`` table.
"""

import logging
import queue
import threading
import time
from typing import Any, Dict, List, Optional

from .channels import CHANNEL_CLASSES, Channel, Event, build_channels
from .ratelimit import RateLimiter, severity_rank

log = logging.getLogger("reaper.notify")

#: Tier activation -> severity.
TIER_SEVERITY = {"warning": "warning", "critical": "error", "full": "critical"}


class Notifier:
    def __init__(self, channels: List[Channel], limiter: Optional[RateLimiter] = None,
                 log_repo=None, history=None, clock=time.time) -> None:
        self.channels = channels
        self.limiter = limiter or RateLimiter()
        self.log_repo = log_repo
        self.history = history
        self._clock = clock
        self._q: "queue.Queue[Optional[Event]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.sent_count = 0
        self.failed_count = 0

    # ---- lifecycle ----
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="reaper-notifier", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- API ----
    def emit(self, severity: str, title: str, message: str, key: Optional[str] = None,
             area: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> Event:
        ev = Event(severity=severity, title=title, message=message,
                   key=key or f"{area or '-'}:{title}", area=area,
                   meta={k: str(v) for k, v in (meta or {}).items()}, ts=self._clock())
        self._q.put(ev)
        return ev

    def emit_sync(self, ev: Event) -> Dict[str, str]:
        """Deliver *ev* now on the calling thread (tests, `notify test`)."""
        return self._deliver(ev)

    def describe(self) -> List[Dict[str, Any]]:
        return [c.describe() for c in self.channels]

    def channel(self, name: str) -> Optional[Channel]:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    # ---- worker ----
    def _run(self) -> None:
        while not self._stop.is_set() or not self._q.empty():
            try:
                ev = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev is None:
                continue
            try:
                self._deliver(ev)
            except Exception as exc:                       # never die
                log.error("notifier: unexpected error: %s", exc)

    def _deliver(self, ev: Event) -> Dict[str, str]:
        outcomes: Dict[str, str] = {}
        now = self._clock()
        for ch in self.channels:
            if not ch.enabled:
                outcomes[ch.name] = "disabled"
                continue
            if not ch.accepts(ev):
                outcomes[ch.name] = "below_min_severity"
                continue
            allowed, why = self.limiter.allow(f"{ch.name}|{ev.key}", ev.severity, now)
            if not allowed:
                outcomes[ch.name] = why or "suppressed"
                self._log(ch, ev, why or "suppressed")
                continue
            try:
                ch.send(ev)
                outcomes[ch.name] = "sent"
                self.sent_count += 1
                self._log(ch, ev, "sent")
            except Exception as exc:
                outcomes[ch.name] = "failed"
                self.failed_count += 1
                self.limiter.forget(f"{ch.name}|{ev.key}")      # allow a retry next time
                log.warning("notify %s failed: %s", ch.name, exc)
                self._log(ch, ev, "failed", error=str(exc))
        if self.history is not None and any(v == "sent" for v in outcomes.values()):
            try:
                self.history.record("notification_sent", ev.area or "", detail={
                    "title": ev.title, "severity": ev.severity, "channels": outcomes})
            except Exception as exc:
                log.warning("notify: history write failed: %s", exc)
        return outcomes

    def _log(self, ch: Channel, ev: Event, outcome: str, error: Optional[str] = None) -> None:
        if self.log_repo is None:
            return
        try:
            self.log_repo.record(ch.name, ev.severity, ev.title, outcome, area=ev.area,
                                 event_key=ev.key, body=ev.message, error=error,
                                 detail=dict(ev.meta))
        except Exception as exc:
            log.warning("notify: log write failed: %s", exc)


def build_notifier(notifications_cfg: Dict[str, Any], label: str, log_repo=None,
                   history=None) -> Notifier:
    channels = build_channels(notifications_cfg or {}, label)
    limiter = RateLimiter(window=float((notifications_cfg or {}).get("rate_limit_seconds", 600)),
                          max_per_hour=int((notifications_cfg or {}).get("max_per_hour", 0)))
    return Notifier(channels, limiter, log_repo=log_repo, history=history)
