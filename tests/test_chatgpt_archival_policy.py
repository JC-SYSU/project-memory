"""Regression tests for quarantining ChatGPT web-export memories."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.candidate_validator import validate_response  # noqa: E402
from memory_palace.contracts import NormalizedMessage, Window  # noqa: E402
from memory_palace.memory_recall import recall_memories  # noqa: E402
from memory_palace.memory_recall_or_groups import (  # noqa: E402
    recall_memories_or_groups,
)
from memory_palace.memory_recall_query import query_memory_recall  # noqa: E402
from memory_palace.window_commit import (  # noqa: E402
    commit_window_result,
    initialize_database,
)
from memory_palace.worker_orchestrator import (  # noqa: E402
    _record_status_for_source_session,
)


class ChatGPTArchivalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self._tmp.name) / "memory.sqlite3")
        initialize_database(self.database)
        self._next_id = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _id(self) -> str:
        self._next_id += 1
        return f"test-id-{self._next_id}"

    def _commit(self, *, source_session_id: str, record_status: str, run_id: str) -> None:
        window = Window(
            messages=(
                NormalizedMessage(
                    source_line=1,
                    timestamp="2026-08-24T00:00:00+08:00",
                    role="user",
                    body="Journal homepage lookup API decision.",
                ),
            ),
            consumed_through_line=1,
            body_chars=37,
        )
        validation = validate_response(
            json.dumps(
                {
                    "candidates": [
                        {
                            "title": f"{record_status} journal homepage lookup",
                            "summary": "Journal homepage lookup API decision.",
                            "evidence_lines": [1],
                        }
                    ]
                }
            ),
            allowed_evidence_lines={1},
        )
        commit_window_result(
            self.database,
            project_id="project",
            source_session_id=source_session_id,
            window_run_id=run_id,
            window=window,
            validation=validation,
            model="test-model",
            reasoning_effort="low",
            usage=None,
            duration_ms=1,
            record_status=record_status,
            clock=lambda: "2026-08-24T00:00:01+08:00",
            id_factory=self._id,
        )

    def test_chatgpt_sessions_are_archived_and_default_recall_excludes_them(self) -> None:
        self.assertEqual(
            _record_status_for_source_session("chatgpt-web-v1:conversation"),
            "archived",
        )
        self.assertEqual(
            _record_status_for_source_session("codex-session"), "active"
        )
        self._commit(
            source_session_id="chatgpt-web-v1:conversation",
            record_status="archived",
            run_id="chatgpt-window",
        )
        self._commit(
            source_session_id="codex-session",
            record_status="active",
            run_id="codex-window",
        )

        with sqlite3.connect(self.database) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM records WHERE source_session_id = ?",
                    ("chatgpt-web-v1:conversation",),
                ).fetchone()[0],
                "archived",
            )

        default_results = recall_memories(
            self.database, project_id="project", query="journal homepage", limit=10
        )
        self.assertEqual([item["status"] for item in default_results], ["active"])
        self.assertEqual(
            [item["status"] for item in recall_memories(
                self.database,
                project_id="project",
                query="journal homepage",
                limit=10,
                status_scope="archived",
            )],
            ["archived"],
        )

        grouped = recall_memories_or_groups(
            self.database,
            project_id="project",
            query_groups=["journal", "homepage", "lookup", "api"],
        )
        self.assertEqual([item["status"] for item in grouped["results"]], ["active"])
        wrapped = query_memory_recall(
            self.database, project_id="project", query="journal homepage"
        )
        self.assertEqual([item["status"] for item in wrapped["results"]], ["active"])


if __name__ == "__main__":
    unittest.main()
