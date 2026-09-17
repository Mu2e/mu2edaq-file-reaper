import gzip
import os
import threading

import pytest

from mu2edaq_file_reaper import actions
from mu2edaq_file_reaper.compressors import get_compressor
from mu2edaq_file_reaper.domain import FileInfo
from mu2edaq_file_reaper.scanner import enumerate_files


def info_for(area, name):
    for i in enumerate_files(area):
        if i.name == name or i.rel == name:
            return i
    raise AssertionError(f"{name} not found")


def make_ctx(area, fake_clock, **kw):
    base = dict(area_root=area.real_path, dry_run=False, compressor=get_compressor("gzip"), level=6,
                clock=fake_clock)
    base.update(kw)
    return actions.ActionContext(**base)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------
def test_delete_ok_and_dry_run(tree, area_factory, fake_clock):
    p = tree.file("a.dat", size=500, age_days=1)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    r = actions.delete_file(info, make_ctx(area, fake_clock, dry_run=True), 0)
    assert r.outcome == "dry_run" and r.bytes_freed == 500 and os.path.exists(p)
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "ok" and r.bytes_freed == 500 and not os.path.exists(p)
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "vanished"


def test_delete_refuses_changed_file(tree, area_factory, fake_clock):
    p = tree.file("a.dat", size=10, age_days=1)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    with open(p, "ab") as fh:
        fh.write(b"more")                        # size and mtime change after the scan
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "changed" and os.path.exists(p)


def test_delete_refuses_replaced_inode_and_symlink(tree, area_factory, fake_clock):
    p = tree.file("a.dat", size=10, age_days=1)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    os.unlink(p)
    tree.file("a.dat", size=10, age_days=1)      # same name, new inode
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "changed"
    os.unlink(p)
    victim = tree.file("victim.dat", size=10, age_days=1)
    os.symlink(victim, p)
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and os.path.exists(victim)


def test_delete_refuses_settling_hardlink_outside_and_protected(tree, area_factory, fake_clock):
    p = tree.file("a.dat", size=10, age_days=0)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    r = actions.delete_file(info, make_ctx(area, fake_clock), 3600)
    assert r.outcome == "skipped" and "settle" in r.error
    tree.hardlink("b.dat", "a.dat")
    old_info = info_for(area, "a.dat")
    r = actions.delete_file(old_info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and "hard-linked" in r.error
    assert actions.delete_file(old_info, make_ctx(area, fake_clock), 0, allow_hardlinks=True).outcome == "ok"
    outside = tree.file("c.dat", age_days=1)
    bad = info_for(area, "c.dat")
    bad = FileInfo(**{**bad.__dict__, "path": "/etc/passwd"})
    r = actions.delete_file(bad, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "outside_area"
    r = actions.delete_file(info_for(area, "c.dat"), make_ctx(area, fake_clock, protected=frozenset({outside})), 0)
    assert r.outcome == "skipped" and r.reason == "protected" and os.path.exists(outside)


def test_delete_refuses_when_parent_directory_swapped(tree, area_factory, fake_clock):
    tree.file("sub/a.dat", age_days=1)
    area, _ = area_factory(tree.root)
    info = info_for(area, os.path.join("sub", "a.dat"))
    os.rename(tree.path("sub"), tree.path("sub-moved"))
    os.makedirs(tree.path("sub"))
    tree.file("sub/a.dat", age_days=1)
    r = actions.delete_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "changed"
    assert os.path.exists(tree.path("sub/a.dat")) and os.path.exists(tree.path("sub-moved/a.dat"))


# ---------------------------------------------------------------------------
# compress
# ---------------------------------------------------------------------------
def test_compress_ok_preserves_metadata_and_verifies(tree, area_factory, fake_clock):
    content = b"compressible " * 20000
    p = tree.file("a.dat", content=content, age_days=3, atime_days=2)
    st0 = os.stat(p)
    os.chmod(p, 0o640)
    st0 = os.stat(p)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    r = actions.compress_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "ok" and r.dest_path == p + ".gz" and r.bytes_freed > 0
    assert not os.path.exists(p) and os.path.exists(p + ".gz")
    st1 = os.stat(p + ".gz")
    assert st1.st_mtime_ns == st0.st_mtime_ns and st1.st_atime_ns == st0.st_atime_ns
    assert (st1.st_mode & 0o777) == 0o640
    with gzip.open(p + ".gz", "rb") as fh:
        assert fh.read() == content
    assert not [n for n in os.listdir(tree.root) if n.startswith(".reaper-tmp-")]


@pytest.mark.parametrize("algo", ["bz2", "xz"])
def test_other_compressors_roundtrip_with_full_verify(tree, area_factory, fake_clock, algo):
    content = b"abc" * 50000
    p = tree.file("a.dat", content=content, age_days=3)
    area, _ = area_factory(tree.root)
    comp = get_compressor(algo)
    r = actions.compress_file(info_for(area, "a.dat"), make_ctx(area, fake_clock, compressor=comp), 0)
    assert r.outcome == "ok" and r.dest_path.endswith(comp.suffix)
    with open(r.dest_path, "rb") as raw, comp.open_read(raw) as fh:
        assert fh.read() == content


def test_compress_dry_run_touches_nothing(tree, area_factory, fake_clock):
    p = tree.file("a.dat", size=1000, age_days=3)
    area, _ = area_factory(tree.root)
    r = actions.compress_file(info_for(area, "a.dat"), make_ctx(area, fake_clock, dry_run=True, assumed_ratio=0.25), 0)
    assert r.outcome == "dry_run" and r.bytes_freed == 750 and os.path.exists(p) and os.listdir(tree.root) == ["a.dat"]


def test_compress_incompressible_and_dest_exists(tree, area_factory, fake_clock):
    p = tree.file("r.dat", content=os.urandom(200000), age_days=3)
    area, _ = area_factory(tree.root)
    r = actions.compress_file(info_for(area, "r.dat"), make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "incompressible" and os.path.exists(p)
    assert os.listdir(tree.root) == ["r.dat"]
    tree.file("c.dat", content=b"z" * 10000, age_days=3)
    tree.file("c.dat.gz", content=b"existing", age_days=3)
    r = actions.compress_file(info_for(area, "c.dat"), make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "dest_exists"
    assert open(tree.path("c.dat.gz"), "rb").read() == b"existing"


def test_compress_aborts_on_interrupt_leaving_no_temp(tree, area_factory, fake_clock):
    p = tree.file("big.dat", content=b"y" * (3 * 1024 * 1024), age_days=3)
    area, _ = area_factory(tree.root)
    ev = threading.Event()
    ev.set()
    r = actions.compress_file(info_for(area, "big.dat"), make_ctx(area, fake_clock, interrupt=ev), 0)
    assert r.outcome == "skipped" and r.reason == "interrupted"
    assert os.listdir(tree.root) == ["big.dat"] and os.path.exists(p)


def test_compress_detects_source_change_during_copy(tree, area_factory, fake_clock, monkeypatch):
    p = tree.file("a.dat", content=b"q" * 500000, age_days=3)
    area, _ = area_factory(tree.root)
    info = info_for(area, "a.dat")
    real_fstat = os.fstat
    calls = {"n": 0}

    def fstat_then_mutate(fd):
        st = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] == 4:                      # the re-stat after the copy
            with open(p, "ab") as fh:
                fh.write(b"!")
            return real_fstat(fd)
        return st
    monkeypatch.setattr(os, "fstat", fstat_then_mutate)
    r = actions.compress_file(info, make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason in ("changed",)
    assert os.path.exists(p) and not os.path.exists(p + ".gz")
    assert not [n for n in os.listdir(tree.root) if n.startswith(".reaper-tmp-")]


def test_verify_failure_is_reported(tree, area_factory, fake_clock, monkeypatch):
    p = tree.file("a.dat", content=b"v" * 100000, age_days=3)
    area, _ = area_factory(tree.root)
    monkeypatch.setattr(actions, "_gzip_trailer", lambda fd, size: (0, 12345))
    r = actions.compress_file(info_for(area, "a.dat"), make_ctx(area, fake_clock), 0)
    assert r.outcome == "skipped" and r.reason == "verify" and os.path.exists(p)
    assert os.listdir(tree.root) == ["a.dat"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def test_contained_and_prune_empty_dirs(tree, area_factory):
    root = os.path.realpath(tree.root)
    assert actions.contained(os.path.join(root, "x"), root)
    assert not actions.contained(root, root)
    assert not actions.contained(os.path.dirname(root), root)
    tree.dir("a/b/c")
    tree.dir("keep")
    tree.file("keep/f.dat")
    prot_dir = tree.dir("p/q")
    removed = actions.prune_empty_dirs([tree.path("a/b/c"), tree.path("keep"), prot_dir], root,
                                       protected=frozenset({os.path.join(prot_dir, "x.db")}))
    assert removed == 3                                     # c, b, a
    assert not os.path.exists(tree.path("a")) and os.path.exists(tree.path("keep")) and os.path.exists(prot_dir)
    assert os.path.exists(root)
