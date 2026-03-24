# Extraction Pipeline Reference

This document covers the full extraction pipeline: how extraction is triggered in each
adapter, how signals are written and processed by the daemon, the step-by-step journey
from raw transcript to stored memories, and the public API surface.

---

## Table of Contents

1. [Extraction Triggers](#1-extraction-triggers)
2. [Signal File Protocol](#2-signal-file-protocol)
3. [Rolling Extraction Mode](#3-rolling-extraction-mode)
4. [The Extraction Pipeline Step by Step](#4-the-extraction-pipeline-step-by-step)
5. [Extraction Priority Invariant](#5-extraction-priority-invariant)
6. [Hook Entry Points](#6-hook-entry-points)
7. [Daemon Lifecycle](#7-daemon-lifecycle)
8. [Public API vs CLI](#8-public-api-vs-cli)
9. [Key Constants and Configuration](#9-key-constants-and-configuration)

---

## 1. Extraction Triggers

Extraction is initiated differently by each adapter. All paths converge on the daemon's
signal-processing loop or, for the synchronous path, on `extract_from_transcript()` directly.

### OpenClaw (OC)

```
User runs /compact
  → OC fires before_compaction hook
  → hook calls: quaid hook-extract --precompact
  → hook_extract() writes a "compaction" signal file
  → daemon picks up signal, runs extraction, clears carryover
```

OC supports compaction control (`supports_compaction_control=True`), meaning it can
delay the actual compaction until extraction completes. The signal written has
`"supports_compaction_control": true`.

### Claude Code (CC)

CC uses two asynchronous paths:

**Path 1 — PreCompact hook:**
```
Claude Code fires PreCompact hook before summarizing context
  → CC hook calls: quaid hook-extract --precompact
  → hook_extract() writes a "compaction" signal file
  → daemon processes asynchronously (CC cannot wait for extraction)
```

**Path 2 — Stop hook (session end):**
```
Claude Code fires Stop hook (session ends normally)
  → CC hook calls: quaid hook-extract  (no --precompact)
  → hook_extract() writes a "session_end" signal file
  → daemon processes asynchronously
```

CC does not support compaction control (`supports_compaction_control=False`). Extraction
is always deferred — CC fires and forgets.

### Idle timeout extraction (daemon-initiated)

The daemon's main loop runs a periodic idle check (`check_idle_sessions()`). Any session
whose transcript has grown past its cursor and has not been modified for
`capture.inactivity_timeout_minutes` (default: 30) gets a synthetic `"timeout"` signal
written. This catches sessions that end without firing a Stop hook (e.g. process kill).

### Orphan sweep (session-init-initiated)

When a new session starts, `hook_session_init()` calls `sweep_orphaned_sessions()`.
This scans all known session cursors, finds sessions with un-extracted content, and
writes a `"session_end"` signal for each. This catches content from previous sessions
that was never extracted (e.g. the daemon was stopped before processing).

### Synchronous path (CLI / manual)

```python
from core.ingest_runtime import run_extract_from_transcript

result = run_extract_from_transcript(
    transcript="User: ...\nAssistant: ...",
    owner_id="quaid",
    label="cli",
    dry_run=False,
)
```

This calls `ingest.extract.extract_from_transcript()` directly, bypassing the daemon and
signal protocol entirely. Used by `quaid hook-extract` in the legacy direct mode and by
CLI invocations of `ingest/extract.py`.

---

## 2. Signal File Protocol

### Signal directory

Signals are written at the `QUAID_HOME` level (not per-instance), so any running daemon
can pick them up regardless of which instance is active:

```
QUAID_HOME/data/extraction-signals/
```

The daemon polls this directory every 5 seconds (default `poll_interval`).

### Signal file naming

```
<timestamp_ms>_<pid>_<uuid8>_<signal_type>.json
```

Example: `1710001234567_12345_a1b2c3d4_compaction.json`

Files are sorted lexicographically (timestamp prefix ensures chronological order).
Up to 100 signals are processed per poll cycle (`MAX_SIGNALS_PER_POLL = 100`).

### Signal file fields

```json
{
  "type": "compaction",
  "session_id": "abc123def456...",
  "transcript_path": "/Users/user/.claude/projects/-Users.../abc123.jsonl",
  "adapter": "claude-code",
  "supports_compaction_control": false,
  "timestamp": "2026-03-13T10:00:00Z",
  "meta": {}
}
```

| Field | Description |
|-------|-------------|
| `type` | Signal type: `compaction`, `reset`, `session_end`, or `timeout` |
| `session_id` | Session identifier (alphanumeric + `_-`, max 128 chars) |
| `transcript_path` | Absolute path to the adapter's `.jsonl` session file |
| `adapter` | Adapter name: `"claude-code"`, `"openclaw"`, `"unknown"` |
| `supports_compaction_control` | Whether OC can delay compaction until extraction finishes |
| `timestamp` | ISO-8601 UTC timestamp of when the signal was written |
| `meta` | Reserved for future use, currently `{}` |

### Valid signal types

| Type | Trigger | Carryover behavior |
|------|---------|-------------------|
| `compaction` | Context about to be compacted | Cleared after extraction |
| `reset` | Session reset (`/new`, `/reset`) | Cleared after extraction |
| `session_end` | Session ended cleanly | Persisted for next extraction |
| `timeout` | Session idle beyond threshold | Persisted for next extraction |

Invalid signal types default to `session_end` with a warning log.

### Signal lifecycle

1. Adapter writes signal file atomically (temp file + `os.replace()`).
2. Daemon reads and processes the signal.
3. On success: signal file is deleted (`mark_signal_processed()`).
4. On failure: signal file is preserved for retry (daemon logs the error and continues).
5. Corrupt signal files (JSON parse errors) are deleted immediately.

---

## 3. Rolling Extraction Mode

Rolling extraction is the daemon's staged-ingest path for transcripts that are too
large or too expensive to wait on until the final compaction/reset boundary.

It is used in two places:

- Runtime daemon operation, via `check_chunk_ready_sessions(max_tokens)`, which
  stages extraction once a transcript grows past the configured chunk budget.
- Benchmark harness stress lanes, especially `rolling-obd` and the imported-Claude
  replay scripts, which intentionally drive the real daemon signal path in stage
  batches and then perform one final flush.

### Stage vs flush

Rolling extraction has two phases:

1. **Stage**
   - The daemon reads only the next chunk-budget-sized transcript window.
   - It parses and extracts facts/snippets/journal/project logs into staged state.
   - It advances the session cursor immediately, so the same lines are not re-read.
   - It persists carryover context for the next rolling stage.

2. **Flush**
   - Triggered by a terminal signal such as `compaction`, `reset`, `session_end`,
     `timeout`, or an explicit benchmark final-flush signal.
   - The daemon takes the accumulated staged payload and calls
     `apply_extracted_payloads()`.
   - This is the point where facts are actually published to the DB and dedup/store
     telemetry is finalized.

### Rolling state and telemetry files

Per-session staged state lives at:

```
QUAID_HOME/<instance>/data/rolling-extraction/<session_id>.json
```

Rolling daemon telemetry is appended to:

```
QUAID_HOME/<instance>/logs/daemon/rolling-extraction.jsonl
```

Important event types:

- `rolling_stage` — one staged chunk was extracted and added to rolling state
- `rolling_flush` — staged payload was published
- `rolling_flush_error` — final publish path failed and the signal was preserved

Important telemetry families on `rolling_flush`:

- extract timings: stage wall, flush wall, signal-to-publish
- carryover counts: `carry_facts_*`, duplicate carry drops
- publish counts: facts stored/skipped, snippets/journal/project logs
- dedup metrics: hash hits, scanned rows, gray-zone rows, LLM checks
- rolling candidate metrics: vec query count, vec candidates returned, vec
  candidate limit, vec limit hits
- rolling fallback metrics: FTS query count, FTS candidates returned, FTS
  candidate limit hits, fallback scan count, token-prefilter counts
- embedding cache metrics

### Failure and resume semantics

- Stage-state files persist until flush succeeds.
- If flush fails, the terminal signal is intentionally left on disk for retry.
- Benchmark harness rolling drivers also maintain a pre-publish checkpoint so a
  benchmark rerun can resume from "stage already done, flush still pending"
  instead of re-extracting the whole transcript.

Rolling extraction is still fail-hard on publish errors. It is a staging strategy,
not a degraded mode.

---

## 4. The Extraction Pipeline Step by Step

### Overview

```
Signal file
  → daemon reads signal
  → cursor check (find new content since last extraction)
  → transcript slice written to temp file
  → adapter parses JSONL → normalized transcript text
  → subagent transcripts merged (if any)
  → carryover context loaded
  → extract_from_transcript() called
      → capture skip patterns applied
      → transcript chunked (~30k chars / chunk, max 10 chunks)
      → per chunk: build carry context → call Opus → parse JSON response
      → facts stored (dedup check at store time)
      → edges stored
      → soul snippets written
      → journal entries written
      → project logs emitted
  → cursor advanced
  → carryover updated or cleared
  → post-extraction: sync_all_projects(), snapshot_all_projects()
  → notification sent (Telegram / pending file)
```

### Step 1: Locate transcript

The daemon reads `transcript_path` from the signal file. If the path has changed since
the last cursor write (file rotation), the cursor is reset to 0.

### Step 2: Cursor check

Each session has a cursor file at:
```
QUAID_HOME/<instance>/data/session-cursors/<session_id>.json
```

The cursor records the last processed line offset. Only lines after the cursor are
extracted. If the cursor is ahead of the file length (file truncation/rotation), it
resets to 0.

```json
{
  "session_id": "abc123",
  "line_offset": 847,
  "transcript_path": "/path/to/abc123.jsonl",
  "updated_at": "2026-03-13T10:00:00Z"
}
```

### Step 3: Transcript slice

New lines (from cursor to EOF) are written to a per-instance temp file:
```
QUAID_HOME/<instance>/data/tmp/<random>.jsonl
```

Maximum 50,000 lines are read per extraction (`MAX_TRANSCRIPT_LINES`). If capped, a
warning is logged. On `compaction` or `reset` signals, if lines remain above the cap
and the transcript may be wiped before the daemon cycles again, a follow-up
`session_end` signal is written immediately so the remaining content is extracted on
the next daemon cycle rather than lost. The follow-up signal carries
`meta.reason = "cap_followup"` and `meta.cap_offset` recording where extraction stopped.

### Step 4: JSONL parsing

The adapter's `parse_session_jsonl()` method converts the raw JSONL slice into a
normalized transcript:

```
User: <text>
Assistant: <text>
User: <text>
...
```

For Claude Code, `parse_session_jsonl()` skips:
- `file-history-snapshot`, `progress`, `system`, `result`, `summary` record types
- `tool_use`, `tool_result`, `thinking` content blocks
- `<system-reminder>` tagged content
- `[quaid]` and `[notify]` prefixed messages

### Step 5: Subagent transcript merge

If the session has registered subagents with harvestable transcripts
(registered via `hook_subagent_stop`), their parsed transcript text is appended:

```
<parent transcript text>

--- Subagent (task) ---
<child transcript text>
```

Per-child advisory size: 50,000 chars. If a child transcript exceeds this, a warning
is logged but the text is still included — the extraction chunker handles splitting
large inputs.

Total merged cap: 200,000 chars. If the total merged size would exceed
`MAX_MERGED_CHARS`, remaining subagents are **deferred rather than dropped**: a
follow-up `session_end` signal is written for the parent session so the deferred
subagents are harvested on the next daemon cycle. The follow-up signal carries
`meta.reason = "deferred_subagents"` and `meta.deferred_count`. The parent session's
own content is always included in full; only overflow subagents are deferred.

### Step 6: Chunking (waterfall batching)

`extract_from_transcript()` chunks the transcript at turn boundaries (`\n\n`) using
`lib.batch_utils.chunk_text_by_tokens()`:

- Default chunk size: `capture.chunk_tokens` tokens (default 8,000).
- Legacy `capture.chunk_size` / `chunkSize` is still accepted for old configs, but it
  now mirrors the same token budget instead of using a char-to-token heuristic.
- Maximum chunks per extraction: 10. Transcripts exceeding this are capped (the oldest
  content in the window is what gets cut — the cursor already excluded all previously
  extracted content).
- Each chunk is sent to Opus independently with carryover context from prior chunks.

**Truncation is banned.** Chunking is waterfall batching: chunk N's distilled facts
feed chunk N+1 as carryover context. No content is silently discarded.

### Step 7: Per-chunk LLM call

For each chunk, the extraction prompt is built:

```python
# If carryover facts exist from earlier chunks or earlier extractions:
user_message = (
    "Use this context from earlier conversation chunks for continuity. ...\n\n"
    "=== EARLIER CHUNK CONTEXT ===\n<carry_context>\n=== END CONTEXT ===\n\n"
    "Extract memorable facts and journal entries from this conversation chunk:\n\n<chunk>"
)
```

The carry context includes up to 40 high-confidence facts, formatted as:
```
- [category | source | confidence] fact text | edges: subject --rel--> object
```

Opus is called via `call_deep_reasoning()` with:
- `max_tokens=6144` per chunk
- `timeout=min(600.0, remaining_budget)`
- Total extraction wall-clock budget: 600 seconds (`MAX_EXTRACT_WALL_SECONDS`)

If Opus returns non-JSON prose, a one-pass repair is attempted via `call_fast_reasoning()`
before the chunk is skipped.

### Step 8: Parsing Opus output

The expected JSON response shape:

```json
{
  "facts": [
    {
      "text": "The owner prefers dark mode in all editors",
      "category": "preference",
      "domains": ["personal", "technical"],
      "extraction_confidence": "high",
      "keywords": "dark mode editor preference",
      "privacy": "shared",
      "confidence_reason": "User stated directly",
      "source": "user",
      "edges": [
        {"subject": "Owner", "relation": "prefers", "object": "dark mode"}
      ]
    }
  ],
  "soul_snippets": {
    "SOUL.md": ["Owner is methodical about their dev environment"],
    "USER.md": ["Prefers dark mode in all editors"]
  },
  "journal_entries": {
    "SOUL.md": "A session about editor configuration and tooling preferences..."
  },
  "project_logs": {
    "quaid": ["Updated extraction pipeline docs"]
  }
}
```

Facts are validated:
- Must be a dict with a string `"text"` field
- Text must be at least 3 words
- `domains` array must be present, non-empty, and contain only registered domain IDs
- Facts missing domains or with unrecognized domain IDs are skipped with a warning

### Step 9: Storing facts (dedup check)

Each valid fact is stored via `memory_service.store()`, which performs automatic
deduplication using vector similarity. The return status is one of:

| Status | Meaning |
|--------|---------|
| `created` | New memory stored; `facts_stored += 1` |
| `duplicate` | Near-identical memory exists; `facts_skipped += 1` |
| `updated` | Existing memory updated with new details; `facts_stored += 1` |
| `blocked` | Circuit breaker tripped; extraction aborts |

### Step 10: Storing edges

For each successfully stored fact, edges from `fact["edges"]` are created via
`memory_service.create_edge()`. Subject and object entities are looked up by name
(case-insensitive, fuzzy match). Missing entities are auto-created as Person nodes.

### Step 11: Soul snippets and journal

After all chunks are processed, snippets and journal entries collected across all chunks
are written:

- **Soul snippets** (`write_snippets=True`): Written to `data/soul-snippets/`.
  The nightly janitor FOLD/REWRITE/DISCARDs them into core files (SOUL.md, USER.md, ENVIRONMENT.md).
- **Journal entries** (`write_journal=True`): Written to `journal/*.journal.md`.
  Opus distills themes from these into core markdown. Old entries are archived monthly.

### Step 12: Project logs

`project_logs` from Opus output are appended to `projects/<name>/PROJECT.log` via
`core.docs.updater.append_project_logs()`. Project events are also emitted to
`projects/staging/` as JSON for the doc updater.

### Step 13: Cursor advance and carryover

The cursor is only advanced when all chunks are fully processed
(`chunks_processed == chunks_total`). If extraction is incomplete, the cursor stays
at its old position and the signal is preserved for retry.

On success:
- Cursor is advanced to `cursor_offset + len(new_lines)`.
- Carryover: cleared on `compaction`/`reset` signals (logical boundary); persisted on
  `session_end`/`timeout` signals.

### Step 14: Post-extraction hooks

After a successful extraction, the daemon runs:

1. `sync_all_projects()` — copies project context files to adapter workspaces.
2. `snapshot_all_projects()` — takes a shadow git snapshot for projects with `source_root`.
3. `update_project_docs()` — updates stale doc chunks from shadow git diffs.

---

## 5. Extraction Priority Invariant

The extraction prompt enforces a strict ordering of what gets extracted:

1. **User facts** — first priority. Personal information, preferences, relationships,
   life events. These are the core value of the memory system and must never be crowded
   out by technical content.
2. **Agent-action memories** — second priority. What the agent did, what was decided.
3. **Technical/project-state facts** — third priority. Code changes, architecture
   decisions, debugging outcomes.

This invariant means: if a conversation has both personal content and a long technical
debugging session, the personal content must be captured even if the technical content
exceeds the extraction budget. Chunking and carry context are designed to support this
— user facts extracted in chunk 1 are carried forward so chunk 2 does not re-extract
them but also does not lose them.

Agent/technical extraction must never reduce baseline user-memory coverage.

---

## 6. Hook Entry Points

All hooks are in `core/interface/hooks.py` and invoked via the `quaid` CLI.
Each hook reads JSON from stdin and writes to stdout/stderr.

### `inject` — UserPromptSubmit memory injection

**When called:** On every user message (Claude Code `UserPromptSubmit` hook).

**Stdin:**
```json
{"prompt": "user message text", "cwd": "/path/to/project", "session_id": "abc123"}
```

**What it does:**
1. Ensures a cursor exists for the current session (seeds it if not).
2. Calls `recall_fast()` (parallel HyDE fanout, hard time budget) with the user's message.
3. Drains `data/cc-pending-notifications.jsonl` (CC's deferred notification channel).
4. Outputs `additionalContext` containing recalled memories + any pending notifications.

**Stdout:**
```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "[Quaid Memory Context]\n  1. [fact] ..."
  }
}
```

Queries shorter than 3 words are skipped (no recall performed).

---

### `inject-compact` — post-compaction re-injection

**When called:** After Claude Code compacts context (PostCompact equivalent).

**Stdin:**
```json
{"cwd": "/path/to/project", "session_id": "abc123"}
```

**What it does:** Recalls up to 10 memories scoped to the current working directory,
prints them as plain text to stdout (no JSON wrapper). Uses `recall()` with
`use_reranker=False` for speed.

---

### `extract` — write extraction signal

**When called:** On `PreCompact` hook (`--precompact` flag) or `Stop` hook (no flag).

**Stdin:**
```json
{"transcript_path": "/path/to/session.jsonl", "session_id": "abc123", "cwd": "..."}
```

**What it does:**
1. Calls `ensure_alive()` to start the daemon if not running.
2. Determines signal type: `"compaction"` if `--precompact`, else `"session_end"`.
3. Calls `write_signal()` to write the signal file atomically.
4. Logs signal file name to stderr.

Nothing is extracted synchronously. The hook exits immediately; the daemon processes
the signal asynchronously.

---

### `session-init` — project docs injection at session start

**When called:** On Claude Code `PreToolUse` or session start hook.

**Stdin:**
```json
{"session_id": "abc123", "cwd": "/path/to/project"}
```

**What it does (in order):**
1. Calls `ensure_alive()` to start the daemon if not running.
2. Calls `sweep_orphaned_sessions()` to queue any un-extracted previous sessions.
3. Seeds a cursor for the current session (so the daemon can find it for timeout extraction).
4. Collects identity files (`USER.md`, `SOUL.md`, `ENVIRONMENT.md`) from the per-instance
   identity directory.
5. Collects `TOOLS.md` and `AGENTS.md` from all project subdirectories under `projects/`.
   The `quaid` project is sorted first.
6. Checks janitor health; prepends a warning if janitor hasn't run in 24 hours.
7. Checks compatibility circuit breaker; prepends a warning if degraded.
8. Writes combined content to `.claude/rules/quaid-projects.md` (idempotent — only
   writes if content has changed).

The rules file is auto-loaded by Claude Code at session start, cached via prompt
caching, and preserved through compaction. This is more reliable than `additionalContext`
(which is ephemeral).

The rules file target directory is resolved from `QUAID_RULES_DIR` env var if set,
otherwise `<hook_cwd>/.claude/rules/`.

---

### `subagent-start` — register subagent

**When called:** On Claude Code `SubagentStart` or OpenClaw `subagent_spawned` hook.

**Stdin:**
```json
{"session_id": "parent-session-id", "agent_id": "child-agent-id", "agent_type": "task"}
```

**What it does:** Registers the child agent in `core/subagent_registry`. The daemon
will:
- Skip standalone extraction for this child session
- Merge the child's transcript into the parent's next extraction batch

---

### `subagent-stop` — mark subagent complete

**When called:** On Claude Code `SubagentStop` or OpenClaw `subagent_ended` hook.

**Stdin:**
```json
{
  "session_id": "parent-session-id",
  "agent_id": "child-agent-id",
  "agent_type": "task",
  "agent_transcript_path": "~/.claude/projects/.../child.jsonl"
}
```

**What it does:** Marks the child as complete in the subagent registry and records
its transcript path. The daemon's `process_signal()` picks this up and merges the
child transcript into the parent's extraction batch the next time the parent is
extracted.

---

## 7. Daemon Lifecycle

### Starting and stopping

```bash
# Start daemon (forks double-daemonizes, writes PID file)
quaid daemon start

# Stop daemon (sends SIGTERM, waits up to 10s, force-kills if needed)
quaid daemon stop

# Check status
quaid daemon status

# Run in foreground (debugging only)
quaid daemon run
```

### File locations

| File | Path |
|------|------|
| PID file | `QUAID_HOME/<instance>/data/extraction-daemon.pid` |
| Log file | `QUAID_HOME/<instance>/logs/daemon/extraction-daemon.log` |
| Signal directory | `QUAID_HOME/data/extraction-signals/` |
| Cursor files | `QUAID_HOME/<instance>/data/session-cursors/<session_id>.json` |
| Carryover files | `QUAID_HOME/<instance>/data/extraction-carryover/<session_id>.json` |
| Temp files | `QUAID_HOME/<instance>/data/tmp/` |

### PID file and stale detection

`read_pid()` reads the PID file and verifies the process is alive via `os.kill(pid, 0)`.
If the process is dead, the stale PID file is deleted and `None` is returned.

`start_daemon()` uses `fcntl.flock()` on the PID file to prevent concurrent starts
(TOCTOU race prevention). The daemon uses a double-fork to fully detach from the
calling process, then redirects stdio to the log file.

Log rotation: 10MB per file, 3 backup files kept (`RotatingFileHandler`).

### Daemon main loop

The loop runs at 5-second poll intervals (default `poll_interval=5.0`):

```
while not shutdown_requested:
  version_watcher.tick()        # cheap mtime check
  circuit_breaker check         # skip extraction if writes disabled
  read_pending_signals()        # process all queued signals
  for each signal: process_signal(sig)
  every N seconds: check_idle_sessions()
  janitor_scheduler.tick()      # run maintenance if due
  sleep(poll_interval)
```

The idle check interval adapts to `capture.inactivity_timeout_minutes`: it uses
`min(idle_check_interval, max(5.0, timeout_seconds / 2.0))` so shorter configured
timeouts get more frequent checks.

### Graceful shutdown

On SIGTERM/SIGINT, `shutdown_requested` is set to True. The loop finishes its current
signal batch, then processes any remaining signals in the queue before exiting. The PID
file is removed in the `finally` block.

Signals that were in-flight when the daemon crashed are preserved in the signal
directory and will be re-processed when the daemon restarts.

### Daemon not running — signal queuing

If the daemon is not running when an adapter writes a signal, the signal file sits in
`QUAID_HOME/data/extraction-signals/` until the daemon starts. `ensure_alive()` is
called at every hook invocation (`hook_extract`, `hook_session_init`) to auto-start the
daemon if it has died.

The orphan sweep at session-init also handles the case where a prior session's signals
were never processed.

---

## 8. Public API vs CLI

### `core/interface/api.py` — Python API

The public Python API is intended for internal use by hooks, tests, and operator
scripts that import Quaid directly. It wraps `memory_service` with simplified
signatures. The `project` parameter is not exposed (project scoping is handled
internally by the memory service).

Key functions:

| Function | Description |
|----------|-------------|
| `store(text, owner_id, ...)` | Store a memory with automatic deduplication. Returns `{id, status, similarity, existing_text}`. |
| `recall(query, owner_id, ...)` | Full recall pipeline: vector + FTS + graph + HyDE + reranking. Skips queries < 3 words. |
| `recall_fast(query, owner_id, ...)` | Fast recall for hook-inject hot path: parallel HyDE fanout with hard time budget. No graph traversal or reranking. |
| `search(query, owner_id, ...)` | Direct hybrid search, no HyDE/intent/reranking. |
| `create_edge(subject, relation, object, owner_id, ...)` | Create a relationship edge between entities. |
| `forget(node_id=None, query=None)` | Delete a memory by ID or by query match. |
| `get_memory(node_id)` | Fetch a single memory by UUID. |
| `stats()` | Graph-level statistics. |
| `list_domains(active_only=True)` | List registered memory domains. |
| `register_domain(domain, description, active)` | Register or update a domain. |
| `projects_search_docs(query, limit, project)` | Search documentation chunks via RAG. |

### CLI — operator interface

Agents and operators interact with Quaid through the `quaid` CLI, not the Python API
directly. The CLI handles environment setup (`QUAID_HOME`, `QUAID_INSTANCE`) and
delegates to the appropriate Python entry point.

```bash
# Memory operations
quaid recall "query"
quaid store "text"
quaid stats

# Hook invocations (used by adapter hooks, not called manually)
quaid hook-inject           # reads JSON from stdin
quaid hook-inject-compact   # reads JSON from stdin
quaid hook-extract [--precompact]  # reads JSON from stdin
quaid hook-session-init     # reads JSON from stdin
quaid hook-subagent-start   # reads JSON from stdin
quaid hook-subagent-stop    # reads JSON from stdin

# Daemon management
quaid daemon start
quaid daemon stop
quaid daemon status
quaid daemon run            # foreground, debugging only

# Project docs
quaid docs search "query"
quaid docs check
quaid docs update --apply
quaid registry list

# Maintenance
quaid janitor --task all --apply
quaid doctor
```

### `core/ingest_runtime.py` — runtime-safe ingest entry points

This thin module provides lazy-import wrappers for the ingest subsystem, keeping the
core runtime decoupled from ingest module internals:

```python
run_extract_from_transcript(transcript, owner_id, label, dry_run)
run_docs_ingest(transcript_path, label, session_id)
run_session_logs_ingest(session_id, owner_id, label, ...)
```

These are the canonical way for core code to invoke ingest functions without creating
circular imports.

---

## 9. Key Constants and Configuration

### Daemon constants (`core/extraction_daemon.py`)

| Constant | Value | Description |
|----------|-------|-------------|
| `VALID_SIGNAL_TYPES` | `("compaction", "reset", "session_end", "timeout")` | Accepted signal types |
| `MAX_TRANSCRIPT_LINES` | 50,000 | Max lines read per extraction |
| `MAX_CARRYOVER_FACTS` | 50 | Max facts kept in carryover file |
| `MAX_SIGNALS_PER_POLL` | 100 | Max signals processed per poll cycle |

### Extraction constants (`ingest/extract.py`)

| Constant | Value | Description |
|----------|-------|-------------|
| `MAX_EXTRACT_WALL_SECONDS` | 600.0 | Total wall-clock budget for one extraction |
| `MAX_CHUNKS` | 10 | Max chunks per transcript |
| Default chunk size | 8,000 tokens | From `capture.chunk_tokens` config |
| Max Opus tokens per chunk | 6,144 | Response `max_tokens` |

### Configuration keys

| Key | Description |
|-----|-------------|
| `capture.enabled` | If false, extraction is skipped entirely |
| `capture.chunk_tokens` | Target tokens per extraction chunk (default 8,000) |
| `capture.chunk_size` | Legacy alias; mirrored to the same token budget for old configs |
| `capture.inactivity_timeout_minutes` | Idle timeout for daemon-generated timeout signals (default 30) |
| `capture.skip_patterns` | List of regex patterns; matching transcript lines are removed before extraction |
| `retrieval.domains` | Active domain definitions (used to validate extracted fact domains) |
| `retrieval.fail_hard` / `retrieval.failHard` | If true, fail-hard on retrieval/LLM errors |

### Subagent transcript merge caps

| Cap | Value | Behavior on breach |
|-----|-------|--------------------|
| `MAX_CHILD_CHARS` (per-child advisory) | 50,000 chars | Warning logged; child still included — chunker handles the size |
| `MAX_MERGED_CHARS` (total merged) | 200,000 chars | Overflow subagents deferred via follow-up `session_end` signal, not dropped |

When the total cap is reached, a follow-up `session_end` signal is written for the
parent session so deferred subagents are harvested on the next daemon cycle. The
parent session's own content is always included in full.
