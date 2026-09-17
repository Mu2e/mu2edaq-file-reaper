import os

from mu2edaq_file_reaper.domain import FileInfo
from mu2edaq_file_reaper.policies import POLICIES, build_queue, diff_new_paths, ordering_key


def fi(path, atime, mtime, size=10):
    return FileInfo(path=path, rel=os.path.basename(path), name=os.path.basename(path), size=size,
                    atime_ns=int(atime * 1e9), mtime_ns=int(mtime * 1e9), ctime_ns=0, uid=0, gid=0,
                    mode=0o100644, nlink=1, dev=1, ino=hash(path) & 0xFFFF, dir_dev=1, dir_ino=1)


def test_ordering_keys():
    f = fi("/a/x", atime=200, mtime=100)
    assert ordering_key(f, POLICIES["LRU-Delete"]) == 200
    assert ordering_key(f, POLICIES["Age-Delete"]) == 100
    g = fi("/a/y", atime=50, mtime=100)
    assert ordering_key(g, POLICIES["LRU-Compress"]) == 100


def test_build_queue_orders_oldest_first_with_path_tiebreak():
    files = [fi("/a/b", 300, 300), fi("/a/a", 300, 300), fi("/a/c", 100, 100)]
    q = build_queue(files, POLICIES["Age-Delete"], "full")
    assert [e.info.path for e in q] == ["/a/c", "/a/a", "/a/b"]
    assert all(e.queue == "delete" and e.tier == "full" and e.policy == "Age-Delete" for e in q)
    q2 = build_queue(files, POLICIES["LRU-Compress"], "warning", lambda u, g: ("me", "us"))
    assert q2[0].owner == "me" and q2[0].queue == "compress"


def test_owner_lookup_failure_falls_back_to_ids():
    def boom(u, g):
        raise KeyError
    q = build_queue([fi("/a/a", 1, 1)], POLICIES["LRU-Delete"], "critical", boom)
    assert q[0].owner == "0"


def test_diff_new_paths():
    assert diff_new_paths(["a", "b", "c"], frozenset({"b"})) == ["a", "c"]
