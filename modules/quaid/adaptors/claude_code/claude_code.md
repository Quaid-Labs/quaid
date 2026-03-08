## Memory System (Quaid)

Quaid is an automatic memory system that persists knowledge across sessions.

### Automatic behavior (no action needed)
- Memories are automatically recalled on each message and injected as context
- Knowledge is automatically extracted before compaction and at session end
- Critical memories are re-injected after context compaction

### Manual commands (use via Bash tool when helpful)
- `quaid recall "query"` — Search memories for past context before unfamiliar work
- `quaid store "fact"` — Save important patterns, architectural decisions, or bug fixes
- `quaid hook-search "query"` — Search both memories and project documentation
- `quaid stats` — Check memory system health
