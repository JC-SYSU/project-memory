"""Strip client-injected noise from visible user/assistant text.

Used by the session browser on top of the carrier whitelist (Codex
Normalizer / Claude Code transcript compiler).  The extraction Normalizer does
not call this module; extraction input is unchanged.

User text: drops whole injected messages (approval-review transcripts, Codex
internal context), unwraps attachment wrappers to the typed request, and
removes injected XML blocks.  Both roles: replace image/base64 payloads with a
short placeholder.  Text that is left empty returns ``None`` (drop message).
"""

from __future__ import annotations

import re

__all__ = ["clean_user_text", "clean_assistant_text"]

_DROP_USER_PREFIXES = (
    "<codex_internal_context",
    "The following is the Codex agent history",
)

_WRAPPER_HEADS = (
    "# Files mentioned by the user:",
    "# Applications mentioned by the user:",
    "# Images mentioned by the user:",
    "# URLs mentioned by the user:",
)
_REQUEST_MARKER = re.compile(r"^## My request(?: for Codex)?:[ \t]*\n?", re.M)

_INJECTED_TAGS = (
    "image", "appshot", "in-app-browser-context", "environment_context",
    "turn_aborted", "system-reminder",
)
_INJECTED_BLOCK = re.compile(
    r"<(%s)\b[^>]*>.*?</\1>|<(?:%s)\b[^>]*/>|</?image\b[^>]*>"
    % ("|".join(map(re.escape, _INJECTED_TAGS)), "|".join(map(re.escape, _INJECTED_TAGS))),
    re.S,
)

_DATA_URI = re.compile(r"data:([a-z]+/[a-z0-9.+-]+);base64,[A-Za-z0-9+/=]{16,}", re.I)
_RAW_BASE64 = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{1000,}={0,2}")


def _strip_payloads(text: str) -> str:
    text = _INJECTED_BLOCK.sub("", text)
    text = _DATA_URI.sub(lambda m: f"[{m.group(1)} base64 已省略]", text)
    return _RAW_BASE64.sub("[base64 已省略]", text)


def _finish(text: str) -> str | None:
    return text if text.strip() else None


def clean_user_text(text: str) -> str | None:
    """Return the typed user request, or None when nothing visible remains."""
    if text.lstrip().startswith(_DROP_USER_PREFIXES):
        return None
    if text.lstrip().startswith(_WRAPPER_HEADS):
        match = _REQUEST_MARKER.search(text)
        if match is None:
            return None  # attachment list with no typed request
        text = text[match.end():]
    return _finish(_strip_payloads(text))


def clean_assistant_text(text: str) -> str | None:
    """Assistant text keeps its prose; only binary payloads are replaced."""
    text = _DATA_URI.sub(lambda m: f"[{m.group(1)} base64 已省略]", text)
    return _finish(_RAW_BASE64.sub("[base64 已省略]", text))