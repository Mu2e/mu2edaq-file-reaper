"""HTML pages.

The live dashboards (Areas, area detail, Queues, Notifications) render an empty
shell and fill it from ``/api/v1`` with ``fetch()`` on a refresh interval, so
they stay current without a page reload.  Config, API, About, Sitemap and the
login form are rendered server-side from settings that only change on restart.
Exclusions and Tokens render a shell and load their lists from the API because
both are edited in place.

Every page must render with an empty store (no areas configured) — the test
suite hits every route with a test client on a bare app.
"""

import importlib.metadata
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Blueprint, abort, redirect, render_template, request, url_for

from .. import START_TIME, __version__
from ..config import area_to_dict
from ..formatting import fmt_bytes, fmt_duration, fmt_pct
from ..settings import ENV_PREFIX, get_settings, redact_url
from ..state import AREA_STATES, STORE
from . import reaper_ext
from .auth import is_admin_session, login_admin, logout_admin

bp = Blueprint("views", __name__)

REPO_URL = "https://github.com/Mu2e/mu2edaq-file-reaper"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _store():
    """The live state store: the one wired into ``Deps`` when available."""
    try:
        deps = reaper_ext().get("deps")
    except Exception:
        deps = None
    store = getattr(deps, "store", None)
    return store if store is not None else STORE


def _area_names() -> List[Dict[str, str]]:
    """``[{name, label}]`` for filter selects; store first, config as fallback."""
    out: List[Dict[str, str]] = []
    seen = set()
    for snap in _store().snapshot():
        out.append({"name": snap.get("name", ""), "label": snap.get("label") or snap.get("name", "")})
        seen.add(snap.get("name"))
    for a in get_settings().areas:
        if a.name not in seen:
            out.append({"name": a.name, "label": a.label or a.name})
    return out


def _safe_next(target: Optional[str]) -> str:
    """Only relative, same-site paths are honoured as a post-login redirect."""
    if not target:
        return url_for("views.index")
    t = target.strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or ":" in t.split("?", 1)[0]:
        return url_for("views.index")
    return t


def _pkg_ver(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "?"


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# live dashboards
# ---------------------------------------------------------------------------
@bp.route("/")
def index():
    return render_template("index.html", title="Areas", area_states=AREA_STATES)


@bp.route("/areas/<name>")
def area(name):
    settings = get_settings()
    cfg = next((a for a in settings.areas if a.name == name), None)
    snap = _store().area(name)
    if cfg is None and snap is None:
        abort(404)
    return render_template("area.html", title=f"Area {name}", name=name,
                           area_cfg=area_to_dict(cfg) if cfg else None,
                           snap=snap or {}, fmt_duration=fmt_duration, fmt_pct=fmt_pct)


@bp.route("/queues")
def queues():
    return render_template("queues.html", title="Queues", areas=_area_names(),
                           sel_area=request.args.get("area", ""),
                           sel_kind=request.args.get("kind", ""),
                           sel_status=request.args.get("status", ""))


@bp.route("/history")
def history():
    return render_template("history.html", title="History", areas=_area_names(),
                           q=request.args)


@bp.route("/exclusions")
def exclusions():
    return render_template("exclusions.html", title="Exclusions", areas=_area_names())


@bp.route("/notifications")
def notifications():
    return render_template("notifications.html", title="Notifications")


@bp.route("/tokens")
def tokens():
    if not is_admin_session():
        return redirect(url_for("views.login", next=url_for("views.tokens")))
    return render_template("tokens.html", title="API Tokens")


# ---------------------------------------------------------------------------
# server-rendered reference pages
# ---------------------------------------------------------------------------
@bp.route("/config")
def config():
    settings = get_settings()
    raw_yaml = None
    if settings.config_path:
        try:
            with open(settings.config_path, encoding="utf-8") as fh:
                raw_yaml = fh.read()
        except OSError as exc:
            raw_yaml = f"# Could not read {settings.config_path}: {exc}"

    notif: Dict[str, Any] = {}
    for cname, ccfg in (settings.notifications or {}).items():
        if isinstance(ccfg, dict):
            notif[cname] = {k: ("***" if k in ("token", "webhook_url", "password") and v else v)
                            for k, v in ccfg.items()}
        else:
            notif[cname] = ccfg

    return render_template(
        "config.html", title="Config", settings=settings,
        scalars=settings.as_dict(redact=True),
        areas=[area_to_dict(a) for a in settings.areas],
        issues=list(settings.config_issues), notifications=notif,
        raw_yaml=raw_yaml, env_prefix=ENV_PREFIX,
        fmt_duration=fmt_duration, fmt_pct=fmt_pct, fmt_bytes=fmt_bytes,
    )


def _api_routes(port: int) -> List[Dict[str, str]]:
    """Human reference of every /api/v1 route: method, path, scope, description,
    example curl and the equivalent ``mu2edaq-reaper`` invocation."""
    base = f"http://localhost:{port}/api/v1"
    auth = '-H "Authorization: Bearer $TOKEN"'
    return [
        {"group": "Liveness", "method": "GET", "path": "/api/v1/health", "scope": "none",
         "desc": "Scheduler liveness, database ping, config issue count. status is \"degraded\" when "
                 "the scheduler is dead or stalled or the database is unreachable.",
         "curl": f"curl -s {base}/health | python3 -m json.tool",
         "cli": "mu2edaq-reaper status"},
        {"group": "Liveness", "method": "GET", "path": "/api/v1/version", "scope": "none",
         "desc": "Application name, version, label and API version.",
         "curl": f"curl -s {base}/version", "cli": "mu2edaq-reaper version"},
        {"group": "Liveness", "method": "GET", "path": "/api/v1/whoami", "scope": "none",
         "desc": "Who the server thinks you are: principal kind, name, scopes, actor string.",
         "curl": f"curl -s {auth} {base}/whoami", "cli": "mu2edaq-reaper token whoami"},

        {"group": "Areas", "method": "GET", "path": "/api/v1/state", "scope": "read",
         "desc": "Everything: every area snapshot, the state summary, scheduler health and store meta.",
         "curl": f"curl -s {auth} {base}/state | python3 -m json.tool",
         "cli": "mu2edaq-reaper status --json"},
        {"group": "Areas", "method": "GET", "path": "/api/v1/areas", "scope": "read",
         "desc": "All area snapshots plus the summary counts (good, warning, ... queued_compress, queued_delete, acting).",
         "curl": f"curl -s {auth} {base}/areas", "cli": "mu2edaq-reaper areas"},
        {"group": "Areas", "method": "GET", "path": "/api/v1/areas/<name>", "scope": "read",
         "desc": "One area: live snapshot, parsed config and usage_history [{ts, used_pct}] from recent scan_end events.",
         "curl": f"curl -s {auth} {base}/areas/data-raw", "cli": "mu2edaq-reaper area data-raw"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/areas/<name>/pause", "scope": "operate",
         "desc": "Stop acting on the area (scans continue). Body/JSON: reason.",
         "curl": f"curl -s -X POST {auth} -H 'Content-Type: application/json' "
                 f"-d '{{\"reason\": \"run in progress\"}}' {base}/areas/data-raw/pause",
         "cli": "mu2edaq-reaper pause data-raw --reason 'run in progress'"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/areas/<name>/resume", "scope": "operate",
         "desc": "Resume acting after a pause.",
         "curl": f"curl -s -X POST {auth} {base}/areas/data-raw/resume",
         "cli": "mu2edaq-reaper resume data-raw"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/areas/<name>/rescan", "scope": "operate",
         "desc": "Request a scan of the area on the next scheduler tick. JSON dry_run: true plans only.",
         "curl": f"curl -s -X POST {auth} {base}/areas/data-raw/rescan",
         "cli": "mu2edaq-reaper rescan data-raw"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/areas/<name>/disable", "scope": "admin",
         "desc": "Take the area out of service: no scans, no actions. Body/JSON: reason.",
         "curl": f"curl -s -X POST {auth} -d 'reason=disk being replaced' {base}/areas/data-raw/disable",
         "cli": "mu2edaq-reaper disable data-raw --reason 'disk being replaced'"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/areas/<name>/enable", "scope": "admin",
         "desc": "Put a disabled area back in service.",
         "curl": f"curl -s -X POST {auth} {base}/areas/data-raw/enable",
         "cli": "mu2edaq-reaper enable data-raw"},
        {"group": "Areas", "method": "POST", "path": "/api/v1/dry-run/<name>", "scope": "operate",
         "desc": "Run one scan now in dry-run mode on the request thread and return the plan "
                 "(report + resulting snapshot with queues). 409 if a scan is already running.",
         "curl": f"curl -s -X POST {auth} {base}/dry-run/data-raw | python3 -m json.tool",
         "cli": "mu2edaq-reaper plan data-raw"},

        {"group": "Queues", "method": "GET", "path": "/api/v1/queues", "scope": "read",
         "desc": "Flattened queue entries across areas. Filters: area, kind=compress|delete, status.",
         "curl": f"curl -s {auth} '{base}/queues?kind=delete&area=data-raw'",
         "cli": "mu2edaq-reaper queues --kind delete --area data-raw"},
        {"group": "Queues", "method": "GET", "path": "/api/v1/queues/<id-or-path>", "scope": "read",
         "desc": "One queue entry by dev:ino id or absolute path.",
         "curl": f"curl -s {auth} {base}/queues/data/raw/run_001.dat",
         "cli": "mu2edaq-reaper queues --area data-raw --json"},

        {"group": "Exclusions", "method": "GET", "path": "/api/v1/exclusions", "scope": "read",
         "desc": "Active exclusion rules. ?all=1 includes removed/expired ones; ?area= restricts.",
         "curl": f"curl -s {auth} '{base}/exclusions?all=1'",
         "cli": "mu2edaq-reaper exclude list --all"},
        {"group": "Exclusions", "method": "POST", "path": "/api/v1/exclusions", "scope": "operate",
         "desc": "Add a rule. JSON: pattern (or path), area (null = global), kind (path|glob, auto), "
                 "reason, expires_in (e.g. 7d) or expires_at.",
         "curl": f"curl -s -X POST {auth} -H 'Content-Type: application/json' "
                 f"-d '{{\"pattern\": \"/data/raw/keep_*\", \"area\": \"data-raw\", \"reason\": \"calibration\", "
                 f"\"expires_in\": \"30d\"}}' {base}/exclusions",
         "cli": "mu2edaq-reaper exclude add '/data/raw/keep_*' --area data-raw --reason calibration --expires-in 30d"},
        {"group": "Exclusions", "method": "DELETE", "path": "/api/v1/exclusions/<id>", "scope": "operate",
         "desc": "Deactivate a rule by id.",
         "curl": f"curl -s -X DELETE {auth} {base}/exclusions/12",
         "cli": "mu2edaq-reaper exclude rm 12"},

        {"group": "History", "method": "GET", "path": "/api/v1/history", "scope": "read",
         "desc": "Audit events, newest first. See the query parameters below. format=csv downloads a CSV.",
         "curl": f"curl -s {auth} '{base}/history?area=data-raw&type=action_delete&since=24h&limit=50'",
         "cli": "mu2edaq-reaper history --area data-raw --type action_delete --since 24h --limit 50"},
        {"group": "History", "method": "GET", "path": "/api/v1/history/types", "scope": "read",
         "desc": "The event_type vocabulary.",
         "curl": f"curl -s {auth} {base}/history/types", "cli": "mu2edaq-reaper history --types"},

        {"group": "Notifications", "method": "GET", "path": "/api/v1/notifications", "scope": "read",
         "desc": "Configured channels (enabled, min_severity, disabled_reason, last_sent) and the recent log. ?limit=.",
         "curl": f"curl -s {auth} {base}/notifications", "cli": "mu2edaq-reaper notify status"},
        {"group": "Notifications", "method": "POST", "path": "/api/v1/notifications/test", "scope": "admin",
         "desc": "Send a test event. JSON: channel (omit for all), severity, title, message.",
         "curl": f"curl -s -X POST {auth} -H 'Content-Type: application/json' "
                 f"-d '{{\"channel\": \"slack\"}}' {base}/notifications/test",
         "cli": "mu2edaq-reaper notify test slack"},

        {"group": "Tokens", "method": "GET", "path": "/api/v1/tokens", "scope": "admin",
         "desc": "API tokens (never the secret). ?all=1 includes revoked.",
         "curl": f"curl -s {auth} '{base}/tokens?all=1'", "cli": "mu2edaq-reaper token list --all"},
        {"group": "Tokens", "method": "POST", "path": "/api/v1/tokens", "scope": "admin",
         "desc": "Mint a token. JSON: name, scopes [read|operate|admin], expires_in. The plaintext is "
                 "returned once. Bootstrap with X-Admin-Token instead of a bearer.",
         "curl": f"curl -s -X POST -H \"X-Admin-Token: $ADMIN_TOKEN\" -H 'Content-Type: application/json' "
                 f"-d '{{\"name\": \"shifter\", \"scopes\": [\"operate\"], \"expires_in\": \"90d\"}}' {base}/tokens",
         "cli": "mu2edaq-reaper token create shifter --scopes operate --expires-in 90d"},
        {"group": "Tokens", "method": "DELETE", "path": "/api/v1/tokens/<id>", "scope": "admin",
         "desc": "Revoke a token by id (takes effect within the verification cache TTL).",
         "curl": f"curl -s -X DELETE {auth} {base}/tokens/3", "cli": "mu2edaq-reaper token revoke 3"},

        {"group": "Config", "method": "GET", "path": "/api/v1/config", "scope": "read",
         "desc": "Settings in effect (secrets redacted), parsed areas with tiers, config issues, notification config.",
         "curl": f"curl -s {auth} {base}/config | python3 -m json.tool", "cli": "mu2edaq-reaper config"},
    ]


@bp.route("/api")
def api_docs():
    settings = get_settings()
    routes = _api_routes(settings.effective_api_port)
    groups: List[str] = []
    for r in routes:
        if r["group"] not in groups:
            groups.append(r["group"])
    from ..db.models import EVENT_TYPES
    return render_template("api.html", title="API", routes=routes, groups=groups,
                           event_types=list(EVENT_TYPES), settings=settings,
                           port=settings.effective_api_port, area_states=AREA_STATES)


@bp.route("/sitemap")
def sitemap():
    return render_template("sitemap.html", title="Sitemap")


@bp.route("/about")
def about():
    settings = get_settings()
    uptime = datetime.now(timezone.utc) - START_TIME
    snaps = _store().snapshot()
    n_compress = sum(s.get("queues", {}).get("compress", {}).get("total", 0) for s in snaps)
    n_delete = sum(s.get("queues", {}).get("delete", {}).get("total", 0) for s in snaps)
    integrations = {
        "mu2edaq_discovery": _importable("mu2edaq_discovery"),
        "mu2edaq_notify": _importable("mu2edaq_notify"),
        "zmq": _importable("zmq"),
        "zstandard": _importable("zstandard"),
        "daq_alert": _importable("daq_alert"),
    }
    return render_template(
        "about.html", title="About", settings=settings, app_version=__version__,
        uptime_str=fmt_duration(uptime.total_seconds()), hostname=socket.gethostname(),
        os_info=platform.platform(), pid=os.getpid(), py_ver=sys.version.split()[0],
        flask_ver=_pkg_ver("flask"), jinja_ver=_pkg_ver("jinja2"), yaml_ver=_pkg_ver("pyyaml"),
        sqla_ver=_pkg_ver("sqlalchemy"), integrations=integrations,
        db_url=redact_url(settings.database_url), n_areas=len(snaps) or len(settings.areas),
        n_compress=n_compress, n_delete=n_delete,
        scan_display=fmt_duration(settings.scan_interval), repo_url=REPO_URL,
    )


# ---------------------------------------------------------------------------
# admin session
# ---------------------------------------------------------------------------
@bp.route("/login", methods=["GET", "POST"])
def login():
    nxt = _safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        token = request.form.get("token", "")
        if login_admin(token):
            return redirect(nxt)
        error = "That admin token was not accepted." if get_settings().admin_token \
            else "No admin token is configured on this server (reaper.admin_token)."
    return render_template("login.html", title="Admin sign in", next=nxt, error=error,
                           configured=bool(get_settings().admin_token))


@bp.route("/logout", methods=["POST"])
def logout():
    logout_admin()
    return redirect(url_for("views.index"))
