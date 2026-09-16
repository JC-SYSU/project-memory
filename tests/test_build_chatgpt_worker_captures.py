"""Focused tests for the ChatGPT Markdown -> Capture JSONL compiler."""

from __future__ import annotations

import csv
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "rust-rewrite" / "src"
for candidate in (SRC, str(ROOT / "tools")):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from memory_palace.codex_normalizer import normalize_capture  # noqa: E402
from memory_palace.project_paths import read_session_metadata  # noqa: E402
import build_chatgpt_worker_captures as compiler  # noqa: E402

ASSIGNMENT_HEADER = [
    "session_order",
    "session_file",
    "title",
    "created_at",
    "message_count",
    "recommended_project",
    "project_path",
    "project_kind",
    "confidence",
    "rationale",
]


def write_markdown(
    path: Path,
    *,
    title: str = "Demo",
    conversation_id: str = "11111111-2222-3333-4444-555555555555",
    body: str,
) -> None:
    path.write_text(
        "---\n"
        f'title: {json.dumps(title, ensure_ascii=False)}\n'
        f'conversation_id: "{conversation_id}"\n'
        'created_at: "2026-01-12T21:18:01.261576+08:00"\n'
        'source_file: "/tmp/chat.html"\n'
        'rendering_policy: "current_node ancestry"\n'
        "alternate_branches_omitted: false\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{body}",
        encoding="utf-8",
    )


def assignments_csv(
    path: Path, rows: list[list[str]], header: list[str] | None = None
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows([header or ASSIGNMENT_HEADER, *rows])


class SelectionTests(unittest.TestCase):
    def test_filter_requires_project_local_high(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            base = Path(tmp)
            assignments = base / "PROJECT_ASSIGNMENTS.csv"
            sessions = base / "sessions"
            output = base / "worker-captures"
            sessions.mkdir()
            rows = [
                ["1", "sessions/a.md", "A", "t", "2", "样例项目", "/tmp/p1", "local", "high", "r"],
                ["2", "sessions/b.md", "B", "t", "2", "", "", "", "", "no match"],
                ["3", "sessions/c.md", "C", "t", "2", "示例项目", "", "chatgpt", "high", "r"],
                ["4", "sessions/d.md", "D", "t", "2", "邮件模板", "/tmp/p2", "local", "medium", "r"],
            ]
            assignments_csv(assignments, rows)
            selected = compiler.select_assignments(assignments)
            self.assertEqual([r["session_file"] for r in selected], ["sessions/a.md"])

            # --min-confidence=medium widens the filter to include row 4.
            wider = compiler.select_assignments(assignments, min_confidence="medium")
            self.assertEqual(
                [r["session_file"] for r in wider],
                ["sessions/a.md", "sessions/d.md"],
            )


class CompileTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.sessions = self.base / "sessions"
        self.output = self.base / "worker-captures"
        self.sessions.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- fixtures -------------------------------------------------------

    def standard_body(self) -> str:
        return (
            "## user\n\n"
            "<!-- message_id: 00000000-0000-0000-0000-000000000001; "
            "created_at: 2026-01-12T21:18:00.566000+08:00 -->\n\n"
            "请帮我优化配置\n\n"
            "## ChatGPT\n\n"
            "<!-- message_id: 00000000-0000-0000-0000-000000000002; "
            "created_at: 2026-01-12T21:18:01.182962+08:00 -->\n\n"
            "好的。\n\n"
            "## 一个普通二级标题\n\n"
            "正文里的二级标题不能切分消息。\n\n"
            "继续同一条消息。\n\n"
            "## Custom user info\n\n"
            "<!-- message_id: 00000000-0000-0000-0000-000000000003; "
            "created_at: 2026-01-12T21:19:00.000000+08:00 -->\n\n"
            "用户自定义信息\n"
        )

    def compile_markdown(
        self, body: str, *, project_path: str = "/tmp/p1", name: str = "a.md"
    ) -> tuple[Path, list[str]]:
        write_markdown(self.sessions / name, body=body)
        assignments = self.base / "PROJECT_ASSIGNMENTS.csv"
        assignments_csv(
            assignments,
            [["1", f"sessions/{name}", "A", "t", "2", "样例项目", project_path, "local", "high", "r"]],
        )
        entries = compiler.compile_all(
            assignments_path=assignments,
            sessions_dir=self.sessions,
            output_dir=self.output,
        )
        self.assertEqual(len(entries), 1)
        return Path(entries[0]["capture"]), [json.loads(line) for line in Path(entries[0]["capture"]).read_text(encoding="utf-8").splitlines()]

    # ---- required scenarios ----------------------------------------------

    def test_three_roles_and_body_headings(self) -> None:
        capture_path, records = self.compile_markdown(self.standard_body())
        self.assertEqual(records[0]["type"], "import_meta")
        self.assertEqual(
            records[0]["payload"]["target_project_root"], "/tmp/p1"
        )
        self.assertEqual(records[1]["type"], "session_meta")
        self.assertEqual(
            records[1]["payload"]["id"],
            "chatgpt-web-v1:11111111-2222-3333-4444-555555555555",
        )
        self.assertEqual(records[1]["payload"]["cwd"], "/tmp/p1")
        messages = records[2:]
        self.assertEqual(len(messages), 3)  # body H2 did not split
        user, assistant, custom = messages
        self.assertEqual(user["type"], "event_msg")
        self.assertEqual(user["payload"]["type"], "user_message")
        self.assertEqual(user["payload"]["message"], "请帮我优化配置")
        self.assertEqual(user["timestamp"], "2026-01-12T21:18:00.566000+08:00")
        self.assertEqual(
            user["payload"]["chatgpt_message_id"],
            "00000000-0000-0000-0000-000000000001",
        )
        self.assertEqual(user["payload"]["source_markdown_line"], 12)  # '## user' line
        self.assertEqual(assistant["type"], "response_item")
        self.assertEqual(assistant["payload"]["type"], "message")
        self.assertEqual(assistant["payload"]["role"], "assistant")
        self.assertEqual(
            assistant["payload"]["content"],
            [{"type": "output_text", "text": "好的。\n\n## 一个普通二级标题\n\n正文里的二级标题不能切分消息。\n\n继续同一条消息。"}],
        )
        self.assertEqual(custom["payload"]["type"], "user_message")
        self.assertEqual(custom["payload"]["message"], "用户自定义信息")

        # session_meta drives the manual-intake identity path unchanged.
        metadata = read_session_metadata(capture_path)
        self.assertEqual(metadata.cwd, "/tmp/p1")
        self.assertEqual(
            metadata.session_id,
            "chatgpt-web-v1:11111111-2222-3333-4444-555555555555",
        )

    def test_attachment_line_excluded_from_body(self) -> None:
        body = (
            "## user\n\n"
            "<!-- message_id: 00000000-0000-0000-0000-000000000001; "
            "created_at: 2026-01-12T21:18:00.566000+08:00 -->\n\n"
            "看下这个文件\n\n"
            "**Attached export files:** `file-abc.dat`, `file-def.dat`\n\n"
            "## ChatGPT\n\n"
            "<!-- message_id: 00000000-0000-0000-0000-000000000002; "
            "created_at: 2026-01-12T21:18:01.182962+08:00 -->\n\n"
            "收到。\n"
        )
        capture_path, records = self.compile_markdown(body)
        messages = records[2:]
        self.assertEqual(messages[0]["payload"]["message"], "看下这个文件")
        self.assertNotIn("Attached export files", messages[0]["payload"]["message"])
        self.assertEqual(
            messages[0]["payload"]["attached_export_files"],
            ["file-abc.dat", "file-def.dat"],
        )
        import_meta = records[0]["payload"]
        self.assertEqual(
            import_meta["attached_export_files"], ["file-abc.dat", "file-def.dat"]
        )
        manifest = json.loads(
            (self.output / "capture_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["capture_count"], 1)
        self.assertEqual(
            manifest["captures"][0]["attached_export_files"],
            ["file-abc.dat", "file-def.dat"],
        )

    def test_malformed_markdown_rejected_without_partial_output(self) -> None:
        bad_bodies = {
            "no-comment": "## user\n\n直接正文没有注释行\n",
            "bad-comment": (
                "## user\n\n"
                "<!-- created_at: 2026-01-12T21:18:00.566000+08:00 -->\n\n"
                "正文\n"
            ),
            "no-blocks": "只有标题，没有消息块\n",
            "bad-frontmatter-json": None,  # patched in below, see frontmatter test
        }
        del bad_bodies["bad-frontmatter-json"]
        for name, body in bad_bodies.items():
            with self.subTest(case=name):
                output = self.base / f"out-{name}"
                write_markdown(self.sessions / f"{name}.md", body=body)
                assignments = self.base / f"assign-{name}.csv"
                assignments_csv(
                    assignments,
                    [["1", f"sessions/{name}.md", "A", "t", "1", "样例项目", "/tmp/p1", "local", "high", "r"]],
                )
                with self.assertRaises(compiler.CompileError) as ctx:
                    compiler.compile_all(
                        assignments_path=assignments,
                        sessions_dir=self.sessions,
                        output_dir=output,
                    )
                self.assertIn(f"{name}.md:", str(ctx.exception))
                self.assertEqual(list(output.iterdir()), [])  # nothing partial

    def test_frontmatter_invalid_json_reports_file_and_line(self) -> None:
        write_markdown(
            self.sessions / "badfm.md",
            body="## user\n\n正文\n",
            title="ok-title",
        )
        # Corrupt line 2 (the title value) into invalid JSON in place.
        target = self.sessions / "badfm.md"
        lines = target.read_text(encoding="utf-8").split("\n")
        lines[1] = 'title: "unterminated'
        target.write_text("\n".join(lines), encoding="utf-8")
        assignments = self.base / "assign-badfm.csv"
        assignments_csv(
            assignments,
            [["1", "sessions/badfm.md", "A", "t", "1", "样例项目", "/tmp/p1", "local", "high", "r"]],
        )
        output = self.base / "out-badfm"
        with self.assertRaises(compiler.CompileError) as ctx:
            compiler.compile_all(
                assignments_path=assignments,
                sessions_dir=self.sessions,
                output_dir=output,
            )
        message = str(ctx.exception)
        self.assertIn("badfm.md:2:", message)  # file + line reported
        self.assertIn("frontmatter", message)
        # CLI must surface it as a clean error, not a traceback.
        exit_code = compiler.main(
            [
                "--assignments", str(assignments),
                "--sessions-dir", str(self.sessions),
                "--output-dir", str(output),
            ]
        )
        self.assertEqual(exit_code, 1)

    def test_custom_output_dir_with_foreign_files_is_refused(self) -> None:
        write_markdown(self.sessions / "a.md", body=self.standard_body())
        assignments = self.base / "PROJECT_ASSIGNMENTS.csv"
        assignments_csv(
            assignments,
            [["1", "sessions/a.md", "A", "t", "3", "样例项目", "/tmp/p1", "local", "high", "r"]],
        )
        output = self.base / "precious"
        output.mkdir()
        (output / "keep-me.txt").write_text("user data", encoding="utf-8")
        (output / "notes").mkdir()  # foreign subdirectory too
        with self.assertRaises(compiler.CompileError) as ctx:
            compiler.compile_all(
                assignments_path=assignments,
                sessions_dir=self.sessions,
                output_dir=output,
            )
        self.assertIn("not owned by this tool's manifest", str(ctx.exception))
        # Nothing was deleted or written.
        self.assertEqual(
            sorted(p.name for p in output.iterdir()), ["keep-me.txt", "notes"]
        )
        self.assertEqual(
            (output / "keep-me.txt").read_text(encoding="utf-8"), "user data"
        )

    def test_foreign_jsonl_outside_manifest_is_preserved(self) -> None:
        # A user .jsonl the tool never wrote must survive a rerun even though
        # the directory otherwise holds only this tool's proven artifacts.
        self.compile_markdown(self.standard_body())
        (self.output / "not-owned.jsonl").write_text("user data", encoding="utf-8")
        with self.assertRaises(compiler.CompileError) as ctx:
            self.compile_markdown(self.standard_body())
        self.assertIn("not-owned.jsonl", str(ctx.exception))
        # The user file is untouched; this tool's prior artifacts remain too.
        self.assertEqual(
            (self.output / "not-owned.jsonl").read_text(encoding="utf-8"),
            "user data",
        )
        self.assertTrue((self.output / "a.jsonl").exists())
        self.assertTrue((self.output / "capture_manifest.json").exists())

    def test_foreign_manifest_is_preserved(self) -> None:
        # A capture_manifest.json this tool did NOT write (wrong format, or a
        # file from another tool) must never be deleted, even when it is the
        # only entry in the directory.
        write_markdown(self.sessions / "a.md", body=self.standard_body())
        assignments = self.base / "PROJECT_ASSIGNMENTS.csv"
        assignments_csv(
            assignments,
            [["1", "sessions/a.md", "A", "t", "3", "样例项目", "/tmp/p1", "local", "high", "r"]],
        )
        for label, payload in (
            ("wrong-format", '{"format":"some-other-tool","captures":[]}'),
            ("corrupt-json", "{not valid json"),
            ("empty-file", ""),
        ):
            with self.subTest(case=label):
                foreign_dir = self.base / f"fm-{label}"
                foreign_dir.mkdir()
                target = foreign_dir / "capture_manifest.json"
                target.write_text(payload, encoding="utf-8")
                with self.assertRaises(compiler.CompileError) as ctx:
                    compiler.compile_all(
                        assignments_path=assignments,
                        sessions_dir=self.sessions,
                        output_dir=foreign_dir,
                    )
                self.assertIn("capture_manifest.json", str(ctx.exception))
                # The foreign manifest survived unchanged.
                self.assertEqual(
                    target.read_text(encoding="utf-8"), payload
                )
                self.assertEqual(list(foreign_dir.iterdir()), [target])

    def test_rerun_over_tool_artifacts_still_cleans(self) -> None:
        # An output dir holding only this tool's manifest-proven artifacts is
        # safe to rebuild: prior captures are removed, the new set written.
        self.compile_markdown(self.standard_body())
        self.compile_markdown(self.standard_body())
        names = sorted(p.name for p in self.output.iterdir())
        self.assertEqual(names, ["a.jsonl", "capture_manifest.json"])

    def test_normalize_capture_round_trip(self) -> None:
        capture_path, _ = self.compile_markdown(self.standard_body())
        normalized = normalize_capture(capture_path)
        self.assertEqual(
            [(m.role, m.timestamp) for m in normalized],
            [
                ("user", "2026-01-12T21:18:00.566000+08:00"),
                ("assistant", "2026-01-12T21:18:01.182962+08:00"),
                ("user", "2026-01-12T21:19:00.000000+08:00"),
            ],
        )
        self.assertEqual(normalized[0].body, "请帮我优化配置")
        self.assertEqual(
            normalized[1].body,
            "好的。\n\n## 一个普通二级标题\n\n正文里的二级标题不能切分消息。\n\n继续同一条消息。",
        )
        self.assertEqual(normalized[2].body, "用户自定义信息")
        # Traceability fields survive alongside the frozen carriers.
        raw_records = [
            json.loads(line)
            for line in capture_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(
            [r["payload"].get("source_markdown_line") for r in raw_records[2:]],
            [12, 18, 30],  # '## user' / '## ChatGPT' / '## Custom user info' lines
        )

    def test_rerun_is_idempotent(self) -> None:
        self.compile_markdown(self.standard_body())
        before = sorted(p.name for p in self.output.iterdir())
        content_before = {
            p.name: p.read_bytes() for p in self.output.iterdir()
        }
        self.compile_markdown(self.standard_body())
        after = sorted(p.name for p in self.output.iterdir())
        self.assertEqual(before, after)
        self.assertEqual(
            {p.name: p.read_bytes() for p in self.output.iterdir()}, content_before
        )


if __name__ == "__main__":
    unittest.main()
