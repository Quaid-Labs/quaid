# Janitor (Sandman) Reference
<!-- PURPOSE: Complete reference for nightly janitor pipeline: task list, schedule, thresholds, fail-fast, edge normalization, cost tracking -->
<!-- SOURCES: janitor.py, workspace_audit.py, adaptors/openclaw/index.ts, lib/adapter.py, lib/providers.py -->

Nightly memory maintenance pipeline. Cleans, decays, deduplicates, detects contradictions, and maintains memory quality.

## Schedule
- **Cron name:** `sandman`
- **Time:** 4:30 AM Asia/Makassar (WITA)
- **Script:** `modules/quaid/janitor.py`
- **Logs:** `logs/janitor.log` (structured JSON)
- **Stats:** `data/janitor-stats.json` (run metrics + API cost)
- **Session:** `isolated` (dedicated session per run, not main Alfie session)

### Session Isolation (Critical)

The sandman cron MUST run with `sessionTarget: "isolated"` to prevent context window overflow. If configured with `sessionTarget: "main"`, the janitor output accumulates in Alfie's main session across runs, eventually hitting the 200K token limit.

**Cron configuration requirements:**
- `sessionTarget: "isolated"` — Creates fresh session per run
- `payload.kind: "agentTurn"` — Required for isolated sessions
- `wakeMode: "now"` — Immediate execution

To verify or fix:
```bash
# Check current configuration
clawdbot cron list

# Fix if needed (replace JOB_ID with actual ID)
clawdbot cron edit <JOB_ID> --session isolated --message "Run janitor..."
```

## Concurrency Lock

The janitor uses a lock file at `data/.janitor.lock` to prevent concurrent runs. If a second instance is launched while the first is running, it exits immediately with an error. The lock file is considered stale after 2 hours (7200 seconds) and is auto-removed, allowing a new run to proceed. To force a run when the lock is stuck, manually delete the lock file.

**Lock file path resolution:** The lock file path is resolved using the `CLAWDBOT_WORKSPACE` environment variable if set, falling back to the script's relative parent directory (`Path(__file__).parent.parent.parent`). This ensures the lock file is placed in the correct workspace `data/` directory even when the janitor is invoked from a non-standard working directory or when the workspace root differs from the script's location.

## Adapter Layer

The janitor is decoupled from the host platform (OpenClaw/ClawdBot) via an adapter layer. This abstraction allows the Python modules to run independently of the specific runtime environment. The adapter layer handles platform-specific concerns like workspace path resolution, environment variable access, and integration hooks. This was formalized in the adapter layer refactor (`de6ed157`) and subsequent bug fixes (`cbd5f146`).

**Sanitization:** The adapter layer includes sanitization support (`1159f1f7`) to ensure personal data (names, identifiers) can be scrubbed from prompts, comments, and docstrings before external release. This works in conjunction with the broader personal data sanitization applied across the codebase (`1a70fdd9`, `bad3fbca`).

## Security

The janitor requires LLM access for review/dedup/contradiction tasks. Provider/model routing is resolved through the adapter/provider layer and `config/memory.json` model tier settings; core janitor logic is provider-agnostic.

**Personal data sanitization:** Prompts reference the configured owner generically rather than embedding hardcoded personal identifiers.

## Pipeline Tasks (in execution order)

| # | Task | LLM | Category | Description |
|---|------|-----|----------|-------------|
| 0 | **backup** | None | Infra | Backup keychain + workspace to NAS |
| 0b | **embeddings** | None | Infra | Backfill missing embeddings |
| 1 | **workspace** | Opus | Infra | Single-pass audit of changed workspace files |
| 1b | **docs_staleness** | Opus | Infra | Update stale docs from git diffs |
| 1c | **docs_cleanup** | Opus | Infra | Clean bloated docs (churn-based trigger) |
| 1d-snippets | **snippets** | Opus | Infra | Review & fold pending snippets into core markdown |
| 1d-journal | **journal** | Opus | Infra | Distill journal entries into core markdown themes, archive old entries |
| 2 | **review** | Opus | Memory | Batch-review pending memories (KEEP/DELETE/FIX/MERGE/MOVE_TO_PROJECT) |
| 2a | **temporal** | None | Memory | Resolve relative dates (tomorrow, yesterday) to absolute |
| 2b | **dedup_review** | Opus | Memory | Auto-confirm hash_exact entries, review embedding-based rejections (catch false positives) |
| 3 | **duplicates** | Haiku | Memory | Token-recall + batched dedup detection |
| 4 | **contradictions** | Haiku | Memory | Token-recall + batched detection, stored to DB |
| 4b | **contradictions (resolve)** | Opus | Memory | Resolve pending contradictions (keep_a/keep_b/merge/keep_both) |
| 5 | **decay** | None | Memory | Confidence decay on old unused memories |
| 5b | **decay_review** | Opus | Memory | Review decayed facts (DELETE/EXTEND/PIN) |
| 7 | **rag** | None | Infra | RAG reindex + project discovery + event processing |
| 8 | **tests** | None | Infra | Run vitest suite (npm test; only when `--task tests`, `QUAID_DEV=1`, or `janitor.run_tests=true`) |
| 9 | **cleanup** | None | Infra | Prune old recall_log (90d), dedup_log (90d), health_snapshots (180d), orphaned embeddings |

> **Category** determines fail-fast behavior — see "Fail-Fast Pipeline Guard" below.

### Task 0: Backup
Runs NAS backup scripts (`scripts/backup-*.sh`) before any maintenance, ensuring a rollback snapshot exists.

### Task 1: Workspace Audit (Opus)
Single-pass review of changed workspace markdown files:
1. Detect files changed since last run (mtime comparison)
2. Read file contents
3. Call Opus with review prompt
4. Parse and apply decisions (move to docs, convert to memory, move to project, etc.)

### Task 1b: Documentation Staleness (Opus)
Detects and updates stale docs using git-based drift detection via `detect_drift_from_git()`:
1. Check `config/memory.json → docs.sourceMapping` for monitored files
2. Compare source file vs doc file git commit timestamps (not just mtime) for accurate staleness scoring
3. Gather git diffs since doc was last modified
4. Call Opus to update doc based on diffs
5. Log to `doc_update_log` SQLite table (replaces the old `data/docs-update-log.json` for concurrent safety and queryability)

### Task 1c: Documentation Cleanup (Opus)
Cleans bloated docs based on churn heuristics (not time-based):
- **Triggers:** 10+ updates since last cleanup OR 30%+ size growth
- **State tracking:** `data/docs-cleanup-state.json`
- **Cleanup prompt:** Remove stale sections, consolidate redundant explanations, trim verbosity
- Preserves all current, accurate information
- Resets churn counters after cleanup

### Task 1d-snippets: Soul Snippets Review (Opus)
Reviews pending soul snippets (from `.snippets.md` files) and decides whether to fold them into core markdown files:
- **Source:** `*.snippets.md` files written by extraction hook during compaction/reset
- **Target files:** SOUL.md, USER.md, MEMORY.md, AGENTS.md
- **Decisions per snippet:** FOLD (add as-is), REWRITE (rephrase then add), DISCARD (remove)
- **Backups:** Created before any parent file modification
- **Config:** `docs.journal` in memory.json (`snippetsEnabled`, `targetFiles`, `maxSnippetsPerFile`). Note: config key was renamed from `docs.soulSnippets` to `docs.journal` for the unified journal system; snippets are controlled by the `snippetsEnabled` sub-key.
- **Module:** `soul_snippets.py`

### Task 1d-journal: Journal Distillation (Opus)
Distills journal diary entries into core markdown themes and archives processed entries:
- **Source:** `journal/*.journal.md` files written by extraction hook during compaction/reset
- **Target files:** SOUL.md, USER.md, MEMORY.md (core markdown updated with distilled themes)
- **Archive:** Processed entries moved to `journal/archive/{FILE}-{YYYY-MM}.md`
- **Dedup:** One entry per date+trigger per file (e.g., one Compaction and one Reset per day)
- **State tracking:** `journal/.distillation-state.json` tracks last distillation per file
- **Frequency:** Weekly by default (force with `--force-distill`)
- **Config:** `docs.journal` in memory.json (`enabled`, `mode`, `journalDir`, `targetFiles`)
- **Module:** `soul_snippets.py` (`run_journal_distillation()`)

### Task 2: Memory Review (Opus)
Pending memories are batched (50 per batch) and sent to Opus:
- **KEEP**: Personal facts, preferences, opinions, decisions (with reasoning), relationships, life events, health, locations, schedules. Personal tech decisions count (e.g., "owner chose X because Y").
- **DELETE**: Noise, conversational filler, vague statements. Also: system architecture facts, infrastructure knowledge, operational rules for AI agents, tool/config descriptions — these belong in docs/RAG, not personal memory.
- **FIX**: Good info with attribution errors ("The user" → owner name from config). Also resolves relative temporal references using `created_at`.
- **MERGE**: Related memories consolidated into one
- **MOVE_TO_PROJECT**: Facts that belong in project context rather than personal memory — moved to the appropriate project's knowledge base.

Decisions are applied immediately after each batch response.

**Owner name parameterization:** All review prompts use the owner name from `SystemsConfig` rather than hardcoding any specific name. This ensures the janitor is portable across installations. The owner name is injected into prompt templates at call time (e.g., for FIX decisions that correct attribution, or KEEP criteria referencing the owner's personal facts).

**MERGE resolution summaries:** When the review decision is MERGE, the janitor creates a `resolution_summary` node containing the merged text, linked to the original nodes via `resolved_from` edges. This preserves provenance — you can trace back from any merged fact to the originals that produced it.

### Task 2a: Temporal Resolution (no LLM)
Fixes relative temporal references in fact text using the fact's `created_at` timestamp:
- Patterns: tomorrow (+1d), yesterday (-1d), today (0d), tonight (0d), this morning (0d), next week (+7d), last week (-7d), next month (+30d), last month (-30d), next year (+365d), last year (-365d)
- Replaces with absolute dates (e.g., "tomorrow" → "on 2026-02-06")
- Auto-adjusts tense for past events ("is meeting" → "met")
- Also added to the Opus review prompt so new pending facts get resolved during review via FIX

### Task 2b: Dedup Review (Opus)
Reviews dedup candidates to catch false positives and confirm true duplicates:
- **Auto-confirm:** `hash_exact` entries are auto-confirmed without LLM review (content-identical matches need no judgment)
- **LLM review:** Embedding-based rejections (where vector similarity flagged a pair but it wasn't hash-exact) are reviewed by Opus to catch false positives
- **Prompt bias:** The review prompt is tuned to bias toward CONFIRM (~90% confirmation rate) — most dedup candidates flagged by the pipeline are genuine duplicates; the LLM review exists primarily to catch the minority of false positives rather than to second-guess the pipeline

### Task 3: Duplicates (Haiku)
Token-recall + batched LLM dedup detection:
1. Extract significant tokens per memory (nouns, names — not stopwords)
2. SQL LIKE search for candidate pairs sharing tokens
3. Vector similarity on candidates only (~30 per memory vs O(n²) full scan)
4. Batch pairs per Haiku call for confirmation (batch sizes are dynamic via `TokenBatchBuilder`, based on model context window x budget percent)

**Content hash dedup:** Before LLM-based dedup, a fast content hash check identifies exact-match duplicates. The hash comparison includes memories with `flagged` status in addition to `pending`, `approved`, and `active` statuses, preventing flagged memories from being overlooked as potential duplicates.

**Merge safety:** When applying merges, the task tracks already-merged node IDs within each run. If either node in a candidate pair was already merged earlier in the same run, the pair is skipped. This prevents cascading merges where a node that was already soft-deleted gets merged again, which could cause data loss or errors. Individual merge operations are wrapped in try/catch so a single failed merge doesn't abort the entire dedup pass.

**FK constraint handling:** Merge operations can encounter foreign key constraint errors when the target node has dependent records (e.g., edges referencing it). These FK violations are caught specifically and logged as warnings rather than errors, preventing a single constraint issue from disrupting the dedup pass. The merge is skipped and the pair is recorded for potential manual review.

### Task 4: Contradictions (Haiku)
Same token-recall approach as duplicates, but looking for conflicting facts:
- Example: "Owner lives in Bali" vs "Owner lives in Seattle"
- Confirmed contradictions are persisted to the `contradictions` table via `store_contradiction()`
- Status tracking: pending → resolved/false_positive

**Temporal awareness:** Candidate pairs now include temporal metadata (`created_at`, `valid_from`, `valid_until`) which is passed to the LLM prompt. The prompt explicitly instructs that facts from different time periods are **not contradictions** — they represent temporal succession (e.g., "lives in Austin" recorded 2024 and "lives in Bali" recorded 2025 is a life change, not a contradiction). Only facts that cannot both be true at the same time are flagged as true contradictions. This prevents false positives from natural life changes like relocations, job changes, or evolving preferences.

**Contradiction resolution — MERGE:** When contradictions are resolved via MERGE, the merged memory is stored with `status: "approved"` (not immediately active) so it still passes through the normal graduation pipeline. A `resolution_summary` node is also created (type `resolution_summary`, status `archived`) and linked to the surviving merged node via a `resolved_from` edge. This preserves provenance — you can trace from the resolution summary back to the merged result, and from there to the original contradicting facts. Resolution summary creation is best-effort; failures are silently ignored to avoid disrupting the merge flow.

### Task 4b: Resolve Contradictions (Opus)
Resolves pending contradictions detected by Task 4:
- **Decisions:** `keep_a`, `keep_b`, `keep_both`, `merge`
- **MERGE resolution:** Creates a `resolution_summary` node (type `resolution_summary`, status `archived`) linked to the surviving merged node via a `resolved_from` edge. Preserves provenance.
- **Merged status:** Merged memory stored with `status: "approved"` (not immediately active) — passes through normal graduation pipeline.
- **Fail-fast:** Skipped if `memory_pipeline_ok` is `False` (i.e., earlier memory task failed).

### Task 5: Decay (Ebbinghaus Exponential)
Uses the Ebbinghaus forgetting curve with access-scaled half-life:

**Formula:** `R = 2^(-t / half_life)` where:
- `half_life = baseHalfLifeDays × (1 + accessBonusFactor × access_count) × (2 if verified)`
- `t` = days since last access

**Config** (`config/memory.json → decay`):
- `mode`: `"exponential"` (Ebbinghaus) or `"linear"` (legacy)
- `baseHalfLifeDays`: `60` — base half-life before access scaling
- `accessBonusFactor`: `0.15` — each access extends half-life by 15%
- `minimumConfidence`: `0.1` — below this, fact is queued for review

**Tier behavior:**
- **Pinned:** Never decay (excluded entirely)
- **Verified:** 2x half-life multiplier
- **Auto-captured:** Normal decay rate
- Frequently accessed memories decay slower (access_count scales half-life)
- When confidence drops below threshold: queued for Task 5b review (not silently deleted)

### Task 7: RAG Reindex + Project Discovery

Enhanced from simple reindex to include project management:

**7a: Process queued project events** — runs `project_updater.process_all_events()` to handle any events from compact/reset that weren't processed in background.

**7b: Auto-discover for autoIndex projects** — scans each project's `homeDir` for new files matching `patterns` (respects `exclude`), registers them in the doc_registry.

**7c: Sync PROJECT.md External Files** — parses PROJECT.md "External Files" sections and creates registry entries for declared external files.

**7d: RAG reindex** — original behavior: `docs_rag.py reindex` + indexes project directories for projects with `autoIndex: true`.

### Task 8: Tests
Runs the vitest test suite. Output parser handles both vitest format (`Tests X failed | Y passed (Z)`) and test-runner.js summary format (`Total: X / Passed: Y / Failed: Z`).

**Config dependency:** Task 8 requires the `cfg` (config) object to be available. A previous bug (`9c28d5a6`) caused `cfg` to be undefined when Task 8 ran, resulting in a runtime error. The config object is now correctly passed through to the test task execution context.

## Fail-Fast Pipeline Guard

The janitor uses a `memory_pipeline_ok` boolean to protect memory quality:

- **Memory tasks** (2, 2a, 2b, 3, 4, 4b, 5, 5b): If any fails, the flag is set to `False`. Remaining memory tasks are **skipped** and graduation is **blocked**.
- **Infrastructure tasks** (0, 0b, 1, 1b, 1c, 1d-snippets, 1d-journal, 6, 7, 8, 9): Always run regardless of pipeline health.
- **Graduation** (`approved → active`): Only happens at the end of `--task all --apply` when `memory_pipeline_ok` is `True`. If blocked, facts remain as `approved/pending` and will be reprocessed next run.

This prevents partially-processed facts from graduating to `active` status where they would never be reprocessed.

The guard only applies to `--task all` runs. Explicit single-task runs (`--task duplicates`) always execute.

### System Gates

The janitor integrates with the system gates framework (added in release-readiness Wave 1). System gates provide a mechanism for protected regions — critical sections of the pipeline that must not be interrupted. Gate checks are performed before operations that modify shared state (e.g., database writes during memory review, dedup merges). If a gate is locked, the operation is deferred rather than blocked, ensuring data integrity without stalling the pipeline.

---

## Edge Extraction (Capture-Time)

Edge extraction has been moved from the nightly janitor to capture time. When facts are extracted from conversations, edges are now extracted in the same LLM call, providing:

- **Lower latency:** Edges available immediately, not after overnight processing
- **Better context:** The LLM has full conversation context during extraction
- **Reduced costs:** Single combined call vs separate fact extraction + edge extraction passes

The `_normalize_edge()` function still applies during storage:

**Inverse flipping** — edges like `child_of`, `son_of`, `daughter_of` are flipped to `parent_of` with subject/object swapped:
- `(Owner, child_of, Parent)` → `(Parent, parent_of, Owner)`
- Causal inverses: `led_to`, `caused`, `resulted_in`, `triggered` → `caused_by` (effect becomes subject)
- `(stress, led_to, symptoms)` → `(symptoms, caused_by, stress)`

**Synonym resolution** — edges are normalized to canonical forms without flipping:
- `mother_of` / `father_of` → `parent_of` (same direction — these are already parent-subject)
- `married_to` → `spouse_of`
- Causal synonyms: `because_of`, `due_to` → `caused_by` (same direction — effect is already subject)

**Symmetric alphabetical ordering** — symmetric relations always put entities in alphabetical order:
- `(Melina, spouse_of, Quaid)` → `(Quaid, spouse_of, Melina)`
- Symmetric relations: `spouse_of`, `partner_of`, `sibling_of`, `friend_of`, `neighbor_of`, `colleague_of`, `knows`, `related_to`, `family_of`

---

## Edge Keywords

When new relation types are created during edge extraction, search keywords are provided — words that would appear in user queries related to this relationship. This enables better search triggering.

**LLM-provided keywords:** The extraction prompt instructs that new relation types should include a `keywords` field with 5-10 relevant query words. Example:
```json
{"fact": 4, "has_edge": true, "subject": "Quaid", "relation": "mentors", "object": "Alex", "keywords": ["mentor", "mentee", "mentoring", "coached", "guidance"]}
```

**Storage:** Keywords are stored via `store_edge_keywords(relation, keywords)` in memory_graph.py.

**Backfill:** When a relation type is encountered without keywords, `ensure_keywords_for_relation()` generates them.

**Retrieval:** `get_edge_keywords(relation)` returns the stored keywords for a relation type.

---

## Scalable Dedup & Contradiction Detection

**Problem:** O(n²) pairwise comparison doesn't scale. At 467 memories, 214K vector comparisons; at 24K memories (1 year), 576M.

**Solution — Token-based recall:**
- 214K comparisons → 12K candidates
- 1,128 individual API calls → 1 batched call
- Full pipeline: ~2 minutes at ~$0.09/run

**Batching:** `TokenBatchBuilder` dynamically packs items based on model context window × budget percent (default 50%). Replaces fixed batch sizes. Safety cap of 500 items per batch prevents degenerate cases.

**Safeguards:**
- Consecutive failure abort: 3 failed batches in a row stops the loop
- Time limit: configurable per-task timeout (`janitor.taskTimeoutMinutes`)
- Cost tracking: input/output tokens and estimated USD
- Merged-ID tracking: prevents double-merging nodes within a single run (see Task 3)
- FK constraint handling: foreign key violations during merges are caught and logged as warnings (see Task 3)
- Lock file: prevents concurrent janitor runs (`data/.janitor.lock`, stale after 2 hours/7200s — auto-removed)

## Prompt Versioning

All LLM decisions in the janitor are tagged with a prompt version hash for reproducibility. A SHA256 hash of the system prompt template is computed at call time via `_prompt_hash()` (first 12 hex chars of SHA256), and a prefix `[prompt:abc123def456]` is prepended to the decision text stored in the relevant table (dedup_log, contradictions, decay_review_queue).

**Tagged tasks:**
- **Task 3 (Duplicates):** Dedup review reasons are prefixed with the prompt hash
- **Task 4 (Contradictions):** Contradiction explanations and resolution reasons are prefixed with the prompt hash
- **Task 2b (Dedup Review):** Dedup rejection review reasons are prefixed with the prompt hash
- **Task 5b (Decay Review):** Decay review reasons are prefixed with the prompt hash

This allows:
- **Auditing:** Which prompt version produced a given decision
- **Reproducibility:** Correlating decision quality with prompt changes
- **Debugging:** When a prompt change causes unexpected behavior, you can filter by prompt hash to find affected decisions

The hash covers the full system prompt text (including any dynamically injected context like temporal metadata).

---

## Log Format

```json
{"ts": "2026-02-02T01:48:33Z", "level": "info", "component": "janitor", "event": "janitor_start", "task": "decay", "dry_run": true}
{"ts": "2026-02-02T01:48:33Z", "level": "info", "component": "janitor", "event": "janitor_complete", "task": "decay", "success": true, "duration_seconds": 0.02, "llm_calls": 0, "errors": 0}
```

## CLI Usage

The janitor uses argparse for CLI argument parsing.

```bash
# Dry run (report only)
python3 janitor.py --task all --dry-run

# Apply changes
python3 janitor.py --task all --apply

# Single task
python3 janitor.py --task duplicates --dry-run

# Full scan (ignore last-run timestamp)
python3 janitor.py --task all --apply --full-scan
```

## Dashboard Integration

- `/api/metrics/janitor` — Status overview
- `/api/metrics/janitor/runs` — Last 3 runs (compact display)
- `/api/metrics/janitor/detail` — Full activity log

## Cost Tracking

Every run writes `api_usage` to `data/janitor-stats.json`:
```json
{
  "api_usage": {
    "calls": 26,
    "input_tokens": 38800,
    "output_tokens": 15700,
    "estimated_cost_usd": 0.094
  }
}
```

## LLM Provider Dispatch

The janitor uses the same provider-agnostic model-tier architecture as the rest of Quaid. Rather than hardcoding a single provider, model references in `config/memory.json` are resolved through adapter/provider dispatch and gateway state.

Model names are resolved via `models.deepReasoning` / `models.fastReasoning` and `models.providerModelClasses` in `config/memory.json`, with adapter/provider dispatch selecting the active provider.

The provider/adapter refactor separates LLM and embedding concerns into distinct interfaces, keeping janitor logic independent of provider-specific details.

## Module Boundaries

The janitor follows strict module boundary conventions enforced by `__all__` exports across the Quaid plugin. Each module declares its public API via `__all__`, and internal helpers are not importable by convention. Shared utilities live in the `lib/` package. This structure was formalized in the module boundaries refactor (`301cfb68`) to ensure clean separation between the janitor pipeline, LLM clients, config loading, and shared library code.

## Memory Lifecycle

Facts flow through the pipeline before becoming permanent:

```
Extraction (Opus, at compaction/reset) → status: pending, edges extracted inline
    ↓
Task 2: Review (Opus) → KEEP/FIX → status: approved | DELETE → hard deleted | MOVE_TO_PROJECT → relocated
    ↓
Task 2a: Temporal resolution (regex) → fix relative dates
    ↓
Task 3: Dedup (pending/approved vs all)
    ↓
Task 4: Contradictions (pending/approved vs all)
    ↓
Final: Graduate approved → active (only if pipeline_ok on --task all --apply)
```

Once `active`, a fact is never reprocessed by review, dedup, or contradiction detection. Only decay (Task 5) touches active facts. Nightly runs only process the day's new pending/approved facts.

Graduation is **blocked** if any memory task failed during the run (fail-fast guard). Facts remain as `approved/pending` and will be reprocessed next run.

The `--full-scan` flag bypasses the status filter for backfill runs (e.g., initial processing on all existing facts).

## Memory Tiers (Affects Decay)

| Tier | Column | Half-Life | Description |
|------|--------|-----------|-------------|
| Pinned | `pinned=1` | Never decays | Critical facts, never expire |
| Verified | `verified=1` | 2× base (120d default) | Important, slower decay |
| Auto-captured | default | Base (60d default) | Conversation extractions, access-scaled |

## Configuration

Settings are driven by `config/memory.json`. Key sections used by the janitor:

| Config Section | Used By | Settings |
|----------------|---------|----------|
| `models.deepReasoning` | review, workspace | High-tier reasoning model (explicit or `default`) |
| `models.fastReasoning` | duplicates, contradictions | Haiku model ID |
| `database.path` | all tasks | Main DB path |
| `rag.docsDir` | rag task | Docs directory to index |
| `decay` | decay task | Decay rates, thresholds |
| `dedup` | duplicates | Similarity threshold, batch size |
| `models.providerModelClasses` | all LLM tasks | Provider→deep/fast model class mapping used when tier is `default` |

Model names are resolved from `config/memory.json` by model tier, then routed through adapter/provider dispatch using active gateway provider/auth state.

**Owner name:** The `SystemsConfig.ownerName` value is injected into all LLM prompts that reference the memory owner. This replaces previously hardcoded name references, making the janitor portable across installations.

**Config availability:** The `cfg` object must be propagated to all tasks that need it. Infrastructure tasks like Task 8 (tests) also depend on config for path resolution and feature flags — ensure the config object is passed through correctly when adding new tasks.

## Error Handling

The janitor wraps each task execution in structured error handling:

- **Task-level try/catch:** Each task is wrapped individually so a failure in one task doesn't crash the entire pipeline (beyond the fail-fast guard for memory tasks).
- **LLM response validation:** API responses are validated before processing; malformed JSON from LLM calls is logged and skipped rather than crashing.
- **Database safety:** Write operations use transactions; failed writes are rolled back cleanly.
- **Graceful degradation:** If LLM API keys are missing or invalid, LLM tasks are skipped with clear error messages while non-LLM tasks still execute.
- **Dedup merge isolation:** Individual merge operations in Task
