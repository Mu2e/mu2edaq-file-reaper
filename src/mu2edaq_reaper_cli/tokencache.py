"""Per-user cache of API tokens, keyed by reaper base URL.

File: ``~/.config/mu2edaq/file-reaper/tokens.yaml`` (override with the
``MU2EDAQ_REAPER_TOKEN_FILE`` environment variable).  Layout::

    "http://mu2edaq01:5004":
      token: rpr_...
      name: shifter
      saved: 2026-09-11T14:02:11Z

The file holds live credentials, so on POSIX it must be owner-only: ``load()``
refuses a group- or world-readable file, and ``save()`` always writes 0600
inside a 0700 directory, atomically (temp file + rename).
"""

import os
import stat
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml

ENV_TOKEN_FILE = "MU2EDAQ_REAPER_TOKEN_FILE"
DEFAULT_PATH = os.path.join("~", ".config", "mu2edaq", "file-reaper", "tokens.yaml")


class TokenCacheError(Exception):
    """The cache file is unreadable, malformed or has unsafe permissions."""


def normalise_url(url: str) -> str:
    u = (url or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "http://" + u
    return u


def cache_path(override: Optional[str] = None) -> str:
    """Resolved cache file path: explicit argument > env > default."""
    p = override or os.environ.get(ENV_TOKEN_FILE) or DEFAULT_PATH
    return os.path.abspath(os.path.expanduser(p))


def _check_permissions(path: str) -> None:
    if os.name != "posix":
        return
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise TokenCacheError(
            f"token cache {path} is readable by others (mode {mode:04o}); "
            f"refusing to use it.  Fix with: chmod 600 {path}")


def load(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Return the whole mapping ``{url: {token, name, saved}}`` (empty if absent)."""
    p = cache_path(path)
    if not os.path.exists(p):
        return {}
    _check_permissions(p)
    try:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise TokenCacheError(f"cannot read token cache {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise TokenCacheError(f"token cache {p} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise TokenCacheError(f"token cache {p} must be a mapping of url -> entry")
    out: Dict[str, Dict[str, Any]] = {}
    for url, entry in data.items():
        if isinstance(entry, dict) and entry.get("token"):
            out[normalise_url(str(url))] = dict(entry)
    return out


def _write_atomic(p: str, data: Dict[str, Dict[str, Any]]) -> None:
    parent = os.path.dirname(p)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(parent, 0o700)
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".tokens.", suffix=".tmp", dir=parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# mu2edaq-reaper token cache -- keep private (chmod 600)\n")
            if data:
                yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=True)
        os.replace(tmp, p)
        if os.name == "posix":
            os.chmod(p, 0o600)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(url: str, token: str, name: Optional[str] = None, path: Optional[str] = None) -> str:
    """Store *token* for *url*; returns the cache file path."""
    if not token or not token.strip():
        raise TokenCacheError("refusing to store an empty token")
    p = cache_path(path)
    data = load(p)
    data[normalise_url(url)] = {
        "token": token.strip(),
        "name": name or "",
        "saved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_atomic(p, data)
    return p


def get(url: str, path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The cached entry for *url* (``{token, name, saved}``) or ``None``."""
    return load(path).get(normalise_url(url))


def remove(url: str, path: Optional[str] = None) -> bool:
    """Forget the token for *url*.  Returns True when something was removed."""
    p = cache_path(path)
    data = load(p)
    key = normalise_url(url)
    if key not in data:
        return False
    del data[key]
    _write_atomic(p, data)
    return True


def list_all(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Every cached entry, with the token replaced by a short prefix."""
    out = {}
    for url, entry in load(path).items():
        tok = str(entry.get("token", ""))
        out[url] = {"name": entry.get("name", ""), "saved": entry.get("saved", ""),
                    "prefix": tok[:12] + "..." if len(tok) > 12 else "***"}
    return out
