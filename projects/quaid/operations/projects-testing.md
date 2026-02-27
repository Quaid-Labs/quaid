# Projects System - Quaid Test Plan

Tests to verify the projects system works end-to-end using the real `quaid` project docs in this repo.

---

## Test 1: List docs by project

**What it tests:** `docs_list` tool with project filter

**Say to the agent:**
> "List all docs in the quaid project"

**Expected:** Agent uses `docs_list` with `project: "quaid"` and returns docs from `projects/quaid/`.

---

## Test 2: Project-filtered search

**What it tests:** `projects_search` with `project` parameter

**Say to the agent:**
> "Search for 'deduplication' only in the quaid project"

**Expected:** Agent uses `projects_search` with `query: "deduplication"` and `project: "quaid"`. Results include `projects/quaid/reference/memory-deduplication-system.md`.

---

## Test 3: Read a doc by path

**What it tests:** `docs_read` tool

**Say to the agent:**
> "Read projects/quaid/reference/memory-schema.md"

**Expected:** Agent uses `docs_read` and returns doc content.

---

## Test 4: Create a new project

**What it tests:** `project_create` tool + directory scaffolding

**Say to the agent:**
> "Create a new project called 'test-essay' with label 'Test Essay'"

**Expected:** `projects/test-essay/PROJECT.md` is created.

**Verify afterward (CLI):**
```bash
cat projects/test-essay/PROJECT.md
```

**Cleanup:**
```bash
rm -rf projects/test-essay/
```

---

## Test 5: Register an existing doc to a project

**What it tests:** `docs_register` tool

**Say to the agent:**
> "Register projects/quaid/reference/memory-system-design.md under the quaid project with description 'Design reference'"

**Expected:** Agent uses `docs_register`; doc appears in `docs_list` for `quaid`.

---

## Test 6: Compact triggers project event

**What it tests:** Event emission on `/compact`

**Then say:**
> /compact

**Expected:**
1. Agent indicates project/doc processing in background.
2. Event staging path reflects processing state:
```bash
ls -la projects/staging/
ls -la projects/staging/failed/ 2>/dev/null
```

---

## Test 7: Staleness detection

**What it tests:** source/doc drift surfaced through projects search

**Setup (CLI):**
```bash
touch modules/quaid/core/lifecycle/janitor.py
```

**Then say to the agent:**
> "Search docs for janitor pipeline"

**Expected:** A staleness/drift warning for `projects/quaid/reference/janitor-reference.md`.

**Reset:**
```bash
git checkout modules/quaid/core/lifecycle/janitor.py
```

---

## Test 8: Project ownership resolution

**What it tests:** path -> project mapping

**Say to the agent:**
> "Which project owns modules/quaid/core/lifecycle/janitor.py?"

**Expected:** `quaid`.

---

## Test 9: Auto-discover new files

**What it tests:** janitor `rag` task auto-discovery

**Setup (CLI):**
```bash
echo "# Research Notes" > projects/quaid/reference/research-notes.md
```

**Run janitor task:**
```bash
cd modules/quaid && python3 core/lifecycle/janitor.py --task rag --dry-run
```

**Expected:** output mentions discovering/processing `projects/quaid/reference/research-notes.md`.

**Cleanup:**
```bash
rm projects/quaid/reference/research-notes.md
```

---

## Test 10: End-to-end workflow

**What it tests:** create project, add docs, search, compact

**Steps:**
1. Create `weekend-plans` project.
2. Register `projects/weekend-plans/ideas.md`.
3. Search project docs for `ideas`.
4. Trigger `/compact`.

**Expected:** each operation succeeds and project events process cleanly.

---

## Quick CLI Verification

```bash
cd ~/quaid/dev/modules/quaid

python3 datastore/docsdb/registry.py list --project quaid
python3 datastore/docsdb/registry.py find-project modules/quaid/core/lifecycle/janitor.py
python3 datastore/docsdb/registry.py source-mappings --project quaid

python3 -m pytest tests/ -v
```
