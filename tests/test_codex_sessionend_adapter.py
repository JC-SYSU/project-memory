"""codex_sessionend_adapter + codex_sessionend_hook_runner.

Covers payload validation, the frozen identity spec, the accept/duplicate/
ignored/conflict decisions, source-size-in-identity (resume => new job), and
the runner's fail-open behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.codex_sessionend_adapter import (  # noqa: E402
    SessionEndConflictError,
    SessionEndPayloadError,
    accept_codex_sessionend,
    validate_sessionend_payload,
)
from memory_palace.project_paths import ensure_project_state  # noqa: E402

SID = "019900aa-bandon-test-session"


def make_payload(project: Path, rollout: Path | None, **over: object) -> dict:
    p: dict = {
        "cwd": str(project),
        "hook_event_name": "SessionEnd",
        "reason": "other",
        "session_id": SID,
        "transcript_path": str(rollout) if rollout else None,
    }
    p.update(over)
    return p


class SessionEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pm-sessend-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "proj"
        self.project.mkdir()
        paths = ensure_project_state(str(self.project))
        self.project_id = paths.project_id
        self.db = paths.database_path
        self.captures = Path(paths.captures_directory)
        self.rollout = self.tmp / "rollout.jsonl"
        self.rollout.write_text('{"a":1}\n{"a":2}\n', encoding="utf-8")

    def accept(self, payload: dict):
        return accept_codex_sessionend(
            payload, {str(self.project): self.project_id}, self.captures, self.db,
            now_epoch=1_700_000_000,
        )

    # ── validation ────────────────────────────────────────────────────────
    def test_validation_rejects_bad_fields(self) -> None:
        base = make_payload(self.project, self.rollout)
        self.assertIs(validate_sessionend_payload(base), base)
        for mutate in (
            lambda p: p.pop("reason"),
            lambda p: p.update(reason=""),
            lambda p: p.update(reason=7),
            lambda p: p.update(hook_event_name="PreCompact"),
            lambda p: p.pop("transcript_path"),
            lambda p: p.update(transcript_path=""),
            lambda p: p.update(cwd="relative/path"),
        ):
            payload = make_payload(self.project, self.rollout)
            mutate(payload)
            with self.subTest(mutate=mutate), self.assertRaises(SessionEndPayloadError):
                validate_sessionend_payload(payload)
        # transcript_path 缺失键与显式 None 是不同输入；None 合法。
        payload = make_payload(self.project, None)
        self.assertIs(validate_sessionend_payload(payload), payload)

    # ── accept flow ───────────────────────────────────────────────────────
    def test_accept_freezes_capture_and_queues_job(self) -> None:
        receipt = self.accept(make_payload(self.project, self.rollout))
        self.assertEqual(receipt.disposition, "accepted")
        self.assertEqual(receipt.job_state, "queued")
        self.assertTrue(os.path.exists(receipt.capture_path))
        self.assertTrue(receipt.job_id.startswith("codex-sessionend-"))
        self.assertEqual(len(receipt.event_key), 64)

    def test_replay_is_duplicate(self) -> None:
        self.accept(make_payload(self.project, self.rollout))
        again = self.accept(make_payload(self.project, self.rollout))
        self.assertEqual(again.disposition, "duplicate")
        self.assertIsNone(again.byte_count)

    def test_grown_rollout_yields_second_job(self) -> None:
        first = self.accept(make_payload(self.project, self.rollout))
        with self.rollout.open("a", encoding="utf-8") as fh:
            fh.write('{"a":3}\n')
        second = self.accept(make_payload(self.project, self.rollout))
        self.assertEqual(second.disposition, "accepted")
        self.assertNotEqual(second.job_id, first.job_id)
        with closing(sqlite3.connect(self.db)) as conn:
            jobs = conn.execute(
                "SELECT count(*), count(DISTINCT job_id) FROM extraction_jobs"
            ).fetchone()
        self.assertEqual(jobs, (2, 2))

    def test_null_transcript_ignored_without_side_effects(self) -> None:
        receipt = self.accept(make_payload(self.project, None))
        self.assertEqual(
            (receipt.disposition, receipt.reason), ("ignored", "transcript_unavailable")
        )
        self.assertEqual(list(self.captures.iterdir()), [])

    def test_unbound_project_ignored(self) -> None:
        receipt = accept_codex_sessionend(
            make_payload(self.project, self.rollout), {}, self.captures, self.db,
            now_epoch=1_700_000_000,
        )
        self.assertEqual(
            (receipt.disposition, receipt.reason), ("ignored", "project_unbound")
        )
        self.assertEqual(list(self.captures.iterdir()), [])

    def test_capture_without_job_is_conflict(self) -> None:
        receipt = self.accept(make_payload(self.project, self.rollout))
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM extraction_jobs")
            conn.commit()  # 只剩 Capture，没有 Job
        with self.assertRaises(SessionEndConflictError):
            self.accept(make_payload(self.project, self.rollout))
        self.assertTrue(os.path.exists(receipt.capture_path))

    # ── runner fail-open ──────────────────────────────────────────────────
    def _run_runner(self, stdin: str, env_extra: dict) -> subprocess.CompletedProcess:
        env = {**os.environ, "PYTHONPATH": str(SRC), **env_extra}
        return subprocess.run(
            [sys.executable, "-B", str(SRC / "codex_sessionend_hook_runner.py")],
            input=stdin, capture_output=True, text=True, env=env,
        )

    def test_runner_end_to_end_and_fail_open(self) -> None:
        env_extra = {"CODEX_MEMORY_NO_DRAINER": "1"}
        ok = self._run_runner(json.dumps(make_payload(self.project, self.rollout)), env_extra)
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))
        replay = self._run_runner(json.dumps(make_payload(self.project, self.rollout)), env_extra)
        self.assertEqual(replay.returncode, 0)
        bad = self._run_runner("not json", env_extra)
        self.assertEqual(bad.returncode, 0)
        self.assertIn('"status":"error"', bad.stderr)
        with closing(sqlite3.connect(self.db)) as conn:
            (count,) = conn.execute("SELECT count(*) FROM extraction_jobs").fetchone()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
