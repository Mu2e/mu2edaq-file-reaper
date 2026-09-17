"""``mu2edaq-reaper`` — command line client for mu2edaq-file-reaper.

Every subcommand is a ``Command(help, add_arguments, run)`` entry in
``COMMANDS``; ``run(args, ctx)`` returns the process exit status.  Output is a
readable table by default and the raw API JSON with ``--json``.

Exit codes: 0 success, 1 API/usage error (including 401/403), 2 the server
could not be reached.
"""

import argparse
import getpass
import json
import os
import sys
import time
from collections import namedtuple
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import __version__
from . import config as configmod
from . import tokencache
from .client import ReaperClient, ReaperConnectionError, ReaperError

Command = namedtuple("Command", "help add_arguments run")

PROG = "mu2edaq-reaper"
DASH = "-"
_IEC = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


# ------------------------------------------------------------------ helpers --
def fmt_bytes(n: Any) -> str:
    """``1.2 GiB`` style (IEC).  Unknown -> ``-``."""
    if n is None:
        return DASH
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in _IEC:
        if abs(v) < 1024.0 or unit == _IEC[-1]:
            return f"{v:.0f} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024.0
    return f"{v:.1f} EiB"


def fmt_duration(seconds: Any) -> str:
    """Two most significant units: ``3d 4h``, ``4h 12m``, ``12m 5s``, ``5s``."""
    if seconds is None:
        return DASH
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return str(seconds)
    if total < 0:
        return DASH
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def fmt_pct(v: Any) -> str:
    if v is None:
        return DASH
    try:
        return f"{float(v):.1f}%"
    except (TypeError, ValueError):
        return str(v)


def fmt_ts(v: Any) -> str:
    """ISO string or epoch -> ``YYYY-mm-dd HH:MM:SS`` (UTC for epochs)."""
    if v in (None, ""):
        return DASH
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    s = str(v)
    return s.replace("T", " ").replace("+00:00", "Z").rstrip("Z")[:19] if len(s) >= 19 else s


def fmt_age(v: Any) -> str:
    """Age of an epoch/ISO timestamp as a duration."""
    if v in (None, ""):
        return DASH
    try:
        if isinstance(v, (int, float)):
            ts = float(v)
        else:
            s = str(v)
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
    except (TypeError, ValueError):
        return str(v)
    return fmt_duration(max(0.0, time.time() - ts))


def yn(v: Any) -> str:
    return "yes" if v else "no"


def table(rows: Sequence[Sequence[Any]], headers: Optional[Sequence[str]] = None,
          right: Iterable[int] = ()) -> str:
    """Plain fixed-width table.  *right* lists column indices to right-align."""
    cells = [[DASH if c is None else str(c) for c in r] for r in rows]
    if headers:
        cells.insert(0, [str(h) for h in headers])
    if not cells:
        return ""
    ncol = max(len(r) for r in cells)
    for r in cells:
        r.extend([""] * (ncol - len(r)))
    widths = [max(len(r[i]) for r in cells) for i in range(ncol)]
    right = set(right)
    out = []
    for idx, r in enumerate(cells):
        parts = []
        for i, c in enumerate(r):
            parts.append(c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
        out.append("  ".join(parts).rstrip())
        if headers and idx == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def emit_json(data: Any) -> int:
    print(json.dumps(data, indent=2, sort_keys=False, default=str))
    return 0


def err(msg: str) -> None:
    print(f"{PROG}: {msg}", file=sys.stderr)


def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


# ------------------------------------------------------------------ context --
class Context:
    """What every command receives: resolved settings and a lazy client."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.json = bool(args.json)
        self._settings: Optional[configmod.Settings] = None
        self._client: Optional[ReaperClient] = None

    @property
    def settings(self) -> configmod.Settings:
        if self._settings is None:
            a = self.args
            self._settings = configmod.resolve(
                url=a.url, instance=a.instance, token=a.token, admin_token=a.admin_token,
                timeout=a.timeout, config=a.config)
        return self._settings

    @property
    def url(self) -> str:
        return self.settings.url or configmod.DEFAULT_URL

    @property
    def client(self) -> ReaperClient:
        if self._client is None:
            s = self.settings
            self._client = ReaperClient(s.url, token=s.token, admin_token=s.admin_token,
                                        timeout=s.timeout)
        return self._client

    def admin_client(self, prompt: bool = True) -> ReaperClient:
        """Client for admin-only calls.  An explicit admin secret (CLI/env)
        takes precedence over a cached bearer token; with neither, prompt on a
        TTY."""
        s = self.settings
        if s.admin_token and s.sources.get("admin_token") in ("cli", "env"):
            return ReaperClient(s.url, token=None, admin_token=s.admin_token, timeout=s.timeout)
        if s.token:
            return self.client
        if s.admin_token:
            return ReaperClient(s.url, token=None, admin_token=s.admin_token, timeout=s.timeout)
        if prompt and sys.stdin.isatty():
            secret = getpass.getpass(f"admin token for {s.url}: ").strip()
            if secret:
                return ReaperClient(s.url, token=None, admin_token=secret, timeout=s.timeout)
        raise ReaperError(401, "no bearer token and no admin token available "
                               "(use --admin-token, MU2EDAQ_REAPER_ADMIN_TOKEN or "
                               f"'{PROG} token login')")


# ---------------------------------------------------------- area rendering --
def _area_line(a: Dict[str, Any]) -> List[Any]:
    usage = a.get("usage") or {}
    q = a.get("queues") or {}
    flags = []
    if a.get("paused"):
        flags.append("paused")
    if a.get("disabled"):
        flags.append("disabled")
    if a.get("dry_run"):
        flags.append("dry-run")
    tier = a.get("selected_tier") or (",".join(a.get("active_tiers") or []) or DASH)
    return [a.get("name"), a.get("state"), fmt_pct(usage.get("used_pct")),
            fmt_bytes(usage.get("used")), fmt_bytes(usage.get("total")), tier,
            (q.get("compress") or {}).get("total", 0), (q.get("delete") or {}).get("total", 0),
            fmt_age(a.get("last_scan_at")), " ".join(flags) or DASH]


AREA_HEADERS = ["AREA", "STATE", "USED%", "USED", "TOTAL", "TIER", "Q-COMP", "Q-DEL", "SCANNED", "FLAGS"]
AREA_RIGHT = (2, 3, 4, 6, 7)


def _print_areas(areas: List[Dict[str, Any]]) -> None:
    if not areas:
        print("(no areas configured)")
        return
    print(table([_area_line(a) for a in areas], AREA_HEADERS, right=AREA_RIGHT))


def _print_queue_entries(entries: List[Dict[str, Any]], limit: Optional[int] = None,
                         with_area: bool = True) -> None:
    rows = []
    for e in entries[:limit] if limit else entries:
        r = [e.get("queue"), e.get("tier"), e.get("status"), fmt_bytes(e.get("size")),
             fmt_ts(e.get("key_ts")), e.get("owner"), e.get("path")]
        if with_area:
            r.insert(0, e.get("area"))
        rows.append(r)
    headers = ["QUEUE", "TIER", "STATUS", "SIZE", "KEY-TS", "OWNER", "PATH"]
    right = [3]
    if with_area:
        headers.insert(0, "AREA")
        right = [4]
    if not rows:
        print("(no queue entries)")
    else:
        print(table(rows, headers, right=right))
    if limit and len(entries) > limit:
        print(f"... {len(entries) - limit} more (raise --limit)")


# ---------------------------------------------------------------- commands --
def cmd_status(args: argparse.Namespace, ctx: Context) -> int:
    health = ctx.client.health()
    state: Optional[Dict[str, Any]] = None
    state_err: Optional[str] = None
    try:
        state = ctx.client.state()
    except ReaperError as exc:
        if exc.status not in (401, 403):
            raise
        state_err = exc.message
    if ctx.json:
        return emit_json({"health": health, "state": state})
    sched = health.get("scheduler") or {}
    print(f"{health.get('label', DASH)}  mu2edaq-file-reaper {health.get('version', DASH)}  "
          f"status={health.get('status')}  uptime={fmt_duration(health.get('uptime_s'))}  "
          f"url={ctx.url}")
    print(f"  scheduler alive={yn(sched.get('alive'))} stalled={yn(sched.get('stalled'))} "
          f"tick-age={fmt_duration(sched.get('tick_age_s'))} "
          f"next-tick={fmt_duration(sched.get('next_tick_in_s'))} "
          f"busy={','.join(sched.get('busy') or []) or DASH}")
    print(f"  database={'ok' if health.get('database_ok') else 'FAIL'}  "
          f"dry-run={yn(health.get('dry_run'))}  scan-interval={fmt_duration(health.get('scan_interval'))}  "
          f"config-issues={health.get('config_issues', 0)}  areas={health.get('areas', 0)}")
    if state is None:
        print(f"  (area state unavailable: {state_err}; log in with '{PROG} token login')")
        return 0 if health.get("status") == "ok" else 1
    s = state.get("summary") or {}
    print(f"  areas: good={s.get('good', 0)} warning={s.get('warning', 0)} critical={s.get('critical', 0)} "
          f"full={s.get('full', 0)} paused={s.get('paused', 0)} disabled={s.get('disabled', 0)} "
          f"unknown={s.get('unknown', 0)} missing={s.get('missing', 0)}  "
          f"queued: compress={s.get('queued_compress', 0)} delete={s.get('queued_delete', 0)}  "
          f"acting={s.get('acting', 0)}")
    print()
    _print_areas(state.get("areas") or [])
    return 0 if health.get("status") == "ok" else 1


def cmd_areas(args: argparse.Namespace, ctx: Context) -> int:
    data = ctx.client.areas()
    if ctx.json:
        return emit_json(data)
    _print_areas(data.get("areas") or [])
    return 0


def _add_name(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", metavar="NAME", help="area name")


def cmd_area(args: argparse.Namespace, ctx: Context) -> int:
    data = ctx.client.area(args.name)
    if ctx.json:
        return emit_json(data)
    a = data.get("area") or {}
    cfg = data.get("config") or {}
    usage = a.get("usage") or {}
    print(f"{a.get('name')}  [{a.get('state')}]  {a.get('label') or ''}")
    print(f"  path:        {a.get('path')}" + (f"  (real {a['real_path']})"
                                             if a.get("real_path") and a.get("real_path") != a.get("path") else ""))
    print(f"  usage:       {fmt_pct(usage.get('used_pct'))}  used {fmt_bytes(usage.get('used'))} of "
          f"{fmt_bytes(usage.get('total'))}, free {fmt_bytes(usage.get('free'))}  "
          f"(measured {fmt_age(usage.get('ts'))} ago)")
    print(f"  flags:       enabled={yn(a.get('enabled'))} paused={yn(a.get('paused'))} "
          f"disabled={yn(a.get('disabled'))} dry-run={yn(a.get('dry_run'))} atime={a.get('atime_mode')}")
    if a.get("paused"):
        print(f"  paused:      by {a.get('paused_by')} at {fmt_ts(a.get('paused_at'))}: {a.get('paused_reason') or DASH}")
    if a.get("disabled"):
        print(f"  disabled:    by {a.get('disabled_by')} at {fmt_ts(a.get('disabled_at'))}: {a.get('disabled_reason') or DASH}")
    print(f"  active tier: {a.get('selected_tier') or DASH}  "
          f"(active: {', '.join(a.get('active_tiers') or []) or 'none'})")
    tiers = a.get("tiers") or {}
    if tiers:
        print("  tiers:")
        rows = [[name, "*" if t.get("active") else "", t.get("threshold"), fmt_pct(t.get("trigger")),
                 fmt_pct(t.get("stop")), t.get("policy"), fmt_duration(t.get("min_age")),
                 t.get("bytes_to_stop_str") or fmt_bytes(t.get("bytes_to_stop"))]
                for name, t in tiers.items()]
        print("    " + table(rows, ["TIER", "ACT", "THRESHOLD", "TRIGGER", "STOP", "POLICY", "MIN-AGE", "TO-STOP"],
                             right=(3, 4, 7)).replace("\n", "\n    "))
    q = a.get("queues") or {}
    print(f"  queues:      compress={(q.get('compress') or {}).get('total', 0)} "
          f"delete={(q.get('delete') or {}).get('total', 0)}")
    sc = a.get("scan") or {}
    print(f"  last scan:   {fmt_ts(a.get('last_scan_at'))} ({fmt_age(a.get('last_scan_at'))} ago)  "
          f"outcome={sc.get('outcome') or DASH} pass={sc.get('pass_no')} files={sc.get('files_seen')} "
          f"acted={sc.get('acted')} freed={fmt_bytes(sc.get('bytes_freed'))} "
          f"took={fmt_duration(sc.get('duration_s'))}"
          + (f"  reason={sc.get('reason')}" if sc.get("reason") else ""))
    print(f"  next scan:   in {fmt_duration(a.get('next_scan_in_s'))}")
    prog = a.get("progress") or {}
    if prog.get("acting"):
        print(f"  acting:      {prog.get('acted')} done, {prog.get('remaining')} remaining, "
              f"freed {fmt_bytes(prog.get('bytes_freed'))}; current {prog.get('current_path')}")
    rc = a.get("reject_counts") or {}
    if rc:
        print("  rejects:     " + ", ".join(f"{k}={v}" for k, v in sorted(rc.items())))
    for w in a.get("warnings") or []:
        print(f"  warning:     {w}")
    for e in a.get("config_errors") or []:
        print(f"  config err:  {e}")
    if cfg:
        print(f"  config:      settle={fmt_duration(cfg.get('settle_seconds'))} "
              + " ".join(f"{k}={v}" for k, v in cfg.items()
                         if k in ("min_free", "max_depth", "follow_symlinks", "compressor") and v is not None))
    return 0


def _add_reason(p: argparse.ArgumentParser) -> None:
    _add_name(p)
    p.add_argument("--reason", help="why (recorded in the history)")


def _area_action(verb: str) -> Callable[[argparse.Namespace, Context], int]:
    def run(args: argparse.Namespace, ctx: Context) -> int:
        client = ctx.client
        if verb in ("enable", "disable") and not ctx.settings.token:
            client = ctx.admin_client()
        res = getattr(client, verb)(args.name, reason=getattr(args, "reason", None))
        if ctx.json:
            return emit_json(res)
        st = res.get("state") or {}
        print(f"{verb} {res.get('area')}: ok (actor {res.get('actor')})"
              + (f"  reason: {res.get('reason')}" if res.get("reason") else ""))
        if st:
            print(f"  state={st.get('state')} paused={yn(st.get('paused'))} disabled={yn(st.get('disabled'))}")
        return 0
    return run


def _add_plan(p: argparse.ArgumentParser) -> None:
    _add_name(p)
    p.add_argument("--limit", type=int, default=20, help="queue entries to show (default 20)")


def cmd_plan(args: argparse.Namespace, ctx: Context) -> int:
    res = ctx.client.dry_run(args.name)
    if ctx.json:
        return emit_json(res)
    r = res.get("report") or {}
    st = res.get("state") or {}
    print(f"dry-run plan for {res.get('area')}  scan={r.get('scan_id')}  outcome={r.get('outcome')}"
          + (f"  reason={r.get('reason')}" if r.get("reason") else ""))
    print(f"  passes={r.get('passes')} would-act={r.get('acted')} would-free={fmt_bytes(r.get('bytes_freed'))} "
          f"took={fmt_duration(r.get('duration_s'))}")
    usage = st.get("usage") or {}
    print(f"  usage now {fmt_pct(usage.get('used_pct'))}; active tier {st.get('selected_tier') or DASH}")
    entries: List[Dict[str, Any]] = []
    for kind in ("compress", "delete"):
        q = (st.get("queues") or {}).get(kind) or {}
        for e in q.get("entries") or []:
            entries.append({**e, "queue": e.get("queue") or kind})
        if q.get("total", 0) > q.get("shown", 0):
            print(f"  {kind}: {q.get('total')} queued ({q.get('shown')} in snapshot)")
    print()
    _print_queue_entries(entries, limit=args.limit, with_area=False)
    return 0


def _add_queues(p: argparse.ArgumentParser) -> None:
    p.add_argument("--area")
    p.add_argument("--kind", choices=["compress", "delete"])
    p.add_argument("--status", help="queued|acting|done|skipped|... (server-defined)")
    p.add_argument("--limit", type=int, default=50)


def cmd_queues(args: argparse.Namespace, ctx: Context) -> int:
    data = ctx.client.queues(area=args.area, kind=args.kind, status=args.status)
    if ctx.json:
        return emit_json(data)
    t = data.get("totals") or {}
    print(f"queued: compress={t.get('compress', 0)} delete={t.get('delete', 0)}  "
          f"matching={data.get('total', 0)}")
    _print_queue_entries(data.get("entries") or [], limit=args.limit)
    return 0


# ---- exclude ---------------------------------------------------------------
def _add_exclude(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="subcommand", metavar="<add|rm|list>")
    a = sub.add_parser("add", help="add an exclusion pattern")
    a.add_argument("pattern", metavar="PATTERN", help="absolute path or glob")
    a.add_argument("--area", help="limit to one area (default: all)")
    a.add_argument("--kind", choices=["path", "glob", "prefix", "regex"], help="pattern kind (auto)")
    a.add_argument("--reason")
    a.add_argument("--expires-in", metavar="DURATION", help="e.g. 6h, 7d")
    r = sub.add_parser("rm", help="remove an exclusion by id")
    r.add_argument("id", type=int, metavar="ID")
    ls = sub.add_parser("list", help="list exclusions")
    ls.add_argument("--area")
    ls.add_argument("--all", action="store_true", help="include inactive/expired")


def _print_exclusions(rows: List[Dict[str, Any]]) -> None:
    if not rows:
        print("(no exclusions)")
        return
    print(table([[x.get("id"), x.get("area") or "*", x.get("kind"), x.get("pattern"),
                  fmt_ts(x.get("expires_at")), yn(x.get("active")), x.get("created_by"),
                  x.get("reason") or DASH] for x in rows],
                ["ID", "AREA", "KIND", "PATTERN", "EXPIRES", "ACTIVE", "BY", "REASON"], right=(0,)))


def cmd_exclude(args: argparse.Namespace, ctx: Context) -> int:
    if args.subcommand == "add":
        res = ctx.client.add_exclusion(args.pattern, area=args.area, kind=args.kind,
                                       reason=args.reason, expires_in=args.expires_in)
        if ctx.json:
            return emit_json(res)
        x = res.get("exclusion") or {}
        print(f"added exclusion #{x.get('id')}: {x.get('kind')} {x.get('pattern')} "
              f"area={x.get('area') or '*'} expires={fmt_ts(x.get('expires_at'))}")
        return 0
    if args.subcommand == "rm":
        res = ctx.client.remove_exclusion(args.id)
        if ctx.json:
            return emit_json(res)
        x = res.get("exclusion") or {}
        print(f"removed exclusion #{x.get('id', args.id)}: {x.get('pattern')}")
        return 0
    res = ctx.client.exclusions(area=getattr(args, "area", None), all=getattr(args, "all", False))
    if ctx.json:
        return emit_json(res)
    _print_exclusions(res.get("exclusions") or [])
    return 0


# ---- history ---------------------------------------------------------------
def _add_history(p: argparse.ArgumentParser) -> None:
    p.add_argument("--area")
    p.add_argument("--path", metavar="GLOB")
    p.add_argument("--type", metavar="T1,T2", help="event types (see 'history --types')")
    p.add_argument("--tier")
    p.add_argument("--since", metavar="TS|DURATION", help="ISO time or relative, e.g. 24h")
    p.add_argument("--until", metavar="TS|DURATION")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--asc", action="store_true", help="oldest first")
    p.add_argument("--csv", metavar="FILE", help="write CSV to FILE ('-' for stdout)")
    p.add_argument("--types", action="store_true", help="list the known event types and exit")


def cmd_history(args: argparse.Namespace, ctx: Context) -> int:
    if args.types:
        res = ctx.client.history_types()
        if ctx.json:
            return emit_json(res)
        for t in res.get("types") or []:
            print(t)
        return 0
    filters = dict(area=args.area, path=args.path, types=_split_csv(args.type), tier=args.tier,
                   since=args.since, until=args.until, limit=args.limit, offset=args.offset,
                   asc=args.asc)
    if args.csv:
        text = ctx.client.history_csv(**filters)
        if args.csv == "-":
            sys.stdout.write(text)
        else:
            with open(args.csv, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            print(f"wrote {max(0, text.count(chr(10)) - 1)} rows to {args.csv}")
        return 0
    data = ctx.client.history(**filters)
    if ctx.json:
        return emit_json(data)
    events = data.get("events") or []
    print(f"history: {len(events)} of {data.get('total', 0)} events "
          f"(offset {data.get('offset', 0)}, limit {data.get('limit')})")
    if not events:
        return 0
    rows = []
    for e in events:
        detail = e.get("path") or ""
        extra = []
        if e.get("bytes_freed"):
            extra.append(f"freed {fmt_bytes(e['bytes_freed'])}")
        elif e.get("size_bytes"):
            extra.append(fmt_bytes(e["size_bytes"]))
        if e.get("count"):
            extra.append(f"n={e['count']}")
        if e.get("reason"):
            extra.append(str(e["reason"]))
        rows.append([e.get("id"), fmt_ts(e.get("ts")), e.get("area") or DASH, e.get("event_type"),
                     e.get("tier") or DASH, e.get("outcome") or DASH, e.get("actor") or DASH,
                     (detail + ("  " if detail and extra else "") + " ".join(extra)) or DASH])
    print(table(rows, ["ID", "TIME", "AREA", "EVENT", "TIER", "OUTCOME", "ACTOR", "DETAIL"], right=(0,)))
    return 0


# ---- notify ----------------------------------------------------------------
def _add_notify(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="subcommand", metavar="<test|status>")
    t = sub.add_parser("test", help="send a test notification (admin)")
    t.add_argument("channel", nargs="?", metavar="CHANNEL", help="one channel (default: all)")
    t.add_argument("--severity", default=None, choices=["info", "warning", "critical"])
    s = sub.add_parser("status", help="channel status and recent sends")
    s.add_argument("--limit", type=int, default=20)


def cmd_notify(args: argparse.Namespace, ctx: Context) -> int:
    if args.subcommand == "test":
        client = ctx.client if ctx.settings.token else ctx.admin_client()
        res = client.test_notification(channel=args.channel, severity=args.severity)
        if ctx.json:
            return emit_json(res)
        if res.get("outcome") == "disabled":
            print(f"{res.get('channel')}: disabled ({res.get('reason')})")
            return 1
        for ch, outcome in (res.get("outcomes") or {}).items():
            print(f"{ch}: {outcome}")
        if not res.get("outcomes"):
            print("no channels configured")
        return 0
    res = ctx.client.notifications(limit=getattr(args, "limit", None))
    if ctx.json:
        return emit_json(res)
    chans = res.get("channels") or []
    print(f"notifications: sent={res.get('sent', 0)} failed={res.get('failed', 0)} channels={len(chans)}")
    if chans:
        rows = []
        for c in chans:
            rows.append([c.get("name"), c.get("kind") or c.get("type") or DASH, yn(c.get("enabled")),
                         fmt_ts(c.get("last_sent")), c.get("disabled_reason") or c.get("detail") or DASH])
        print(table(rows, ["CHANNEL", "KIND", "ENABLED", "LAST-SENT", "NOTE"]))
    recent = res.get("recent") or []
    if recent:
        print()
        print(table([[fmt_ts(r.get("ts")), r.get("channel"), r.get("severity"), r.get("outcome"),
                      r.get("title") or r.get("key") or DASH] for r in recent],
                    ["TIME", "CHANNEL", "SEVERITY", "OUTCOME", "TITLE"]))
    return 0


# ---- token -----------------------------------------------------------------
def _add_token(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="subcommand",
                           metavar="<create|list|revoke|login|logout|whoami>")
    c = sub.add_parser("create", help="mint a new API token (admin)")
    c.add_argument("name", metavar="NAME")
    c.add_argument("--scopes", default="read", metavar="S1,S2", help="read,operate,admin (default read)")
    c.add_argument("--expires-in", metavar="DURATION", help="e.g. 30d")
    c.add_argument("--save", action="store_true", help="store the new token in the cache for this url")
    ls = sub.add_parser("list", help="list tokens (admin)")
    ls.add_argument("--all", action="store_true", help="include revoked")
    r = sub.add_parser("revoke", help="revoke a token by id (admin)")
    r.add_argument("id", type=int, metavar="ID")
    lg = sub.add_parser("login", help="store a token in the cache for this url")
    lg.add_argument("token", nargs="?", metavar="TOKEN", help="prompted if omitted")
    lg.add_argument("--name", help="label for the cache entry")
    sub.add_parser("logout", help="forget the cached token for this url")
    sub.add_parser("whoami", help="show the identity and scopes the server sees")


def cmd_token(args: argparse.Namespace, ctx: Context) -> int:
    sc = args.subcommand
    if sc == "create":
        client = ctx.admin_client()
        res = client.create_token(args.name, args.scopes, expires_in=args.expires_in)
        plain = res.get("token")
        if args.save and plain:
            tokencache.save(ctx.url, plain, args.name, ctx.settings.token_file)
        if ctx.json:
            return emit_json(res)
        rec = res.get("record") or {}
        print(f"created token #{rec.get('id')} {rec.get('name')} scopes={','.join(rec.get('scopes') or [])} "
              f"expires={fmt_ts(rec.get('expires_at'))}")
        print(f"token: {plain}")
        print(f"({res.get('note') or 'store this token now; it cannot be shown again'})")
        if args.save:
            print(f"saved to {tokencache.cache_path(ctx.settings.token_file)} for {ctx.url}")
        return 0
    if sc == "list":
        client = ctx.client if ctx.settings.token else ctx.admin_client()
        res = client.tokens(all=args.all)
        if ctx.json:
            return emit_json(res)
        rows = res.get("tokens") or []
        if not rows:
            print("(no tokens)")
            return 0
        print(table([[t.get("id"), t.get("name"), t.get("prefix"), ",".join(t.get("scopes") or []),
                      yn(t.get("active")), fmt_ts(t.get("created_at")), t.get("created_by"),
                      fmt_ts(t.get("expires_at")), fmt_ts(t.get("last_used_at")),
                      fmt_ts(t.get("revoked_at"))] for t in rows],
                    ["ID", "NAME", "PREFIX", "SCOPES", "ACTIVE", "CREATED", "BY", "EXPIRES",
                     "LAST-USED", "REVOKED"], right=(0,)))
        return 0
    if sc == "revoke":
        client = ctx.client if ctx.settings.token else ctx.admin_client()
        res = client.revoke_token(args.id)
        if ctx.json:
            return emit_json(res)
        rec = res.get("record") or {}
        print(f"revoked token #{rec.get('id', args.id)} {rec.get('name') or ''}".rstrip())
        return 0
    if sc == "login":
        plain = args.token
        if not plain:
            if not sys.stdin.isatty():
                err("no token given and stdin is not a terminal")
                return 1
            plain = getpass.getpass(f"API token for {ctx.url}: ").strip()
        if not plain:
            err("empty token")
            return 1
        path = tokencache.save(ctx.url, plain, args.name or "", ctx.settings.token_file)
        # Verify it, but a failure is not fatal: the server may be down.
        ident = None
        try:
            ident = ReaperClient(ctx.url, token=plain, timeout=ctx.settings.timeout).whoami()
        except (ReaperError, ReaperConnectionError) as exc:
            print(f"warning: could not verify token against {ctx.url}: {exc}", file=sys.stderr)
        if ctx.json:
            return emit_json({"url": ctx.url, "saved": path, "prefix": plain[:8] + "...",
                              "whoami": ident})
        print(f"stored token {plain[:8]}... for {ctx.url} in {path}")
        if ident:
            if ident.get("kind") == "anonymous":
                print("warning: the server does not recognise this token")
                return 1
            print(f"identity: {ident.get('actor')} scopes={','.join(ident.get('scopes') or [])}")
        return 0
    if sc == "logout":
        removed = tokencache.remove(ctx.url, ctx.settings.token_file)
        if ctx.json:
            return emit_json({"url": ctx.url, "removed": removed})
        print(f"{'removed' if removed else 'no'} cached token for {ctx.url}")
        return 0
    # whoami
    res = ctx.client.whoami()
    if ctx.json:
        return emit_json(res)
    src = ctx.settings.sources.get("token", "none")
    print(f"{ctx.url}: {res.get('kind')} {res.get('name')} actor={res.get('actor')} "
          f"scopes={','.join(res.get('scopes') or []) or 'none'}  (token from {src})")
    return 0 if res.get("kind") != "anonymous" else 1


# ---- instances / config / version -----------------------------------------
def _add_instances(p: argparse.ArgumentParser) -> None:
    p.add_argument("--discovery-timeout", type=float, default=None, metavar="SECONDS")


def cmd_instances(args: argparse.Namespace, ctx: Context) -> int:
    timeout = args.discovery_timeout
    if timeout is None:
        try:
            timeout = configmod.resolve(url=None, instance=None, config=ctx.args.config,
                                        use_discovery=False).discovery_timeout
        except configmod.ConfigError:
            timeout = 2.0
    try:
        recs = configmod.discover_instances(timeout=timeout)
    except ImportError:
        err("the mu2edaq_discovery package is not installed; install mu2edaq-discovery "
            "(pip install -e ../mu2edaq-discovery) or use --url")
        return 1
    if ctx.json:
        return emit_json(recs)
    if not recs:
        print("no file-reaper instances answered discovery")
        return 1
    rows = []
    for r in recs:
        m = r.get("meta") or {}
        rows.append([m.get("instance") or DASH, r.get("host"), r.get("port"), m.get("api_port") or DASH,
                     r.get("version") or DASH, m.get("dry_run") or DASH, m.get("areas") or DASH,
                     configmod.endpoint_url(r)])
    print(table(rows, ["INSTANCE", "HOST", "PORT", "API-PORT", "VERSION", "DRY-RUN", "AREAS", "URL"],
                right=(2, 3, 6)))
    return 0


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--local", action="store_true", help="show this client's resolved settings instead")


def cmd_config(args: argparse.Namespace, ctx: Context) -> int:
    if args.local:
        d = ctx.settings.as_dict()
        if ctx.json:
            return emit_json(d)
        print(f"config file: {d['config_file'] or '(none found)'}")
        for k in ("url", "instance", "timeout", "token_file", "discovery_timeout", "token", "admin_token"):
            print(f"  {k:<18} {d[k] if d[k] is not None else DASH}   [{d['sources'].get(k, '-')}]")
        return 0
    data = ctx.client.config()
    if ctx.json:
        return emit_json(data)
    areas = data.get("areas") or []
    notif = data.get("notifications") or {}
    issues = data.get("issues") or []
    scalars = {k: v for k, v in data.items() if k not in ("areas", "notifications", "issues")
               and not isinstance(v, (dict, list))}
    width = max(len(k) for k in scalars) if scalars else 10
    for k in sorted(scalars):
        print(f"  {k:<{width}}  {scalars[k]}")
    print(f"  {'areas':<{width}}  {len(areas)}: " + ", ".join(str(a.get("name")) for a in areas))
    print(f"  {'notifications':<{width}}  " + (", ".join(sorted(notif)) or "none"))
    if issues:
        print("  config issues:")
        for i in issues:
            print(f"    - {i}")
    return 0


def cmd_version(args: argparse.Namespace, ctx: Context) -> int:
    local = {"client": PROG, "version": __version__}
    remote: Optional[Dict[str, Any]] = None
    remote_err: Optional[str] = None
    if not args.local:
        try:
            remote = ctx.client.version()
        except (ReaperError, ReaperConnectionError) as exc:
            remote_err = str(exc)
    if ctx.json:
        return emit_json({**local, "server": remote, "server_error": remote_err})
    print(f"{PROG} {__version__}")
    if remote:
        print(f"server {remote.get('name')} {remote.get('version')} label={remote.get('label')} "
              f"api={remote.get('api')} at {ctx.url}")
    elif remote_err:
        print(f"server: unavailable ({remote_err})")
    return 0


def _add_version(p: argparse.ArgumentParser) -> None:
    p.add_argument("--local", action="store_true", help="do not contact the server")


COMMANDS: Dict[str, Command] = {
    "status":    Command("health, summary counts and one line per area", None, cmd_status),
    "areas":     Command("table of every area", None, cmd_areas),
    "area":      Command("details of one area (usage, tiers, queues, last scan)", _add_name, cmd_area),
    "pause":     Command("pause reaping of an area (operate)", _add_reason, _area_action("pause")),
    "resume":    Command("resume a paused area (operate)", _add_reason, _area_action("resume")),
    "disable":   Command("disable an area entirely (admin)", _add_reason, _area_action("disable")),
    "enable":    Command("re-enable a disabled area (admin)", _add_reason, _area_action("enable")),
    "rescan":    Command("request an immediate scan (operate)", _add_name, _area_action("rescan")),
    "plan":      Command("dry-run scan now and show what would be done (operate)", _add_plan, cmd_plan),
    "queues":    Command("list queued compress/delete entries", _add_queues, cmd_queues),
    "exclude":   Command("manage exclusion patterns (add|rm|list)", _add_exclude, cmd_exclude),
    "history":   Command("query the audit history", _add_history, cmd_history),
    "notify":    Command("notification channels (test|status)", _add_notify, cmd_notify),
    "token":     Command("API tokens (create|list|revoke|login|logout|whoami)", _add_token, cmd_token),
    "instances": Command("discover file-reaper instances on the network", _add_instances, cmd_instances),
    "config":    Command("show the server configuration (or --local for the client's)", _add_config, cmd_config),
    "version":   Command("client and server versions", _add_version, cmd_version),
}

#: Commands that never contact a server through ``ctx.client`` at parse time.
_NEEDS_SUBCOMMAND = ("exclude", "notify", "token")


# ------------------------------------------------------------------- parser --
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Command line client for mu2edaq-file-reaper instances (REST API v1).",
        epilog="Options override the environment (MU2EDAQ_REAPER_URL, _TOKEN, _ADMIN_TOKEN, "
               "_INSTANCE, _TIMEOUT, _CONFIG), which overrides the YAML config file "
               "(mu2edaq-reaper.yaml), which overrides the defaults.  Tokens are cached per URL in "
               f"{tokencache.DEFAULT_PATH} (owner-only).")
    parser.add_argument("-V", "--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--url", metavar="URL", help=f"reaper base URL (default {configmod.DEFAULT_URL})")
    parser.add_argument("--instance", metavar="LABEL", help="find the reaper by instance label via discovery")
    parser.add_argument("--token", metavar="TOKEN", help="bearer token (default: env, then cache)")
    parser.add_argument("--admin-token", metavar="SECRET", help="admin secret, for minting the first token")
    parser.add_argument("--timeout", type=float, metavar="SECONDS", help="HTTP timeout (default 10)")
    parser.add_argument("--json", action="store_true", help="print the raw API JSON")
    parser.add_argument("-c", "--config", metavar="FILE", help="YAML config file")
    subs = parser.add_subparsers(dest="command", metavar="<command>")
    for name, cmd in COMMANDS.items():
        sub = subs.add_parser(name, help=cmd.help, description=cmd.help)
        if cmd.add_arguments is not None:
            cmd.add_arguments(sub)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command in _NEEDS_SUBCOMMAND and not getattr(args, "subcommand", None):
        parser.parse_args([args.command, "--help"])
        return 2
    ctx = Context(args)
    try:
        return int(COMMANDS[args.command].run(args, ctx) or 0)
    except ReaperConnectionError as exc:
        err(f"cannot reach {exc.url}: {exc.reason}")
        return 2
    except ReaperError as exc:
        if exc.status in (401, 403):
            err(f"{exc.message} (HTTP {exc.status})")
            err(f"hint: store a token with '{PROG} token login', pass --token, or set "
                f"MU2EDAQ_REAPER_TOKEN; an admin can mint one with '{PROG} token create NAME "
                f"--scopes operate --admin-token SECRET --save'")
        else:
            err(f"{exc.message} (HTTP {exc.status})")
        return 1
    except (configmod.ConfigError, tokencache.TokenCacheError) as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        err("interrupted")
        return 130
    except (OSError, ValueError) as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
