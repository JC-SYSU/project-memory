"""A project-local frozen Capture survives the loss of a directory alias."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codex_precompact_hook_runner import process_payload
from memory_palace.project_paths import project_paths_for_cwd
from memory_palace.worker_orchestrator import run_once


class CapturePathAliasTests(unittest.TestCase):
    def run_after_removing_alias(self, *, alter_session: bool = False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            alias = root / "alias"
            if os.name == "nt":
                subprocess.run(
                    ["cmd.exe", "/d", "/c", "mklink", "/j", str(alias), str(project)],
                    capture_output=True, check=True, timeout=10,
                )
            else:
                alias.symlink_to(project, target_is_directory=True)

            rollout = root / "rollout.jsonl"

            def stub(session_id):
                return json.dumps({
                    "type": "session_meta",
                    "payload": {"id": session_id, "cwd": str(alias)},
                }) + "\n"

            rollout.write_text(stub("alias-test"), encoding="utf-8")
            payload = {
                "session_id": "alias-test", "cwd": str(alias),
                "hook_event_name": "PreCompact", "model": "fixture",
                "turn_id": "0", "trigger": "manual", "transcript_path": str(rollout),
            }
            with mock.patch.dict(os.environ, {"CODEX_MEMORY_NO_DRAINER": "1"}):
                self.assertEqual(process_payload(payload, clock=lambda: 123), "accepted")

            if os.name == "nt":
                alias.rmdir()  # Removes the junction, never the target.
            else:
                alias.unlink()

            paths = project_paths_for_cwd(str(project))
            capture, = Path(paths.captures_directory).glob("*.jsonl")
            self.assertTrue(capture.is_file())
            if alter_session:
                capture.write_text(stub("not-the-queued-session"), encoding="utf-8")
            # Empty visible transcript: pre-processing only, never an upstream request.
            return run_once(paths.database_path, now_epoch=123)

    def test_worker_finds_capture_via_the_database_when_the_alias_disappears(self):
        self.assertEqual(self.run_after_removing_alias()["status"], "succeeded")

    def test_worker_does_not_substitute_another_sessions_capture(self):
        result = self.run_after_removing_alias(alter_session=True)
        self.assertEqual(result["status"], "retry_wait")
        self.assertEqual(result["error_code"], "capture_unavailable")
