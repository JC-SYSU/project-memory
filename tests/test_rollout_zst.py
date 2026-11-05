"""Codex-compressed rollout (.jsonl.zst) support across every raw-session read path."""

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

from memory_palace.capture_store import freeze_capture  # noqa: E402
from memory_palace.codex_normalizer import normalize_capture  # noqa: E402
from memory_palace.project_paths import (  # noqa: E402
    ProjectPathError,
    ensure_project_state,
    read_session_metadata,
)
from memory_palace.rollout_io import is_rollout_file_name, open_rollout_text  # noqa: E402
import memory_palace_worker_runner as runner  # noqa: E402

try:
    from compression import zstd as _stdlib_zstd  # type: ignore[import-not-found]
except ImportError:
    _stdlib_zstd = None
try:
    import zstandard
except ImportError:
    zstandard = None

TS = "2026-11-05T00:00:00.000Z"


def _compress(data: bytes) -> bytes:
    if _stdlib_zstd is not None:
        return _stdlib_zstd.compress(data)
    if zstandard is not None:
        return zstandard.ZstdCompressor().compress(data)
    raise unittest.SkipTest("no zstd encoder available")


def _rollout_bytes(session_id: str, cwd: str) -> bytes:
    records = [
        {"timestamp": TS, "type": "session_meta", "payload": {"id": session_id, "cwd": cwd}},
        {"timestamp": TS, "type": "event_msg", "payload": {"type": "item_completed",
         "item": {"type": "UserMessage", "content": [{"type": "text", "text": "中文问题"}]}}},
        {"timestamp": TS, "type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "回答"}]}},
    ]
    return ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode("utf-8")


class RolloutZstTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, rel: str, data: bytes) -> Path:
        path = self.base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_suffix_recognition(self) -> None:
        self.assertTrue(is_rollout_file_name("rollout-x.jsonl"))
        self.assertTrue(is_rollout_file_name("rollout-x.jsonl.zst"))
        self.assertFalse(is_rollout_file_name("rollout-x.jsonl.backup"))
        self.assertFalse(is_rollout_file_name("rollout-x.jsonl.zst.tmp"))

    def test_metadata_reads_identically_from_plain_and_zst(self) -> None:
        raw = _rollout_bytes("s1", "/tmp/project")
        plain = self._write("a/r.jsonl", raw)
        packed = self._write("b/r.jsonl.zst", _compress(raw))
        self.assertEqual(read_session_metadata(plain), read_session_metadata(packed))

    def test_corrupt_zst_is_a_project_path_error(self) -> None:
        bad = self._write("r.jsonl.zst", b"\x28\xb5\x2f\xfd not really zstd")
        with self.assertRaises(ProjectPathError):
            read_session_metadata(bad)

    def test_freeze_decompresses_and_capture_normalizes_like_plain(self) -> None:
        raw = _rollout_bytes("s1", "/tmp/project")
        packed = self._write("src/r.jsonl.zst", _compress(raw))
        plain = self._write("src/r.jsonl", raw)
        frozen = freeze_capture(packed, self.base / "cap-from-zst.jsonl")
        self.assertEqual(Path(frozen.path).read_bytes(), raw)
        self.assertEqual(frozen.byte_count, len(raw))
        self.assertEqual(normalize_capture(frozen.path), normalize_capture(plain))
        self.assertEqual([m.role for m in normalize_capture(packed)], ["user", "assistant"])

    def test_open_rollout_text_streams_all_lines(self) -> None:
        raw = _rollout_bytes("s1", "/tmp/project")
        packed = self._write("r.jsonl.zst", _compress(raw))
        with open_rollout_text(packed) as handle:
            self.assertEqual(handle.read(), raw.decode("utf-8"))

    def test_drainer_discovers_projects_from_zst_and_archived_sessions(self) -> None:
        live_project = self.base / "live"
        archived_project = self.base / "archived"
        for project in (live_project, archived_project):
            project.mkdir()
            ensure_project_state(str(project))
        codex = self.base / ".codex"
        self._write(".codex/sessions/2026/10/01/rollout-a.jsonl.zst",
                    _compress(_rollout_bytes("a", str(live_project))))
        self._write(".codex/archived_sessions/rollout-b.jsonl.zst",
                    _compress(_rollout_bytes("b", str(archived_project))))
        self._write(".codex/sessions/2026/10/01/rollout-a.jsonl.backup", b"ignored")
        found = runner.discover_project_databases(str(codex / "sessions"))
        self.assertEqual(sorted(found), sorted(
            str(p / ".memory-palace" / "memory-palace.sqlite3") for p in (live_project, archived_project)
        ))
        for path in found:
            sqlite3.connect(path).close()

    def test_non_codex_root_does_not_pull_in_siblings(self) -> None:
        project = self.base / "p"
        project.mkdir()
        ensure_project_state(str(project))
        self._write("mirror/x.jsonl", _rollout_bytes("x", str(project)))
        self._write("archived_sessions/y.jsonl", _rollout_bytes("y", str(self.base / "other")))
        self.assertEqual(runner._session_roots(str(self.base / "mirror")), [str(self.base / "mirror")])


if __name__ == "__main__":
    unittest.main()