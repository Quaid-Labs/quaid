## Memory System (Quaid)

Quaid is an automatic memory system that persists knowledge across sessions.

### Automatic behavior (no action needed)
- Memories are automatically recalled on each message and injected as context
- Knowledge is automatically extracted before compaction and at session end
- Project docs are loaded via `.claude/rules/` and persist through compaction

### Manual commands (use via Bash tool when helpful)
- `quaid recall "query"` — Search memories for past context before unfamiliar work
- `quaid store "fact"` — Save important patterns, architectural decisions, or bug fixes
- `quaid search "query"` — Fast search (no reranking, cheaper than recall)
- `quaid docs search "query"` — Search project documentation
- `quaid docs search "query" --project <name>` — Search docs in a specific project
- `quaid recall "query" --docs` — Search both memories and project documentation
- `quaid session list` — List recent conversation sessions
- `quaid domain list` — List knowledge domains
- `quaid stats` — Check memory system health
- `quaid doctor` — Full health check

### Environment
Commands require `QUAID_HOME` to be set (configured automatically by hooks).
