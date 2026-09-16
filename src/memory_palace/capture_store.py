"""Immutable Capture Freeze v0.1 (I01)。

把调用方明确指定的源文件原始字节，无覆盖、原子地冻结到调用方明确
指定的目标路径，返回冻结结果。

不可变语义：

- 成功后本模块不再修改该 Capture；
- 再次写入同一目标必须拒绝（``FileExistsError``），不覆盖、不续写、
  不静默当作成功；
- 调用方后续修改或删除源文件，不影响已冻结 Capture；
- 目标在完整复制成功前不会作为正式 Capture 暴露。

本模块不做：JSONL 解析或重写、内容 Hash、manifest、Job/Hook/receipt、
Project/Session 绑定、命名策略与清理策略。只用 Python 标准库；
不读环境变量、不联网、不访问 SQLite。
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from os import PathLike

__all__ = ["FrozenCapture", "freeze_capture"]

#: 复制块大小；只影响复制性能，不影响结果字节。
_CHUNK_SIZE = 1024 * 1024

#: 临时文件前缀：与目标同目录、mkstemp 唯一命名，保证只清理自己的文件。
_TEMP_PREFIX = "capture_freeze_tmp_"


@dataclass(frozen=True, slots=True)
class FrozenCapture:
    """成功发布后的冻结结果。"""

    path: str  # 成功发布后的目标路径（文件系统绝对路径）
    byte_count: int  # 实际冻结的原始字节数


def _cleanup_own_temp(tmp_path: str) -> None:
    """只删除本次调用自己创建且仍未发布的临时文件；不存在时忽略。

    绝不删除任何预先存在的文件或其他调用的临时文件——只持有并移除
    本次 mkstemp 返回的唯一路径。
    """
    try:
        os.unlink(tmp_path)
    except OSError:
        pass


def freeze_capture(
    source_path: str | PathLike[str],
    destination_path: str | PathLike[str],
) -> FrozenCapture:
    """把源文件原始字节无损、无覆盖、原子地冻结到目标路径。

    目标父目录必须已经存在；本函数不递归创建目录。目标已存在时抛
    ``FileExistsError`` 且不触碰已有文件。发布前失败时只清理本次
    调用自己创建的临时文件，最终目标不会在此之前出现。

    异常沿用标准文件异常：源不存在/不可读、父目录不存在/不可写、
    目标已存在、复制或发布失败均抛对应 ``OSError`` 子类。
    """
    source = os.fspath(source_path)
    destination = os.fspath(destination_path)
    destination_parent = os.path.dirname(destination) or "."

    # 快速失败：目标已存在。最终裁决由发布时的无覆盖原子关联给出，
    # 这里只是提前返回，避免无谓复制。
    if os.path.lexists(destination):
        raise FileExistsError(f"destination already exists: {destination}")

    byte_count = 0
    tmp_fd: int | None = None
    tmp_path: str | None = None
    published = False
    try:
        # 在目标同一目录创建唯一临时文件；父目录不存在或不可写时
        # mkstemp 抛出对应 OSError，且不会留下任何文件。
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=_TEMP_PREFIX, dir=destination_parent)
        with os.fdopen(tmp_fd, "wb") as tmp_file:
            tmp_fd = None
            with open(source, "rb") as src_file:
                while True:
                    chunk = src_file.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    tmp_file.write(chunk)
                    byte_count += len(chunk)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())

        # 无覆盖原子发布：目标已存在（含并发争用）时 os.link 抛
        # FileExistsError，原文件字节与元数据保持不变。
        os.link(tmp_path, destination)
        published = True
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            if published:
                # 发布已成功：移除本调用的临时名字，避免同一 inode 双名残留。
                os.unlink(tmp_path)
            else:
                # 发布前失败：只清理自己的临时文件。
                _cleanup_own_temp(tmp_path)

    return FrozenCapture(path=os.path.abspath(destination), byte_count=byte_count)
