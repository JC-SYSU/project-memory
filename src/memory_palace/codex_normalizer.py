"""Deterministic adapter for the frozen Codex Capture JSONL contract."""

from __future__ import annotations

import json
from os import PathLike
from typing import Any

from .contracts import NormalizedMessage
from .rollout_io import open_rollout_text


class NormalizationError(ValueError):
    """A JSONL parsing error with its contract-level kind and source line."""

    def __init__(self, kind: str, source_line: int) -> None:
        self.kind = kind
        self.source_line = source_line
        super().__init__(f"{kind} at source line {source_line}")


def normalize_capture(
    path: str | PathLike[str], start_line: int = 1
) -> tuple[NormalizedMessage, ...]:
    """Normalize one UTF-8 Codex Capture JSONL file.

    Only the two frozen visible carriers are retained.  A malformed JSON line
    raises ``NormalizationError`` before any result is returned.
    """

    if start_line < 1:
        raise ValueError("start_line must be at least 1")

    with open_rollout_text(path) as capture:
        text = capture.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    last_nonempty_line = next(
        (line_number for line_number in range(len(lines), 0, -1) if lines[line_number - 1].strip()),
        0,
    )

    parsed: list[tuple[int, Any]] = []
    for source_line, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue

        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            kind = "incomplete_tail" if source_line == last_nonempty_line else "malformed_line"
            error = NormalizationError(kind, source_line)
            error.__cause__ = exc
            raise error from exc
        parsed.append((source_line, record))

    # Codex 0.148 起不再写 event_msg/user_message，用户正文改由
    # item_completed/UserMessage 承载。两种载体在同一文件里从未同时出现
    # （2026-11-05 全机 425 个 Codex Capture 实测）；仍按整文件只选一种，
    # 保证同一句话不会因格式过渡被重复收录。
    use_item_user = not any(
        isinstance(record, dict) and _is_legacy_user_message(record)
        for _, record in parsed
    )

    normalized: list[NormalizedMessage] = []
    for source_line, record in parsed:
        if source_line < start_line or not isinstance(record, dict):
            continue

        message = _normalize_record(record, source_line, use_item_user=use_item_user)
        if message is not None:
            normalized.append(message)

    return tuple(normalized)


def _is_legacy_user_message(record: dict[str, Any]) -> bool:
    payload = record.get("payload")
    return (
        record.get("type") == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "user_message"
    )


def _user_body(message: str) -> str | None:
    """Shared exclusions for user text, whichever carrier it came from."""

    if not message:
        return None
    if message.lstrip().startswith("<codex_internal_context"):
        return None
    if message.startswith(
        "The following is the Codex agent history whose request action you are assessing."
    ):
        return None
    return message


def _item_user_text(item: dict[str, Any]) -> str:
    """Join visible text blocks of a UserMessage item.

    Images and skill blocks are multimedia/injection carriers (§3.4) and are
    skipped.  ``<in-app-browser-context`` is auto-generated UI state that the
    legacy carrier never contained, so it is dropped to keep both carriers
    equivalent.
    """

    content = item.get("content")
    if not isinstance(content, list):
        return ""
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
        and not block["text"].lstrip().startswith("<in-app-browser-context")
    ]
    return "\n".join(texts)


def _normalize_record(
    record: dict[str, Any], source_line: int, *, use_item_user: bool = False
) -> NormalizedMessage | None:
    """Apply the frozen carrier whitelist to one already-parsed record."""

    timestamp = record.get("timestamp")
    payload = record.get("payload")
    if not isinstance(timestamp, str) or not isinstance(payload, dict):
        return None

    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        if not isinstance(message, str):
            return None
        body = _user_body(message)
        if body is None:
            return None
        return NormalizedMessage(source_line, timestamp, "user", body)

    if (
        use_item_user
        and record.get("type") == "event_msg"
        and payload.get("type") == "item_completed"
    ):
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "UserMessage":
            return None
        body = _user_body(_item_user_text(item))
        if body is None:
            return None
        return NormalizedMessage(source_line, timestamp, "user", body)

    if (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    ):
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        output_text = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "output_text"
            and isinstance(block.get("text"), str)
            and block["text"]
        ]
        if not output_text:
            return None
        return NormalizedMessage(source_line, timestamp, "assistant", "\n".join(output_text))

    return None
