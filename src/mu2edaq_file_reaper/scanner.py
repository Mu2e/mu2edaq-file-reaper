"""Safe recursive enumeration of an area (I/O).

* iterative ``os.scandir`` — no recursion limit, no ``os.walk`` symlink surprises;
* never follows symlinks (files or directories);
* never crosses a filesystem boundary (``st_dev`` must match the area root);
* records the parent directory's ``(dev, ino)`` so the action layer can prove
  the directory it opens later is the one that was scanned;
* errors while walking are counted, never raised.
"""

import os
import stat
import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterator, List, Optional, Set, Tuple

from .domain import AreaConfig, FileInfo

TEMP_PREFIX = ".reaper-tmp-"


@dataclass
class ScanStats:
    files:        int = 0
    dirs:         int = 0
    symlinks:     int = 0
    other:        int = 0
    other_fs:     int = 0
    errors:       int = 0
    bytes_total:  int = 0
    error_samples: List[str] = field(default_factory=list)
    siblings:     Dict[str, FrozenSet[str]] = field(default_factory=dict)


def enumerate_files(area: AreaConfig, root_dev: Optional[int] = None,
                    protected: FrozenSet[str] = frozenset(),
                    stats: Optional[ScanStats] = None,
                    collect_siblings: bool = True) -> Iterator[FileInfo]:
    root = area.real_path
    stats = stats if stats is not None else ScanStats()
    if root_dev is None:
        try:
            root_dev = os.stat(root).st_dev
        except OSError as exc:
            stats.errors += 1
            stats.error_samples.append(f"{root}: {exc}")
            return
    stack = [root]
    while stack:
        dirpath = stack.pop()
        try:
            dst = os.stat(dirpath, follow_symlinks=False)
        except OSError as exc:
            stats.errors += 1
            if len(stats.error_samples) < 20:
                stats.error_samples.append(f"{dirpath}: {exc}")
            continue
        if not stat.S_ISDIR(dst.st_mode):
            continue
        if dst.st_dev != root_dev:
            stats.other_fs += 1
            continue
        stats.dirs += 1
        names: Set[str] = set()
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    names.add(entry.name)
                    try:
                        if entry.is_symlink():
                            stats.symlinks += 1
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            stats.other += 1
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        stats.errors += 1
                        if len(stats.error_samples) < 20:
                            stats.error_samples.append(f"{entry.path}: {exc}")
                        continue
                    if st.st_dev != root_dev:
                        stats.other_fs += 1
                        continue
                    path = entry.path
                    if path in protected:
                        continue
                    stats.files += 1
                    stats.bytes_total += st.st_size
                    yield FileInfo(
                        path=path, rel=os.path.relpath(path, root), name=entry.name,
                        size=st.st_size, atime_ns=st.st_atime_ns, mtime_ns=st.st_mtime_ns,
                        ctime_ns=st.st_ctime_ns, uid=st.st_uid, gid=st.st_gid,
                        mode=st.st_mode, nlink=st.st_nlink, dev=st.st_dev, ino=st.st_ino,
                        dir_dev=dst.st_dev, dir_ino=dst.st_ino,
                    )
        except OSError as exc:
            stats.errors += 1
            if len(stats.error_samples) < 20:
                stats.error_samples.append(f"{dirpath}: {exc}")
            continue
        if collect_siblings:
            stats.siblings[dirpath] = frozenset(names)


def sweep_temp_files(area: AreaConfig, older_than_s: int,
                     clock=time.time) -> int:
    """Remove ``.reaper-tmp-*`` leftovers from a crashed compression, if old enough."""
    removed = 0
    now = clock()
    stack = [area.real_path]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if entry.name.startswith(TEMP_PREFIX) and entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if now - st.st_mtime >= older_than_s:
                                os.unlink(entry.path)
                                removed += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return removed


def open_inodes_proc() -> Optional[FrozenSet[Tuple[int, int]]]:
    """``(dev, ino)`` of every file open by a visible process, via /proc.  ``None``
    when /proc is unavailable (macOS) — callers then skip the check."""
    if not os.path.isdir("/proc/self/fd"):
        return None
    out: Set[Tuple[int, int]] = set()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        fd_dir = f"/proc/{pid}/fd"
        try:
            for fd in os.listdir(fd_dir):
                try:
                    st = os.stat(os.path.join(fd_dir, fd))
                    if stat.S_ISREG(st.st_mode):
                        out.add((st.st_dev, st.st_ino))
                except OSError:
                    continue
        except OSError:
            continue
    return frozenset(out)
