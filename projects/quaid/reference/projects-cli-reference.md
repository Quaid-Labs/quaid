# Projects CLI Reference

## Canonical Path

`projects/quaid/reference/projects-cli-reference.md`

## Common Commands

```bash
# list projects
python3 modules/quaid/docs_registry.py list-projects

# list docs in a project
python3 modules/quaid/docs_registry.py list --project quaid

# inspect one project
python3 modules/quaid/docs_registry.py get-project quaid

# search docs
python3 modules/quaid/docs_rag.py search "memory extraction flow"
```

## Notes

- Project metadata is managed by the docs registry.
- Search/index behavior is managed by `docs_rag.py`.
- Project update events are processed by `project_updater.py`.
