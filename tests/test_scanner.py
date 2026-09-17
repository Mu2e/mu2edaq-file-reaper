import os
import stat
import sys

import pytest

from mu2edaq_file_reaper.scanner import ScanStats, enumerate_files, sweep_temp_files


def scan(area):
    st = ScanStats()
    infos = list(enumerate_files(area, stats=st))
    return infos, st


def test_enumerates_regular_files_only_and_records_parent_identity(tree, area_factory):
    a = tree.file("a.dat", age_days=1)
    b = tree.file("sub/deep/b.dat", age_days=2)
    tree.dir("empty")
    tree.symlink("link.dat", a)
    tree.symlink("linkdir", tree.path("sub"))
    if hasattr(os, "mkfifo"):
        os.mkfifo(tree.path("pipe"))
    area, _ = area_factory(tree.root)
    infos, st = scan(area)
    names = sorted(i.rel for i in infos)
    assert names == ["a.dat", os.path.join("sub", "deep", "b.dat")]
    assert st.symlinks == 2 and st.files == 2 and st.dirs >= 4
    deep = next(i for i in infos if i.name == "b.dat")
    dst = os.stat(os.path.dirname(deep.path))
    assert (deep.dir_dev, deep.dir_ino) == (dst.st_dev, dst.st_ino)
    assert deep.rel in st.siblings[os.path.dirname(deep.path)] or "b.dat" in st.siblings[os.path.dirname(deep.path)]


def test_other_filesystem_is_skipped(tree, area_factory):
    tree.file("a.dat")
    area, _ = area_factory(tree.root)
    st = ScanStats()
    infos = list(enumerate_files(area, root_dev=os.stat(tree.root).st_dev + 12345, stats=st))
    assert infos == [] and st.other_fs >= 1


@pytest.mark.skipif(os.geteuid() == 0 if hasattr(os, "geteuid") else True, reason="root ignores permissions")
def test_permission_errors_are_counted_not_raised(tree, area_factory):
    tree.file("ok.dat")
    locked = tree.dir("locked")
    tree.file("locked/secret.dat")
    os.chmod(locked, 0)
    try:
        area, _ = area_factory(tree.root)
        infos, st = scan(area)
        assert [i.name for i in infos] == ["ok.dat"]
        assert st.errors >= 1 and st.error_samples
    finally:
        os.chmod(locked, stat.S_IRWXU)


def test_protected_paths_are_not_yielded(tree, area_factory):
    p = tree.file("fts.db")
    tree.file("x.dat")
    area, _ = area_factory(tree.root)
    infos = list(enumerate_files(area, protected=frozenset({p})))
    assert [i.name for i in infos] == ["x.dat"]


def test_sweep_temp_files(tree, area_factory, fake_clock):
    old = tree.file(".reaper-tmp-abc.gz", age_days=1)
    fresh = tree.file("sub/.reaper-tmp-def.gz", age_days=0)
    keep = tree.file("data.dat", age_days=1)
    area, _ = area_factory(tree.root)
    assert sweep_temp_files(area, older_than_s=300, clock=fake_clock) == 1
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(keep)
