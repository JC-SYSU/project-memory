"""Upstream model connectivity probes for the memory-palace extractor.

The preview path is free: it reads environment variables and formats them
without any network request.  The probe path performs four real Chat
Completions requests; callers gate it behind an explicit confirmation.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "ProbeResult",
    "UpstreamConfig",
    "load_config",
    "missing_config_markdown",
    "preview_markdown",
    "probe_chat",
    "probe_effort",
    "probe_reachability",
    "probe_tool_calling",
    "render_report",
    "run_probes",
]

DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    base_url: str
    api_key: str
    model: str
    effort: str

    @property
    def missing(self) -> list[str]:
        missing: list[str] = []
        if not self.base_url:
            missing.append("PROJECT_MEMORY_BASE_URL")
        if not self.api_key:
            missing.append("PROJECT_MEMORY_API_KEY")
        if not self.model:
            missing.append("PROJECT_MEMORY_MODEL")
        return missing


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    ok: bool
    message: str


def load_config() -> UpstreamConfig:
    effort = os.environ.get("PROJECT_MEMORY_REASONING_EFFORT", "").strip()
    return UpstreamConfig(
        base_url=os.environ.get("PROJECT_MEMORY_BASE_URL", "").strip(),
        api_key=os.environ.get("PROJECT_MEMORY_API_KEY", "").strip(),
        model=os.environ.get("PROJECT_MEMORY_MODEL", "").strip(),
        effort=effort or DEFAULT_EFFORT,
    )


def _endpoint_label(base_url: str) -> str:
    if not base_url:
        return "（未配置）"
    try:
        parts = urlsplit(base_url)
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "（无法解析）"
    host = parts.hostname or ""
    if not host:
        return "（无法解析）"
    return f"{parts.scheme}://{host}{port}{parts.path}"


def _fetch(
    base_url: str,
    api_key: str,
    body: dict[str, Any],
    timeout: float,
) -> tuple[int, str]:
    url = f"{base_url.rstrip('/')}/chat/completions"
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
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)
    except TimeoutError:
        return 0, "请求超时"


def _tokens_used(body_text: str) -> int:
    try:
        return int(json.loads(body_text).get("usage", {}).get("total_tokens", 0))
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return 0


def _choice(body_text: str) -> dict[str, Any] | None:
    try:
        choices = json.loads(body_text)["choices"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0]


def probe_reachability(
    config: UpstreamConfig, timeout: float = DEFAULT_TIMEOUT
) -> ProbeResult:
    status, body = _fetch(
        config.base_url,
        config.api_key,
        {
            "model": config.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
        timeout,
    )
    if status == 0:
        return ProbeResult(
            "连通与认证",
            False,
            f"网络不可达（{body}）。检查 BASE_URL 是否可访问、代理与防火墙。",
        )
    if status in (401, 403):
        return ProbeResult(
            "连通与认证",
            False,
            f"HTTP {status}：认证失败。检查 PROJECT_MEMORY_API_KEY 是否正确、是否还有效。",
        )
    if status == 404:
        return ProbeResult(
            "连通与认证",
            False,
            "HTTP 404：端点不存在。BASE_URL 填 API 根（如 https://api.example.com/v1），"
            "不是完整路径 /chat/completions，也不是 /responses、/messages 等其他协议。",
        )
    if status >= 500:
        return ProbeResult(
            "连通与认证",
            False,
            f"HTTP {status}：服务端错误。稍后重试；持续失败联系模型服务商。",
        )
    if status != 200:
        return ProbeResult(
            "连通与认证",
            False,
            f"HTTP {status}：{body[:200]}。结合返回信息检查 URL 与请求格式。",
        )
    return ProbeResult(
        "连通与认证",
        True,
        f"端点可达，认证通过（本次 {_tokens_used(body)} tokens）",
    )


def probe_chat(config: UpstreamConfig, timeout: float = DEFAULT_TIMEOUT) -> ProbeResult:
    status, body = _fetch(
        config.base_url,
        config.api_key,
        {
            "model": config.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        },
        timeout,
    )
    if status != 200:
        return ProbeResult("Chat Completions 协议", False, f"HTTP {status}：{body[:200]}")
    choice = _choice(body)
    if choice is None or not choice.get("message"):
        return ProbeResult(
            "Chat Completions 协议",
            False,
            f"200 但响应结构异常（不是 Chat Completions 格式）：{body[:200]}",
        )
    return ProbeResult(
        "Chat Completions 协议",
        True,
        f"协议正常（本次 {_tokens_used(body)} tokens）",
    )


def probe_tool_calling(
    config: UpstreamConfig, timeout: float = DEFAULT_TIMEOUT
) -> ProbeResult:
    request_body = {
        "model": config.model,
        "messages": [{"role": "user", "content": "call the tool"}],
        "max_tokens": 512,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "probe",
                    "description": "connectivity probe",
                    "parameters": {
                        "type": "object",
                        "properties": {"echo": {"type": "string"}},
                    },
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "probe"}},
    }
    status, response_body = _fetch(
        config.base_url, config.api_key, request_body, timeout
    )
    if status == 400:
        return ProbeResult(
            "强制工具调用",
            False,
            "HTTP 400：端点拒绝了强制工具调用（tools + tool_choice）。"
            "本系统依赖它才能可靠拿到结构化结果；请更换支持强制 tool_choice 的端点/模型。",
        )
    if status != 200:
        return ProbeResult("强制工具调用", False, f"HTTP {status}：{response_body[:200]}")
    choice = _choice(response_body)
    if choice is None:
        return ProbeResult(
            "强制工具调用",
            False,
            f"200 但响应结构异常（缺 choices[0].message）：{response_body[:200]}",
        )
    message = choice.get("message")
    if isinstance(message, dict) and message.get("tool_calls"):
        return ProbeResult(
            "强制工具调用",
            True,
            f"端点按 tool_choice 返回了 tool_calls（本次 {_tokens_used(response_body)} tokens）",
        )
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        return ProbeResult(
            "强制工具调用",
            False,
            f"响应被 max_tokens={request_body['max_tokens']} 截断（finish_reason=length），"
            "tool_calls 未及输出。推理模型会先消耗预算输出推理内容；"
            "本探针已放宽到 512，仍截断说明该模型推理开销更大，或端点未按 tool_choice 强制调用。",
        )
    return ProbeResult(
        "强制工具调用",
        False,
        f"200 但响应里没有 tool_calls（finish_reason={finish_reason!r}）："
        "端点未按 tool_choice 强制调用工具。本系统依赖强制工具调用拿到结构化结果；"
        "请更换支持该能力的端点/模型。",
    )


def probe_effort(
    config: UpstreamConfig, timeout: float = DEFAULT_TIMEOUT
) -> ProbeResult:
    status, body = _fetch(
        config.base_url,
        config.api_key,
        {
            "model": config.model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "reasoning_effort": config.effort,
        },
        timeout,
    )
    if status == 400:
        return ProbeResult(
            f"reasoning_effort={config.effort}",
            False,
            f"HTTP 400：端点不接受 `reasoning_effort={config.effort}` 字段。"
            "本系统会在每个请求里发送该字段，端点必须容忍它。"
            "尝试换档位（low/medium/high/max）或换端点。",
        )
    if status != 200:
        return ProbeResult(
            f"reasoning_effort={config.effort}", False, f"HTTP {status}：{body[:200]}"
        )
    return ProbeResult(
        f"reasoning_effort={config.effort}",
        True,
        f"字段被端点接受（本次 {_tokens_used(body)} tokens）",
    )


def run_probes(
    config: UpstreamConfig, *, timeout: float = DEFAULT_TIMEOUT
) -> list[ProbeResult]:
    if config.missing:
        return [ProbeResult("配置", False, "缺少配置：" + "、".join(config.missing))]
    probes: list[tuple[str, Callable[[], ProbeResult]]] = [
        ("连通与认证", lambda: probe_reachability(config, timeout)),
        ("Chat Completions 协议", lambda: probe_chat(config, timeout)),
        ("强制工具调用", lambda: probe_tool_calling(config, timeout)),
        (
            f"reasoning_effort={config.effort}",
            lambda: probe_effort(config, timeout),
        ),
    ]
    results: list[ProbeResult] = []
    with ThreadPoolExecutor(max_workers=len(probes)) as executor:
        futures = [executor.submit(probe) for _, probe in probes]
        for (name, _), future in zip(probes, futures):
            try:
                results.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report, do not abort
                results.append(ProbeResult(name, False, f"探针异常：{exc}"))
    return results


def preview_markdown(config: UpstreamConfig) -> str:
    lines = [
        "## 上游连接检查（预览）",
        "",
        f"- 端点：{_endpoint_label(config.base_url)}",
        f"- 模型：{config.model or '（未配置）'}",
        f"- reasoning_effort：{config.effort}",
        f"- API Key：{'已配置' if config.api_key else '未配置'}",
        "",
    ]
    if config.missing:
        lines.append(
            f"缺少配置：{'、'.join(config.missing)}。"
            "请先设置这些环境变量；当前不会发起任何请求。"
        )
    else:
        lines.append(
            "尚未发起请求。确认检查会向上游模型发送 4 次小请求，可能产生少量费用。"
            "确认后请以 `confirm=true` 再次调用。"
        )
    return "\n".join(lines) + "\n"


def missing_config_markdown(config: UpstreamConfig) -> str:
    return (
        "## 上游连接检查\n\n"
        f"缺少配置：{'、'.join(config.missing)}。"
        "请先设置这些环境变量；当前不会发起任何请求。\n\n"
        "**结论：缺少配置，未发起请求**\n"
    )


def render_report(config: UpstreamConfig, results: list[ProbeResult]) -> str:
    lines = [
        "## 上游连接检查",
        "",
        f"- 端点：{_endpoint_label(config.base_url)}",
        f"- 模型：{config.model or '（未配置）'}",
        f"- reasoning_effort：{config.effort}",
        f"- API Key：{'已配置' if config.api_key else '未配置'}",
        "",
    ]
    for result in results:
        lines.append(
            f"- {'通过' if result.ok else '失败'}：{result.name}，{result.message}"
        )
    failed = sum(1 for result in results if not result.ok)
    conclusion = (
        "上游连接正常" if not failed else f"上游连接有问题（{failed} 项未通过）"
    )
    lines.extend(["", f"**结论：{conclusion}**"])
    return "\n".join(lines) + "\n"
