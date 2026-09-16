"""Compile one Claude Code session JSONL into the frozen Codex-shaped Capture contract.

纯逐行函数：每条 CC 源行至多产出一条 Capture 消息行，历史行永不改写，
因此输出对 append-only 增长的输入保持前缀稳定，兼容宫殿 §3.3 游标契约。

白名单（全部由 2026-09-06 对 ~/.claude/projects 全量真实样本的普查背书）：
- type=="user"  ∧ origin.kind=="human" ∧ 非 isMeta/isSidechain
  - content 为 str：原样收录；以命令包装标签开头的排除
    （<command-name> ×357、<local-command-caveat>/<local-command-stdout> ×10、<bash-input> ×5）
  - content 为 list：仅提取 text 块并换行拼接（真实存在 human 文本+图片混排 ×14），
    图片块按 §3.4 多媒体排除
- type=="assistant" ∧ 非 isSidechain：每个 text 块产出一行；thinking/tool_use 排除

排除的噪声族（实测计数）：tool_result 伪装的 user 行、isMeta 注入 ×323、
task-notification ×318、attachment 全族、system 全族（含 compact_boundary ×154）、
sdk ×20、sidechain 子代理内部。未见于语料的规则一律不写（蓝图最小排除纪律）。

末行 JSON 解析失败视为未写完的尾巴：丢弃并停止（下次事件自然补齐）。
中间行损坏抛 CompileError，由上层 fail-open 处理，绝不产出半成品。
"""

from __future__ import annotations

import json
from os import PathLike
from typing import Any

SOURCE_SESSION_PREFIX = "claude-code-v1:"

#: 已验证的 CC 命令/输出包装标签：以这些开头的可见用户文本是壳，不是人话。
#: <command-message> 变体由 2026-09-06 全量侧漏审计实证（6 条技能调用壳）。
_EXCLUDED_USER_PREFIXES = (
    "<command-name>",
    "<command-message>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<bash-input>",
)


class CompileError(ValueError):
    """A transcript line violates the compile contract (kind + source line)."""

    def __init__(self, kind: str, source_line: int) -> None:
        self.kind = kind
        self.source_line = source_line
        super().__init__(f"{kind} at source line {source_line}")


def dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def source_session_id_for(cc_session_id: str) -> str:
    """Namespaced so palace cursors/records never collide with Codex uuids."""
    return SOURCE_SESSION_PREFIX + cc_session_id


def session_meta_line(*, source_session_id: str, cwd: str, timestamp: str) -> str:
    return dumps(
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": source_session_id, "cwd": cwd},
        }
    )


def _user_payload(record: dict[str, Any]) -> str | None:
    """Return the visible user text, or None when the line is not admitted."""
    if record.get("isSidechain") or record.get("isMeta"):
        return None
    origin = record.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "human":
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped or stripped.startswith(_EXCLUDED_USER_PREFIXES):
            return None
        return content
    if isinstance(content, list):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ]
        if not texts:
            return None
        joined = "\n".join(texts).strip()
        if not joined or joined.startswith(_EXCLUDED_USER_PREFIXES):
            return None
        return "\n".join(texts)
    return None


def _assistant_payloads(record: dict[str, Any]) -> list[str]:
    """One entry per visible assistant text block in this line."""
    if record.get("isSidechain"):
        return []
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"]
    ]


def compile_visible_messages(transcript_path: str | PathLike[str]) -> list[tuple[str, str, str]]:
    """Read a live Claude Code transcript and return (timestamp, role, text) tuples.

    Malformed non-final JSON raises CompileError; a malformed final line is
    treated as an unwritten tail, dropped, and parsing stops there.
    """
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    last_nonempty = next(
        (n for n in range(len(lines), 0, -1) if lines[n - 1].strip()), 0
    )

    messages: list[tuple[str, str, str]] = []
    for source_line, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            if source_line == last_nonempty:
                break  # unwritten tail: drop, remain prefix-stable
            raise CompileError("malformed_line", source_line) from exc
        if not isinstance(record, dict):
            continue
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        record_type = record.get("type")
        if record_type == "user":
            visible = _user_payload(record)
            if visible is not None:
                messages.append((timestamp, "user", visible))
        elif record_type == "assistant":
            for block_text in _assistant_payloads(record):
                messages.append((timestamp, "assistant", block_text))
    return messages


def build_capture_lines(
    messages: list[tuple[str, str, str]],
    *,
    source_session_id: str,
    cwd: str,
    meta_timestamp: str,
) -> list[str]:
    """Assemble the full Codex-shaped Capture JSONL body (header first)."""
    capture = [
        session_meta_line(
            source_session_id=source_session_id, cwd=cwd, timestamp=meta_timestamp
        )
    ]
    for timestamp, role, body in messages:
        if role == "user":
            capture.append(
                dumps(
                    {
                        "timestamp": timestamp,
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": body},
                    }
                )
            )
        else:
            capture.append(
                dumps(
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": body}],
                        },
                    }
                )
            )
    return capture
