# TODO

## Forward-Looking Adaptability (Post-Benchmark Priority)

- [ ] Multi-user memory foundations (schema/interfaces now, behavior later):
  - add stable `entity_id`/`actor_id` ownership shape across memory, journal, snippets, session-log indexes
  - add channel/source identity mapping table (alias -> canonical entity)
  - add query-time subject scope API (`self`, `counterparty`, `group`, `global`)
  - define privacy policy boundaries for cross-entity recall
- [ ] Group conversation context loading:
  - resolve participant set from source channel
  - load correct user/core markdown bundles per active participants
  - support direct-message vs group-message context switches
- [ ] Adapter portability contract:
  - adapter only reports source/channel/message identity and log locations
  - ingest/datastore own parsing/indexing/search behavior
  - no business logic in adapter fallbacks
- [ ] Benchmark track:
  - add multi-user benchmark lane (agent swarms + group chat attribution)
  - add privacy regression assertions (no leakage across entities)

## Fallback Sweep Backlog

- [ ] Enforce fail-hard policy consistently:
  - failHard=true: no fallbacks
  - failHard=false: fallback allowed, but must emit loud warnings
- [ ] Audit remaining fallback-heavy modules:
  - `modules/quaid/lib/providers.py`
  - `modules/quaid/lib/embeddings.py`
  - `modules/quaid/datastore/docsdb/rag.py`
  - `modules/quaid/datastore/memorydb/memory_graph.py` (best-effort blocks and soft-degrade paths)
  - `modules/quaid/core/session-timeout.ts` (event-message fallback behavior)
- [ ] Add tests that fail if silent fallback paths are introduced in critical flows (recall/extract/janitor).

