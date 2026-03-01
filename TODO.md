# TODO

## Forward-Looking Adaptability (Post-Benchmark Priority)

- [ ] Full prompt-system migration follow-ups:
  - migrate remaining hardcoded `system_prompt` callsites to `prompt_sets` keys for consistency:
    - `modules/quaid/datastore/memorydb/memory_graph.py:1752`
    - `modules/quaid/datastore/memorydb/memory_graph.py:2696`
    - `modules/quaid/datastore/memorydb/memory_graph.py:3054`
    - `modules/quaid/core/lifecycle/workspace_audit.py:791`
    - `modules/quaid/datastore/memorydb/maintenance_ops.py:2938`
  - tighten `prompt_sets` bootstrap lock path for free-threading safety (remove outer unchecked `_BOOTSTRAPPED` read / single-lock bootstrap path)
  - make `reload_config()` fully atomic across `_config` clear + plugin reset + prompt-set reset/load to remove the current race window

- [ ] Multi-user memory foundations (schema/interfaces now, behavior later):
  - lock to `docs/MULTI-USER-MEMORY-SPEC.md` as canonical design
  - seed additive schema/indexes for `entities`, `entity_aliases`, `sources`, `source_participants`
  - unify identity fields across memory/session logs/journal/snippets/projects
  - seed principal/trust/auth/delegation skeletons (`identity_credentials`, `identity_sessions`, `delegation_grants`, `trust_assertions`)
  - add centralized policy decision contract (`allow`/`deny`/`allow_redacted`) and audit-log schema
  - keep runtime default in `identity.mode=single_user` until benchmark gates pass
  - implement resolver + privacy policy as registered providers (single active registration)
- [ ] Group conversation context loading:
  - resolve participant set from source channel
  - enforce participant membership timelines (`active_from`/`active_to`) in policy checks
  - load correct user/core markdown bundles per active participants
  - support direct-message vs group-message context switches
- [ ] Domain datastore routing foundations:
  - add ingest `target_datastore` classification contract
  - add core routing policy for conversation-derived writes (memory vs domain stores)
  - require explicit dual-write config + audit trail (no silent fan-out)
  - declare datastore domain/policy class metadata in plugin manifests
- [ ] Domain-tag migration tooling:
  - current limitation: historical memories may be untagged because domain tagging is strongest at extraction time
  - add `scripts/migrate-memory-domains.py` to backfill domains with dry-run + confidence scoring
  - support review-first mode (emit candidates for operator approval before applying)
  - document recommendation: configure domain set at bot creation time for highest recall quality
- [ ] Enterprise/compliance forward-seed contracts:
  - add residency metadata (`region`, `residency_class`) to datastore/record policy envelope
  - define right-to-delete propagation contract (facts, edges, derived summaries/indexes)
  - add consent/purpose binding fields (`purpose_tag`, `consent_scope`) to policy contract
  - enforce tenant/principal-aware cache keying for retrieval/reranker/embedding/session caches
  - add backup/restore scoping metadata + encrypted snapshot expectations
  - define break-glass admin override contract (reason/approval/alert + audit)
  - require sink-side redaction policy for notifications/telemetry/debug logs
  - record policy version/ruleset hash on every access decision for replay
- [ ] Adapter portability contract:
  - adapter only reports source/channel/message identity and log locations
  - ingest/datastore own parsing/indexing/search behavior
  - no business logic in adapter fallbacks
  - eliminate remaining direct `adaptors/openclaw` imports/usages of `datastore` and `ingest` modules; all adapter IO must route through core-owned contract surfaces
- [ ] Benchmark track:
  - add multi-user benchmark lane (agent swarms + group chat attribution)
  - add privacy regression assertions (no leakage across entities)

- [ ] OpenClaw release detector/tester (replace fallback-heavy compatibility drift):
  - detect OpenClaw hook/tool lifecycle API changes at install/bootstrap time
  - run a focused compatibility test suite against detected version before gateway enable
  - fail loudly with actionable diagnostics instead of introducing adapter-level soft fallbacks

- [ ] Plugin runtime migration (foundation seeded in phase 1):
  - [x] enforce plugin manifest + slot/type preflight at config boot (`plugins.strict` hard-fail, non-strict loud diagnostics)
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
