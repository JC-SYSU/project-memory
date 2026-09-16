"""Window Commit Core v0.1：一次已验证 Window 结果的原子且幂等提交。

职责边界：

- 输入是冻结 Window + 已通过冻结 Validator 的完整 validation dict；
- 本模块不调用模型、不重新解析模型正文、不重复校验可信上游字段；
- 全部写入（Record/Evidence、ReviewItem、cursor、usage、AuditEvent、commit
  marker）在同一个 SQLite 事务中完成，任一失败整体回滚、cursor 不前进；
- window_run_id 是唯一幂等键：重放不新增/修改任何行，也不重新调用 clock
  或 id_factory 生成持久对象。

不属于本阶段：Job Queue、Worker 循环、Reviewer、FTS、Recall、Hook、CLI、
重试、价格计算与第三方框架。只使用标准库 sqlite3。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .contracts import Window

# 固定安全错误码：响应级失败时抛出，数据库零变化。
RESPONSE_NOT_COMMITTABLE = "response_not_committable"

# usage 白名单：只保存实际存在的非敏感 token 字段，不新增字段。
_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")

# 固定的领域常量。
_ACTIVE_RECORD_STATUS = "active"
_ARCHIVED_RECORD_STATUS = "archived"
_VALID_RECORD_STATUSES = (_ACTIVE_RECORD_STATUS, _ARCHIVED_RECORD_STATUS)
_REVIEW_STATE = "queued"
_AUDIT_EVENT_TYPE = "window_committed"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS records (
    record_id       TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    window_run_id   TEXT NOT NULL,
    candidate_index INTEGER NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL,
    status          TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    UNIQUE (window_run_id, candidate_index)
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id     TEXT PRIMARY KEY,
    record_id       TEXT NOT NULL REFERENCES records(record_id),
    source_session_id TEXT NOT NULL,
    source_line     INTEGER NOT NULL,
    evidence_index  INTEGER NOT NULL,
    UNIQUE (record_id, evidence_index)
);

CREATE TABLE IF NOT EXISTS review_items (
    review_item_id      TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    source_session_id   TEXT NOT NULL,
    window_run_id       TEXT NOT NULL,
    candidate_index     INTEGER NOT NULL,
    raw_candidate_json  TEXT NOT NULL,
    validation_errors_json TEXT NOT NULL,
    window_snapshot_json    TEXT NOT NULL,
    state               TEXT NOT NULL,
    attempts            INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (window_run_id, candidate_index)
);

CREATE TABLE IF NOT EXISTS session_cursor (
    source_session_id   TEXT PRIMARY KEY,
    consumed_through_line INTEGER NOT NULL,
    window_run_id       TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_usage (
    window_run_id   TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    model           TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    duration_ms     INTEGER,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    total_tokens    INTEGER,
    reasoning_tokens INTEGER,
    recorded_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_event_id  TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    window_run_id   TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS window_commit_markers (
    window_run_id   TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    record_ids_json TEXT NOT NULL,
    review_item_ids_json TEXT NOT NULL,
    record_count    INTEGER NOT NULL,
    review_item_count INTEGER NOT NULL,
    committed_through_line INTEGER NOT NULL,
    committed_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_session ON records(source_session_id);
"""


def _default_clock() -> str:
    """默认 UTC 系统时间，秒级精度。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_id_factory() -> str:
    """默认系统生成 UUID。"""
    return str(uuid.uuid4())


def initialize_database(database_path: str) -> None:
    """建立本阶段所需表、唯一键、外键与索引。"""
    conn = sqlite3.connect(database_path)
    try:
        # 外键约束是每连接开关：本模块拥有的连接必须显式启用，
        # 否则 evidence.record_id -> records.record_id 形同虚设。
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA_SQL)
    finally:
        conn.close()


def _build_window_snapshot(window: Window) -> dict[str, Any]:
    """冻结 Window 的正式快照，供未来 Reviewer 使用。

    只含全部正式消息的 source_line/timestamp/role/body 与
    consumed_through_line、body_chars；不含 API Key、Prompt、URL、请求头
    或完整模型响应。
    """
    return {
        "messages": [message.as_dict() for message in window.messages],
        "consumed_through_line": window.consumed_through_line,
        "body_chars": window.body_chars,
    }


def _collect_candidate_warnings(validation: dict[str, Any]) -> list[dict[str, Any]]:
    """收集全部合法与非法 Candidate 的 warnings，只用于 Audit。"""
    warnings: list[dict[str, Any]] = []
    for candidate in validation.get("valid_candidates", []):
        warnings.extend(candidate.get("warnings", []))
    for candidate in validation.get("invalid_candidates", []):
        warnings.extend(candidate.get("warnings", []))
    return warnings


def commit_window_result(
    database_path: str,
    *,
    project_id: str,
    source_session_id: str,
    window_run_id: str,
    window: Window,
    validation: dict[str, Any],
    model: str,
    reasoning_effort: str,
    usage: dict[str, Any] | None,
    duration_ms: int | None,
    record_status: str = _ACTIVE_RECORD_STATUS,
    clock: Callable[[], str] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """原子且幂等地提交一次已验证的 Window 结果。

    返回（首次提交）等价于::

        {
            "status": "committed",
            "idempotent_replay": False,
            "record_ids": [...],
            "review_item_ids": [...],
            "committed_through_line": 205,
        }

    重放同一 window_run_id 时返回首次提交产生的 ID 与
    ``idempotent_replay=True``，不产生任何数据库变化。

    响应级失败（``validation["response_errors"]`` 非空）时抛出
    :data:`RESPONSE_NOT_COMMITTABLE`，完全不接触数据库。
    """
    # 1. 响应级失败：在接触任何数据库之前拒绝，保证零写入。
    if validation.get("response_errors"):
        raise ValueError(RESPONSE_NOT_COMMITTABLE)
    if record_status not in _VALID_RECORD_STATUSES:
        raise ValueError(f"window_commit: invalid record status {record_status!r}")

    conn = sqlite3.connect(database_path, isolation_level=None)
    try:
        # 外键约束必须在本连接上启用（且需在事务外），与初始化连接一致。
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")

        # 2. 幂等检查：同一 window_run_id 已提交则直接返回首次结果。
        #    重放路径不调用 clock 或 id_factory，不生成任何持久对象。
        marker_row = conn.execute(
            "SELECT record_ids_json, review_item_ids_json, committed_through_line"
            "  FROM window_commit_markers"
            " WHERE window_run_id = ?",
            (window_run_id,),
        ).fetchone()
        if marker_row is not None:
            conn.execute("ROLLBACK")
            return {
                "status": "committed",
                "idempotent_replay": True,
                "record_ids": json.loads(marker_row[0]),
                "review_item_ids": json.loads(marker_row[1]),
                "committed_through_line": marker_row[2],
            }

        now = clock() if clock is not None else _default_clock()
        new_id = id_factory if id_factory is not None else _default_id_factory

        # 3. Record + Evidence。as_of 按 Window 消息顺序找到该 Candidate
        #    引用的最后一条消息，原样使用其 timestamp，不解析时间格式。
        record_ids: list[str] = []
        for candidate in validation.get("valid_candidates", []):
            candidate_index = candidate["index"]
            normalized = candidate["normalized_candidate"]
            evidence_lines = normalized["evidence_lines"]
            as_of: str | None = None
            for message in window.messages:
                if message.source_line in evidence_lines:
                    as_of = message.timestamp
            if as_of is None:
                raise ValueError(
                    "window_commit: candidate evidence does not match any window message"
                )
            record_id = new_id()
            record_ids.append(record_id)
            conn.execute(
                "INSERT INTO records"
                " (record_id, project_id, source_session_id, window_run_id,"
                "  candidate_index, title, summary, status, captured_at, as_of)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record_id, project_id, source_session_id, window_run_id,
                 candidate_index, normalized["title"], normalized["summary"],
                 record_status, now, as_of),
            )
            for evidence_index, source_line in enumerate(evidence_lines):
                conn.execute(
                    "INSERT INTO evidence"
                    " (evidence_id, record_id, source_session_id, source_line,"
                    "  evidence_index)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (new_id(), record_id, source_session_id, source_line,
                     evidence_index),
                )

        # 4. ReviewItem：每条非法 Candidate 一个，状态固定 queued，
        #    保存 raw Candidate、errors 与原 Window 快照。
        review_item_ids: list[str] = []
        snapshot = _build_window_snapshot(window)
        for candidate in validation.get("invalid_candidates", []):
            candidate_index = candidate["index"]
            review_item_id = new_id()
            review_item_ids.append(review_item_id)
            conn.execute(
                "INSERT INTO review_items"
                " (review_item_id, project_id, source_session_id, window_run_id,"
                "  candidate_index, raw_candidate_json, validation_errors_json,"
                "  window_snapshot_json, state, attempts, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (review_item_id, project_id, source_session_id, window_run_id,
                 candidate_index,
                 json.dumps(candidate["raw_candidate"], ensure_ascii=False),
                 json.dumps(candidate["errors"], ensure_ascii=False),
                 json.dumps(snapshot, ensure_ascii=False),
                 _REVIEW_STATE, 0, now),
            )

        # 5. Session cursor：写为 window.consumed_through_line。
        conn.execute(
            "INSERT INTO session_cursor"
            " (source_session_id, consumed_through_line, window_run_id, updated_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(source_session_id) DO UPDATE SET"
            "   consumed_through_line = excluded.consumed_through_line,"
            "   window_run_id = excluded.window_run_id,"
            "   updated_at = excluded.updated_at",
            (source_session_id, window.consumed_through_line, window_run_id, now),
        )

        # 6. Model usage：白名单字段，缺失如实为 NULL。
        usage_values: dict[str, Any] = {}
        if isinstance(usage, dict):
            for field in _USAGE_FIELDS:
                if field in usage:
                    usage_values[field] = usage[field]
        conn.execute(
            "INSERT INTO model_usage"
            " (window_run_id, project_id, source_session_id, model, reasoning_effort,"
            "  duration_ms, prompt_tokens, completion_tokens, total_tokens,"
            "  reasoning_tokens, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (window_run_id, project_id, source_session_id, model, reasoning_effort,
             duration_ms,
             usage_values.get("prompt_tokens"),
             usage_values.get("completion_tokens"),
             usage_values.get("total_tokens"),
             usage_values.get("reasoning_tokens"),
             now),
        )

        # 7. AuditEvent：window_committed，记录 Run、Record/ReviewItem 数
        #    与全部 response/Candidate warnings。warnings 只进 Audit。
        audit_payload = json.dumps(
            {
                "window_run_id": window_run_id,
                "record_count": len(record_ids),
                "review_item_count": len(review_item_ids),
                "response_warnings": validation.get("response_warnings", []),
                "candidate_warnings": _collect_candidate_warnings(validation),
            },
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT INTO audit_events"
            " (audit_event_id, project_id, source_session_id, window_run_id,"
            "  event_type, payload_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (new_id(), project_id, source_session_id, window_run_id,
             _AUDIT_EVENT_TYPE, audit_payload, now),
        )

        # 8. 幂等 commit marker（最后写入）。
        conn.execute(
            "INSERT INTO window_commit_markers"
            " (window_run_id, project_id, source_session_id, record_ids_json,"
            "  review_item_ids_json, record_count, review_item_count,"
            "  committed_through_line, committed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (window_run_id, project_id, source_session_id,
             json.dumps(record_ids), json.dumps(review_item_ids),
             len(record_ids), len(review_item_ids),
             window.consumed_through_line, now),
        )

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {
        "status": "committed",
        "idempotent_replay": False,
        "record_ids": record_ids,
        "review_item_ids": review_item_ids,
        "committed_through_line": window.consumed_through_line,
    }
