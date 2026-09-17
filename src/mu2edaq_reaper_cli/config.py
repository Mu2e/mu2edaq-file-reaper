"""Settings resolution for ``mu2edaq-reaper``.

Precedence, highest first:

    1. command line options
    2. environment variables (``MU2EDAQ_REAPER_*``)
    3. YAML config file (keys: url, instance, timeout, token_file)
    4. built-in defaults (url http://localhost:5004)

The YAML file is looked up in this order unless an explicit path is given:

    $MU2EDAQ_REAPER_CONFIG
    ./mu2edaq-reaper.yaml
    ./config/mu2edaq-reaper.yaml
    ~/.config/mu2edaq/file-reaper/cli.yaml
    /etc/mu2edaq/file-reaper-cli.yaml

Endpoint resolution: an explicit URL wins; otherwise an ``instance`` label is
looked up through ``mu2edaq-discovery``; otherwise the default URL.  Token
resolution: explicit > environment > the per-URL token cache.
"""

import os
from typing import Any, Dict, Iterable, List, Optional

import yaml

from . import tokencache
from .client import normalise_url

ENV_PREFIX = "MU2EDAQ_REAPER_"
ENV_URL = ENV_PREFIX + "URL"
ENV_TOKEN = ENV_PREFIX + "TOKEN"
ENV_ADMIN_TOKEN = ENV_PREFIX + "ADMIN_TOKEN"
ENV_INSTANCE = ENV_PREFIX + "INSTANCE"
ENV_TIMEOUT = ENV_PREFIX + "TIMEOUT"
ENV_CONFIG = ENV_PREFIX + "CONFIG"

DEFAULT_URL = "http://localhost:5004"
DEFAULT_TIMEOUT = 10.0
DISCOVERY_APP = "file-reaper"

DEFAULTS: Dict[str, Any] = {
    "url": None,            # None -> discovery/instance -> DEFAULT_URL
    "instance": None,
    "timeout": DEFAULT_TIMEOUT,
    "token_file": None,
    "discovery_timeout": 2.0,
}

CONFIG_KEYS = ("url", "instance", "timeout", "token_file", "discovery_timeout")


class ConfigError(Exception):
    pass


def config_search_paths(explicit: Optional[str] = None) -> Iterable[str]:
    if explicit:
        return [os.path.expanduser(explicit)]
    paths: List[str] = []
    env = os.environ.get(ENV_CONFIG)
    if env:
        paths.append(os.path.expanduser(env))
    paths.append(os.path.join(os.getcwd(), "mu2edaq-reaper.yaml"))
    paths.append(os.path.join(os.getcwd(), "config", "mu2edaq-reaper.yaml"))
    paths.append(os.path.expanduser(os.path.join("~", ".config", "mu2edaq", "file-reaper", "cli.yaml")))
    paths.append(os.path.join("/etc", "mu2edaq", "file-reaper-cli.yaml"))
    return paths


def find_config_file(explicit: Optional[str] = None) -> Optional[str]:
    for p in config_search_paths(explicit):
        if os.path.isfile(p):
            return p
    if explicit:
        raise ConfigError(f"config file not found: {explicit}")
    return None


def load_config_file(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")
    # Accept both a flat file and a nested ``reaper:`` / ``cli:`` section.
    for section in ("cli", "reaper"):
        if isinstance(data.get(section), dict):
            data = {**data, **data[section]}
    return {k: data[k] for k in CONFIG_KEYS if k in data}


def _float(value: Any, what: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{what} must be a number, got {value!r}")


class Settings:
    """Effective settings after merging every source."""

    def __init__(self) -> None:
        self.url: Optional[str] = None
        self.instance: Optional[str] = None
        self.timeout: float = DEFAULT_TIMEOUT
        self.token: Optional[str] = None
        self.admin_token: Optional[str] = None
        self.token_file: Optional[str] = None
        self.discovery_timeout: float = 2.0
        self.config_file: Optional[str] = None
        self.sources: Dict[str, str] = {}
        self.endpoint: Optional[Dict[str, Any]] = None   # discovery record, if used

    def as_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "instance": self.instance, "timeout": self.timeout,
                "token_file": self.token_file, "discovery_timeout": self.discovery_timeout,
                "config_file": self.config_file,
                "token": "***" if self.token else None,
                "admin_token": "***" if self.admin_token else None,
                "sources": dict(self.sources)}


def resolve(url: Optional[str] = None, instance: Optional[str] = None,
            token: Optional[str] = None, admin_token: Optional[str] = None,
            timeout: Optional[float] = None, config: Optional[str] = None,
            token_file: Optional[str] = None, use_discovery: bool = True,
            environ: Optional[Dict[str, str]] = None) -> Settings:
    """Merge CLI > env > config file > defaults and resolve endpoint + token."""
    env = os.environ if environ is None else environ
    s = Settings()
    # Only an explicit --config must exist; $MU2EDAQ_REAPER_CONFIG is one of
    # the search-path entries and may be absent.
    s.config_file = find_config_file(config or None)
    filecfg = load_config_file(s.config_file)

    def pick(key: str, cli_value: Any, env_key: Optional[str]) -> Any:
        if cli_value is not None and cli_value != "":
            s.sources[key] = "cli"
            return cli_value
        if env_key and env.get(env_key):
            s.sources[key] = "env"
            return env[env_key]
        if key in filecfg and filecfg[key] not in (None, ""):
            s.sources[key] = "config"
            return filecfg[key]
        s.sources[key] = "default"
        return DEFAULTS.get(key)

    raw_url = pick("url", url, ENV_URL)
    s.instance = pick("instance", instance, ENV_INSTANCE)
    s.timeout = _float(pick("timeout", timeout, ENV_TIMEOUT), "timeout")
    s.discovery_timeout = _float(pick("discovery_timeout", None, ENV_PREFIX + "DISCOVERY_TIMEOUT"),
                                 "discovery_timeout")
    tf = pick("token_file", token_file, tokencache.ENV_TOKEN_FILE)
    s.token_file = os.path.expanduser(str(tf)) if tf else None
    s.admin_token = pick("admin_token", admin_token, ENV_ADMIN_TOKEN)

    # ---- endpoint: explicit url > instance via discovery > default --------
    if raw_url:
        s.url = normalise_url(str(raw_url))
    elif s.instance and use_discovery:
        rec = find_instance(str(s.instance), timeout=s.discovery_timeout)
        if rec is None:
            raise ConfigError(f"no file-reaper instance {s.instance!r} answered discovery "
                              f"(use --url to address it directly)")
        s.endpoint = rec
        s.url = endpoint_url(rec)
        s.sources["url"] = "discovery"
    else:
        s.url = DEFAULT_URL
        s.sources["url"] = "default"

    # ---- token: explicit > env > cache -------------------------------------
    if token:
        s.token = token
        s.sources["token"] = "cli"
    elif env.get(ENV_TOKEN):
        s.token = env[ENV_TOKEN]
        s.sources["token"] = "env"
    else:
        entry = tokencache.get(s.url, s.token_file)
        if entry:
            s.token = str(entry["token"])
            s.sources["token"] = "cache"
    return s


# ------------------------------------------------------------------ discovery --
def discover_instances(timeout: float = 2.0) -> List[Dict[str, Any]]:
    """All file-reaper ANNOUNCE records on the network (ImportError if the
    ``mu2edaq_discovery`` package is not installed)."""
    from mu2edaq_discovery import discover   # lazy: optional dependency
    recs = discover(filter={"app": DISCOVERY_APP}, timeout=timeout)
    return sorted(recs, key=lambda r: (str((r.get("meta") or {}).get("instance", "")),
                                       str(r.get("host", "")), int(r.get("port") or 0)))


def endpoint_url(rec: Dict[str, Any]) -> str:
    meta = rec.get("meta") or {}
    scheme = rec.get("scheme") or "http"
    if scheme not in ("http", "https"):
        scheme = "http"
    host = rec.get("host") or "localhost"
    port = meta.get("api_port") or rec.get("port")
    if ":" in str(host) and not str(host).startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}" if port else f"{scheme}://{host}"


def match_instance(rec: Dict[str, Any], label: str) -> bool:
    meta = rec.get("meta") or {}
    want = label.strip().lower()
    if str(meta.get("instance", "")).strip().lower() == want:
        return True
    return want in str(rec.get("name", "")).lower()


def find_instance(label: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        recs = discover_instances(timeout=timeout)
    except ImportError:
        raise ConfigError("the mu2edaq_discovery package is not installed; install "
                          "mu2edaq-discovery or pass --url / MU2EDAQ_REAPER_URL") from None
    exact = [r for r in recs if str((r.get("meta") or {}).get("instance", "")).lower() == label.lower()]
    if exact:
        return exact[0]
    loose = [r for r in recs if match_instance(r, label)]
    return loose[0] if loose else None
