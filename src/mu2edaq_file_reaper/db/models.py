"""SQLAlchemy 2.x ORM models.

Byte counts are ``BigInteger`` (SQLite stores 64-bit INTEGER; Postgres needs the
type).  ``JSON`` maps to TEXT on SQLite.  Every timestamp is a UTC-aware
datetime; the repository layer never hands out naive values.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


#: Vocabulary for HistoryEvent.event_type.
EVENT_TYPES = (
    "scan_start", "scan_end", "scan_abort",
    "tier_activate", "tier_deactivate",
    "queue_add", "queue_remove",
    "action_delete", "action_compress", "action_failed",
    "policy_insufficient", "space_not_reclaimed",
    "exclusion_add", "exclusion_remove",
    "area_pause", "area_resume", "area_disable", "area_enable",
    "notification_sent", "token_create", "token_revoke", "maintenance",
)


class Meta(Base):
    __tablename__ = "meta"
    key:   Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), nullable=False)


class HistoryEvent(Base):
    __tablename__ = "history_events"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts:         Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    area:       Mapped[str] = mapped_column(String(128), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    path:       Mapped[Optional[str]] = mapped_column(String(1024))
    tier:       Mapped[Optional[str]] = mapped_column(String(16))
    policy:     Mapped[Optional[str]] = mapped_column(String(16))
    queue:      Mapped[Optional[str]] = mapped_column(String(8))
    outcome:    Mapped[Optional[str]] = mapped_column(String(16))
    reason:     Mapped[Optional[str]] = mapped_column(String(64))
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    bytes_freed: Mapped[Optional[int]] = mapped_column(BigInteger)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    used_pct_before: Mapped[Optional[float]] = mapped_column(Float)
    used_pct_after:  Mapped[Optional[float]] = mapped_column(Float)
    count:      Mapped[Optional[int]] = mapped_column(Integer)
    actor:      Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    scan_id:    Mapped[Optional[str]] = mapped_column(String(36))
    detail:     Mapped[Optional[dict]] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_hist_area_ts", "area", "ts"),
        Index("ix_hist_type_ts", "event_type", "ts"),
        Index("ix_hist_path", "path"),
        Index("ix_hist_scan", "scan_id"),
        Index("ix_hist_ts", "ts"),
    )


class Exclusion(Base):
    __tablename__ = "exclusions"

    id:         Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    area:       Mapped[Optional[str]] = mapped_column(String(128))      # None = every area
    pattern:    Mapped[str] = mapped_column(String(1024), nullable=False)
    kind:       Mapped[str] = mapped_column(String(8), nullable=False, default="path")  # path | glob
    reason:     Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active:     Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("area", "pattern", name="uq_excl_area_pattern"),
        Index("ix_excl_active", "active"),
    )


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id:           Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name:         Mapped[str] = mapped_column(String(128), nullable=False)
    prefix:       Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash:   Mapped[str] = mapped_column(String(256), nullable=False)
    scopes:       Mapped[str] = mapped_column(String(64), nullable=False, default="read")
    created_by:   Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at:   Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at:   Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[Optional[str]] = mapped_column(String(64))

    @property
    def active(self) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and _aware(self.expires_at) <= utcnow():
            return False
        return True

    @property
    def scope_set(self):
        return frozenset(s.strip() for s in self.scopes.split(",") if s.strip())


class AreaState(Base):
    __tablename__ = "area_state"

    area:            Mapped[str] = mapped_column(String(128), primary_key=True)
    path:            Mapped[Optional[str]] = mapped_column(String(1024))
    paused:          Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    paused_reason:   Mapped[Optional[str]] = mapped_column(Text)
    paused_by:       Mapped[Optional[str]] = mapped_column(String(64))
    paused_at:       Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    disabled:        Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    disabled_reason: Mapped[Optional[str]] = mapped_column(Text)
    disabled_by:     Mapped[Optional[str]] = mapped_column(String(64))
    disabled_at:     Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    active_tiers:    Mapped[Optional[list]] = mapped_column(JSON)
    last_scan_at:    Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_scan_id:    Mapped[Optional[str]] = mapped_column(String(36))
    updated_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False,
                                                      default=utcnow, onupdate=utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_log"

    id:        Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts:        Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    channel:   Mapped[str] = mapped_column(String(16), nullable=False)
    severity:  Mapped[str] = mapped_column(String(16), nullable=False)
    area:      Mapped[Optional[str]] = mapped_column(String(128))
    event_key: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    title:     Mapped[str] = mapped_column(String(256), nullable=False)
    body:      Mapped[Optional[str]] = mapped_column(Text)
    outcome:   Mapped[str] = mapped_column(String(16), nullable=False)   # sent|failed|suppressed_dup|suppressed_rate|disabled
    error:     Mapped[Optional[str]] = mapped_column(Text)
    detail:    Mapped[Optional[dict]] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_notif_ts", "ts"),
        Index("ix_notif_key_ts", "event_key", "ts"),
        Index("ix_notif_channel_ts", "channel", "ts"),
    )


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
