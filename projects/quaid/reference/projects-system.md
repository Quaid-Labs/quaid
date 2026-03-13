# Projects System — Internal Reference

> **This file has been consolidated.**
>
> The authoritative reference is now:
> [`projects/quaid/reference/projects-reference.md`](./projects-reference.md)
>
> That document covers everything that was here: the two-registry model and critical
> distinction, full lifecycle function signatures (`create_project`, `link_project`,
> `unlink_project`, `update_project`, `delete_project`), `DocsRegistry` operations,
> global registry vs `project show`, cross-instance project sharing, shadow git
> (`ShadowGit` class, methods, return types, lifecycle integration), project updater
> (event model, `process_event`, `process_all_events`, `evaluate_doc_health`,
> `append_project_logs`, watchdog), sync engine (`sync_project`, `sync_all_projects`,
> stale target cleanup), CLI reference, key invariants and gotchas, and file locations
> quick reference.

**Updated:** 2026-03-13 — consolidated into projects-reference.md
