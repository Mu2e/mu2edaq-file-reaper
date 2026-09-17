"""Command-line entry point for the daemon.

Configuration layers, lowest priority first::

    dataclass defaults -> YAML config file -> .env file -> environment -> command line

Every option except ``--config`` defaults to ``None`` so :meth:`Settings.apply`
can tell "not supplied" from "supplied".
"""

import argparse
import logging
import os
import secrets
import signal
import sys
import threading
from typing import Optional

from . import __version__
from .config import build_areas, load_config
from .daemon import daemonize, write_pid_file
from .settings import ENV_PREFIX, get_settings, load_env_file

#: Config file used when neither --config nor the environment names one.
DEFAULT_CONFIG = "config/mu2edaq-file-reaper.yaml"

log = logging.getLogger("reaper")


def build_parser(defaults) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mu2edaq-file-reaper",
        description="Policy-driven disk cleanup daemon for the Mu2e DAQ: monitors disk "
                    "areas, alarms on warning/critical/full tiers, compresses or deletes "
                    "files by LRU/age policy, keeps an audit history, and serves a web UI "
                    "and REST API.")
    p.add_argument("--config", "-c", default=None, metavar="FILE",
                   help=f"YAML configuration file (default: {DEFAULT_CONFIG}, or ${ENV_PREFIX}CONFIG)")
    p.add_argument("--env-file", default=None, metavar="FILE",
                   help="dotenv file read below the real environment (default: .env next to the config)")
    p.add_argument("--label", default=None, metavar="NAME", help="instance label for discovery and notifications")
    p.add_argument("--host", default=None, metavar="ADDR", help=f"web bind address (default: {defaults.web_host})")
    p.add_argument("--port", "-p", type=int, default=None, metavar="PORT", help=f"web port (default: {defaults.web_port})")
    p.add_argument("--api-port", type=int, default=None, metavar="PORT", help="serve the API on a second port as well")
    p.add_argument("--scan-interval", type=int, default=None, metavar="SECONDS",
                   help=f"seconds between scans (default: {defaults.scan_interval})")
    p.add_argument("--dry-run", action="store_true", default=None, help="plan and log, never modify the filesystem")
    p.add_argument("--database-url", default=None, metavar="URL", help="SQLAlchemy URL (default: sqlite:///./data/file-reaper.db)")
    p.add_argument("--admin-token", default=None, metavar="TOKEN", help="admin secret for UI writes (prefer the environment)")
    p.add_argument("--workers", type=int, default=None, metavar="N", help="concurrent area scans")
    p.add_argument("--no-discovery", action="store_true", default=None, help="do not advertise via mu2edaq-discovery")
    p.add_argument("--daemon", "-d", action="store_true", default=None, help="run as a background daemon (POSIX only)")
    p.add_argument("--pid-file", default=None, metavar="FILE", help="write the daemon PID to FILE")
    p.add_argument("--log-file", default=None, metavar="FILE", help="rotating log file")
    p.add_argument("--verbose", "-v", action="store_true", default=None, help="debug logging")
    p.add_argument("--check-config", action="store_true", default=False,
                   help="load and validate the configuration, print the result, and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _opt_int(d: dict, key: str) -> Optional[int]:
    return int(d[key]) if key in d and d[key] is not None else None


def _opt_bool(d: dict, key: str) -> Optional[bool]:
    return bool(d[key]) if key in d and d[key] is not None else None


def apply_yaml(settings, cfg: dict, config_path: Optional[str]) -> None:
    r = cfg.get("reaper", {}) or {}
    settings.apply(
        label=r.get("label"), web_host=r.get("web_host"),
        web_port=_opt_int(r, "web_port"), api_port=_opt_int(r, "api_port"),
        scan_interval=_opt_int(r, "scan_interval"), dry_run=_opt_bool(r, "dry_run"),
        daemon=_opt_bool(r, "daemon"), pid_file=r.get("pid_file"), log_file=r.get("log_file"),
        log_max_bytes=_opt_int(r, "log_max_bytes"), log_backup_count=_opt_int(r, "log_backup_count"),
        admin_token=str(r["admin_token"]) if r.get("admin_token") else None,
        workers=_opt_int(r, "workers"), max_passes_per_scan=_opt_int(r, "max_passes_per_scan"),
        remeasure_every=_opt_int(r, "remeasure_every"), stall_window=_opt_int(r, "stall_window"),
        shutdown_grace_s=_opt_int(r, "shutdown_grace_s"),
        queue_publish_limit=_opt_int(r, "queue_publish_limit"),
        history_log_queue_adds=r.get("history_log_queue_adds"),
        compress_verify=r.get("compress_verify"),
        compress_min_ratio=float(r["compress_min_ratio"]) if "compress_min_ratio" in r else None,
        compress_assumed_ratio=float(r["compress_assumed_ratio"]) if "compress_assumed_ratio" in r else None,
        protected_paths=list(r["protected_paths"]) if r.get("protected_paths") else None,
        allow_shallow_root=_opt_bool(r, "allow_shallow_root"),
        fake_usage_file=r.get("fake_usage_file"),
        config_path=config_path if cfg else None,
    )
    d = cfg.get("database", {}) or {}
    settings.apply(database_url=d.get("url"), history_retention_days=_opt_int(d, "history_retention_days"))
    disc = cfg.get("discovery", {}) or {}
    settings.apply(discovery_enabled=_opt_bool(disc, "enabled"), discovery_app=disc.get("app"),
                   discovery_name=disc.get("name"))
    api = cfg.get("api", {}) or {}
    settings.apply(api_open_read=_opt_bool(api, "open_read"))
    settings.notifications = dict(cfg.get("notifications") or {})
    settings.raw_config = cfg


def load_settings(argv=None):
    """Resolve every configuration layer; returns (settings, args)."""
    settings = get_settings()
    args = build_parser(settings).parse_args(argv)

    env_config = os.environ.get(ENV_PREFIX + "CONFIG")
    config_path = args.config or env_config or DEFAULT_CONFIG
    cfg = load_config(config_path, required=bool(args.config or env_config))

    # ---- layer 2: YAML ----
    apply_yaml(settings, cfg, config_path)

    # ---- layer 3: .env (below the real environment) ----
    env_file = args.env_file or os.environ.get(ENV_PREFIX + "ENV_FILE")
    if not env_file and cfg:
        candidate = os.path.join(os.path.dirname(os.path.abspath(config_path)), ".env")
        if os.path.isfile(candidate):
            env_file = candidate
    if not env_file and os.path.isfile(".env"):
        env_file = ".env"
    issues = []
    if env_file:
        dotenv = load_env_file(env_file)
        if dotenv:
            issues += settings.apply_env({**dotenv})
            settings.env_file = env_file

    # ---- layer 4: environment ----
    issues += settings.apply_env()

    # ---- layer 5: command line ----
    settings.apply(
        label=args.label, web_host=args.host, web_port=args.port, api_port=args.api_port,
        scan_interval=args.scan_interval, dry_run=args.dry_run or None,
        database_url=args.database_url, admin_token=args.admin_token, workers=args.workers,
        discovery_enabled=False if args.no_discovery else None,
        daemon=args.daemon or None, pid_file=args.pid_file, log_file=args.log_file,
        verbose=args.verbose, config_path=config_path if cfg else None,
    )

    areas, area_issues = build_areas(cfg, allow_shallow_root=settings.allow_shallow_root)
    settings.areas = areas
    settings.config_issues = issues + area_issues
    return settings, args


def main(argv=None) -> None:
    settings, args = load_settings(argv)
    from .logsetup import configure_logging

    if args.check_config:
        configure_logging(None, settings.verbose, 0, 0, False)
        print(f"config: {settings.config_path or '(defaults)'}")
        print(f"label: {settings.label}  web: {settings.web_host}:{settings.web_port}  "
              f"api_port: {settings.effective_api_port}  scan_interval: {settings.scan_interval}s  "
              f"dry_run: {settings.dry_run}")
        print(f"database: {settings.as_dict()['database_url']}")
        for a in settings.areas:
            tiers = ", ".join(f"{n}={t.threshold:g}%/{t.policy}/min_age={t.min_age}s"
                              for n, t in a.tiers.items())
            print(f"area {a.name}: {a.real_path}  enabled={a.enabled} dry_run={a.dry_run}  [{tiers}]")
        for issue in settings.config_issues:
            print(f"issue: {issue}")
        sys.exit(1 if any("dropped" in i for i in settings.config_issues) else 0)

    for issue in settings.config_issues:
        print(f"[Config] Warning: {issue}", file=sys.stderr)
    if not settings.areas:
        print("[Config] Warning: no areas configured; the reaper will only serve its web UI.",
              file=sys.stderr)

    # ---- daemonize before starting threads ----
    if settings.daemon:
        log_dest = settings.log_file or os.devnull
        print(f"[Daemon] Daemonizing. Log: {log_dest}  PID file: {settings.pid_file or '(none)'}")
        try:
            daemonize(settings.log_file)
        except RuntimeError as exc:
            print(f"[Daemon] {exc}", file=sys.stderr)
            sys.exit(1)
        if settings.pid_file:
            write_pid_file(settings.pid_file)
    configure_logging(settings.log_file if not settings.daemon else None, settings.verbose,
                      settings.log_max_bytes, settings.log_backup_count, settings.daemon)

    bootstrap_token = None
    if not settings.admin_token:
        bootstrap_token = secrets.token_urlsafe(18)
        settings.admin_token = bootstrap_token

    # ---- wire the engine ----
    from .db import init_db
    from .db.repo import AreaStateRepo, ExclusionRepo, HistoryRepo, NotificationLogRepo, TokenRepo
    from .discovery import start_responder, stop_responder
    from .exclusions import ExclusionRegistry
    from .notify import build_notifier
    from .reaper import Deps
    from .scheduler import Scheduler
    from .state import STORE
    from .usage import fake_usage_from_file, measure_usage, shutil_usage
    from .web import create_app, serve

    if settings.database_url.startswith("sqlite:///"):
        db_dir = os.path.dirname(os.path.abspath(settings.database_url[len("sqlite:///"):]))
        os.makedirs(db_dir, exist_ok=True)
    db = init_db(settings.database_url)
    history = HistoryRepo(db)
    exclusions = ExclusionRegistry(ExclusionRepo(db))
    exclusions.reload()
    notifier = build_notifier(settings.notifications, settings.label,
                              log_repo=NotificationLogRepo(db), history=history)
    notifier.start()
    usage_fn = fake_usage_from_file(settings.fake_usage_file) if settings.fake_usage_file else shutil_usage
    if settings.fake_usage_file:
        log.warning("fake_usage_file is set (%s): disk usage is NOT real", settings.fake_usage_file)
    deps = Deps(store=STORE, history=history, area_state=AreaStateRepo(db), exclusions=exclusions,
                notifier=notifier, measure_usage=lambda p: measure_usage(p, usage_fn))
    sched = Scheduler(deps, settings, history=history)

    app = create_app(db=db, scheduler=sched, deps=deps, notifier=notifier,
                     tokens=TokenRepo(db), exclusions_repo=ExclusionRepo(db))

    log.info("mu2edaq-file-reaper %s starting: label=%s areas=%d scan_interval=%ss dry_run=%s",
             __version__, settings.label, len(settings.areas), settings.scan_interval, settings.dry_run)
    log.info("web UI at http://localhost:%d  API at http://localhost:%d/api/v1",
             settings.web_port, settings.effective_api_port)
    if bootstrap_token:
        log.warning("no reaper.admin_token configured; one-time bootstrap admin token: %s "
                    "(set %sADMIN_TOKEN or reaper.admin_token to make it permanent)",
                    bootstrap_token, ENV_PREFIX)

    servers = serve(app, settings)
    responder = start_responder(settings)
    sched.start()

    stop_event = threading.Event()

    def _on_signal(signum, _frame):
        if stop_event.is_set():
            log.warning("second signal %s: exiting immediately", signum)
            os._exit(1)
        log.info("signal %s received: shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass
    try:
        while not stop_event.wait(1.0):
            if not any(s.thread.is_alive() for s in servers):
                log.error("web server thread died; shutting down")
                break
    finally:
        sched.stop()
        notifier.stop()
        stop_responder(responder)
        for s in servers:
            s.shutdown()
        db.dispose()
        log.info("mu2edaq-file-reaper stopped")


if __name__ == "__main__":
    main()
