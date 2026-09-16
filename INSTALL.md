[中文版](INSTALL.zh-CN.md) | English

# Installation Guide (for the agent performing the install)

This document is an operating spec: run the steps in order — every step lists an **expected output** and **failure handling**. When everything passes, run the "Acceptance checklist" at the end and report the results. **Do not skip steps** — the components are independent, and a missing piece shows up as "installed but silently not working" (e.g. hooks installed but env vars unset: enqueueing works, extraction always fails).

Let `$REPO` be the repository root. All commands below run from `$REPO`. Platforms: **macOS / Linux / Windows**; on Windows replace every `python3` below with `python` (or `py -3`); the Codex config directory is the same `%USERPROFILE%\.codex` (i.e. `~/.codex`). The only exception: section 5 (launchd) is macOS-only.

## 0. Prerequisites

| # | Check | Command | Expected | Failure handling |
| ---- | ---- | ---- | ---- | ---- |
| 0.1 | Python >= 3.11 | `python3 --version` | 3.11.x or newer | install it, then restart |
| 0.2 | Codex installed | `codex mcp list` | command exists (may report no servers) | install Codex first |
| 0.3 | User config dir | `ls ~/.codex/` | config.toml or hooks.json may already exist | the installer creates them if missing |

### 0.5 Probe the clients first — pick your install profile (three-way decision)

```bash
command -v codex   # is the Codex CLI on PATH?
command -v claude  # is the Claude Code CLI on PATH?
```

| Profile | Detected on this machine | Install what | Follow sections |
| ---- | ---- | ---- | ---- |
| **A** | Codex, no Claude Code | Palace core only: deps + Codex hooks/MCP registration + env vars | 1, 2, 3, 4; 5 optional; skip 3.5; accept only Codex items |
| **B** | both Codex and Claude Code | Core + full CC compatibility layer (`cc-integration/` scripts, `~/.claude/settings.json` hooks, `~/.claude.json` mcpServers registration) | 1–4 + 3.5 + 5 optional; accept Codex and CC items |
| **C** | Claude Code only | Core + full CC compatibility layer, repository deployed under the **CC home** `~/.claude/memory-palace/`, all paths pointing there | 1, 2, 4 + 3.5 (placeholders = that directory); no Codex registration at all; 5 optional |
| not installable | neither client | The system is triggered by Codex or Claude Code hooks — install at least one client first | — |

Profile C paths (the only real difference from A/B — scripts derive the palace `src/` relative to themselves, so any root works as long as the repo layout is intact):

| Piece | Profile C path |
| ---- | ---- |
| repo root | `~/.claude/memory-palace/` |
| hooks command | `{{PYTHON}} -B ~/.claude/memory-palace/cc-integration/cc_capture_hook_runner.py` |
| MCP command | `~/.claude/memory-palace/cc-integration/cc_memory_palace_recall_wrapper.zsh` |
| pointer dir | default `~/.claude/memory-palace-sessions` (override with `CC_MEMORY_MIRROR_ROOT`) |
| env injection target | CC-spawned drainer subprocesses (launchctl setenv / shell rc, same as section 4) |
| launchd (optional) | `WorkingDirectory` = `~/.claude/memory-palace`, otherwise same template |

Shared across all profiles: env-var injection rules (section 4), data layout (section 6), uninstall (section 7 — profile C is done once you delete the repo directory).

## 1. Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

Expected: `mcp` and `pydantic` installed.
Failure: check network/pip source; if the target machine uses a virtualenv, activate it first and keep using the same interpreter for every `python3` below.

## 2. Verify the core package imports

```bash
PYTHONPATH=$REPO/src python3 -c "import memory_palace.worker_orchestrator, memory_palace.extraction_core, memory_palace.segmenter"
```

Expected: no output, no error.
Failure: missing deps → back to section 1; syntax error → broken clone, re-clone from source.

## 3. Register Codex integration (hooks + MCP)

One-shot installer, preview by default, `--apply` writes:

```bash
python3 scripts/install.py            # dry-run: prints every action it would take
python3 scripts/install.py --apply    # actually write
```

What the installer does (equivalent to the manual tables below):

- Merges two events into `~/.codex/hooks.json`: `PreCompact` (enqueue before compaction) and `UserPromptSubmit` (inject a recall-routing prompt on every question). An existing identical command is skipped; other hooks are preserved.
- Appends the `[mcp_servers.memory_palace_recall]` stanza to `~/.codex/config.toml` (skips if present).
- Backs up every target as `*.bak-<UTC timestamp>` before writing; re-parses after writing (JSON/TOML); on parse failure restores the backup and aborts.

Expected (`--apply`): lines like `hooks[...]: registered ...` and `config.toml: ... appended ...`.
Failure: installer aborts without writing on any failure; fix per the error (usually permissions or paths).

After writing hooks, Codex asks to re-trust them (`/hooks`); verify with `codex mcp list`.

### Manual equivalents (if you don't trust the installer)

**hooks** — merge into `~/.codex/hooks.json` (replace `{{PYTHON}}` with the interpreter from 0.1, `{{INSTALL_DIR}}` with `$REPO`):

```json
{
  "hooks": {
    "PreCompact": [
      { "matcher": "^(manual|auto)$", "hooks": [
        { "type": "command", "command": "{{PYTHON}} -B {{INSTALL_DIR}}/src/codex_precompact_hook_runner.py", "timeout": 30 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [
        { "type": "command", "command": "{{PYTHON}} -B {{INSTALL_DIR}}/src/codex_recall_prompt_hook_runner.py", "timeout": 5 } ] }
    ]
  }
}
```

**MCP** — append to `~/.codex/config.toml`:

```toml
[mcp_servers.memory_palace_recall]
command = "{{PYTHON}}"
args = ["-B", "{{INSTALL_DIR}}/src/memory_palace_recall_mcp_server.py"]
startup_timeout_sec = 10.0
tool_timeout_sec = 30.0
```

### 3.5 Claude Code integration (mandatory for profiles B/C; skip for A)

Claude Code talks to the same palace — same queue, same drainer — in both directions, with zero changes to the palace source. Two pieces:

1. **Write path**: `PreCompact` / `SessionEnd` hook events compile CC sessions into Codex-shaped captures and enqueue them (`cc-integration/cc_capture_hook_runner.py`, fail-open, silent exit 0);
2. **Read path**: the MCP server is exposed to CC through a wrapper (`cc-integration/cc_memory_palace_recall_wrapper.zsh` → `src/memory_palace_recall_mcp_server.py`). The wrapper is required: the server binds the project database to its cwd at startup, and CC only promises `CLAUDE_PROJECT_DIR` — not the spawn cwd — so the wrapper explicitly cd's.

Configuration: `templates/claude.example.json` (annotated): merge the hooks stanza into the `"hooks"` key of `~/.claude/settings.json`, and the mcpServers stanza into the `"mcpServers"` key of `~/.claude.json`. Placeholders are the same as section 3 (`{{PYTHON}}` / `{{INSTALL_DIR}}`). Pointer files default to `~/.claude/memory-palace-sessions`; override with `CC_MEMORY_MIRROR_ROOT`.

Acceptance: in a new CC session, asking a historical question about the current project should make the model call `memory_recall`; a pointer file `~/.claude/memory-palace-sessions/<session-id>.jsonl` (single session_meta line) appearing per session confirms the write path. Historical batch backfill: `cc-integration/cc_backfill_driver.py` (scans `~/.claude/projects/*/*.jsonl`, safe to re-run). CC-side tests: `PYTHONPATH=../src python3 -m pytest cc-integration/tests/`.

Env vars: no new ones — the same `PROJECT_MEMORY_*` four-tuple is inherited by CC-spawned drainer subprocesses (section 4).

## 4. Configure environment variables (mandatory — the most commonly missed step)

**Recommended: the friendly wizard** `python3 scripts/setup_env.py` — you only fill in the URL and the API key:

```bash
python3 scripts/setup_env.py
```

- interactive prompts: `BASE_URL` and `API_KEY` required (key input is masked); `MODEL` is auto-detected from the endpoint's `/models` list for you to pick (manual input only if detection fails); `REASONING_EFFORT` defaults to `high`, press Enter to accept;
- writes to `~/.zshenv` (override with `--rc-file`; existing lines are never duplicated); on macOS it also prints the `launchctl setenv` commands for App-launched processes;
- then **automatically runs connectivity and usability tests** and reports each one:
  1. reachability + auth (per-status fixes: 401/404/network);
  2. Chat Completions protocol;
  3. **forced tool calling** accepted? (if not, the endpoint cannot work — switch endpoints);
  4. `reasoning_effort` tolerated.
  
  Every failed probe prints a concrete fix; re-run after adjusting (standalone: `python3 scripts/test_connection.py`).

Non-interactive (agent/automation):

```bash
python3 scripts/setup_env.py --set BASE_URL=... --set API_KEY=... \
    [--set MODEL=...] [--set EFFORT=high] [--rc-file <path>]
```

If MODEL is unset it probes and picks the first model. `--skip-write` collects and tests without writing.

### Manual fallback

The four variables are read from the process environment, **all required, no defaults** (`RuntimeConfig.from_env` in `extraction_core.py`); any missing key makes extraction fail immediately. The variables must be present in the process that actually runs the worker — the worker is spawned as a child of the hook and inherits the Codex launch environment; with launchd it's the launchd environment.

| Variable | Required | What to put | Example |
| ---- | ---- | ---- | ---- |
| `PROJECT_MEMORY_API_KEY` | yes | model service key | `sk-...` |
| `PROJECT_MEMORY_BASE_URL` | yes | chat/completions API **root**, without the endpoint path itself | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `PROJECT_MEMORY_MODEL` | yes | model id | `glm-5.3-flash` |
| `PROJECT_MEMORY_REASONING_EFFORT` | yes | effort level `low`/`medium`/`high`/`max`, `high` recommended | `high` |

`BASE_URL` check: this system speaks the chat/completions protocol. Fill in the API root (requests go to `{BASE_URL}/chat/completions`). **Wrong ways**: a full endpoint path (e.g. `/v1/chat/completions`) — the path is appended again, 404; `/responses` or `/messages` protocol endpoints — not supported; HTTP instead of HTTPS — never.

Injection targets (by how Codex/worker is started; usually more than one; **profile C only needs the CC spawn chain**):

1. Codex started from the terminal: `export` into the terminal; persist in `~/.zshenv` (bash: also `~/.bashrc`).
2. Codex started from a macOS app: `launchctl setenv PROJECT_MEMORY_API_KEY sk-...` (restart the app; check with `launchctl getenv`).
3. launchd-resident worker: same `launchctl setenv`, or the `EnvironmentVariables` key in the plist.

Verify in the current process environment:

```bash
python3 -c "import os; [print(f'{k} =', 'OK' if os.environ.get(k) else 'MISSING') for k in ('PROJECT_MEMORY_API_KEY','PROJECT_MEMORY_BASE_URL','PROJECT_MEMORY_MODEL','PROJECT_MEMORY_REASONING_EFFORT')]"
```

Expected: four `OK`s. Any `MISSING` — do not proceed. The installer's dry-run and `--apply` both check the same four in the invoking environment and warn (non-blocking — fine if you only install hooks, but acceptance item 2 will fail).

### Endpoint and model requirements

- Protocol: HTTPS + `Authorization: Bearer`; request bodies are OpenAI Chat Completions format (`model`, `messages`, `reasoning_effort`, `max_tokens` capped at 32768, `tools`, `tool_choice`). Transport is stdlib `urllib`; no extra HTTP dependency.
- **The endpoint must support forced tool calling** (`tool_choice` naming a function; response JSON comes from `tool_calls[0].function.arguments`). Endpoints without forced `tool_choice`, or gateways that coerce output via grammar/structured-output constraints, cannot work.
- Suggested models: `glm-5.3-flash` (production-proven; 6/6 tool calls valid) and `deepseek-v4-flash-0731` (backup); other models of the same tier are usable after one real forced-tool-call test.
- Use effort `high`; `max` burns quota on reasoning in some gateways and truncates the body — on corrupted/empty responses, drop the effort level, don't raise quota.

## 5. (Optional) launchd-resident worker (macOS only)

Without it everything still works (each hook triggers a transient drainer). To keep a poller resident:

```bash
sed -e "s|{{PYTHON}}|$(command -v python3)|" \
    -e "s|{{INSTALL_DIR}}|$REPO|" \
    -e "s|{{SESSIONS_ROOT}}|$HOME/.codex/sessions|" \
    templates/launchd.example.plist > ~/Library/LaunchAgents/com.yourname.project-memory-worker.plist
launchctl load -w ~/Library/LaunchAgents/com.yourname.project-memory-worker.plist
```

(Change the Label to your own.) Unload: `launchctl unload -w <same path>`, then delete the file. Template semantics: `RunAtLoad` starts it immediately, `KeepAlive` relaunches on exit, `ThrottleInterval=10` seconds between crash restarts; the worker is single-job (`run_once()` claims one due job per invocation).

## 6. Data layout (what acceptance verifies against)

- Transcripts (authoritative evidence): Codex session JSONL under `~/.codex/sessions`; the system only reads.
- Project state (derived layer): each enabled project's `.memory-palace/`, created by `ensure_project_state` on first enqueue:
  - `memory-palace.sqlite3` — one database, eight tables: `records`, `evidence`, `review_items`, `session_cursor`, `model_usage`, `audit_events`, `window_commit_markers`, `extraction_jobs`;
  - `captures/` — frozen copies of captured source.
- Memory is rebuildable: deleting `.memory-palace/` only drops the retrieval layer; transcripts are untouched.

## 6.5 First run: scan and batch-enqueue historical sessions (recommended)

Existing Codex/Claude Code sessions **do not** produce memories automatically — enqueueing happens only for future hook events. On first use, scan what's there, review the plan, then enqueue. Fixed three-step flow: **scan (read-only) → you confirm → execute**.

**Profile A/B (Codex history)** — `scripts/backfill_codex.py`:

```bash
# 1) scan and statistics (read-only, writes nothing):
python3 scripts/backfill_codex.py
#    prints: enqueueable count/size, per-project counts, skipped reasons, batch plan
# 2) after you confirm the plan:
python3 scripts/backfill_codex.py --apply [--batch-size 50]
```

- Enqueue criteria: has a `session_meta` record and cwd points to an existing project directory → enqueueable; no meta / deleted dir → reported and skipped, never enqueued.
- Idempotent: every session goes through the same admission gate as the PreCompact hook (`process_payload`); re-runs report `duplicate` and skip. Safe to interrupt and re-run.
- Batching: grouped by project, largest files first; per-session drainer wakes are suppressed within a batch, one wake at the end, so the drainer drains serially.
- Results go to `backfill-codex-manifest.csv` (repo root, gitignored); the final line counts `accepted=`new / `duplicate=`exists / `ignored=`rejected / `error=`exception.

**Profile B/C (Claude Code history)** — `cc-integration/cc_backfill_driver.py` (same three-step flow):

```bash
python3 cc-integration/cc_backfill_driver.py --dry-run   # 1) read-only preview
python3 cc-integration/cc_backfill_driver.py             # 2) run after confirmation
```

- Scans `~/.claude/projects/*/*.jsonl` (`--exclude-cwd` available); reuses the CC hook's admission gate and idempotency; safe to re-run.
- Batching: enqueue the batch with per-session wakes suppressed, one wake at the end; manifest enables resuming.

Profile B can run both. Extraction itself is executed serially by the drainer and really calls the model (quota consumption) — run large batches off-peak.

## Acceptance checklist (all items must pass)

Profile gating per 0.5: **profile A — Codex items only (1–5); profiles B/C — the Codex items (if any) *plus* all CC items from 3.5** (ask a historical question in a new CC session, a pointer file appears under `~/.claude/memory-palace-sessions/`, `PYTHONPATH=../src python3 -m pytest cc-integration/tests/` green).

| # | Verify | Command | Pass criteria |
| ---- | ---- | ---- | ---- |
| 1 | MCP registered | `codex mcp list` | `memory_palace_recall` present, status connected |
| 2 | env vars | the check command from section 4 | all four `OK` |
| 3 | UserPromptSubmit injection | new Codex session, trust hooks via `/hooks`, send one ordinary message | no error in the session (runner is silently successful) |
| 4 | PreCompact enqueue | minimal transcript + valid payload (command below) | exit 0, `.memory-palace/` contains sqlite + `captures/` |
| 5 | extraction chain (optional, costs quota) | wait for the drainer after item 4 | an `extraction_jobs` row in a non-`failed` terminal state |

Item 4 command:

```bash
mkdir -p /tmp/pm-test-project
printf '{"type":"session_meta","payload":{"id":"test-session-1","cwd":"/tmp/pm-test-project"}}\n' > /tmp/pm-test-session.jsonl
echo '{"session_id":"test","cwd":"/tmp/pm-test-project","hook_event_name":"PreCompact","model":"test","turn_id":"1","trigger":"auto","transcript_path":"/tmp/pm-test-session.jsonl"}' \
  | python3 $REPO/src/codex_precompact_hook_runner.py; echo "exit=$?"
ls /tmp/pm-test-project/.memory-palace/
```

The runner is fail-open: **success produces no output; exit 0 is success**. `transcript_path` of `null` takes the ignored branch (same silent exit 0, no database) — a way to check the runner is alive. Item 4's accepted branch triggers one real extraction attempt (model call). Clean up afterwards: `rm -rf /tmp/pm-test-project /tmp/pm-test-session.jsonl`.

## 7. Uninstall

1. Delete the `PreCompact` (matcher `^(manual|auto)$`) and `UserPromptSubmit` entries pointing at this repository from `~/.codex/hooks.json`;
2. Delete the `[mcp_servers.memory_palace_recall]` stanza from `~/.codex/config.toml`;
3. If launchd-resident: `launchctl unload -w <plist>` and delete the plist;
4. Delete the repository — project `.memory-palace/` directories and transcripts live outside it.

## 8. Troubleshooting

| Symptom | Cause & fix |
| ---- | ---- |
| hooks don't trigger | hooks not trusted: `/hooks` in the session |
| `codex mcp list` shows memory_palace_recall error | `command` interpreter differs from Codex's runtime (unactivated venv, etc.); hand-edit config.toml; raise `startup_timeout_sec` from 10s if needed |
| MCP tools listed but calls time out | `tool_timeout_sec` default 30s; raise for huge recalls |
| enqueue works, extraction always fails | the classic: env vars set only in the terminal, not in the Codex/launchd process env. Check every injection rail in section 4 |
| `pip install` fails | pip source unreachable; `mcp`/`pydantic` must be installable from your source |
| installer parse failure | it auto-restored the backup; compare with the `.bak-*` to confirm |
| HTTP 404 on requests | `BASE_URL` filled with a full endpoint path (appended twice); see section 4 fill-in rules |
| empty body / corrupted JSON | effort level too high burning quota: drop to `high`/`medium` and retry |