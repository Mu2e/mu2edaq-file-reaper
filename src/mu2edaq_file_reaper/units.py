"""Parsing of sizes, percentages and durations as written in the YAML.

Pure functions only.  Sizes follow the diskwatcher grammar (SI ``500 GB`` and
IEC ``500 GiB`` units, bare numbers are bytes); percentages are ``"80%"`` or a
bare number when the caller says a bare number means percent; durations are
``"90s" "10m" "6h" "7d" "2w"`` or bare seconds.
"""

import re
from typing import Union

#: Unit suffix -> multiplier, case-insensitive lookup.  Display is always IEC.
UNITS = {
    "":    1,
    "B":   1,
    "K":   1000,          "KB":  1000,
    "M":   1000 ** 2,     "MB":  1000 ** 2,
    "G":   1000 ** 3,     "GB":  1000 ** 3,
    "T":   1000 ** 4,     "TB":  1000 ** 4,
    "P":   1000 ** 5,     "PB":  1000 ** 5,
    "KIB": 1024,
    "MIB": 1024 ** 2,
    "GIB": 1024 ** 3,
    "TIB": 1024 ** 4,
    "PIB": 1024 ** 5,
}

_DURATION_UNITS = {"": 1, "S": 1, "M": 60, "H": 3600, "D": 86400, "W": 604800}

_PERCENT_RE  = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")
_SIZE_RE     = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z]*)$")
_DURATION_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*([A-Za-z]*)$")


class UnitError(ValueError):
    """Raised for a value that cannot be understood."""


def _reject_bool(value, what: str) -> None:
    if isinstance(value, bool):
        # bool is an int subclass; a bare `true` is certainly a mistake.
        raise UnitError(f"unrecognised {what}: {value!r}")


def parse_size(value: Union[int, float, str]) -> int:
    """Parse ``"500 GiB"``, ``"2TB"``, ``1048576`` into a byte count."""
    _reject_bool(value, "size")
    if isinstance(value, (int, float)):
        if value < 0:
            raise UnitError(f"size cannot be negative: {value!r}")
        return int(round(value))
    if not isinstance(value, str):
        raise UnitError(f"unrecognised size: {value!r}")
    text = value.strip()
    if not text:
        raise UnitError("size is empty")
    match = _SIZE_RE.match(text)
    if not match:
        raise UnitError(f"unrecognised size: {value!r}")
    number, suffix = match.group(1), match.group(2).upper()
    if suffix not in UNITS:
        raise UnitError(f"unknown size unit {match.group(2)!r} in {value!r}")
    amount = float(number)
    if amount < 0:
        raise UnitError(f"size cannot be negative: {value!r}")
    return int(round(amount * UNITS[suffix]))


def parse_percent(value: Union[int, float, str], lo: float = 0.0, hi: float = 100.0,
                  inclusive_lo: bool = False) -> float:
    """Parse ``80``, ``"80"`` or ``"80%"`` into a float within (lo, hi].

    *inclusive_lo* allows exactly *lo* (used for water marks, where 0 is valid).
    """
    _reject_bool(value, "percentage")
    if isinstance(value, (int, float)):
        pct = float(value)
    elif isinstance(value, str):
        text = value.strip()
        match = _PERCENT_RE.match(text)
        if match:
            pct = float(match.group(1))
        else:
            try:
                pct = float(text)
            except ValueError:
                raise UnitError(f"unrecognised percentage: {value!r}") from None
    else:
        raise UnitError(f"unrecognised percentage: {value!r}")
    if pct > hi or pct < lo or (pct == lo and not inclusive_lo):
        bound = "[" if inclusive_lo else "("
        raise UnitError(f"percentage must be in {bound}{lo:g}, {hi:g}]: {value!r}")
    return pct


def parse_duration(value: Union[int, float, str]) -> int:
    """Parse ``"7d"``, ``"90s"``, ``"1.5h"`` or bare seconds into whole seconds."""
    _reject_bool(value, "duration")
    if isinstance(value, (int, float)):
        if value < 0:
            raise UnitError(f"duration cannot be negative: {value!r}")
        return int(round(value))
    if not isinstance(value, str):
        raise UnitError(f"unrecognised duration: {value!r}")
    text = value.strip()
    if not text:
        raise UnitError("duration is empty")
    match = _DURATION_RE.match(text)
    if not match:
        raise UnitError(f"unrecognised duration: {value!r}")
    number, suffix = match.group(1), match.group(2).upper()
    if suffix not in _DURATION_UNITS:
        raise UnitError(f"unknown duration unit {match.group(2)!r} in {value!r}")
    amount = float(number)
    if amount < 0:
        raise UnitError(f"duration cannot be negative: {value!r}")
    return int(round(amount * _DURATION_UNITS[suffix]))
