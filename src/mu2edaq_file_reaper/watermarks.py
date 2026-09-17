"""Trigger / stop math, hysteresis and tier selection.

Pure functions only — no I/O, no globals, no clock.  This module decides when
the reaper starts deleting and when it stops, so everything in it is directly
unit-tested at the boundaries.

Convention: every percentage here is **percent of capacity used**, computed the
way ``df`` does (``100 * used / (used + free)``) so operators can compare the
numbers with ``df -h`` on the node.

For a tier with threshold θ, ``high_water`` h and ``low_water`` l::

    trigger = min(θ + h, 100)
    stop    = max(θ - l, 0)

    active  = used > stop        if the tier was active on the previous evaluation
            = used >= trigger    otherwise

Comparisons are inclusive on the *bad* side: reaching the trigger exactly
activates, reaching the stop exactly deactivates.
"""

from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

from .domain import TIER_ORDER, Marks, TierConfig, UsageSample


def used_percent(total: int, used: int, free: int) -> Optional[float]:
    """``df`` convention: percent of (used + free) that is used, or None."""
    denom = used + free
    if total <= 0 or denom <= 0 or used < 0 or free < 0:
        return None
    return 100.0 * used / denom


def resolve_marks(tier: TierConfig) -> Marks:
    """Trigger and stop points for *tier*, clamped to [0, 100]."""
    clamped: List[str] = []
    trigger = tier.threshold + tier.high_water
    if trigger > 100.0:
        trigger = 100.0
        clamped.append("trigger_100")
    stop = tier.threshold - tier.low_water
    if stop < 0.0:
        stop = 0.0
        clamped.append("stop_0")
    return Marks(trigger=trigger, stop=stop, clamped=tuple(clamped))


def tier_is_active(used_pct: float, marks: Marks, was_active: bool) -> bool:
    """Hysteresis: stay active until at or below stop; else enter at or above trigger."""
    if was_active:
        return used_pct > marks.stop
    return used_pct >= marks.trigger


def evaluate_tiers(used_pct: float, tiers: Dict[str, TierConfig],
                   previously_active: FrozenSet[str]) -> FrozenSet[str]:
    """Return the set of tier names active at *used_pct*."""
    active = set()
    for name, cfg in tiers.items():
        if tier_is_active(used_pct, resolve_marks(cfg), name in previously_active):
            active.add(name)
    return frozenset(active)


def highest_active(active: Iterable[str],
                   exclude: FrozenSet[str] = frozenset()) -> Optional[str]:
    """Most severe active tier by name order (full > critical > warning)."""
    candidates = [t for t in active if t not in exclude]
    if not candidates:
        return None
    return max(candidates, key=lambda t: TIER_ORDER.index(t) if t in TIER_ORDER else -1)


def transitions(before: FrozenSet[str],
                after: FrozenSet[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``(activated, deactivated)`` in severity order."""
    order = {t: i for i, t in enumerate(TIER_ORDER)}
    key = lambda t: order.get(t, -1)     # noqa: E731
    up   = tuple(sorted(after - before, key=key))
    down = tuple(sorted(before - after, key=key))
    return up, down


def should_continue(used_pct: float, marks: Marks) -> bool:
    """Keep acting while used space is still above the stop point."""
    return used_pct > marks.stop


def policy_insufficient(used_pct: float, tier: TierConfig) -> bool:
    """After a tier's queue is exhausted: still above the *threshold* itself?"""
    return used_pct > tier.threshold


def bytes_to_stop(usage: UsageSample, marks: Marks) -> int:
    """How many bytes must be freed to bring used_pct down to the stop point."""
    denom = usage.used + usage.free
    if denom <= 0:
        return 0
    target_used = denom * marks.stop / 100.0
    need = usage.used - target_used
    return max(0, int(round(need)))


def simulate_after(usage: UsageSample, bytes_freed: int) -> UsageSample:
    """Dry-run arithmetic: the sample after freeing *bytes_freed*."""
    freed = max(0, min(bytes_freed, usage.used))
    used = usage.used - freed
    free = usage.free + freed
    pct = used_percent(usage.total, used, free)
    return UsageSample(total=usage.total, used=used, free=free,
                       used_pct=pct if pct is not None else usage.used_pct, ts=usage.ts)


def check_tier_config(tiers: Dict[str, TierConfig]) -> List[str]:
    """Human-readable warnings about a tier block that is legal but odd."""
    issues: List[str] = []
    for name in TIER_ORDER:
        cfg = tiers.get(name)
        if cfg is None:
            continue
        marks = resolve_marks(cfg)
        if "trigger_100" in marks.clamped:
            issues.append(f"tier {name}: threshold {cfg.threshold:g}% + high_water "
                          f"{cfg.high_water:g}% exceeds 100%; it can only trigger on a "
                          f"completely full disk")
        if "stop_0" in marks.clamped:
            issues.append(f"tier {name}: threshold {cfg.threshold:g}% - low_water "
                          f"{cfg.low_water:g}% is below 0%; stop point clamped to 0% so the "
                          f"policy runs until no eligible files remain")
    present = [(n, tiers[n].threshold) for n in TIER_ORDER if n in tiers]
    for (n1, t1), (n2, t2) in zip(present, present[1:]):
        if t1 >= t2:
            issues.append(f"tiers {n1} ({t1:g}%) and {n2} ({t2:g}%) do not ascend; "
                          f"{n2} still runs first when both are active")
    return issues
