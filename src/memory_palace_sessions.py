"""CLI: list a project's sessions or export one clean conversation.

    python3 memory_palace_sessions.py list [--project DIR] [--json] [--include-empty]
    python3 memory_palace_sessions.py read SESSION_ID [--project DIR] [--json] [--out FILE]

Reads the clients' raw transcripts directly (no palace snapshots, no model
calls, no writes except --out).  ``read`` exports the whole session; use the
MCP tool for paged reading inside a conversation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from memory_palace.session_browser import (  # noqa: E402
    SessionBrowserError,
    list_sessions,
    read_session,
)

_ROLE_LABEL = {"user": "用户", "assistant": "助手"}


def _project(value: str | None) -> str:
    path = os.path.abspath(value or os.getcwd())
    if not os.path.isdir(path):
        raise SystemExit(f"项目目录不存在：{path}")
    return path


def _cmd_list(args: argparse.Namespace) -> int:
    sessions = list_sessions(_project(args.project), include_empty=args.include_empty)
    if args.json:
        print(json.dumps([s.as_dict() for s in sessions], ensure_ascii=False, indent=1))
        return 0
    for s in sessions:
        when = (s.updated_at or "")[:16].replace("T", " ")
        if s.available:
            print(f"{s.session_id}  {s.client:11}  {when:16}  {s.message_count:4} 条  {s.title}")
        else:
            print(f"{s.session_id}  {s.client:11}  {'':16}     -   [原始文件已不在，无法读取]")
    print(f"共 {len(sessions)} 个会话，其中 {sum(not s.available for s in sessions)} 个无法读取")
    return 0


def _full_session(project: str, session_id: str) -> dict:
    page = read_session(project, session_id, max_messages=10**9, max_chars=10**12)
    return {"session": page["session"], "messages": page["messages"]}


def _markdown(data: dict) -> str:
    s = data["session"]
    lines = [
        f"# {s['title'] or s['session_id']}",
        "",
        f"- session_id: `{s['session_id']}`",
        f"- client: {s['client']}",
        f"- 时间: {s['started_at']} — {s['updated_at']}",
        f"- 消息: {s['message_count']} 条（用户 {s['user_message_count']}）",
        "",
    ]
    for m in data["messages"]:
        lines += [f"## {_ROLE_LABEL.get(m['role'], m['role'])} · {m['timestamp']}", "", m["text"].rstrip(), ""]
    return "\n".join(lines)


def _cmd_read(args: argparse.Namespace) -> int:
    try:
        data = _full_session(_project(args.project), args.session_id)
    except SessionBrowserError as exc:
        messages = {
            "unknown_session": "找不到该会话",
            "ambiguous_session": "前缀匹配到多个会话，请给更长的 id",
            "session_unavailable": "该会话的原始文件已不在，无法读取",
            "invalid_argument": "session_id 格式不对",
        }
        print(messages.get(str(exc), str(exc)), file=sys.stderr)
        return 2
    text = json.dumps(data, ensure_ascii=False, indent=1) if args.json else _markdown(data)
    if args.out:
        try:
            with open(args.out, "x", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except FileExistsError:
            print(f"文件已存在，未覆盖：{args.out}", file=sys.stderr)
            return 2
        print(f"已写入 {args.out}（{data['session']['message_count']} 条消息）")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_palace_sessions", description="列出项目会话或导出一个会话的干净对话")
    sub = parser.add_subparsers(dest="command", required=True)
    p_list = sub.add_parser("list", help="列出项目会话")
    p_list.add_argument("--project")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--include-empty", action="store_true", help="包含无可见消息的会话")
    p_list.set_defaults(func=_cmd_list)
    p_read = sub.add_parser("read", help="导出一个会话的干净对话")
    p_read.add_argument("session_id")
    p_read.add_argument("--project")
    p_read.add_argument("--json", action="store_true")
    p_read.add_argument("--out", help="写入文件（文件已存在时拒绝覆盖）")
    p_read.set_defaults(func=_cmd_read)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())