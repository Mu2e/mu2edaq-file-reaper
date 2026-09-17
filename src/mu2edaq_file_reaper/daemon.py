"""POSIX daemonisation and PID-file handling."""

import atexit
import os
import sys


def daemonize(log_file: str = None) -> None:
    """Detach from the controlling terminal using the POSIX double-fork idiom.

    After this call returns (in the grandchild process only):
      - stdin  is redirected to /dev/null
      - stdout and stderr are redirected to *log_file* (or /dev/null)
      - the process has no controlling terminal and belongs to its own session

    The two intermediate parent processes exit via os._exit() so that no
    atexit handlers or Python finalizers run in them.

    Raises RuntimeError on platforms without os.fork() (e.g. Windows).
    """
    if not hasattr(os, "fork"):
        raise RuntimeError(
            "Daemon mode requires os.fork(), which is not available on this platform."
        )

    # Flush Python-level buffers before forking so they aren't duplicated.
    sys.stdout.flush()
    sys.stderr.flush()

    # ---- first fork ----
    pid = os.fork()
    if pid > 0:
        os._exit(0)          # parent exits; child becomes a new process group leader

    os.setsid()              # create a new session; child is the session leader
    os.umask(0o022)

    # ---- second fork ----
    pid = os.fork()
    if pid > 0:
        os._exit(0)          # session leader exits; grandchild can never acquire a tty

    # ---- grandchild: we are the daemon ----

    # Redirect stdin to /dev/null
    with open(os.devnull, "r") as dn:
        os.dup2(dn.fileno(), sys.stdin.fileno())
    sys.stdin = open(os.devnull, "r")

    # Redirect stdout and stderr to the log file (or /dev/null)
    log_dest = log_file if log_file else os.devnull
    log_fh   = open(log_dest, "a", buffering=1)   # append; line-buffered text
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())
    sys.stdout = log_fh
    sys.stderr = log_fh


def write_pid_file(path: str) -> None:
    """Write the current PID to *path* and register its removal at exit."""
    try:
        with open(path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        atexit.register(remove_pid_file, path)
    except OSError as exc:
        print(f"[Daemon] Warning: could not write PID file {path}: {exc}",
              file=sys.stderr)


def remove_pid_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
