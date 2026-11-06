#!/bin/zsh
# SessionStart（memory-palace）：仅当当前项目存在记忆宫殿库时注入一句召回提醒。
# 每会话一次，非每轮；无库项目与 resume 会话零输出。
# 库路径与 src/memory_palace/project_paths.py 绑定逻辑一致：
#   $cwd/.memory-palace/memory-palace.sqlite3
emulate -L zsh
input=$(cat)
source=$(printf '%s' "$input" | /usr/bin/python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("source", ""))
except Exception:
    pass')
[ "$source" = "resume" ] && exit 0
dir="${CLAUDE_PROJECT_DIR:-}"
if [ -z "$dir" ]; then
  dir=$(printf '%s' "$input" | /usr/bin/python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("cwd", ""))
except Exception:
    pass')
fi
case "$dir" in
  /*) ;;
  *) exit 0 ;;
esac
[ -f "$dir/.memory-palace/memory-palace.sqlite3" ] || exit 0
echo "本项目配有记忆宫殿库（.memory-palace/）。问题涉及过往裁决、约束、偏好、实验结果，用户提到之前/上次/当初，或你不确定当前上下文是否足够时，先调用 memory_recall（每次恰好四组同方向关键词）再回答；与历史明确无关的全新问题不要调用。"
