#!/bin/bash
# Claude Code PreToolUse hook: deny direct reads of raw session transcripts and point the
# agent at the memory palace's memory_read_session tool, which exports a clean conversation
# (tool calls, injected blocks, images and base64 removed).
#
# Register for matcher "Bash|Read|Grep|Glob" (see templates/claude.settings.hooks.example.json).
#
# Denied: ~/.claude/projects/**/*.jsonl, .../subagents, session-uuid paths under
#   ~/.claude/projects, Grep/Glob over a whole session directory, ~/.codex/sessions,
#   ~/.codex/archived_sessions.
# Allowed: ~/.claude/projects/<dir>/memory/, /tmp/memory-palace-sessions/ exports, plain
#   directory listings, ~/.codex/session_index.jsonl.
# Exempt working directories: colon-separated absolute paths in MEMORY_PALACE_DEV_DIRS
#   (for maintaining the palace itself, which needs raw transcripts). Unset = no exemption.
# Requires jq. Fails open: any parse problem exits 0 with no output.
jq --arg dev_dirs "${MEMORY_PALACE_DEV_DIRS:-}" '
  def raw_transcript:
    test("\\.codex/(archived_)?sessions(/|\\b)")
    or test("\\.claude/projects/[^\\s\"'"'"']*(\\.jsonl|/subagents)")
    or (test("\\.claude/projects") and test("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"))
    or (test("\\.claude/projects") and test("jsonl"));
  def searches_session_dir:
    (.tool_name == "Grep" or .tool_name == "Glob")
    and ((.tool_input.path // "") | test("\\.claude/projects(/[^/]+)?/?$"));
  def exempt:
    (.cwd // "") as $cwd
    | [ $dev_dirs | split(":")[] | select(length > 0) | rtrimstr("/") ]
    | any(. as $d | $cwd == $d or ($cwd | startswith($d + "/")));
  ( [ .tool_input.command, .tool_input.file_path, .tool_input.path, .tool_input.pattern, .tool_input.glob ]
    | map(select(type == "string")) | join("\n") ) as $text
  | if (($text | raw_transcript) or searches_session_dir) and (exempt | not) then
      {hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: "直接读取原始会话文件（~/.claude/projects 下的 jsonl、~/.codex/sessions）已被会话读取路由规则拦截。读取、查看或总结某个会话：先用 ToolSearch 加载 mcp__memory-palace-recall__memory_read_session，传入 session_id（完整 id 或至少 8 位前缀）；返回 status=other_project 时，带 project=<found_in_project> 再调用一次；然后用 Read 读取它返回的临时文件 path。要在该会话上继续工作，使用 continue-session skill。不要改用 find、cat、python 或自写脚本绕过。"
      }}
    else empty end
' 2>/dev/null
exit 0