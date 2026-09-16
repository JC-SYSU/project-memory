#!/usr/bin/env python3
"""Create a conservative, reviewable project-assignment table for a ChatGPT export."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMPORT = ROOT / "imports" / "chatgpt_web_export"

# Populate with your own projects: key -> (display label, absolute path, kind).
# kind is "local" for an on-disk project, "chatgpt" for an online-only session group.
PROJECTS = {
    "example_a": ("示例项目 A", "/absolute/path/to/example-a", "local"),
    "example_b": ("示例项目 B", "/absolute/path/to/example-b", "local"),
}

# Each record has exactly one target.  The list is intentionally conservative:
# broad research, personal tech support, and generic image tasks are left empty.
ASSIGNMENTS: dict[str, tuple[str, str, str]] = {}

def add(target: str, confidence: str, rationale: str, numbers: str) -> None:
    for number in numbers.split():
        if number in ASSIGNMENTS:
            raise ValueError(f"duplicate assignment: {number}")
        ASSIGNMENTS[number] = (target, confidence, rationale)

# Example: add(target, confidence, rationale, numbers) maps 4-digit session
# order numbers from the export to one target project.
add("example_a", "high", "会话主题与示例项目 A 的职责直接重合", "0001 0002")
add("example_b", "medium", "主题明确相关但需要人工确认", "0003")


def main() -> None:
    manifest = json.loads((IMPORT / "manifest.json").read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for session in manifest["sessions"]:
        order = session["file"].split("_", 1)[0]
        target_key, confidence, rationale = ASSIGNMENTS.get(order, ("", "", "没有和现有项目职责形成足够直接的对应关系"))
        label, path, kind = PROJECTS.get(target_key, ("", "", ""))
        rows.append(
            {
                "session_order": order,
                "session_file": f"sessions/{session['file']}",
                "title": session["title"],
                "created_at": session["created_at"] or "",
                "message_count": str(session["messages"]),
                "recommended_project": label,
                "project_path": path,
                "project_kind": kind,
                "confidence": confidence,
                "rationale": rationale,
            }
        )

    fields = list(rows[0])
    output = IMPORT / "PROJECT_ASSIGNMENTS.csv"
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["recommended_project"] or "（留空）" for row in rows)
    lines = [
        "# ChatGPT 历史对话—现有 Codex 项目归属建议",
        "",
        "对应表：`PROJECT_ASSIGNMENTS.csv`。每行只给出一个最合适的现有项目；未达到直接关联门槛时，目标项目字段留空。",
        "",
        "## 判定边界",
        "",
        "- 这是归属建议，不移动会话、不写入任何项目的记忆库，也不把同一会话复制给多个项目。",
        "- 依据为会话标题及其指向的任务主题；`high` 是主题与项目职责直接重合，`medium` 是明确相关但仍需人工确认。",
        "- 项目清单取自运行时的 `PROJECTS` 表；`chatgpt` 项目保留为候选，以免人为排除当前可见项目。",
        "- 无合适项目的对话有意留空；这些会话不应为了提高覆盖率而被强行归档。",
        "",
        "## 统计",
        "",
        f"- 总会话：{len(rows)}",
        f"- 已建议归属：{len(rows) - counts['（留空）']}",
        f"- 留空：{counts['（留空）']}",
        "",
        "| 建议项目 | 会话数 |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {count} |" for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))]
    (IMPORT / "ASSIGNMENT_README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {output}")
    print(f"assigned={len(rows) - counts['（留空）']} unassigned={counts['（留空）']}")
    for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
        print(f"{name}: {count}")


if __name__ == "__main__":
    main()
