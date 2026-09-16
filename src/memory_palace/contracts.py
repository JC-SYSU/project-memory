"""Shared data contracts for the deterministic preparation pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    """One retained visible message and its original physical source line."""

    source_line: int
    timestamp: str
    role: str
    body: str

    def as_dict(self) -> dict[str, Any]:
        """Return the four-field normalized representation."""
        return {
            "source_line": self.source_line,
            "timestamp": self.timestamp,
            "role": self.role,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class Window:
    """A segment of messages within the soft target character limit."""

    messages: tuple[NormalizedMessage, ...]
    consumed_through_line: int
    body_chars: int
