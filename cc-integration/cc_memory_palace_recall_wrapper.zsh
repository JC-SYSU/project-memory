#!/bin/zsh
# Claude Code 接入记忆宫殿召回的 wrapper（只读）。
# 记忆宫殿 server 以启动时的 os.getcwd() 精确绑定项目本地库；
# Claude Code 文档只承诺注入 CLAUDE_PROJECT_DIR，未承诺 spawn cwd，
# 故显式 cd 到项目根再 exec，避免赌未文档化的工作目录行为。
cd "${CLAUDE_PROJECT_DIR:-$PWD}" || exit 1
REPO="$(cd "$(dirname "$0")"/.. && pwd)"
# 解释器解析次序：显式环境变量 > PATH 查找 > 常见绝对路径
# （CC 从 App 启动时 PATH 被精简，command -v 可能落空）。
if [[ -n "${PROJECT_MEMORY_PYTHON:-}" && -x "${PROJECT_MEMORY_PYTHON}" ]]; then
  PY="${PROJECT_MEMORY_PYTHON}"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  PY=/opt/homebrew/bin/python3
elif [[ -x /usr/local/bin/python3 ]]; then
  PY=/usr/local/bin/python3
else
  echo "memory-palace wrapper: no python3 found; set PROJECT_MEMORY_PYTHON" >&2
  exit 1
fi
exec "$PY" -B "$REPO/src/memory_palace_recall_mcp_server.py"