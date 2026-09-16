#!/usr/bin/env python3
"""分批搬运器：把历史 Claude Code 会话送入记忆宫殿队列。

只做三件事：找会话 → 走正式接单门（复用 cc_capture_hook_runner 的准入门
与幂等逻辑，不自己另造标准）→ 记清单（backfill-manifest.csv，可断点续跑）。

排队策略：整批入队时逐个压住唤醒（wake=noop），批末统一唤醒一次
Drainer，由它串行慢慢消化；随时 Ctrl+C，已入队任务在 SQLite 里不会丢。
重跑安全：同一会话同一可见条数 = duplicate，不产生第二次抽取。
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cc_capture_hook_runner as runner  # noqa: E402
from cc_transcript_compiler import compile_visible_messages  # noqa: E402

MANIFEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backfill-manifest.csv")
_PROJECTS_GLOB = os.path.expanduser("~/.claude/projects/*/*.jsonl")


def session_head(path: str) -> tuple[str | None, str | None]:
    """从会话文件头部提取 sessionId 与 cwd（各取首个出现的非空值）。"""
    sid = cwd = None
    with open(path, encoding="utf-8", errors="replace") as handle:
        for _ in range(40):
            line = handle.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if not cwd and isinstance(record.get("cwd"), str) and record["cwd"]:
                cwd = record["cwd"]
            if not sid and isinstance(record.get("sessionId"), str) and record["sessionId"]:
                sid = record["sessionId"]
            if cwd and sid:
                break
    return sid, cwd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只列清单，不入队不唤醒")
    parser.add_argument(
        "--exclude-cwd", action="append", default=[],
        help="精确等于该 cwd 的会话不搬运（如 /home/user）",
    )
    parser.add_argument("--limit", type=int, default=0, help="本轮最多搬运会话数（0=不限）")
    args = parser.parse_args(argv)

    rows = []
    stats = {"dry": 0, "accepted": 0, "duplicate": 0, "ignored": 0, "error": 0, "skipped": 0}
    ts_now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    files = sorted(glob.glob(_PROJECTS_GLOB), key=os.path.getmtime)

    for path in files:
        sid, cwd = session_head(path)
        if not sid or not cwd:
            rows.append((ts_now, path, "-", "-", "-", "skipped:no_head", ""))
            stats["skipped"] += 1
            continue
        if cwd in args.exclude_cwd:
            rows.append((ts_now, path, sid, cwd, "-", "-", "skipped:excluded", ""))
            stats["skipped"] += 1
            continue
        try:
            messages = compile_visible_messages(path)
        except Exception as exc:  # 编译失败仅记录，不入队（fail-soft per item）
            rows.append((ts_now, path, sid, cwd, "-", "-", "error:compile", type(exc).__name__))
            stats["error"] += 1
            continue
        chars = sum(len(m[2]) for m in messages)
        if args.dry_run:
            disposition = "dry" if messages else "dry:zero_visible_would_ignore"
            rows.append((ts_now, path, sid, cwd, len(messages), chars, disposition, ""))
            stats["dry"] += 1
            continue
        if stats["accepted"] + stats["duplicate"] >= args.limit > 0:
            rows.append((ts_now, path, sid, cwd, len(messages), chars, "skipped:limit", ""))
            stats["skipped"] += 1
            continue
        try:
            disposition = runner.process_payload(
                {
                    "session_id": sid,
                    "transcript_path": path,
                    "cwd": cwd,
                    "hook_event_name": "PreCompact",
                    "trigger": "manual",
                },
                wake=lambda env: None,  # 压住逐个唤醒，批末统一唤醒
            )
        except Exception as exc:
            rows.append((ts_now, path, sid, cwd, len(messages), chars, "error:intake", type(exc).__name__))
            stats["error"] += 1
            continue
        key = disposition.split(":", 1)[0]
        stats[key] = stats.get(key, 0) + 1
        rows.append((ts_now, path, sid, cwd, len(messages), chars, disposition, ""))

    new_file = not os.path.lexists(MANIFEST)
    with open(MANIFEST, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new_file:
            writer.writerow(["ts", "file", "session_id", "cwd", "visible_msgs", "visible_chars", "disposition", "note"])
        writer.writerows(rows)

    for row in rows:
        print(f"  {row[6]:28s} {row[4]}msg/{row[5]}ch  {row[3]}  {str(row[2])[:8]}")
    print("stats:", stats, "| manifest:", MANIFEST)

    if not args.dry_run and (stats["accepted"] or stats["duplicate"]):
        runner._wake_drainer(runner._drainer_env(dict(os.environ)))
        print("已统一唤醒 Drainer 串行消化（进度可问我，或看清单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
