"""Deterministic adapter for the frozen Codex Capture JSONL contract."""

from __future__ import annotations

import json
from os import PathLike
from pathlib import Path
from typing import Any

from .contracts import NormalizedMessage


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

    with Path(path).open("r", encoding="utf-8", newline="") as capture:
        text = capture.read()
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    last_nonempty_line = next(
        (line_number for line_number in range(len(lines), 0, -1) if lines[line_number - 1].strip()),
        0,
    )
    normalized: list[NormalizedMessage] = []

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

        if source_line < start_line or not isinstance(record, dict):
            continue

        message = _normalize_record(record, source_line)
        if message is not None:
            normalized.append(message)

    return tuple(normalized)


def _normalize_record(record: dict[str, Any], source_line: int) -> NormalizedMessage | None:
    """Apply the frozen carrier whitelist to one already-parsed record."""

    timestamp = record.get("timestamp")
    payload = record.get("payload")
    if not isinstance(timestamp, str) or not isinstance(payload, dict):
        return None

    if record.get("type") == "event_msg" and payload.get("type") == "user_message":
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            return None
        if message.lstrip().startswith("<codex_internal_context"):
            return None
        if message.startswith(
            "The following is the Codex agent history whose request action you are assessing."
        ):
            return None
        return NormalizedMessage(source_line, timestamp, "user", message)

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
