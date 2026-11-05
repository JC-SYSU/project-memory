"""Session browser: fresh raw transcripts only, visible text only, honest gaps."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace import session_browser as sb  # noqa: E402
from memory_palace.project_paths import ensure_project_state  # noqa: E402
from memory_palace.visible_text import clean_assistant_text, clean_user_text  # noqa: E402

TS = "2026-11-05T01:00:00.000Z"
B64 = "iVBORw0KGgo" + "A" * 2000


def _dump(path: Path, records: list[dict], *, zst: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n").encode()
    if zst:
        try:
            from compression import zstd  # type: ignore[import-not-found]
            data = zstd.compress(data)
        except ImportError:
            import zstandard
            data = zstandard.ZstdCompressor().compress(data)
    path.write_bytes(data)


def codex_meta(sid: str, cwd: str) -> dict:
    return {"timestamp": TS, "type": "session_meta", "payload": {"id": sid, "cwd": cwd}}


def codex_user(text: str, ts: str = TS) -> dict:
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "item_completed",
            "item": {"type": "UserMessage", "content": [{"type": "text", "text": text}]}}}


def codex_assistant(text: str, ts: str = TS) -> dict:
    return {"timestamp": ts, "type": "response_item", "payload": {"type": "message",
            "role": "assistant", "content": [{"type": "output_text", "text": text}]}}


def cc_user(text: str, cwd: str) -> dict:
    return {"type": "user", "timestamp": TS, "cwd": cwd, "isSidechain": False,
            "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


def cc_assistant(text: str, cwd: str) -> dict:
    return {"type": "assistant", "timestamp": TS, "cwd": cwd, "isSidechain": False,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class VisibleTextTests(unittest.TestCase):
    def test_attachment_wrapper_unwraps_to_typed_request(self) -> None:
        text = "# Files mentioned by the user:\n\n## a.png: /tmp/a.png\n\n## My request for Codex:\n修一下这个\n"
        self.assertEqual(clean_user_text(text), "修一下这个\n")

    def test_attachment_list_without_request_is_dropped(self) -> None:
        self.assertIsNone(clean_user_text("# Files mentioned by the user:\n\n## a.png: /tmp/a.png\n"))

    def test_injected_blocks_and_base64_removed_from_user(self) -> None:
        text = (f'<image name=[Image #1] path="/tmp/x.png">data:image/png;base64,{B64}</image>'
                "<in-app-browser-context source=\"ambient-ui-state\">x</in-app-browser-context>看图")
        self.assertEqual(clean_user_text(text), "看图")

    def test_approval_and_internal_context_dropped(self) -> None:
        self.assertIsNone(clean_user_text("The following is the Codex agent history added since ..."))
        self.assertIsNone(clean_user_text("<codex_internal_context source=\"goal\">x</codex_internal_context>"))

    def test_assistant_prose_kept_payload_replaced(self) -> None:
        self.assertEqual(clean_assistant_text("用 `<system-reminder>` 标签"), "用 `<system-reminder>` 标签")
        self.assertEqual(clean_assistant_text(f"图：data:image/png;base64,{B64} 完"), "图：[image/png base64 已省略] 完")
        self.assertEqual(clean_assistant_text(f"x {B64} y"), "x [base64 已省略] y")


class SessionBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.project = self.home / "work" / "my.proj"
        self.project.mkdir(parents=True)
        self.patch = mock.patch.object(sb, "_home", lambda: str(self.home))
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.tmp.cleanup()

    def _cc_dir(self, cwd: Path) -> Path:
        return self.home / ".claude" / "projects" / sb.re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    def test_lists_both_clients_newest_first_with_clean_text(self) -> None:
        p = str(self.project)
        _dump(self.home / ".codex/sessions/2026/11/01/rollout-2026-11-01T00-00-00-11111111-1111-1111-1111-111111111111.jsonl.zst",
              [codex_meta("11111111-1111-1111-1111-111111111111", p),
               {"timestamp": TS, "type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "# AGENTS.md instructions\n<INSTRUCTIONS>x"}]}},
               codex_user("Codex 里的问题", "2026-11-01T00:00:00Z"),
               codex_assistant("Codex 的回答", "2026-11-01T00:00:01Z")], zst=True)
        _dump(self._cc_dir(self.project) / "22222222-2222-2222-2222-222222222222.jsonl",
              [cc_user("<command-name>/clear</command-name>", p), cc_user("CC 里的问题", p),
               {"type": "user", "timestamp": TS, "cwd": p, "toolUseResult": {}, "isSidechain": False,
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "ls output"}]}},
               cc_assistant("CC 的回答", p)])
        sessions = sb.list_sessions(p)
        self.assertEqual([(s.client, s.title, s.message_count) for s in sessions],
                         [("claude-code", "CC 里的问题", 2), ("codex", "Codex 里的问题", 2)])
        page = sb.read_session(p, "11111111")
        self.assertEqual([(m["role"], m["text"]) for m in page["messages"]],
                         [("user", "Codex 里的问题"), ("assistant", "Codex 的回答")])

    def test_cc_directory_collision_is_resolved_by_recorded_cwd(self) -> None:
        other = self.home / "work" / "my-proj"  # encodes to the same directory name
        _dump(self._cc_dir(self.project) / "33333333-3333-3333-3333-333333333333.jsonl",
              [cc_user("别的项目", str(other)), cc_assistant("x", str(other))])
        self.assertEqual(sb.list_sessions(str(self.project)), [])

    def test_codex_fork_segments_merge_in_time_order(self) -> None:
        p, sid = str(self.project), "44444444-4444-4444-4444-444444444444"
        base = self.home / ".codex/sessions/2026/10"
        _dump(base / f"28/rollout-2026-10-28T09-00-00-{sid}.jsonl",
              [codex_meta(sid, p), codex_user("第一段", "2026-10-28T01:00:00Z")])
        _dump(base / f"29/rollout-2026-10-29T09-00-00-{sid}_55555555-5555-5555-5555-555555555555.jsonl.zst",
              [codex_meta(sid, p), codex_user("第二段", "2026-10-29T01:00:00Z")], zst=True)
        [info] = sb.list_sessions(p)
        self.assertEqual(len(info.files), 2)
        texts = [m["text"] for m in sb.read_session(p, sid)["messages"]]
        self.assertEqual(texts, ["第一段", "第二段"])

    def test_missing_raw_transcript_is_reported_never_rebuilt(self) -> None:
        p = str(self.project)
        paths = ensure_project_state(p)
        capture = Path(paths.captures_directory) / "c.jsonl"
        _dump(capture, [codex_meta("66666666-6666-6666-6666-666666666666", p), codex_user("快照里的话")])
        conn = sqlite3.connect(paths.database_path)
        conn.execute("INSERT INTO extraction_jobs (job_id, project_id, source_session_id, capture_path, state,"
                     " attempts, queued_at_epoch, not_before_epoch, updated_at_epoch) VALUES"
                     " ('j', ?, '66666666-6666-6666-6666-666666666666', ?, 'succeeded', 1, 0, 0, 0)",
                     (p, str(capture)))
        conn.commit()
        conn.close()
        [info] = sb.list_sessions(p)
        self.assertFalse(info.available)
        self.assertEqual(info.unavailable_reason, "raw_transcript_missing")
        with self.assertRaisesRegex(sb.SessionBrowserError, "session_unavailable"):
            sb.read_session(p, "66666666")

    def test_paging_by_chars_and_oversized_message(self) -> None:
        p, sid = str(self.project), "77777777-7777-7777-7777-777777777777"
        _dump(self.home / f".codex/sessions/2026/11/02/rollout-2026-11-02T00-00-00-{sid}.jsonl",
              [codex_meta(sid, p)] + [codex_user(f"{i}" * 10) for i in range(5)] + [codex_assistant("长" * 50)])
        first = sb.read_session(p, sid, max_chars=25)
        self.assertEqual([m["index"] for m in first["messages"]], [0, 1])
        self.assertEqual(first["next_offset"], 2)
        last = sb.read_session(p, sid, offset=5, max_chars=25)
        self.assertTrue(last["truncated_message"])
        self.assertEqual(len(last["messages"][0]["text"]), 25)
        self.assertIsNone(last["next_offset"])

    def test_id_resolution_errors(self) -> None:
        with self.assertRaisesRegex(sb.SessionBrowserError, "unknown_session"):
            sb.read_session(str(self.project), "deadbeef")
        with self.assertRaisesRegex(sb.SessionBrowserError, "invalid_argument"):
            sb.read_session(str(self.project), "../../etc/passwd")
        with self.assertRaisesRegex(sb.SessionBrowserError, "unknown_session"):
            sb.read_session(str(self.project), "1234")  # short prefixes never auto-resolve


if __name__ == "__main__":
    unittest.main()