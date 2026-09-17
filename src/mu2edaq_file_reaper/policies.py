"""Policy registry, ordering keys and queue construction.  Pure."""

from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional

from .domain import FileInfo, QueueEntry


@dataclass(frozen=True)
class Policy:
    name: str
    kind: str      # delete | compress
    key:  str      # lru | age


POLICIES: Dict[str, Policy] = {
    "LRU-Delete":   Policy("LRU-Delete",   "delete",   "lru"),
    "Age-Delete":   Policy("Age-Delete",   "delete",   "age"),
    "LRU-Compress": Policy("LRU-Compress", "compress", "lru"),
    "Age-Compress": Policy("Age-Compress", "compress", "age"),
}


def ordering_key(info: FileInfo, policy: Policy) -> float:
    """Epoch seconds: LRU = most recent of atime/mtime; Age = mtime."""
    if policy.key == "lru":
        return max(info.atime_ns, info.mtime_ns) / 1e9
    return info.mtime_ns / 1e9


def build_queue(eligible: Iterable[FileInfo], policy: Policy, tier: str,
                owner_lookup: Optional[Callable[[int, int], tuple]] = None) -> List[QueueEntry]:
    """Oldest key first; ties broken by path so the order is deterministic."""
    entries = []
    for info in eligible:
        owner, group = ("", "")
        if owner_lookup is not None:
            try:
                owner, group = owner_lookup(info.uid, info.gid)
            except Exception:
                owner, group = (str(info.uid), str(info.gid))
        entries.append(QueueEntry(info=info, queue=policy.kind, tier=tier, policy=policy.name,
                                  key_ts=ordering_key(info, policy), owner=owner, group=group))
    entries.sort(key=lambda e: (e.key_ts, e.info.path))
    return entries


def diff_new_paths(current: Iterable[str], previous: FrozenSet[str]) -> List[str]:
    return [p for p in current if p not in previous]


def owner_names(uid: int, gid: int) -> tuple:
    """Resolve uid/gid to names, falling back to the numbers."""
    try:
        import grp
        import pwd
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        try:
            group = grp.getgrgid(gid).gr_name
        except KeyError:
            group = str(gid)
        return user, group
    except ImportError:                    # Windows
        return str(uid), str(gid)
