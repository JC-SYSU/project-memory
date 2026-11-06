"""install.py / install_claude.py: relocation, Claude Code merges, skill copy, dry-run safety.

Every test runs against a temporary HOME; nothing touches the real ~/.codex or ~/.claude.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import install  # noqa: E402
import install_claude  # noqa: E402

PY = "/usr/bin/python3-example"


class _TempHome(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="pm-install-"))
        self.addCleanup(shutil.rmtree, self.home, True)

    def write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    def read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))


class RelocateTests(_TempHome):
    def make_repo(self, where: Path) -> Path:
        (where / ".git").mkdir(parents=True)
        (where / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (where / "src").mkdir()
        (where / "src" / "a.py").write_text("x = 1\n")
        (where / "dirty.txt").write_text("uncommitted\n")
        os.symlink("src/a.py", where / "link.py")
        return where

    def test_standard_location_prefers_codex(self) -> None:
        self.assertEqual(install.standard_location(self.home, True), self.home / ".codex" / "memory-palace")
        self.assertEqual(install.standard_location(self.home, False), self.home / ".claude" / "memory-palace")

    def test_apply_moves_everything_and_deletes_source(self) -> None:
        source = self.make_repo(self.home / "Downloads" / "project-memory")
        target = self.home / ".codex" / "memory-palace"
        before = install._manifest(source)
        _, moved = install.relocate(source, target, apply=True)
        self.assertTrue(moved)
        self.assertFalse(source.exists())
        self.assertEqual(install._manifest(target), before)
        self.assertTrue((target / "link.py").is_symlink())
        self.assertEqual((target / "dirty.txt").read_text(), "uncommitted\n")

    def test_dry_run_changes_nothing(self) -> None:
        source = self.make_repo(self.home / "src-repo")
        target = self.home / ".claude" / "memory-palace"
        messages, moved = install.relocate(source, target, apply=False)
        self.assertFalse(moved)
        self.assertTrue(source.exists())
        self.assertFalse(target.exists())
        self.assertIn("would copy", "\n".join(messages))

    def test_already_in_place(self) -> None:
        target = self.make_repo(self.home / ".codex" / "memory-palace")
        messages, moved = install.relocate(target, target, apply=True)
        self.assertFalse(moved)
        self.assertIn("already at", messages[0])

    def test_existing_other_target_stops_without_touching_either(self) -> None:
        source = self.make_repo(self.home / "a")
        target = self.make_repo(self.home / ".codex" / "memory-palace")
        (target / "dirty.txt").write_text("other\n")
        with self.assertRaises(SystemExit):
            install.relocate(source, target, apply=True)
        self.assertEqual((source / "dirty.txt").read_text(), "uncommitted\n")
        self.assertEqual((target / "dirty.txt").read_text(), "other\n")

    def test_mismatched_copy_rolls_back(self) -> None:
        source = self.make_repo(self.home / "a")
        target = self.home / ".codex" / "memory-palace"
        real = install._manifest
        calls = []

        def flaky(root: Path) -> dict:
            calls.append(root)
            data = real(root)
            return data if len(calls) == 1 else {**data, "extra": ("file",)}

        with mock.patch.object(install, "_manifest", flaky), self.assertRaises(SystemExit):
            install.relocate(source, target, apply=True)
        self.assertTrue((source / "src" / "a.py").exists())
        self.assertFalse(target.exists())

    def test_interpreter_inside_repo_is_refused(self) -> None:
        source = self.make_repo(self.home / "a")
        with mock.patch.object(install.sys, "executable", str(source / ".venv" / "bin" / "python3")), \
                self.assertRaises(SystemExit):
            install.relocate(source, self.home / ".codex" / "memory-palace", apply=True)
        self.assertTrue(source.exists())


class ClaudeMergeTests(_TempHome):
    def incoming(self, repo: Path = Path("/opt/pm")) -> tuple[dict, dict]:
        base = install_claude.render(ROOT / "templates" / "claude.example.json", repo, PY)
        return base["hooks"], base["mcpServers"][install_claude.MCP_NAME]

    def test_fresh_settings_get_three_events(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        hooks, _ = self.incoming()
        install_claude.merge_hooks(settings, hooks, apply=True)
        data = self.read_json(settings)["hooks"]
        self.assertEqual(sorted(data), ["PreCompact", "SessionEnd", "SessionStart"])
        self.assertIn(f"{PY} -B /opt/pm/cc-integration/cc_capture_hook_runner.py",
                      data["PreCompact"][0]["hooks"][0]["command"])

    def test_existing_hooks_kept_and_same_script_not_duplicated(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        old_runner = "/venv/python3 -B /old/place/cc_capture_hook_runner.py"
        self.write_json(settings, {
            "model": "keep-me",
            "hooks": {
                "PreCompact": [{"matcher": "", "hooks": [{"type": "command", "command": old_runner}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "/x/other-sweep.py --sweep"}]}],
            },
        })
        hooks, _ = self.incoming()
        install_claude.merge_hooks(settings, hooks, apply=True)
        data = self.read_json(settings)
        self.assertEqual(data["model"], "keep-me")
        pre = [h["command"] for g in data["hooks"]["PreCompact"] for h in g["hooks"]]
        self.assertEqual(pre, [old_runner])
        end = [h["command"] for g in data["hooks"]["SessionEnd"] for h in g["hooks"]]
        self.assertEqual(len(end), 2)
        self.assertTrue(any("cc_capture_hook_runner.py" in c for c in end))
        self.assertTrue(list(settings.parent.glob("settings.json.bak-*")))

    def test_second_run_changes_nothing(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        hooks, _ = self.incoming()
        install_claude.merge_hooks(settings, hooks, apply=True)
        first = settings.read_text()
        messages = install_claude.merge_hooks(settings, hooks, apply=True)
        self.assertEqual(settings.read_text(), first)
        self.assertIn("settings.json: nothing to change", messages)

    def test_dry_run_writes_nothing(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        hooks, server = self.incoming()
        install_claude.merge_hooks(settings, hooks, apply=False)
        install_claude.merge_mcp(self.home / ".claude.json", server, apply=False)
        self.assertFalse(settings.exists())
        self.assertFalse((self.home / ".claude.json").exists())

    def test_mcp_added_with_python_and_other_keys_kept(self) -> None:
        target = self.home / ".claude.json"
        self.write_json(target, {"numStartups": 7, "mcpServers": {"other": {"command": "x"}}})
        target.chmod(0o600)
        _, server = self.incoming()
        install_claude.merge_mcp(target, server, apply=True)
        data = self.read_json(target)
        self.assertEqual(data["numStartups"], 7)
        self.assertIn("other", data["mcpServers"])
        entry = data["mcpServers"][install_claude.MCP_NAME]
        self.assertEqual(entry["command"], "/opt/pm/cc-integration/cc_memory_palace_recall_wrapper.zsh")
        self.assertEqual(entry["env"], {"PROJECT_MEMORY_PYTHON": PY})
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_existing_mcp_server_left_alone(self) -> None:
        target = self.home / ".claude.json"
        self.write_json(target, {"mcpServers": {install_claude.MCP_NAME: {"command": "/mine"}}})
        _, server = self.incoming()
        messages = install_claude.merge_mcp(target, server, apply=True)
        self.assertEqual(self.read_json(target)["mcpServers"][install_claude.MCP_NAME], {"command": "/mine"})
        self.assertIn("already present", messages[0])


class SkillTests(_TempHome):
    def test_copied_once_when_skill_dirs_are_linked(self) -> None:
        codex_skills = self.home / ".codex" / "skills"
        codex_skills.mkdir(parents=True)
        os.symlink(codex_skills, self.home / ".claude-skills")
        install.install_skill(ROOT, [codex_skills, self.home / ".claude-skills"], apply=True)
        self.assertTrue((codex_skills / "continue-session" / "SKILL.md").exists())
        messages = install.install_skill(ROOT, [codex_skills, self.home / ".claude-skills"], apply=True)
        self.assertIn("already up to date", messages[0])
        self.assertIn("same directory", messages[1])

    def test_modified_skill_left_alone(self) -> None:
        dest = self.home / "skills" / "continue-session"
        dest.mkdir(parents=True)
        (dest / "SKILL.md").write_text("my edits\n")
        messages = install.install_skill(ROOT, [self.home / "skills"], apply=True)
        self.assertEqual((dest / "SKILL.md").read_text(), "my edits\n")
        self.assertIn("differs", messages[0])


class MainTests(_TempHome):
    def fake_clients(self, *names: str) -> str:
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        for name in names:
            stub = bin_dir / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        return f"{bin_dir}:/usr/bin:/bin"

    def snapshot(self) -> list[str]:
        return sorted(str(p.relative_to(self.home)) for p in self.home.rglob("*"))

    def test_dry_run_writes_nothing_and_shows_new_location(self) -> None:
        env = {**os.environ, "HOME": str(self.home), "PATH": self.fake_clients("codex", "claude")}
        before = self.snapshot()
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "install.py")],
                                env=env, capture_output=True, text=True, check=True)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("profile B", result.stdout)
        self.assertIn(str(self.home / ".codex" / "memory-palace" / "cc-integration"), result.stdout)
        self.assertTrue(ROOT.exists())

    def test_no_client_stops(self) -> None:
        env = {**os.environ, "HOME": str(self.home), "PATH": self.fake_clients()}
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "install.py")],
                                env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def test_apply_keep_location_profile_c(self) -> None:
        argv = ["install.py", "--apply", "--keep-location"]
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(install, "detect", return_value=(False, True)), \
                mock.patch.object(install, "pip_install", return_value=[]), \
                mock.patch.object(install, "import_check", return_value=[]), \
                mock.patch.object(install, "ensure_executable", return_value=[]), \
                mock.patch("sys.stdout"):
            self.assertEqual(install.main(), 0)
        hooks = self.read_json(self.home / ".claude" / "settings.json")["hooks"]
        self.assertEqual(set(hooks) - {"PreToolUse"}, {"PreCompact", "SessionEnd", "SessionStart"})
        if shutil.which("jq"):
            self.assertIn("route_session_read.sh", hooks["PreToolUse"][0]["hooks"][0]["command"])
        self.assertIn(install_claude.MCP_NAME, self.read_json(self.home / ".claude.json")["mcpServers"])
        self.assertTrue((self.home / ".claude" / "skills" / "continue-session" / "SKILL.md").exists())
        self.assertFalse((self.home / ".codex").exists())


if __name__ == "__main__":
    unittest.main()
