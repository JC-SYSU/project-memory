"""Regression tests for the MCP tool registration contract."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace_recall_mcp_server import _build_server  # noqa: E402


class McpToolContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _build_server(str(ROOT))

    def _tools(self):
        return asyncio.run(self.server.list_tools())

    def test_tool_names(self) -> None:
        names = {tool.name for tool in self._tools()}
        self.assertEqual(
            names, {"memory_recall", "memory_health", "memory_upstream_check"}
        )

    def test_health_is_read_only(self) -> None:
        tool = next(tool for tool in self._tools() if tool.name == "memory_health")
        annotations = tool.annotations
        self.assertIsNotNone(annotations)
        assert annotations is not None
        self.assertTrue(annotations.readOnlyHint)
        self.assertTrue(annotations.idempotentHint)
        self.assertFalse(annotations.openWorldHint)

    def test_upstream_requires_confirmation_and_spends(self) -> None:
        tool = next(
            tool for tool in self._tools() if tool.name == "memory_upstream_check"
        )
        annotations = tool.annotations
        self.assertIsNotNone(annotations)
        assert annotations is not None
        self.assertFalse(annotations.readOnlyHint)
        self.assertFalse(annotations.idempotentHint)
        self.assertTrue(annotations.openWorldHint)
        self.assertIn("confirm", tool.inputSchema.get("properties", {}))
        self.assertIn("confirm=true", tool.description or "")


if __name__ == "__main__":
    unittest.main()
