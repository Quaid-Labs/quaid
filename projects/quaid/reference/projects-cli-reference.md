# Projects CLI Reference

## Canonical Path

`projects/quaid/reference/projects-cli-reference.md`

## Common Commands

```bash
# list projects
python3 modules/quaid/datastore/docsdb/registry.py list-projects

# list docs in a project
python3 modules/quaid/datastore/docsdb/registry.py list --project quaid

# inspect one project
python3 modules/quaid/datastore/docsdb/registry.py get-project quaid

# search docs
python3 modules/quaid/datastore/docsdb/rag.py search "memory extraction flow"
```

## Notes

- Project metadata is managed by `datastore/docsdb/registry.py`.
- Search/index behavior is managed by `datastore/docsdb/rag.py`.
- Project update events are processed by `datastore/docsdb/project_updater.py`.
