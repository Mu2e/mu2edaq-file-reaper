"""Human-readable formatting helpers shared by the poller, API and templates."""

#: Em dash used everywhere a value is unknown or not applicable.
DASH = "—"

#: Binary (IEC) unit suffixes, used for *display* of every byte count.
_IEC_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def fmt_duration(seconds) -> str:
    """Convert a number of seconds into a human-readable string."""
    if seconds is None or seconds < 0:
        return DASH
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s   = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_bytes(n) -> str:
    """Format a byte count as a human-readable IEC string (e.g. '3.7 GiB').

    Display is always binary/IEC even when the configured threshold was written
    in SI units, so a ``2TB`` threshold renders as ``1.8 TiB``.
    """
    if n is None:
        return DASH
    for unit in _IEC_UNITS:
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} EiB"


def fmt_pct(value) -> str:
    """Format a percentage to one decimal place, or the em dash if unknown."""
    if value is None:
        return DASH
    return f"{value:.1f}%"
