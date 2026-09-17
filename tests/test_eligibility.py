import os

import pytest

from mu2edaq_file_reaper.domain import FileInfo
from mu2edaq_file_reaper.eligibility import (
    REJECT_REASONS, EligibilityContext, classify, is_compressed_name, is_temp_name, matches_any, partition,
)
from mu2edaq_file_reaper.exclusions import ExclusionRules
from mu2edaq_file_reaper.fts_gate import FtsSnapshot
from mu2edaq_file_reaper.policies import POLICIES

NOW = 1_000_000.0
DAY = 86400


def fi(name="a.dat", age_days=10.0, atime_days=None, size=100, nlink=1, root="/area", sub=""):
    mtime = NOW - age_days * DAY
    atime = NOW - (atime_days if atime_days is not None else age_days) * DAY
    path = os.path.join(root, sub, name) if sub else os.path.join(root, name)
    return FileInfo(path=path, rel=os.path.relpath(path, root), name=name, size=size,
                    atime_ns=int(atime * 1e9), mtime_ns=int(mtime * 1e9), ctime_ns=0, uid=0, gid=0,
                    mode=0o100644, nlink=nlink, dev=1, ino=5, dir_dev=1, dir_ino=2)


def ctx(policy="LRU-Delete", **kw):
    base = dict(now=NOW, policy=POLICIES[policy], min_age=DAY, settle_seconds=300,
                area_root="/area", compressed_suffixes=(".gz", ".bz2"))
    base.update(kw)
    return EligibilityContext(**base)


def test_plain_old_file_is_eligible():
    assert classify(fi(), ctx()).eligible


@pytest.mark.parametrize("reason,info,c", [
    ("temp_name", fi("x.tmp"), ctx()),
    ("temp_name", fi(".reaper-tmp-123.gz"), ctx()),
    ("excluded_glob", fi("keep.raw"), ctx(exclude=("*.raw",))),
    ("excluded_glob", fi("a.dat", sub="hold"), ctx(exclude=("hold/*",))),
    ("not_included", fi("a.dat"), ctx(include=("*.log",))),
    ("protected", fi("fts.db"), ctx(protected=frozenset({"/area/fts.db"}))),
    ("manual_exclusion", fi("a.dat"), ctx(manual_exclusions=ExclusionRules(exact=frozenset({"/area/a.dat"})))),
    ("hardlink", fi(nlink=2), ctx()),
    ("already_compressed", fi("a.dat.gz"), ctx("LRU-Compress")),
    ("settling", fi(age_days=0.001), ctx()),
    ("too_young", fi(age_days=0.5), ctx()),
    ("too_young", fi(age_days=10, atime_days=0.5), ctx("LRU-Delete")),      # LRU key is atime
    ("fts_not_complete", fi(), ctx(fts=FtsSnapshot(frozenset(), 0, 0))),
    ("open_file", fi(), ctx(open_inodes=frozenset({(1, 5)}))),
    ("outside_area", fi(root="/elsewhere"), ctx()),
])
def test_each_rejection_reason(reason, info, c):
    v = classify(info, c)
    assert not v.eligible and v.reason == reason
    assert reason in REJECT_REASONS


def test_age_policy_ignores_fresh_atime_and_hardlinks_can_be_allowed():
    assert classify(fi(age_days=10, atime_days=0.5), ctx("Age-Delete")).eligible
    assert classify(fi(nlink=3), ctx(allow_hardlinks=True)).eligible


def test_compressed_copy_exists_only_blocks_compression():
    sib = {"/area": frozenset({"a.dat", "a.dat.gz"})}
    v = classify(fi("a.dat"), ctx("LRU-Compress", siblings=sib))
    assert v.reason == "compressed_copy_exists"
    assert classify(fi("a.dat"), ctx("LRU-Delete", siblings=sib)).eligible
    assert classify(fi("a.dat"), ctx("Age-Delete", siblings=sib)).eligible


def test_fts_gate_allows_transferred_and_reaper_compressed():
    snap = FtsSnapshot(frozenset({"/area/a.dat"}), 0, 1)
    assert classify(fi("a.dat"), ctx(fts=snap)).eligible
    assert classify(fi("a.dat.gz"), ctx("Age-Delete", fts=snap)).eligible
    assert classify(fi("b.dat"), ctx(fts=snap)).reason == "fts_not_complete"


def test_helpers_and_partition():
    assert is_temp_name("foo.part") and is_temp_name("~x") is False and is_temp_name("x~")
    assert is_compressed_name("A.TGZ", (".tgz",)) and not is_compressed_name("a.dat", (".gz",))
    assert matches_any("sub/x.log", "x.log", ("*.log",)) and matches_any("sub/x.log", "x.log", ("sub/*",))
    files = [fi("a.dat"), fi("b.tmp"), fi("c.dat", nlink=2), fi("d.dat", age_days=0.1)]
    el, rej = partition(files, ctx())
    assert [f.name for f in el] == ["a.dat"]
    assert rej == {"temp_name": 1, "hardlink": 1, "too_young": 1}
