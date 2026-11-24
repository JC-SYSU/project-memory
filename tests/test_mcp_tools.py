"""Regression tests for the MCP tool registration contract."""

from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

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
            names,
            {
                "memory_recall",
                "memory_health",
                "memory_upstream_check",
                "memory_list_sessions",
                "memory_read_session",
            },
        )

    def test_session_tools_are_read_only(self) -> None:
        for name in ("memory_list_sessions", "memory_read_session"):
            tool = next(tool for tool in self._tools() if tool.name == name)
            assert tool.annotations is not None
            self.assertTrue(tool.annotations.readOnlyHint)
            self.assertFalse(tool.annotations.openWorldHint)

    def test_read_session_returns_a_file_not_the_body(self) -> None:
        tool = next(tool for tool in self._tools() if tool.name == "memory_read_session")
        self.assertEqual(set(tool.inputSchema["properties"]), {"session_id", "project"})
        self.assertEqual(tool.inputSchema.get("required"), ["session_id"])
        output = set(tool.outputSchema["properties"])
        self.assertTrue({"path", "estimated_tokens", "exceeds_threshold", "chunks"} <= output)
        self.assertNotIn("messages", output)

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


class McpUpstreamStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_previews_inherited_values_without_showing_key(self) -> None:
        env = {
            "PROJECT_MEMORY_BASE_URL": "http://127.0.0.1:1/v1",
            "PROJECT_MEMORY_API_KEY": "stdio-test-key",
            "PROJECT_MEMORY_MODEL": "stdio-test-model",
            "PROJECT_MEMORY_REASONING_EFFORT": "low",
        }
        with TemporaryDirectory() as tmp:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", str(SRC / "memory_palace_recall_mcp_server.py")],
                cwd=tmp,
                env=env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=10)) as client:
                    await client.initialize()
                    result = await client.call_tool("memory_upstream_check", {})
        self.assertFalse(result.isError)
        text = result.content[0].text
        self.assertIn("stdio-test-model", text)
        self.assertIn("reasoning_effort：low", text)
        self.assertIn("API Key：已配置", text)
        self.assertIn("尚未发起请求", text)
        self.assertNotIn(env["PROJECT_MEMORY_API_KEY"], text)


if __name__ == "__main__":
    unittest.main()
