# Quaid — Tool Usage Guide

Quaid is an active knowledge layer. Use the `quaid` CLI via your Bash tool — no tool registration needed.

**Environment:** `QUAID_HOME` and `QUAID_INSTANCE` are baked into hooks at install time. If calling `quaid` from a shell outside of a hook, ensure both are set.

**For full project docs, architecture, and reference index:** every tracked project has its own `PROJECT.md` at `QUAID_HOME/projects/<project-name>/PROJECT.md`. Read the relevant project's `PROJECT.md` first. If you do not know the project name yet, docs recall/search will try to infer it and surface the best matching `PROJECT.md`.

---

## Memory

```bash
quaid recall "query"                    # default stores: vector + graph
quaid recall "query" '{"stores": ["vector", "graph", "docs"]}'
quaid recall "query" '{"stores": ["docs"], "project": "quaid"}'  # docs only
quaid store "text"                      # manual memory insertion
quaid get-node <id>
quaid get-edges <id>
quaid delete-node <id>
quaid stats
```

**recall config JSON** (all fields optional):
```json
{
  "stores": ["vector", "graph", "docs"],
  "limit": 5,
  "domain_filter": {"technical": true},
  "domain_boost": ["technical", "project"],
  "project": "quaid",
  "fast": false,
  "date_from": "YYYY-MM-DD",
  "date_to": "YYYY-MM-DD"
}
```

**Stores:**
- `vector` — semantic + FTS hybrid search across all memories (domain-filtered by `domain_filter`/`domain_boost`)
- `graph` — graph-aware recall with edge traversal (expands via relationship edges)
- `docs` — project docs RAG; returns chunks plus the relevant `PROJECT.md` when a project is set or confidently inferred

**`domain_filter` vs `domain_boost`:** Default to `domain_boost` (soft preference). Use `domain_filter` only when you must exclude other domains entirely.

**Output flags:** `--json` (machine-readable), `--debug` (scoring breakdown)

---

## Domains

<!-- AUTO-GENERATED:DOMAIN-LIST:START -->
Available domains (from datastore `domain_registry` active rows):
- `finance`: budgeting, purchases, salary, bills
- `health`: training, injuries, routines, wellness
- `household`: home, chores, food planning, shared logistics
- `legal`: contracts, policy, and regulatory constraints
- `personal`: identity, preferences, relationships, life events
- `project`: project status, tasks, files, milestones
- `research`: options considered, comparisons, tradeoff analysis
- `schedule`: dates, appointments, deadlines
- `technical`: code, infra, APIs, architecture
- `travel`: trips, moves, places, logistics
- `work`: job/team/process decisions not deeply technical
<!-- AUTO-GENERATED:DOMAIN-LIST:END -->

```bash
quaid domain list
quaid domain register <name> "description"
```

---

## Project Docs

```bash
quaid recall "query" '{"stores": ["docs"]}'                     # semantic RAG search across project docs
quaid recall "query" '{"stores": ["docs"], "project": "<name>"}' # scoped to one project
quaid docs list [--project <name>]
quaid docs check                              # check for stale docs
quaid docs update --apply                     # update stale docs from source diffs
quaid registry register <path> --project <name>  # link external file into project
quaid registry list [--project <name>]
```

- For an actively worked-on project, read its `PROJECT.md` first. Use docs recall/search when you need deeper detail or do not yet know which project matches the task.
- `PROJECT.md` should be the overview and navigation map. Registry/project commands are the exact-truth backstop when you need to confirm current tracked files or ownership.

---

## Projects

```bash
quaid project list
quaid project create <name> [--description "..."] [--source-root /path]
quaid project show <name>
quaid project update <name> [--description "..."] [--source-root /path]  # update existing project fields
quaid project link <name>     # add current instance to existing project (idempotent)
quaid project unlink <name>   # remove current instance (does not delete project)
quaid project delete <name>   # destructive — removes dir + all SQLite rows
quaid project snapshot [<name>]
quaid project sync
quaid global-registry list    # cross-instance project list
```

**File placement:**
- In-project files → `QUAID_HOME/projects/<name>/`
- Project docs → `QUAID_HOME/projects/<name>/docs/`
- External code files → link with `quaid registry register <path> --project <name>`
- Ephemeral/drafts/quick work → project `misc--$QUAID_INSTANCE` at `$QUAID_HOME/projects/misc--$QUAID_INSTANCE/` (pre-created at install; always tell the user when writing here)

---

## Sessions

```bash
quaid session list [--limit 5]
quaid session load --session-id <id>

# Load last session — truncated to last ~20k tokens by default.
# Always check size first; only load if it makes sense to load.
quaid session last --size-only                  # token count only, no content loaded
quaid session last                              # load last session, last 20k tokens
quaid session last --max-tokens 50000           # load last session, last 50k tokens
quaid session last --max-tokens 0               # load full transcript (use with caution)
```

**Token size note:** `last` returns `total_tokens_estimated` (full session size) and
`loaded_tokens_estimated` (what was actually returned). If `truncated: true`, the
response includes a `truncation_note` explaining how to load more. Default cap is
20k tokens to avoid flooding context — override only when older context is needed.

---

## Maintenance

```bash
quaid janitor --task all --dry-run
quaid janitor --task all --apply              # add --approve when applyMode=ask
quaid doctor
quaid updater doc-health <project> [--dry-run]
```

---

## Config & Instances

```bash
quaid config show
quaid config edit [--shared | --instance <id>]
quaid config set <dotted.key> <value> [--shared]
quaid instances list [--json]
```

**Cross-instance search:** Override `QUAID_INSTANCE` at call time to read another instance's memory (both instances must share `QUAID_HOME`):
```bash
QUAID_INSTANCE=openclaw quaid recall "query"   # search openclaw's memory from CC context
```

---

## Retrieval Policy

- Treat auto-injected memory as hints — verify concrete claims (names, dates, versions) with explicit `recall`.
- Only facts stated explicitly in assistant messages are reliably retained as memory. Do not assume raw tool output or private reasoning will be preserved.
- Project file writes may be tracked from actual filesystem changes, but if a tool result or your reasoning yields a durable fact, decision, status update, or outcome worth remembering, state it clearly in your reply.
- For codebase/architecture questions, include `"docs"` in stores: `recall "query" '{"stores":["docs"]}'`.
- Do not upgrade a planned, offered, interviewing, or job-searching state into a completed current state unless the retrieved evidence explicitly says the change already happened.
- For questions about what the agent or assistant found, suggested, or recommended, answer the suggestion itself rather than the currently implemented feature.

## Quick Playbooks

**Personal/relationship question:** `recall "query"` → if the first pass feels adjacent rather than decisive, run one narrower follow-up `recall`

**Technical/project question:** read the relevant `PROJECT.md` or run `recall "query" '{"stores":["docs"]}'` → if the answer depends on implementation, schema, API shape, tests, or UI details, use docs recall so it can bring back the project's `PROJECT.md` and deeper docs together

**Memory + docs in one pass:** `recall "query" '{"stores": ["vector","graph","docs"]}'`

**Missing session context:** `session last --size-only` to check size → `session last` to load (truncated to 20k tokens); use `--max-tokens 0` only if older context is specifically needed

**Conflicting facts:** prefer newest; if unresolved, surface uncertainty and suggest janitor review
