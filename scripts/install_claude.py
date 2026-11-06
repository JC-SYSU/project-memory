#!/usr/bin/env python3
"""Register Project Memory with Claude Code: settings.json hooks + recall MCP server.

Writes two targets (called from install.py; not meant to be run alone):
  ~/.claude/settings.json   "hooks": PreCompact + SessionEnd (capture runner),
                            SessionStart (recall reminder), and optionally
                            PreToolUse (session-read routing, needs jq)
  ~/.claude.json            "mcpServers": memory-palace-recall (stdio wrapper)

Design, same as install_codex.py:
  * idempotent: an entry whose command runs the same script (same file name,
    any path) is left alone, so a second copy of the capture hook is never
    added; an existing memory-palace-recall server is left alone.
  * fail-closed: each target is backed up, written atomically, re-parsed,
    and restored from the backup if the parse fails.
  * apply=False only reports what would change.

~/.claude.json is Claude Code's live state file and Claude Code rewrites it
while sessions run. It is re-read right before the write and replaced in one
os.replace, which keeps the window for a lost update short; running the
installer with no Claude Code session open avoids it entirely.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MCP_NAME = "memory-palace-recall"
_SCRIPT_TOKEN = re.compile(r"[^\s'\"]+\.(?:py|zsh|sh)\b")


def render(template: Path, repo: Path, python: str) -> dict:
    text = template.read_text(encoding="utf-8")
    text = text.replace("{{INSTALL_DIR}}", str(repo)).replace("{{PYTHON}}", python)
    return json.loads(text)


def _script_names(command: str) -> set[str]:
    return {os.path.basename(token) for token in _SCRIPT_TOKEN.findall(command)}


def _backup(target: Path) -> Path | None:
    if not target.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, path)
    return path


def _write_json(target: Path, data: dict) -> Path | None:
    """Back up, write atomically (keeping the file mode), re-parse; restore on failure."""
    backup = _backup(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, target)
        json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        if backup is not None:
            shutil.copy2(backup, target)
        raise SystemExit(f"{target.name}: validation failed, restored backup: {exc}") from exc
    return backup


def _load(target: Path) -> dict:
    if not target.exists():
        return {}
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{target}: top level is not a JSON object, not touching it")
    return data


def merge_hooks(settings: Path, incoming: dict, apply: bool) -> list[str]:
    """Merge hook groups into settings.json["hooks"], skipping scripts already wired."""
    messages: list[str] = []
    current = _load(settings)
    hooks = current.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SystemExit(f"{settings}: \"hooks\" is not an object, not touching it")
    changed = False
    for event, groups in incoming.items():
        existing = hooks.setdefault(event, [])
        wired = {
            name
            for group in existing
            for entry in group.get("hooks", [])
            for name in _script_names(entry.get("command", ""))
        }
        for group in groups:
            for entry in group["hooks"]:
                names = _script_names(entry["command"])
                if names & wired:
                    messages.append(f"settings.json hooks[{event}]: {', '.join(sorted(names))} already registered, skipped")
                    continue
                existing.append({**{k: v for k, v in group.items() if k != "hooks"}, "hooks": [entry]})
                wired |= names
                changed = True
                messages.append(f"settings.json hooks[{event}]: register {entry['command']}")
    if not changed:
        return messages + ["settings.json: nothing to change"]
    if not apply:
        return messages + ["--apply not given; settings.json not written"]
    backup = _write_json(settings, current)
    return messages + [f"settings.json written (backup: {backup or 'new file'})"]


def merge_mcp(claude_json: Path, server: dict, apply: bool) -> list[str]:
    """Add mcpServers.memory-palace-recall to ~/.claude.json unless a server of that name exists."""
    current = _load(claude_json)
    existing = current.get("mcpServers", {}).get(MCP_NAME)
    if existing is not None:
        return [f"~/.claude.json mcpServers.{MCP_NAME}: already present "
                f"(command {existing.get('command', '?')}), skipped"]
    if not apply:
        return [f"~/.claude.json mcpServers.{MCP_NAME}: would add {server['command']}",
                "--apply not given; ~/.claude.json not written"]
    current = _load(claude_json)  # re-read: Claude Code may have rewritten it meanwhile
    current.setdefault("mcpServers", {})[MCP_NAME] = server
    backup = _write_json(claude_json, current)
    return [f"~/.claude.json mcpServers.{MCP_NAME}: added {server['command']} "
            f"(backup: {backup or 'new file'})"]


def install(templates: Path, repo: Path, python: str, home: Path, apply: bool,
            route_hook: bool) -> list[str]:
    """templates: where to read the templates; repo: the install dir written into commands."""
    base = render(templates / "claude.example.json", repo, python)
    hooks = dict(base["hooks"])
    messages: list[str] = []
    if route_hook:
        if shutil.which("jq"):
            hooks.update(render(templates / "claude.settings.hooks.example.json", repo, python)["hooks"])
        else:
            messages.append("route hook: jq not found, PreToolUse routing hook skipped "
                            "(install jq and re-run to add it)")
    messages += merge_hooks(home / ".claude" / "settings.json", hooks, apply)
    messages += merge_mcp(home / ".claude.json", base["mcpServers"][MCP_NAME], apply)
    return messages
