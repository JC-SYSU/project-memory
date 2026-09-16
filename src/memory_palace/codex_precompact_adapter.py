"""Codex PreCompact Adapter v0.1。

把一条官方 Codex PreCompact 事件机械转换为一次 I01 Capture 冻结 + I02
Job 登记，并把首次接单、完整重放、安全忽略和明确失败区分清楚。

处理链固定为：

    PreCompact payload
      -> 机械校验官方字段（缺/错/空一律抛 PreCompactPayloadError）
      -> transcript_path 为 None：ignored(transcript_unavailable)
      -> cwd 未在显式绑定中精确匹配：ignored(project_unbound)
      -> 对既有 transcript 且已绑定 Project 的事件计算稳定身份
      -> 首见：I01 freeze_capture -> I02 register_frozen_capture_job -> accepted
      -> Capture 与 Job 都完整存在且核对通过：duplicate
      -> 仅 Capture / 仅 Job / 关键字段不符：PreCompactConflictError

平台与范围：

- 首先且只服务 Codex 主会话；全程不解析、不校验 transcript 正文，
  只把 transcript_path 原样交给 I01 冻结原始字节；
- session_id 原样作为 source_session_id；稳定身份只用
  ["codex-precompact-v0.1", session_id, turn_id, trigger]，不加模型、
  时间、路径、内容 hash 或随机数；
- 不做路径归一化、父目录回溯、Git 探测或自动建 Project；
- ignored 场景零副作用：不冻结、不登记、不访问 Queue、不创建目录；
- 不初始化 Queue、不读取系统时间（now_epoch 由调用方显式提供）、
  不读环境变量、不联网、不访问 Git；
- 本模块不是 Hook runner：不读 stdin、不写 stdout/stderr、不返回
  continue:false，不阻止 Codex 压缩；异常全部对调用方可见。

异常语义：payload 无效抛 PreCompactPayloadError；半完成或不一致的
稳定身份抛 PreCompactConflictError；I01/I02 的原始异常（含
FileNotFoundError、FileExistsError、JobQueueError、sqlite3.Error）
一律原样传播，I01 失败时 I02 不会被调用，I02 失败时已冻结的
Capture 保留，不补偿、不重试、不新增事件表。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from os import PathLike
from typing import Any

from .capture_job_registration import register_frozen_capture_job
from .capture_store import freeze_capture
from .job_queue import JOB_STATES, get_job

__all__ = [
    "PreCompactPayloadError",
    "PreCompactConflictError",
    "PreCompactReceipt",
    "validate_precompact_payload",
    "accept_codex_precompact",
]

#: 稳定身份数组的第一元素；一旦发布不可变。
_STABLE_SCHEMA_PREFIX = "codex-precompact-v0.1"

#: hook_event_name 只接受官方事件名。
_HOOK_EVENT_NAME = "PreCompact"

#: trigger 只接受官方两个值。
_VALID_TRIGGERS = ("manual", "auto")

#: 除 transcript_path（允许 None）外全部必填的非空字符串字段。
_REQUIRED_STRING_FIELDS = (
    "session_id",
    "cwd",
    "hook_event_name",
    "model",
    "turn_id",
    "trigger",
)

#: ignored 的固定 reason。
_IGNORED_TRANSCRIPT_UNAVAILABLE = "transcript_unavailable"
_IGNORED_PROJECT_UNBOUND = "project_unbound"


class PreCompactPayloadError(ValueError):
    """官方字段缺失、类型错误、空字符串、事件名或 trigger 非法。"""


class PreCompactConflictError(RuntimeError):
    """稳定身份只存在 Capture 或只存在 Job，或既有 Job 关键字段不符。

    不能冒充完整接单，不能自动补偿。
    """


@dataclass(frozen=True, slots=True)
class PreCompactReceipt:
    """一次接单决策的不可变收据，只含安全字段。

    disposition 取值 accepted / duplicate / ignored；reason 只在
    ignored 时非 None；event_key / capture_path / byte_count / job_id /
    job_state 在 ignored 时全部为 None；project_id 仅在 Project 未绑定
    时为 None；source_session_id 是 payload.session_id 的原样值。
    """

    disposition: str
    reason: str | None
    event_key: str | None
    capture_path: str | None
    byte_count: int | None
    job_id: str | None
    project_id: str | None
    source_session_id: str
    job_state: str | None


def _require_nonempty_string(
    payload: Mapping[str, object], field: str
) -> str:
    """取必填字符串字段：缺失、非字符串（含 bool）、空字符串均失败。"""
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise PreCompactPayloadError(
            f"invalid field {field!r}: must be a non-empty string"
        )
    return value


def validate_precompact_payload(payload: object) -> Mapping[str, object]:
    """纯校验一条官方 PreCompact payload，返回原 Mapping。

    transcript_path 允许 None；为字符串时必须非空。未知额外字段
    允许存在并忽略。不校验 transcript 正文格式，不要求路径位于任何
    特定目录，也不访问时钟、文件、SQLite 或其他外部状态。
    """
    if not isinstance(payload, Mapping):
        raise PreCompactPayloadError("payload must be a mapping")
    for field in _REQUIRED_STRING_FIELDS:
        _require_nonempty_string(payload, field)
    if not os.path.isabs(payload["cwd"]):
        raise PreCompactPayloadError("invalid cwd: must be absolute")

    # transcript_path 必须存在（允许显式 None）；缺失与 None 是不同输入。
    if "transcript_path" not in payload:
        raise PreCompactPayloadError(
            "invalid field 'transcript_path': missing required field"
        )
    transcript_path = payload["transcript_path"]
    if transcript_path is not None and (
        not isinstance(transcript_path, str) or not transcript_path
    ):
        raise PreCompactPayloadError(
            "invalid field 'transcript_path': must be a non-empty string or None"
        )

    if payload["hook_event_name"] != _HOOK_EVENT_NAME:
        raise PreCompactPayloadError(
            f"invalid hook_event_name: {payload['hook_event_name']!r}"
        )
    if payload["trigger"] not in _VALID_TRIGGERS:
        raise PreCompactPayloadError(
            f"invalid trigger: {payload['trigger']!r}"
        )
    return payload


def _stable_identity(session_id: str, turn_id: str, trigger: str) -> tuple[str, str, str]:
    """按冻结规格计算稳定身份，返回 (event_key, capture_filename, job_id)。

    稳定输入严格为
    ["codex-precompact-v0.1", session_id, turn_id, trigger]，
    编码必须为指定 json.dumps 参数生成的 UTF-8 字节，
    event_key 是这些字节的十六进制 SHA-256（64 个小写字符）。
    """
    stable_bytes = json.dumps(
        [_STABLE_SCHEMA_PREFIX, session_id, turn_id, trigger],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    event_key = hashlib.sha256(stable_bytes).hexdigest()
    capture_filename = f"codex-precompact-{event_key}.jsonl"
    job_id = f"codex-precompact-{event_key}"
    return event_key, capture_filename, job_id


def _receipt_ignored(
    *, reason: str, project_id: str | None, source_session_id: str
) -> PreCompactReceipt:
    return PreCompactReceipt(
        disposition="ignored",
        reason=reason,
        event_key=None,
        capture_path=None,
        byte_count=None,
        job_id=None,
        project_id=project_id,
        source_session_id=source_session_id,
        job_state=None,
    )


def accept_codex_precompact(
    payload: Mapping[str, object],
    project_bindings: Mapping[str, str],
    capture_directory: str | PathLike[str],
    database_path: str | PathLike[str],
    *,
    now_epoch: int,
) -> PreCompactReceipt:
    """机械转换一次官方 PreCompact 事件为接单决策（见模块 docstring）。"""
    payload = validate_precompact_payload(payload)

    session_id = payload["session_id"]
    transcript_path = payload["transcript_path"]
    cwd_value = payload["cwd"]

    # ignored 分支先行：不冻结、不登记、不访问 Queue、不创建目录。
    if transcript_path is None:
        # transcript_path 不可用优先于 cwd 未绑定；两个分支都零副作用。
        return _receipt_ignored(
            reason=_IGNORED_TRANSCRIPT_UNAVAILABLE,
            project_id=project_bindings.get(cwd_value),
            source_session_id=session_id,
        )

    # Project 绑定：Python mapping 的字符串精确匹配，不做任何路径整理。
    project_id = project_bindings.get(cwd_value)
    if project_id is None:
        return _receipt_ignored(
            reason=_IGNORED_PROJECT_UNBOUND,
            project_id=None,
            source_session_id=session_id,
        )

    event_key, capture_filename, job_id = _stable_identity(
        session_id, payload["turn_id"], payload["trigger"]
    )
    # Capture 目标严格为 join(fspath(capture_directory), 固定文件名)；不创建目录。
    capture_path = os.path.join(os.fspath(capture_directory), capture_filename)

    # 冻结前先看既有状态：capture only / job only 都是明确冲突。
    capture_exists = os.path.lexists(capture_path)
    existing_job = get_job(os.fspath(database_path), job_id=job_id)

    capture_only_conflict = capture_exists and existing_job is None
    job_only_conflict = (not capture_exists) and existing_job is not None
    if capture_only_conflict:
        raise PreCompactConflictError(f"capture exists without job: {job_id}")
    if job_only_conflict:
        raise PreCompactConflictError(f"job exists without capture: {job_id}")

    if capture_exists and existing_job is not None:
        # 完整重放：完整核对既有 Job 的后返回 duplicate。
        expected_capture_path = os.path.abspath(capture_path)
        mismatched: list[str] = []
        if existing_job["job_id"] != job_id:
            mismatched.append("job_id")
        if existing_job["project_id"] != project_id:
            mismatched.append("project_id")
        if existing_job["source_session_id"] != session_id:
            mismatched.append("source_session_id")
        if existing_job["capture_path"] != expected_capture_path:
            mismatched.append("capture_path")
        if existing_job["state"] not in JOB_STATES:
            mismatched.append(f"state={existing_job['state']!r}")
        if mismatched:
            raise PreCompactConflictError(
                f"duplicate check failed for {job_id}: {','.join(mismatched)}"
            )
        return PreCompactReceipt(
            disposition="duplicate",
            reason=None,
            event_key=event_key,
            capture_path=expected_capture_path,
            byte_count=None,  # duplicate 不重新读 Capture，允许为 None。
            job_id=job_id,
            project_id=project_id,
            source_session_id=session_id,
            job_state=existing_job["state"],  # 如实返回既有状态，不改写。
        )

    # 首次接单：I01 一次，成功后 I02 一次；严格顺序复用冻结入口。
    frozen = freeze_capture(payload["transcript_path"], capture_path)
    job = register_frozen_capture_job(
        os.fspath(database_path),
        job_id=job_id,
        project_id=project_id,
        source_session_id=session_id,
        frozen_capture=frozen,
        now_epoch=now_epoch,
    )
    return PreCompactReceipt(
        disposition="accepted",
        reason=None,
        event_key=event_key,
        capture_path=frozen.path,
        byte_count=frozen.byte_count,
        job_id=job["job_id"],
        project_id=job["project_id"],
        source_session_id=job["source_session_id"],
        job_state=job["state"],
    )
