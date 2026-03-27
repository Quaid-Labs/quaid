# Live Validation Log

Records of manual live validation runs against real adapter instances.

This file is a historical record, not the source of truth for the current
release bar. The current live-suite definition lives in
`modules/quaid/tests/LIVE-TEST-GUIDE.md`, and release approval follows
`docs/RELEASE.md`.

---


## 2026-03-13 — v0.3.0 Prerelease Validation

### Summary

Full M0–M7 live test run against OC 3.11 (<oc-host>) and CC (local testbench).

### Environment

| Adapter | Host | Version |
|---------|------|---------|
| OpenClaw | <oc-host> | 2026.3.11 |
| Claude Code | local | current stable |
| Quaid | both | 0.3.0-canary (commit `14d99b20`) |

### Results

| Marker | Description | OC 3.11 | CC | Notes |
|--------|-------------|---------|-----|-------|
| M0 | Fresh install / bootstrap | ✅ | ✅ | |
| M1 | Store and recall | ✅ | ✅ | |
| M2 | Session signal extraction | ✅ | ✅ | hook-extract writes signal; daemon processes async |
| M3 | PreCompact/extraction trigger | ✅ | ✅* | CC has no `/compact`; uses `PreCompact` hook. Signal written + synchronous ingest confirmed (513→518 nodes). |
| M4 | /new session signal | ✅ | N/A | CC has no `/new`; session boundary is new Claude Code session. Not directly testable in-session. |
| M5 | Memory injection | ✅ | ✅ | 6 memories injected via `[Quaid Memory Context]` blocks per message |
| M6 | Deliberate recall | ✅ | ✅ | |
| M7 | Graph edges | ✅ | ✅ | 10 edges verified via `quaid get-edges` CLI; Marcus→Owner (sibling_of), Marcus→employer (works_at), etc. |

*M3 on CC: `PreCompact` hook writes `compaction.json` signal to `data/extraction-signals/`; ingest pipeline confirmed via `run_extract_from_transcript` synchronous call.

### Notes

- **M4 on CC is N/A by design.** OpenClaw exposes `/new` as a command; Claude Code session boundaries are managed by the host. Extraction fires at `SessionEnd` (hook registered in `settings.json`).
- **Extraction daemon PID file** was stale during CC M3 test (PID 3830 was a session-init hook process, not the daemon). Daemon should be started via `quaid daemon start` at boot, not rely on stale PID.
- **M3 OC** tested via user-typed `/compact` in OpenClaw.
- **M7 CC** confirmed both via raw `sqlite3` and `quaid get-edges` CLI on `fe21a6b0` (Marcus node).

---

## 2026-03-13 — Installer Improvements (same session)

Changes shipped in `9240ee2a feat(installer): shared embeddings config, instance ID prompt, and config targeting`:

| Feature | Status |
|---------|--------|
| Instance ID prompt in installer | ✅ Shipped — shows existing instances, per-adapter sharing tips, default = adapter name if not taken |
| Shared embeddings config (`QUAID_HOME/shared/config/memory.json`) | ✅ Shipped — first install wins; subsequent installs inherit |
| `detectSharedEmbeddings()` provider-agnostic check | ✅ Shipped — checks by provider block, stubbed for openai/cohere |
| `quaid instances list` CLI | ✅ Shipped — lists all instances under QUAID_HOME, `--json` flag |
| `quaid config edit --shared / --instance <id>` | ✅ Shipped — both `config_cli.mjs` and `config_cli.py` |
| Legacy flat `QUAID_HOME/config/memory.json` write removed | ✅ Shipped |
| Compatibility matrix populated for v0.3.0 | ✅ Shipped — OC ≥2026.3.7 compatible (tested 3.7/3.8/3.11), CC ≥1.0.0 compatible |

---

## 2026-03-13 — Projects System Live Test Run

Full OC CRUD + CC CRUD + Cross-Platform (XP) tests on <oc-host>.
Both OC and CC share `QUAID_HOME=<your-quaid-home>`.

### Bugs Found and Fixed

| Bug | Fix |
|-----|-----|
| `create_project` set `instances: [adapter.adapter_id()]` — always returned `openclaw` regardless of `QUAID_INSTANCE` | Changed to `instance_id()` which reads `QUAID_INSTANCE` correctly |
| `delete_project` left ghost rows in SQLite `project_definitions` and `doc_registry` | Added `DELETE FROM project_definitions/doc_registry WHERE name/project = ?` after JSON registry save |
| `rag reindex --all` did not index files registered via `doc_registry` | Added third pass: enumerate `DocsRegistry().list_docs()` and index each file not yet chunked |
| `quaid project link/unlink` commands did not exist | Implemented `link_project`, `unlink_project` in `project_registry.py` and wired into CLI |

### Results

| Test | Result | Notes |
|------|--------|-------|
| OC-P1 Create | ✅ | `oc-test-proj` at `openclaw/projects/oc-test-proj/` |
| OC-P2 Register doc | ✅ | `/tmp/oc-test-doc.md` registered (id=6) |
| OC-P3 Search | ✅ | `/tmp/oc-test-doc.md` top result (similarity 0.732) |
| OC-P4 Show | ✅ | `instances: ["openclaw"]`, correct JSON |
| OC-P5 Janitor dry-run | ✅ | No orphan warnings |
| OC-P6 Markdown sanity | ✅ | `# Project: OC Test Project`, UTF-8, `docs/` present |
| OC-P7 Delete | ✅ | Registry empty, dir gone |
| CC-P1 Create | ✅ | `cc-test-proj` at `claude-code/projects/cc-test-proj/` |
| CC-P2 Register doc | ✅ | `/tmp/cc-test-doc.md` registered |
| CC-P3 Search | ✅ | `/tmp/cc-test-doc.md` top result (similarity 0.747); required `reindex --all` (janitor RAG task does not trigger registry pass — follow-up needed) |
| CC-P4 Show | ✅ | `instances: ["claude-code"]` after adapter_id bug fix applied |
| CC-P5 Janitor dry-run | ✅ | No orphan warnings |
| CC-P6 Markdown sanity | ✅ | `# Project: CC Test Project`, UTF-8, `docs/` present |
| CC-P7 Delete | ✅ | Registry empty, dir gone |
| XP-1 Global registry | ✅ | OC and CC see identical list |
| XP-2 OC creates project+doc | ✅ | `shared-xp-proj`, `/tmp/oc-xp-doc.md` registered |
| XP-3 CC sees project | ✅ | `shared-xp-proj` visible via CC global-registry |
| XP-4 CC links + adds doc | ✅ | `instances: ["openclaw","claude-code"]`; both docs listed |
| XP-5 CC sees OC doc | ✅ | `/tmp/oc-xp-doc.md` top result in CC search (similarity 0.734) |
| XP-6 OC sees CC doc | ✅ | `/tmp/cc-xp-doc.md` top result in OC search (similarity 0.717) |
| XP-7 Janitor cross-instance | ✅ | No orphan warnings |
| XP-8 Markdown sanity | ✅ | `# Project: Cross-Platform Test`, UTF-8, `docs/` present |
| XP-9 Cleanup | ✅ | Registry empty, dir gone |

### Follow-up items

- `janitor --task rag --apply` does not trigger doc_registry pass — only `reindex --all` does. Consider wiring the doc_registry pass into the janitor RAG task.
- OC-P3 and CC-P3 work after `reindex --all` but the test protocol lists `janitor --task rag --apply` as the indexing step. Update protocol or wire janitor to call reindex.

---

## 2026-03-14 — M1–M10 OC Live Test (canary, <oc-host>)

### Bugs Found

| Bug | Severity | Notes |
|-----|----------|-------|
| M4: `SessionTimeoutManager` reads `timeoutMinutes` once at plugin init — config change alone doesn't take effect | Medium | Must restart OpenClaw after changing `capture.inactivityTimeoutMinutes`. LIVE-TEST-GUIDE.md updated. |
| M3: OC gateway prepends `[Day Date Time TZ] ` to every user message — slash-command detection patterns checked bare `/compact` and never matched | High | Fixed in `handleSlashLifecycleFromMessage` and `tickSessionIndex`: strip OC timestamp prefix, use last non-empty line. `adapter.js` rebuild required after `.ts` change. |
| M3/rate-limit: `injectProjectContext` + `injectFullJournalContext` fired unconditionally on every session start, burning token budget before agent could respond | Medium | Gated both calls behind `isAutoInjectEnabled()`. Use `retrieval.autoInject=false` to suppress all injection during rate-constrained testing; re-enable before M5. |
| M3/rate-limit: Repeated 429 retries exhaust the OAuth account hourly budget | Low | Test infra issue — each failed retry adds to rate-limit pressure. Use longer backoffs (15-30 min) when hitting hard account limits, and skip to non-LLM milestones (M9, M10) while waiting. |
| M8: Neither OC nor CC `before_agent_start` injects Quaid TOOLS.md (project CLI guide) into agent context | High | Agent created plain folders/files instead of using `quaid project create`/`link`/`registry register`. CC session-init writes `.claude/rules/quaid-projects.md` but that only covers CC identity files; project CLI guide is missing from both. OC needs equivalent TOOLS.md injection in `before_agent_start`. |
| CC extraction daemon always used `claude-opus-4-6` regardless of config | High | `adaptors/claude_code/adapter.py` `get_llm_provider()` called `ClaudeCodeOAuthLLMProvider()` with no args, defaulting to Opus. Fixed to read `deepReasoning`/`fastReasoning` from config. |
| Installer: `setup-quaid.mjs` and `lib/` are at `~/quaid/dev/` root, not `modules/quaid/` | Low | LIVE-TEST-GUIDE.md rsync command and installer path corrected. |
| M7: stale graph edges from prior test run (Oliver) persisted between runs | Low | No wipe between runs. Janitor would clean on next nightly run. Not a current-run extraction failure. |

### Results

| Milestone | Result | Notes |
|-----------|--------|-------|
| M0 | ✅ PASS | Clean install, health/doctor passed |
| M1 | ✅ PASS | `/new` extraction, PROOFNEW fact stored |
| M2 | ✅ PASS | `/reset` extraction, PROOFRESET fact stored |
| M3 | ⚠️ PARTIAL | Signal path verified (daemon-compaction logged); Opus extraction rate-limited. Daemon cursor preserved for auto-retry. |
| M4 | ✅ PASS | Timeout extraction fired after 1-min config + restart |
| M5–M9 | ❌ BLOCKED | Anthropic OAuth account persistently rate-limited (daily cap exhausted by M3 retry storm). All LLM-dependent milestones blocked. |
| M10 | ✅ PASS (infra) | health/stats/docs CLI all run; config loaded; DB accessible |

### Environment Blocker

The `<oc-host>` OC instance used the operator's Claude.ai OAuth path during this run. The M3 retry storm (multiple failed attempts before the adapter.js rebuild fix) exhausted the account's daily rate-limit budget. After the budget was consumed, even bare single-turn Haiku calls returned 429 immediately. This persisted for 1.5+ hours with no recovery.

**Test infra recommendation:** Add a dedicated Anthropic API key for live test runs. The `QUAID_TEST_API_KEY` env var (or a dedicated test-only key in `~/.openclaw/agents/main/agent/auth-profiles.json`) should be used for live test sessions to avoid consuming a maintainer's personal account quota.

### Follow-up items

- Wire TOOLS.md injection into OC `before_agent_start` (prependContext), matching CC's session-init hook path.
- Add DB wipe step to LIVE-TEST-GUIDE.md pre-run checklist to avoid stale edge contamination across runs.
- Add dedicated test API key configuration to the test environment on the live-test host.
- LIVE-TEST-GUIDE.md: note that `retrieval.autoInject` must be `false` for M3 (no LLM calls during signal test) and re-enabled before M5.

---

## 2026-03-14 — M1 OC Second Run (canary, <oc-host>, fresh reinstall)

### Bugs Found

| Bug | Severity | Notes |
|-----|----------|-------|
| M1: `resolveLifecycleHookSessionId` uses `eventObj.sessionId` before sessions.json key lookup | High | OC hook events carry an internal session UUID that differs from the transcript filename. sessions.json is authoritative. Fix: try `resolveSessionIdFromSessionKey(eventSessionKey)` first, fall back to direct event sessionId. |
| M1: TUI sessions (`agent:main:tui-*`) lose to `agent:main:main` on `updatedAt` in `pickActiveInteractiveSession` | High | OC may refresh `agent:main:main.updatedAt` for background/relay activity while the user is active in a TUI session, causing the session watcher to track the wrong (often empty) session. Fix: give TUI and telegram session keys a higher priority tier than `agent:main:main`. |
| M1: `memoryConfigCandidates()` reads `QUAID_HOME/config/memory.json` which doesn't exist on fresh installs | Critical | Installer writes per-instance config to `QUAID_HOME/QUAID_INSTANCE/config/memory.json`; adapter was never looking there. `getMemoryConfig()` returned `buildFallbackMemoryConfig()` with no `autoInject` key → always defaulted to `true`. All config changes (autoInject, model tier) were written to the right file but silently ignored. Fix: prepend `QUAID_HOME/QUAID_INSTANCE/config/memory.json` to candidates list. |
| M1/rate-limit: Repeated retry storms exhaust OAuth daily cap | Low | Same infra issue as previous run. Skip to non-LLM milestones (M9, M10) while waiting for reset. |

### Fixes Shipped (commits on canary)

| Commit | Description |
|--------|-------------|
| `0139dd41` | `resolveLifecycleHookSessionId`: sessions.json key lookup before event sessionId |
| `0139dd41` | `pickActiveInteractiveSession`: filesystem fallback when sessions.json absent |
| `533aa056` | `pickActiveInteractiveSession`: TUI sessions outrank `agent:main:main` (tier sort) |
| `d19645f4` | `memoryConfigCandidates`: read per-instance config first (`QUAID_HOME/INSTANCE/config/memory.json`) |

### Results

| Milestone | Result | Notes |
|-----------|--------|-------|
| M0 | ✅ PASS | Fresh reinstall, health/doctor passed, autoInject=false set |
| M1 | ⏸ BLOCKED (env) | Config-path fix confirmed working (first bare turn clean, no injection). Provider rate-limited on first agent turn — daily cap. Retry after reset. |
| M9 | ❌ BLOCKED (env) | `--dry-run` still makes LLM classifier calls. Janitor hung holding `.janitor.lock`, returned 50-byte non-JSON rate-limit error body. Not a Quaid bug. |
| M10 | 🔄 IN PROGRESS | health/stats/docs — fully read-only, no LLM calls. |

### Notes

- Hot-swapping adapter.js requires an OC **gateway restart**, not just a new TUI session. The old adapter.js stays in memory until the gateway process is restarted.
- `retrieval.autoInject=false` must be set via Python direct write (not `quaid config set`) on fresh installs because `@clack/prompts` is missing from the installed plugin's `node_modules`.

---

## 2026-03-14 — M5–M6 OC Resume (canary, <oc-host>)

### Bugs Found

| Bug | Severity | Notes |
|-----|----------|-------|
| M6: `before_prompt_build` auto-inject uses OC gateway exec-error strings as recall query | Medium | When an OC gateway subprocess dies (e.g. reranker killed by SIGTERM), OC sends `Exec failed (cmd, signal SIGTERM) :: ...` as `event.prompt`. This string passed all existing prompt filters and became the recall query, producing garbled `[quaid][recall] source=auto_inject query="Exec failed..."` log entries and a meaningless DB lookup. Fix: added `Exec failed` to the skip-pattern regex in `beforePromptBuildHandler`. |
| M6: OC retrieval sub-agent scores fact as perfect match (5) but main OC agent says "no concrete details" | Medium | OC-side bug — `quaid recall "niece"` CLI returns Anne correctly; the issue is OC's retrieval pipeline not surfacing sub-agent results to the main agent response. Not a Quaid bug. |

### Fixes Shipped (commits on canary)

| Commit | Description |
|--------|-------------|
| `a3d97071` | `before_prompt_build`: skip `Exec failed` OC gateway error strings as recall queries |

### Diagnostics

- `quaid recall "niece"` on the live-test host returns correct facts (`User has a niece`, `Users niece Anne is 7 years old and loves drawing`) even while gateway logs show reranker timeouts.
- Reranker SIGTERM is a transient OC infra issue; Quaid recall degrades gracefully (skips reranking, returns vector results).
- M5 still blocked by OAuth rate limit (429 before first LLM turn).

---

## Previous Sessions

### 2026-03-08 — CC Hook Wiring Verification

- `hook-inject`: confirmed `[Quaid Memory Context]` blocks injected per user message
- `PreCompact` hook: signal written correctly
- `SessionEnd` hook: signal written correctly
- Extraction: 47 new memories extracted from live transcript (6 → 53 nodes at the time)
