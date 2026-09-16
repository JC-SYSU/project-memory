"""Project-local paths and Codex session metadata for PPR-01."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from os import PathLike
from urllib.parse import quote

from .worker_orchestrator import initialize_worker_database

__all__ = [
    "ProjectPathError",
    "ProjectPaths",
    "SessionMetadata",
    "project_paths_for_cwd",
    "read_session_metadata",
    "ensure_project_state",
    "validate_existing_database",
]

_STATE_DIRECTORY = ".memory-palace"
_DATABASE_NAME = "memory-palace.sqlite3"
_CAPTURES_DIRECTORY = "captures"
_REQUIRED_TABLES = frozenset(
    {
        "records",
        "evidence",
        "review_items",
        "session_cursor",
        "model_usage",
        "audit_events",
        "window_commit_markers",
        "extraction_jobs",
    }
)


class ProjectPathError(RuntimeError):
    """A cwd, session JSONL, or project-local state violates the PPR contract."""


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """The unmodified cwd identity and its fixed project-local locations."""

    project_path: str
    project_id: str
    state_directory: str
    database_path: str
    captures_directory: str


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """The two authoritative fields carried by a Codex session_meta record."""

    session_id: str
    cwd: str


def _require_absolute_cwd(cwd: object) -> str:
    if not isinstance(cwd, str) or not cwd or not os.path.isabs(cwd):
        raise ProjectPathError("invalid_cwd")
    return cwd


def project_paths_for_cwd(cwd: object) -> ProjectPaths:
    """Calculate local paths without normalizing or otherwise rewriting cwd."""
    project_path = _require_absolute_cwd(cwd)
    state_directory = os.path.join(project_path, _STATE_DIRECTORY)
    return ProjectPaths(
        project_path=project_path,
        project_id=project_path,
        state_directory=state_directory,
        database_path=os.path.join(state_directory, _DATABASE_NAME),
        captures_directory=os.path.join(state_directory, _CAPTURES_DIRECTORY),
    )


def read_session_metadata(session_jsonl_path: str | PathLike[str]) -> SessionMetadata:
    """Read session_id and cwd from a structured session_meta JSONL record."""
    try:
        with open(os.fspath(session_jsonl_path), "r", encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProjectPathError("invalid_session_jsonl") from exc
                if not isinstance(record, dict) or record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    raise ProjectPathError("invalid_session_meta")
                session_id = payload.get("id")
                if not isinstance(session_id, str) or not session_id:
                    raise ProjectPathError("invalid_session_id")
                return SessionMetadata(
                    session_id=session_id,
                    cwd=_require_absolute_cwd(payload.get("cwd")),
                )
    except (OSError, UnicodeDecodeError) as exc:
        raise ProjectPathError("session_unavailable") from exc
    raise ProjectPathError("session_meta_not_found")


def _read_only_uri(database_path: str) -> str:
    return "file:" + quote(database_path, safe="/:") + "?mode=ro"


def validate_existing_database(database_path: str | PathLike[str]) -> None:
    """Require an existing, readable SQLite database with the eight business tables."""
    path = os.fspath(database_path)
    if not os.path.lexists(path):
        raise ProjectPathError("database_missing")
    try:
        conn = sqlite3.connect(_read_only_uri(path), uri=True)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ProjectPathError("database_unusable") from exc
    tables = {row[0] for row in rows}
    if not _REQUIRED_TABLES.issubset(tables):
        raise ProjectPathError("database_schema_incomplete")


def _ensure_directory(path: str) -> None:
    if os.path.lexists(path):
        if not os.path.isdir(path):
            raise ProjectPathError("project_state_path_invalid")
        return
    os.mkdir(path)


def ensure_project_state(cwd: object) -> ProjectPaths:
    """Require an existing directory, then reuse or complete its local state."""
    paths = project_paths_for_cwd(cwd)
    # The declared project itself is never created, normalized, or replaced.
    # lexists distinguishes a dangling symlink (a present non-directory object)
    # from a path that has no filesystem object at all.
    if not os.path.lexists(paths.project_path):
        raise ProjectPathError("project_path_missing")
    if not os.path.isdir(paths.project_path):
        raise ProjectPathError("project_path_not_directory")
    _ensure_directory(paths.state_directory)
    if os.path.lexists(paths.database_path):
        validate_existing_database(paths.database_path)
    else:
        initialize_worker_database(paths.database_path)
    _ensure_directory(paths.captures_directory)
    return paths
