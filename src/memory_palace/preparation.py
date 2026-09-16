"""Pipeline composition for deterministic Codex Capture preparation."""

from __future__ import annotations

from os import PathLike

from .codex_normalizer import normalize_capture
from .contracts import Window
from .segmenter import segment


def prepare_codex_capture(
    path: str | PathLike[str],
    *,
    start_line: int = 1,
    soft_target_chars: int = 60_000,
) -> list[Window]:
    """Prepare a Codex Capture JSONL file into segmented windows.
    
    This is the narrow composition interface that combines normalization
    and segmentation without duplicating their logic.
    
    Args:
        path: Path to the Codex Capture JSONL file
        start_line: First source line to include (inclusive)
        soft_target_chars: Target character count per window
        
    Returns:
        List of windows, each containing messages and metadata
        
    Raises:
        NormalizationError: If JSONL parsing fails
        ValueError: If start_line < 1
    """
    messages = normalize_capture(path, start_line=start_line)
    return segment(
        messages,
        start_line=start_line,
        soft_target_chars=soft_target_chars,
    )
