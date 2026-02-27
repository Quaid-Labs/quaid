# Decision 001: SQLite Over Cloud Database

**Date:** 2026-01-31  
**Status:** Accepted  
**Deciders:** Quaid, Alfie

## Context

The original memory system used LanceDB with cloud storage. We needed to decide whether to continue with cloud-based storage or move to a local solution.

## Decision

**Use SQLite with WAL mode for all memory storage.**

## Rationale

1. **Cost** — Cloud storage has per-query costs that add up. SQLite is free.

2. **Latency** — Local disk is ~1ms. Cloud roundtrip is 50-200ms. For auto-injection on every message, this matters.

3. **Privacy** — All data stays on the Mac mini. No external API calls for storage.

4. **Simplicity** — Single file database. Easy to backup, restore, inspect. `sqlite3 memory.db` just works.

5. **Reliability** — No network dependency. Works offline. No cloud outages.

6. **Sufficient scale** — SQLite handles millions of rows easily. Our memory system will have thousands at most.

## Trade-offs Accepted

- No built-in replication (mitigated by NAS backups)
- No multi-machine access (not needed for single-agent setup)
- Manual index management (worth it for control)

## Consequences

- All memory operations are local and fast
- Backup is just copying a file
- Future multi-agent setup will need to consider shared access (but can use WAL mode for concurrent reads)

## See Also

- [002-local-ollama-embeddings.md](./002-local-ollama-embeddings.md)
- `modules/quaid/datastore/memorydb/memory_graph.py`
