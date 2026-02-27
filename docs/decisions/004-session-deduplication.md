# Decision 004: Session-Based Deduplication

**Date:** 2026-02-01  
**Status:** Accepted  
**Deciders:** Quaid, Alfie

## Context

Two problems emerged with memory injection:

1. **Duplicate injection** — The same memories were being injected multiple times per session, wasting tokens
2. **Feedback loops** — Memories captured in the current conversation were immediately injected back

## Decision

**Use timestamp-based session IDs to track injection and filter current-session memories.**

## Implementation

### Session ID Format
```
{unix_timestamp}_{random_suffix}
```
Generated once per session start, stored in session context.

### Injection Tracking
```sql
-- Track which memories were injected this session
ALTER TABLE nodes ADD COLUMN last_injected_session TEXT;
```

On recall:
1. Query semantically relevant memories
2. Filter out memories where `session_id = current_session` (just captured)
3. Filter out memories where `last_injected_session = current_session` (already shown)
4. Update `last_injected_session` for returned memories

### Result
- ~90% token savings on memory injection
- No immediate feedback of just-captured facts
- Clean separation between "what I just learned" and "what I already knew"

## Trade-offs Accepted

- Adds a column and filter logic
- Session ID must be passed through the call chain
- Memories captured late in session won't surface until next session

## Consequences

- Memory injection is efficient (once per session per memory)
- No confusing echo of just-stated facts
- Cleaner conversation flow

## See Also

- `projects/quaid/reference/memory-deduplication-system.md`
- `modules/quaid/datastore/memorydb/memory_graph.py` § recall()
