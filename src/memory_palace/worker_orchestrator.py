"""Single Worker Orchestrator v0.1 (W01) — 单次串行编排核心。

职责边界：

- 一次 `run_once()` 只领取一个到期 Extraction Job；
- 从该 Source Session 已提交 cursor + 1 开始预处理冻结 Capture；
- 逐 Window 严格串行：每页一次 `extract_window`，响应级成功才调用
  冻结 `commit_window_result`，提交成功后才进入下一 Window；
- W01-P01：每个非空 Window 在本地 Prompt/请求体完成后、Transport 真正
  发生前运行同步双 TCP 连通门（两连两关连续成功才放行；失败按 3s 起、
  2 倍增长、300s 封顶原地等待）；门等待不改变 Job 状态、不消耗重试、
  不推进 cursor，且不计入 duration_ms；
- 当前可见 Window 全部完成后把 Job 标记为 succeeded；
- 预处理 / 模型 / 响应级失败不提交失败页、cursor 不前移，交给冻结 Q01
  收敛为 retry_wait 或达到次数上限后的 failed；
- 不实现 watch loop、daemon、进程锁、全局网络状态机、Hook、Reviewer、
  continuation Job 或第二套 cursor/状态。

本模块不发起真实模型调用；transport 由调用方注入（测试与 Audit 使用
Fake Transport；TCP 门使用 Fake Connector/Fake Sleep）。只使用标准库。
"""

from __future__ import annotations

import json
import socket
import sqlite3
import time
import urllib.parse
from typing import Any, Callable

from .codex_normalizer import NormalizationError
from .contracts import Window
from .extraction_core import ExtractionCoreError, RuntimeConfig, extract_window
from .job_queue import (
    claim_due_job,
    initialize_job_queue,
    mark_retryable_failure,
    mark_succeeded,
    recover_running,
)
from .preparation import prepare_codex_capture
from .window_commit import commit_window_result, initialize_database

__all__ = [
    "initialize_worker_database",
    "recover_interrupted_jobs",
    "run_once",
]

#: 固定安全错误码：仅当冻结 Commit Core 或 cursor 查询自身 SQLite 失败时使用。
DATABASE_ERROR = "database_error"

# ChatGPT web-export Captures use this explicit source-session prefix.  They
# are historical material: retain generated records for later review, but do
# not place them in the default active-recall set.
_CHATGPT_WEB_SOURCE_SESSION_PREFIX = "chatgpt-web-v1:"

#: Window Run 身份固定前缀（冻结规则，见 HANDOFF 第 8 节）。
_WINDOW_RUN_ID_PREFIX = "w01"

#: W01-P01 双 TCP 门冻结参数：单次连接 timeout、首失败 sleep、2 倍退避、300s 封顶。
_TCP_CONNECT_TIMEOUT_SECONDS = 3.0
_TCP_INITIAL_DELAY_SECONDS = 3.0
_TCP_BACKOFF_MULTIPLIER = 2.0
_TCP_MAX_DELAY_SECONDS = 300.0


def _record_status_for_source_session(source_session_id: str) -> str:
    return (
        "archived"
        if source_session_id.startswith(_CHATGPT_WEB_SOURCE_SESSION_PREFIX)
        else "active"
    )


def _parse_tcp_target(base_url: str) -> tuple[str, int]:
    """用标准库机械解析 base_url 的 host/port；非法输入抛 ValueError 直接传播。

    - host 取 URL hostname（域名 / IPv4 / URL 形式 IPv6 都交给标准库）；
    - 显式端口优先；``https`` 无显式端口用 443，``http`` 用 80；
    - 不解析 DNS、不保存 IP、不打印或 Hash 完整 base_url；
    - 非 http/https、缺 host 或非法 port 属配置/编程错误：不翻译为断网、
      不进入无限等待。
    """
    parsed = urllib.parse.urlsplit(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported TCP gate scheme: {scheme}")
    host = parsed.hostname
    if not host:
        raise ValueError("missing TCP gate host")
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return host, port


def _default_tcp_connector(host: str, port: int, timeout: float) -> Any:
    """生产默认连接器：标准库 socket.create_connection，返回可关闭 socket。"""
    return socket.create_connection((host, port), timeout=timeout)


def _tcp_probe(host: str, port: int, connector: Callable[..., Any]) -> None:
    """单次 TCP 连接：连接成功立即关闭；OSError 由门循环处理。"""
    sock = connector(host, port, _TCP_CONNECT_TIMEOUT_SECONDS)
    sock.close()


def _tcp_gate(
    host: str,
    port: int,
    connector: Callable[..., Any],
    sleep: Callable[[float], None],
) -> None:
    """同步双 TCP 门：两连两关连续成功才放行；OSError 按冻结退避原地等待。

    一轮严格为：第一次独立连接成功即关闭；第二次独立连接成功即关闭；两次
    都成功才返回。任一次连接抛 OSError 即本轮失败：已成功建立/关闭的连接
    保持关闭，不执行本轮剩余连接，等待后从第一次连接重新开始。等待序列
    3、6、12、24、48、96、192、300、300……，等待次数不设上限。
    """
    delay = _TCP_INITIAL_DELAY_SECONDS
    while True:
        try:
            _tcp_probe(host, port, connector)
            _tcp_probe(host, port, connector)
            return
        except OSError:
            pass
        sleep(delay)
        delay = min(delay * _TCP_BACKOFF_MULTIPLIER, _TCP_MAX_DELAY_SECONDS)


def _make_before_transport(
    base_url: str,
    *,
    tcp_connector: Callable[..., Any] | None,
    sleep: Callable[[float], None] | None,
    gate_clock: dict[str, float | None],
) -> Callable[[], None]:
    """构造当前 Window 的窄 before_transport 回调（W01-P01）。

    回调由 Extraction Core 在 Prompt/请求体/URL 全部构造完成后、
    ``transport.post_json()`` 之前同步调用：解析 TCP 目标 -> 双 TCP 门 ->
    放行后记录内部计时起点，使 ``duration_ms`` 覆盖放行后的 Transport、
    响应解析与 Validator，不含本地准备与 TCP 等待。注入参数为 ``None``
    时使用真实 ``socket.create_connection`` / ``time.sleep``。
    """
    connector = _default_tcp_connector if tcp_connector is None else tcp_connector
    sleeper = time.sleep if sleep is None else sleep

    def before_transport() -> None:
        host, port = _parse_tcp_target(base_url)
        _tcp_gate(host, port, connector, sleeper)
        gate_clock["value"] = time.monotonic()

    return before_transport


def initialize_worker_database(database_path: str) -> None:
    """增量初始化：只调用冻结 Queue 初始化与冻结 Commit 表初始化。

    不建立任何新业务表，不删除、不重建已有表。
    """
    initialize_job_queue(database_path)
    initialize_database(database_path)


def recover_interrupted_jobs(
    database_path: str,
    *,
    now_epoch: int,
) -> list[dict]:
    """启动恢复：转调 Q01，把全部遗留 running 改为 retry_wait。

    独立于 `run_once()` 的进程启动动作，不得在每次 `run_once()` 中自动执行。
    """
    return recover_running(database_path, now_epoch=now_epoch)


def _read_consumed_through_line(database_path: str, source_session_id: str) -> int:
    """读取该 Session 已提交 cursor；不存在时视为 0。"""
    conn = sqlite3.connect(database_path)
    try:
        row = conn.execute(
            "SELECT consumed_through_line FROM session_cursor"
            " WHERE source_session_id = ?",
            (source_session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return 0
    return int(row[0])


def _make_window_run_id(
    job_id: str, source_session_id: str, window: Window
) -> str:
    """生成稳定的 Window Run 身份（冻结规则，字节级可复现）。

    同一 Job 对同一消费边界必须得到相同 run_id：这是 Commit Core 幂等
    重放的唯一键，不解析正文、不使用内容 Hash、不读时间。
    """
    return json.dumps(
        [_WINDOW_RUN_ID_PREFIX, job_id, source_session_id, window.consumed_through_line],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _duration_ms(start: float) -> int:
    """单调时钟测量的同步调用耗时，取非负整数毫秒。"""
    return max(0, int((time.monotonic() - start) * 1000))


def _retryable_result(
    job: dict[str, Any],
    error_code: str,
    windows_committed: int,
) -> dict[str, Any]:
    """Q01 收敛后的安全返回：retry_wait 或 failed，只报告安全字段。"""
    return {
        "status": job["state"],
        "job_id": job["job_id"],
        "error_code": error_code,
        "windows_committed": windows_committed,
    }


def _mark_retryable(
    database_path: str,
    job_id: str,
    error_code: str,
    now_epoch: int,
    retry_delay_seconds: int,
    windows_committed: int,
) -> dict[str, Any]:
    """调用一次冻结 Q01 mark_retryable_failure，由 Q01 决定 retry_wait/failed。"""
    job = mark_retryable_failure(
        database_path,
        job_id=job_id,
        error_code=error_code,
        now_epoch=now_epoch,
        retry_at_epoch=now_epoch + retry_delay_seconds,
    )
    return _retryable_result(job, error_code, windows_committed)


def _finish_succeeded(
    database_path: str,
    job_id: str,
    now_epoch: int,
    windows_committed: int,
) -> dict[str, Any]:
    """全部当前可见 Window 提交后调用一次冻结 mark_succeeded。"""
    mark_succeeded(database_path, job_id=job_id, now_epoch=now_epoch)
    return {
        "status": "succeeded",
        "job_id": job_id,
        "windows_committed": windows_committed,
    }


def run_once(
    database_path: str,
    *,
    now_epoch: int,
    runtime_config: RuntimeConfig | None = None,
    transport: Any | None = None,
    timeout_seconds: float = 300.0,
    retry_delay_seconds: int = 300,
    soft_target_chars: int = 60_000,
    commit_clock: Callable[[], str] | None = None,
    id_factory: Callable[[], str] | None = None,
    tcp_connector: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """单次串行编排：领取一个到期 Job 并处理到当前可见边界或首次失败。

    没有到期 Job 时立即返回 ``{"status": "idle"}``，不等待、不 sleep。

    W01-P01：``tcp_connector`` / ``sleep`` 是测试注入参数，默认 ``None``
    时每个非空 Window 在模型调用前使用真实 ``socket.create_connection``
    与 ``time.sleep`` 执行同步双 TCP 门。空 Job / 空后缀不读模型环境、
    不解析网络目标、不连接、不 sleep。
    """
    job = claim_due_job(database_path, now_epoch=now_epoch)
    if job is None:
        return {"status": "idle"}

    job_id = job["job_id"]
    source_session_id = job["source_session_id"]
    windows_committed = 0

    # 1. 已提交 cursor + 1 为预处理起点；cursor 查询失败按 database_error 收敛。
    try:
        consumed_through_line = _read_consumed_through_line(
            database_path, source_session_id
        )
    except sqlite3.Error:
        return _mark_retryable(
            database_path, job_id, DATABASE_ERROR, now_epoch,
            retry_delay_seconds, windows_committed,
        )
    start_line = consumed_through_line + 1

    # 2. 预处理冻结 Capture；解析/读数失败不推进 cursor。
    try:
        windows = prepare_codex_capture(
            job["capture_path"],
            start_line=start_line,
            soft_target_chars=soft_target_chars,
        )
    except NormalizationError as exc:
        return _mark_retryable(
            database_path, job_id, exc.kind, now_epoch,
            retry_delay_seconds, windows_committed,
        )
    except OSError:
        return _mark_retryable(
            database_path, job_id, "capture_unavailable", now_epoch,
            retry_delay_seconds, windows_committed,
        )

    # 3. 无可见消息：不调用模型、不提交 Window，直接 succeeded。
    if not windows:
        return _finish_succeeded(database_path, job_id, now_epoch, windows_committed)

    # 4. 严格按返回顺序逐个 Window 串行处理。
    for window in windows:
        # 未提供 runtime_config 时，只在第一个非空 Window 即将调用模型前
        # 执行一次 from_env；空 Job 不读模型环境。
        if runtime_config is None:
            try:
                runtime_config = RuntimeConfig.from_env()
            except ExtractionCoreError as exc:
                return _mark_retryable(
                    database_path, job_id, exc.code, now_epoch,
                    retry_delay_seconds, windows_committed,
                )

        window_run_id = _make_window_run_id(job_id, source_session_id, window)

        # 5. 每页最多一次 Transport 调用；失败页不提交、不推进 cursor。
        #    W01-P01：本地 Prompt/请求体在 extract_window 内先完成，随后
        #    双 TCP 门放行，才进入真实传输计时并调用 Transport。门放行前
        #    的本地准备与 TCP 等待不计入 duration_ms。
        started = time.monotonic()
        gate_clock: dict[str, float | None] = {"value": None}
        before_transport = _make_before_transport(
            runtime_config.base_url,
            tcp_connector=tcp_connector,
            sleep=sleep,
            gate_clock=gate_clock,
        )
        try:
            result = extract_window(
                window,
                runtime_config,
                transport=transport,
                timeout_seconds=timeout_seconds,
                before_transport=before_transport,
            )
        except ExtractionCoreError as exc:
            return _mark_retryable(
                database_path, job_id, exc.code, now_epoch,
                retry_delay_seconds, windows_committed,
            )
        # 门放行后由 before_transport 记录内部计时起点；防御性回退到本次
        # 迭代起点（正常路径下 gate_clock 必然已填充）。
        duration_start = gate_clock["value"]
        if duration_start is None:
            duration_start = started
        duration_ms = _duration_ms(duration_start)

        validation = result["validation"]

        # 6. 响应级失败不得调用 commit。
        if validation.get("response_errors"):
            return _mark_retryable(
                database_path, job_id, "response_not_committable", now_epoch,
                retry_delay_seconds, windows_committed,
            )

        # 7. 响应级成功（mixed 与 all-invalid 都属于成功）原子提交；
        #    提交成功后才进入下一 Window。
        try:
            commit_window_result(
                database_path,
                project_id=job["project_id"],
                source_session_id=source_session_id,
                window_run_id=window_run_id,
                window=window,
                validation=validation,
                model=runtime_config.model,
                reasoning_effort=runtime_config.reasoning_effort,
                usage=result.get("usage"),
                duration_ms=duration_ms,
                record_status=_record_status_for_source_session(source_session_id),
                clock=commit_clock,
                id_factory=id_factory,
            )
        except sqlite3.Error:
            return _mark_retryable(
                database_path, job_id, DATABASE_ERROR, now_epoch,
                retry_delay_seconds, windows_committed,
            )
        windows_committed += 1

    # 8. 全部当前可见 Window 提交成功后才标记 succeeded。
    return _finish_succeeded(database_path, job_id, now_epoch, windows_committed)
