"""Regression tests for the read-only memory-palace health report."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.health_report import build_health_report, render_markdown
from memory_palace.worker_orchestrator import initialize_worker_database

NOW = datetime(2026, 10, 23, 12, 0)
BASE_EPOCH = int(NOW.timestamp())


def _add_job(
    database: Path,
    job_id: str,
    state: str,
    *,
    attempts: int = 0,
    updated: int = BASE_EPOCH,
    error: str | None = None,
) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO extraction_jobs "
            "(job_id, project_id, source_session_id, capture_path, state, "
            "attempts, queued_at_epoch, not_before_epoch, updated_at_epoch, "
            "last_error_code) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                "/tmp/project",
                "session",
                "/tmp/capture",
                state,
                attempts,
                BASE_EPOCH - 60,
                BASE_EPOCH - 60,
                updated,
                error,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _add_review(database: Path, item_id: str, state: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO review_items "
            "(review_item_id, project_id, source_session_id, window_run_id, "
            "candidate_index, raw_candidate_json, validation_errors_json, "
            "window_snapshot_json, state, attempts, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_id,
                "/tmp/project",
                "session",
                "window",
                0,
                "{}",
                "[]",
                "{}",
                state,
                0,
                "2026-10-23T00:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _add_record(database: Path, record_id: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO records "
            "(record_id, project_id, source_session_id, window_run_id, "
            "candidate_index, title, summary, status, captured_at, as_of) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                record_id,
                "/tmp/project",
                "session",
                "window",
                0,
                "title",
                "summary",
                "active",
                "2026-10-23T03:00:00+00:00",
                "2026-10-23T03:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _make_database(root: Path, name: str) -> Path:
    database = root / name / ".memory-palace" / "memory-palace.sqlite3"
    database.parent.mkdir(parents=True)
    initialize_worker_database(str(database))
    return database


class HealthReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _report(self, *, drainer: str = "none", max_depth: int = 5):
        return build_health_report(
            self.root,
            max_depth=max_depth,
            now=NOW,
            drainer_probe=lambda: drainer,
        )

    def test_normal_report(self) -> None:
        database = _make_database(self.root, "clean")
        _add_job(database, "ok-1", "succeeded")
        _add_job(database, "ok-2", "succeeded")
        _add_review(database, "review-1", "committed")
        _add_record(database, "record-1")

        text = render_markdown(self._report())
        self.assertIn("**结论：正常**", text)
        self.assertNotIn("### 5. 备注", text)
        self.assertIn("| succeeded | 已入库（累计） | 2 |", text)
        self.assertIn("| committed | 已审查入库 | 1 |", text)
        self.assertIn("| 无 | | | | | | |", text)

    def test_problem_report_uses_plain_language(self) -> None:
        database = _make_database(self.root, "problem")
        _add_job(database, "fail-1", "failed", attempts=2, error="http_non_2xx")
        _add_job(database, "fail-2", "failed", attempts=1, error="transport_error")
        _add_job(database, "retry-1", "retry_wait")
        _add_job(database, "queued-1", "queued")
        _add_job(database, "unknown-1", "paused")
        _add_review(database, "pending-1", "queued")
        _add_record(database, "record-1")

        missing = _make_database(self.root, "missing")
        missing.unlink()
        sqlite3.connect(str(missing)).close()

        text = render_markdown(self._report())
        self.assertIn(
            "**结论：需处理**（2 个抽取任务失败，1 条记录待人工审查；"
            "2 个任务在排队，但后台抽取进程没有运行；1 个库读取失败；"
            "1 个库出现未知状态）",
            text,
        )
        self.assertIn("| problem | 2 | 1 | 1 | 0 | 1 |", text)
        self.assertIn("| problem | fail-1 | http_non_2xx | 2 |", text)
        self.assertIn("| paused | 未知状态（原样列出） | 1 |", text)
        self.assertIn("- 后台抽取进程：未运行；最老未完成任务：", text)
        self.assertIn(
            "- 读取失败：missing 库（missing extraction_jobs table）。", text
        )
        self.assertIn(
            "- 未知状态：problem 库的抽取任务状态 paused=1。", text
        )
        self.assertIn("- 先看失败明细；修复后重新处理这些任务。", text)

    def test_paths_with_spaces_and_chinese(self) -> None:
        name = "dir with spaces/中文项目"
        database = _make_database(self.root, name)
        _add_job(database, "queued-1", "queued")

        report = self._report()
        self.assertIn(name, [library.name for library in report.libraries])
        self.assertIn(f"| {name} |", render_markdown(report))

    def test_depth_limit(self) -> None:
        deep = _make_database(self.root, "a/b/c/d/e")
        _add_job(deep, "deep-1", "succeeded")

        shallow_names = [library.name for library in self._report(max_depth=5).libraries]
        deep_names = [library.name for library in self._report(max_depth=8).libraries]
        self.assertNotIn("a/b/c/d/e", shallow_names)
        self.assertIn("a/b/c/d/e", deep_names)

    def test_read_only(self) -> None:
        database = _make_database(self.root, "clean")
        _add_job(database, "ok-1", "succeeded")
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        self._report()
        after = hashlib.sha256(database.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_drainer_judgment(self) -> None:
        database = _make_database(self.root, "queued")
        _add_job(database, "queued-1", "queued")

        self.assertIn("**结论：正常**", render_markdown(self._report(drainer="running")))
        self.assertIn(
            "1 个任务在排队，但后台抽取进程没有运行",
            render_markdown(self._report(drainer="none")),
        )
        self.assertIn(
            "1 个任务在排队，后台抽取进程状态未知",
            render_markdown(self._report(drainer="unknown")),
        )


if __name__ == "__main__":
    unittest.main()
