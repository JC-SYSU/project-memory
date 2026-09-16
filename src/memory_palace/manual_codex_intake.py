"""Manual Codex Session Intake v0.1 (I03)。

调用方显式指定既有 Codex 会话 JSONL 的来源路径、Capture 目标路径、
隔离数据库路径与全部身份字段；顺序调用冻结 I01 freeze_capture()
与冻结 I02 register_frozen_capture_job()，两步都成功才返回带
accepted=True 的收据。本入口首先且只服务 Codex 主会话，不涉及
其他平台。

本模块不做：会话扫描、Project/Session 推断、ID 与时间生成、队列
初始化、自动重试、Capture 删除、模型调用、环境读取、命令行入口。
只用标准库与冻结的 .capture_store / .capture_job_registration。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from os import PathLike
from typing import Any

from .capture_job_registration import register_frozen_capture_job
from .capture_store import freeze_capture
from .job_queue import get_job
from .project_paths import (
    ProjectPathError,
    ensure_project_state,
    read_session_metadata,
)

__all__ = [
    "ManualIntakeReceipt",
    "ManualSessionDuplicateError",
    "accept_manual_codex_session",
    "intake_codex_session_jsonl",
]


@dataclass(frozen=True, slots=True)
class ManualIntakeReceipt:
    """接单成功后的不可变收据，只含安全字段。

    accepted 只证明 Capture 已冻结且 Job 已可靠登记，不证明任何后续
    处理已经发生。
    """

    accepted: bool
    capture_path: str
    byte_count: int
    job_id: str
    project_id: str
    source_session_id: str
    job_state: str


class ManualSessionDuplicateError(RuntimeError):
    """The deterministic session capture and job already exist together."""


def accept_manual_codex_session(
    source_path: str | os.PathLike[str],
    capture_destination_path: str | os.PathLike[str],
    database_path: str | os.PathLike[str],
    *,
    job_id: str,
    project_id: str,
    source_session_id: str,
    now_epoch: int,
) -> ManualIntakeReceipt:
    """冻结指定 Codex 会话并登记一条 queued Job。

    固定顺序：I01 一次，成功后 I02 一次；I01 失败时 I02 调用次数为 0。
    所有参数原样转交，不生成、不推断、不整理。登记失败时 Capture 保留。
    不返回 accepted=False；失败就是异常原样传播。
    """
    frozen = freeze_capture(source_path, capture_destination_path)
    job = register_frozen_capture_job(
        os.fspath(database_path),
        job_id=job_id,
        project_id=project_id,
        source_session_id=source_session_id,
        frozen_capture=frozen,
        now_epoch=now_epoch,
    )
    return ManualIntakeReceipt(
        accepted=True,
        capture_path=frozen.path,
        byte_count=frozen.byte_count,
        job_id=job["job_id"],
        project_id=job["project_id"],
        source_session_id=job["source_session_id"],
        job_state=job["state"],
    )


def _manual_identity(session_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"manual-codex-{digest}", f"manual-codex-{digest}.jsonl"


def intake_codex_session_jsonl(
    source_path: str | os.PathLike[str], *, now_epoch: int
) -> ManualIntakeReceipt:
    """Intake one Codex JSONL using only its structured session_meta identity."""
    metadata = read_session_metadata(source_path)
    paths = ensure_project_state(metadata.cwd)
    job_id, capture_name = _manual_identity(metadata.session_id)
    capture_path = os.path.join(paths.captures_directory, capture_name)
    capture_exists = os.path.lexists(capture_path)
    existing_job = get_job(paths.database_path, job_id=job_id)
    if capture_exists and existing_job is not None:
        raise ManualSessionDuplicateError("manual_session_already_intaken")
    if capture_exists or existing_job is not None:
        raise ProjectPathError("manual_session_state_conflict")
    return accept_manual_codex_session(
        source_path,
        capture_path,
        paths.database_path,
        job_id=job_id,
        project_id=paths.project_id,
        source_session_id=metadata.session_id,
        now_epoch=now_epoch,
    )
