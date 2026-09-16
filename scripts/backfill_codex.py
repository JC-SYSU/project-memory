#!/usr/bin/env python3
"""Scan Codex session JSONL and batch-queue them for memory extraction.

Flow (designed for first installation):
  1. run WITHOUT --apply: read-only survey + a concrete batching suggestion
     (per-project counts, sizes, batch plan), nothing is written;
  2. review it, then re-run with --apply to actually enqueue.

Idempotent: each session goes through the same admission gate as the
PreCompact hook (codex_precompact_hook_runner.process_payload), so sessions
already queued report "duplicate" and are skipped.  Sessions without a
session_meta record, or whose cwd no longer exists, are reported and never
enqueued.

Drainer waking is suppressed during the batch and triggered exactly once at
the end, so the queue is drained serially instead of spawning one worker per
session.
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))

os.environ.setdefault("CODEX_MEMORY_NO_DRAINER", "1")  # batch wakes once at the end

from codex_precompact_hook_runner import process_payload  # noqa: E402
from memory_palace.project_paths import (  # noqa: E402
    ProjectPathError,
    read_session_metadata,
)

MANIFEST = REPO / "backfill-codex-manifest.csv"


def _line_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for _ in handle:
            count += 1
    return count


def survey(sessions_root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (enqueueable, skipped): per-session records with metadata."""
    enqueueable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(sessions_root / "**" / "*.jsonl"), recursive=True)):
        path = Path(path)
        size = path.stat().st_size
        record = {"path": path, "size": size}
        try:
            meta = read_session_metadata(path)
        except (ProjectPathError, OSError) as exc:
            record["reason"] = type(exc).__name__.replace("ProjectPathError", "no-session-meta")
            skipped.append(record)
            continue
        record["session_id"] = meta.session_id
        record["cwd"] = meta.cwd
        if not os.path.isdir(meta.cwd):
            record["reason"] = "cwd-missing"
            skipped.append(record)
            continue
        record["lines"] = _line_count(path)
        enqueueable.append(record)
    return enqueueable, skipped


def print_survey(enqueueable: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
    total_bytes = sum(r["size"] for r in enqueueable)
    groups: Counter[str] = Counter(r["cwd"] for r in enqueueable)
    print(f"Codex 会话扫描结果（只读）：")
    print(f"  可入队（有 session_meta 且项目目录存在）：{len(enqueueable)} 个，共 {total_bytes / 1e6:.1f} MB")
    print(f"  跳过（无 session_meta / 项目目录已不存在）：{len(skipped)} 个")
    if skipped:
        reasons = Counter(str(r.get("reason")) for r in skipped)
        print(f"    跳过原因分布：{dict(reasons)}")
    print(f"\n按项目分组（建议按组整批入队）：")
    for cwd, count in groups.most_common():
        print(f"  {count:4d} 个  {cwd}")


def batch_plan(enqueueable: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
    # 组内按体积降序，组间稳定：大文件先入队，模型配额压力靠 drainer 串行消化。
    ordered = sorted(enqueueable, key=lambda r: (-r["size"], r["cwd"]))
    return [ordered[i : i + batch_size] for i in range(0, len(ordered), batch_size)]


def apply_batch(batch: list[dict[str, Any]], manifest: Any) -> Counter[str]:
    stats: Counter[str] = Counter()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for record in batch:
        payload = {
            "session_id": record["session_id"],
            "cwd": record["cwd"],
            "hook_event_name": "PreCompact",
            "model": "backfill",
            "turn_id": "0",
            "trigger": "manual",
            "transcript_path": str(record["path"]),
        }
        try:
            disposition = process_payload(payload)
        except Exception as exc:
            stats["error"] += 1
            manifest.writerow([ts, record["path"], record["session_id"], record["cwd"], "error:" + type(exc).__name__])
            continue
        stats[disposition] += 1
        manifest.writerow([ts, record["path"], record["session_id"], record["cwd"], disposition])
    return stats


def wake_once() -> None:
    import subprocess

    subprocess.Popen(
        [
            sys.executable,
            "-B",
            str(SRC / "memory_palace_worker_runner.py"),
            "--sessions-root",
            str(Path.home() / ".codex" / "sessions"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path, default=Path.home() / ".codex" / "sessions")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--apply", action="store_true", help="actually enqueue (default: survey only)")
    args = parser.parse_args()

    if not args.sessions_root.is_dir():
        print(f"sessions 目录不存在：{args.sessions_root}")
        return 1

    enqueueable, skipped = survey(args.sessions_root)
    print_survey(enqueueable, skipped)
    batches = batch_plan(enqueueable, args.batch_size)
    print(f"\n入队批次建议（每组内先大后小，共 {len(batches)} 批，drainer 串行消化）：")
    for index, batch in enumerate(batches, start=1):
        print(f"  批 {index:3d}: {len(batch):3d} 个会话，{sum(r['size'] for r in batch) / 1e6:.1f} MB")

    if not args.apply:
        print("\n以上为只读统计与建议；确认后运行（幂等，可随时中断重跑，已入队不会重复）：")
        print(f"  python3 scripts/backfill_codex.py --apply [--batch-size N]")
        return 0

    with args.manifest.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if args.manifest.stat().st_size == 0:
            writer.writerow(["ts", "path", "session_id", "cwd", "disposition"])
        totals: Counter[str] = Counter()
        for index, batch in enumerate(batches, start=1):
            print(f"批 {index}/{len(batches)} 入队中……")
            totals += apply_batch(batch, writer)
            print(f"  本批累计：{dict(totals)}")
    wake_once()
    print(f"\n完成。入队结果：{dict(totals)}（accepted=新入队，duplicate=已存在，ignored=被拒，error=异常）")
    print(f"清单已写：{args.manifest}")
    print("drainer 已唤醒一次，队列将由它串行抽取；观察库存用：python3 src/memory_palace_worker_runner.py --sessions-root <根>（或交给 hook/launchd）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())