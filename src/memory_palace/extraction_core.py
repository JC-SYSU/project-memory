"""Worker Extraction Core v0.2 — 单次 Window 同步抽取链组装（强制工具调用形态）。

v0.1 -> v0.2 变更（2026-09-10 实验驱动，见 docs/WORKER_TOOLCALL_UPGRADE_20260910.md）：
- response_format(json_schema) -> tools + 强制 tool_choice：候选经 tool_calls
  结构字段返回，围栏包裹/漏括号/裸字符串等响应级格式病在协议层不可能发生
  （实测同一失败窗口文本模式 0-16 条非法，工具模式 6/6 全合法）；
- 请求携带 max_tokens 上限：防止 reasoning_effort=max/high 的推理阶段
  烧穿网关默认配额导致正文为空/截断（实测 effort=max 无上限时 8000 token
  全部耗在推理，content 为空）；
- 响应解析改为取首个 tool_call 的 function.arguments 原文送 validate_response；
  无 tool_call 一律 api_response_shape_invalid（响应级失败，由 Q01 收敛重试）。

已验收 Window -> 确定性 Prompt 渲染 -> 一次 OpenAI-compatible 调用
-> tool_call arguments 原文 -> 已验收 validate_response -> 返回正文/验证/usage。

不消费 Job、不循环、不重试、不写数据库、不推进 cursor。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .candidate_validator import validate_response
from .contracts import Window

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extraction_system.txt"

_ENV_NAMES = (
    "PROJECT_MEMORY_BASE_URL",
    "PROJECT_MEMORY_API_KEY",
    "PROJECT_MEMORY_MODEL",
    "PROJECT_MEMORY_REASONING_EFFORT",
)

_USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")

_USER_PROMPT_HEADER = (
    "Extract durable project memory from this bounded Window. "
    "Call the submit_candidates tool exactly once with all Candidates."
)

_TOOL_NAME = "submit_candidates"

#: 输出配额硬上限：给正文留足空间，防止推理阶段吃光网关默认配额。
_MAX_TOKENS_CAP = 32768

# 固定安全错误码；不为每个 HTTP 状态码建立业务枚举。
_FAILURE_CODES = (
    "transport_error",
    "http_non_2xx",
    "api_response_not_json",
    "api_response_shape_invalid",
    "missing_runtime_environment",
)


class ExtractionCoreError(Exception):
    """固定安全错误码；字符串不含 URL、Key、请求头、响应正文或底层异常文本。"""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        if code not in _FAILURE_CODES:
            raise ValueError(f"unknown failure code: {code}")
        self.code = code
        self.detail = detail
        message = code
        if detail:
            message = f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    base_url: str
    api_key: str
    model: str
    reasoning_effort: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "RuntimeConfig":
        """四个环境变量全部必填、无默认值；缺失时抛安全错误码，只列缺失变量名。"""
        if environ is None:
            environ = os.environ
        missing = [name for name in _ENV_NAMES if not environ.get(name)]
        if missing:
            raise ExtractionCoreError(
                "missing_runtime_environment",
                detail=", ".join(missing),
            )
        return cls(
            base_url=environ["PROJECT_MEMORY_BASE_URL"],
            api_key=environ["PROJECT_MEMORY_API_KEY"],
            model=environ["PROJECT_MEMORY_MODEL"],
            reasoning_effort=environ["PROJECT_MEMORY_REASONING_EFFORT"],
        )


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes


class Transport(Protocol):
    def post_json(
        self, url: str, body: dict[str, Any], api_key: str, timeout: float
    ) -> HttpResponse: ...


class UrllibTransport:
    """标准库 urllib 传输层；HTTPError 转成 HttpResponse，其余异常向上抛。"""

    def post_json(
        self, url: str, body: dict[str, Any], api_key: str, timeout: float
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            # HTTP 错误统一转成带状态码和 body 的 HttpResponse，由 Core 判定 http_non_2xx。
            return HttpResponse(exc.code, exc.read())


def _read_frozen_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def render_prompts(window: Window) -> tuple[str, str]:
    """确定性渲染 system/user 两段 prompt；同一输入生成字节级一致文本。"""
    system_prompt = _read_frozen_system_prompt()
    allowed_source_lines = [message.source_line for message in window.messages]
    blocks = [
        f'<message source_line="{message.source_line}" '
        f'timestamp="{message.timestamp}" role="{message.role}">\n'
        f"{message.body}\n"
        "</message>"
        for message in window.messages
    ]
    lines = [
        _USER_PROMPT_HEADER,
        "",
        "<allowed_source_lines>",
        json.dumps(allowed_source_lines, separators=(",", ":")),
        "</allowed_source_lines>",
        "",
        "<window_messages>",
        "\n\n".join(blocks),
        "</window_messages>",
    ]
    user_prompt = "\n".join(lines)
    return system_prompt, user_prompt


def _candidate_tool_schema(window: Window) -> dict[str, Any]:
    """与 v0.1 response schema 同构的 submit_candidates 工具 schema。"""
    return {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": "提交从当前 Window 中提取的全部记忆候选。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "candidates": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "evidence_lines": {
                                    "type": "array",
                                    "minItems": 1,
                                    "items": {
                                        "type": "integer",
                                        "enum": [
                                            message.source_line
                                            for message in window.messages
                                        ],
                                    },
                                },
                            },
                            "required": ["title", "summary", "evidence_lines"],
                        },
                    }
                },
                "required": ["candidates"],
            },
        },
    }


def build_request(window: Window, runtime_config: RuntimeConfig) -> dict[str, Any]:
    """确定性请求体：固定 model/messages/reasoning_effort/tools(强制 tool_choice)/max_tokens。"""
    system_prompt, user_prompt = render_prompts(window)
    return {
        "model": runtime_config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "reasoning_effort": runtime_config.reasoning_effort,
        "max_tokens": _MAX_TOKENS_CAP,
        "tools": [_candidate_tool_schema(window)],
        "tool_choice": {
            "type": "function",
            "function": {"name": _TOOL_NAME},
        },
    }


def _extract_usage(usage: Any) -> dict[str, int] | None:
    """只保留顶层 usage 中实际存在且为数字的四个白名单字段；布尔值不算数字。"""
    if not isinstance(usage, dict):
        return None
    out: dict[str, int] = {}
    for field in _USAGE_FIELDS:
        value = usage.get(field)
        if type(value) is int or type(value) is float:
            out[field] = value
    return out or None


def extract_window(
    window: Window,
    runtime_config: RuntimeConfig,
    *,
    transport: Transport | None = None,
    timeout_seconds: float = 300.0,
    before_transport: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """单次 Window 同步抽取。最多调用 Transport 一次，不重试、不 fallback。

    ``before_transport`` 为 W01-P01 窄回调：在 Prompt/请求体/URL 均已构造
    后、``transport.post_json()`` 真正发生前同步调用一次。默认 ``None`` 时
    行为与 baseline 完全一致。回调异常原样向上传播，不翻译为
    ``transport_error``。不扩展成 middleware、事件钩子链或重试框架。
    """
    if transport is None:
        transport = UrllibTransport()
    request = build_request(window, runtime_config)
    url = f"{runtime_config.base_url.rstrip('/')}/chat/completions"
    if before_transport is not None:
        before_transport()
    try:
        response = transport.post_json(url, request, runtime_config.api_key, timeout_seconds)
    except ExtractionCoreError:
        raise
    except Exception as exc:
        raise ExtractionCoreError("transport_error") from exc

    if not 200 <= response.status_code < 300:
        raise ExtractionCoreError("http_non_2xx")

    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ExtractionCoreError("api_response_not_json")

    # v0.2：候选必须经 submit_candidates 工具调用提交。无 tool_call 一律
    # 响应级失败（Q01 收敛重试），绝不从正文文本里抢救。
    try:
        message = payload["choices"][0]["message"]
        tool_calls = message["tool_calls"]
        if not isinstance(tool_calls, list) or not tool_calls:
            raise LookupError
        content = tool_calls[0]["function"]["arguments"]
    except (KeyError, IndexError, TypeError, LookupError):
        raise ExtractionCoreError("api_response_shape_invalid")
    if not isinstance(content, str):
        raise ExtractionCoreError("api_response_shape_invalid")

    # Evidence 允许集合严格来自 Window 消息的原始 JSONL 行号。
    allowed = {message.source_line for message in window.messages}
    validation = validate_response(content, allowed_evidence_lines=allowed)
    usage = _extract_usage(payload.get("usage"))
    return {
        "model_content": content,
        "validation": validation,
        "usage": usage,
    }
