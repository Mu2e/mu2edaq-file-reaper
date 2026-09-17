"""Frozen value types shared by every engine module.

No logic beyond trivial derived properties lives here; the modules that
*decide* things (:mod:`watermarks`, :mod:`eligibility`, :mod:`policies`) are
pure functions over these types, which keeps them directly unit-testable.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

#: Tier names in ascending severity.  Selection between active tiers is by this
#: order, never by threshold value, so ``full`` always means "most aggressive".
TIER_ORDER: Tuple[str, ...] = ("warning", "critical", "full")

POLICY_NAMES: Tuple[str, ...] = ("LRU-Delete", "Age-Delete", "LRU-Compress", "Age-Compress")

#: Tier -> policy used when the YAML does not say otherwise.
DEFAULT_TIER_POLICY = {"warning": "LRU-Compress", "critical": "LRU-Delete", "full": "Age-Delete"}
DEFAULT_THRESHOLDS  = {"warning": 80.0, "critical": 90.0, "full": 95.0}
DEFAULT_MIN_AGE     = {"warning": 7 * 86400, "critical": 3 * 86400, "full": 86400}


@dataclass(frozen=True)
class TierConfig:
    """One of warning/critical/full for one area."""

    name:       str
    threshold:  float                  # percent of capacity USED
    policy:     str
    min_age:    int = 0                # seconds, on the policy's ordering key
    high_water: float = 0.0            # percent above threshold that triggers
    low_water:  float = 10.0           # percent below threshold that stops
    compressor: Optional[str] = None   # override the area's algorithm


@dataclass(frozen=True)
class AreaConfig:
    """A configured disk area.  ``real_path`` is resolved once at config time."""

    name:            str
    path:            str
    real_path:       str
    label:           str
    tiers:           Dict[str, TierConfig]
    include:         Tuple[str, ...] = ("*",)
    exclude:         Tuple[str, ...] = ()
    settle_seconds:  int = 300
    fts_db:          Optional[str] = None
    prune_empty_dirs: bool = False
    dry_run:         bool = False
    enabled:         bool = True
    compressor:      str = "gzip"
    compress_level:  int = 6
    skip_suffixes:   Tuple[str, ...] = ()
    max_actions_per_scan: int = 0
    check_open_files: bool = False
    allow_hardlinks: bool = False
    config_errors:   Tuple[str, ...] = ()


@dataclass(frozen=True)
class UsageSample:
    total:    int
    used:     int
    free:     int
    used_pct: float
    ts:       float


@dataclass(frozen=True)
class Marks:
    """Resolved trigger/stop points for one tier, in percent used."""

    trigger: float
    stop:    float
    clamped: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FileInfo:
    """Everything the engine knows about a candidate file from the scan stat."""

    path:      str
    rel:       str
    name:      str
    size:      int
    atime_ns:  int
    mtime_ns:  int
    ctime_ns:  int
    uid:       int
    gid:       int
    mode:      int
    nlink:     int
    dev:       int
    ino:       int
    dir_dev:   int
    dir_ino:   int

    @property
    def atime(self) -> float:
        return self.atime_ns / 1e9

    @property
    def mtime(self) -> float:
        return self.mtime_ns / 1e9


@dataclass(frozen=True)
class Verdict:
    eligible: bool
    reason:   Optional[str] = None      # one of eligibility.REJECT_REASONS when not eligible


@dataclass(frozen=True)
class QueueEntry:
    info:   FileInfo
    queue:  str                          # "compress" | "delete"
    tier:   str
    policy: str
    key_ts: float                        # ordering key, epoch seconds
    owner:  str = ""
    group:  str = ""


@dataclass(frozen=True)
class ActionResult:
    outcome:     str                     # ok | dry_run | skipped | failed
    reason:      Optional[str] = None    # skipped / failed reason code
    bytes_freed: int = 0
    duration_s:  float = 0.0
    dest_path:   Optional[str] = None
    error:       Optional[str] = None


@dataclass(frozen=True)
class PassStop:
    """Why act_on_queue() stopped, and where."""

    reason:    str    # stop_reached | exhausted | paused | disabled | shutdown | limit | stalled | error
    acted:     int
    remaining: int
    bytes_freed: int = 0
