# Projects CLI Reference

All commands use the `quaid` binary. Set `QUAID_HOME` and `QUAID_INSTANCE` before running.

```bash
export QUAID_HOME=/Users/owner/quaid
export QUAID_INSTANCE=openclaw   # or claude-code
```

---

## Project Registry (`quaid project`)

### List projects
```bash
quaid project list
quaid project list --json
```

### Create a project
```bash
quaid registry create-project <name> --label "Display Name"
```
Creates `QUAID_HOME/shared/projects/<name>/` with `PROJECT.md` and `docs/` subdir.
Also registers the project in `QUAID_HOME/project-registry.json` and the SQLite `project_definitions` table.

### Show a project
```bash
quaid project show <name>
```
Prints JSON with `canonical_path`, `instances`, `created_at`, `description`.

### Link current instance to an existing project
```bash
quaid project link <name>
```
Adds the current `QUAID_INSTANCE` to the project's `instances` list. Idempotent.
Use when a second adapter wants to participate in a project created by another adapter.

### Unlink current instance from a project
```bash
quaid project unlink <name>
```
Removes the current `QUAID_INSTANCE` from the project's `instances` list. Idempotent.
Does not delete the project or its files.

### Delete a project
```bash
quaid project delete <name>
```
- Removes from `project-registry.json`
- Removes `project_definitions` row from `memory.db`
- Removes all `doc_registry` rows for this project from `memory.db`
- Removes canonical project directory (`shared/projects/<name>/`)
- Destroys shadow git tracking if configured
- Does NOT touch the user's `source_root` directory

---

## Doc Registry (`quaid registry` / `quaid docs`)

### List registered docs
```bash
quaid registry list
quaid docs list --project <name>
```

### Register a doc to a project
```bash
quaid registry register <file_path> --project <name> --description "..."
```
Accepts absolute paths or paths relative to the workspace root.

### Search project docs (semantic RAG)
```bash
quaid docs search "query" --project <name>
quaid docs search "query"                   # search all projects
```
Requires embeddings (Ollama on example.local). Returns ranked chunks with similarity scores.

### Check for stale docs
```bash
quaid docs check
```

### Update stale docs
```bash
quaid docs update --apply
```

### Reindex all docs (force embeddings refresh)
```bash
cd <quaid_module_root> && python3 datastore/docsdb/rag.py reindex --all
```
Scans `docs/` dir, top-level workspace `.md` files, and all `doc_registry` entries.

---

## Global Registry (`quaid global-registry`)

### List all projects (cross-instance view)
```bash
quaid global-registry list
```
Reads `QUAID_HOME/project-registry.json`. On example.local, both OC and CC share the same
file, so this shows the complete cross-adapter project list.

---

## Cross-Instance Workflow

```bash
# OC creates a project
QUAID_INSTANCE=openclaw quaid registry create-project my-proj --label "My Project"

# OC registers a doc
QUAID_INSTANCE=openclaw quaid registry register /path/to/doc.md --project my-proj

# CC sees it
QUAID_INSTANCE=claude-code quaid global-registry list | grep my-proj

# CC links to it
QUAID_INSTANCE=claude-code quaid project link my-proj

# CC registers its own doc
QUAID_INSTANCE=claude-code quaid registry register /path/to/cc-doc.md --project my-proj

# Both adapters can now search all docs
QUAID_INSTANCE=openclaw quaid docs search "query" --project my-proj    # sees CC doc too
QUAID_INSTANCE=claude-code quaid docs search "query" --project my-proj  # sees OC doc too

# CC leaves the project (without deleting it)
QUAID_INSTANCE=claude-code quaid project unlink my-proj

# OC deletes the project entirely (unlinks all, removes files and DB rows)
QUAID_INSTANCE=openclaw quaid project delete my-proj
```

---

## Notes

- **Shared projects directory**: `QUAID_HOME/shared/projects/` — both OC and CC read/write the same location on example.local.
- **Global registry**: `QUAID_HOME/project-registry.json` — shared file, same content for both adapters on the same machine.
- **SQLite tables**: `project_definitions` (project metadata) and `doc_registry` (registered files) both live in `QUAID_HOME/data/memory.db`.
- **RAG search**: Doc chunks are in `doc_chunks` in `memory.db`. Both adapters share the same DB on alfie, so indexing by one adapter makes docs searchable by both.
- **`project delete` is destructive** — it clears all instance linkages, removes the canonical directory, and purges all DB entries for the project.
