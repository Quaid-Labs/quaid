# Quaid — Tool Usage Guide

Quaid is an active knowledge layer. Use the Quaid CLI via your Bash tool — no tool registration needed. Prefer `quaid` when it is on `PATH`. If it is not, current installs use `$QUAID_HOME/modules/quaid/quaid`; older installs may still use `$QUAID_HOME/plugins/quaid/quaid`.

**Environment:** `QUAID_HOME` and `QUAID_INSTANCE` are baked into hooks at install time. If calling the CLI from a shell outside of a hook, ensure both are set.

**For full project docs, architecture, and reference index:** every tracked project has its own `PROJECT.md` at `QUAID_VISIBLE_HOME/projects/<project-name>/PROJECT.md`. Read the relevant project's `PROJECT.md` first. If you do not know the project name yet, docs recall/search will try to infer it and surface the best matching `PROJECT.md`.

---

## Memory

```bash
quaid recall "query"                    # default stores: vector + graph
quaid recall "query" '{"stores": ["vector", "graph", "docs"]}'
quaid recall "query" '{"stores": ["docs"], "project": "quaid"}'  # docs only
quaid recall "exact wording" '{"stores": ["session_chunks"]}'     # raw session chunks only
quaid recall "query" --include-chunks --max-chunk-tokens 256      # attach linked evidence chunks
quaid store "text"                      # manual memory insertion
quaid get-node <id>
quaid get-edges <id>
quaid delete <id>             # delete node by id
quaid stats
```

**recall config JSON** (all fields optional):
```json
{
  "stores": ["vector", "graph", "docs", "session_chunks"],
  "limit": 5,
  "domain_filter": {"technical": true},
  "domain_boost": ["technical", "project"],
  "project": "quaid",
  "fast": false,
  "date_from": "YYYY-MM-DD",
  "date_to": "YYYY-MM-DD",
  "temporal_dimension": "auto",
  "include_chunks": false,
  "max_chunk_tokens": 512,
  "max_total_chunk_tokens": 2048
}
```

**Stores:**
- `vector` — semantic + FTS hybrid search across all memories (domain-filtered by `domain_filter`/`domain_boost`)
- `graph` — graph-aware recall with edge traversal (expands via relationship edges)
- `docs` — project docs RAG; returns chunks plus the relevant `PROJECT.md` when a project is set or confidently inferred
- `session_chunks` — owner-scoped raw transcript chunks; use for exact wording or conversation evidence

**`domain_filter` vs `domain_boost`:** Default to `domain_boost` (soft preference). Use `domain_filter` only when you must exclude other domains entirely.

**Temporal filters:** `date_from`/`date_to` use `temporal_dimension`: `auto`, `occurred`, `mentioned`, or `record`.

**Evidence order model:**
- **1st-order** — original source/session text (`source_chunks`, `session_chunks`, `session_microchunks`)
- **2nd-order** — extracted compact memories (`nodes` Fact/Preference/Event rows)
- **3rd-order** — structured graph/cluster evidence (`edges`, relation summaries, graph fact clusters)
- **4th-order** — ephemeral model interpretation (query plans, reranker decisions, drill queries)

Prefer lower-order evidence when a question depends on exact wording, dates, or
co-reference. Higher-order evidence is still useful for relationship, list, and
summary questions, but it should preserve a path back to source evidence.

**Chunk evidence:** default recall omits explicit `source_chunk` payload dicts. Use `--include-chunks` or `include_chunks:true`; cap with `max_chunk_tokens` and `max_total_chunk_tokens`. Deliberate recall may still attach bounded first-order source/session text to a selected compact memory row before output sanitization, so the answerer can see supporting transcript context without exposing raw `source_chunk_id` by default. `session_chunks` results show chunk ids; expand with the session-chunk fetch surface.

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
quaid project list [--names-only]
quaid project create <name> [--description "..."] [--source-root /path]
quaid project show <name>
quaid project update <name> [--description "..."] [--source-root /path]  # update existing project fields
quaid project link <name>     # add current instance to existing project (idempotent)
quaid project unlink <name>   # remove current instance (does not delete project)
quaid project delete <name>   # destructive — removes dir + all SQLite rows
quaid project snapshot [<name>]
quaid project status <name>
quaid global-registry list    # cross-instance project list
```

**File placement:**
- Canonical project docs and Quaid-managed metadata → `QUAID_VISIBLE_HOME/projects/<name>/`
- Real source/code files may live outside Quaid home — keep them in their real working location and link them with `quaid registry register <path> --project <name>`
- Ephemeral/drafts/quick work → use project `misc--$QUAID_INSTANCE` as the owner, but prefer a real working path and register it immediately instead of treating `~/quaid/` as a dump directory

---

## Maintenance

```bash
quaid janitor --task all --dry-run
quaid janitor --task all --apply              # supervisor-owned across live instances; add --approve when applyMode=ask
quaid janitor --task all --apply --instance <name>  # supervisor-owned single-instance maintenance
quaid doctor
quaid supervisor status                       # inspect root runtime supervisor
quaid supervisor ensure                       # start runtime supervisor if needed
quaid supervisor stop                         # stop supervisor process group
quaid docs update <project>                   # queue supervisor-owned project docs refresh
quaid project status <project>                # inspect freshness, worker, and cursor state
quaid project diff <project> [--full]         # inspect pending source/log delta
quaid notify --deferred-status                # inspect buffered janitor/update notices
quaid notify --deferred-drain                 # fetch buffered notices when a human user is present
```

- Deferred notices are non-urgent system messages from janitor, update checks, and similar background work.
- **Do not ask permission to read deferred notices.** They are normal Quaid context, not a privileged action.
- **Only drain deferred notices when a human user is present.** Do not drain them during autonomous/background actions where no user is actively interacting.
- **When you detect pending deferred notices at the start of a human-facing session or task, drain them proactively** using `quaid notify --deferred-drain` and relay the results to the user. Do not wait for the user to ask.
- If a notice arrived mid-task, drain at the next natural break in the human-facing conversation and mention it briefly ("I have a Quaid notice — [summary]").
- Active outage/failure notices use the normal live notification path and should be relayed immediately when surfaced.

---

## Config & Instances

```bash
quaid config path                    # print the active config file path
quaid auth refresh <token>           # store/refresh shared provider auth
quaid instances list [--json]
```

**Note:** `quaid config` is deprecated during prerelease except for `config path`
and the compatibility `config set-auth` route. Prefer direct JSON edits of the layered config files
(`~/.quaid/shared/config/global/config.json`, `~/.quaid/shared/config/<platform>/config.json`,
`~/.quaid/instances/<instance>/config.json`). Edit the file layer that matches your scope — the
resolver layers instance → platform → global. For scripted changes use a python one-liner
(`python3 -c "import json,os; p=os.path.expanduser('...'); c=json.load(open(p)); c['key']='value'; json.dump(c, open(p,'w'), indent=2)"`)
or `jq`.

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

**Missing session context:** inspect internal `session_logs` storage directly rather than using a public CLI.

**Conflicting facts:** prefer newest; if unresolved, surface uncertainty and suggest janitor review
