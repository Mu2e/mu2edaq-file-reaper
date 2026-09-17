"""YAML loading and area construction.

Nothing here raises for a *bad* configuration and nothing calls ``sys.exit``:
a control-room daemon must keep running on a partly wrong config.  Problems
accumulate as human-readable strings shown on ``/config`` and
``/api/v1/config``; an area with fatal errors is dropped (and listed) rather
than silently accepted.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .domain import (
    DEFAULT_MIN_AGE,
    DEFAULT_THRESHOLDS,
    DEFAULT_TIER_POLICY,
    POLICY_NAMES,
    TIER_ORDER,
    AreaConfig,
    TierConfig,
)
from .units import UnitError, parse_duration, parse_percent
from .watermarks import check_tier_config

#: Top-level keys the loader understands; anything else is reported.
TOP_KEYS = {"reaper", "database", "discovery", "api", "defaults", "areas", "notifications"}
AREA_KEYS = {"path", "label", "name", "enabled", "thresholds", "tiers", "include", "exclude",
             "settle_seconds", "fts_db", "prune_empty_dirs", "dry_run", "compression",
             "max_actions_per_scan", "check_open_files", "allow_hardlinks"}
TIER_KEYS = {"policy", "min_age", "low_water", "high_water", "compressor", "threshold"}

#: Realpaths that must never be an area root.
SYSTEM_ROOTS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc",
    "/root", "/run", "/sbin", "/srv", "/sys", "/usr", "/var",
    # macOS keeps several of these under /private and symlinks the top-level names.
    "/private", "/private/etc", "/private/var", "/System", "/Library", "/Applications",
})

DEFAULT_EXCLUDE = ("*.tmp", "*.part", ".~*", "*.pid", "*.db", "*.db-wal", "*.db-shm",
                   "*.db-journal", ".reaper-tmp-*")
DEFAULT_SKIP_SUFFIXES = (".gz", ".bz2", ".xz", ".zst", ".zip", ".7z", ".lz4",
                         ".tgz", ".tbz2", ".txz")

_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def load_config(path: str, required: bool = False) -> Dict[str, Any]:
    """Read *path*.  A missing file is fatal only when *required*."""
    if not os.path.isfile(path):
        if required:
            raise SystemExit(f"[Config] Error: config file not found: {path}")
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"[Config] Error: {path} must contain a YAML mapping at top level")
    return data


def area_name_from_path(path: str) -> str:
    """``/data/raw`` -> ``data-raw``; used as the URL/API identifier."""
    stripped = path.strip("/").replace("/", "-") or "root"
    return _NAME_RE.sub("-", stripped)


def _as_list(value, what: str, issues: List[str], default: Tuple[str, ...]) -> Tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return tuple(value)
    issues.append(f"{what}: expected a list of strings, got {value!r}; using default")
    return default


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """One-level-deep merge for the defaults: / area: blocks."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            for k2, v2 in value.items():
                if isinstance(v2, dict) and isinstance(merged.get(k2), dict):
                    merged[k2] = {**merged[k2], **v2}
                else:
                    merged[k2] = v2
            out[key] = merged
        else:
            out[key] = value
    return out


def _build_tiers(spec: Dict[str, Any], prefix: str,
                 issues: List[str]) -> Dict[str, TierConfig]:
    thresholds_cfg = spec.get("thresholds") or {}
    tiers_cfg = spec.get("tiers")
    if tiers_cfg is None:
        tiers_cfg = {name: {} for name in TIER_ORDER}
    if not isinstance(tiers_cfg, dict):
        issues.append(f"{prefix}: tiers must be a mapping; using defaults")
        tiers_cfg = {name: {} for name in TIER_ORDER}
    if not isinstance(thresholds_cfg, dict):
        issues.append(f"{prefix}: thresholds must be a mapping; using defaults")
        thresholds_cfg = {}

    tiers: Dict[str, TierConfig] = {}
    for name, tcfg in tiers_cfg.items():
        if name not in TIER_ORDER:
            issues.append(f"{prefix}: unknown tier {name!r} (expected one of {', '.join(TIER_ORDER)})")
            continue
        if tcfg is None or tcfg is False:
            continue                                    # `warning: null` disables the tier
        if not isinstance(tcfg, dict):
            issues.append(f"{prefix}: tier {name} must be a mapping")
            continue
        for key in tcfg:
            if key not in TIER_KEYS:
                issues.append(f"{prefix}: tier {name}: unknown key {key!r}")
        try:
            raw_thr = tcfg.get("threshold", thresholds_cfg.get(name, DEFAULT_THRESHOLDS[name]))
            threshold = parse_percent(raw_thr)
        except UnitError as exc:
            issues.append(f"{prefix}: tier {name}: bad threshold: {exc}; tier dropped")
            continue
        policy = tcfg.get("policy", DEFAULT_TIER_POLICY[name])
        if policy not in POLICY_NAMES:
            issues.append(f"{prefix}: tier {name}: unknown policy {policy!r} "
                          f"(expected one of {', '.join(POLICY_NAMES)}); tier dropped")
            continue
        try:
            min_age = parse_duration(tcfg.get("min_age", DEFAULT_MIN_AGE[name]))
        except UnitError as exc:
            issues.append(f"{prefix}: tier {name}: bad min_age: {exc}; tier dropped")
            continue
        try:
            low = parse_percent(tcfg.get("low_water", 10), lo=0.0, hi=100.0, inclusive_lo=True)
            high = parse_percent(tcfg.get("high_water", 0), lo=0.0, hi=100.0, inclusive_lo=True)
        except UnitError as exc:
            issues.append(f"{prefix}: tier {name}: bad water mark: {exc}; tier dropped")
            continue
        compressor = tcfg.get("compressor")
        tiers[name] = TierConfig(name=name, threshold=threshold, policy=policy, min_age=min_age,
                                 high_water=high, low_water=low,
                                 compressor=str(compressor) if compressor else None)
    for msg in check_tier_config(tiers):
        issues.append(f"{prefix}: {msg}")
    return tiers


def build_areas(cfg: Dict[str, Any], allow_shallow_root: bool = False,
                ) -> Tuple[List[AreaConfig], List[str]]:
    """Turn the ``defaults:`` and ``areas:`` blocks into :class:`AreaConfig` objects."""
    issues: List[str] = []
    for key in cfg:
        if key not in TOP_KEYS:
            issues.append(f"unknown top-level key {key!r}")

    defaults = cfg.get("defaults") or {}
    if not isinstance(defaults, dict):
        issues.append("defaults: must be a mapping; ignored")
        defaults = {}

    raw_areas = cfg.get("areas") or []
    if not isinstance(raw_areas, list):
        issues.append("areas: must be a list")
        raw_areas = []

    areas: List[AreaConfig] = []
    seen_names = set()
    for idx, raw in enumerate(raw_areas):
        prefix = f"areas[{idx}]"
        if not isinstance(raw, dict) or not raw.get("path"):
            issues.append(f"{prefix}: every area needs a 'path'; entry skipped")
            continue
        for key in raw:
            if key not in AREA_KEYS:
                issues.append(f"{prefix}: unknown key {key!r}")
        path = str(raw["path"])
        prefix = f"area {path}"
        spec = _merge(defaults, raw)
        area_errors: List[str] = []

        expanded = os.path.abspath(os.path.expanduser(path))
        real = os.path.realpath(expanded)
        if real in SYSTEM_ROOTS or expanded.rstrip("/") in SYSTEM_ROOTS:
            issues.append(f"{prefix}: refusing to manage system root {real}; area dropped")
            continue
        depth = len([p for p in real.split(os.sep) if p])
        if depth < 2 and not allow_shallow_root:
            issues.append(f"{prefix}: {real} is a top-level directory; set "
                          f"reaper.allow_shallow_root to manage it; area dropped")
            continue
        if not os.path.isdir(real):
            area_errors.append(f"path does not exist or is not a directory: {real}")

        name = str(raw.get("name") or area_name_from_path(path))
        if name in seen_names:
            issues.append(f"{prefix}: duplicate area name {name!r}; area dropped")
            continue
        seen_names.add(name)

        tiers = _build_tiers(spec, prefix, issues)
        if not tiers:
            issues.append(f"{prefix}: no tiers configured; area is monitor-only")

        comp = spec.get("compression") or {}
        if not isinstance(comp, dict):
            issues.append(f"{prefix}: compression must be a mapping; using defaults")
            comp = {}
        try:
            settle = parse_duration(spec.get("settle_seconds", 300))
        except UnitError as exc:
            issues.append(f"{prefix}: bad settle_seconds: {exc}; using 300")
            settle = 300

        fts_db = spec.get("fts_db")
        if fts_db:
            fts_db = os.path.realpath(os.path.expanduser(str(fts_db)))
            if not os.path.isfile(fts_db):
                area_errors.append(f"fts_db not found: {fts_db} (nothing will be eligible "
                                   f"until it appears)")

        areas.append(AreaConfig(
            name=name, path=path, real_path=real, label=str(spec.get("label") or path),
            tiers=tiers,
            include=_as_list(spec.get("include"), f"{prefix}: include", issues, ("*",)),
            exclude=_as_list(spec.get("exclude"), f"{prefix}: exclude", issues, DEFAULT_EXCLUDE),
            settle_seconds=settle,
            fts_db=fts_db or None,
            prune_empty_dirs=bool(spec.get("prune_empty_dirs", False)),
            dry_run=bool(spec.get("dry_run", False)),
            enabled=bool(spec.get("enabled", True)),
            compressor=str(comp.get("algorithm", "gzip")),
            compress_level=int(comp.get("level", 6)),
            skip_suffixes=_as_list(comp.get("skip_suffixes"), f"{prefix}: compression.skip_suffixes",
                                   issues, DEFAULT_SKIP_SUFFIXES),
            max_actions_per_scan=int(spec.get("max_actions_per_scan", 0) or 0),
            check_open_files=bool(spec.get("check_open_files", False)),
            allow_hardlinks=bool(spec.get("allow_hardlinks", False)),
            config_errors=tuple(area_errors),
        ))
        for err in area_errors:
            issues.append(f"{prefix}: {err}")
    return areas, issues


def area_to_dict(area: AreaConfig) -> Dict[str, Any]:
    """JSON-friendly view of an area for /config and the API."""
    return {
        "name": area.name, "path": area.path, "real_path": area.real_path, "label": area.label,
        "enabled": area.enabled, "dry_run": area.dry_run,
        "tiers": {n: {"threshold": t.threshold, "policy": t.policy, "min_age": t.min_age,
                      "low_water": t.low_water, "high_water": t.high_water,
                      "compressor": t.compressor}
                  for n, t in area.tiers.items()},
        "include": list(area.include), "exclude": list(area.exclude),
        "settle_seconds": area.settle_seconds, "fts_db": area.fts_db,
        "prune_empty_dirs": area.prune_empty_dirs, "compressor": area.compressor,
        "compress_level": area.compress_level, "skip_suffixes": list(area.skip_suffixes),
        "max_actions_per_scan": area.max_actions_per_scan,
        "check_open_files": area.check_open_files, "allow_hardlinks": area.allow_hardlinks,
        "config_errors": list(area.config_errors),
    }
