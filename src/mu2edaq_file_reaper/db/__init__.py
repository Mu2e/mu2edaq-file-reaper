"""Database access: engine creation, SQLite pragmas and a scoped session.

SQLite is the default (``sqlite:///./data/file-reaper.db``); any SQLAlchemy URL
works, PostgreSQL via ``postgresql+psycopg://``.  Rules that keep the many
threads (scheduler, workers, notifier, Flask) from stepping on each other:

* every repository call is one short transaction via :meth:`Database.session_scope`;
* nobody holds a session across a filesystem action;
* worker threads call :meth:`Database.remove` at scan end, Flask on teardown.
"""

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from .models import SCHEMA_VERSION, Base, Meta


class Database:
    def __init__(self, url: str, echo: bool = False) -> None:
        self.url = url
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if url in ("sqlite://", "sqlite:///:memory:") or ":memory:" in url:
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, echo=echo, future=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA synchronous=NORMAL")
                    cur.execute("PRAGMA busy_timeout=5000")
                    cur.execute("PRAGMA foreign_keys=ON")
                finally:
                    cur.close()
        self.Session = scoped_session(
            sessionmaker(bind=self.engine, future=True, expire_on_commit=False))

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        with self.session_scope() as s:
            row = s.get(Meta, "schema_version")
            if row is None:
                s.add(Meta(key="schema_version", value=str(SCHEMA_VERSION)))

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            self.Session.remove()

    def remove(self) -> None:
        """Release the current thread's session (call at scan end / request end)."""
        self.Session.remove()

    def dispose(self) -> None:
        self.Session.remove()
        self.engine.dispose()

    def ping(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


def init_db(url: str, echo: bool = False) -> Database:
    db = Database(url, echo=echo)
    db.create_all()
    return db
