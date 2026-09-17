"""Per-file eligibility verdicts.  Pure — every input arrives in the context.

A file is eligible for a tier's policy when *none* of the rejection reasons in
:data:`REJECT_REASONS` applies.  The order of checks matters only for which
reason is reported; the cheap structural checks come first.
"""

import fnmatch
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .domain import FileInfo, Verdict
from .exclusions import ExclusionRules, is_excluded
from .fts_gate import FtsSnapshot
from .policies import Policy, ordering_key

REJECT_REASONS: Tuple[str, ...] = (
    "temp_name", "excluded_glob", "not_included", "protected", "manual_exclusion",
    "hardlink", "already_compressed", "compressed_copy_exists", "settling", "too_young",
    "fts_not_complete", "open_file", "outside_area",
)

TEMP_PATTERNS: Tuple[str, ...] = ("*.tmp", "*.part", "*.partial", ".*.swp", "*~",
                                  ".reaper-tmp-*", ".~*")


@dataclass(frozen=True)
class EligibilityContext:
    now:                 float
    policy:              Policy
    min_age:             int
    settle_seconds:      int
    include:             Tuple[str, ...] = ("*",)
    exclude:             Tuple[str, ...] = ()
    protected:           FrozenSet[str] = frozenset()
    manual_exclusions:   ExclusionRules = field(default_factory=ExclusionRules)
    fts:                 Optional[FtsSnapshot] = None
    open_inodes:         Optional[FrozenSet[Tuple[int, int]]] = None
    allow_hardlinks:     bool = False
    compressed_suffixes: Tuple[str, ...] = ()
    area_root:           str = ""
    siblings:            Optional[Dict[str, FrozenSet[str]]] = None   # dir -> names


def is_temp_name(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in TEMP_PATTERNS)


def is_compressed_name(name: str, suffixes: Iterable[str]) -> bool:
    lower = name.lower()
    return any(lower.endswith(s.lower()) for s in suffixes if s)


def matches_any(rel: str, name: str, globs: Iterable[str]) -> bool:
    """A glob matches on the area-relative path or on the basename."""
    for g in globs:
        if fnmatch.fnmatchcase(rel, g) or fnmatch.fnmatchcase(name, g):
            return True
    return False


def compressed_sibling_exists(info: FileInfo, ctx: EligibilityContext) -> bool:
    """``X`` sits next to ``X.gz`` (etc.) — a previous run compressed but did not
    get to unlink the original.  ``X`` may then be deleted but not compressed
    again, so this check is applied to compress policies only."""
    if not ctx.siblings:
        return False
    names = ctx.siblings.get(os.path.dirname(info.path))
    if not names:
        return False
    return any(info.name + s in names for s in ctx.compressed_suffixes if s)


def classify(info: FileInfo, ctx: EligibilityContext) -> Verdict:
    if ctx.area_root:
        root = ctx.area_root.rstrip(os.sep) + os.sep
        if not info.path.startswith(root):
            return Verdict(False, "outside_area")
    if is_temp_name(info.name):
        return Verdict(False, "temp_name")
    if ctx.exclude and matches_any(info.rel, info.name, ctx.exclude):
        return Verdict(False, "excluded_glob")
    if ctx.include and not matches_any(info.rel, info.name, ctx.include):
        return Verdict(False, "not_included")
    if info.path in ctx.protected:
        return Verdict(False, "protected")
    if is_excluded(info.path, ctx.manual_exclusions):
        return Verdict(False, "manual_exclusion")
    if info.nlink > 1 and not ctx.allow_hardlinks:
        return Verdict(False, "hardlink")
    if ctx.policy.kind == "compress":
        if is_compressed_name(info.name, ctx.compressed_suffixes):
            return Verdict(False, "already_compressed")
        if compressed_sibling_exists(info, ctx):
            return Verdict(False, "compressed_copy_exists")
    mtime = info.mtime_ns / 1e9
    if ctx.now - mtime < ctx.settle_seconds:
        return Verdict(False, "settling")
    if ctx.now - ordering_key(info, ctx.policy) < ctx.min_age:
        return Verdict(False, "too_young")
    if ctx.fts is not None and not ctx.fts.allows(info.path, ctx.compressed_suffixes):
        return Verdict(False, "fts_not_complete")
    if ctx.open_inodes is not None and (info.dev, info.ino) in ctx.open_inodes:
        return Verdict(False, "open_file")
    return Verdict(True)


def partition(infos: Iterable[FileInfo],
              ctx: EligibilityContext) -> Tuple[List[FileInfo], Dict[str, int]]:
    eligible: List[FileInfo] = []
    rejects: Dict[str, int] = {}
    for info in infos:
        verdict = classify(info, ctx)
        if verdict.eligible:
            eligible.append(info)
        else:
            rejects[verdict.reason] = rejects.get(verdict.reason, 0) + 1
    return eligible, rejects
