# Projects System — Alfie Test Plan

Tests to verify the projects system works end-to-end via Alfie.
Run these in a Telegram conversation with Alfie.

---

## Test 1: List docs by project

**What it tests:** `docs_list` tool with project filter

**Say to Alfie:**
> "List all my docs in the quaid project"

**Expected:** Alfie uses `docs_list` with `project: "quaid"` and shows ~23 docs including janitor-reference.md, memory-schema.md, decisions/001-005, etc.

**Then say:**
> "Now list the spark-agents project docs"

**Expected:** ~7 docs including spark-planning.md, spark-agents.md, hardware upgrade plan.

---

## Test 2: Project-filtered search

**What it tests:** `docs_search` with `project` parameter

**Say to Alfie:**
> "Search for 'deduplication' but only in the quaid project"

**Expected:** Alfie uses `docs_search` with `query: "deduplication"` and `project: "quaid"`. Results should include memory-deduplication-system.md and possibly janitor-reference.md. Should NOT include any spark/infrastructure/integrations docs.

**Then say:**
> "Search for 'voice' in the integrations project"

**Expected:** Results from voice-calls.md and tts-voices.md only.

---

## Test 3: Read a doc by path

**What it tests:** `docs_read` tool

**Say to Alfie:**
> "Read the spark planning document for me"

**Expected:** Alfie uses `docs_read` with `identifier: "projects/spark-agents/spark-planning.md"` (or finds it by title) and returns the full content.

---

## Test 4: Create a new project

**What it tests:** `project_create` tool + directory scaffolding

**Say to Alfie:**
> "Create a new project called 'test-essay' with the label 'Test Essay' and description 'A test essay project'"

**Expected:** Alfie uses `project_create`. A new directory `projects/test-essay/` should be created with a `PROJECT.md` template inside it.

**Verify afterward (CLI):**
```bash
cat projects/test-essay/PROJECT.md
```

**Cleanup:** `rm -rf projects/test-essay/`

---

## Test 5: Register a doc to a project

**What it tests:** `docs_register` tool

**Say to Alfie:**
> "Register projects/infrastructure/clawdbot-wishlist.md under the quaid project with description 'Feature wishlist'"

**Expected:** Alfie uses `docs_register`. The doc should now appear when you list quaid docs.

**Verify:**
> "List quaid docs — is clawdbot-wishlist.md there?"

**Cleanup (CLI):** The doc was already registered under infrastructure, so this will move it. To restore:
```bash
python3 docs_registry.py register projects/infrastructure/clawdbot-wishlist.md --project infrastructure --description "Feature wishlist and priorities"
```

---

## Test 6: Compact triggers project event

**What it tests:** Event emission on `/compact`

**Setup:** Have a conversation with Alfie about the knowledge layer — discuss something specific like "I think we should add a decay visualization to the dashboard."

**Then say:**
> /compact

**Expected:**
1. Alfie should mention updating project docs in background
2. Check for event file:
```bash
ls -la projects/staging/
```
3. If a background processor ran, the event file should be gone (processed). Check the log:
```bash
# Look for project_updater output
ls -la projects/staging/failed/ 2>/dev/null  # Should be empty/nonexistent
```

---

## Test 7: Staleness detection

**What it tests:** Mtime-based staleness check via docs_search

**Setup (CLI):**
```bash
# Touch a source file to make it newer than its tracked doc
touch plugins/quaid/janitor.py
```

**Then say to Alfie:**
> "Search docs for 'janitor pipeline'"

**Expected:** Results should include a STALENESS WARNING mentioning janitor-reference.md is behind janitor.py.

**Reset (CLI):**
```bash
# Restore original mtime
git checkout plugins/quaid/janitor.py
```

---

## Test 8: Cross-project awareness

**What it tests:** Path resolution across projects

**Say to Alfie:**
> "Which project does plugins/quaid/janitor.py belong to?"

**Expected:** Alfie should be able to figure out it's the quaid project (via sourceRoots). This tests whether Alfie can use the project tools to answer project-ownership questions.

> "What about projects/spark-agents/spark-planning.md?"

**Expected:** spark-agents project.

---

## Test 9: Auto-discover new files

**What it tests:** Janitor Task 7 auto-discovery

**Setup (CLI):**
```bash
# Create a new file in a project directory
echo "# Research Notes" > projects/spark-agents/research-notes.md
```

**Run janitor task:**
```bash
cd plugins/quaid && python3 janitor.py --task rag --dry-run
```

**Expected:** Output should mention discovering `projects/spark-agents/research-notes.md`.

**Cleanup:**
```bash
rm projects/spark-agents/research-notes.md
```

---

## Test 10: End-to-end project workflow

**What it tests:** Full workflow — create project, add docs, search, compact

**Say to Alfie:**
1. "Create a project called 'weekend-plans' with label 'Weekend Plans'"
2. "Register a new doc at projects/weekend-plans/ideas.md"
3. Create the file manually or ask Alfie to write some ideas
4. "Search weekend-plans for 'ideas'"
5. Have a conversation about weekend plans
6. `/compact`

**Expected:** Each step should work. After compact, a project event should be emitted for weekend-plans (if Alfie correctly identifies the project from the conversation).

**Cleanup:**
```bash
rm -rf projects/weekend-plans/
python3 docs_registry.py unregister projects/weekend-plans/ideas.md
python3 docs_registry.py unregister projects/weekend-plans/PROJECT.md
```

---

## Quick CLI Verification (no Alfie needed)

Run these to verify the system state is correct:

```bash
cd ~/clawd/plugins/quaid

# All projects visible
python3 docs_registry.py list --project quaid
python3 docs_registry.py list --project spark-agents
python3 docs_registry.py list --project infrastructure
python3 docs_registry.py list --project integrations

# Path resolution
python3 docs_registry.py find-project plugins/quaid/janitor.py     # → quaid
python3 docs_registry.py find-project projects/spark-agents/spark-planning.md      # → spark-agents
python3 docs_registry.py find-project projects/infrastructure/ollama-setup.md     # → infrastructure
python3 docs_registry.py find-project projects/integrations/voice-calls.md        # → integrations
python3 docs_registry.py find-project projects/infrastructure/01-git-versioning.md  # → infrastructure

# Source mappings (for staleness checks)
python3 docs_registry.py source-mappings --project quaid

# Tests
python3 -m pytest tests/ -v
```
