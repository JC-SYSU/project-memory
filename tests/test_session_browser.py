"""Session browser: fresh raw transcripts only, visible text only, honest gaps."""

from __future__ import annotations

import json
import os
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

    def test_cc_queued_prompt_is_a_user_message(self) -> None:
        p = str(self.project)

        def queued(mode: str, text: str) -> dict:
            return {"type": "attachment", "timestamp": TS, "cwd": p, "isSidechain": False,
                    "attachment": {"type": "queued_command", "commandMode": mode, "prompt": text}}

        _dump(self._cc_dir(self.project) / "cccccccc-cccc-cccc-cccc-cccccccccccc.jsonl",
              [cc_user("开始", p), cc_assistant("在做", p),
               queued("prompt", "等等，别删那一条"),
               queued("task-notification", "<task-notification>x</task-notification>"),
               cc_assistant("好的，恢复", p)])
        page = sb.read_session(p, "cccccccc")
        self.assertEqual([(m["role"], m["text"]) for m in page["messages"]], [
            ("user", "开始"), ("assistant", "在做"), ("user", "等等，别删那一条"), ("assistant", "好的，恢复"),
        ])

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


class SessionExportTests(SessionBrowserTests):
    """Export to a temp file: small sessions whole, large ones flagged with chunk ranges."""

    def setUp(self) -> None:
        super().setUp()
        from memory_palace import session_export as se
        self.se = se
        self.export_dir = self.home / "exports"
        self.dir_patch = mock.patch.object(se, "export_directory", lambda: str(self.export_dir))
        self.dir_patch.start()

    def tearDown(self) -> None:
        self.dir_patch.stop()
        super().tearDown()

    def _codex_session(self, sid: str, messages: list[dict]) -> None:
        _dump(self.home / f".codex/sessions/2026/11/03/rollout-2026-11-03T00-00-00-{sid}.jsonl",
              [codex_meta(sid, str(self.project))] + messages)

    def test_small_session_exports_whole_private_file(self) -> None:
        sid = "88888888-8888-8888-8888-888888888888"
        self._codex_session(sid, [codex_user("问题"), codex_assistant(f"图 data:image/png;base64,{B64}")])
        export = self.se.export_session(str(self.project), sid)
        text = Path(export.path).read_text(encoding="utf-8")
        self.assertFalse(export.exceeds_threshold)
        self.assertEqual(export.chunks, [])
        self.assertIn("## [0] 用户", text)
        self.assertIn("[image/png base64 已省略]", text)
        self.assertNotIn("iVBORw0KGgo", text)
        self.assertEqual(os.stat(export.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.export_dir).st_mode & 0o777, 0o700)

    def test_large_session_is_flagged_with_contiguous_chunks(self) -> None:
        sid = "99999999-9999-9999-9999-999999999999"
        body = "长" * 30_000  # about 21.6k estimated tokens each
        self._codex_session(sid, [codex_user(body) for _ in range(8)])
        export = self.se.export_session(str(self.project), sid)
        self.assertTrue(export.exceeds_threshold)
        lines = Path(export.path).read_text(encoding="utf-8").split("\n")
        chunks = export.chunks
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0]["first_message"], 0)
        self.assertEqual(chunks[-1]["last_message"], 7)
        for left, right in zip(chunks, chunks[1:]):
            self.assertEqual(left["last_line"] + 1, right["first_line"])
            self.assertEqual(left["last_message"] + 1, right["first_message"])
        for chunk in chunks:
            self.assertTrue(lines[chunk["first_line"] - 1].startswith(f"## [{chunk['first_message']}]"))
            self.assertLessEqual(chunk["estimated_tokens"], self.se.CHUNK_TARGET_TOKENS)

    def test_old_exports_are_swept(self) -> None:
        self.export_dir.mkdir(mode=0o700)
        stale = self.export_dir / "session-old.md"
        stale.write_text("x")
        os.utime(stale, (0, 0))
        keep = self.export_dir / "unrelated.txt"
        keep.write_text("x")
        os.utime(keep, (0, 0))
        sid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self._codex_session(sid, [codex_user("q")])
        self.se.export_session(str(self.project), sid)
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    def test_cross_project_lookup(self) -> None:
        other = self.home / "elsewhere"
        other.mkdir()
        sid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        _dump(self.home / f".codex/sessions/2026/11/04/rollout-2026-11-04T00-00-00-{sid}.jsonl.zst",
              [codex_meta(sid, str(other)), codex_user("别处")], zst=True)
        with self.assertRaisesRegex(sb.SessionBrowserError, "unknown_session"):
            self.se.export_session(str(self.project), sid)
        self.assertEqual(sb.locate_session_project(sid), str(other))
        self.assertEqual(Path(self.se.export_session(str(other), sid).path).exists(), True)

    def test_token_estimate_mixes_cjk_and_ascii(self) -> None:
        self.assertEqual(self.se.estimate_tokens("中" * 100), 72)
        self.assertEqual(self.se.estimate_tokens("a" * 100), 40)


if __name__ == "__main__":
    unittest.main()