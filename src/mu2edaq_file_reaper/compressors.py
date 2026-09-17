"""Compression algorithm registry.

gzip, bz2 and xz come from the standard library; zstd needs the optional
``zstandard`` package and is marked unavailable without it.
"""

import bz2
import gzip
import lzma
from dataclasses import dataclass
from typing import BinaryIO, Callable, Dict, Tuple

try:                                     # optional
    import zstandard as _zstd
except Exception:                         # pragma: no cover
    _zstd = None


@dataclass(frozen=True)
class Compressor:
    name:       str
    suffix:     str
    available:  bool
    open_write: Callable[[BinaryIO, int], BinaryIO]   # (raw fileobj, level) -> writer
    open_read:  Callable[[BinaryIO], BinaryIO]        # (raw fileobj) -> reader
    max_level:  int


def _gzip_w(fh, level):  return gzip.GzipFile(fileobj=fh, mode="wb", compresslevel=level, mtime=0)
def _gzip_r(fh):         return gzip.GzipFile(fileobj=fh, mode="rb")
def _bz2_w(fh, level):   return bz2.BZ2File(fh, mode="wb", compresslevel=level)
def _bz2_r(fh):          return bz2.BZ2File(fh, mode="rb")
def _xz_w(fh, level):    return lzma.LZMAFile(fh, mode="wb", preset=level)
def _xz_r(fh):           return lzma.LZMAFile(fh, mode="rb")


def _zstd_w(fh, level):
    return _zstd.ZstdCompressor(level=level).stream_writer(fh, closefd=False)


def _zstd_r(fh):
    return _zstd.ZstdDecompressor().stream_reader(fh, closefd=False)


COMPRESSORS: Dict[str, Compressor] = {
    "gzip": Compressor("gzip", ".gz",  True, _gzip_w, _gzip_r, 9),
    "bz2":  Compressor("bz2",  ".bz2", True, _bz2_w,  _bz2_r,  9),
    "xz":   Compressor("xz",   ".xz",  True, _xz_w,   _xz_r,   9),
    "zstd": Compressor("zstd", ".zst", _zstd is not None, _zstd_w, _zstd_r, 22),
}

#: Files already carrying one of these suffixes are never compressed again.
COMPRESSED_SUFFIXES: Tuple[str, ...] = (".gz", ".bz2", ".xz", ".zst", ".zip", ".7z", ".lz4",
                                        ".tgz", ".tbz2", ".txz", ".rar")


class CompressorError(ValueError):
    pass


def get_compressor(name: str) -> Compressor:
    comp = COMPRESSORS.get((name or "gzip").lower())
    if comp is None:
        raise CompressorError(f"unknown compression algorithm {name!r} "
                              f"(expected one of {', '.join(COMPRESSORS)})")
    if not comp.available:
        raise CompressorError(f"compression algorithm {name!r} needs the optional "
                              f"'zstandard' package (pip install zstandard)")
    return comp


def clamp_level(comp: Compressor, level: int) -> int:
    return max(1, min(int(level), comp.max_level))
