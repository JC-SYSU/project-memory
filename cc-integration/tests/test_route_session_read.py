"""route_session_read.sh: deny raw transcript reads, allow everything else, honour dev dirs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "route_session_read.sh")
HOME = "/Users/example"
UUID = "abcdef12-1234-1234-1234-123456789abc"


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class RouteSessionReadTests(unittest.TestCase):
    def _decision(self, tool: str, tool_input: dict, cwd: str = HOME, dev_dirs: str | None = None) -> str:
        env = dict(os.environ)
        env.pop("MEMORY_PALACE_DEV_DIRS", None)
        if dev_dirs is not None:
            env["MEMORY_PALACE_DEV_DIRS"] = dev_dirs
        result = subprocess.run(
            ["/bin/bash", HOOK],
            input=json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": cwd}),
            capture_output=True, text=True, env=env, check=True,
        )
        return "deny" if '"deny"' in result.stdout else "allow"

    def test_raw_transcript_access_is_denied(self) -> None:
        cases = [
            ("Bash", {"command": f"find ~/.claude/projects -name '{UUID}*'"}),
            ("Bash", {"command": f"cat ~/.claude/projects/-Users-example/{UUID}.jsonl"}),
            ("Bash", {"command": "find ~/.claude/projects -name '*.jsonl'"}),
            ("Bash", {"command": "ls -t ~/.claude/projects/-Users-example/*.jsonl"}),
            ("Bash", {"command": "python3 x.py ~/.codex/sessions/2026/11/05/rollout-a.jsonl.zst"}),
            ("Bash", {"command": "zstd -dc ~/.codex/archived_sessions/rollout-x.jsonl.zst"}),
            ("Read", {"file_path": f"{HOME}/.claude/projects/-Users-example/{UUID}.jsonl"}),
            ("Read", {"file_path": f"{HOME}/.claude/projects/-Users-example/{UUID}/subagents/a.jsonl"}),
            ("Grep", {"pattern": "foo", "path": f"{HOME}/.claude/projects/-Users-example"}),
            ("Grep", {"pattern": "foo", "path": f"{HOME}/.claude/projects"}),
            ("Glob", {"pattern": "**/*.jsonl", "path": f"{HOME}/.claude/projects"}),
            ("Glob", {"pattern": f"{HOME}/.codex/sessions/**/*"}),
        ]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertEqual(self._decision(tool, tool_input), "deny")

    def test_everything_else_is_allowed(self) -> None:
        cases = [
            ("Read", {"file_path": "/tmp/memory-palace-sessions/session-abcdef12-x.md"}),
            ("Read", {"file_path": f"{HOME}/.claude/projects/-Users-example/memory/MEMORY.md"}),
            ("Grep", {"pattern": "x", "path": f"{HOME}/.claude/projects/-Users-example/memory"}),
            ("Bash", {"command": "ls ~/.claude/projects | head"}),
            ("Bash", {"command": "tail -3 ~/.codex/session_index.jsonl"}),
            ("Bash", {"command": "git status"}),
            ("Grep", {"pattern": "foo", "path": f"{HOME}/repos/app"}),
        ]
        for tool, tool_input in cases:
            with self.subTest(tool=tool, tool_input=tool_input):
                self.assertEqual(self._decision(tool, tool_input), "allow")

    def test_dev_dirs_exempt_only_listed_trees(self) -> None:
        raw = {"command": f"cat ~/.claude/projects/-Users-example/{UUID}.jsonl"}
        dev = f"{HOME}/project-memory:{HOME}/other/"
        self.assertEqual(self._decision("Bash", raw, cwd=f"{HOME}/project-memory", dev_dirs=dev), "allow")
        self.assertEqual(self._decision("Bash", raw, cwd=f"{HOME}/project-memory/src", dev_dirs=dev), "allow")
        self.assertEqual(self._decision("Bash", raw, cwd=f"{HOME}/other", dev_dirs=dev), "allow")
        self.assertEqual(self._decision("Bash", raw, cwd=f"{HOME}/project-memory-x", dev_dirs=dev), "deny")
        self.assertEqual(self._decision("Bash", raw, cwd=f"{HOME}/project-memory", dev_dirs=""), "deny")

    def test_malformed_input_fails_open(self) -> None:
        result = subprocess.run(["/bin/bash", HOOK], input="not json", capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()