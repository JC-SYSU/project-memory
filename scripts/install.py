#!/usr/bin/env python3
"""One-shot installer: Python deps + Codex integration.

Usage:
  python3 scripts/install.py            # dry run: print everything that would happen
  python3 scripts/install.py --apply    # run it: pip deps, import check, register hooks + MCP

The Codex registration step reuses install_codex.py: merges hooks.json
(PreCompact + UserPromptSubmit) and appends [mcp_servers.memory_palace_recall]
to ~/.codex/config.toml, with backups and post-write validation.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO / "requirements.txt"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_codex  # noqa: E402


def pip_install(apply: bool) -> list[str]:
    if not apply:
        return ["deps: would run pip install -r requirements.txt"]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)]
    )
    if result.returncode != 0:
        raise SystemExit("pip install failed, stopping before any configuration change")
    return ["deps: installed from requirements.txt"]


def import_check(apply: bool) -> list[str]:
    probe = (
        "import memory_palace.worker_orchestrator, memory_palace.extraction_core, "
        "memory_palace.segmenter"
    )
    if not apply:
        return ["imports: would verify core package imports (PYTHONPATH=src)"]
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            "core package import check failed:\n" + result.stderr.decode("utf-8", "replace")
        )
    return ["imports: core package imports OK"]


ENV_NAMES = (
    "PROJECT_MEMORY_API_KEY",
    "PROJECT_MEMORY_BASE_URL",
    "PROJECT_MEMORY_MODEL",
    "PROJECT_MEMORY_REASONING_EFFORT",
)


def check_env() -> list[str]:
    """Warn on missing model-service env vars; extraction fails without them."""
    missing = [name for name in ENV_NAMES if not os.environ.get(name)]
    if not missing:
        return ["env: 四个 PROJECT_MEMORY_* 变量均已设置"]
    return [
        "env: 警告，以下变量在当前进程环境中缺失（抽取任务会失败）："
        + ", ".join(missing)
        + "；配置方法见 INSTALL.md「环境变量」"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually install (default: dry-run)")
    parser.add_argument("--hooks-path", type=Path, default=None)
    parser.add_argument("--config-path", type=Path, default=None)
    args = parser.parse_args()

    messages: list[str] = []
    messages += pip_install(args.apply)
    messages += import_check(args.apply)
    messages += check_env()
    if args.hooks_path is not None:
        install_codex.HOOKS = args.hooks_path
    if args.config_path is not None:
        install_codex.CONFIG = args.config_path
    hook_msgs, _ = install_codex.merge_hooks(args.apply)
    messages += hook_msgs
    messages += install_codex.merge_config(args.apply)

    print("\n".join(messages))
    if not args.apply:
        print("\nRun with --apply to install. After writing hooks, Codex will ask to "
              "re-trust them (/hooks).")
    else:
        print("\n下一步：python3 scripts/setup_env.py   # 填写模型服务环境变量（只问 URL/Key）并自动做连通性测试")
        print("然后参考 INSTALL.md 6.5 扫描历史会话入队。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())