"""Disk usage measurement and atime-mode detection (I/O; every caller injects
the measuring function so tests can fake a disk)."""

import json
import os
import shutil
import time
from typing import Callable, Optional, Tuple

from .domain import UsageSample
from .watermarks import used_percent

DiskUsageFn = Callable[[str], Tuple[int, int, int]]


def shutil_usage(path: str) -> Tuple[int, int, int]:
    u = shutil.disk_usage(path)
    return u.total, u.used, u.free


def measure_usage(path: str, disk_usage: DiskUsageFn = shutil_usage,
                  clock: Callable[[], float] = time.time) -> Optional[UsageSample]:
    """A :class:`UsageSample`, or ``None`` when the filesystem cannot be measured.

    ``None`` is a first-class answer: the engine never acts on an area it
    cannot measure.
    """
    try:
        total, used, free = disk_usage(path)
    except OSError:
        return None
    pct = used_percent(total, used, free)
    if pct is None:
        return None
    return UsageSample(total=int(total), used=int(used), free=int(free), used_pct=pct, ts=clock())


def fake_usage_from_file(path: str) -> DiskUsageFn:
    """Test hook: a JSON file mapping area path -> [total, used, free].

    Re-read on every call so a running demo can be steered by editing the file.
    A path missing from the file falls back to the real filesystem.
    """
    def _fn(area_path: str) -> Tuple[int, int, int]:
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        entry = data.get(area_path) or data.get(os.path.realpath(area_path))
        if entry and len(entry) == 3:
            return int(entry[0]), int(entry[1]), int(entry[2])
        return shutil_usage(area_path)
    return _fn


def atime_mode(path: str) -> str:
    """``strictatime`` | ``relatime`` | ``noatime`` | ``unknown`` for the mount holding *path*.

    Under ``noatime`` the LRU ordering key silently degrades to mtime (the key
    is ``max(atime, mtime)``), which the UI reports.
    """
    try:
        st = os.statvfs(path)
    except (OSError, AttributeError):
        return "unknown"
    flag = getattr(st, "f_flag", 0)
    if flag & getattr(os, "ST_NOATIME", 0):
        return "noatime"
    if flag & getattr(os, "ST_RELATIME", 0):
        return "relatime"
    # Linux does not expose relatime through statvfs on every kernel; fall
    # back to the mount table, longest mount-point prefix wins.
    try:
        real = os.path.realpath(path)
        best: Tuple[int, str] = (-1, "")
        with open("/proc/self/mounts", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 4:
                    continue
                mnt, opts = parts[1], parts[3]
                if real == mnt or real.startswith(mnt.rstrip("/") + "/") or mnt == "/":
                    if len(mnt) > best[0]:
                        best = (len(mnt), opts)
        if best[0] >= 0:
            opts = best[1].split(",")
            if "noatime" in opts:
                return "noatime"
            if "relatime" in opts:
                return "relatime"
            if "strictatime" in opts:
                return "strictatime"
            return "relatime"          # Linux default since 2.6.30
    except OSError:
        pass
    return "unknown"


def device_of(path: str) -> Optional[int]:
    try:
        return os.stat(path).st_dev
    except OSError:
        return None
