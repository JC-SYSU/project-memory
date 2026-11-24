"""Drainer startup and ownership of the worker lock."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace_worker_runner import _run_loop, _try_process_lock


class WorkerRunnerTests(unittest.TestCase):
    def test_first_run_creates_lock_directory_and_processes_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "missing" / ".memory-palace" / "worker-drainer.lock"
            seen = []
            _run_loop(
                tmp, lock_path=str(lock),
                discover=lambda _: ["queue.db"] if not seen else [],
                recover=lambda *_args, **_kwargs: None,
                run=lambda path, **_kwargs: seen.append(path),
                sleep=lambda _: None,
            )
            self.assertEqual(seen, ["queue.db"])
            self.assertTrue(lock.is_file())

    def test_child_process_cannot_take_held_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = str(Path(tmp) / ".memory-palace" / "worker-drainer.lock")
            child = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from memory_palace_worker_runner import _try_process_lock\n"
                "with _try_process_lock(sys.argv[2]) as acquired: "
                "raise SystemExit(0 if acquired else 1)"
            )

            def child_status():
                result = subprocess.run(
                    [sys.executable, "-c", child, str(SRC), lock],
                    capture_output=True, text=True, timeout=15,
                )
                self.assertEqual(result.stderr, "")
                return result.returncode

            with _try_process_lock(lock) as acquired:
                self.assertTrue(acquired)
                self.assertEqual(child_status(), 1)
            self.assertEqual(child_status(), 0)
