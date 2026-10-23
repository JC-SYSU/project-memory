"""Regression tests for the upstream connectivity probes."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.upstream_probe import (
    UpstreamConfig,
    load_config,
    missing_config_markdown,
    preview_markdown,
    render_report,
    run_probes,
)

SECRET = "test-secret-value"


class _Handler(http.server.BaseHTTPRequestHandler):
    count = 0
    fail_tools = False
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        with type(self).lock:
            type(self).count += 1
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if type(self).fail_tools and "tools" in body:
            data = b'{"error":"tools rejected"}'
            self.send_response(400)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if "tools" in body:
            payload = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "1",
                                    "type": "function",
                                    "function": {"name": "probe", "arguments": "{}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"total_tokens": 10},
            }
        else:
            payload = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"total_tokens": 2},
            }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        _ = (format, args)


class UpstreamProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _Handler.count = 0
        _Handler.fail_tools = False
        self._env = {
            "PROJECT_MEMORY_BASE_URL": f"http://127.0.0.1:{self.port}/v1",
            "PROJECT_MEMORY_API_KEY": SECRET,
            "PROJECT_MEMORY_MODEL": "test-model",
            "PROJECT_MEMORY_REASONING_EFFORT": "high",
        }
        self._saved = {key: os.environ.get(key) for key in self._env}
        os.environ.update(self._env)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_preview_sends_no_requests(self) -> None:
        config = load_config()
        text = preview_markdown(config)
        self.assertEqual(_Handler.count, 0)
        self.assertIn("尚未发起请求", text)
        self.assertNotIn(SECRET, text)

    def test_probes_all_pass(self) -> None:
        config = load_config()
        results = run_probes(config, timeout=5)
        self.assertEqual(_Handler.count, 4)
        self.assertTrue(all(result.ok for result in results))
        text = render_report(config, results)
        self.assertIn("**结论：上游连接正常**", text)
        self.assertNotIn(SECRET, text)

    def test_tool_calling_failure(self) -> None:
        _Handler.fail_tools = True
        config = load_config()
        results = run_probes(config, timeout=5)
        tool_result = next(
            result for result in results if result.name == "强制工具调用"
        )
        self.assertFalse(tool_result.ok)
        text = render_report(config, results)
        self.assertIn("**结论：上游连接有问题（1 项未通过）**", text)

    def test_missing_config_sends_no_requests(self) -> None:
        config = UpstreamConfig("", "", "", "high")
        results = run_probes(config)
        self.assertEqual(_Handler.count, 0)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("缺少配置", missing_config_markdown(config))

    def test_cli_passes_against_stub(self) -> None:
        env = dict(os.environ)
        env.update(self._env)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "test_connection.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("四项全部通过", completed.stdout)
        self.assertNotIn(SECRET, completed.stdout)

    def test_cli_reports_missing_config(self) -> None:
        env = dict(os.environ)
        for key in self._env:
            env.pop(key, None)
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "test_connection.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("缺少配置", completed.stdout)


if __name__ == "__main__":
    unittest.main()
