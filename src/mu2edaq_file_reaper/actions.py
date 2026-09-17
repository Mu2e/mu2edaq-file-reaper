"""Filesystem actions: verified delete and atomic verified compress.

The scan-time :class:`FileInfo` is a *plan*.  Nothing here trusts it: every
action re-opens the parent directory and the file by ``dir_fd`` + name, proves
they are the objects that were scanned (device, inode, size, mtime, link count)
and only then acts.  Any mismatch is a *skip*, never an error — the file will
simply be looked at again on the next scan.

Compression writes to a ``.reaper-tmp-*`` file in the same directory, verifies
the result, copies mode/owner/atime/mtime onto it, renames it into place and
only then unlinks the original.  A crash at any point leaves either the
original (plus a temp file that the next scan sweeps) or both files — never a
half-written archive under the final name.
"""

import errno
import os
import stat
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Callable, FrozenSet, Iterable, Optional

from .compressors import Compressor, clamp_level
from .domain import ActionResult, FileInfo
from .scanner import TEMP_PREFIX

CHUNK = 1024 * 1024
O_NOATIME = getattr(os, "O_NOATIME", 0)
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_HAS_DIR_FD = os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd


class SafetyRefusal(Exception):
    """An intentional "no" from the safety checks; carries a reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class Interrupted(Exception):
    pass


@dataclass(frozen=True)
class ActionContext:
    area_root:   str
    dry_run:     bool
    compressor:  Optional[Compressor]
    level:       int
    verify:      str = "crc"          # crc | size | none
    min_ratio:   float = 0.95
    assumed_ratio: float = 0.5        # dry-run estimate of compressed/original
    protected:   FrozenSet[str] = frozenset()
    clock:       Callable[[], float] = time.time
    interrupt:   Optional[threading.Event] = None
    shutdown:    Optional[threading.Event] = None

    def interrupted(self) -> bool:
        return bool((self.interrupt is not None and self.interrupt.is_set())
                    or (self.shutdown is not None and self.shutdown.is_set()))


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def contained(real_path: str, area_root: str) -> bool:
    """*real_path* lies strictly inside *area_root*."""
    root = os.path.realpath(area_root)
    try:
        common = os.path.commonpath([real_path, root])
    except ValueError:
        return False
    return common == root and real_path != root


def compressed_name(name: str, comp: Compressor) -> str:
    return name + comp.suffix


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _check_containment(info: FileInfo, ctx: ActionContext) -> None:
    real = os.path.realpath(info.path)
    if not contained(real, ctx.area_root):
        raise SafetyRefusal("outside_area", f"{info.path} resolves outside {ctx.area_root}")
    if real in ctx.protected or info.path in ctx.protected:
        raise SafetyRefusal("protected", f"{info.path} is protected")


def open_parent_verified(info: FileInfo) -> int:
    parent = os.path.dirname(info.path)
    flags = os.O_RDONLY | O_DIRECTORY | O_NOFOLLOW
    try:
        dirfd = os.open(parent, flags)
    except OSError as exc:
        raise SafetyRefusal("changed", f"parent directory unavailable: {exc}") from exc
    try:
        dst = os.fstat(dirfd)
    except OSError as exc:
        os.close(dirfd)
        raise SafetyRefusal("changed", f"cannot stat parent: {exc}") from exc
    if (dst.st_dev, dst.st_ino) != (info.dir_dev, info.dir_ino):
        os.close(dirfd)
        raise SafetyRefusal("changed", "parent directory is not the one scanned")
    return dirfd


def open_file_verified(dirfd: int, info: FileInfo, settle_seconds: int, now: float,
                       allow_hardlinks: bool = False) -> int:
    flags = os.O_RDONLY | O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        try:
            fd = os.open(info.name, flags | O_NOATIME, dir_fd=dirfd)
        except PermissionError:
            fd = os.open(info.name, flags, dir_fd=dirfd)
    except FileNotFoundError:
        raise SafetyRefusal("vanished", "file no longer exists")
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SafetyRefusal("changed", "path became a symlink") from exc
        raise SafetyRefusal("changed", f"cannot open: {exc}") from exc
    try:
        st = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise SafetyRefusal("changed", f"cannot stat: {exc}") from exc
    problems = []
    if not stat.S_ISREG(st.st_mode):
        problems.append("not a regular file")
    if (st.st_dev, st.st_ino) != (info.dev, info.ino):
        problems.append("inode changed")
    if st.st_size != info.size:
        problems.append("size changed")
    if st.st_mtime_ns != info.mtime_ns:
        problems.append("mtime changed")
    if st.st_nlink > 1 and not allow_hardlinks:
        problems.append("hard-linked")
    if now - st.st_mtime < settle_seconds:
        problems.append("modified within settle window")
    if problems:
        os.close(fd)
        raise SafetyRefusal("changed", "; ".join(problems))
    return fd


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------
def delete_file(info: FileInfo, ctx: ActionContext, settle_seconds: int,
                allow_hardlinks: bool = False) -> ActionResult:
    started = ctx.clock()
    try:
        _check_containment(info, ctx)
        if ctx.dry_run:
            return ActionResult("dry_run", bytes_freed=info.size,
                                duration_s=ctx.clock() - started)
        dirfd = open_parent_verified(info)
        try:
            fd = open_file_verified(dirfd, info, settle_seconds, ctx.clock(), allow_hardlinks)
            os.close(fd)
            os.unlink(info.name, dir_fd=dirfd)
        finally:
            os.close(dirfd)
        return ActionResult("ok", bytes_freed=info.size, duration_s=ctx.clock() - started)
    except SafetyRefusal as exc:
        return ActionResult("skipped", reason=exc.reason, error=str(exc),
                            duration_s=ctx.clock() - started)
    except OSError as exc:
        return ActionResult("failed", reason="os_error", error=str(exc),
                            duration_s=ctx.clock() - started)


# ---------------------------------------------------------------------------
# Compress
# ---------------------------------------------------------------------------
def _gzip_trailer(fd: int, size: int):
    """(crc32, isize) from a gzip member's last 8 bytes."""
    if size < 8:
        return None
    os.lseek(fd, size - 8, os.SEEK_SET)
    tail = os.read(fd, 8)
    if len(tail) != 8:
        return None
    return (int.from_bytes(tail[:4], "little"), int.from_bytes(tail[4:], "little"))


def _verify_temp(tmp_fd: int, comp: Compressor, out_size: int, in_size: int,
                 crc: int, mode: str, ctx: ActionContext) -> None:
    if mode == "none":
        return
    if comp.name == "gzip":
        trailer = _gzip_trailer(tmp_fd, out_size)
        if trailer is None:
            raise SafetyRefusal("verify", "gzip trailer unreadable")
        t_crc, t_isize = trailer
        if t_isize != (in_size & 0xFFFFFFFF):
            raise SafetyRefusal("verify", "gzip ISIZE mismatch")
        if mode == "crc" and t_crc != crc:
            raise SafetyRefusal("verify", "gzip CRC mismatch")
        return
    # Other algorithms: full decompression pass.
    os.lseek(tmp_fd, 0, os.SEEK_SET)
    raw = os.fdopen(os.dup(tmp_fd), "rb", closefd=True)
    total = 0
    crc2 = 0
    try:
        with comp.open_read(raw) as reader:
            while True:
                if ctx.interrupted():
                    raise Interrupted()
                block = reader.read(CHUNK)
                if not block:
                    break
                total += len(block)
                if mode == "crc":
                    crc2 = zlib.crc32(block, crc2)
    finally:
        raw.close()
    if total != in_size:
        raise SafetyRefusal("verify", "decompressed size mismatch")
    if mode == "crc" and crc2 != crc:
        raise SafetyRefusal("verify", "decompressed CRC mismatch")


def compress_file(info: FileInfo, ctx: ActionContext, settle_seconds: int,
                  allow_hardlinks: bool = False) -> ActionResult:
    started = ctx.clock()
    comp = ctx.compressor
    if comp is None:
        return ActionResult("failed", reason="no_compressor", error="no compressor configured")
    dest_name = compressed_name(info.name, comp)
    dest_path = os.path.join(os.path.dirname(info.path), dest_name)
    try:
        _check_containment(info, ctx)
        if ctx.dry_run:
            est = int(info.size * (1.0 - ctx.assumed_ratio))
            return ActionResult("dry_run", bytes_freed=max(0, est), dest_path=dest_path,
                                duration_s=ctx.clock() - started)
        dirfd = open_parent_verified(info)
        tmp_path = None
        src_fd = None
        try:
            if os.path.lexists(dest_path):
                raise SafetyRefusal("dest_exists", f"{dest_name} already exists")
            src_fd = open_file_verified(dirfd, info, settle_seconds, ctx.clock(), allow_hardlinks)
            src_st = os.fstat(src_fd)
            tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(info.path),
                                                prefix=TEMP_PREFIX,
                                                suffix=f".{os.getpid()}{comp.suffix}")
            crc = 0
            copied = 0
            raw_out = os.fdopen(tmp_fd, "wb", closefd=False)
            try:
                with comp.open_write(raw_out, clamp_level(comp, ctx.level)) as writer:
                    src = os.fdopen(os.dup(src_fd), "rb", closefd=True)
                    try:
                        while True:
                            if ctx.interrupted():
                                raise Interrupted()
                            block = src.read(CHUNK)
                            if not block:
                                break
                            crc = zlib.crc32(block, crc)
                            copied += len(block)
                            writer.write(block)
                    finally:
                        src.close()
                raw_out.flush()
                os.fsync(tmp_fd)
            finally:
                raw_out.close()
            # The source must not have changed while we read it.
            st2 = os.fstat(src_fd)
            if (st2.st_size, st2.st_mtime_ns, st2.st_ino) != (src_st.st_size, src_st.st_mtime_ns, src_st.st_ino) \
                    or copied != src_st.st_size:
                raise SafetyRefusal("changed", "source changed during compression")
            out_size = os.fstat(tmp_fd).st_size
            _verify_temp(tmp_fd, comp, out_size, copied, crc, ctx.verify, ctx)
            if out_size >= ctx.min_ratio * info.size:
                raise SafetyRefusal("incompressible",
                                    f"compressed {out_size} B is not below {ctx.min_ratio:.0%} of {info.size} B")
            try:
                os.fchmod(tmp_fd, stat.S_IMODE(src_st.st_mode))
            except OSError:
                pass
            try:
                os.fchown(tmp_fd, src_st.st_uid, src_st.st_gid)
            except (OSError, AttributeError):
                pass
            os.close(tmp_fd)
            tmp_fd = -1
            os.utime(tmp_path, ns=(src_st.st_atime_ns, src_st.st_mtime_ns))
            # Rename into place, then remove the original.
            if os.path.lexists(dest_path):
                raise SafetyRefusal("dest_exists", f"{dest_name} appeared during compression")
            os.rename(tmp_path, dest_path)
            tmp_path = None
            try:
                os.fsync(dirfd)
            except OSError:
                pass
            os.unlink(info.name, dir_fd=dirfd)
            return ActionResult("ok", bytes_freed=info.size - out_size, dest_path=dest_path,
                                duration_s=ctx.clock() - started)
        finally:
            if src_fd is not None:
                try:
                    os.close(src_fd)
                except OSError:
                    pass
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            os.close(dirfd)
    except Interrupted:
        return ActionResult("skipped", reason="interrupted", duration_s=ctx.clock() - started)
    except SafetyRefusal as exc:
        return ActionResult("skipped", reason=exc.reason, error=str(exc),
                            duration_s=ctx.clock() - started)
    except OSError as exc:
        return ActionResult("failed", reason="os_error", error=str(exc),
                            duration_s=ctx.clock() - started)


# ---------------------------------------------------------------------------
# Empty-directory pruning
# ---------------------------------------------------------------------------
def prune_empty_dirs(dirs: Iterable[str], area_root: str,
                     protected: FrozenSet[str] = frozenset()) -> int:
    """Remove now-empty directories, walking upward but never reaching the root."""
    root = os.path.realpath(area_root)
    removed = 0
    for d in sorted(set(dirs), key=len, reverse=True):
        cur = os.path.realpath(d)
        while contained(cur, root):
            if cur in protected or any(p.startswith(cur + os.sep) for p in protected):
                break
            try:
                if os.path.islink(cur) or os.listdir(cur):
                    break
                os.rmdir(cur)
                removed += 1
            except OSError:
                break
            cur = os.path.dirname(cur)
    return removed
