"""Fail-open Claude Code PreCompact/SessionEnd intake runner for the Memory Palace.

CC 侧独立接入（蓝图 §1.2：独立 Source Adapter + 独立 Hook，不反污染 Codex 主线）：
- 宫殿模块只 import 只读：I01 freeze_capture / I02 register_frozen_capture_job /
  ensure_project_state / get_job —— 全部按既有冻结契约调用，不修改任何宫殿文件；
- 事件身份独立：SHA256(["claude-code-v0.1", session_id, 事件描述符, 可见消息数])；
  可见数不变的重放是 duplicate，增长才产生新 Job；
- SessionEnd 按用户裁决先睡 10s 再二次编译取消息更多的一份（官方文档：转写异步落盘）；
- 指针文件 ~/.claude/memory-palace-sessions/<session_id>.jsonl（单行 session_meta）
  让未修改的 W04 Drainer 以 --sessions-root 该目录即可发现本项目库；
- 唤醒 Drainer 时继承当前进程环境（2026-09-10 裁决：环境变量是模型配置的唯一
  事实源，env 文件已退役；PROJECT_MEMORY_* 四变量必须预先注入派生链环境）；
- 任何异常只向 stderr 输出一行不含路径/正文/异常原文的安全 JSON，并且恒退出 0，
  绝不阻塞压缩或会话退出。快照不做任何清理（与宫殿本体口径一致）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from typing import Any, TextIO

_INTEGRATION_DIR = os.path.dirname(os.path.abspath(__file__))
PALACE_SRC = os.path.abspath(os.path.join(_INTEGRATION_DIR, "..", "src"))
DRAINER_PATH = os.path.join(PALACE_SRC, "memory_palace_worker_runner.py")
MIRROR_ROOT = os.environ.get(
    "CC_MEMORY_MIRROR_ROOT",
    os.path.expanduser("~/.claude/memory-palace-sessions"),
)
SESSIONEND_TAIL_WAIT_SECONDS = 2
_RUNTIME_ENV_KEYS = (
    "PROJECT_MEMORY_BASE_URL",
    "PROJECT_MEMORY_API_KEY",
    "PROJECT_MEMORY_MODEL",
    "PROJECT_MEMORY_REASONING_EFFORT",
)
COMPONENT_NAME = "cc_memory_capture_hook_runner"

if PALACE_SRC not in sys.path:
    sys.path.insert(0, PALACE_SRC)

from cc_transcript_compiler import (  # noqa: E402
    build_capture_lines,
    compile_visible_messages,
    session_meta_line,
    source_session_id_for,
)
from memory_palace.capture_job_registration import (  # noqa: E402
    register_frozen_capture_job,
)
from memory_palace.capture_store import freeze_capture  # noqa: E402
from memory_palace.job_queue import JOB_STATES, get_job  # noqa: E402
from memory_palace.project_paths import ensure_project_state  # noqa: E402

__all__ = ["HookInputError", "process_payload", "run_hook", "main"]

_STABLE_SCHEMA_PREFIX = "claude-code-v0.1"
_NO_WAKE_ENV = "CC_MEMORY_NO_DRAINER"

# 会话级准入门（2026-09-06 补充裁决：三道全要、sdk 一刀切、阈值 0）。
# 有意偏差：宫殿 Codex 侧无此门槛，CC 侧从严；被拒会话在触碰任何项目状态前返回。
_TEMP_CWD_PREFIXES = ("/tmp/", "/private/", "/var/")
_SDK_ENTRYPOINT_PREFIX = "sdk-"


def _cwd_denied(cwd: str) -> bool:
    return cwd == "/tmp" or cwd.startswith(_TEMP_CWD_PREFIXES)


def _detect_entrypoint(transcript_path: str) -> str | None:
    """First non-sidechain line's entrypoint value; scan stops at first hit."""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"entrypoint"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("isSidechain"):
                    continue
                value = record.get("entrypoint")
                if isinstance(value, str) and value:
                    return value
    except OSError:
        return None
    return None


class HookInputError(ValueError):
    """stdin payload does not meet the CC hook contract."""


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


def _validate_payload(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise HookInputError("payload_not_mapping")
    for field in ("session_id", "hook_event_name", "cwd"):
        value = payload.get(field)
        if not isinstance(value, str) or not value:
            raise HookInputError(f"invalid_field:{field}")
    if not os.path.isabs(payload["cwd"]):
        raise HookInputError("invalid_cwd")
    event = payload["hook_event_name"]
    if event == "PreCompact":
        if payload.get("trigger") not in ("manual", "auto"):
            raise HookInputError("invalid_trigger")
    elif event == "SessionEnd":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            raise HookInputError("invalid_reason")
    else:
        raise HookInputError("unsupported_event")
    transcript = payload.get("transcript_path")
    if transcript is not None and (not isinstance(transcript, str) or not transcript):
        raise HookInputError("invalid_transcript_path")
    return payload


def _event_descriptor(payload: dict[str, Any]) -> str:
    if payload["hook_event_name"] == "PreCompact":
        return "precompact:" + payload["trigger"]
    return "sessionend:" + payload["reason"]


def _stable_identity(session_id: str, descriptor: str, visible_count: int) -> tuple[str, str, str]:
    stable_bytes = json.dumps(
        [_STABLE_SCHEMA_PREFIX, session_id, descriptor, visible_count],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    event_key = hashlib.sha256(stable_bytes).hexdigest()
    filename = f"claude-code-{event_key}.jsonl"
    job_id = f"claude-code-{event_key}"
    return event_key, filename, job_id


def _compile_with_tail_wait(
    payload: dict[str, Any], *, sleep: Callable[[float], None]
) -> list[tuple[str, str, str]]:
    messages = compile_visible_messages(payload["transcript_path"])
    if payload["hook_event_name"] != "SessionEnd":
        return messages
    sleep(SESSIONEND_TAIL_WAIT_SECONDS)
    later = compile_visible_messages(payload["transcript_path"])
    return later if len(later) > len(messages) else messages


def _write_pointer(source_session_id: str, cwd: str, timestamp: str) -> None:
    os.makedirs(MIRROR_ROOT, exist_ok=True)
    # 指针文件名只用原始 CC session_id：同一会话的多个事件共享一行稳定内容。
    session_uuid = source_session_id.split(":", 1)[-1]
    pointer_path = os.path.join(MIRROR_ROOT, f"{session_uuid}.jsonl")
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="pointer_", dir=MIRROR_ROOT)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as handle:
            handle.write(
                session_meta_line(
                    source_session_id=source_session_id, cwd=cwd, timestamp=timestamp
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, pointer_path)
    finally:
        if os.path.lexists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _drainer_env(environ: dict[str, str]) -> dict[str, str]:
    """2026-09-10 裁决：env 文件已退役，环境变量是唯一事实源，原样继承。

    若派生链环境缺 PROJECT_MEMORY_* 四键，Drainer 的 from_env 会按既有
    契约抛 missing_runtime_environment，Job 走 retry_wait——那是配置
    注入层（launchd/shell）的故障，本层不做文件兜底、不掩盖。
    """
    return dict(environ)


def _wake_drainer(env: dict[str, str]) -> None:
    """Same non-blocking global-drainer wake as the Codex chain, extra root only.

    The unmodified W04 drainer owns cross-source serialization through its fixed
    flock; concurrent wakes exit immediately by design.
    """
    subprocess.Popen(
        [
            sys.executable,
            "-B",
            DRAINER_PATH,
            "--sessions-root",
            MIRROR_ROOT,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
        env=env,
    )


def process_payload(
    payload: dict[str, Any],
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    wake: Callable[[dict[str, str]], None] = _wake_drainer,
    environ: dict[str, str] | None = None,
) -> str:
    """Run one intake; returns accepted/duplicate/ignored. Raises on real failure."""
    payload = _validate_payload(payload)
    if payload.get("transcript_path") is None:
        return "ignored"
    if _cwd_denied(payload["cwd"]):
        return "ignored:temp_cwd"
    entrypoint = _detect_entrypoint(payload["transcript_path"])
    if entrypoint is not None and entrypoint.startswith(_SDK_ENTRYPOINT_PREFIX):
        return "ignored:sdk_entrypoint"

    messages = _compile_with_tail_wait(payload, sleep=sleep)
    visible_count = len(messages)
    if visible_count == 0:
        return "ignored:no_visible_dialog"
    session_id = payload["session_id"]
    source_session_id = source_session_id_for(session_id)
    event_key, capture_filename, job_id = _stable_identity(
        session_id, _event_descriptor(payload), visible_count
    )
    del event_key  # filename/job_id 已由派生名承载

    paths = ensure_project_state(payload["cwd"])
    capture_path = os.path.join(paths.captures_directory, capture_filename)
    existing_capture = os.path.lexists(capture_path)
    existing_job = get_job(paths.database_path, job_id=job_id)

    if existing_capture and existing_job is None:
        raise RuntimeError("capture_exists_without_job")
    if not existing_capture and existing_job is not None:
        raise RuntimeError("job_exists_without_capture")

    now_epoch = int(clock())
    if existing_capture and existing_job is not None:
        mismatched = []
        if existing_job["job_id"] != job_id:
            mismatched.append("job_id")
        if existing_job["project_id"] != paths.project_id:
            mismatched.append("project_id")
        if existing_job["source_session_id"] != source_session_id:
            mismatched.append("source_session_id")
        if existing_job["capture_path"] != os.path.abspath(capture_path):
            mismatched.append("capture_path")
        if existing_job["state"] not in JOB_STATES:
            mismatched.append("state")
        if mismatched:
            raise RuntimeError("duplicate_check_failed:" + ",".join(mismatched))
        disposition = "duplicate"
    else:
        capture_lines = build_capture_lines(
            messages,
            source_session_id=source_session_id,
            cwd=paths.project_path,
            meta_timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(now_epoch)),
        )
        temp_fd, temp_path = tempfile.mkstemp(
            prefix="cc_compile_tmp_", dir=paths.captures_directory
        )
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
                handle.write("\n".join(capture_lines) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            frozen = freeze_capture(temp_path, capture_path)
        finally:
            if os.path.lexists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        register_frozen_capture_job(
            paths.database_path,
            job_id=job_id,
            project_id=paths.project_id,
            source_session_id=source_session_id,
            frozen_capture=frozen,
            now_epoch=now_epoch,
        )
        disposition = "accepted"

    _write_pointer(
        source_session_id,
        paths.project_path,
        time.strftime("%Y-%m-%dT%H:%M:%S%z", time.gmtime(now_epoch)),
    )
    base_env = dict(os.environ if environ is None else environ)
    if (base_env.get(_NO_WAKE_ENV) or "") != "1":
        wake(_drainer_env(base_env))
    return disposition


def run_hook(
    *,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    wake: Callable[[dict[str, str]], None] = _wake_drainer,
) -> int:
    del stdout
    try:
        payload = json.loads(stdin.read())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return _emit_fail_open(stderr, exc)
    try:
        process_payload(payload, clock=clock, sleep=sleep, wake=wake)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return _emit_fail_open(stderr, exc)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if argv and argv[0] not in ():
        return _emit_fail_open(sys.stderr, HookInputError("unexpected_argv"))
    try:
        return run_hook(stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        return _emit_fail_open(sys.stderr, exc)


if __name__ == "__main__":
    raise SystemExit(main())
