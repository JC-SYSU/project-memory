#!/usr/bin/env python3
"""傻瓜式环境变量向导：只问 URL 和 Key，其余尽量自动。

交互模式（默认）：
  python3 scripts/setup_env.py
  逐项提问——BASE_URL、API_KEY 必填；MODEL 优先从端点的 /models 列表
  探测，探测不到再手输；REASONING_EFFORT 默认 high，直接回车接受。
  填写后写入 rc 文件（默认 ~/.zshenv，--rc-file 可改），macOS 上另行
  打印 launchctl setenv 命令（供 App 启动的进程继承），随后自动运行
  连通性/可用性测试（scripts/test_connection.py）。

非交互模式（agent/自动化）：
  python3 scripts/setup_env.py --set BASE_URL=... --set API_KEY=... \
      [--set MODEL=...] [--set EFFORT=low] [--rc-file <path>]

已存在的环境变量会作为默认值显示，直接回车保留。
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_NAMES = (
    ("BASE_URL", "PROJECT_MEMORY_BASE_URL", "模型服务 API 根（chat/completions 协议的根地址，不要带 /chat/completions）"),
    ("API_KEY", "PROJECT_MEMORY_API_KEY", "模型服务密钥"),
    ("MODEL", "PROJECT_MEMORY_MODEL", "模型标识"),
    ("EFFORT", "PROJECT_MEMORY_REASONING_EFFORT", "推理档位"),
)


def fetch_models(base_url: str, api_key: str) -> list[str]:
    """OpenAI-compatible /models list; empty list on any failure."""
    url = f"{base_url.rstrip('/')}/models"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        return [entry["id"] for entry in payload.get("data", []) if isinstance(entry, dict)]
    except (OSError, urllib.error.HTTPError, json.JSONDecodeError, KeyError):
        return []


def pick_model(models: list[str]) -> str:
    print(f"探测到该端点提供 {len(models)} 个模型：")
    for index, model in enumerate(models[:20], start=1):
        print(f"  {index:3d}. {model}")
    if len(models) > 20:
        print(f"  … 共 {len(models)} 个，只列前 20")
    choice = input("输入编号或完整模型名（直接回车=第一个）：").strip()
    if not choice:
        return models[0]
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        return models[int(choice) - 1]
    return choice


def interactive(existing: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, env_name, description in _NAMES:
        default = existing.get(env_name, "")
        if key == "API_KEY":
            hint = f"（回车保留 {default[:4]}…）" if default else ""
            entered = getpass.getpass(f"{description}{hint}: ").strip()
            values[env_name] = entered or default
            continue
        entered = input(f"{description}[默认 {default}]：" if default else f"{description}：").strip()
        values[env_name] = entered or default
        if not values[env_name] and key in ("BASE_URL",):
            print("BASE_URL 必填。")
            sys.exit(2)
    if not values["PROJECT_MEMORY_MODEL"]:
        print("\n尝试探测模型列表……")
        models = fetch_models(values["PROJECT_MEMORY_BASE_URL"], values["PROJECT_MEMORY_API_KEY"])
        if models:
            values["PROJECT_MEMORY_MODEL"] = pick_model(models)
        else:
            print("端点未提供 /models（或探测失败），请手动输入模型标识。")
            values["PROJECT_MEMORY_MODEL"] = input("模型标识: ").strip()
    return values


def write_rc(rc_file: Path, values: dict[str, str]) -> None:
    lines = rc_file.read_text(encoding="utf-8").splitlines() if rc_file.exists() else []
    present = {
        line[len("export "):].split("=", 1)[0]
        for line in lines
        if line.startswith("export PROJECT_MEMORY_")
    }
    additions = [f"export {name}={value}" for name, value in values.items() if name not in present]
    if additions:
        with rc_file.open("a", encoding="utf-8") as handle:
            handle.write("\n".join([""] + additions + [""]))
        print(f"已写入 {rc_file}（{len(additions)} 行）")
    else:
        print(f"{rc_file} 里这些变量已存在，未重复写入")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="非交互赋值，可多次；KEY 为 BASE_URL/API_KEY/MODEL/EFFORT")
    parser.add_argument("--rc-file", type=Path, default=Path.home() / ".zshenv")
    parser.add_argument("--no-test", action="store_true", help="写入后不自动跑连通性测试")
    parser.add_argument("--skip-write", action="store_true", help="只收集与测试，不写 rc 文件")
    args = parser.parse_args()

    existing = {name: os.environ.get(name, "") for _, name, _ in _NAMES}
    if args.set:
        overrides = dict(piece.split("=", 1) for piece in args.set)
        values = dict(existing)
        for key, value in overrides.items():
            env_name = f"PROJECT_MEMORY_{key}"
            if env_name not in values:
                print(f"未知键 {key!r}（可用：BASE_URL/API_KEY/MODEL/EFFORT）")
                return 2
            values[env_name] = value
        if not values["PROJECT_MEMORY_BASE_URL"] or not values["PROJECT_MEMORY_API_KEY"]:
            print("非交互模式必须提供 BASE_URL 与 API_KEY（--set BASE_URL=… --set API_KEY=…）。")
            return 2
        if not values["PROJECT_MEMORY_MODEL"]:
            models = fetch_models(
                values["PROJECT_MEMORY_BASE_URL"], values["PROJECT_MEMORY_API_KEY"]
            )
            if models:
                values["PROJECT_MEMORY_MODEL"] = models[0]
                print(f"已自动探测并选择模型：{models[0]}")
            else:
                print("模型探测失败（端点未实现 /models）；非交互模式请用 --set MODEL=… 显式给出。")
                return 2
        if not values["PROJECT_MEMORY_REASONING_EFFORT"]:
            values["PROJECT_MEMORY_REASONING_EFFORT"] = "high"
    else:
        values = interactive(existing)

    if not args.skip_write:
        write_rc(args.rc_file, values)
    if sys.platform == "darwin" and not args.skip_write:
        print("\nmacOS 提示：若 Codex/Claude Code 从 App 启动（非终端），还需对 launchd 注入：")
        for name, value in values.items():
            print(f"  launchctl setenv {name} {value if name == 'PROJECT_MEMORY_BASE_URL' or name == 'PROJECT_MEMORY_MODEL' or name == 'PROJECT_MEMORY_REASONING_EFFORT' else '********'}")
        print("  （launchctl getenv <名> 可查；重启 App 生效）")

    if args.no_test:
        return 0
    print("\n填写完成，开始连通性与可用性测试……")
    env = {**os.environ, **values}
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve().parent / "test_connection.py")],
        env=env,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())