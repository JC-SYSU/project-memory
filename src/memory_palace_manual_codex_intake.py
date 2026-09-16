"""Thin manual Codex JSONL intake CLI for one project-local database."""

from __future__ import annotations

import argparse
import os
import sys
import time

_SCRIPT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIRECTORY not in sys.path:
    sys.path.insert(0, _SCRIPT_DIRECTORY)

from memory_palace.manual_codex_intake import intake_codex_session_jsonl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_palace_manual_codex_intake")
    parser.add_argument("session_jsonl", metavar="SESSION_JSONL")
    args = parser.parse_args(argv)
    intake_codex_session_jsonl(args.session_jsonl, now_epoch=int(time.time()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
