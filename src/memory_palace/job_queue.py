"""Extraction Job Queue Core v0.1 (Q01)。

一个不调用模型的 SQLite 提取任务队列，面向冻结的单 Worker 架构。
一个 Job 代表一次已冻结 Capture 的普通处理任务（不是 Window）。

Queue 只做机械状态转换：
    登记新 Job                 -> queued
    claim（到期）queued/retry_wait -> running   （attempts += 1）
    mark_succeeded            -> succeeded
    mark_retryable_failure    -> retry_wait | failed   （按 attempts 上限）
    mark_failed               -> failed
    recover_running           -> retry_wait   （启动恢复，全部遗留 running）
    requeue_failed            -> retry_wait   （管理员重新排队，attempts 重置 0）

本模块不计算退避、不判断错误类型、不读取 Capture、不保存 cursor、不创建
continuation Job，也不实现 Worker/模型调用/Reviewer/heartbeat/lease/CLI。
"""

from __future__ import annotations

import sqlite3
import sys
from typing import Any, Callable

__all__ = [
    "JobQueueError",
    "MAX_ATTEMPTS",
    "RECOVERY_ERROR_CODE",
    "JOB_STATES",
    "JOB_FIELDS",
    "initialize_job_queue",
    "enqueue_job",
    "claim_due_job",
    "mark_succeeded",
    "mark_retryable_failure",
    "mark_failed",
    "recover_running",
    "requeue_failed",
    "get_job",
]

MAX_ATTEMPTS = 3
RECOVERY_ERROR_CODE = "worker_interrupted"

JOB_STATES = ("queued", "running", "retry_wait", "succeeded", "failed")

#: 全局状态机（文档用途）。enqueue 创建新行进入 queued；claim 走
#: queued/retry_wait -> running；recover_running 走 running -> retry_wait；
#: requeue_failed 走 failed -> retry_wait。入口函数用 _require_state 做
#: 精确当前态校验（见 mark_*/requeue 各自的"只有 X 可以"语义），
#: 不依赖这张表做宽校验。
_ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running"}),
    "retry_wait": frozenset({"running"}),
    "running": frozenset({"succeeded", "retry_wait", "failed"}),
    "failed": frozenset({"retry_wait"}),
    "succeeded": frozenset(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_jobs (
    enqueue_seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              TEXT NOT NULL UNIQUE,
    project_id          TEXT NOT NULL,
    source_session_id   TEXT NOT NULL,
    capture_path        TEXT NOT NULL,
    state               TEXT NOT NULL,
    attempts            INTEGER NOT NULL,
    queued_at_epoch     INTEGER NOT NULL,
    not_before_epoch    INTEGER NOT NULL,
    started_at_epoch    INTEGER,
    finished_at_epoch   INTEGER,
    updated_at_epoch    INTEGER NOT NULL,
    last_error_code     TEXT
);
CREATE INDEX IF NOT EXISTS idx_extraction_jobs_claim
    ON extraction_jobs (state, not_before_epoch, enqueue_seq);
"""

#: 每个 Job 的持久字段，按稳定顺序返回。
JOB_FIELDS = (
    "enqueue_seq",
    "job_id",
    "project_id",
    "source_session_id",
    "capture_path",
    "state",
    "attempts",
    "queued_at_epoch",
    "not_before_epoch",
    "started_at_epoch",
    "finished_at_epoch",
    "updated_at_epoch",
    "last_error_code",
)


class JobQueueError(Exception):
    """带固定机器码的业务异常。

    消息只包含固定 code，可附带 job_id；绝不包含 Capture 内容、环境、
    数据库转储或底层 SQLite 文本。
    """

    def __init__(self, code: str, job_id: str | None = None) -> None:
        self.code = code
        self.job_id = job_id
        message = code if job_id is None else f"{code}: {job_id}"
        super().__init__(message)


def _connect(database_path: str) -> sqlite3.Connection:
    """打开连接并进入 autocommit 模式，由调用方显式控制事务。"""
    if sys.version_info >= (3, 12):
        conn = sqlite3.connect(database_path, autocommit=True)
    else:
        conn = sqlite3.connect(database_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {field: row[field] for field in JOB_FIELDS}


def _run_in_transaction(
    database_path: str, work: Callable[[sqlite3.Connection], Any]
) -> Any:
    """在单个 IMMEDIATE 写事务中执行 work。

    每个状态转换都发生在这里，因此永远不会暴露成"先读后写"两步。
    任何异常都会回滚，不留部分状态。
    """
    conn = _connect(database_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = work(conn)
        conn.execute("COMMIT")
        return result
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def initialize_job_queue(database_path: str) -> None:
    """增量初始化：只创建 extraction_jobs 表与必要索引。

    不删除、不重建、不改写其他表；在已有 sentinel/Window Commit 表的
    数据库上运行后原表和数据保持不变。重复初始化安全。
    """
    conn = _connect(database_path)
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()


def enqueue_job(
    database_path: str,
    *,
    job_id: str,
    project_id: str,
    source_session_id: str,
    capture_path: str,
    queued_at_epoch: int,
    not_before_epoch: int | None = None,
) -> dict[str, Any]:
    """登记一个已冻结 Capture 的 queued Job。

    相同 job_id 再次登记必须固定报错 ``job_already_exists``，绝不把不同
    输入静默当作幂等成功。不做按 Capture/Session/路径/内容的去重，不检查
    Capture 文件是否存在，不创建 Project、cursor 或其他表记录。
    """
    not_before = queued_at_epoch if not_before_epoch is None else not_before_epoch

    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        try:
            conn.execute(
                "INSERT INTO extraction_jobs "
                "(job_id, project_id, source_session_id, capture_path, state, attempts, "
                " queued_at_epoch, not_before_epoch, started_at_epoch, finished_at_epoch, "
                " updated_at_epoch, last_error_code) "
                "VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, NULL, NULL, ?, NULL)",
                (
                    job_id,
                    project_id,
                    source_session_id,
                    capture_path,
                    queued_at_epoch,
                    not_before,
                    queued_at_epoch,
                ),
            )
        except sqlite3.IntegrityError:
            raise JobQueueError("job_already_exists", job_id) from None
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)

    return _run_in_transaction(database_path, work)


def _claim_one(
    conn: sqlite3.Connection, state: str, now_epoch: int
) -> dict[str, Any] | None:
    """领取 `state`（queued 或 retry_wait）中最早到期的一条。

    在调用方事务内执行，读取与更新原子不可分割。领取后 attempts 已 +1，
    finished_at/last_error_code 被清空。
    """
    target = conn.execute(
        "SELECT enqueue_seq FROM extraction_jobs "
        "WHERE state = ? AND not_before_epoch <= ? "
        "ORDER BY enqueue_seq LIMIT 1",
        (state, now_epoch),
    ).fetchone()
    if target is None:
        return None
    conn.execute(
        "UPDATE extraction_jobs "
        "SET state = 'running', attempts = attempts + 1, "
        "    started_at_epoch = ?, updated_at_epoch = ?, "
        "    finished_at_epoch = NULL, last_error_code = NULL "
        "WHERE enqueue_seq = ?",
        (now_epoch, now_epoch, target["enqueue_seq"]),
    )
    row = conn.execute(
        "SELECT * FROM extraction_jobs WHERE enqueue_seq = ?",
        (target["enqueue_seq"],),
    ).fetchone()
    return _row_to_dict(row)


def claim_due_job(database_path: str, *, now_epoch: int) -> dict[str, Any] | None:
    """原子领取一条到期 Job。

    顺序：到期 queued 优先于到期 retry_wait；同一状态按 enqueue_seq 升序。
    每次只领取一条，没有到期 Job 时返回 None（不等待、不 sleep）。
    """

    def work(conn: sqlite3.Connection) -> dict[str, Any] | None:
        claimed = _claim_one(conn, "queued", now_epoch)
        if claimed is None:
            claimed = _claim_one(conn, "retry_wait", now_epoch)
        return claimed

    return _run_in_transaction(database_path, work)


def _require_state(
    conn: sqlite3.Connection, job_id: str, required_state: str
) -> sqlite3.Row:
    """取回 Job 并精确校验当前状态。

    入口语义按 HANDOFF 逐条定义：只有 running 可以成功/报告失败/直接失败，
    只有 failed 可以 requeue。虽然 running -> retry_wait 在全局状态机里
    合法（mark_retryable_failure、recover_running），但 requeue_failed 的
    入口只允许 failed，因此这里不做"目标态在允许集合内"的宽校验。
    """
    row = conn.execute(
        "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise JobQueueError("job_not_found", job_id)
    if row["state"] != required_state:
        raise JobQueueError("invalid_job_transition", job_id)
    return row


def mark_succeeded(
    database_path: str, *, job_id: str, now_epoch: int
) -> dict[str, Any]:
    """只有 running 可以成功：进入 succeeded，写 finished/updated，清空错误码。"""

    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        _require_state(conn, job_id, "running")
        conn.execute(
            "UPDATE extraction_jobs "
            "SET state = 'succeeded', finished_at_epoch = ?, updated_at_epoch = ?, "
            "    last_error_code = NULL "
            "WHERE job_id = ?",
            (now_epoch, now_epoch, job_id),
        )
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)

    return _run_in_transaction(database_path, work)


def mark_retryable_failure(
    database_path: str,
    *,
    job_id: str,
    error_code: str,
    now_epoch: int,
    retry_at_epoch: int,
) -> dict[str, Any]:
    """只有 running 可以报告可重试失败。

    未达上限：进入 retry_wait，写 retry_at 与错误码；
    已达上限：直接进入 failed，忽略 retry_at，写 finished 与错误码。
    Queue 不计算退避、不判断错误类型。
    """

    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        row = _require_state(conn, job_id, "running")
        if row["attempts"] < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE extraction_jobs "
                "SET state = 'retry_wait', not_before_epoch = ?, "
                "    updated_at_epoch = ?, last_error_code = ? "
                "WHERE job_id = ?",
                (retry_at_epoch, now_epoch, error_code, job_id),
            )
        else:
            conn.execute(
                "UPDATE extraction_jobs "
                "SET state = 'failed', finished_at_epoch = ?, "
                "    updated_at_epoch = ?, last_error_code = ? "
                "WHERE job_id = ?",
                (now_epoch, now_epoch, error_code, job_id),
            )
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)

    return _run_in_transaction(database_path, work)


def mark_failed(
    database_path: str, *, job_id: str, error_code: str, now_epoch: int
) -> dict[str, Any]:
    """只有 running 可以直接失败：立即进入 failed，不经过 retry_wait。"""

    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        _require_state(conn, job_id, "running")
        conn.execute(
            "UPDATE extraction_jobs "
            "SET state = 'failed', finished_at_epoch = ?, updated_at_epoch = ?, "
            "    last_error_code = ? "
            "WHERE job_id = ?",
            (now_epoch, now_epoch, error_code, job_id),
        )
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)

    return _run_in_transaction(database_path, work)


def recover_running(database_path: str, *, now_epoch: int) -> list[dict[str, Any]]:
    """启动恢复：一个事务内把当前全部 running 改为 retry_wait。

    保留 attempts 与最近一次 started_at，清空 finished_at，写固定
    worker_interrupted，not_before=now_epoch。按 enqueue_seq 返回恢复后的
    Job；没有遗留项时返回空列表。
    """

    def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        targets = conn.execute(
            "SELECT enqueue_seq FROM extraction_jobs "
            "WHERE state = 'running' ORDER BY enqueue_seq"
        ).fetchall()
        if not targets:
            return []
        seqs = [t["enqueue_seq"] for t in targets]
        placeholders = ",".join("?" * len(seqs))
        conn.execute(
            "UPDATE extraction_jobs "
            "SET state = 'retry_wait', not_before_epoch = ?, updated_at_epoch = ?, "
            "    finished_at_epoch = NULL, last_error_code = ? "
            f"WHERE enqueue_seq IN ({placeholders})",
            (now_epoch, now_epoch, RECOVERY_ERROR_CODE, *seqs),
        )
        rows = conn.execute(
            f"SELECT * FROM extraction_jobs WHERE enqueue_seq IN ({placeholders}) "
            "ORDER BY enqueue_seq",
            seqs,
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    return _run_in_transaction(database_path, work)


def requeue_failed(database_path: str, *, job_id: str, now_epoch: int) -> dict[str, Any]:
    """管理员重新排队：只有 failed 可以。

    进入 retry_wait，attempts 重置为 0，not_before=now_epoch，清空
    started/finished/last_error_code。不复制 Job、不生成新 job_id、
    不改变 Project/Session/Capture。succeeded 不允许重新排队。
    """

    def work(conn: sqlite3.Connection) -> dict[str, Any]:
        _require_state(conn, job_id, "failed")
        conn.execute(
            "UPDATE extraction_jobs "
            "SET state = 'retry_wait', attempts = 0, not_before_epoch = ?, "
            "    started_at_epoch = NULL, finished_at_epoch = NULL, "
            "    last_error_code = NULL, updated_at_epoch = ? "
            "WHERE job_id = ?",
            (now_epoch, now_epoch, job_id),
        )
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)

    return _run_in_transaction(database_path, work)


def get_job(database_path: str, *, job_id: str) -> dict[str, Any] | None:
    """只读取回一个 Job 的完整持久字段，不存在时返回 None。"""
    conn = _connect(database_path)
    try:
        row = conn.execute(
            "SELECT * FROM extraction_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()
