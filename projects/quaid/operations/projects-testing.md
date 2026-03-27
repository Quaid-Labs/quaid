# Projects System — Live Test Protocol

> **Note:** Replace `<oc-host>` with your OpenClaw host, `$QUAID_HOME` with your actual Quaid home path, and instance names with your configured values.

End-to-end validation of the projects system on `<oc-host>`, where OC and CC share the same `QUAID_HOME`.

**All commands run on `<oc-host>`** — prefix with `ssh <oc-host>` or open a shell there.

```bash
# OC environment
OC_ENV="QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw"

# CC environment
CC_ENV="QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code"

# Quaid binary (same for both — same extension, different QUAID_INSTANCE)
QUAID=~/.local/bin/quaid
```

Projects are shared at `$QUAID_HOME/projects/` — both adapters read and write the same directory.
The global registry lives at `$QUAID_HOME/projects/project-registry.json` — shared by both adapters.

Run order:
1. OC CRUD
2. CC CRUD
3. Cross-platform

---

## OC CRUD

### OC-P1: Create project

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry create-project oc-test-proj --label 'OC Test Project'"
```

**Expected:** `Created project 'oc-test-proj' at .../projects/oc-test-proj`

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry list | grep oc-test-proj"
```

---

### OC-P2: Register a doc

```bash
ssh <oc-host> "echo '# OC Doc — test content about memory extraction' > /tmp/oc-test-doc.md && \
  QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry register /tmp/oc-test-doc.md --project oc-test-proj --description 'OC test doc'"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid docs list --project oc-test-proj"
```

---

### OC-P3: Search project docs

Trigger indexing first (embeddings run on `<oc-host>` via Ollama):
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid janitor --task rag --apply --approve"
```

Then search:
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid recall 'memory extraction' '{\"stores\":[\"docs\"],\"project\":\"oc-test-proj\"}'"
```

**Expected:** At least one hit; `/tmp/oc-test-doc.md` near the top.

---

### OC-P4: Project show

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid project show oc-test-proj"
```

**Expected:** JSON with `canonical_path`, `instances: ["openclaw"]`, `created_at`.

---

### OC-P5: Janitor check

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid janitor --task rag --dry-run 2>&1 | grep -E 'oc-test-proj|orphan|ERROR'"
```

**Expected:** No orphan warnings or errors.

---

### OC-P6: Markdown sanity check

```bash
ssh <oc-host> "head -5 $QUAID_HOME/projects/oc-test-proj/PROJECT.md && \
  file $QUAID_HOME/projects/oc-test-proj/PROJECT.md && \
  ls $QUAID_HOME/projects/oc-test-proj/"
```

**Expected:**
- First line is `# Project: OC Test Project`
- `file` reports UTF-8 text
- `docs/` subdirectory present

---

### OC-P7: Delete project

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid project delete oc-test-proj"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry list | grep oc-test-proj"   # empty
```

**Expected:** Project gone from registry list and `projects/oc-test-proj/` removed.

---

## CC CRUD

CC uses the same `QUAID_HOME` as OC but `QUAID_INSTANCE=claude-code`. The `claude-code/` instance dir is created on first use.

### CC-P1: Create project

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid registry create-project cc-test-proj --label 'CC Test Project'"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid registry list | grep cc-test-proj"
```

---

### CC-P2: Register a doc

```bash
ssh <oc-host> "echo '# CC Doc — test content about session extraction' > /tmp/cc-test-doc.md && \
  QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid registry register /tmp/cc-test-doc.md --project cc-test-proj --description 'CC test doc'"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid docs list --project cc-test-proj"
```

---

### CC-P3: Search project docs

Trigger indexing (same Ollama instance as OC — `<oc-host>`):
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid janitor --task rag --apply --approve"
```

Then search:
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid recall 'session extraction' '{\"stores\":[\"docs\"],\"project\":\"cc-test-proj\"}'"
```

**Expected:** At least one hit; `/tmp/cc-test-doc.md` near the top.

---

### CC-P4: Project show

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid project show cc-test-proj"
```

**Expected:** JSON with `instances: ["claude-code"]`.

---

### CC-P5: Janitor check

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid janitor --task rag --dry-run 2>&1 | grep -E 'cc-test-proj|orphan|ERROR'"
```

**Expected:** No orphan warnings or errors.

---

### CC-P6: Markdown sanity check

```bash
ssh <oc-host> "head -5 $QUAID_HOME/projects/cc-test-proj/PROJECT.md && \
  file $QUAID_HOME/projects/cc-test-proj/PROJECT.md && \
  ls $QUAID_HOME/projects/cc-test-proj/"
```

**Expected:** `# Project: CC Test Project`, UTF-8, `docs/` present.

---

### CC-P7: Delete project

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid project delete cc-test-proj"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid registry list | grep cc-test-proj"   # empty
```

---

## Cross-Platform

Both OC and CC share `QUAID_HOME=$QUAID_HOME`, so the global registry is truly shared — `project-registry.json` is the same file for both. These steps verify that cross-instance project ownership and doc sharing work correctly.

---

### XP-1: Global registry visible to both adapters

```bash
# From OC
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid global-registry list"

# From CC — same output, same file
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid global-registry list"
```

**Expected:** Both commands show the same project list. No divergence.

---

### XP-2: OC creates a project and a doc

```bash
# Create project
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry create-project shared-xp-proj --label 'Cross-Platform Test'"

# Write and register a doc
ssh <oc-host> "echo '# OC contribution — memory architecture overview' > /tmp/oc-xp-doc.md && \
  QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry register /tmp/oc-xp-doc.md --project shared-xp-proj --description 'OC arch doc'"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid docs list --project shared-xp-proj"
```

---

### XP-3: CC sees the project

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid global-registry list | grep shared-xp-proj"
```

**Expected:** `shared-xp-proj` visible with `instances: openclaw`.

---

### XP-4: CC links to the project and adds a doc

```bash
# Link CC instance to the project
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid project link shared-xp-proj"

# Write and register a CC doc to the shared project
ssh <oc-host> "echo '# CC contribution — session extraction design notes' > /tmp/cc-xp-doc.md && \
  QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid registry register /tmp/cc-xp-doc.md --project shared-xp-proj --description 'CC session doc'"
```

**Verify:**
```bash
# Project now shows both instances
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid project show shared-xp-proj"

# Both docs visible
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid docs list --project shared-xp-proj"
```

**Expected:** `instances: ["openclaw", "claude-code"]`; both docs listed.

---

### XP-5: CC can see and search the OC doc

```bash
# Index all docs (Ollama on <oc-host> handles both OC and CC docs)
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid janitor --task rag --apply --approve"

# CC searches for OC's doc
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=claude-code \
  ~/.local/bin/quaid recall 'memory architecture' '{\"stores\":[\"docs\"],\"project\":\"shared-xp-proj\"}'"
```

**Expected:** OC doc (`/tmp/oc-xp-doc.md`) appears in CC's search results.

---

### XP-6: OC can see and search the CC doc

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid recall 'session extraction' '{\"stores\":[\"docs\"],\"project\":\"shared-xp-proj\"}'"
```

**Expected:** CC doc (`/tmp/cc-xp-doc.md`) appears in OC's search results.

---

### XP-7: Janitor check (cross-instance)

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid janitor --task rag --dry-run 2>&1 | grep -E 'shared-xp-proj|orphan|ERROR'"
```

**Expected:** No errors or orphan warnings.

---

### XP-8: Markdown sanity check (shared project)

```bash
ssh <oc-host> "head -5 $QUAID_HOME/projects/shared-xp-proj/PROJECT.md && \
  file $QUAID_HOME/projects/shared-xp-proj/PROJECT.md && \
  ls $QUAID_HOME/projects/shared-xp-proj/"
```

**Expected:** `# Project: Cross-Platform Test`, UTF-8, `docs/` present.

---

### XP-9: Cleanup

```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid project delete shared-xp-proj"
```

**Verify:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid registry list | grep shared-xp-proj"   # empty
```

---

## Pass Criteria

| Check | Pass condition |
|-------|---------------|
| OC CRUD | P1–P7 all complete without errors |
| OC janitor | P5 — no orphans |
| OC markdown | P6 — `#` heading, UTF-8, `docs/` exists |
| CC CRUD | P1–P7 all complete without errors |
| CC janitor | P5 — no orphans |
| CC markdown | P6 — `#` heading, UTF-8, `docs/` exists |
| Global registry | XP-1 — both adapters see same list |
| OC doc created | XP-2 — doc registered to shared project |
| CC sees OC project | XP-3 — global registry shows it |
| CC links + makes doc | XP-4 — project shows both instances; both docs listed |
| CC sees OC doc | XP-5 — search returns OC doc |
| OC sees CC doc | XP-6 — search returns CC doc |
| Cross-instance janitor | XP-7 — no orphans |
| Cleanup | XP-9 — project gone from global registry |

---

## Notes

- **Shared QUAID_HOME**: Both OC and CC use `QUAID_HOME=$QUAID_HOME` on `<oc-host>`. The global registry and shared project files are the same for both adapters. This is a requirement for cross-platform tests to work — separate QUAID_HOME paths mean separate registries.
- **Projects location**: `$QUAID_HOME/projects/` — shared across both adapters.
- **CC instance dir**: `$QUAID_HOME/claude-code/` — created on first use.
- **RAG indexing**: Both OC and CC use the same Ollama instance on `<oc-host>`. Indexing from either adapter writes chunks that are visible to both (same `memory.db`).
- **`project link` command**: Adds the current `QUAID_INSTANCE` to the project's `instances` list in `project-registry.json` without changing ownership or moving files.
- **M3/M4 on CC**: CC has no `/compact` or `/new` commands — not part of this protocol. CC extraction uses `PreCompact` hook and `SessionEnd` hook.

---

## Single-Adapter Smoke Tests (original)

### Test 1: List docs by project

**What it tests:** `docs_list` tool with project filter

**Say to the agent:**
> "List all docs in the quaid project"

**Expected:** Agent uses `docs_list` with `project: "quaid"` and returns docs from `projects/quaid/`.

---

### Test 2: Project-filtered search

**What it tests:** `projects_search` with `project` parameter

**Say to the agent:**
> "Search for 'deduplication' only in the quaid project"

**Expected:** Agent uses `projects_search` with `query: "deduplication"` and `project: "quaid"`. Results include `projects/quaid/reference/memory-deduplication-system.md`.

---

### Test 3: Read a doc by path

**What it tests:** `docs_read` tool

**Say to the agent:**
> "Read projects/quaid/reference/memory-schema.md"

**Expected:** Agent uses `docs_read` and returns doc content.

---

### Test 4: Create a new project

**What it tests:** `project_create` tool + directory scaffolding

**Say to the agent:**
> "Create a new project called 'test-essay' with label 'Test Essay'"

**Expected:** `projects/test-essay/PROJECT.md` is created.

**Cleanup:**
```bash
ssh <oc-host> "QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  ~/.local/bin/quaid project delete test-essay"
```

---

### Test 5: Register an existing doc to a project

**What it tests:** `docs_register` tool

**Say to the agent:**
> "Register projects/quaid/reference/memory-system-design.md under the quaid project with description 'Design reference'"

**Expected:** Agent uses `docs_register`; doc appears in `docs_list` for `quaid`.

---

### Test 6: Compact triggers project event

**What it tests:** Event emission on `/compact`

**Then say:**
> /compact

**Expected:**
1. Agent indicates project/doc processing in background.
2. Event staging path reflects processing state:
```bash
ls -la ~/quaid/projects/staging/
```

---

### Test 7: Staleness detection

**What it tests:** source/doc drift surfaced through projects search

**Setup:**
```bash
ssh <oc-host> "touch $QUAID_HOME/claudecode/modules/quaid/core/lifecycle/janitor.py"
```

**Then say to the agent:**
> "Search docs for janitor pipeline"

**Expected:** A staleness/drift warning for `projects/quaid/reference/janitor-reference.md`.

**Reset:**
```bash
ssh <oc-host> "cd $QUAID_HOME/claudecode/modules/quaid && git checkout core/lifecycle/janitor.py"
```

---

### Test 8: Project ownership resolution

**What it tests:** path → project mapping

**Say to the agent:**
> "Which project owns modules/quaid/core/lifecycle/janitor.py?"

**Expected:** `quaid`.

---

### Quick CLI Verification

```bash
ssh <oc-host> "cd $QUAID_HOME/claudecode/modules/quaid && \
  QUAID_HOME=$QUAID_HOME QUAID_INSTANCE=openclaw \
  python3 datastore/docsdb/registry.py list --project quaid"
```
