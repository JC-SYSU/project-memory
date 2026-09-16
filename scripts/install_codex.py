#!/usr/bin/env python3
"""Register Project Memory with Codex: hooks.json + recall MCP server.

Writes two targets:
  ~/.codex/hooks.json     merges PreCompact and UserPromptSubmit entries
                          (recall routing prompt is injected by the runner)
  ~/.codex/config.toml    appends [mcp_servers.memory_palace_recall]

Design:
  * idempotent: an existing entry with the same command is left untouched;
    an existing memory_palace_recall stanza prevents re-append.
  * fail-closed: every target is backed up before modification; the written
    file is re-parsed (json / tomllib) and the previous version is restored
    if validation fails.
  * --apply writes; without it the script only prints what would change.

After --apply, Codex will ask to trust the new hooks (see /hooks).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from tomllib import load

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "templates"
PYTHON = sys.executable
CODEX_HOME = Path.home() / ".codex"
HOOKS = CODEX_HOME / "hooks.json"
CONFIG = CODEX_HOME / "config.toml"


def render(template_name: str) -> str:
    source = (TEMPLATES / template_name).read_text(encoding="utf-8")
    return source.replace("{{INSTALL_DIR}}", str(REPO)).replace("{{PYTHON}}", PYTHON)


def backup(target: Path) -> str | None:
    """Copy target aside; returns the backup path, or None when target is new."""
    if not target.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, path)
    return str(path)


def merge_hooks(apply: bool) -> tuple[list[str], str | None]:
    """Merge the two hook events into the user hooks.json.

    Returns (messages, rendered template). An existing identical command
    under the same event+matcher is skipped.
    """
    rendered = render("hooks.example.json")
    incoming = json.loads(rendered)["hooks"]
    messages: list[str] = []

    if HOOKS.exists():
        current = json.loads(HOOKS.read_text(encoding="utf-8"))
    else:
        current = {"hooks": {}}
    current.setdefault("hooks", {})
    changed = False
    for event, groups in incoming.items():
        existing_groups = current["hooks"].setdefault(event, [])
        for group in groups:
            command = group["hooks"][0]["command"]
            old_group = next(
                (g for g in existing_groups if g.get("matcher") == group.get("matcher")),
                None,
            )
            if old_group is not None and any(
                entry["command"] == command for entry in old_group["hooks"]
            ):
                messages.append(f"hooks[{event}]: already registered, skipped")
                continue
            if old_group is None:
                existing_groups.append({**group, "hooks": list(group["hooks"])})
                messages.append(f"hooks[{event}]: registered {command}")
            else:
                old_group["hooks"].append(group["hooks"][0])
                messages.append(f"hooks[{event}]: registered {command}")
            changed = True

    if not changed:
        messages.append("hooks.json: nothing to change")
        return messages, None
    if not apply:
        messages.append("--apply not given; hooks.json not written")
        return messages, None

    backup_path = backup(HOOKS)
    payload = json.dumps(current, ensure_ascii=False, indent=2) + "\n"
    try:
        HOOKS.write_text(payload, encoding="utf-8")
        json.loads(HOOKS.read_text(encoding="utf-8"))  # validate
    except Exception as exc:
        if backup_path is not None:
            shutil.copy2(backup_path, HOOKS)
        raise SystemExit(f"hooks.json validation failed, restored: {exc}") from exc
    messages.append(f"hooks.json written (backup: {backup_path or 'new file'})")
    return messages, None


def merge_config(apply: bool) -> list[str]:
    """Append the recall MCP stanza to ~/.codex/config.toml if absent."""
    rendered = render("config.mcp.example.toml")
    messages: list[str] = []

    if CONFIG.exists():
        with CONFIG.open("rb") as handle:
            current = load(handle)
        if "memory_palace_recall" in current.get("mcp_servers", {}):
            messages.append("config.toml: [mcp_servers.memory_palace_recall] already present, skipped")
            return messages
    else:
        current = {}

    if not apply:
        messages.append("--apply not given; config.toml not written")
        return messages

    backup_path = backup(CONFIG)
    separator = "\n" if CONFIG.exists() else ""
    with CONFIG.open("a", encoding="utf-8") as handle:
        handle.write(separator + rendered + "\n")
    try:
        with CONFIG.open("rb") as handle:
            load(handle)  # validate TOML
    except Exception as exc:
        if backup_path is not None:
            shutil.copy2(backup_path, CONFIG)
        raise SystemExit(f"config.toml validation failed, restored: {exc}") from exc
    messages.append(
        f"config.toml: [mcp_servers.memory_palace_recall] appended (backup: {backup_path or 'new file'})"
    )
    return messages


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--hooks-path", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=None)
    args = parser.parse_args()

    global HOOKS, CONFIG
    HOOKS = args.hooks_path or HOOKS
    CONFIG = args.config_path or CONFIG

    messages, _ = merge_hooks(args.apply)
    messages += merge_config(args.apply)
    print("\n".join(messages))
    if not args.apply:
        print("\nRun with --apply to write. Codex will ask to re-trust hooks afterwards (/hooks).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())