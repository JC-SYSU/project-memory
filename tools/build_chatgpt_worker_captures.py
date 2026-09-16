#!/usr/bin/env python3
"""Compile assigned ChatGPT web-export Markdown transcripts into Capture JSONL.

One JSONL per source Markdown, shaped for the existing Memory Palace worker
chain (read_session_metadata + normalize_capture).  Selection follows
PROJECT_ASSIGNMENTS.csv: non-empty recommended_project, project_kind=local,
confidence=high.  No model calls, no worker runs, no .memory-palace writes.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_BASE = Path(__file__).resolve().parents[1] / "imports" / "chatgpt_web_export"
DEFAULT_ASSIGNMENTS = DEFAULT_BASE / "PROJECT_ASSIGNMENTS.csv"
DEFAULT_SESSIONS_DIR = DEFAULT_BASE / "sessions"
DEFAULT_OUTPUT_DIR = DEFAULT_BASE / "worker-captures"
DEFAULT_MANIFEST = DEFAULT_OUTPUT_DIR / "capture_manifest.json"
DEFAULT_MIN_CONFIDENCE = "high"

CONFIDENCE_ORDER = ["low", "medium", "high"]
CAPTURE_FORMAT = "chatgpt-markdown-capture-v1"

# Exactly these three H2 headings open a message block; anything else
# (including look-alikes with trailing spaces) stays inside message bodies.
BOUNDARY_RE = re.compile(r"^## (user|ChatGPT|Custom user info)$")
COMMENT_RE = re.compile(
    r"^<!-- message_id: ([0-9a-fA-F-]{36}); created_at: ([^>]+?) -->$"
)
FRONTMATTER_FIELDS = ("title", "conversation_id", "created_at")


class CompileError(ValueError):
    """A source Markdown violated the fixed layout; file and line included."""

    def __init__(self, path: Path, line_number: int, detail: str) -> None:
        self.path = path
        self.line_number = line_number
        super().__init__(f"{path}:{line_number}: {detail}")


@dataclass(slots=True)
class MessageBlock:
    """One parsed message block with its provenance fields."""

    author: str
    message_id: str
    created_at: str
    start_line: int
    body: str
    attachments: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SessionTranscript:
    """Everything one source Markdown contributes to its Capture."""

    title: str
    conversation_id: str
    blocks: list[MessageBlock]


def _confidence_at_least(value: str, minimum: str) -> bool:
    if value not in CONFIDENCE_ORDER or minimum not in CONFIDENCE_ORDER:
        return False
    return CONFIDENCE_ORDER.index(value) >= CONFIDENCE_ORDER.index(minimum)


def select_assignments(
    assignments_path: Path, min_confidence: str = DEFAULT_MIN_CONFIDENCE
) -> list[dict[str, str]]:
    """Return assignment rows passing the frozen selection filter."""
    with assignments_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    selected = []
    for row in rows:
        if not (row.get("recommended_project") or "").strip():
            continue
        if row.get("project_kind") != "local":
            continue
        if not _confidence_at_least(
            (row.get("confidence") or "").strip(), min_confidence
        ):
            continue
        selected.append(row)
    return selected


def _parse_frontmatter(path: Path, lines: list[str]) -> dict[str, str]:
    """Read the fixed title/conversation_id/created_at frontmatter fields."""
    if not lines or lines[0].strip() != "---":
        raise CompileError(path, 1, "missing frontmatter opening '---'")
    try:
        close = next(
            i for i in range(1, len(lines)) if lines[i].strip() == "---"
        )
    except StopIteration:
        raise CompileError(path, len(lines), "unterminated frontmatter") from None
    fields: dict[str, str] = {}
    for i in range(1, close):
        match = re.match(r"^([A-Za-z_]+): (.*)$", lines[i])
        if match is None:
            raise CompileError(path, i + 1, f"malformed frontmatter line: {lines[i]!r}")
        key, raw = match.groups()
        if key in FRONTMATTER_FIELDS:
            try:
                fields[key] = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise CompileError(
                    path, i + 1, f"invalid JSON value for frontmatter {key!r}: {raw!r}"
                ) from exc
    missing = [key for key in FRONTMATTER_FIELDS if key not in fields]
    if missing:
        raise CompileError(path, close + 1, f"frontmatter missing {missing}")
    if not isinstance(fields["conversation_id"], str) or not fields["conversation_id"]:
        raise CompileError(path, close + 1, "conversation_id must be a non-empty string")
    return fields


def parse_session_markdown(path: Path) -> SessionTranscript:
    """Parse one transcript into ordered message blocks.

    Only exact '## user', '## ChatGPT' and '## Custom user info' lines split
    messages.  Every block must carry a 'message_id; created_at' comment as
    its first non-empty content line, or the file is rejected with its line.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    frontmatter = _parse_frontmatter(path, lines)

    boundaries: list[tuple[int, str]] = [
        (i, BOUNDARY_RE.match(line).group(1))  # type: ignore[union-attr]
        for i, line in enumerate(lines)
        if BOUNDARY_RE.match(line)
    ]
    if not boundaries:
        raise CompileError(path, 1, "no message blocks found")

    blocks: list[MessageBlock] = []
    for index, (line_index, author) in enumerate(boundaries):
        end = boundaries[index + 1][0] if index + 1 < len(boundaries) else len(lines)
        comment_line = next(
            (j for j in range(line_index + 1, end) if lines[j].strip()), None
        )
        if comment_line is None:
            raise CompileError(path, line_index + 2, f"## {author} block has no content")
        match = COMMENT_RE.match(lines[comment_line].strip())
        if match is None:
            raise CompileError(
                path,
                comment_line + 1,
                f"## {author} block does not start with a message_id comment",
            )
        message_id, created_at = match.groups()

        body_lines: list[str] = []
        attachments: list[str] = []
        for j in range(comment_line + 1, end):
            line = lines[j]
            if line.startswith("**Attached export files:**"):
                attachments = [
                    name.strip()
                    for name in re.findall(r"`([^`]+)`", line)
                    if name.strip()
                ]
                continue
            body_lines.append(line)
        blocks.append(
            MessageBlock(
                author=author,
                message_id=message_id,
                created_at=created_at,
                start_line=line_index + 1,  # 1-based source Markdown line
                body="\n".join(body_lines).strip("\n"),
                attachments=attachments,
            )
        )
    return SessionTranscript(
        title=frontmatter["title"],
        conversation_id=frontmatter["conversation_id"],
        blocks=blocks,
    )


def _user_record(block: MessageBlock) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "user_message",
        "message": block.body,
        "chatgpt_message_id": block.message_id,
        "source_markdown_line": block.start_line,
    }
    if block.attachments:
        payload["attached_export_files"] = block.attachments
    return {"type": "event_msg", "payload": payload, "timestamp": block.created_at}


def _assistant_record(block: MessageBlock) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": block.body}],
        "chatgpt_message_id": block.message_id,
        "source_markdown_line": block.start_line,
    }
    if block.attachments:
        payload["attached_export_files"] = block.attachments
    return {"type": "response_item", "payload": payload, "timestamp": block.created_at}


def build_records(
    transcript: SessionTranscript,
    *,
    source_markdown: Path,
    assignments_path: Path,
    target_project_root: str,
) -> list[dict[str, Any]]:
    """Assemble the full JSONL record list for one transcript."""
    session_id = f"chatgpt-web-v1:{transcript.conversation_id}"
    import_meta = {
        "type": "import_meta",
        "payload": {
            "format": CAPTURE_FORMAT,
            "source_platform": "chatgpt_web_export",
            "source_markdown": str(source_markdown),
            "conversation_id": transcript.conversation_id,
            "title": transcript.title,
            "assignment_file": str(assignments_path),
            "target_project_root": target_project_root,
        },
    }
    for block in transcript.blocks:  # attachments also surface per-message
        if block.attachments:
            import_meta["payload"]["attached_export_files"] = sorted(
                {name for b in transcript.blocks for name in b.attachments}
            )
            break
    session_meta = {
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": target_project_root},
    }
    records = [import_meta, session_meta]
    for block in transcript.blocks:
        if block.author in ("user", "Custom user info"):
            records.append(_user_record(block))
        elif block.author == "ChatGPT":
            records.append(_assistant_record(block))
        else:  # pragma: no cover - BOUNDARY_RE admits no other authors
            raise CompileError(
                source_markdown, block.start_line, f"unknown author {block.author}"
            )
    return records


def compile_one(
    row: dict[str, str],
    *,
    sessions_dir: Path,
    output_dir: Path,
    assignments_path: Path,
) -> dict[str, Any]:
    """Compile one assignment row and atomically write its Capture JSONL."""
    source = sessions_dir / Path(row["session_file"]).name
    if not source.is_file():
        raise CompileError(source, 0, "source Markdown not found")
    target_root = row["project_path"].strip()
    if not target_root.startswith("/"):
        raise CompileError(source, 0, f"project_path is not absolute: {target_root!r}")

    transcript = parse_session_markdown(source)
    records = build_records(
        transcript,
        source_markdown=source,
        assignments_path=assignments_path,
        target_project_root=target_root,
    )
    output_path = output_dir / f"{source.stem}.jsonl"
    temp_path = output_dir / f"{source.stem}.jsonl.tmp"
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    temp_path.replace(output_path)
    return {
        "capture": str(output_path),
        "conversation_id": transcript.conversation_id,
        "session_id": f"chatgpt-web-v1:{transcript.conversation_id}",
        "title": transcript.title,
        "source_markdown": str(source),
        "target_project_root": target_root,
        "recommended_project": row["recommended_project"],
        "imported_message_count": len(transcript.blocks),
        "attached_export_files": sorted(
            {name for b in transcript.blocks for name in b.attachments}
        ),
    }


def _read_owned_manifest(output_dir: Path) -> set[str] | None:
    """File names this tool's own manifest claims in the output directory.

    Returns None when there is no manifest or it is not one this tool wrote,
    so callers can distinguish 'nothing proven to own' from 'owns nothing'.
    Only a manifest with the matching format string is trusted to prove
    ownership of the capture files it lists.
    """
    manifest_path = output_dir / "capture_manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("format") != CAPTURE_FORMAT:
        return None
    captures = manifest.get("captures")
    if not isinstance(captures, list):
        return None
    names: set[str] = set()
    for entry in captures:
        if isinstance(entry, dict) and isinstance(entry.get("capture"), str):
            names.add(Path(entry["capture"]).name)
    return names


def prepare_output_directory(output_dir: Path) -> None:
    """Clear the output directory only for files proven to be this tool's own.

    Ownership is proven by a prior manifest written by this tool that lists
    the capture files by name.  The manifest itself is deletable only when it
    passed that format check; a foreign or corrupt manifest is treated as a
    foreign entry and the directory is refused.  Anything else present
    (stray *.jsonl files, subdirectories) aborts compilation before anything
    is deleted or written.
    """
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise CompileError(output_dir, 0, f"output path is not a real directory: {output_dir}")
    if not output_dir.exists():
        output_dir.mkdir(parents=True)
        return

    owned_captures = _read_owned_manifest(output_dir)
    if owned_captures is None:
        # No trusted manifest: the manifest file (if any) is itself foreign,
        # so only an empty directory is acceptable here.
        deletable: set[str] = set()
    else:
        deletable = owned_captures | {"capture_manifest.json"}
    foreign = sorted(
        entry.name
        for entry in output_dir.iterdir()
        if entry.name not in deletable
    )
    if foreign:
        raise CompileError(
            output_dir,
            0,
            f"output directory holds entries not owned by this tool's manifest, refusing to clean: {foreign[:5]}",
        )
    # Only the proven artifacts are removed; the directory itself is reused.
    for name in sorted(deletable & {e.name for e in output_dir.iterdir()}):
        (output_dir / name).unlink()


def compile_all(
    *,
    assignments_path: Path = DEFAULT_ASSIGNMENTS,
    sessions_dir: Path = DEFAULT_SESSIONS_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    min_confidence: str = DEFAULT_MIN_CONFIDENCE,
) -> list[dict[str, Any]]:
    """Compile every selected assignment; regenerate the output directory."""
    assignments_path = assignments_path.resolve()
    sessions_dir = sessions_dir.resolve()
    output_dir = output_dir.resolve()
    if min_confidence not in CONFIDENCE_ORDER:
        raise ValueError(f"min_confidence must be one of {CONFIDENCE_ORDER}")

    rows = select_assignments(assignments_path, min_confidence)
    prepare_output_directory(output_dir)

    entries: list[dict[str, Any]] = []
    failures: list[str] = []
    for row in rows:
        try:
            entries.append(
                compile_one(
                    row,
                    sessions_dir=sessions_dir,
                    output_dir=output_dir,
                    assignments_path=assignments_path,
                )
            )
        except CompileError as exc:
            failures.append(str(exc))
    if failures:
        # Leave nothing behind on any failure: rebuild-from-scratch semantics.
        prepare_output_directory(output_dir)
        raise CompileError(Path(failures[0]), 0, f"{len(failures)} failures: {failures}")

    manifest = {
        "format": CAPTURE_FORMAT,
        "assignment_file": str(assignments_path),
        "sessions_directory": str(sessions_dir),
        "min_confidence": min_confidence,
        "capture_count": len(entries),
        "total_imported_messages": sum(e["imported_message_count"] for e in entries),
        "captures": entries,
    }
    (output_dir / "capture_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignments", type=Path, default=DEFAULT_ASSIGNMENTS)
    parser.add_argument("--sessions-dir", type=Path, default=DEFAULT_SESSIONS_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--min-confidence",
        default=DEFAULT_MIN_CONFIDENCE,
        choices=CONFIDENCE_ORDER,
    )
    args = parser.parse_args(argv)
    try:
        entries = compile_all(
            assignments_path=args.assignments,
            sessions_dir=args.sessions_dir,
            output_dir=args.output_dir,
            min_confidence=args.min_confidence,
        )
    except (CompileError, OSError, csv.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    projects: dict[str, int] = {}
    for entry in entries:
        projects[entry["target_project_root"]] = (
            projects.get(entry["target_project_root"], 0) + 1
        )
    print(f"captures={len(entries)}")
    print(f"messages={sum(e['imported_message_count'] for e in entries)}")
    for project in sorted(projects):
        print(f"{projects[project]:>3}  {project}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
