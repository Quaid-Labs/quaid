# Quaid Tools Reference

## Agent Tools (available during conversations)

These tools are registered automatically when the plugin loads.

### memory_recall
Search the memory graph for relevant facts about the user.
- `query`: Search query — use entity names and specific topics
- `limit`: Max results (default: 5, max: 8). Lower is better for quality.
- `use_graph`: Traverse relationship graph — use for people/family queries (default: true)
- `graph_depth`: Graph traversal depth (default: 1). Use 2 for extended relationships like nephew/cousin.

### memory_store
Store a new fact in the memory graph. Use sparingly — extraction at compaction handles most cases.
- `text`: Information to remember

### memory_forget
Delete specific memories when the user asks to forget something.
- `query`: Search to find the memory
- `memory_id`: Specific memory ID (if known)

### docs_search
Search project documentation (architecture, implementation, reference guides).
- `query`: Search query
- `limit`: Max results (default: 5)
- `project`: Filter by project name

### docs_read
Read the full content of a registered document by file path or title.
- `identifier`: File path (workspace-relative) or document title

### docs_list
List registered documents, optionally filtered by project or type.
- `project`: Filter by project name
- `type`: Filter by asset type (doc, note, reference)

### docs_register
Register a document for indexing and tracking.
- `file_path`: Workspace-relative path
- `project`: Project name (default: 'default')
- `source_files`: Source files this doc tracks (enables staleness detection)

### project_create
Create a new project with a PROJECT.md template.
- `name`: Project name (kebab-case, e.g., 'my-essay')
- `label`: Display label (e.g., 'My Essay')
- `description`: Project description

## CLI Commands

Run from the workspace root:

```bash
quaid doctor          # Health check (DB, embeddings, API key, gateway)
quaid stats           # Database statistics (node counts, edge counts, pending/active)
quaid search <query>  # Search memory
quaid config          # Show current configuration
quaid export          # Export all facts as JSON
quaid janitor --dry-run          # Preview nightly maintenance
quaid janitor --task review      # Run a specific janitor task
quaid re-embed        # Re-embed all facts (after changing embedding model)
```

## Python Entry Points

From `plugins/quaid/`:

```bash
# Memory operations
python3 memory_graph.py search "query" --owner <owner> --limit 10
python3 memory_graph.py store "fact" --owner <owner> --category fact
python3 memory_graph.py stats
python3 memory_graph.py get-edges <node_id>

# Doc management
python3 docs_updater.py check              # Show stale docs
python3 docs_updater.py update-stale --apply
python3 docs_rag.py search "query"         # RAG search (local embeddings)

# Project management
python3 docs_registry.py list
python3 docs_registry.py list --project <name>
python3 docs_registry.py create-project <name> --label "Label"
python3 docs_registry.py auto-discover <project>
```
