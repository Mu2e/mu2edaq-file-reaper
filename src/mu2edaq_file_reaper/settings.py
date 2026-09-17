"""Process-wide runtime settings.

Configuration layers, lowest priority first::

    dataclass defaults -> YAML config file -> .env file -> environment -> command line

Everything lives on a single mutable :class:`Settings` instance reached through
:func:`get_settings`.

.. important::
   Call ``get_settings()`` **inside** function bodies.  Never bind it at module
   scope and never ``from .settings import web_port`` — both capture a value
   before :func:`~mu2edaq_file_reaper.cli.main` has applied the config file,
   the environment and the CLI flags.  ``main()`` only ever calls
   :meth:`Settings.apply`; it never rebinds the singleton.
"""

import os
import socket
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional

from .domain import AreaConfig

#: Prefix for every environment-variable override.
ENV_PREFIX = "MU2EDAQ_FILE_REAPER_"

#: Environment variable name -> (settings attribute, coercion).  ``None`` = bool.
_ENV_MAP = {
    ENV_PREFIX + "CONFIG":         ("config_path",    str),
    ENV_PREFIX + "LABEL":          ("label",          str),
    ENV_PREFIX + "WEB_HOST":       ("web_host",       str),
    ENV_PREFIX + "WEB_PORT":       ("web_port",       int),
    ENV_PREFIX + "API_PORT":       ("api_port",       int),
    ENV_PREFIX + "SCAN_INTERVAL":  ("scan_interval",  int),
    ENV_PREFIX + "DRY_RUN":        ("dry_run",        None),
    ENV_PREFIX + "DAEMON":         ("daemon",         None),
    ENV_PREFIX + "PID_FILE":       ("pid_file",       str),
    ENV_PREFIX + "LOG_FILE":       ("log_file",       str),
    ENV_PREFIX + "VERBOSE":        ("verbose",        None),
    ENV_PREFIX + "ADMIN_TOKEN":    ("admin_token",    str),
    ENV_PREFIX + "WORKERS":        ("workers",        int),
    ENV_PREFIX + "DATABASE_URL":   ("database_url",   str),
    ENV_PREFIX + "OPEN_READ":      ("api_open_read",  None),
    ENV_PREFIX + "DISCOVERY":      ("discovery_enabled", None),
}

_TRUE  = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(raw: str) -> Optional[bool]:
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def default_label() -> str:
    """Short hostname, the default instance label."""
    return socket.gethostname().split(".")[0]


@dataclass
class Settings:
    """Scalar settings plus the parsed area list and notification config."""

    # ---- reaper: ----
    label:           str = field(default_factory=default_label)
    web_host:        str = "0.0.0.0"
    web_port:        int = 5004
    api_port:        Optional[int] = None
    scan_interval:   int = 600
    dry_run:         bool = False
    daemon:          bool = False
    pid_file:        Optional[str] = None
    log_file:        Optional[str] = None
    log_max_bytes:   int = 10 * 1024 * 1024
    log_backup_count: int = 5
    verbose:         bool = False
    admin_token:     str = ""
    workers:         int = 4
    max_passes_per_scan: int = 3
    remeasure_every: int = 1
    stall_window:    int = 20
    shutdown_grace_s: int = 30
    queue_publish_limit: int = 1000
    history_log_queue_adds: str = "new_only"      # all | new_only | none
    compress_verify: str = "crc"                  # crc | size | none
    compress_min_ratio: float = 0.95
    compress_assumed_ratio: float = 0.5           # dry-run estimate
    protected_paths: List[str] = field(default_factory=list)
    allow_shallow_root: bool = False
    fake_usage_file: Optional[str] = None         # test hook: JSON {path: [total, used, free]}

    # ---- database: ----
    database_url:    str = "sqlite:///./data/file-reaper.db"
    history_retention_days: int = 365

    # ---- discovery: ----
    discovery_enabled: bool = True
    discovery_app:   str = "file-reaper"
    discovery_name:  Optional[str] = None

    # ---- api: ----
    api_open_read:   bool = True

    # ---- resolved from the config file ----
    config_path:     Optional[str] = None
    env_file:        Optional[str] = None
    areas:           List[AreaConfig] = field(default_factory=list)
    notifications:   Dict[str, Any] = field(default_factory=dict)
    raw_config:      Dict[str, Any] = field(default_factory=dict)
    config_issues:   List[str] = field(default_factory=list)

    def apply(self, **kwargs: Any) -> None:
        """Set attributes from *kwargs*, ignoring any whose value is ``None``.

        Skipping ``None`` is what makes the layering work: argparse defaults and
        absent environment variables both arrive as ``None`` and leave the lower
        layer's value in place.
        """
        known = {f.name for f in fields(self)}
        for key, value in kwargs.items():
            if value is None:
                continue
            if key not in known:
                raise AttributeError(f"unknown setting: {key!r}")
            setattr(self, key, value)

    def apply_env(self, environ: Optional[Dict[str, str]] = None) -> List[str]:
        """Apply ``MU2EDAQ_FILE_REAPER_*`` overrides; return warnings for bad values."""
        env = os.environ if environ is None else environ
        issues: List[str] = []
        for name, (attr, coerce) in _ENV_MAP.items():
            if name not in env:
                continue
            raw = env[name]
            if coerce is None:
                value = _as_bool(raw)
                if value is None:
                    issues.append(f"{name}: {raw!r} is not a boolean; ignored")
                    continue
            else:
                try:
                    value = coerce(raw)
                except (TypeError, ValueError):
                    issues.append(f"{name}: {raw!r} is not valid; ignored")
                    continue
            setattr(self, attr, value)
        return issues

    def as_dict(self, redact: bool = True) -> Dict[str, Any]:
        """Scalar settings only — for the /config page and /api/v1/config."""
        skip = {"areas", "notifications", "raw_config", "config_issues"}
        out = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in skip}
        if redact:
            out["admin_token"] = "***" if self.admin_token else ""
            out["database_url"] = redact_url(self.database_url)
        return out

    @property
    def effective_api_port(self) -> int:
        return self.api_port or self.web_port


def redact_url(url: str) -> str:
    """Hide a password inside a SQLAlchemy URL (``user:***@host``)."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    if ":" in creds:
        creds = creds.split(":", 1)[0] + ":***"
    return f"{scheme}://{creds}@{host}"


def load_env_file(path: str) -> Dict[str, str]:
    """Tiny ``.env`` reader: ``KEY=value`` lines, ``#`` comments, optional quotes.

    Values already present in ``os.environ`` are *not* returned, so the real
    environment always wins over the file.
    """
    values: Dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key and key not in os.environ:
                    values[key] = val
    except OSError:
        return {}
    return values


_SETTINGS = Settings()


def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return _SETTINGS


def reset_settings(**kwargs: Any) -> Settings:
    """Restore defaults, then apply *kwargs*.  Intended for tests only."""
    fresh = Settings()
    for f in fields(fresh):
        setattr(_SETTINGS, f.name, getattr(fresh, f.name))
    _SETTINGS.apply(**kwargs)
    return _SETTINGS
