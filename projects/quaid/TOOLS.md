# Quaid — Tool Reference

Use Quaid through Bash. Prefer `quaid` when it is on `PATH`; otherwise use `$QUAID_HOME/modules/quaid/quaid` for current installs or `$QUAID_HOME/plugins/quaid/quaid` for older installs.

`QUAID_HOME` and `QUAID_INSTANCE` are normally set by the adapter. If calling Quaid outside a hook/session, set both explicitly.

## Recall And Memory

```bash
quaid recall "query"
quaid recall "query" '{"stores":["vector","graph","docs"]}'
quaid recall "query" '{"stores":["docs"],"project":"quaid"}'
quaid store "text"
quaid get-node <id>
quaid get-edges <id>
quaid delete <id>
quaid stats
```

Recall config JSON fields:

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

Use `domain_boost` for soft preference. Use `domain_filter` only when other domains must be excluded. Add `--json` for machine-readable output and `--debug` for scoring details.

## Project Docs And Registry

```bash
quaid docs list [--project <name>]
quaid docs check
quaid docs update --apply
quaid registry register <path> --project <name>
quaid registry list [--project <name>]
```

`docs` recall searches project docs RAG and can include the matching `PROJECT.md`. `PROJECT.md` is the overview/map; registry commands are the exact-truth backstop for tracked files and ownership.

## Projects

```bash
quaid project list [--names-only]
quaid project create <name> [--description "..."] [--source-root /path]
quaid project show <name>
quaid project update <name> [--description "..."] [--source-root /path]
quaid project link <name>
quaid project unlink <name>
quaid project delete <name>
quaid project snapshot [<name>]
quaid project sync
quaid project status <project>
quaid project diff <project> [--full]
quaid global-registry list
```

Put real source files in their real working locations and register/link them. Quaid-managed project docs and metadata live under `QUAID_VISIBLE_HOME/projects/<name>/`.

## Maintenance And Supervisor

```bash
quaid janitor --task all --dry-run
quaid janitor --task all --apply
quaid doctor
quaid supervisor status
quaid supervisor ensure
quaid supervisor stop
quaid docs update <project>
quaid notify --deferred-status
quaid notify --deferred-drain
```

Use `supervisor stop` for normal teardown. Emergency cleanup should target the supervisor process group, not individual workers.

## Config And Instances

```bash
quaid config show
quaid config edit [--shared]
quaid instances list [--json]
QUAID_INSTANCE=<instance> quaid recall "query"
```

`quaid config set` is deprecated. Edit the correct layered JSON file directly: instance, platform, then global.

## Domains

```bash
quaid domain list
quaid domain register <name> "description"
```
