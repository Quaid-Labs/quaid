# Live Validation Log

Records of manual live validation runs against real adapter instances.

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
| M7 | Graph edges | ✅ | ✅ | 10 edges verified via `quaid get-edges` CLI; Marcus→Solomon (sibling_of), Marcus→OHSU (works_at), etc. |

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

## 2026-03-14 — M1–M10 OC Live Test (canary, example.local)

### Bugs Found

| Bug | Severity | Notes |
|-----|----------|-------|
| M4: `SessionTimeoutManager` reads `timeoutMinutes` once at plugin init — config change alone doesn't take effect | Medium | Must restart OpenClaw after changing `capture.inactivityTimeoutMinutes`. LIVE-TEST-GUIDE.md updated. |
| M8: Neither OC nor CC `before_agent_start` injects Quaid TOOLS.md (project CLI guide) into agent context | High | Agent created plain folders/files instead of using `quaid project create`/`link`/`registry register`. CC session-init writes `.claude/rules/quaid-projects.md` but that only covers CC identity files; project CLI guide is missing from both. OC needs equivalent TOOLS.md injection in `before_agent_start`. |
| CC extraction daemon always used `claude-opus-4-6` regardless of config | High | `adaptors/claude_code/adapter.py` `get_llm_provider()` called `ClaudeCodeOAuthLLMProvider()` with no args, defaulting to Opus. Fixed to read `deepReasoning`/`fastReasoning` from config. |
| Installer: `setup-quaid.mjs` and `lib/` are at `~/quaid/dev/` root, not `modules/quaid/` | Low | LIVE-TEST-GUIDE.md rsync command and installer path corrected. |
| M7: stale graph edges from prior test run (Oliver) persisted between runs | Low | No wipe between runs. Janitor would clean on next nightly run. Not a current-run extraction failure. |

### Follow-up items

- Wire TOOLS.md injection into OC `before_agent_start` (prependContext), matching CC's session-init hook path.
- Add DB wipe step to LIVE-TEST-GUIDE.md pre-run checklist to avoid stale edge contamination across runs.

---

## Previous Sessions

### 2026-03-08 — CC Hook Wiring Verification

- `hook-inject`: confirmed `[Quaid Memory Context]` blocks injected per user message
- `PreCompact` hook: signal written correctly
- `SessionEnd` hook: signal written correctly
- Extraction: 47 new memories extracted from live transcript (6 → 53 nodes at the time)
