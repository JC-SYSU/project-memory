"""Offline tests for the CC→Palace capture intake (P0 acceptance)."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cc_capture_hook_runner as runner  # noqa: E402
from cc_transcript_compiler import (  # noqa: E402
    CompileError,
    compile_visible_messages,
)

# 准入门会拒绝 /tmp、/private/、/var/ 下的 cwd —— 测试沙盒必须住在放行区（home 下）
SANDBOX_BASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".sandboxes"
)
os.makedirs(SANDBOX_BASE, exist_ok=True)


def user_line(text, **extra):
    base = {
        "type": "user",
        "timestamp": "2026-09-06T09:00:00.000Z",
        "message": {"role": "user", "content": text},
        "origin": {"kind": "human"},
        "promptSource": "typed",
        "isSidechain": False,
    }
    base.update(extra)
    return json.dumps(base, ensure_ascii=False)


def tool_result_line():
    return json.dumps(
        {
            "type": "user",
            "timestamp": "2026-09-06T09:00:01.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}],
            },
            "isSidechain": False,
        }
    )


def assistant_line(blocks, ts="2026-09-06T09:00:02.000Z", sidechain=False):
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": ts,
            "message": {"role": "assistant", "content": blocks},
            "isSidechain": sidechain,
        }
    )


def write_transcript(directory: str, lines: list[str]) -> str:
    path = os.path.join(directory, "session.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


class CompilerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX_BASE)
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_whitelist_admits_only_visible_dialog(self):
        path = write_transcript(
            self.tmp,
            [
                user_line("真实问题"),
                tool_result_line(),
                user_line("技能注入", isMeta=True),
                json.dumps({"type": "user", "timestamp": "t", "message": {"content": [{"type": "text", "text": "非human"}]}}),
                assistant_line([{"type": "thinking", "thinking": "x"}]),
                assistant_line([{"type": "text", "text": "回答一"}]),
                assistant_line([{"type": "text", "text": "回答二"}], ts="2026-09-06T09:00:03.000Z"),
                assistant_line([{"type": "text", "text": "子代理"}], sidechain=True),
                json.dumps({"type": "system", "timestamp": "t", "subtype": "stop_hook_summary"}),
                json.dumps({"type": "attachment", "timestamp": "t", "attachment": {"type": "hook_success"}}),
            ],
        )
        messages = compile_visible_messages(path)
        self.assertEqual(
            messages,
            [
                ("2026-09-06T09:00:00.000Z", "user", "真实问题"),
                ("2026-09-06T09:00:02.000Z", "assistant", "回答一"),
                ("2026-09-06T09:00:03.000Z", "assistant", "回答二"),
            ],
        )

    def test_image_text_user_line_keeps_text_only(self):
        path = write_transcript(
            self.tmp,
            [
                json.dumps(
                    {
                        "type": "user",
                        "timestamp": "2026-09-06T09:00:00.000Z",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "看图"},
                                {"type": "image", "source": {"data": "…"}},
                            ],
                        },
                        "origin": {"kind": "human"},
                    }
                )
            ],
        )
        self.assertEqual(compile_visible_messages(path), [("2026-09-06T09:00:00.000Z", "user", "看图")])

    def test_command_wrappers_excluded(self):
        for prefix in ("<command-name>", "<command-message>", "<local-command-caveat>", "<local-command-stdout>", "<bash-input>"):
            path = write_transcript(self.tmp, [user_line(prefix + "/config</command-name>")])
            self.assertEqual(compile_visible_messages(path), [], prefix)

    def test_mid_text_tag_mentions_are_kept_verbatim(self):
        """标签出现在正文中段 = 对话在谈论它，属真实内容，一字不动。"""
        body = "帮我看看这段话：中间有 <command-message> 和 <system-reminder> 标签，算注入吗"
        path = write_transcript(self.tmp, [user_line(body)])
        self.assertEqual(compile_visible_messages(path)[0][2], body)

    def test_incomplete_tail_dropped_malformed_middle_raises(self):
        good = user_line("第一句")
        tail = write_transcript(self.tmp, [good, '{"type":"assistant","time'])
        self.assertEqual(len(compile_visible_messages(tail)), 1)
        bad = write_transcript(self.tmp, [good[:10], good])
        with self.assertRaises(CompileError):
            compile_visible_messages(bad)

    def test_prefix_stability_under_app_growth(self):
        first = write_transcript(self.tmp, [user_line("甲"), assistant_line([{"type": "text", "text": "乙"}])])
        before = compile_visible_messages(first)
        with open(first, "a", encoding="utf-8") as handle:
            handle.write(tool_result_line() + "\n" + user_line("丙") + "\n")
        after = compile_visible_messages(first)
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(after[-1][2], "丙")


class IdentityTests(unittest.TestCase):
    def test_identity_tracks_session_event_and_visible_count(self):
        a = runner._stable_identity("s1", "precompact:auto", 10)
        self.assertEqual(a, runner._stable_identity("s1", "precompact:auto", 10))
        self.assertNotEqual(a, runner._stable_identity("s1", "precompact:auto", 11))
        self.assertNotEqual(a, runner._stable_identity("s1", "sessionend:other", 10))
        self.assertEqual(a[1], f"claude-code-{a[0]}.jsonl")
        self.assertEqual(a[2], f"claude-code-{a[0]}")


class RunnerIntakeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX_BASE)
        self.project = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[dict[str, str]] = []
        self.slept: list[float] = []
        self.original_mirror = runner.MIRROR_ROOT
        runner.MIRROR_ROOT = os.path.join(self._tmp.name, "mirror")
        self.addCleanup(setattr, runner, "MIRROR_ROOT", self.original_mirror)

    def _payload(self, event="PreCompact", **extra) -> dict:
        payload = {
            "session_id": "cc-session-1",
            "transcript_path": self.transcript,
            "cwd": self.project,
            "hook_event_name": event,
        }
        if event == "PreCompact":
            payload["trigger"] = "auto"
        else:
            payload["reason"] = "other"
        payload.update(extra)
        return payload

    def _run(self, payload):
        return runner.process_payload(
            payload,
            clock=lambda: 1_757_000_000.0,
            sleep=self.slept.append,
            wake=self.calls.append,
            environ={},
        )

    def test_accepted_duplicate_and_growth(self):
        self.transcript = write_transcript(self.project, [user_line("问题"), assistant_line([{"type": "text", "text": "答"}])])
        self.assertEqual(self._run(self._payload()), "accepted")
        # 不可见工具行增长：可见数不变 → duplicate，不产生第二份 Capture
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(tool_result_line() + "\n")
        self.assertEqual(self._run(self._payload()), "duplicate")
        captures = os.listdir(os.path.join(self.project, ".memory-palace", "captures"))
        self.assertEqual(len(captures), 1)
        # 真实增长 → 新 Job
        with open(self.transcript, "a", encoding="utf-8") as handle:
            handle.write(user_line("再问") + "\n")
        self.assertEqual(self._run(self._payload(trigger="manual")), "accepted")
        captures = os.listdir(os.path.join(self.project, ".memory-palace", "captures"))
        self.assertEqual(len(captures), 2)
        # 队列两 Job、指针已写、两次唤醒 env 补齐
        import sqlite3

        conn = sqlite3.connect(os.path.join(self.project, ".memory-palace", "memory-palace.sqlite3"))
        states = [row[0] for row in conn.execute("SELECT state FROM extraction_jobs ORDER BY enqueue_seq")]
        conn.close()
        self.assertEqual(states, ["queued", "queued"])
        self.assertTrue(os.path.lexists(os.path.join(runner.MIRROR_ROOT, "cc-session-1.jsonl")))
        # accepted/duplicate/accepted 三次都唤醒（镜像 Codex runner：duplicate 也补唤醒）
        self.assertEqual(len(self.calls), 3)
        # 2026-09-10 裁决：env 文件退役，唤醒 env 原样继承调用方环境（environ={} → 原样透传）
        for env in self.calls:
            self.assertEqual(env, {})
        # 回读：Capture 可被宫殿正式 normalizer 解析
        sys.path.insert(0, runner.PALACE_SRC)
        from memory_palace.codex_normalizer import normalize_capture

        newest = max(
            (
                os.path.join(self.project, ".memory-palace", "captures", name)
                for name in captures
            ),
            key=os.path.getmtime,
        )
        normalized = normalize_capture(newest)
        self.assertEqual([m.role for m in normalized], ["user", "assistant", "user"])
        self.assertEqual(normalized[0].body, "问题")

    def test_session_end_waits_and_picks_longer(self):
        self.transcript = write_transcript(self.project, [user_line("早")])
        original_compile = runner.compile_visible_messages

        def growing_compile(path):
            messages = original_compile(path)
            if not self.slept:
                # 第一次读之后模拟异步落盘：尾部补齐，第二次读自然更长
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(assistant_line([{"type": "text", "text": "尾"}]) + "\n")
            return messages

        runner.compile_visible_messages = growing_compile
        self.addCleanup(setattr, runner, "compile_visible_messages", original_compile)
        # _compile_with_tail_wait 引用模块级名，需同 patch
        self.assertEqual(self._run(self._payload("SessionEnd")), "accepted")
        self.assertEqual(self.slept, [runner.SESSIONEND_TAIL_WAIT_SECONDS])

    def test_precompact_does_not_sleep(self):
        self.transcript = write_transcript(self.project, [user_line("甲")])
        self.assertEqual(self._run(self._payload()), "accepted")
        self.assertEqual(self.slept, [])

    def test_missing_transcript_ignored(self):
        self.transcript = write_transcript(self.project, [user_line("甲")])
        payload = self._payload()
        payload["transcript_path"] = None
        self.assertEqual(self._run(payload), "ignored")
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.lexists(os.path.join(self.project, ".memory-palace")))

    def test_no_wake_env_switch(self):
        self.transcript = write_transcript(self.project, [user_line("甲")])
        disposition = runner.process_payload(
            self._payload(), clock=lambda: 1.0, sleep=lambda s: None,
            wake=self.calls.append, environ={runner._NO_WAKE_ENV: "1"},
        )
        self.assertEqual(disposition, "accepted")
        self.assertEqual(self.calls, [])

    def test_conflict_raises_and_fail_open_exits_zero(self):
        self.transcript = write_transcript(self.project, [user_line("甲")])
        self.assertEqual(self._run(self._payload()), "accepted")
        capture_dir = os.path.join(self.project, ".memory-palace", "captures")
        for name in os.listdir(capture_dir):
            os.unlink(os.path.join(capture_dir, name))  # 制造 capture XOR job
        stderr = io.StringIO()
        code = runner.run_hook(
            stdin=io.StringIO(json.dumps(self._payload())),
            stdout=io.StringIO(),
            stderr=stderr,
            sleep=lambda s: None,
            wake=self.calls.append,
        )
        self.assertEqual(code, 0)
        safe = json.loads(stderr.getvalue().strip())
        self.assertEqual(safe["status"], "error")
        self.assertNotIn(self.project, stderr.getvalue())  # 不回显路径/正文
        self.assertEqual(runner.run_hook(stdin=io.StringIO("{not json"), stdout=io.StringIO(), stderr=io.StringIO(), sleep=lambda s: None, wake=self.calls.append), 0)


class AdmissionGateTests(unittest.TestCase):
    """会话级准入门：temp cwd / sdk entrypoint 一刀切 / 零可见对话，全部零副作用。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=SANDBOX_BASE)
        self.project = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.calls: list = []
        self.original_mirror = runner.MIRROR_ROOT
        runner.MIRROR_ROOT = os.path.join(self._tmp.name, "mirror")
        self.addCleanup(setattr, runner, "MIRROR_ROOT", self.original_mirror)

    def _run(self, transcript, cwd=None, event="PreCompact"):
        payload = {
            "session_id": "gate-1",
            "transcript_path": transcript,
            "cwd": cwd or self.project,
            "hook_event_name": event,
        }
        payload["trigger" if event == "PreCompact" else "reason"] = "auto" if event == "PreCompact" else "other"
        return runner.process_payload(payload, sleep=lambda s: None, wake=self.calls.append, environ={})

    def _assert_rejected(self, disposition, transcript, cwd=None):
        self.assertTrue(disposition.startswith("ignored:"), disposition)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.lexists(os.path.join(cwd or self.project, ".memory-palace")))
        self.assertFalse(os.path.lexists(runner.MIRROR_ROOT))

    def test_temp_cwd_denied(self):
        path = write_transcript(self.project, [user_line("hi", entrypoint="cli")])
        for denied in ("/private/tmp/x", "/tmp", "/tmp/x", "/var/folders/abc"):
            self._assert_rejected(self._run(path, cwd=denied), path, cwd=denied)

    def test_sdk_entrypoint_denied_all_variants(self):
        for ep in ("sdk-py", "sdk-cli", "sdk-ts"):
            path = write_transcript(self.project, [user_line("hi", entrypoint=ep)])
            self._assert_rejected(self._run(path), path)

    def test_cli_entrypoint_admitted(self):
        path = write_transcript(self.project, [user_line("hi", entrypoint="cli")])
        self.assertEqual(self._run(path), "accepted")

    def test_zero_visible_denied(self):
        path = write_transcript(self.project, [tool_result_line()])
        self._assert_rejected(self._run(path), path)

    def test_zero_until_tail_arrives_is_accepted(self):
        # SessionEnd 尾差补齐后才出现首条可见对话：门 3 必须在二次编译之后才裁决
        path = write_transcript(self.project, [tool_result_line()])
        original = runner.compile_visible_messages
        def growing(p):
            msgs = original(p)
            if not self.grew:
                self.grew = True
                with open(p, "a", encoding="utf-8") as h:
                    h.write(user_line("迟到的尾巴") + "\n")
            return msgs
        self.grew = False
        runner.compile_visible_messages = growing
        self.addCleanup(setattr, runner, "compile_visible_messages", original)
        self.assertEqual(self._run(path, event="SessionEnd"), "accepted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
