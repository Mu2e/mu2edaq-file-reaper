"""Logging: console in the foreground, a size-rotated file when configured."""

import logging
import logging.handlers
import os
import sys
from typing import Optional

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(log_file: Optional[str], verbose: bool, max_bytes: int,
                      backup_count: int, daemon: bool) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(FORMAT)
    if not daemon:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(fmt)
        root.addHandler(console)
    if log_file:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=max(0, int(max_bytes)),
                                                      backupCount=max(0, int(backup_count)))
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            print(f"[Log] Warning: cannot open log file {log_file}: {exc}", file=sys.stderr)
    logging.getLogger("werkzeug").setLevel(logging.WARNING if not verbose else logging.INFO)
