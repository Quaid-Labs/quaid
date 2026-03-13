# Quaid — Tool Usage Guide

Quaid is an active knowledge layer. Use the `quaid` CLI via your Bash tool — no tool registration needed.

**Environment:** `QUAID_HOME` and `QUAID_INSTANCE` are baked into hooks at install time. If calling `quaid` from a shell outside of a hook, ensure both are set.

**For full project docs, architecture, and reference index:** read the Quaid project's `PROJECT.md` — found at `$QUAID_HOME/shared/projects/quaid/PROJECT.md` or via `quaid docs search "topic" --project quaid`.

---

## Memory

```bash
quaid recall "query"          # semantic + graph + reranking (use for facts, relationships, timelines)
quaid search "query"          # fast search, no reranking (use for quick lookups)
quaid store "text"            # manual memory insertion (prefer extraction over manual store)
quaid get-node <id>
quaid get-edges <id>
quaid delete-node <id>
quaid stats
```

**Key flags** (`recall` and `search`):
- `--limit N` — result count (default 5)
- `--project <name>` — scope to project
- `--date-from / --date-to YYYY-MM-DD`
- `--domain-boost '["technical","project"]'` — multiplier on matching domains before ranking; use for broad recall when you want to prefer certain topics without excluding others. List form uses 1.3× each; map form sets explicit multipliers: `'{"technical": 1.5}'`
- `--domain-filter '{"technical": true}'` — hard filter: only returns memories tagged with true domains. Use only when you are certain other domains should be excluded entirely.
- `--json` / `--debug`

**When to use boost vs filter:** Default to `--domain-boost`. Only use `--domain-filter` when the question is definitively scoped and cross-domain noise would be wrong (e.g. "show me only finance memories").

**store categories:** `preference`, `fact`, `decision`, `entity`, `other`

---

## Domains

<!-- Manually maintained — update when quaid domain register adds new domains -->
| Domain | Covers |
|--------|--------|
| `finance` | budgeting, purchases, salary, bills |
| `health` | training, injuries, routines, wellness |
| `household` | home, chores, food planning, shared logistics |
| `legal` | contracts, policy, regulatory constraints |
| `personal` | identity, preferences, relationships, life events |
| `project` | project status, tasks, files, milestones |
| `research` | options considered, comparisons, tradeoff analysis |
| `schedule` | dates, appointments, deadlines |
| `technical` | code, infra, APIs, architecture |
| `travel` | trips, moves, places, logistics |
| `work` | job/team/process decisions not deeply technical |
<!-- end domain list -->

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
quaid hook-search "query"                     # unified memory + docs search in one pass
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

---

## Retrieval Policy

- Treat auto-injected memory as hints — verify concrete claims (names, dates, versions) with explicit `recall`.
- Prefer `docs search` for codebase/architecture questions.
- Use `hook-search` when you want a single pass across both memories and docs.
- Use `--domain-boost` before broadening to full recall.

## Quick Playbooks

**Personal/relationship question:** `recall` → if low confidence, add `--domain-boost '["personal"]'`

**Technical/project question:** `recall --domain-boost '["technical","project"]'` → if still unclear, `docs search --project <name>`

**Missing session context:** `session list --limit 1` → `session load --session-id <id>`

**Conflicting facts:** prefer newest; if unresolved, surface uncertainty and suggest janitor review
