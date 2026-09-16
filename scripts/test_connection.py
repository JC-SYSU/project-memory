#!/usr/bin/env python3
"""Connectivity and usability probe for the memory-palace model endpoint.

Four probes, run in order; the script exits 0 only when all four pass:

  1. reachability + auth      POST {base}/chat/completions, minimal body
  2. chat completions works   same request returns a proper choices[0]
  3. forced tool calling      request with tools + tool_choice accepted
  4. reasoning_effort field   endpoint accepts the "reasoning_effort" key

Values are read from PROJECT_MEMORY_* env vars by default; override with
flags.  Every failure prints a concrete fix suggestion (see INSTALL.md
「端点与模型要求」for the fill-in rules).
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from typing import Any

_ERR = "\033[31m✘\033[0m"
_OK = "\033[32m✔\033[0m"


def _fetch(base_url: str, api_key: str, body: dict[str, Any]) -> tuple[int, str]:
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
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)


def _tokens_used(body_text: str) -> int:
    try:
        return json.loads(body_text).get("usage", {}).get("total_tokens", 0)
    except (json.JSONDecodeError, AttributeError):
        return 0


def probe_reachability(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    status, body = _fetch(
        base_url,
        api_key,
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
    )
    if status == 0:
        return False, f"网络不可达（{body}）。检查 BASE_URL 是否可访问、代理与防火墙。"
    if status == 401 or status == 403:
        return False, f"HTTP {status}：认证失败。检查 PROJECT_MEMORY_API_KEY 是否正确、是否还有效。"
    if status == 404:
        return False, (
            f"HTTP 404：端点不存在。BASE_URL 填的是 API 根（如 https://api.example.com/v1），"
            f"不是完整路径 /chat/completions，也不是 /responses、/messages 等其他协议。"
            f"见 INSTALL.md「端点与模型要求」填法对照表。"
        )
    if status >= 500:
        return False, f"HTTP {status}：服务端错误。稍后重试；持续失败联系模型服务商。"
    if status != 200:
        return False, f"HTTP {status}：{body[:200]}。结合返回信息检查 URL 与请求格式。"
    return True, f"连通与认证通过（本次 {_tokens_used(body)} tokens）"


def probe_chat(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    status, body = _fetch(
        base_url,
        api_key,
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
    )
    if status != 200:
        return False, f"HTTP {status}：{body[:200]}"
    try:
        choices = json.loads(body)["choices"]
        assert choices and choices[0].get("message")
    except (json.JSONDecodeError, KeyError, IndexError, AssertionError):
        return False, f"200 但响应结构异常（不是 Chat Completions 格式）：{body[:200]}"
    return True, f"Chat Completions 协议正常（本次 {_tokens_used(body)} tokens）"


def probe_tool_calling(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "call the tool"}],
        "max_tokens": 64,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "probe",
                    "description": "connectivity probe",
                    "parameters": {"type": "object", "properties": {"echo": {"type": "string"}}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "probe"}},
    }
    status, response_body = _fetch(base_url, api_key, body)
    if status == 400:
        return False, (
            "HTTP 400：端点拒绝了强制工具调用（tools + tool_choice）。"
            "本系统依赖它才能可靠拿到结构化结果；请更换支持强制 tool_choice 的端点/模型，"
            "或用语法约束/grammar 出结构的网关不可用（见 INSTALL.md「端点与模型要求」）。"
        )
    if status != 200:
        return False, f"HTTP {status}：{response_body[:200]}"
    message = json.loads(response_body)["choices"][0]["message"]
    markers = message.get("tool_calls") is not None or message.get("finish_reason") in (
        "tool_calls",
        "length",
        "stop",
    )
    if not markers:
        return False, "200 但响应里既无 tool_calls 也无预期 finish_reason，协议行为异常。"
    return True, f"强制工具调用协议正常（本次 {_tokens_used(response_body)} tokens）"


def probe_effort(base_url: str, api_key: str, model: str, effort: str) -> tuple[bool, str]:
    status, body = _fetch(
        base_url,
        api_key,
        {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "reasoning_effort": effort,
        },
    )
    if status == 400:
        return False, (
            f"HTTP 400：端点不接受 `reasoning_effort={effort}` 字段。"
            f"本系统会在每个请求里发送该字段，端点必须容忍它。"
            f"尝试换档位（low/medium/high/max）或换端点；确认该字段名与取值在端点文档中受支持。"
        )
    if status != 200:
        return False, f"HTTP {status}：{body[:200]}"
    return True, f"reasoning_effort={effort} 被端点接受（本次 {_tokens_used(body)} tokens）"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("PROJECT_MEMORY_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("PROJECT_MEMORY_API_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("PROJECT_MEMORY_MODEL", ""))
    parser.add_argument(
        "--effort", default=os.environ.get("PROJECT_MEMORY_REASONING_EFFORT", "high")
    )
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("PROJECT_MEMORY_BASE_URL", args.base_url),
            ("PROJECT_MEMORY_API_KEY", args.api_key),
            ("PROJECT_MEMORY_MODEL", args.model),
        )
        if not value
    ]
    if missing:
        print("缺少配置：", ", ".join(missing))
        print("先填写环境变量（可运行 python3 scripts/setup_env.py 引导填写），或用相应 --flag 传入。")
        return 2

    results = [
        probe_reachability(args.base_url, args.api_key, args.model),
        probe_chat(args.base_url, args.api_key, args.model),
        probe_tool_calling(args.base_url, args.api_key, args.model),
        probe_effort(args.base_url, args.api_key, args.model, args.effort),
    ]
    for ok, note in results:
        print(f"{_OK if ok else _ERR} {note}")
    failed = sum(not ok for ok, _ in results)
    if failed:
        print(f"\n{failed} 项未通过。按上方建议调整后重跑本脚本；若与预期不符，见 INSTALL.md「故障排查」。")
        return 1
    print("\n四项全部通过：该端点可用。抽取任务将正常执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())