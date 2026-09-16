"""Frozen Capture Job Registration v0.1 (I02)。

把调用方明确提供的 FrozenCapture 与 Job 字段，原样转交给冻结 Q01
enqueue_job()，返回 queued Job。

登记失败时保留 Capture，不自动删除。本模块不读取、不 stat、不校验
Capture，不做路径或内容去重；不初始化队列、不包装异常、不重试、
不唤醒常驻进程。只用 Python 标准库与冻结的 .capture_store / .job_queue。
"""

from __future__ import annotations

import os
from os import PathLike
from typing import Any

from .capture_store import FrozenCapture
from .job_queue import enqueue_job

__all__ = ["register_frozen_capture_job"]


def register_frozen_capture_job(
    database_path: str | PathLike[str],
    *,
    job_id: str,
    project_id: str,
    source_session_id: str,
    frozen_capture: FrozenCapture,
    now_epoch: int,
) -> dict[str, Any]:
    """把已冻结 Capture 登记为一条 queued Extraction Job。

    语义严格等价于一次 enqueue_job() 调用：job_id / project_id /
    source_session_id / now_epoch 原样转交，capture_path 只取
    frozen_capture.path。异常（JobQueueError / sqlite3.Error / 其他）
    一律原样传播，不包装、不吞掉、不翻译；失败后不做任何文件操作。
    """
    return enqueue_job(
        os.fspath(database_path),
        job_id=job_id,
        project_id=project_id,
        source_session_id=source_session_id,
        capture_path=frozen_capture.path,
        queued_at_epoch=now_epoch,
        not_before_epoch=now_epoch,
    )
