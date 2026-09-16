"""Hook-wake single Worker Drainer across project-local databases."""

from __future__ import annotations

import argparse
import contextlib
import os
import sqlite3
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

try:
    import fcntl  # POSIX
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    import msvcrt  # type: ignore[import-not-found, unused-ignore]

from memory_palace.project_paths import (
    ProjectPathError,
    project_paths_for_cwd,
    read_session_metadata,
    validate_existing_database,
)
from memory_palace.worker_orchestrator import recover_interrupted_jobs, run_once

_POLL_SECONDS = 15
_LOCK_PATH = os.path.expanduser("~/.codex/project-memory/.memory-palace/worker-drainer.lock")


def _iter_session_jsonl_paths(sessions_root: str) -> list[str]:
    paths: list[str] = []
    for directory, directories, filenames in os.walk(sessions_root):
        directories.sort()
        for filename in sorted(filenames):
            if filename.endswith(".jsonl"):
                paths.append(os.path.join(directory, filename))
    return paths


def discover_project_databases(sessions_root: str) -> list[str]:
    """Find existing valid local databases from structured session_meta.cwd values."""
    databases_by_cwd: dict[str, str] = {}
    for session_path in _iter_session_jsonl_paths(sessions_root):
        try:
            metadata = read_session_metadata(session_path)
            paths = project_paths_for_cwd(metadata.cwd)
        except ProjectPathError:
            continue
        if not os.path.lexists(paths.database_path):
            continue
        validate_existing_database(paths.database_path)
        databases_by_cwd[paths.project_path] = paths.database_path
    return [databases_by_cwd[cwd] for cwd in sorted(databases_by_cwd)]


def _read_only_uri(database_path: str) -> str:
    return "file:" + quote(database_path, safe="/:") + "?mode=ro"


def _has_unfinished_jobs(database_path: str) -> bool:
    """Read the frozen Q01 states without adding a queue or status store."""
    conn = sqlite3.connect(_read_only_uri(database_path), uri=True)
    try:
        row = conn.execute(
            "SELECT 1 FROM extraction_jobs "
            "WHERE state IN ('queued', 'running', 'retry_wait') LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return row is not None


@contextlib.contextmanager
def _try_process_lock(lock_path: str):
    """Hold a kernel file lock for the entire drainer lifecycle.

    The lock is released by the OS when this process exits, including an
    abnormal exit; no stale PID or cleanup record is involved.  POSIX uses
    ``flock(LOCK_EX | LOCK_NB)``; Windows lacks flock and uses the byte-range
    lock ``msvcrt.locking(LK_NBLCK)`` on the first byte instead (a lock file
    shorter than one byte cannot be locked, so one NUL byte is written).
    """
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    if fcntl is not None:
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        return
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\x00")
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined, possibly-unbound]
            acquired = True
        except OSError:
            yield False
            return
        yield True
    finally:
        if acquired:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined, possibly-unbound]
        os.close(fd)


def _run_loop(
    sessions_root: str,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    discover: Callable[[str], list[str]] = discover_project_databases,
    recover: Callable[..., Any] = recover_interrupted_jobs,
    run: Callable[..., dict[str, Any]] = run_once,
    pending: Callable[[str], bool] = _has_unfinished_jobs,
    lock_path: str = _LOCK_PATH,
) -> None:
    """Drain all local queues serially, then exit after an idle confirmation."""
    with _try_process_lock(lock_path) as acquired:
        if not acquired:
            return

        recovered: set[str] = set()
        idle_confirmed = False
        while True:
            databases = discover(sessions_root)
            for database_path in databases:
                if database_path not in recovered:
                    recover(database_path, now_epoch=int(clock()))
                    recovered.add(database_path)
                run(database_path, now_epoch=int(clock()))

            # Re-discover before deciding to sleep/exit so a new project and a
            # Job registered while a Window was running are consumed by this
            # same locked Drainer on the next pass.
            after_run = discover(sessions_root)
            has_pending = any(pending(path) for path in after_run)
            if has_pending:
                idle_confirmed = False
            elif idle_confirmed:
                return
            else:
                idle_confirmed = True
            # This short poll is both the retry_wait wake-up and the required
            # idle confirmation window for Hook registration races.
            sleep(_POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_palace_worker_runner")
    parser.add_argument(
        "--sessions-root",
        required=True,
        metavar="PATH",
        help="Codex sessions root to scan for JSONL session metadata",
    )
    args = parser.parse_args(argv)
    _run_loop(args.sessions_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
