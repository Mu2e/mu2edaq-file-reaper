"""REST API, version 1, under ``/api/v1``.

Scopes: ``read`` (GET, waived when ``api.open_read`` is true), ``operate``
(pause/resume/rescan/exclusions/dry-run), ``admin`` (enable/disable, tokens,
notification tests).  Every mutating call writes a history event whose
``actor`` names the token or the admin session.
"""

import csv
import io
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from flask import Blueprint, Response, jsonify, request

from .. import START_TIME, __version__
from ..auth import generate_token, hash_token, normalise_scopes, token_prefix
from ..config import area_to_dict
from ..settings import get_settings
from ..state import area_summary
from ..units import UnitError, parse_duration
from . import reaper_ext
from .auth import require_scope, resolve_principal

bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")

EVENT_TYPES_HELP = "comma-separated list, e.g. action_delete,action_compress"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return {k: v for k, v in request.form.items()}
    return {}


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    try:                                   # relative: "24h", "7d"
        return datetime.now(timezone.utc) - timedelta(seconds=parse_duration(v))
    except UnitError:
        return None


def _store():
    return reaper_ext()["deps"].store


def _scheduler():
    return reaper_ext()["scheduler"]


def _area_or_404(name: str):
    snap = _store().area(name)
    if snap is None:
        return None, (jsonify({"error": f"unknown area {name!r}"}), 404)
    return snap, None


# ---------------------------------------------------------------------------
# Liveness
# ---------------------------------------------------------------------------
@bp.route("/health")
def health():
    settings = get_settings()
    sched = _scheduler()
    h = sched.health() if sched is not None else {"alive": False, "stalled": True}
    db = reaper_ext()["db"]
    db_ok = db.ping() if db is not None else False
    issues = len(settings.config_issues)
    degraded = (not h.get("alive")) or h.get("stalled") or not db_ok
    return jsonify({
        "status": "degraded" if degraded else "ok",
        "version": __version__, "label": settings.label,
        "uptime_s": int(time.time() - START_TIME.timestamp()),
        "areas": len(settings.areas), "scan_interval": settings.scan_interval,
        "dry_run": settings.dry_run, "config_issues": issues, "database_ok": db_ok,
        "scheduler": h, "generated": _now_iso(),
    })


@bp.route("/version")
def version():
    return jsonify({"name": "mu2edaq-file-reaper", "version": __version__,
                    "label": get_settings().label, "api": "v1"})


# ---------------------------------------------------------------------------
# Areas / state
# ---------------------------------------------------------------------------
@bp.route("/state")
@require_scope("read", allow_open_read=True)
def state():
    settings = get_settings()
    areas = _store().snapshot()
    sched = _scheduler()
    return jsonify({
        "version": __version__, "label": settings.label, "generated": _now_iso(),
        "scan_interval": settings.scan_interval, "dry_run": settings.dry_run,
        "summary": area_summary(areas), "areas": areas,
        "scheduler": sched.health() if sched else None, **_store().meta(),
    })


@bp.route("/areas")
@require_scope("read", allow_open_read=True)
def areas():
    snaps = _store().snapshot()
    return jsonify({"total": len(snaps), "summary": area_summary(snaps), "areas": snaps,
                    "scan_interval": get_settings().scan_interval})


@bp.route("/areas/<name>")
@require_scope("read", allow_open_read=True)
def area(name):
    snap, err = _area_or_404(name)
    if err:
        return err
    settings = get_settings()
    cfg = next((a for a in settings.areas if a.name == name), None)
    hist = reaper_ext()["history"]
    usage_pts = hist.recent_usage(name, limit=288) if hist is not None else []
    return jsonify({"area": snap, "config": area_to_dict(cfg) if cfg else None,
                    "usage_history": usage_pts})


def _area_action(name: str, verb: str, scope: str):
    snap, err = _area_or_404(name)
    if err:
        return err
    body = _json_body()
    reason = (body.get("reason") or request.args.get("reason") or "")[:500] or None
    actor = resolve_principal().actor
    sched = _scheduler()
    ok = getattr(sched, verb)(name, by=actor, reason=reason) if verb != "rescan" \
        else sched.request_scan(name, dry_run=bool(body.get("dry_run")))
    if not ok:
        return jsonify({"error": f"could not {verb} {name}"}), 409
    return jsonify({"ok": True, "area": name, "action": verb, "actor": actor, "reason": reason,
                    "state": _store().area(name)})


@bp.route("/areas/<name>/pause", methods=["POST"])
@require_scope("operate")
def area_pause(name):
    return _area_action(name, "pause", "operate")


@bp.route("/areas/<name>/resume", methods=["POST"])
@require_scope("operate")
def area_resume(name):
    return _area_action(name, "resume", "operate")


@bp.route("/areas/<name>/rescan", methods=["POST"])
@require_scope("operate")
def area_rescan(name):
    return _area_action(name, "rescan", "operate")


@bp.route("/areas/<name>/disable", methods=["POST"])
@require_scope("admin")
def area_disable(name):
    return _area_action(name, "disable", "admin")


@bp.route("/areas/<name>/enable", methods=["POST"])
@require_scope("admin")
def area_enable(name):
    return _area_action(name, "enable", "admin")


@bp.route("/dry-run/<name>", methods=["POST"])
@require_scope("operate")
def dry_run(name):
    """Run one scan of *name* now in dry-run mode on this request thread and
    return the resulting plan (queues, projected usage)."""
    snap, err = _area_or_404(name)
    if err:
        return err
    sched = _scheduler()
    rt = sched.runtime(name)
    if rt is None:
        return jsonify({"error": "unknown area"}), 404
    if rt.busy:
        return jsonify({"error": "a scan of this area is already running; try again shortly"}), 409
    report = sched.run_now(name, dry_run=True)
    return jsonify({"ok": True, "area": name, "dry_run": True, "actor": resolve_principal().actor,
                    "report": {"scan_id": report.scan_id, "outcome": report.outcome,
                               "reason": report.reason, "passes": report.passes,
                               "acted": report.acted, "bytes_freed": report.bytes_freed,
                               "duration_s": round(report.duration_s, 3)},
                    "state": _store().area(name)})


# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------
@bp.route("/queues")
@require_scope("read", allow_open_read=True)
def queues():
    area_f = request.args.get("area")
    kind_f = request.args.get("kind")
    status_f = request.args.get("status")
    out = []
    for snap in _store().snapshot():
        if area_f and snap["name"] != area_f:
            continue
        for kind, q in snap["queues"].items():
            if kind_f and kind != kind_f:
                continue
            for e in q["entries"]:
                if status_f and e.get("status") != status_f:
                    continue
                out.append({**e, "area": snap["name"], "area_label": snap["label"]})
    totals = {"compress": sum(s["queues"]["compress"]["total"] for s in _store().snapshot()),
              "delete": sum(s["queues"]["delete"]["total"] for s in _store().snapshot())}
    return jsonify({"total": len(out), "totals": totals, "entries": out, "generated": _now_iso()})


@bp.route("/queues/<path:entry_id>")
@require_scope("read", allow_open_read=True)
def queue_entry(entry_id):
    for snap in _store().snapshot():
        for kind, q in snap["queues"].items():
            for e in q["entries"]:
                if e["id"] == entry_id or e["path"] == "/" + entry_id or e["path"] == entry_id:
                    return jsonify({**e, "area": snap["name"], "area_label": snap["label"]})
    return jsonify({"error": "queue entry not found"}), 404


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------
@bp.route("/exclusions")
@require_scope("read", allow_open_read=True)
def exclusions_list():
    repo = reaper_ext()["exclusions_repo"]
    include_inactive = request.args.get("all") in ("1", "true", "yes")
    return jsonify({"exclusions": repo.list(area=request.args.get("area"),
                                             include_inactive=include_inactive)})


@bp.route("/exclusions", methods=["POST"])
@require_scope("operate")
def exclusions_add():
    body = _json_body()
    pattern = (body.get("pattern") or body.get("path") or "").strip()
    if not pattern:
        return jsonify({"error": "pattern (or path) is required"}), 400
    area_name = body.get("area") or None
    if area_name and _store().area(area_name) is None:
        return jsonify({"error": f"unknown area {area_name!r}"}), 404
    expires = None
    if body.get("expires_in"):
        try:
            expires = datetime.now(timezone.utc) + timedelta(seconds=parse_duration(body["expires_in"]))
        except UnitError as exc:
            return jsonify({"error": f"bad expires_in: {exc}"}), 400
    elif body.get("expires_at"):
        expires = _parse_ts(body["expires_at"])
    actor = resolve_principal().actor
    repo = reaper_ext()["exclusions_repo"]
    row = repo.add(pattern, area=area_name, kind=body.get("kind"), reason=body.get("reason"),
                   created_by=actor, expires_at=expires)
    reaper_ext()["deps"].exclusions.reload()
    reaper_ext()["history"].record("exclusion_add", area_name or "", path=pattern, actor=actor,
                                   reason=(body.get("reason") or "")[:64] or None,
                                   detail={"kind": row["kind"], "expires_at": row["expires_at"],
                                           "reason": body.get("reason")})
    return jsonify({"ok": True, "exclusion": row}), 201


@bp.route("/exclusions/<int:exclusion_id>", methods=["DELETE"])
@require_scope("operate")
def exclusions_remove(exclusion_id):
    repo = reaper_ext()["exclusions_repo"]
    row = repo.remove(exclusion_id)
    if row is None:
        return jsonify({"error": "exclusion not found"}), 404
    reaper_ext()["deps"].exclusions.reload()
    actor = resolve_principal().actor
    reaper_ext()["history"].record("exclusion_remove", row["area"] or "", path=row["pattern"],
                                   actor=actor, detail={"id": exclusion_id})
    return jsonify({"ok": True, "exclusion": row})


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
@bp.route("/history")
@require_scope("read", allow_open_read=True)
def history():
    hist = reaper_ext()["history"]
    args = request.args
    types = [t for t in (args.get("type") or args.get("types") or "").split(",") if t]
    try:
        limit = int(args.get("limit", 100))
        offset = int(args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400
    rows, total = hist.query(area=args.get("area") or None, path_glob=args.get("path") or None,
                             event_types=types or None, tier=args.get("tier") or None,
                             scan_id=args.get("scan_id") or None,
                             since=_parse_ts(args.get("since")), until=_parse_ts(args.get("until")),
                             limit=limit, offset=offset,
                             newest_first=args.get("order", "desc") != "asc")
    if args.get("format") == "csv":
        buf = io.StringIO()
        cols = ["id", "ts", "area", "event_type", "path", "tier", "policy", "queue", "outcome",
                "reason", "size_bytes", "bytes_freed", "duration_s", "used_pct_before",
                "used_pct_after", "count", "actor", "scan_id"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=reaper-history.csv"})
    return jsonify({"total": total, "limit": limit, "offset": offset, "events": rows,
                    "generated": _now_iso()})


@bp.route("/history/types")
@require_scope("read", allow_open_read=True)
def history_types():
    from ..db.models import EVENT_TYPES
    return jsonify({"types": list(EVENT_TYPES)})


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
@bp.route("/notifications")
@require_scope("read", allow_open_read=True)
def notifications():
    notifier = reaper_ext()["notifier"]
    db = reaper_ext()["db"]
    from ..db.repo import NotificationLogRepo
    log_repo = NotificationLogRepo(db) if db is not None else None
    channels = notifier.describe() if notifier else []
    last = log_repo.last_sent() if log_repo else {}
    for c in channels:
        c["last_sent"] = last.get(c["name"])
    recent = log_repo.recent(limit=int(request.args.get("limit", 50))) if log_repo else []
    return jsonify({"channels": channels, "recent": recent,
                    "sent": getattr(notifier, "sent_count", 0),
                    "failed": getattr(notifier, "failed_count", 0)})


@bp.route("/notifications/test", methods=["POST"])
@require_scope("admin")
def notifications_test():
    body = _json_body()
    notifier = reaper_ext()["notifier"]
    from ..notify.channels import Event
    channel = body.get("channel")
    ev = Event(severity=body.get("severity", "info"), title=body.get("title", "Test notification"),
               message=body.get("message", f"test from {get_settings().label} at {_now_iso()}"),
               key=f"test:{time.time()}", area=None, meta={"actor": resolve_principal().actor}, ts=time.time())
    if channel:
        ch = notifier.channel(channel)
        if ch is None:
            return jsonify({"error": f"unknown channel {channel!r}"}), 404
        if not ch.enabled:
            return jsonify({"channel": channel, "outcome": "disabled",
                            "reason": ch.disabled_reason}), 200
        try:
            ch.send(ev)
            outcome = {channel: "sent"}
        except Exception as exc:
            outcome = {channel: f"failed: {exc}"}
    else:
        outcome = notifier.emit_sync(ev)
    return jsonify({"ok": True, "outcomes": outcome})


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------
@bp.route("/tokens")
@require_scope("admin")
def tokens_list():
    repo = reaper_ext()["tokens"]
    return jsonify({"tokens": repo.list(include_revoked=request.args.get("all") in ("1", "true"))})


@bp.route("/tokens", methods=["POST"])
@require_scope("admin")
def tokens_create():
    body = _json_body()
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    try:
        scopes = normalise_scopes(body.get("scopes") or ["read"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    expires = None
    if body.get("expires_in"):
        try:
            expires = datetime.now(timezone.utc) + timedelta(seconds=parse_duration(body["expires_in"]))
        except UnitError as exc:
            return jsonify({"error": f"bad expires_in: {exc}"}), 400
    plain = generate_token()
    actor = resolve_principal().actor
    row = reaper_ext()["tokens"].create(name=name, prefix=token_prefix(plain),
                                        token_hash=hash_token(plain), scopes=scopes,
                                        created_by=actor, expires_at=expires)
    reaper_ext()["history"].record("token_create", "", actor=actor,
                                   detail={"name": name, "scopes": sorted(scopes), "id": row["id"]})
    return jsonify({"ok": True, "token": plain, "record": row,
                    "note": "store this token now; it cannot be shown again"}), 201


@bp.route("/tokens/<int:token_id>", methods=["DELETE"])
@require_scope("admin")
def tokens_revoke(token_id):
    row = reaper_ext()["tokens"].revoke(token_id)
    if row is None:
        return jsonify({"error": "token not found"}), 404
    reaper_ext()["token_cache"].invalidate_id(token_id)
    reaper_ext()["history"].record("token_revoke", "", actor=resolve_principal().actor,
                                   detail={"name": row["name"], "id": token_id})
    return jsonify({"ok": True, "record": row})


@bp.route("/whoami")
def whoami():
    p = resolve_principal()
    return jsonify({"kind": p.kind, "name": p.name, "scopes": sorted(p.scopes), "actor": p.actor})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@bp.route("/config")
@require_scope("read", allow_open_read=True)
def config():
    settings = get_settings()
    payload = settings.as_dict()
    payload["issues"] = list(settings.config_issues)
    payload["areas"] = [area_to_dict(a) for a in settings.areas]
    notif = {}
    for name, cfg in (settings.notifications or {}).items():
        if isinstance(cfg, dict):
            notif[name] = {k: ("***" if k in ("token", "webhook_url", "password") and v else v)
                           for k, v in cfg.items()}
        else:
            notif[name] = cfg
    payload["notifications"] = notif
    return jsonify(payload)
