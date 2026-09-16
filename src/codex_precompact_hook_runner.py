"""Fail-open Codex PreCompact runner using payload.cwd project-local state."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

_SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from memory_palace.codex_precompact_adapter import (
    accept_codex_precompact,
    validate_precompact_payload,
)
from memory_palace.project_paths import ensure_project_state

__all__ = ["RunnerInputError", "process_payload", "run_hook", "main"]

COMPONENT_NAME = "codex_precompact_hook_runner"
_NORMAL_DISPOSITIONS = ("accepted", "duplicate", "ignored")
_PLACEHOLDER_PATH = "/__codex_precompact_hook_runner__/unused"
_DRAINER_PATH = os.path.join(_SCRIPT_DIRECTORY, "memory_palace_worker_runner.py")
_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")


class RunnerInputError(ValueError):
    """stdin or argv does not meet the thin runner contract."""


def _emit_fail_open(stderr: TextIO, exc: BaseException) -> int:
    line = json.dumps(
        {
            "component": COMPONENT_NAME,
            "error_type": type(exc).__name__,
            "status": "error",
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    try:
        stderr.write(line + "\n")
        stderr.flush()
    except (OSError, ValueError):
        pass
    return 0


def _parse_arguments(argv: Sequence[str]) -> None:
    if not isinstance(argv, (list, tuple)) or argv:
        raise RunnerInputError


def _read_payload(stdin: TextIO) -> dict[str, Any]:
    try:
        payload: Any = json.loads(stdin.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerInputError from exc
    if not isinstance(payload, dict):
        raise RunnerInputError
    return payload


def process_payload(
    payload: dict[str, Any],
    *,
    clock: Callable[[], float] = time.time,
) -> str:
    """Validate and accept one PreCompact payload; return the disposition.

    Mirrors the CC-side integration contract (cc-integration/cc_capture_
    hook_runner.py::process_payload): accepted / duplicate / ignored are
    returned to the caller; unexpected dispositions and payload errors
    raise.  The original hook path (stdin -> _dispatch) is unchanged.
    """
    validate_precompact_payload(payload)
    if payload.get("transcript_path") is None:
        # This branch never consults cwd, time, SQLite, or project directories.
        receipt = accept_codex_precompact(
            payload,
            {},
            _PLACEHOLDER_PATH,
            _PLACEHOLDER_PATH,
            now_epoch=0,
        )
    else:
        paths = ensure_project_state(payload.get("cwd"))
        receipt = accept_codex_precompact(
            payload,
            {paths.project_path: paths.project_id},
            paths.captures_directory,
            paths.database_path,
            now_epoch=int(clock()),
        )
    if receipt.disposition not in _NORMAL_DISPOSITIONS:
        raise RuntimeError("unexpected_adapter_disposition")
    if receipt.disposition in ("accepted", "duplicate"):
        _wake_drainer()
    return receipt.disposition


def _dispatch(*, stdin: TextIO, clock: Callable[[], float]) -> None:
    payload = _read_payload(stdin)
    process_payload(payload, clock=clock)


def _wake_drainer() -> None:
    """Start one non-blocking drainer attempt after durable acceptance.

    The child discovers its work from project-local SQLite.  No Job ID,
    capture bytes, secret, or model setting crosses this process boundary.
    Popen is intentionally not followed by wait/poll: Hook latency is not
    coupled to queue processing, TCP gates, or model calls.

    Set CODEX_MEMORY_NO_DRAINER=1 in the invoking environment to suppress
    the wake entirely (used by batch backfill, which wakes once at the end).
    """
    if os.environ.get("CODEX_MEMORY_NO_DRAINER"):
        return
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            _DRAINER_PATH,
            "--sessions-root",
            _SESSIONS_ROOT,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def run_hook(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    clock: Callable[[], float] = time.time,
) -> int:
    """Run once and always return zero so PreCompact remains fail-open."""
    del stdout
    try:
        _dispatch(stdin=stdin, clock=clock)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return _emit_fail_open(stderr, exc)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        _parse_arguments(sys.argv[1:] if argv is None else argv)
        return run_hook(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return _emit_fail_open(sys.stderr, exc)


if __name__ == "__main__":
    raise SystemExit(main())
