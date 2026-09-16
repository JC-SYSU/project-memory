[中文版](README.zh-CN.md) | English

# Project Memory

AI assistants start every session from zero. Last month's decisions, the pitfalls you already hit, the limits you set — you explain them again every time. Project Memory archives that history next to your project and lets the model read it back when the context needs it.

## What it does

Nothing to operate once installed. In the background:

1. **Capture.** When a Codex session is about to be compacted, a hook writes "history to archive" into a queue and returns immediately. Your session is not blocked.
2. **Extract.** A worker splits the transcript into bounded pages and asks a model to keep what remains useful across sessions — decisions made, bugs fixed, why things are done this way. Every entry anchors to the exact evidence in the original conversation and lands in the project's own `.memory-palace/`.
3. **Recall.** In a new session, when the question depends on past facts, the model calls `memory_recall` itself and keeps working instead of asking "haven't we done this before?"

That is the whole loop: history archived automatically, model actually reading it.

Runs on macOS, Linux, and Windows — Codex hooks and the MCP mechanism behave the same on all three; only the launchd resident from INSTALL.md section 5 is macOS-only.

## Bring your own model, keep your quota

Both the write path and the read path go through the model endpoint you configure — never through Codex's or Claude Code's subscription quota. Extraction is a modest job: follow instructions, answer in Chinese, support forced tool calls. A pay-per-token flash-tier model keeps the per-session cost small. The four environment variables are documented in INSTALL.md; `scripts/setup_env.py` fills them in and runs connectivity tests.

## One memory across both agents

Codex and Claude Code read and write the same per-project store: one queue, one database, one worker, one recall tool. A decision captured in a Codex session comes back up in Claude Code, and the other way around. There is no second memory to maintain. Switching clients mid-project resets nothing — the memory follows the project, not the tool. (Configuration: INSTALL.md, "Claude Code integration".)

## Deliberately simple

Per project, the whole system is one SQLite database (eight tables: `records`, `evidence`, `review_items`, `session_cursor`, `model_usage`, `audit_events`, `window_commit_markers`, `extraction_jobs`), one queue, one worker, one MCP tool. No services, no distributed state, nothing to scale. Transcripts are the source of truth; memories are a derived layer you can wipe and rebuild. When the job is remembering, small is a feature: everything there is to inspect fits in one database, and every claim points back at a line in the transcript.

## Who it's for

This suits people who keep returning to the same project over many sessions, and anyone tired of restating the context each time. It also helps if you want claims to carry receipts: every memory anchors to the original conversation and can be traced, deleted, and rebuilt.

## How to use

**Install** — copy the block below (the copy button on the right) and paste it into your agent (Codex, Claude Code, or any coding agent). The agent reads the INSTALL guide and does the work:

```
You are installing the Project Memory system from this repository. Do the following:

1. Clone this repository to a sensible location and cd into it.
2. Read INSTALL.md in full (the Chinese version is INSTALL.zh-CN.md if preferred) and execute it step by step: probe the machine and pick the install profile (section 0.5), install dependencies, verify imports, register the Codex integration and — if Claude Code is present — the CC compatibility layer (section 3.5), configure the model-service environment variables via scripts/setup_env.py (only URL and API key are asked) and run its connectivity tests.
3. Before any step that writes outside this repository (hooks.json, config.toml, settings.json, .claude.json, shell rc files, launchd) or consumes model quota (connectivity tests, historical-session backfill from section 6.5), show me what you are about to do and wait for my confirmation — including the survey output before backfilling.
4. When everything is done, run the acceptance checklist (section "Acceptance checklist") and report each item's result to me. Anything that fails: fix it per the troubleshooting table (section 8) before reporting back.
```

If you prefer to install by hand, follow `INSTALL.md` directly.

**Day-to-day**: nothing to run, nothing to check.

**To look something up**: just ask — "how did I handle X in this project before?" — the model queries memory itself.

## What it doesn't do

It distills facts that stay useful across sessions; it does not summarize your current task or pass judgment on your conversation. It uploads nothing: transcripts are read locally, the database stays in the project directory, and the only external connection is the model endpoint you configured. Entries without sufficient evidence never become official records — they land in a review queue and wait for a human. Memories reference source locations (evidence IDs and line numbers) instead of copying the text.

## Where the data lives, how to clean up

Transcripts (the authoritative source) sit in Codex's session directory; the system only reads them. The memory store lives in each project's `.memory-palace/` — one SQLite database plus frozen captures. To wipe a project's memory, delete its `.memory-palace/`; the transcripts are unaffected.

## Quick verification

```bash
codex mcp list   # memory_palace_recall should be connected
```

An end-to-end 3-minute check (includes one model call) is in INSTALL.md, "Acceptance checklist".

## License

MIT. Structure and pipeline details are in INSTALL.md and the docstrings under `src/memory_palace/`.