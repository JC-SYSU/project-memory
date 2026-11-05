"""The extraction system prompt pins the memory output language."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.contracts import NormalizedMessage, Window  # noqa: E402
from memory_palace.extraction_core import render_prompts  # noqa: E402


class ExtractionPromptLanguageTests(unittest.TestCase):
    def setUp(self) -> None:
        window = Window(
            messages=(NormalizedMessage(2, "2026-11-05T00:00:00Z", "user", "use English please"),),
            consumed_through_line=2,
            body_chars=18,
        )
        self.system_prompt, _ = render_prompts(window)

    def test_title_and_summary_must_be_simplified_chinese(self) -> None:
        self.assertIn("Write every title and summary in Simplified Chinese", self.system_prompt)
        self.assertIn("whatever language the Window messages use", self.system_prompt)

    def test_technical_tokens_keep_original_form(self) -> None:
        for token in ("code identifiers", "file paths", "commands", "product or model names"):
            self.assertIn(token, self.system_prompt)
        self.assertIn("do not translate or transliterate them", self.system_prompt)


if __name__ == "__main__":
    unittest.main()