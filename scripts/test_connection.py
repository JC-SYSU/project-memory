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
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_palace.upstream_probe import (  # noqa: E402
    UpstreamConfig,
    probe_chat,
    probe_effort,
    probe_reachability,
    probe_tool_calling,
)

_ERR = "\033[31m✘\033[0m"
_OK = "\033[32m✔\033[0m"
_TIMEOUT_SECONDS = 60.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("PROJECT_MEMORY_BASE_URL", "")
    )
    parser.add_argument(
        "--api-key", default=os.environ.get("PROJECT_MEMORY_API_KEY", "")
    )
    parser.add_argument("--model", default=os.environ.get("PROJECT_MEMORY_MODEL", ""))
    parser.add_argument(
        "--effort", default=os.environ.get("PROJECT_MEMORY_REASONING_EFFORT", "high")
    )
    args = parser.parse_args()

    config = UpstreamConfig(
        base_url=args.base_url.strip(),
        api_key=args.api_key.strip(),
        model=args.model.strip(),
        effort=args.effort.strip() or "high",
    )
    missing = config.missing
    if missing:
        print("缺少配置：", ", ".join(missing))
        print(
            "先填写环境变量（可运行 python3 scripts/setup_env.py 引导填写），"
            "或用相应 --flag 传入。"
        )
        return 2

    results = [
        probe_reachability(config, _TIMEOUT_SECONDS),
        probe_chat(config, _TIMEOUT_SECONDS),
        probe_tool_calling(config, _TIMEOUT_SECONDS),
        probe_effort(config, _TIMEOUT_SECONDS),
    ]
    for result in results:
        print(f"{_OK if result.ok else _ERR} {result.name}：{result.message}")
    failed = sum(not result.ok for result in results)
    if failed:
        print(
            f"\n{failed} 项未通过。按上方建议调整后重跑本脚本；"
            "若与预期不符，见 INSTALL.md「故障排查」。"
        )
        return 1
    print("\n四项全部通过：该端点可用。抽取任务将正常执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
