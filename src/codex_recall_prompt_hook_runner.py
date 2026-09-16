"""Global Codex UserPromptSubmit reminder for on-demand Memory Recall."""

from __future__ import annotations

import json
import sys


RECALL_DECISION_CONTEXT = (
    "回答前，先判断：当前问题的正确处理是否依赖当前上下文中没有明确提供的"
    "过往项目事实、决定、偏好、约束、实验结果或已完成工作。\n\n"
    "如果依赖，先调用 memory_recall；如果当前上下文已经足够，或问题与历史无关，"
    "则不要调用。\n\n"
    "当用户要求召回记忆，却没有取得真实 Recall 结果时，不得声称自己已经召回过。"
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(payload, dict):
        return 0
    if payload.get("hook_event_name") != "UserPromptSubmit":
        return 0

    response = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": RECALL_DECISION_CONTEXT,
        }
    }
    json.dump(response, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
