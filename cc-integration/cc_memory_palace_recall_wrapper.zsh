#!/bin/zsh
# Claude Code 接入记忆宫殿召回的 wrapper（只读）。
# 记忆宫殿 server 以启动时的 os.getcwd() 精确绑定项目本地库；
# Claude Code 文档只承诺注入 CLAUDE_PROJECT_DIR，未承诺 spawn cwd，
# 故显式 cd 到项目根再 exec，避免赌未文档化的工作目录行为。
cd "${CLAUDE_PROJECT_DIR:-$PWD}" || exit 1
REPO="$(cd "$(dirname "$0")"/.. && pwd)"
exec "$(command -v python3)" -B "$REPO/src/memory_palace_recall_mcp_server.py"