"""Codex Normalizer carrier tests: legacy user_message and item_completed UserMessage."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.codex_normalizer import normalize_capture  # noqa: E402

TS = "2026-11-05T00:00:00.000Z"


def meta() -> dict:
    return {"timestamp": TS, "type": "session_meta", "payload": {"id": "s", "cwd": "/x"}}


def legacy_user(text: str) -> dict:
    return {"timestamp": TS, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def item_user(*blocks: dict) -> dict:
    return {
        "timestamp": TS,
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": {"type": "UserMessage", "id": "u", "content": list(blocks)}},
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


def response_user(text: str) -> dict:
    return {
        "timestamp": TS,
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def assistant(text: str) -> dict:
    return {
        "timestamp": TS,
        "type": "response_item",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]},
    }


def agent_item(text: str) -> dict:
    return {
        "timestamp": TS,
        "type": "event_msg",
        "payload": {"type": "item_completed", "item": {"type": "AgentMessage", "content": [{"type": "Text", "text": text}]}},
    }


class CodexNormalizerCarrierTests(unittest.TestCase):
    def _run(self, records: list[dict], start_line: int = 1):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            path = handle.name
        try:
            return normalize_capture(path, start_line=start_line)
        finally:
            Path(path).unlink()

    def test_item_completed_user_message_is_retained(self) -> None:
        result = self._run([
            meta(),
            response_user("# AGENTS.md instructions\n<INSTRUCTIONS>x</INSTRUCTIONS>"),
            response_user("砍掉 1-8"),
            item_user(text_block("砍掉 1-8\n")),
            agent_item("好的"),
            assistant("好的"),
        ])
        self.assertEqual([(m.source_line, m.role, m.body) for m in result], [
            (4, "user", "砍掉 1-8\n"),
            (6, "assistant", "好的"),
        ])

    def test_response_item_user_is_never_a_carrier(self) -> None:
        result = self._run([meta(), response_user("<environment_context>x</environment_context>"), assistant("a")])
        self.assertEqual([m.role for m in result], ["assistant"])

    def test_legacy_file_ignores_item_user_messages(self) -> None:
        result = self._run([meta(), legacy_user("旧载体"), item_user(text_block("新载体")), assistant("a")])
        self.assertEqual([m.body for m in result if m.role == "user"], ["旧载体"])

    def test_item_user_drops_images_skills_and_browser_context(self) -> None:
        result = self._run([
            meta(),
            item_user(
                text_block("<in-app-browser-context source=\"ambient-ui-state\">x</in-app-browser-context>"),
                {"type": "local_image", "path": "/tmp/a.png"},
                {"type": "skill", "name": "s"},
                text_block("看这张图"),
            ),
        ])
        self.assertEqual([m.body for m in result], ["看这张图"])

    def test_item_user_only_injection_yields_nothing(self) -> None:
        result = self._run([
            meta(),
            item_user(text_block("<codex_internal_context source=\"goal\">x</codex_internal_context>")),
            item_user({"type": "local_image", "path": "/tmp/a.png"}),
        ])
        self.assertEqual(result, ())

    def test_start_line_applies_to_item_user(self) -> None:
        result = self._run([meta(), item_user(text_block("一")), assistant("a"), item_user(text_block("二"))], start_line=3)
        self.assertEqual([(m.source_line, m.body) for m in result], [(3, "a"), (4, "二")])

    def test_agent_message_items_are_not_duplicated(self) -> None:
        result = self._run([meta(), agent_item("回复"), assistant("回复")])
        self.assertEqual([(m.source_line, m.role) for m in result], [(3, "assistant")])


if __name__ == "__main__":
    unittest.main()