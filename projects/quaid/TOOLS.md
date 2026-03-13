# Quaid — Tool Usage Guide

Quaid is an active knowledge layer. Use the `quaid` CLI via your Bash tool — no tool registration needed.

**Environment:** `QUAID_HOME` and `QUAID_INSTANCE` are baked into hooks at install time. If calling `quaid` from a shell outside of a hook, ensure both are set.

**For full project docs, architecture, and reference index:** read the Quaid project's `PROJECT.md` — found at `$QUAID_HOME/shared/projects/quaid/PROJECT.md` or via `quaid docs search "topic" --project quaid`.

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
- `docs` — project docs RAG; returns chunks + PROJECT.md when `project` is set

**`domain_filter` vs `domain_boost`:** Default to `domain_boost` (soft preference). Use `domain_filter` only when you must exclude other domains entirely.

**Output flags:** `--json` (machine-readable), `--debug` (scoring breakdown)

**store categories:** `preference`, `fact`, `decision`, `entity`, `other`

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
quaid docs search "query"                     # semantic RAG search across project docs
quaid docs search "query" --project <name>    # scoped to one project
quaid docs list [--project <name>]
quaid docs check                              # check for stale docs
quaid docs update --apply                     # update stale docs from source diffs
quaid registry register <path> --project <name>  # link external file into project
quaid registry list [--project <name>]
```

---

## Projects

```bash
quaid project list
quaid project create <name> [--description "..."] [--source-root /path]
quaid project show <name>
quaid project link <name>     # add current instance to existing project (idempotent)
quaid project unlink <name>   # remove current instance (does not delete project)
quaid project delete <name>   # destructive — removes dir + all SQLite rows
quaid project snapshot [<name>]
quaid project sync
quaid global-registry list    # cross-instance project list
```

**File placement:**
- In-project files → `QUAID_HOME/<instance>/projects/<name>/`
- Project docs → `QUAID_HOME/<instance>/projects/<name>/docs/`
- External code files → link with `quaid registry register <path> --project <name>`
- Temp/scratch → `temp/` or `scratch/` (tell user these are untracked)

---

## Sessions

```bash
quaid session list [--limit 5]
quaid session load --session-id <id>
```

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
- For codebase/architecture questions, include `"docs"` in stores or use `quaid docs search`.
- Use `domain_boost` in config before broadening to full recall.

## Quick Playbooks

**Personal/relationship question:** `recall "query"` → if low confidence, add `'{"domain_boost": ["personal"]}'`

**Technical/project question:** `recall "query" '{"domain_boost": ["technical","project"]}'` → if still unclear, add `"docs"` to stores

**Memory + docs in one pass:** `recall "query" '{"stores": ["vector","graph","docs"]}'`

**Missing session context:** `session list --limit 1` → `session load --session-id <id>`

**Conflicting facts:** prefer newest; if unresolved, surface uncertainty and suggest janitor review
