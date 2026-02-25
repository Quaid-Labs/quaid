# TODO

## Forward-Looking Adaptability (Post-Benchmark Priority)

- [ ] Multi-user memory foundations (schema/interfaces now, behavior later):
  - lock to `docs/MULTI-USER-MEMORY-SPEC.md` as canonical design
  - seed additive schema/indexes for `entities`, `entity_aliases`, `sources`, `source_participants`
  - unify identity fields across memory/session logs/journal/snippets/projects
  - keep runtime default in `identity.mode=single_user` until benchmark gates pass
  - implement resolver + privacy policy as registered providers (single active registration)
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

- [ ] OpenClaw release detector/tester (replace fallback-heavy compatibility drift):
  - detect OpenClaw hook/tool lifecycle API changes at install/bootstrap time
  - run a focused compatibility test suite against detected version before gateway enable
  - fail loudly with actionable diagnostics instead of introducing adapter-level soft fallbacks

- [ ] Plugin runtime migration (foundation seeded in phase 1):
  - convert first-party OpenClaw adapter to plugin registration contract
  - convert ingest flows to plugin registration contract
  - convert memorydb/docsdb/notedb to datastore plugin registration contract
  - route janitor maintenance registration through plugin capabilities
  - enforce conformance tests before enabling third-party plugins by default

## Fallback Sweep Backlog

- [ ] Enforce fail-hard policy consistently:
  - failHard=true: no fallbacks
  - failHard=false: fallback allowed, but must emit loud warnings
- [ ] Audit remaining fallback-heavy modules:
  - `modules/quaid/lib/providers.py` (done: ClaudeCode `.env` OAuth token fallback now gated by `retrieval.fail_hard` + noisy warning path)
  - `modules/quaid/lib/embeddings.py`
  - `modules/quaid/datastore/docsdb/rag.py`
  - `modules/quaid/datastore/memorydb/memory_graph.py` (best-effort blocks and soft-degrade paths)
  - `modules/quaid/core/session-timeout.ts` (event-message fallback behavior)
- [ ] Add tests that fail if silent fallback paths are introduced in critical flows (recall/extract/janitor).
