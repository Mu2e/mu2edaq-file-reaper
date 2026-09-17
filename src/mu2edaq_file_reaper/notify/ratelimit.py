"""Pure de-duplication / rate-limit state machine for notifications."""

from typing import Dict, Optional, Tuple

SEVERITY_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


def severity_rank(sev: str) -> int:
    return SEVERITY_RANK.get((sev or "info").lower(), 1)


class RateLimiter:
    """``allow(key, severity, now)`` -> ``(allowed, suppression_reason)``.

    * the same *key* within *window* seconds is suppressed as a duplicate,
      unless the severity is *higher* than the last one sent for that key;
    * at most *max_per_hour* sends in any rolling hour (0 = unlimited).
    """

    def __init__(self, window: float = 600.0, max_per_hour: int = 0) -> None:
        self.window = window
        self.max_per_hour = max_per_hour
        self._last: Dict[str, Tuple[float, int]] = {}
        self._sent_times = []

    def allow(self, key: str, severity: str, now: float) -> Tuple[bool, Optional[str]]:
        rank = severity_rank(severity)
        last = self._last.get(key)
        if last is not None:
            last_ts, last_rank = last
            if now - last_ts < self.window and rank <= last_rank:
                return False, "suppressed_dup"
        if self.max_per_hour:
            self._sent_times = [t for t in self._sent_times if now - t < 3600.0]
            if len(self._sent_times) >= self.max_per_hour:
                return False, "suppressed_rate"
        self._last[key] = (now, rank)
        self._sent_times.append(now)
        return True, None

    def forget(self, key: str) -> None:
        self._last.pop(key, None)
