import pytest

from mu2edaq_file_reaper.fts_gate import EMPTY_SNAPSHOT, FtsSnapshot, FtsUnavailable, load_fts_snapshot


def test_snapshot_allows_with_suffix_strip():
    s = FtsSnapshot(paths=frozenset({"/d/a.dat", "/d/b.dat.gz"}), loaded_at=0, row_count=2)
    assert s.allows("/d/a.dat")
    assert s.allows("/d/a.dat.gz", (".gz",))          # reaper compressed a transferred file
    assert s.allows("/d/b.dat.gz")
    assert not s.allows("/d/c.dat", (".gz",))
    assert not EMPTY_SNAPSHOT.allows("/d/a.dat") and EMPTY_SNAPSHOT.available is False


def test_load_only_completed_or_deleted(fts_db):
    add = fts_db["add"]
    add("/d/done.dat", "COMPLETED")
    add("/d/gone.dat", "DELETED", compressed="/d/gone.dat.bz2")
    for st in ("PENDING", "PROCESSING", "COMPRESSING", "CHECKSUMMING", "TRANSFERRING", "ERROR", "SKIPPED"):
        add(f"/d/{st.lower()}.dat", st)
    snap = load_fts_snapshot(fts_db["path"])
    assert snap.row_count == 2 and snap.available
    assert snap.allows("/d/done.dat") and snap.allows("/d/gone.dat") and snap.allows("/d/gone.dat.bz2")
    assert not snap.allows("/d/pending.dat") and not snap.allows("/d/error.dat")


def test_missing_or_broken_db_raises(tmp_path):
    with pytest.raises(FtsUnavailable):
        load_fts_snapshot(str(tmp_path / "nope.db"))
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database at all, definitely not sqlite")
    with pytest.raises(FtsUnavailable):
        load_fts_snapshot(str(bad))
