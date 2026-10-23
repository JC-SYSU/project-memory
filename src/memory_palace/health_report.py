"""Read-only health report for memory-palace databases.

The report contract is fixed: five Markdown sections, zero values rendered as
「无」, library names relative to the scan root.  Every query opens the
database with ``mode=ro`` plus ``PRAGMA query_only``; this module never writes
to a database.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from .job_queue import JOB_STATES

__all__ = [
    "HealthReport",
    "LibraryHealth",
    "build_health_report",
    "discover_databases",
    "render_markdown",
]

STATE_DIRECTORY = ".memory-palace"
DATABASE_NAME = "memory-palace.sqlite3"
DEFAULT_MAX_DEPTH = 5
REVIEW_STATES = ("queued", "committed")
MAX_FAILED_DETAILS = 50

_DRAINER_MARKERS = (
    "memory_palace_worker_runner",
    "worker_once_loop",
    "memory-worker",
)
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True, slots=True)
class FailedJob:
    job_id: str
    attempts: int
    error_code: str
    updated_at: str


@dataclass(slots=True)
class LibraryHealth:
    name: str
    database_path: str
    extraction: dict[str, int] = field(default_factory=dict)
    review: dict[str, int] = field(default_factory=dict)
    failed_jobs: list[FailedJob] = field(default_factory=list)
    oldest_unfinished: str | None = None
    last_activity: str | None = None
    read_error: str | None = None


@dataclass(slots=True)
class HealthReport:
    generated_at: datetime
    root: Path
    max_depth: int
    libraries: list[LibraryHealth]
    drainer: str
    scan_errors: list[str] = field(default_factory=list)


def _read_only_uri(database_path: Path) -> str:
    return "file:" + quote(os.fspath(database_path), safe="/:") + "?mode=ro"


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(_read_only_uri(database_path), uri=True, timeout=1.0)
    connection.execute("PRAGMA query_only = ON")
    return connection


def discover_databases(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> list[Path]:
    """Return unique database files under *root* within the depth limit.

    ``max_depth`` counts the database file itself: root is depth 0, so the
    default 5 finds ``<root>/a/b/c/.memory-palace/memory-palace.sqlite3``.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"scan root is not a directory: {root}")

    found: dict[str, Path] = {}

    def ignore_walk_error(_: OSError) -> None:
        return None

    for current, directories, files in os.walk(
        root, followlinks=False, onerror=ignore_walk_error
    ):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            continue
        if current_path.name == STATE_DIRECTORY and DATABASE_NAME in files:
            if depth + 1 <= max_depth:
                database = current_path / DATABASE_NAME
                found.setdefault(os.path.realpath(database), database)
        if depth >= max_depth - 1:
            directories[:] = []
    return sorted(found.values(), key=os.fspath)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _state_counts(connection: sqlite3.Connection, table: str) -> dict[str, int]:
    rows = connection.execute(
        f"SELECT state, COUNT(*) FROM {table} GROUP BY state"
    ).fetchall()
    return {str(state): int(count) for state, count in rows}


def _failed_jobs(connection: sqlite3.Connection) -> list[FailedJob]:
    rows = connection.execute(
        "SELECT job_id, attempts, COALESCE(last_error_code, '-'), "
        "datetime(updated_at_epoch, 'unixepoch', 'localtime') "
        "FROM extraction_jobs WHERE state='failed' "
        "ORDER BY updated_at_epoch DESC"
    ).fetchall()
    return [
        FailedJob(
            job_id=str(job_id),
            attempts=int(attempts),
            error_code=str(error_code),
            updated_at=str(updated_at),
        )
        for job_id, attempts, error_code, updated_at in rows
    ]


def _format_epoch(value: object) -> str | None:
    try:
        return datetime.fromtimestamp(int(value)).strftime(_TIME_FORMAT)  # type: ignore[arg-type]
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _last_activity(connection: sqlite3.Connection) -> str | None:
    candidates: list[str] = []
    if _table_exists(connection, "records"):
        row = connection.execute(
            "SELECT datetime(MAX(julianday(captured_at)), 'localtime') FROM records"
        ).fetchone()
        if row is not None and row[0]:
            candidates.append(str(row[0]))
    if _table_exists(connection, "extraction_jobs"):
        row = connection.execute(
            "SELECT datetime(MAX(updated_at_epoch), 'unixepoch', 'localtime') "
            "FROM extraction_jobs"
        ).fetchone()
        if row is not None and row[0]:
            candidates.append(str(row[0]))
    return max(candidates) if candidates else None


def _library_name(database_path: Path, root: Path) -> str:
    project_directory = database_path.parent.parent
    try:
        relative = project_directory.relative_to(root)
    except ValueError:
        return os.fspath(project_directory)
    name = os.fspath(relative)
    return name if name else "."


def collect_library(database_path: Path, root: Path) -> LibraryHealth:
    """Collect one library; a read failure is recorded, never raised."""
    health = LibraryHealth(
        name=_library_name(database_path, root),
        database_path=os.fspath(database_path),
    )
    try:
        connection = _connect(database_path)
    except sqlite3.Error as exc:
        health.read_error = f"open failed: {exc}"
        return health

    try:
        if not _table_exists(connection, "extraction_jobs"):
            health.read_error = "missing extraction_jobs table"
            return health
        health.extraction = _state_counts(connection, "extraction_jobs")
        if _table_exists(connection, "review_items"):
            health.review = _state_counts(connection, "review_items")
        else:
            health.read_error = "missing review_items table"
        health.failed_jobs = _failed_jobs(connection)
        row = connection.execute(
            "SELECT MIN(queued_at_epoch) FROM extraction_jobs "
            "WHERE state IN ('queued', 'retry_wait', 'running')"
        ).fetchone()
        health.oldest_unfinished = (
            _format_epoch(row[0]) if row is not None and row[0] is not None else None
        )
        health.last_activity = _last_activity(connection)
    except sqlite3.Error as exc:
        health.read_error = f"query failed: {exc}"
    finally:
        connection.close()
    return health


def detect_drainer(
    probe: Callable[[], str | None] | None = None,
) -> str:
    """Return running / none / unknown without assuming a platform."""
    if probe is not None:
        result = probe()
        return result if result in ("running", "none", "unknown") else "unknown"
    try:
        completed = subprocess.run(
            ["ps", "-eo", "args="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    output = completed.stdout
    if not output.strip():
        return "unknown"
    return "running" if any(marker in output for marker in _DRAINER_MARKERS) else "none"


def build_health_report(
    root: Path | str | None = None,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    now: datetime | None = None,
    drainer_probe: Callable[[], str | None] | None = None,
) -> HealthReport:
    root_path = Path(root).expanduser().resolve() if root is not None else Path.home().resolve()
    databases = discover_databases(root_path, max_depth)
    libraries = [collect_library(database, root_path) for database in databases]
    return HealthReport(
        generated_at=now or datetime.now(),
        root=root_path,
        max_depth=max_depth,
        libraries=libraries,
        drainer=detect_drainer(drainer_probe),
        scan_errors=[lib.read_error for lib in libraries if lib.read_error],
    )


def _unknown_states(
    counts: Mapping[str, int], known: Iterable[str]
) -> dict[str, int]:
    known_set = set(known)
    return {state: count for state, count in counts.items() if state not in known_set}


def _totals(report: HealthReport) -> tuple[int, int, int]:
    total_failed = sum(lib.extraction.get("failed", 0) for lib in report.libraries)
    total_review_queued = sum(lib.review.get("queued", 0) for lib in report.libraries)
    unfinished = sum(
        lib.extraction.get(state, 0)
        for lib in report.libraries
        for state in ("queued", "running", "retry_wait")
    )
    return total_failed, total_review_queued, unfinished


def _attention_reason_text(report: HealthReport) -> str:
    total_failed, total_review_queued, unfinished = _totals(report)
    count_reasons: list[str] = []
    other_reasons: list[str] = []
    if total_failed:
        count_reasons.append(f"{total_failed} 个抽取任务失败")
    if total_review_queued:
        count_reasons.append(f"{total_review_queued} 条记录待人工审查")
    if unfinished and report.drainer != "running":
        if report.drainer == "unknown":
            other_reasons.append(f"{unfinished} 个任务在排队，后台抽取进程状态未知")
        else:
            other_reasons.append(f"{unfinished} 个任务在排队，但后台抽取进程没有运行")
    scan_error_count = sum(1 for lib in report.libraries if lib.read_error)
    if scan_error_count:
        other_reasons.append(f"{scan_error_count} 个库读取失败")
    unknown_library_count = sum(
        1
        for lib in report.libraries
        if _unknown_states(lib.extraction, JOB_STATES)
        or _unknown_states(lib.review, REVIEW_STATES)
    )
    if unknown_library_count:
        other_reasons.append(f"{unknown_library_count} 个库出现未知状态")
    parts: list[str] = []
    if count_reasons:
        parts.append("，".join(count_reasons))
    parts.extend(other_reasons)
    return "；".join(parts)


def _table_with_header(header: str, divider: str, rows: list[str], empty: str) -> str:
    lines = [header, divider]
    lines.extend(rows if rows else [empty])
    return "\n".join(lines)


def _drainer_label(value: str) -> str:
    return {"running": "运行中", "none": "未运行", "unknown": "状态未知"}.get(
        value, "状态未知"
    )


def _oldest_unfinished(report: HealthReport) -> str | None:
    values = [lib.oldest_unfinished for lib in report.libraries if lib.oldest_unfinished]
    return min(values) if values else None


def _wait_duration(oldest: str | None, now: datetime) -> str | None:
    if not oldest:
        return None
    try:
        started = datetime.strptime(oldest, _TIME_FORMAT)
    except ValueError:
        return None
    seconds = max(0, int((now - started).total_seconds()))
    if seconds >= 86400:
        days, remainder = divmod(seconds, 86400)
        hours = remainder // 3600
        return f"{days} 天 {hours} 小时"
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟"
    return "不足 1 分钟"


def _action_sentence(report: HealthReport) -> str:
    total_failed, total_review_queued, unfinished = _totals(report)
    if total_failed:
        return "先看失败明细；修复后重新处理这些任务。"
    if total_review_queued:
        return "处理待人工审查的记录。"
    if unfinished and report.drainer != "running":
        return "启动后台抽取进程，或检查它是否异常退出。"
    if any(lib.read_error for lib in report.libraries):
        return "检查无法读取的库和文件权限。"
    if any(
        _unknown_states(lib.extraction, JOB_STATES)
        or _unknown_states(lib.review, REVIEW_STATES)
        for lib in report.libraries
    ):
        return "有库出现未知状态，需核对数据库结构或版本。"
    return "无异常。"


def render_markdown(report: HealthReport) -> str:
    """Render the fixed five-section Markdown report."""
    reason_text = _attention_reason_text(report)
    attention = bool(reason_text)
    root_label = (
        "$HOME" if report.root == Path.home().resolve() else os.fspath(report.root)
    )

    extraction_total = sum(
        sum(lib.extraction.values()) for lib in report.libraries
    )
    review_total = sum(sum(lib.review.values()) for lib in report.libraries)

    extraction_counts = {
        state: sum(lib.extraction.get(state, 0) for lib in report.libraries)
        for state in JOB_STATES
    }
    review_counts = {
        state: sum(lib.review.get(state, 0) for lib in report.libraries)
        for state in REVIEW_STATES
    }

    extraction_rows: dict[str, int] = {}
    review_rows: dict[str, int] = {}
    for lib in report.libraries:
        for state, count in _unknown_states(lib.extraction, JOB_STATES).items():
            extraction_rows[state] = extraction_rows.get(state, 0) + count
        for state, count in _unknown_states(lib.review, REVIEW_STATES).items():
            review_rows[state] = review_rows.get(state, 0) + count

    lines: list[str] = [
        f"## 记忆宫殿健康报告 — {report.generated_at.strftime('%Y-%m-%d %H:%M')}",
        "",
        (
            "**结论：正常**"
            if not reason_text
            else f"**结论：需处理**（{reason_text}）"
        ),
        "",
        f"扫描：`{root_label}` {report.max_depth} 层内命中的 "
        f"`.memory-palace/memory-palace.sqlite3` 共 {len(report.libraries)} 个库，"
        "只读查询，未改动任何数据。",
        "",
        "### 1. 抽取队列",
        "",
        "| 状态 | 含义 | 条数 |",
        "|---|---|---:|",
        f"| failed | 终态失败，需人工重新入队 | {extraction_counts['failed']} |",
        f"| queued | 已入队，等待领取 | {extraction_counts['queued']} |",
        f"| running | 正在抽取 | {extraction_counts['running']} |",
        f"| retry_wait | 失败后等待重试 | {extraction_counts['retry_wait']} |",
        f"| succeeded | 已入库（累计） | {extraction_counts['succeeded']} |",
    ]
    lines.extend(
        f"| {state} | 未知状态（原样列出） | {count} |"
        for state, count in sorted(extraction_rows.items())
    )
    lines.extend([f"| **合计** | | **{extraction_total}** |", "", "### 2. 审查队列", ""])
    lines.extend(
        [
            "| 状态 | 含义 | 条数 |",
            "|---|---|---:|",
            f"| queued | 待人工审查 | {review_counts['queued']} |",
            f"| committed | 已审查入库 | {review_counts['committed']} |",
        ]
    )
    lines.extend(
        f"| {state} | 未知状态（原样列出） | {count} |"
        for state, count in sorted(review_rows.items())
    )
    lines.extend([f"| **合计** | | **{review_total}** |", "", "### 3. 需要处理的库", ""])

    problem_libraries = [
        lib
        for lib in report.libraries
        if lib.extraction.get("failed", 0)
        or lib.extraction.get("retry_wait", 0)
        or lib.extraction.get("queued", 0)
        or lib.extraction.get("running", 0)
        or lib.review.get("queued", 0)
    ]
    problem_libraries.sort(
        key=lambda lib: (
            -lib.extraction.get("failed", 0),
            -lib.extraction.get("retry_wait", 0),
            -lib.review.get("queued", 0),
            -lib.extraction.get("queued", 0),
            -lib.extraction.get("running", 0),
            lib.name,
        )
    )
    problem_rows = [
        "| {name} | {failed} | {retry_wait} | {queued} | {running} | {review} | {last} |".format(
            name=lib.name,
            failed=lib.extraction.get("failed", 0),
            retry_wait=lib.extraction.get("retry_wait", 0),
            queued=lib.extraction.get("queued", 0),
            running=lib.extraction.get("running", 0),
            review=lib.review.get("queued", 0),
            last=lib.last_activity or "-",
        )
        for lib in problem_libraries
    ]
    lines.append(
        _table_with_header(
            "| 库 | failed | retry_wait | queued | running | 待审 | 最近活动 |",
            "|---|---:|---:|---:|---:|---:|---|",
            problem_rows,
            "| 无 | | | | | | |",
        )
    )
    lines.extend(["", "### 4. 失败明细", ""])

    failed_rows: list[tuple[str, FailedJob]] = [
        (lib.name, job) for lib in report.libraries for job in lib.failed_jobs
    ]
    failed_rows.sort(key=lambda item: (item[1].updated_at, item[0]), reverse=True)
    shown_failed = failed_rows[:MAX_FAILED_DETAILS]
    failed_table_rows = [
        f"| {name} | {job.job_id} | {job.error_code} | {job.attempts} | {job.updated_at} |"
        for name, job in shown_failed
    ]
    lines.append(
        _table_with_header(
            "| 库 | job_id | 错误码 | attempts | 最后更新 |",
            "|---|---|---|---:|---|",
            failed_table_rows,
            "| 无 | | | | |",
        )
    )
    if len(failed_rows) > MAX_FAILED_DETAILS:
        lines.extend(
            [
                "",
                f"（仅显示前 {MAX_FAILED_DETAILS} 条，共 {len(failed_rows)} 个失败任务。）",
            ]
        )

    if attention:
        oldest = _oldest_unfinished(report)
        wait = _wait_duration(oldest, report.generated_at)
        lines.extend(["", "### 5. 备注", ""])
        if oldest:
            lines.append(
                f"- 后台抽取进程：{_drainer_label(report.drainer)}；"
                f"最老未完成任务：{oldest}（已等待 {wait or '时长未知'}）。"
            )
        else:
            lines.append(
                f"- 后台抽取进程：{_drainer_label(report.drainer)}；"
                "当前没有未完成任务。"
            )
        scan_errors = [
            f"{lib.name} 库（{lib.read_error}）"
            for lib in report.libraries
            if lib.read_error
        ]
        if scan_errors:
            lines.append(f"- 读取失败：{'；'.join(scan_errors)}。")
        unknown_lines: list[str] = []
        for lib in report.libraries:
            for state, count in sorted(
                _unknown_states(lib.extraction, JOB_STATES).items()
            ):
                unknown_lines.append(
                    f"{lib.name} 库的抽取任务状态 {state}={count}"
                )
            for state, count in sorted(
                _unknown_states(lib.review, REVIEW_STATES).items()
            ):
                unknown_lines.append(
                    f"{lib.name} 库的审查条目状态 {state}={count}"
                )
        if unknown_lines:
            lines.append(f"- 未知状态：{'；'.join(unknown_lines)}。")
        lines.append(f"- {_action_sentence(report)}")

    return "\n".join(lines) + "\n"
