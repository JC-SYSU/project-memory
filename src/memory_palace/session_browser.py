"""Read-only browser over a project's fresh raw sessions (Codex + Claude Code).

Sources are the clients' own transcript files, never palace Captures:

- Claude Code: ``~/.claude/projects/<encoded cwd>/<uuid>.jsonl``; the cwd
  recorded inside the file must equal the project path exactly.
- Codex: ``~/.codex/sessions/**`` and ``~/.codex/archived_sessions/**``, both
  ``.jsonl`` and Codex-compressed ``.jsonl.zst``; ``session_meta.cwd`` must
  equal the project path exactly.  A Codex thread can span several segment
  files (``<id>.jsonl`` plus ``<id>_<segment>.jsonl``); they share one session
  id and are read in timestamp order as one conversation.

Only visible user and assistant text is returned (see ``visible_text``).
Sessions the palace processed earlier whose raw files are gone are reported
as unavailable; their content is never reconstructed from Captures.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import quote

from .claude_code_transcript import CompileError, compile_visible_messages
from .codex_normalizer import NormalizationError, normalize_capture
from .project_paths import ProjectPathError, project_paths_for_cwd, read_session_metadata
from .rollout_io import ROLLOUT_SUFFIXES
from .visible_text import clean_assistant_text, clean_user_text

__all__ = [
    "SessionBrowserError",
    "SessionInfo",
    "Message",
    "list_sessions",
    "read_session",
]

CC_PREFIX = "claude-code-v1:"
CHATGPT_PREFIX = "chatgpt-web-"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TITLE_CHARS = 60


class SessionBrowserError(ValueError):
    """Fixed safe codes: unknown_session, ambiguous_session, session_unavailable, invalid_argument."""


@dataclass(frozen=True, slots=True)
class Message:
    timestamp: str
    role: str
    text: str


@dataclass(slots=True)
class SessionInfo:
    session_id: str
    client: str  # "codex" | "claude-code"
    available: bool
    files: list[str] = field(default_factory=list)
    title: str = ""
    started_at: str = ""
    updated_at: str = ""
    message_count: int = 0
    user_message_count: int = 0
    chars: int = 0
    unavailable_reason: str = ""

    def as_dict(self) -> dict:
        data = {
            "session_id": self.session_id,
            "client": self.client,
            "available": self.available,
            "title": self.title,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "user_message_count": self.user_message_count,
            "chars": self.chars,
        }
        if not self.available:
            data["unavailable_reason"] = self.unavailable_reason
        return data


# ---------------------------------------------------------------- discovery

def _home() -> str:
    return os.path.expanduser("~")


def _cc_project_dir(project: str) -> str:
    return os.path.join(_home(), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", project))


def _cc_file_cwd(path: str) -> str | None:
    """First cwd field in a Claude Code transcript (appears in the first few lines)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index > 200:
                    break
                if '"cwd"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _discover_claude_code(project: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in glob.glob(os.path.join(_cc_project_dir(project), "*.jsonl")):
        session_uuid = os.path.basename(path)[: -len(".jsonl")]
        if not _UUID.fullmatch(session_uuid):
            continue
        # The directory encoding is lossy (/, ., _ and - all become -), so the
        # recorded cwd decides membership.
        if _cc_file_cwd(path) != project:
            continue
        found[session_uuid] = [path]
    return found


def _codex_files() -> list[str]:
    paths: list[str] = []
    for root in ("sessions", "archived_sessions"):
        base = os.path.join(_home(), ".codex", root)
        for suffix in ROLLOUT_SUFFIXES:
            paths.extend(glob.glob(os.path.join(base, "**", f"*{suffix}"), recursive=True))
    return paths


def _discover_codex(project: str) -> dict[str, list[str]]:
    by_id: dict[str, dict[str, str]] = {}
    for path in _codex_files():
        try:
            meta = read_session_metadata(path)
        except ProjectPathError:
            continue
        if meta.cwd != project:
            continue
        # Same segment can exist as live .jsonl and archived .zst; prefer plain.
        segment = os.path.basename(path)
        for suffix in (".zst",):
            if segment.endswith(suffix):
                segment = segment[: -len(suffix)]
        current = by_id.setdefault(meta.session_id, {}).get(segment)
        if current is None or (current.endswith(".zst") and not path.endswith(".zst")):
            by_id[meta.session_id][segment] = path
    # Segment file names start with rollout-<start time>, so name order is time order.
    return {sid: [segments[k] for k in sorted(segments)] for sid, segments in by_id.items()}


def _palace_known_sessions(project: str) -> dict[str, str]:
    """source_session_id -> client for sessions the palace processed before."""
    database = project_paths_for_cwd(project).database_path
    if not os.path.exists(database):
        return {}
    uri = "file:" + quote(database, safe="/:") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            rows = conn.execute("SELECT DISTINCT source_session_id FROM extraction_jobs").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    known: dict[str, str] = {}
    for (sid,) in rows:
        if sid.startswith(CC_PREFIX):
            known[sid[len(CC_PREFIX):]] = "claude-code"
        elif not sid.startswith(CHATGPT_PREFIX):
            known[sid] = "codex"
    return known


# ----------------------------------------------------------------- reading

def _clean(role: str, text: str) -> str | None:
    return clean_user_text(text) if role == "user" else clean_assistant_text(text)


def _load_messages(client: str, files: list[str]) -> list[Message]:
    raw: list[tuple[str, str, str]] = []
    for path in files:
        if client == "claude-code":
            raw.extend(compile_visible_messages(path))
        else:
            raw.extend((m.timestamp, m.role, m.body) for m in normalize_capture(path))
    messages: list[Message] = []
    for timestamp, role, text in raw:
        cleaned = _clean(role, text)
        if cleaned is not None:
            messages.append(Message(timestamp, role, cleaned))
    return messages


def _summarize(info: SessionInfo, messages: list[Message]) -> None:
    info.message_count = len(messages)
    info.user_message_count = sum(1 for m in messages if m.role == "user")
    info.chars = sum(len(m.text) for m in messages)
    if messages:
        info.started_at = messages[0].timestamp
        info.updated_at = messages[-1].timestamp
    first = next((m for m in messages if m.role == "user"), messages[0] if messages else None)
    if first is not None:
        line = " ".join(first.text.split())
        info.title = line[:_TITLE_CHARS] + ("…" if len(line) > _TITLE_CHARS else "")


def _collect(project: str) -> dict[str, SessionInfo]:
    sessions: dict[str, SessionInfo] = {}
    for sid, files in _discover_claude_code(project).items():
        sessions[sid] = SessionInfo(sid, "claude-code", True, files)
    for sid, files in _discover_codex(project).items():
        sessions[sid] = SessionInfo(sid, "codex", True, files)
    for sid, client in _palace_known_sessions(project).items():
        if sid not in sessions:
            sessions[sid] = SessionInfo(
                sid, client, False, unavailable_reason="raw_transcript_missing"
            )
    return sessions


def list_sessions(project: str, *, include_empty: bool = False) -> list[SessionInfo]:
    """All sessions of ``project``, newest first; unavailable ones are kept and flagged."""
    result: list[SessionInfo] = []
    for info in _collect(project).values():
        if info.available:
            try:
                _summarize(info, _load_messages(info.client, info.files))
            except (OSError, NormalizationError, CompileError, UnicodeDecodeError):
                info.available = False
                info.unavailable_reason = "raw_transcript_unreadable"
            if info.available and info.message_count == 0 and not include_empty:
                continue
        result.append(info)
    result.sort(key=lambda s: (s.updated_at or "", s.session_id), reverse=True)
    return result


def _resolve(sessions: dict[str, SessionInfo], session_id: str) -> SessionInfo:
    wanted = session_id.strip()
    if wanted.startswith(CC_PREFIX):
        wanted = wanted[len(CC_PREFIX):]
    if not wanted or not re.fullmatch(r"[0-9a-f-]+", wanted):
        raise SessionBrowserError("invalid_argument")
    if wanted in sessions:
        return sessions[wanted]
    matches = [sid for sid in sessions if sid.startswith(wanted)]
    if len(wanted) >= 8 and len(matches) == 1:
        return sessions[matches[0]]
    raise SessionBrowserError("ambiguous_session" if len(matches) > 1 else "unknown_session")


def read_session(
    project: str,
    session_id: str,
    *,
    offset: int = 0,
    max_messages: int = 60,
    max_chars: int = 20_000,
) -> dict:
    """One page of a session's clean conversation.

    A page holds whole messages and stops before exceeding ``max_chars``.  A
    single message longer than ``max_chars`` is returned alone and truncated,
    with ``truncated_message`` set, so reading always makes progress.
    """
    if offset < 0 or max_messages < 1 or max_chars < 1:
        raise SessionBrowserError("invalid_argument")
    info = _resolve(_collect(project), session_id)
    if not info.available:
        raise SessionBrowserError("session_unavailable")
    try:
        messages = _load_messages(info.client, info.files)
    except (OSError, NormalizationError, CompileError, UnicodeDecodeError):
        raise SessionBrowserError("session_unavailable") from None
    _summarize(info, messages)

    page: list[dict] = []
    chars = 0
    truncated = False
    index = offset
    while index < len(messages) and len(page) < max_messages:
        message = messages[index]
        text = message.text
        if page and chars + len(text) > max_chars:
            break
        if not page and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        page.append({"index": index, "timestamp": message.timestamp, "role": message.role, "text": text})
        chars += len(text)
        index += 1
        if truncated:
            break
    return {
        "session": info.as_dict(),
        "offset": offset,
        "next_offset": index if index < len(messages) else None,
        "truncated_message": truncated,
        "messages": page,
    }