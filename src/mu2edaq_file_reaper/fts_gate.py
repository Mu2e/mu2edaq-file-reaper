"""Guard against deleting data the File Transfer Service has not yet shipped.

Reads the ``files`` table of a ``mu2edaq-fts`` SQLite database **read-only**
and builds an immutable snapshot of every path whose status says the bytes are
safely elsewhere.  An unreadable database yields :data:`EMPTY_SNAPSHOT`, so
the gate fails *closed*: nothing in the area is eligible until FTS is readable
again.
"""

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import FrozenSet, Iterable, Optional

ELIGIBLE_STATUSES = frozenset({"COMPLETED", "DELETED"})


class FtsUnavailable(OSError):
    pass


@dataclass(frozen=True)
class FtsSnapshot:
    paths:     FrozenSet[str]
    loaded_at: float
    row_count: int
    available: bool = True

    def allows(self, path: str, compressed_suffixes: Iterable[str] = ()) -> bool:
        """Pure: *path*, or *path* minus a compression suffix the reaper may have
        added, is a transferred file."""
        if path in self.paths:
            return True
        for suffix in compressed_suffixes:
            if suffix and path.endswith(suffix) and path[: -len(suffix)] in self.paths:
                return True
        return False


EMPTY_SNAPSHOT = FtsSnapshot(paths=frozenset(), loaded_at=0.0, row_count=0, available=False)


def load_fts_snapshot(db_path: str, timeout: float = 5.0,
                      clock=time.time) -> FtsSnapshot:
    if not os.path.isfile(db_path):
        raise FtsUnavailable(f"FTS database not found: {db_path}")
    uri = "file:%s?mode=ro" % db_path
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    except sqlite3.Error as exc:
        raise FtsUnavailable(f"cannot open FTS database {db_path}: {exc}") from exc
    try:
        placeholders = ",".join("?" for _ in ELIGIBLE_STATUSES)
        cur = conn.execute(
            f"SELECT filepath, compressed_path FROM files WHERE status IN ({placeholders})",
            tuple(sorted(ELIGIBLE_STATUSES)))
        paths = set()
        count = 0
        for filepath, compressed in cur:
            count += 1
            for p in (filepath, compressed):
                if p:
                    paths.add(os.path.normpath(p))
                    try:
                        paths.add(os.path.realpath(p))
                    except OSError:
                        pass
    except sqlite3.Error as exc:
        raise FtsUnavailable(f"cannot read FTS database {db_path}: {exc}") from exc
    finally:
        conn.close()
    return FtsSnapshot(paths=frozenset(paths), loaded_at=clock(), row_count=count)
