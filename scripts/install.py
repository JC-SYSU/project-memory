#!/usr/bin/env python3
"""One-shot installer: repo location, Python deps, Codex and Claude Code integration.

Usage:
  python3 scripts/install.py            # dry run: print everything that would happen
  python3 scripts/install.py --apply    # run it

Steps, in order:
  1. Detect the clients: `codex` and `claude` on PATH.
  2. Repo location. The standard place is ~/.codex/memory-palace/ when Codex
     is installed, otherwise ~/.claude/memory-palace/. A repo elsewhere is
     copied there in full (.git, uncommitted changes and all), every file is
     compared by SHA-256, the old copy is deleted, and the installer re-runs
     from the new place. --keep-location skips this and registers in place.
  3. pip install -r requirements.txt, then an import check.
  4. Codex (if present): install_codex.py merges hooks.json and config.toml.
  5. Claude Code (if present): install_claude.py merges ~/.claude/settings.json
     hooks and ~/.claude.json mcpServers.
  6. continue-session skill copied into ~/.codex/skills and/or ~/.claude/skills
     (--no-skill to skip); session-read routing hook for Claude Code
     (--no-route-hook to skip; needs jq).

Every config file is backed up before writing and re-parsed after; a parse
failure restores the backup.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import install_claude  # noqa: E402
import install_codex  # noqa: E402

SKILL = "continue-session"
EXECUTABLES = (
    "cc-integration/cc_memory_palace_recall_wrapper.zsh",
    "cc-integration/cc_session_start_reminder.zsh",
    "cc-integration/route_session_read.sh",
)
ENV_NAMES = (
    "PROJECT_MEMORY_API_KEY",
    "PROJECT_MEMORY_BASE_URL",
    "PROJECT_MEMORY_MODEL",
    "PROJECT_MEMORY_REASONING_EFFORT",
)


def detect() -> tuple[bool, bool]:
    return shutil.which("codex") is not None, shutil.which("claude") is not None


def standard_location(home: Path, has_codex: bool) -> Path:
    return home / (".codex" if has_codex else ".claude") / "memory-palace"


def _manifest(root: Path) -> dict[str, tuple]:
    """Relative path -> (kind, size, sha256 | link target) for every entry under root."""
    out: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                out[rel] = ("link", os.readlink(path))
            elif path.is_dir():
                out[rel] = ("dir",)
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                out[rel] = ("file", path.stat().st_size, digest)
    return out


def relocate(source: Path, target: Path, apply: bool) -> tuple[list[str], bool]:
    """Move the repo to target (copy, verify, delete source). Returns (messages, moved)."""
    source = source.resolve()
    if target.exists() and source.samefile(target):
        return [f"location: repo already at {target}"], False
    if target.exists():
        raise SystemExit(
            f"location: {target} already exists and is a different directory. "
            f"Keep one copy: delete or rename one of {target} and {source}, then re-run "
            f"(or pass --keep-location to register {source} where it is)."
        )
    if target.resolve().is_relative_to(source):
        raise SystemExit(f"location: {target} is inside {source}; pass --keep-location")
    if Path(sys.executable).resolve().is_relative_to(source):
        raise SystemExit(
            f"location: the running interpreter {sys.executable} lives inside {source}; "
            "moving the repo would delete it. Use an interpreter outside the repo, "
            "or pass --keep-location."
        )
    plan = [
        f"location: repo is at {source}; standard location is {target}",
        f"location: {'copying' if apply else 'would copy'} the whole repo (including .git and "
        f"uncommitted changes) to {target}, compare every file by SHA-256, then delete {source}",
    ]
    if not apply:
        return plan + ["location: the commands below are shown with the new location"], False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True)
    if _manifest(source) != _manifest(target):
        shutil.rmtree(target)
        raise SystemExit(f"location: copy to {target} did not match the original; "
                         f"removed the copy, {source} is unchanged")
    shutil.rmtree(source)
    return plan + [f"location: moved to {target}; {source} deleted"], True


def ensure_executable(repo: Path, apply: bool) -> list[str]:
    """Zip downloads lose the exec bit; hooks and the MCP wrapper are run directly."""
    missing = [rel for rel in EXECUTABLES if not os.access(repo / rel, os.X_OK)]
    if not missing:
        return []
    if not apply:
        return [f"chmod: would add +x to {', '.join(missing)}"]
    for rel in missing:
        path = repo / rel
        path.chmod(path.stat().st_mode | 0o111)
    return [f"chmod: added +x to {', '.join(missing)}"]


def _same_tree(a: Path, b: Path) -> bool:
    return a.is_dir() and b.is_dir() and _manifest(a) == _manifest(b)


def install_skill(repo: Path, skill_dirs: list[Path], apply: bool) -> list[str]:
    """Copy skills/continue-session into each skills dir; dirs that resolve to one path count once."""
    source = repo / "skills" / SKILL
    messages: list[str] = []
    seen: set[Path] = set()
    for skills in skill_dirs:
        real = skills.resolve()
        if real in seen:
            messages.append(f"skill: {skills} is the same directory as one above, skipped")
            continue
        seen.add(real)
        dest = skills / SKILL
        if dest.exists():
            if _same_tree(source, dest):
                messages.append(f"skill: {dest} already up to date")
            else:
                messages.append(f"skill: {dest} exists and differs from the repo copy, left alone "
                                "(delete it and re-run to update)")
            continue
        if not apply:
            messages.append(f"skill: would copy {SKILL} to {dest}")
            continue
        skills.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest)
        messages.append(f"skill: copied {SKILL} to {dest}")
    return messages


def pip_install(repo: Path, apply: bool) -> list[str]:
    if not apply:
        return ["deps: would run pip install -r requirements.txt"]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "-r", str(repo / "requirements.txt")]
    )
    if result.returncode != 0:
        raise SystemExit("pip install failed, stopping before any configuration change")
    return ["deps: installed from requirements.txt"]


def import_check(repo: Path, apply: bool) -> list[str]:
    probe = (
        "import memory_palace.worker_orchestrator, memory_palace.extraction_core, "
        "memory_palace.segmenter, mcp"
    )
    if not apply:
        return ["imports: would verify core package imports (PYTHONPATH=src)"]
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    result = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(
            "core package import check failed:\n" + result.stderr.decode("utf-8", "replace")
        )
    return ["imports: core package imports OK"]


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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="actually install (default: dry-run)")
    parser.add_argument("--keep-location", action="store_true",
                        help="register the repo where it is instead of moving it to the standard location")
    parser.add_argument("--no-skill", action="store_true", help="do not copy the continue-session skill")
    parser.add_argument("--no-route-hook", action="store_true",
                        help="do not register the Claude Code session-read routing hook")
    parser.add_argument("--hooks-path", type=Path, default=None, help="Codex hooks.json (default ~/.codex/hooks.json)")
    parser.add_argument("--config-path", type=Path, default=None, help="Codex config.toml (default ~/.codex/config.toml)")
    args = parser.parse_args()

    home = Path.home()
    has_codex, has_claude = detect()
    if not (has_codex or has_claude):
        raise SystemExit("clients: neither `codex` nor `claude` is on PATH; install at least one first")
    profile = "B" if has_codex and has_claude else ("A" if has_codex else "C")
    messages = [f"clients: codex={'yes' if has_codex else 'no'}, claude={'yes' if has_claude else 'no'} "
                f"-> profile {profile}"]

    repo = SOURCE
    if args.keep_location:
        messages.append(f"location: --keep-location, registering {SOURCE} in place")
    else:
        target = standard_location(home, has_codex)
        moved_msgs, moved = relocate(SOURCE, target, args.apply)
        messages += moved_msgs
        if moved:
            print("\n".join(messages), flush=True)
            print(f"\nre-running the installer from {target} (if your shell is in the old "
                  f"directory, cd {target})\n", flush=True)
            script = target / "scripts" / "install.py"
            os.chdir(target)
            os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])
        repo = target

    messages += pip_install(SOURCE, args.apply)
    messages += import_check(SOURCE, args.apply)
    messages += check_env()
    messages += ensure_executable(SOURCE, args.apply)

    skill_dirs: list[Path] = []
    if has_codex:
        install_codex.TEMPLATES = SOURCE / "templates"
        install_codex.REPO = repo
        install_codex.HOOKS = args.hooks_path or home / ".codex" / "hooks.json"
        install_codex.CONFIG = args.config_path or home / ".codex" / "config.toml"
        hook_msgs, _ = install_codex.merge_hooks(args.apply)
        messages += hook_msgs
        messages += install_codex.merge_config(args.apply)
        skill_dirs.append(home / ".codex" / "skills")
    if has_claude:
        messages += install_claude.install(
            SOURCE / "templates", repo, sys.executable, home, args.apply,
            route_hook=not args.no_route_hook,
        )
        skill_dirs.append(home / ".claude" / "skills")
    if not args.no_skill:
        messages += install_skill(SOURCE, skill_dirs, args.apply)

    print("\n".join(messages))
    if not args.apply:
        print("\nRun with --apply to install.")
        return 0
    print("\n下一步：python3 scripts/setup_env.py   # 填写模型服务环境变量（只问 URL/Key）并自动做连通性测试")
    if has_codex:
        print("Codex：hooks 写入后需要重新信任（GUI 设置 → 钩子，或 TUI /hooks），再用 codex mcp list 确认。")
    if has_claude:
        print("Claude Code：新开一个会话生效（resume 的会话沿用旧工具列表），"
              "用 claude mcp get memory-palace-recall 确认 Connected。")
    print("然后参考 INSTALL.md 6.5 扫描历史会话入队。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
