#!/usr/bin/env python3
"""make_demo_tree.py - build a demo tree for mu2edaq-file-reaper and a fake-usage file.

Creates *N* files under DIR with modification and access times spread over
the last *--days* days (so LRU and Age policies have something to order), and
writes a JSON file for the ``reaper.fake_usage_file`` test hook mapping the
tree's realpath to ``[total, used, free]`` at ``--used-pct`` percent used.

    python tools/make_demo_tree.py ./data/demo --files 300 --days 30 --used-pct 82
    ./start-mu2edaq-file-reaper.sh -c config/mu2edaq-file-reaper-test.yaml

Edit the ``used`` value in the JSON file while the reaper runs to walk it
across the 80 / 90 / 95 % tiers.  Nothing here touches anything outside DIR
and the JSON file.  Never point a production reaper at a fake-usage file.
"""

import argparse
import json
import os
import random
import sys
import time

SUBDIRS = ("", "run_001", "run_002", "logs", "logs/old", "calib")


def build(root, files, size, days, seed, compressible):
    random.seed(seed)
    os.makedirs(root, exist_ok=True)
    for sub in SUBDIRS:
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    now = time.time()
    written = 0
    for i in range(files):
        sub = SUBDIRS[i % len(SUBDIRS)]
        ext = (".dat", ".log", ".raw", ".txt")[i % 4]
        path = os.path.join(root, sub, "demo_%04d%s" % (i, ext))
        n = max(1, int(size * random.uniform(0.5, 1.5)))
        if compressible:
            data = (b"mu2e demo payload %06d " % i) * (n // 24 + 1)
            data = data[:n]
        else:
            data = os.urandom(n)
        with open(path, "wb") as fh:
            fh.write(data)
        mtime = now - random.uniform(0.0, days) * 86400.0
        atime = mtime + random.uniform(0.0, max(0.0, now - mtime))
        os.utime(path, (atime, mtime))
        written += len(data)
    return written


def write_usage(usage_file, root, total, used_pct):
    used = int(total * used_pct / 100.0)
    data = {}
    if os.path.isfile(usage_file):
        try:
            with open(usage_file, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError):
            data = {}
    data[os.path.realpath(root)] = [int(total), used, int(total) - used]
    os.makedirs(os.path.dirname(os.path.abspath(usage_file)) or ".", exist_ok=True)
    with open(usage_file, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return used


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("dir", help="directory to fill (created if missing)")
    p.add_argument("--files", type=int, default=200, help="number of files (default 200)")
    p.add_argument("--size", type=int, default=64 * 1024, help="mean file size in bytes (default 65536)")
    p.add_argument("--days", type=float, default=30.0, help="spread of ages in days (default 30)")
    p.add_argument("--used-pct", type=float, default=82.0, help="fake used percent (default 82)")
    p.add_argument("--total", type=int, default=100 * 1024 * 1024,
                   help="fake capacity in bytes (default 100 MiB)")
    p.add_argument("--usage-file", default="./data/demo-usage.json",
                   help="fake_usage_file to write (default ./data/demo-usage.json)")
    p.add_argument("--random-content", action="store_true",
                   help="incompressible random bytes instead of compressible text")
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args(argv)
    written = build(a.dir, a.files, a.size, a.days, a.seed, not a.random_content)
    used = write_usage(a.usage_file, a.dir, a.total, a.used_pct)
    print("wrote %d files (%.1f MiB) under %s" % (a.files, written / 1048576.0, os.path.realpath(a.dir)))
    print("fake usage for that path: total=%d used=%d (%.1f%%) in %s"
          % (a.total, used, a.used_pct, a.usage_file))
    return 0


if __name__ == "__main__":
    sys.exit(main())
