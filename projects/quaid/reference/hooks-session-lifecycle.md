# Hooks and Session Lifecycle Reference

This document describes Quaid's hook entry points, how they are wired into
Claude Code and OpenClaw, and the full session lifecycle for each adapter.

Source files:
- `modules/quaid/core/interface/hooks.py` — all hook implementations
- `modules/quaid/adaptors/claude_code/adapter.py` — Claude Code adapter
- `modules/quaid/adaptors/openclaw/adapter.ts` — OpenClaw adapter (TS)
- `modules/quaid/adaptors/openclaw/adapter.py` — OpenClaw adapter (Python)
- `modules/quaid/core/extraction_daemon.py` — daemon + signal/cursor API
- `modules/quaid/core/subagent_registry.py` — subagent registry
- `~/.claude/settings.json` — live CC hook registration on this machine

---

## 1. Hook Entry Points

All hook commands are dispatched through the `quaid` CLI binary under the
`hook-*` namespace. The single Python entry point is
`core/interface/hooks.py:main()` (the file at
`adaptors/claude_code/hooks.py` is a backward-compatible shim that
imports and re-exports the same `main()`).

General conventions:
- Hook commands read a JSON payload from **stdin** (supplied by the host
  platform) and write results to **stdout**.
- Diagnostic messages go to **stderr** with a `[quaid][hook-name]` prefix.
- `QUAID_HOME` and `QUAID_INSTANCE` must be set in the environment before
  any hook runs (set by the settings.json command strings on CC, set by the
  gateway environment on OC).

---

### `quaid hook-inject`

**Triggered by:** `UserPromptSubmit` (Claude Code) / `before_prompt_build`
(OpenClaw) — fires on every user message.

**Stdin:**
```json
{"prompt": "...", "cwd": "...", "session_id": "..."}
```

**What it does:**

1. Ensures an extraction cursor exists for the current session so the daemon
   can discover the transcript for timeout-based extraction.
2. Calls `adapter.get_pending_context()` to drain any deferred notifications
   (CC only — see notification model below).
3. Calls `recall_fast(query=prompt, owner_id=..., limit=10)` to retrieve the
   top-10 relevant memories for the current prompt.
4. Formats memories as a `[Quaid Memory Context]` block:
   ```
   [Quaid Memory Context]
     1. [fact] User prefers dark mode (relevance: 0.87)
     2. [decision] Switched from pytest to unittest in Jan 2025 (relevance: 0.74)
     ...
   ```
5. Prepends any pending notification context (CC deferred notifications).
6. Returns the combined context via stdout as:
   ```json
   {
     "hookSpecificOutput": {
       "hookEventName": "UserPromptSubmit",
       "additionalContext": "<formatted context>"
     }
   }
   ```

If recall fails but there is pending notification context, that context is
still surfaced. Errors go to stderr; the hook never blocks message delivery.

---

### `quaid hook-inject-compact`

**Triggered by:** Post-compaction (called explicitly when needed to re-inject
context after CC collapses the context window).

**Stdin:**
```json
{"cwd": "...", "session_id": "..."}
```

**What it does:**

Re-injects memories after compaction has cleared the context window. Because
no user prompt is available at this point, recall is driven by the working
directory:

```python
recall(query=f"project context for {cwd}", owner_id=owner, limit=10, use_reranker=False)
```

Output is plain text (not JSON) written directly to stdout. This hook uses
the standard `recall()` pipeline (with graph traversal) but skips the
reranker to reduce latency.

**Difference from `hook-inject`:**
- No prompt available; query is workspace-derived
- Output is plain text, not a `hookSpecificOutput` JSON envelope
- Does not drain pending notifications

---

### `quaid hook-extract`

**Triggered by:** `PreCompact` (with `--precompact`) and `SessionEnd`
(without flag) in Claude Code; `before_compaction` and `session_end` /
`before_reset` in OpenClaw.

**Stdin:**
```json
{"transcript_path": "...", "session_id": "...", "cwd": "..."}
```

**Flags:**
- `--precompact` — marks the signal as `compaction` type (default: `session_end`)

**What it does:**

1. Calls `ensure_alive()` to start the extraction daemon if it is not running.
2. Determines adapter type (currently only `openclaw` is flagged
   `supports_compaction_control=True`; CC is `False`).
3. Calls `write_signal(signal_type, session_id, transcript_path, adapter,
   supports_compaction_control)` to write a signal file to the daemon's
   signal directory.
4. Prints the signal filename to stderr:
   `[quaid][hook-compaction] signal written: 1710000000000_12345_abc123de_compaction.json`

Signal types:
- `compaction` — triggered by `PreCompact` / `before_compaction`
- `session_end` — triggered by `SessionEnd` / `session_end`
- `reset` — triggered by `before_reset` in OpenClaw (compat fallback)
- `timeout` — generated internally by `check_idle_sessions()`

**Important:** `hook-extract` is entirely non-blocking. It writes a signal
file and returns. The extraction daemon processes it asynchronously. If the
daemon is not running when the signal is written, the signal persists on disk
and is picked up when the daemon next starts.

---

### `quaid hook-session-init`

**Triggered by:** `SessionStart` (Claude Code) / session bootstrap (OpenClaw).

**Stdin:**
```json
{"session_id": "...", "cwd": "..."}
```

**What it does (in order):**

1. **Ensure daemon alive** — calls `ensure_alive()` so the extraction daemon
   is running before any other hook fires.

2. **Orphan sweep** — calls `sweep_orphaned_sessions(current_session_id)` to
   queue extraction signals for any previous sessions whose transcripts have
   un-extracted content past their cursor. Returns count of swept sessions.

3. **Seed session cursor** — writes an initial cursor at line offset 0 for
   the current session so `check_idle_sessions()` can discover it. Resolves
   the transcript path via `adapter.get_sessions_dir()` + recursive glob on
   `{session_id}.jsonl`.

4. **Collect identity files** — reads `USER.md`, `SOUL.md`, `MEMORY.md` from
   the adapter's per-instance identity directory
   (`adapter.identity_dir()`). Each file is prefixed as:
   ```
   --- USER.md ---
   <content>
   ```

5. **Collect project docs** — scans all `projects/<name>/` subdirectories for
   `TOOLS.md` and `AGENTS.md`. The `quaid` project directory is sorted first.
   Each file is prefixed as:
   ```
   --- quaid/TOOLS.md ---
   <content>
   ```

6. **Janitor health check** — calls `_check_janitor_health()`. If the janitor
   has never run or last ran more than 24 hours ago, a warning is prepended:
   ```
   [Quaid Warning] Janitor last ran 3 days ago. Stale janitor causes
   memory/doc drift. Run: quaid janitor --task all --apply
   ```

7. **Compatibility check** — calls `notify_on_use_if_degraded()` and prepends
   a `--- SYSTEM WARNING ---` block if the Quaid instance is running in a
   degraded or safe mode.

8. **Write rules file** — combines all sections and writes to:
   - `$QUAID_RULES_DIR/quaid-projects.md` if `QUAID_RULES_DIR` is set, or
   - `{cwd}/.claude/rules/quaid-projects.md` using `cwd` from stdin (falling
     back to `os.getcwd()`).

   **Idempotent:** reads the existing file first and skips the write if
   content is unchanged. This avoids unnecessary filesystem churn and
   prevents Claude Code from invalidating its prompt cache on no-op starts.

The rules file is picked up by Claude Code's automatic `.claude/rules/`
loading mechanism and persists through compaction (unlike `additionalContext`
which is lost when the context window is compacted).

---

### `quaid hook-search "query"`

**Usage:** `quaid hook-search "some query"` (argument, not stdin)

**What it does:**

Runs a combined search across memories and project documentation:

1. `recall(query=query, owner_id=owner, limit=5)` — full memory recall with
   graph traversal.
2. `projects_search_docs(query=query, limit=3)` — RAG search across the
   docs index.

Prints results to stdout in two labeled sections:
```
=== Memory Results ===
  1. [fact] ... (relevance: 0.91)
  ...

=== Documentation Results ===
  1. [Title] First 200 chars of chunk...
  ...
```

Unlike `hook-inject`, this hook is called on-demand (e.g., from the agent
via Bash tool) rather than automatically by the platform.

---

### `quaid hook-subagent-start`

**Triggered by:** `SubagentStart` (Claude Code) / `subagent_spawned` (OpenClaw)

**Stdin:**
```json
{"session_id": "...", "agent_id": "...", "agent_type": "..."}
```
- `session_id` — the **parent** session ID
- `agent_id` — the child/subagent session ID
- `agent_type` — free-form type string (e.g., `"claude-code"`)

**What it does:**

Calls `subagent_registry.register()` to create a child entry under the parent
session:

```json
{
  "child_id": "<agent_id>",
  "parent_session_id": "<session_id>",
  "child_type": "<agent_type>",
  "transcript_path": "",
  "status": "running",
  "harvested": false,
  "registered_at": "2025-03-13T10:00:00Z",
  "completed_at": null,
  "harvested_at": null
}
```

The registry entry tells the extraction daemon to skip standalone timeout
extraction for this child session. Child transcript facts will instead be
merged into the parent session's extraction batch.

---

### `quaid hook-subagent-stop`

**Triggered by:** `SubagentStop` (Claude Code) / `subagent_ended` (OpenClaw)

**Stdin:**
```json
{
  "session_id": "...",
  "agent_id": "...",
  "agent_type": "...",
  "agent_transcript_path": "...",
  "last_assistant_message": "..."
}
```

**What it does:**

Calls `subagent_registry.mark_complete()` to update the child entry:
- Sets `status` to `"complete"`
- Sets `completed_at` timestamp
- Sets `transcript_path` (CC provides the path at stop time; OC may not)

The extraction daemon's parent-session extraction then calls
`get_harvestable(parent_session_id)` to collect completed child transcripts
and include them in the extraction batch. After extraction, the daemon calls
`mark_harvested()` to prevent double-extraction.

---

## 2. Claude Code Hook Wiring

Hooks are registered in `~/.claude/settings.json`. On this machine the live
registration is:

| CC Hook Type | Command |
|---|---|
| `SessionStart` | `quaid hook-session-init` |
| `UserPromptSubmit` | `quaid hook-inject` |
| `PreCompact` | `quaid hook-extract --precompact` |
| `SessionEnd` | `quaid hook-extract` |
| `SubagentStart` | `quaid hook-subagent-start` |
| `SubagentStop` | `quaid hook-subagent-stop` |

Each command is prefixed with environment variables:
```
QUAID_HOME='/Users/owner/quaid/claudecode'
QUAID_INSTANCE='claude-code'
/Users/owner/quaid/claudecode/modules/quaid/quaid hook-<name>
```

The `QUAID_HOME` and `QUAID_INSTANCE` values isolate this Claude Code
instance from other Quaid adapters running on the same machine. Do **not**
set these globally in shell profiles — they are intended to be set per-hook
invocation only.

Hook input JSON is piped to stdin by Claude Code. Each hook type provides
different fields; see the stdin schemas in section 1.

There is **no** `PostCompact` or `inject-compact` hook wired in the current
settings.json. `hook-inject-compact` exists as a callable command but is not
auto-registered.

---

## 3. Session Lifecycle — Claude Code

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SessionStart                                                           │
│    └─ hook-session-init                                                 │
│         ├─ ensure daemon alive                                          │
│         ├─ sweep orphaned sessions (queue extraction for stale tails)   │
│         ├─ seed cursor for current session_id                           │
│         └─ write/update .claude/rules/quaid-projects.md                │
│              (identity files + TOOLS.md + AGENTS.md for all projects)  │
├─────────────────────────────────────────────────────────────────────────┤
│  User sends message                                                     │
│    └─ UserPromptSubmit → hook-inject                                    │
│         ├─ drain pending notifications → prepend to context             │
│         ├─ recall_fast(prompt, limit=10)                                │
│         └─ return {additionalContext: "[Quaid Memory Context]\n ..."}   │
│              (injected into Claude's context for this turn)             │
├─────────────────────────────────────────────────────────────────────────┤
│  (repeat UserPromptSubmit for each user message)                        │
├─────────────────────────────────────────────────────────────────────────┤
│  Context window fills → CC triggers PreCompact                          │
│    └─ hook-extract --precompact                                         │
│         ├─ ensure daemon alive                                          │
│         └─ write_signal(type="compaction", session_id, transcript_path)│
│              → QUAID_HOME/data/extraction-signals/<ts>_<pid>_<uuid>_   │
│                 compaction.json                                         │
│    (Extraction daemon picks up signal asynchronously)                   │
│    └─ Daemon reads transcript from cursor offset                        │
│         ├─ chunks transcript (waterfall batching)                       │
│         ├─ extracts facts + edges via LLM (Opus)                        │
│         ├─ stores in SQLite memory graph                                │
│         └─ advances cursor to EOF                                       │
├─────────────────────────────────────────────────────────────────────────┤
│  SubagentStart (if Claude Code spawns a sub-agent)                      │
│    └─ hook-subagent-start                                               │
│         └─ register child in QUAID_HOME/data/subagent-registry/         │
│              <parent_session_id>.json                                   │
├─────────────────────────────────────────────────────────────────────────┤
│  SubagentStop                                                           │
│    └─ hook-subagent-stop                                                │
│         └─ mark_complete(child_id, transcript_path)                     │
├─────────────────────────────────────────────────────────────────────────┤
│  Session ends (user closes CC or session replaced)                      │
│    └─ SessionEnd → hook-extract                                         │
│         └─ write_signal(type="session_end", session_id, transcript_path)│
│    (Daemon picks up, extracts any remaining content past cursor)        │
└─────────────────────────────────────────────────────────────────────────┘
```

**Idle timeout path (background):**

The extraction daemon's main loop also calls `check_idle_sessions()`.
This scans all cursor files under
`QUAID_HOME/<instance>/data/session-cursors/` and generates a `timeout`
signal for any session whose transcript file has not been modified for
more than 30 minutes and has un-extracted content (i.e., file is larger
than cursor offset). This catches sessions that end without triggering
`SessionEnd` (e.g., process kill, power-off).

---

## 4. Session Lifecycle — OpenClaw

OpenClaw hooks are implemented in the TypeScript adapter
(`adaptors/openclaw/adapter.ts`) and the Python adapter
(`adaptors/openclaw/adapter.py`). The TypeScript layer registers platform
hooks; the Python layer handles extraction signaling.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  Session/agent start                                                    │
│    └─ before_agent_start (TS facade)                                    │
│         └─ inject baseline context (identity files, workspace info)     │
├─────────────────────────────────────────────────────────────────────────┤
│  User sends message                                                     │
│    └─ before_prompt_build (TS)                                          │
│         ├─ recall memories for current prompt (via facade)              │
│         └─ inject as [Quaid Memory Context] block into prompt           │
├─────────────────────────────────────────────────────────────────────────┤
│  (repeat before_prompt_build for each user message)                     │
├─────────────────────────────────────────────────────────────────────────┤
│  User types /compact → gateway triggers before_compaction               │
│    └─ before_compaction (TS)                                            │
│         ├─ filter conversation messages (remove system/heartbeat noise) │
│         └─ writeDaemonSignal(session_id, "compaction", {source: ...})   │
│              → QUAID_HOME/data/extraction-signals/<ts>_compaction.json  │
│    (OC sets supports_compaction_control=True — daemon may ask OC to    │
│     force a /compact if extraction warrants it)                        │
├─────────────────────────────────────────────────────────────────────────┤
│  User types /new or /reset → command:new / command:reset event         │
│    └─ handleSlashLifecycleFromMessage (TS)                              │
│         └─ writeDaemonSignal(session_id, "reset", {source: ...})       │
├─────────────────────────────────────────────────────────────────────────┤
│  before_reset (TS) — compatibility fallback for older runtimes         │
│    └─ writeDaemonSignal(session_id, "reset", {source: "before_reset"}) │
├─────────────────────────────────────────────────────────────────────────┤
│  Session replaced → session_end event                                  │
│    └─ session_end (TS) — primary reset/new lifecycle capture path      │
│         └─ writeDaemonSignal(session_id, "session_end", {...})         │
└─────────────────────────────────────────────────────────────────────────┘
```

**Differences from CC:**

| Aspect | Claude Code | OpenClaw |
|---|---|---|
| Hook system | Native CC settings.json hooks | Gateway plugin hook registration |
| Memory inject | `additionalContext` JSON envelope | Prompt build callback in TS |
| Compaction trigger | CC-driven `PreCompact` | User `/compact`, auto-compaction |
| Reset trigger | `SessionEnd` | `session_end` + `before_reset` compat |
| Compaction control | CC controls timing | OC `supports_compaction_control=True` |
| Notifications | Deferred via `cc-pending-notifications.jsonl` | Live via `openclaw message send` CLI |

---

## 5. Extraction Signal System

### Signal directory

Signals are written at the **QUAID_HOME level** (not per-instance):

```
$QUAID_HOME/data/extraction-signals/
```

This directory is shared across adapter instances and the extraction daemon.

### Signal file format

Filename: `{timestamp_ms}_{pid}_{uuid8}_{signal_type}.json`

Example: `1710000000000_12345_abc123de_compaction.json`

Content:
```json
{
  "type": "compaction",
  "session_id": "<session_id>",
  "transcript_path": "/path/to/session.jsonl",
  "adapter": "claude-code",
  "supports_compaction_control": false,
  "timestamp": "2025-03-13T10:00:00Z",
  "meta": {}
}
```

Valid signal types: `compaction`, `reset`, `session_end`, `timeout`

### Cursor directory

Per-instance, stored under the instance root:

```
$QUAID_HOME/<instance>/data/session-cursors/<session_id>.json
```

Cursor file content:
```json
{
  "session_id": "<session_id>",
  "line_offset": 1234,
  "transcript_path": "/path/to/session.jsonl",
  "updated_at": "2025-03-13T10:05:00Z"
}
```

The cursor tracks the last line number the daemon successfully extracted.
When a new signal arrives for a session, the daemon reads from `line_offset`
to EOF rather than re-extracting the full transcript. This is the mechanism
that prevents double-extraction.

### If the daemon is not running

Signal files are written atomically to disk and persist until the daemon
picks them up. When `ensure_alive()` starts the daemon, it processes all
pending signals in chronological order. No signals are lost during daemon
downtime.

---

## 6. Subagent Registry

Storage: `$QUAID_HOME/data/subagent-registry/<parent_session_id>.json`

Writes are protected by a per-session file lock
(`<parent_session_id>.lock`) using `fcntl.flock(LOCK_EX)`.

### Registry file structure

```json
{
  "parent_session_id": "<parent_session_id>",
  "children": {
    "<child_id>": {
      "child_id": "<child_id>",
      "parent_session_id": "<parent_session_id>",
      "child_type": "claude-code",
      "transcript_path": "/path/to/child.jsonl",
      "status": "complete",
      "harvested": false,
      "registered_at": "2025-03-13T10:00:00Z",
      "completed_at": "2025-03-13T10:10:00Z",
      "harvested_at": null
    }
  }
}
```

### Lifecycle states

| Status | Harvested | Meaning |
|---|---|---|
| `running` | false | Sub-agent is active |
| `complete` | false | Transcript ready for extraction |
| `complete` | true | Transcript already extracted |

### Daemon behavior for subagents

1. When the daemon processes a `session_end` or `compaction` signal for a
   session ID, it calls `is_registered_subagent(session_id)` by scanning all
   registry files. If the session is a registered child, the daemon **skips**
   standalone timeout extraction for it.
2. When the daemon extracts a parent session, it calls
   `get_harvestable(parent_session_id)` to collect completed child entries
   that have a transcript path and have not yet been harvested.
3. Child transcripts are merged into the parent extraction batch, and facts
   are attributed to the parent session lineage.
4. After harvesting, `mark_harvested(parent_session_id, child_id)` is called
   to prevent re-extraction on subsequent parent signals.

Registry files older than 48 hours are removed by `cleanup_old_registries()`,
which is called periodically by the daemon.

---

## 7. Notification Model

### Claude Code (deferred notifications)

CC has no in-terminal notification channel. The adapter writes notifications
to a pending file:

```
$QUAID_HOME/<instance>/data/cc-pending-notifications.jsonl
```

Each line is a JSON entry: `{"message": "...", "ts": "2025-03-13T10:00:00Z"}`

On the **next** `UserPromptSubmit` hook, `get_pending_context()` drains the
file (reads all entries younger than 3600 seconds, deletes the file) and
prepends a formatted block to the `additionalContext`:

```
Print <notification> verbatim before your response, then print ---

<notification>
• Janitor completed: 12 memories merged, 3 decayed
</notification>
```

The agent then relays this to the user verbatim at the top of its response.

### OpenClaw (live notifications)

OC delivers notifications synchronously via the `openclaw message send` CLI:

```bash
openclaw message send --channel <channel> --target <target> --message "..."
```

The channel and target are resolved from
`~/.openclaw/agents/main/sessions/sessions.json` by looking up the most
recently active user-routable session row.

If `QUAID_DISABLE_NOTIFICATIONS` is set (and `force=False`), both adapters
skip delivery silently.

---

## 8. Environment Variables

| Variable | Scope | Purpose |
|---|---|---|
| `QUAID_HOME` | All hooks | Root of the Quaid instance silo |
| `QUAID_INSTANCE` | All hooks | Instance identifier (e.g., `claude-code`) |
| `QUAID_RULES_DIR` | `hook-session-init` | Override destination for `quaid-projects.md` |
| `QUAID_DISABLE_NOTIFICATIONS` | Notifications | Silence all notifications |
| `QUAID_MESSAGE_CLI` | OC notifications | Override `openclaw`/`clawdbot` binary path |
| `CLAWDBOT_WORKSPACE` | OC adapter | OpenClaw workspace root (OC-specific) |

`QUAID_HOME` must be set per-hook-invocation. Do not set it globally.
Each adapter instance on a machine has its own `QUAID_HOME` silo.

---

## 9. Key File Paths (this machine)

| File | Path |
|---|---|
| CC settings (hook registration) | `~/.claude/settings.json` |
| CC rules file (session context) | `<project-cwd>/.claude/rules/quaid-projects.md` |
| CC instance silo | `/Users/owner/quaid/claudecode/` |
| Extraction signals dir | `$QUAID_HOME/data/extraction-signals/` |
| Session cursors dir | `$QUAID_HOME/<instance>/data/session-cursors/` |
| Subagent registry dir | `$QUAID_HOME/data/subagent-registry/` |
| CC pending notifications | `$QUAID_HOME/<instance>/data/cc-pending-notifications.jsonl` |
| CC transcripts (sessions) | `~/.claude/projects/**/<session_id>.jsonl` |
| OC sessions JSON | `~/.openclaw/agents/main/sessions/sessions.json` |
| OC transcripts (sessions) | `~/.openclaw/sessions/` |
| Hook trace log (OC) | `$QUAID_HOME/logs/quaid-hook-trace.jsonl` |
