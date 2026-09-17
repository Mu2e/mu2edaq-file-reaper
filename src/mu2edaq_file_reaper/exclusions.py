"""Manual exclusions: a thread-safe in-memory mirror of the ``exclusions`` table.

Workers fetch an immutable :class:`ExclusionRules` before *every* action so an
exclusion added from the web UI lands before the next file is touched.
"""

import fnmatch
import os
import threading
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class ExclusionRules:
    exact: FrozenSet[str] = frozenset()
    globs: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.exact or self.globs)


EMPTY_RULES = ExclusionRules()


def is_excluded(path: str, rules: ExclusionRules) -> bool:
    """Pure: exact realpath match, or fnmatch on the full path *or* basename."""
    if not rules:
        return False
    if path in rules.exact:
        return True
    name = os.path.basename(path)
    for pattern in rules.globs:
        if fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
        # A directory pattern such as "/data/raw/keep" excludes everything under it.
        if not any(ch in pattern for ch in "*?[") and path.startswith(pattern.rstrip("/") + "/"):
            return True
    return False


def rules_from_rows(rows: Iterable[dict]) -> Dict[Optional[str], ExclusionRules]:
    """Group active rule dicts (from ExclusionRepo) by area (None = global)."""
    exact: Dict[Optional[str], set] = {}
    globs: Dict[Optional[str], list] = {}
    for row in rows:
        if not row.get("active", True):
            continue
        area = row.get("area")
        pattern = row["pattern"]
        if row.get("kind") == "glob" or any(ch in pattern for ch in "*?["):
            globs.setdefault(area, []).append(pattern)
        else:
            exact.setdefault(area, set()).add(os.path.normpath(pattern))
            # also match as a directory prefix
            globs.setdefault(area, []).append(os.path.normpath(pattern))
    out: Dict[Optional[str], ExclusionRules] = {}
    for area in set(exact) | set(globs):
        out[area] = ExclusionRules(exact=frozenset(exact.get(area, ())),
                                   globs=tuple(globs.get(area, ())))
    return out


class ExclusionRegistry:
    def __init__(self, repo=None) -> None:
        self._repo = repo
        self._lock = threading.Lock()
        self._by_area: Dict[Optional[str], ExclusionRules] = {}
        self._merged: Dict[str, ExclusionRules] = {}

    def reload(self) -> None:
        rows = self._repo.active_rules() if self._repo is not None else []
        self.load_rows(rows)

    def load_rows(self, rows: Iterable[dict]) -> None:
        by_area = rules_from_rows(rows)
        with self._lock:
            self._by_area = by_area
            self._merged = {}

    def rules_for(self, area: str) -> ExclusionRules:
        with self._lock:
            cached = self._merged.get(area)
            if cached is not None:
                return cached
            glob_rules = self._by_area.get(None, EMPTY_RULES)
            area_rules = self._by_area.get(area, EMPTY_RULES)
            merged = ExclusionRules(exact=glob_rules.exact | area_rules.exact,
                                    globs=glob_rules.globs + area_rules.globs)
            self._merged[area] = merged
            return merged
