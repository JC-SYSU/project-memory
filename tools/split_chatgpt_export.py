#!/usr/bin/env python3
"""Split a ChatGPT web-export chat.html into one Markdown transcript per conversation.

It follows the export viewer's own `getConversationMessages` logic: only the
ancestry of `current_node` is rendered, while abandoned/regenerated branches
remain in the source export and are counted in the generated manifest.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    CHINA_TIME = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows ships no IANA tzdata; fall back to UTC+8
    from datetime import timedelta

    CHINA_TIME = timezone(timedelta(hours=8))
TEXT_CONTENT_TYPES = {"text", "multimodal_text"}
INVALID_FILENAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def parse_embedded_json(html_path: Path, variable: str) -> Any:
    """Load JSON assigned to a JavaScript variable without interpreting JS."""
    source = html_path.read_text(encoding="utf-8")
    marker = f"var {variable} = "
    try:
        start = source.index(marker) + len(marker)
    except ValueError as exc:
        raise ValueError(f"Could not find `{marker}` in {html_path}") from exc
    value, _ = json.JSONDecoder().raw_decode(source, start)
    return value


def local_time(timestamp: Any) -> str | None:
    if not isinstance(timestamp, (int, float)):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(CHINA_TIME).isoformat()


def safe_filename(title: Any) -> str:
    text = WHITESPACE.sub(" ", str(title or "untitled")).strip()
    text = INVALID_FILENAME.sub("-", text).strip(" .-")
    return (text or "untitled")[:80]


def markdown_text(value: str) -> str:
    return value.rstrip() + "\n"


def rendered_messages(conversation: dict[str, Any]) -> tuple[list[dict[str, Any]], int, bool]:
    """Return messages shown by the export HTML, oldest first."""
    mapping = conversation.get("mapping") or {}
    node_id = conversation.get("current_node")
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    while node_id is not None and node_id not in seen and node_id in mapping:
        seen.add(node_id)
        node = mapping[node_id]
        nodes.append(node)
        node_id = node.get("parent")

    children = Counter(node.get("parent") for node in mapping.values() if node.get("parent"))
    has_alternate_branch = any(count > 1 for count in children.values())

    messages: list[dict[str, Any]] = []
    for node in reversed(nodes):
        message = node.get("message")
        if not isinstance(message, dict) or not message.get("id"):
            continue
        content = message.get("content") or {}
        metadata = message.get("metadata") or {}
        role = (message.get("author") or {}).get("role")
        if role == "system" and not metadata.get("is_user_system_message"):
            continue
        if content.get("content_type") not in TEXT_CONTENT_TYPES:
            continue
        parts = content.get("parts") or []
        if not parts:
            continue

        rendered_parts: list[str] = []
        for part in parts:
            if isinstance(part, str) and part:
                rendered_parts.append(part)
            elif isinstance(part, dict) and part.get("content_type") == "audio_transcription" and part.get("text"):
                rendered_parts.append(f"[Transcript]: {part['text']}")
        if not rendered_parts:
            continue

        author = {"assistant": "ChatGPT", "tool": "ChatGPT", "system": "Custom user info"}.get(role or "", role or "Unknown")
        messages.append(
            {
                "id": message["id"],
                "author": author,
                "created_at": local_time(message.get("create_time")),
                "parts": rendered_parts,
            }
        )
    return messages, len(nodes), has_alternate_branch


def write_conversation(
    destination: Path,
    conversation: dict[str, Any],
    assets: dict[str, list[str]],
    source_path: Path,
) -> dict[str, Any]:
    messages, active_node_count, has_alternate_branch = rendered_messages(conversation)
    create_time = local_time(conversation.get("create_time"))
    title = str(conversation.get("title") or "untitled")

    lines = [
        "---",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"conversation_id: {json.dumps(conversation.get('id') or conversation.get('conversation_id'))}",
        f"created_at: {json.dumps(create_time)}",
        f"source_file: {json.dumps(str(source_path))}",
        'rendering_policy: "current_node ancestry, matching ChatGPT export viewer"',
        f"alternate_branches_omitted: {'true' if has_alternate_branch else 'false'}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    attached_files = 0
    for message in messages:
        lines.extend([f"## {message['author']}", ""])
        if message["created_at"]:
            lines.extend([f"<!-- message_id: {message['id']}; created_at: {message['created_at']} -->", ""])
        for part in message["parts"]:
            lines.extend([markdown_text(part), ""])
        files = list(dict.fromkeys(assets.get(message["id"], [])))
        if files:
            attached_files += len(files)
            lines.append("**Attached export files:** " + ", ".join(f"`{name}`" for name in files))
            lines.append("")

    destination.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {
        "file": destination.name,
        "conversation_id": conversation.get("id") or conversation.get("conversation_id"),
        "title": title,
        "created_at": create_time,
        "messages": len(messages),
        "active_nodes": active_node_count,
        "alternate_branches_omitted": has_alternate_branch,
        "attached_files_referenced": attached_files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_html", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--overwrite", action="store_true", help="replace an existing output directory")
    args = parser.parse_args()

    source = args.chat_html.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if not source.is_file():
        parser.error(f"not a file: {source}")
    if output.exists():
        if not args.overwrite:
            parser.error(f"output already exists: {output} (use --overwrite to replace it)")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    conversations = parse_embedded_json(source, "jsonData")
    assets = parse_embedded_json(source, "assetsJson")
    if not isinstance(conversations, list) or not isinstance(assets, dict):
        raise ValueError("Unexpected ChatGPT web-export data shape")

    records: list[dict[str, Any]] = []
    sessions = output / "sessions"
    sessions.mkdir()
    for position, conversation in enumerate(conversations, start=1):
        if not isinstance(conversation, dict):
            continue
        day = local_time(conversation.get("create_time")) or "undated"
        day = day[:10]
        identifier = str(conversation.get("id") or conversation.get("conversation_id") or f"conversation-{position}").split("-")[0]
        filename = f"{position:04d}_{day}_{safe_filename(conversation.get('title'))}_{identifier}.md"
        records.append(write_conversation(sessions / filename, conversation, assets, source))

    total_messages = sum(item["messages"] for item in records)
    total_files = sum(item["attached_files_referenced"] for item in records)
    branch_count = sum(item["alternate_branches_omitted"] for item in records)
    manifest = {
        "source_file": str(source),
        "source_size_bytes": source.stat().st_size,
        "conversation_count": len(records),
        "rendered_message_count": total_messages,
        "conversations_with_alternate_branches": branch_count,
        "attachment_references": total_files,
        "rendering_policy": "current_node ancestry, matching ChatGPT export viewer",
        "sessions": records,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    index = ["# ChatGPT web-export session index", "", f"- Source: `{source}`", f"- Conversations: {len(records)}", f"- Rendered messages: {total_messages}", f"- Conversations with alternate branches excluded: {branch_count}", f"- Attachment references retained: {total_files}", "", "| # | Created (Asia/Shanghai) | Title | Messages | Transcript |", "| ---: | --- | --- | ---: | --- |"]
    for number, item in enumerate(records, start=1):
        title = item["title"].replace("|", "\\|").replace("\n", " ")
        index.append(f"| {number} | {item['created_at'] or ''} | {title} | {item['messages']} | [open](sessions/{item['file']}) |")
    (output / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    readme = f"""# ChatGPT web-export Markdown backup

Generated from `{source}`.

- One Markdown file per conversation: `sessions/`
- Index: `INDEX.md`
- Machine-readable manifest: `manifest.json`
- Rendered messages: {total_messages} across {len(records)} conversations

## Fidelity boundary

This converter deliberately mirrors the viewer embedded in `chat.html`: each transcript follows the selected `current_node` back through its parents, then reverses it into chronological order. It includes only `text` and `multimodal_text` message content, omits system messages unless the viewer labels them “Custom user info”, and retains audio transcriptions when present. The source has {branch_count} conversations with alternate branches; those branches are intentionally not rendered here, but remain preserved in the original export.

Attachment filenames are retained beneath the associated message. Files themselves are not copied, so their source directory must be retained if they are needed later.
"""
    (output / "README.md").write_text(readme, encoding="utf-8")
    print(json.dumps({k: manifest[k] for k in manifest if k != "sessions"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
