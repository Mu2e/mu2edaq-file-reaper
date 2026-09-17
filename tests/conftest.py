"""Shared fixtures.

Every test gets pristine ``Settings`` and an empty ``StateStore`` (both are
process-wide singletons), a controllable clock, a fake disk whose free space
grows as bytes are freed, and a tree builder that sets atime/mtime precisely.
"""

import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mu2edaq_file_reaper.config import build_areas          # noqa: E402
from mu2edaq_file_reaper.db import init_db                  # noqa: E402
from mu2edaq_file_reaper.db.repo import (                   # noqa: E402
    AreaStateRepo, ExclusionRepo, HistoryRepo, NotificationLogRepo, TokenRepo,
)
from mu2edaq_file_reaper.domain import UsageSample          # noqa: E402
from mu2edaq_file_reaper.exclusions import ExclusionRegistry  # noqa: E402
from mu2edaq_file_reaper.reaper import Deps                 # noqa: E402
from mu2edaq_file_reaper.settings import reset_settings     # noqa: E402
from mu2edaq_file_reaper.state import STORE, StateStore     # noqa: E402
from mu2edaq_file_reaper.usage import measure_usage         # noqa: E402
from mu2edaq_file_reaper.watermarks import used_percent     # noqa: E402

DAY = 86400


@pytest.fixture(autouse=True)
def clean_state():
    reset_settings()
    STORE.__init__()
    yield
    reset_settings()
    STORE.__init__()


@pytest.fixture
def settings():
    return reset_settings()


class FakeClock:
    def __init__(self, now=None):
        self.now = float(now if now is not None else time.time())

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def fake_clock():
    return FakeClock()


class FakeDisk:
    """(total, used, free) that responds to bytes being freed."""

    def __init__(self, total=100 * 1024 * 1024, used_pct=50.0):
        self.total = total
        self.used = int(total * used_pct / 100.0)
        self.calls = 0

    def __call__(self, path):
        self.calls += 1
        return self.total, self.used, self.total - self.used

    def set_used_pct(self, pct):
        self.used = int(self.total * pct / 100.0)

    @property
    def used_pct(self):
        return used_percent(self.total, self.used, self.total - self.used)

    def freed(self, n):
        self.used = max(0, self.used - n)

    def sample(self, ts=0.0):
        return UsageSample(self.total, self.used, self.total - self.used, self.used_pct, ts)


@pytest.fixture
def fake_disk():
    return FakeDisk()


class Tree:
    """Builder for a temp tree with controlled timestamps."""

    def __init__(self, root, clock):
        self.root = root
        self.clock = clock
        os.makedirs(root, exist_ok=True)

    def path(self, rel):
        return os.path.join(self.root, rel)

    def file(self, rel, size=1024, age_days=None, atime_days=None, mtime_days=None,
             content=None):
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = content if content is not None else (b"x" * size)
        with open(p, "wb") as fh:
            fh.write(data)
        now = self.clock()
        m = now - (mtime_days if mtime_days is not None else (age_days or 0)) * DAY
        a = now - (atime_days if atime_days is not None else (age_days or 0)) * DAY
        os.utime(p, (a, m))
        return p

    def dir(self, rel):
        p = self.path(rel)
        os.makedirs(p, exist_ok=True)
        return p

    def symlink(self, rel, target):
        p = self.path(rel)
        os.symlink(target, p)
        return p

    def hardlink(self, rel, source_rel):
        p = self.path(rel)
        os.link(self.path(source_rel), p)
        return p


@pytest.fixture
def tree(tmp_path, fake_clock):
    return Tree(str(tmp_path / "area"), fake_clock)


@pytest.fixture
def mem_db():
    db = init_db("sqlite://")
    yield db
    db.dispose()


@pytest.fixture
def repos(mem_db):
    return {"db": mem_db, "history": HistoryRepo(mem_db), "exclusions": ExclusionRepo(mem_db),
            "area_state": AreaStateRepo(mem_db), "tokens": TokenRepo(mem_db),
            "notif_log": NotificationLogRepo(mem_db)}


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def emit(self, severity, title, message, key=None, area=None, meta=None):
        self.events.append({"severity": severity, "title": title, "message": message,
                            "key": key, "area": area, "meta": meta or {}})

    def titles(self):
        return [e["title"] for e in self.events]

    def keys(self):
        return [e["key"] for e in self.events]

    def describe(self):
        """Match Notifier.describe(): one dict per configured channel.

        This double configures no channels, so the web layer's notifications
        view renders its empty state.
        """
        return []

    def channel(self, name):
        """Match Notifier.channel(): None when no channel has that name."""
        return None


@pytest.fixture
def notifier():
    return RecordingNotifier()


def make_area(root, **overrides):
    """Build one AreaConfig for *root* with fast defaults (settle 0)."""
    spec = {"path": root, "settle_seconds": 0}
    spec.update(overrides)
    areas, issues = build_areas({"areas": [spec]}, allow_shallow_root=True)
    assert areas, issues
    return areas[0], issues


@pytest.fixture
def area_factory():
    return make_area


@pytest.fixture
def deps_factory(repos, notifier, fake_clock, fake_disk):
    """Deps wired to the in-memory DB, a fake disk and a recording notifier.

    The real delete/compress actions are used, wrapped so the fake disk learns
    about freed bytes.
    """
    from mu2edaq_file_reaper import actions

    def _make(disk=None, **overrides):
        disk = disk or fake_disk
        registry = ExclusionRegistry(repos["exclusions"])
        registry.reload()

        def delete(info, ctx, settle, hl=False):
            r = actions.delete_file(info, ctx, settle, hl)
            if r.outcome == "ok":
                disk.freed(r.bytes_freed)
            return r

        def compress(info, ctx, settle, hl=False):
            r = actions.compress_file(info, ctx, settle, hl)
            if r.outcome == "ok":
                disk.freed(r.bytes_freed)
            return r

        kwargs = dict(store=STORE, history=repos["history"], area_state=repos["area_state"],
                      exclusions=registry, notifier=notifier,
                      measure_usage=lambda p: measure_usage(p, disk, fake_clock),
                      delete=delete, compress=compress, clock=fake_clock,
                      atime_mode=lambda p: "relatime")
        kwargs.update(overrides)
        return Deps(**kwargs)
    return _make


@pytest.fixture
def fts_db(tmp_path):
    """A mu2edaq-fts style SQLite database with rows in several statuses."""
    path = str(tmp_path / "fts_status.db")
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE files (id INTEGER PRIMARY KEY, filename TEXT, filepath TEXT UNIQUE,
                    compressed_path TEXT, status TEXT, transfer_end TEXT, ready_for_deletion_at TEXT)""")
    conn.commit()
    conn.close()

    def add(filepath, status, compressed=None):
        c = sqlite3.connect(path)
        c.execute("INSERT INTO files (filename, filepath, compressed_path, status) VALUES (?,?,?,?)",
                  (os.path.basename(filepath), filepath, compressed, status))
        c.commit()
        c.close()
    return {"path": path, "add": add}
