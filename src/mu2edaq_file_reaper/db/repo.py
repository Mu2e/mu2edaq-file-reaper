"""Repositories: the only code that touches the ORM.

Each method is one short transaction.  Return values are plain dicts (JSON
ready) or simple Python values, never live ORM objects, so callers cannot
accidentally hold a session open.
"""

import fnmatch
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select, update

from . import Database
from .models import (
    ApiToken,
    AreaState,
    Exclusion,
    HistoryEvent,
    NotificationLog,
    _aware,
    utcnow,
)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return _aware(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def glob_to_like(pattern: str) -> str:
    """fnmatch glob -> SQL LIKE pattern, escaping LIKE metacharacters with ``\\``."""
    out = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch == "?":
            out.append("_")
        elif ch in ("%", "_", "\\"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
class HistoryRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, event_type: str, area: str = "", **fields: Any) -> int:
        with self.db.session_scope() as s:
            row = HistoryEvent(event_type=event_type, area=area or "", **fields)
            s.add(row)
            s.flush()
            return row.id

    def record_many(self, rows: Iterable[Dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0
        with self.db.session_scope() as s:
            s.execute(HistoryEvent.__table__.insert(), rows)
        return len(rows)

    @staticmethod
    def _to_dict(e: HistoryEvent) -> Dict[str, Any]:
        return {
            "id": e.id, "ts": _iso(e.ts), "area": e.area, "event_type": e.event_type,
            "path": e.path, "tier": e.tier, "policy": e.policy, "queue": e.queue,
            "outcome": e.outcome, "reason": e.reason, "size_bytes": e.size_bytes,
            "bytes_freed": e.bytes_freed, "duration_s": e.duration_s,
            "used_pct_before": e.used_pct_before, "used_pct_after": e.used_pct_after,
            "count": e.count, "actor": e.actor, "scan_id": e.scan_id, "detail": e.detail or {},
        }

    def query(self, area: Optional[str] = None, path_glob: Optional[str] = None,
              event_types: Optional[Sequence[str]] = None, tier: Optional[str] = None,
              scan_id: Optional[str] = None, since: Optional[datetime] = None,
              until: Optional[datetime] = None, limit: int = 100, offset: int = 0,
              newest_first: bool = True) -> Tuple[List[Dict[str, Any]], int]:
        limit = max(1, min(int(limit), 10000))
        offset = max(0, int(offset))
        with self.db.session_scope() as s:
            stmt = select(HistoryEvent)
            cnt = select(func.count(HistoryEvent.id))
            conds = []
            if area:
                conds.append(HistoryEvent.area == area)
            if path_glob:
                if any(ch in path_glob for ch in "*?"):
                    conds.append(HistoryEvent.path.like(glob_to_like(path_glob), escape="\\"))
                else:
                    conds.append(HistoryEvent.path == path_glob)
            if event_types:
                conds.append(HistoryEvent.event_type.in_(list(event_types)))
            if tier:
                conds.append(HistoryEvent.tier == tier)
            if scan_id:
                conds.append(HistoryEvent.scan_id == scan_id)
            if since is not None:
                conds.append(HistoryEvent.ts >= since)
            if until is not None:
                conds.append(HistoryEvent.ts <= until)
            for c in conds:
                stmt = stmt.where(c)
                cnt = cnt.where(c)
            order = HistoryEvent.id.desc() if newest_first else HistoryEvent.id.asc()
            stmt = stmt.order_by(order).limit(limit).offset(offset)
            rows = [self._to_dict(e) for e in s.scalars(stmt)]
            total = s.scalar(cnt) or 0
        return rows, int(total)

    def recent_usage(self, area: str, limit: int = 200) -> List[Dict[str, Any]]:
        """(ts, used_pct) points from scan_end events, oldest first — for sparklines."""
        with self.db.session_scope() as s:
            stmt = (select(HistoryEvent.ts, HistoryEvent.used_pct_after, HistoryEvent.detail)
                    .where(HistoryEvent.area == area, HistoryEvent.event_type == "scan_end")
                    .order_by(HistoryEvent.id.desc()).limit(limit))
            pts = [{"ts": _iso(ts), "used_pct": pct} for ts, pct, _d in s.execute(stmt) if pct is not None]
        pts.reverse()
        return pts

    def prune(self, older_than: datetime) -> int:
        with self.db.session_scope() as s:
            result = s.execute(delete(HistoryEvent).where(HistoryEvent.ts < older_than))
            return result.rowcount or 0

    def prune_days(self, days: int) -> int:
        if days <= 0:
            return 0
        return self.prune(utcnow() - timedelta(days=days))


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------
class ExclusionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _to_dict(x: Exclusion) -> Dict[str, Any]:
        return {"id": x.id, "area": x.area, "pattern": x.pattern, "kind": x.kind,
                "reason": x.reason, "created_by": x.created_by,
                "created_at": _iso(x.created_at), "expires_at": _iso(x.expires_at),
                "active": x.active}

    def add(self, pattern: str, area: Optional[str] = None, kind: Optional[str] = None,
            reason: Optional[str] = None, created_by: str = "system",
            expires_at: Optional[datetime] = None) -> Dict[str, Any]:
        kind = kind or ("glob" if any(ch in pattern for ch in "*?[") else "path")
        with self.db.session_scope() as s:
            existing = s.scalar(select(Exclusion).where(Exclusion.area == area,
                                                        Exclusion.pattern == pattern))
            if existing is not None:
                existing.active = True
                existing.kind = kind
                existing.reason = reason
                existing.created_by = created_by
                existing.expires_at = expires_at
                existing.created_at = utcnow()
                s.flush()
                return self._to_dict(existing)
            row = Exclusion(area=area, pattern=pattern, kind=kind, reason=reason,
                            created_by=created_by, expires_at=expires_at)
            s.add(row)
            s.flush()
            return self._to_dict(row)

    def remove(self, exclusion_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as s:
            row = s.get(Exclusion, exclusion_id)
            if row is None:
                return None
            row.active = False
            s.flush()
            return self._to_dict(row)

    def list(self, area: Optional[str] = None, include_inactive: bool = False) -> List[Dict[str, Any]]:
        with self.db.session_scope() as s:
            stmt = select(Exclusion).order_by(Exclusion.id.asc())
            if not include_inactive:
                stmt = stmt.where(Exclusion.active.is_(True))
            if area:
                stmt = stmt.where((Exclusion.area == area) | (Exclusion.area.is_(None)))
            return [self._to_dict(x) for x in s.scalars(stmt)]

    def active_rules(self) -> List[Dict[str, Any]]:
        """Active, unexpired rules (expired ones are deactivated as a side effect)."""
        now = utcnow()
        with self.db.session_scope() as s:
            rows = list(s.scalars(select(Exclusion).where(Exclusion.active.is_(True))))
            out = []
            for x in rows:
                if x.expires_at is not None and _aware(x.expires_at) <= now:
                    x.active = False
                    continue
                out.append(self._to_dict(x))
            return out


# ---------------------------------------------------------------------------
# API tokens
# ---------------------------------------------------------------------------
class TokenRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _to_dict(t: ApiToken) -> Dict[str, Any]:
        return {"id": t.id, "name": t.name, "prefix": t.prefix, "scopes": sorted(t.scope_set),
                "created_by": t.created_by, "created_at": _iso(t.created_at),
                "expires_at": _iso(t.expires_at), "revoked_at": _iso(t.revoked_at),
                "last_used_at": _iso(t.last_used_at), "last_used_ip": t.last_used_ip,
                "active": t.active}

    def create(self, name: str, prefix: str, token_hash: str, scopes: Iterable[str],
               created_by: str = "system", expires_at: Optional[datetime] = None) -> Dict[str, Any]:
        with self.db.session_scope() as s:
            row = ApiToken(name=name, prefix=prefix, token_hash=token_hash,
                           scopes=",".join(sorted(set(scopes))), created_by=created_by,
                           expires_at=expires_at)
            s.add(row)
            s.flush()
            return self._to_dict(row)

    def candidates(self, prefix: str) -> List[Dict[str, Any]]:
        """Active tokens sharing *prefix*, with their hashes, for verification."""
        with self.db.session_scope() as s:
            rows = s.scalars(select(ApiToken).where(ApiToken.prefix == prefix,
                                                    ApiToken.revoked_at.is_(None)))
            return [dict(self._to_dict(t), token_hash=t.token_hash) for t in rows if t.active]

    def touch(self, token_id: int, ip: Optional[str] = None) -> None:
        with self.db.session_scope() as s:
            s.execute(update(ApiToken).where(ApiToken.id == token_id)
                      .values(last_used_at=utcnow(), last_used_ip=ip))

    def revoke(self, token_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as s:
            row = s.get(ApiToken, token_id)
            if row is None:
                return None
            if row.revoked_at is None:
                row.revoked_at = utcnow()
            s.flush()
            return self._to_dict(row)

    def list(self, include_revoked: bool = True) -> List[Dict[str, Any]]:
        with self.db.session_scope() as s:
            stmt = select(ApiToken).order_by(ApiToken.id.asc())
            if not include_revoked:
                stmt = stmt.where(ApiToken.revoked_at.is_(None))
            return [self._to_dict(t) for t in s.scalars(stmt)]

    def get(self, token_id: int) -> Optional[Dict[str, Any]]:
        with self.db.session_scope() as s:
            row = s.get(ApiToken, token_id)
            return None if row is None else self._to_dict(row)

    def count_active(self) -> int:
        with self.db.session_scope() as s:
            rows = s.scalars(select(ApiToken).where(ApiToken.revoked_at.is_(None)))
            return sum(1 for t in rows if t.active)


# ---------------------------------------------------------------------------
# Area runtime state
# ---------------------------------------------------------------------------
class AreaStateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _to_dict(a: AreaState) -> Dict[str, Any]:
        return {"area": a.area, "path": a.path,
                "paused": a.paused, "paused_reason": a.paused_reason, "paused_by": a.paused_by,
                "paused_at": _iso(a.paused_at),
                "disabled": a.disabled, "disabled_reason": a.disabled_reason,
                "disabled_by": a.disabled_by, "disabled_at": _iso(a.disabled_at),
                "active_tiers": list(a.active_tiers or []),
                "last_scan_at": _iso(a.last_scan_at), "last_scan_id": a.last_scan_id,
                "updated_at": _iso(a.updated_at)}

    def _get_or_create(self, s, area: str, path: Optional[str] = None) -> AreaState:
        row = s.get(AreaState, area)
        if row is None:
            row = AreaState(area=area, path=path, active_tiers=[])
            s.add(row)
            s.flush()
        elif path and row.path != path:
            row.path = path
        return row

    def load(self, area: str, path: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session_scope() as s:
            return self._to_dict(self._get_or_create(s, area, path))

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self.db.session_scope() as s:
            return {a.area: self._to_dict(a) for a in s.scalars(select(AreaState))}

    def set_paused(self, area: str, paused: bool, by: str = "system",
                   reason: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session_scope() as s:
            row = self._get_or_create(s, area)
            row.paused = paused
            row.paused_reason = reason if paused else None
            row.paused_by = by if paused else None
            row.paused_at = utcnow() if paused else None
            s.flush()
            return self._to_dict(row)

    def set_disabled(self, area: str, disabled: bool, by: str = "system",
                     reason: Optional[str] = None) -> Dict[str, Any]:
        with self.db.session_scope() as s:
            row = self._get_or_create(s, area)
            row.disabled = disabled
            row.disabled_reason = reason if disabled else None
            row.disabled_by = by if disabled else None
            row.disabled_at = utcnow() if disabled else None
            s.flush()
            return self._to_dict(row)

    def save_active_tiers(self, area: str, tiers: Iterable[str]) -> None:
        with self.db.session_scope() as s:
            row = self._get_or_create(s, area)
            row.active_tiers = sorted(tiers)

    def mark_scan(self, area: str, scan_id: str) -> None:
        with self.db.session_scope() as s:
            row = self._get_or_create(s, area)
            row.last_scan_at = utcnow()
            row.last_scan_id = scan_id


# ---------------------------------------------------------------------------
# Notification log
# ---------------------------------------------------------------------------
class NotificationLogRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, channel: str, severity: str, title: str, outcome: str,
               area: Optional[str] = None, event_key: str = "", body: Optional[str] = None,
               error: Optional[str] = None, detail: Optional[dict] = None) -> int:
        with self.db.session_scope() as s:
            row = NotificationLog(channel=channel, severity=severity, area=area,
                                  event_key=event_key[:160], title=title[:256], body=body,
                                  outcome=outcome, error=error, detail=detail)
            s.add(row)
            s.flush()
            return row.id

    def recent(self, limit: int = 100, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.db.session_scope() as s:
            stmt = select(NotificationLog).order_by(NotificationLog.id.desc()).limit(limit)
            if channel:
                stmt = stmt.where(NotificationLog.channel == channel)
            return [{"id": n.id, "ts": _iso(n.ts), "channel": n.channel, "severity": n.severity,
                     "area": n.area, "event_key": n.event_key, "title": n.title, "body": n.body,
                     "outcome": n.outcome, "error": n.error, "detail": n.detail or {}}
                    for n in s.scalars(stmt)]

    def last_sent(self) -> Dict[str, Optional[str]]:
        with self.db.session_scope() as s:
            stmt = (select(NotificationLog.channel, func.max(NotificationLog.ts))
                    .where(NotificationLog.outcome == "sent").group_by(NotificationLog.channel))
            return {ch: _iso(ts) for ch, ts in s.execute(stmt)}

    def prune_days(self, days: int) -> int:
        if days <= 0:
            return 0
        with self.db.session_scope() as s:
            result = s.execute(delete(NotificationLog)
                               .where(NotificationLog.ts < utcnow() - timedelta(days=days)))
            return result.rowcount or 0
