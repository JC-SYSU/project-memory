"""Open Codex rollout files whether plain ``.jsonl`` or ``.jsonl.zst``.

Codex compresses cold local rollouts in the background (rollout
``compression.rs``: files older than 7 days become ``<name>.jsonl.zst`` and
the plain file is removed; appending to a resumed session decompresses it
back).  Any palace path that reads a raw Codex session must therefore accept
both suffixes.  Captures inside ``.memory-palace/captures`` stay plain JSONL:
callers decompress once at freeze time.

Decoder choice: stdlib ``compression.zstd`` (Python 3.14+) when present,
otherwise the ``zstandard`` package.  Missing both raises a clear error only
when a compressed file is actually opened.
"""

from __future__ import annotations

import io
import os
from os import PathLike
from typing import BinaryIO

__all__ = [
    "COMPRESSED_SUFFIX",
    "ROLLOUT_SUFFIXES",
    "RolloutDecodeError",
    "RolloutDecoderUnavailable",
    "is_rollout_file_name",
    "open_rollout_binary",
    "open_rollout_text",
]

COMPRESSED_SUFFIX = ".zst"
ROLLOUT_SUFFIXES = (".jsonl", ".jsonl.zst")


class RolloutDecoderUnavailable(RuntimeError):
    """A ``.zst`` rollout was opened but no zstd decoder is installed."""


class RolloutDecodeError(OSError):
    """A ``.zst`` rollout is corrupt or truncated.

    Subclasses OSError so every existing ``except OSError`` read path treats a
    damaged compressed rollout exactly like an unreadable plain file.
    """


class _DecodeErrorMapper(io.RawIOBase):
    """Re-raise library-specific zstd errors as RolloutDecodeError."""

    def __init__(self, inner: BinaryIO) -> None:
        self._inner = inner

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # type: ignore[override]
        try:
            data = self._inner.read(len(buffer))
        except OSError:
            raise
        except Exception as exc:  # zstandard.ZstdError / compression.zstd.ZstdError
            raise RolloutDecodeError("zstd_decode_failed") from exc
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            super().close()


def is_rollout_file_name(name: str) -> bool:
    return name.endswith(ROLLOUT_SUFFIXES)


def _open_zstd_raw(path: str) -> BinaryIO:
    try:
        from compression import zstd  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        return zstd.open(path, "rb")
    try:
        import zstandard
    except ImportError:
        raise RolloutDecoderUnavailable("zstd_decoder_unavailable") from None
    handle = open(path, "rb")
    try:
        # read_across_frames: tolerate multi-frame files, matching `zstd -dc`.
        return zstandard.ZstdDecompressor().stream_reader(
            handle, read_across_frames=True, closefd=True
        )
    except Exception:
        handle.close()
        raise


def _open_zstd(path: str) -> BinaryIO:
    return io.BufferedReader(_DecodeErrorMapper(_open_zstd_raw(path)))


def open_rollout_binary(path: str | PathLike[str]) -> BinaryIO:
    """Open a rollout for binary reading, decompressing ``.zst`` transparently."""
    path = os.fspath(path)
    if path.endswith(COMPRESSED_SUFFIX):
        return _open_zstd(path)
    return open(path, "rb")


def open_rollout_text(
    path: str | PathLike[str], *, errors: str = "strict"
) -> io.TextIOWrapper:
    """Open a rollout as UTF-8 text with universal newlines disabled."""
    return io.TextIOWrapper(
        open_rollout_binary(path), encoding="utf-8", errors=errors, newline=""
    )