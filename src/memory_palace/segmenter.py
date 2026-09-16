"""Minimal Segmenter implementation for deterministic preparation pipeline."""

from typing import Sequence

from .contracts import NormalizedMessage, Window


def segment(
    messages: Sequence[NormalizedMessage],
    *,
    start_line: int,
    soft_target_chars: int = 60_000,
) -> list[Window]:
    """Segment messages into windows starting from start_line.

    Cuts only at message boundaries. Cuts when adding the next message
    would make body_chars strictly greater than soft_target_chars.
    """
    windows = []
    current_messages = []
    current_chars = 0
    
    for msg in messages:
        if msg.source_line < start_line:
            continue
        
        if current_messages:
            new_chars = current_chars + len(msg.body)
            if new_chars > soft_target_chars:
                windows.append(Window(
                    messages=tuple(current_messages),
                    consumed_through_line=current_messages[-1].source_line,
                    body_chars=current_chars,
                ))
                current_messages = []
                current_chars = 0
        
        current_messages.append(msg)
        current_chars += len(msg.body)
    
    if current_messages:
        windows.append(Window(
            messages=tuple(current_messages),
            consumed_through_line=current_messages[-1].source_line,
            body_chars=current_chars,
        ))
    
    return windows
