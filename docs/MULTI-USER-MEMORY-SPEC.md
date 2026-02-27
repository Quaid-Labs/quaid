# Multi-User and Group Memory Spec

Status: Design locked for prelaunch schema/interface seeding (no behavior rollout in this phase)
Owner: Quaid maintainers
Priority: P0 post-release capability, compatibility groundwork must be pre-seeded now

## 1. Purpose

Quaid must evolve from single-user memory to identity-aware memory that works for:

- direct messages with many users,
- group threads with changing participants,
- agent swarms where multiple agent identities communicate.

This spec defines the boundary-correct architecture, schema, and contracts to seed now so future rollout does not require backward-compatibility shims or large migrations.

## 2. Scope

Included now (pre-seeding):

- datastore schema and indexes for identity/source-aware storage,
- cross-layer interfaces (adapter -> core -> ingest -> datastore -> recall),
- privacy policy contract shape and enforcement hooks,
- janitor orchestration contract for identity maintenance routines,
- benchmark/E2E requirements for eventual rollout.

Not included now:

- full policy engine implementation,
- automatic identity linking without review safeguards,
- full shared-memory conflict resolution for autonomous multi-agent collaboration.

## 3. Core Model

Definitions:

- `source`: where a message happens (`dm`, `group`, `thread`, `workspace`).
- `speaker`: who produced a message.
- `subject`: who/what a fact is about.
- `viewer`: identity requesting recall.
- `scope`: visibility boundary for a memory record.

Hard rule:

- `source`, `speaker`, `subject`, and `viewer` are first-class context fields carried through the full pipeline.

## 4. Boundary Ownership

Adapter:

- Provides host metadata only:
  - source/channel IDs,
  - speaker handle/platform ID,
  - participant list (if available),
  - transcript/log location (if available).
- Never performs identity resolution, privacy filtering, or memory business logic.

Core:

- Registers exactly one active identity resolver and one active privacy policy provider.
- Owns orchestration, registration, and request-context assembly.
- Enforces single registration per extension point (same anti-double-registration rule used elsewhere).

Ingest:

- Normalizes adapter payloads into canonical identity envelopes.
- Computes deterministic `source_id` / `source_conversation_id` keys.
- Passes normalized identity context to datastore and recall planner.

Datastore:

- Owns identity persistence, query filters, recall mux behavior, and identity maintenance logic.
- Exposes stable APIs; internal heuristics stay datastore-private.

Janitor:

- Orchestrates only.
- Runs registered datastore maintenance routines and reports outcomes.
- Must not contain datastore identity logic.

## 5. Data Model (Seed Now)

Add and keep in schema now, even if partially unused by current runtime.

Identity tables:

1. `entities`
- `entity_id` (PK)
- `entity_type` (`human`, `agent`, `org`, `system`, `unknown`)
- `canonical_name`
- `created_at`, `updated_at`

2. `entity_aliases`
- `alias_id` (PK)
- `entity_id` (FK -> entities)
- `platform`
- `source_id` (nullable, alias can be source-specific)
- `handle`
- `display_name`
- `confidence`
- `created_at`, `updated_at`
- uniqueness target: (`platform`, `source_id`, `handle`)

3. `sources`
- `source_id` (PK)
- `source_type` (`dm`, `group`, `thread`, `workspace`)
- `platform`
- `external_id`
- `parent_source_id` (nullable)
- `created_at`, `updated_at`

4. `source_participants`
- `source_id` (FK -> sources)
- `entity_id` (FK -> entities)
- `role` (`member`, `owner`, `agent`, `observer`)
- `active_from`, `active_to`

Memory/session extensions:

- `subject_entity_id`
- `speaker_entity_id`
- `source_id`
- `conversation_id`
- `visibility_scope` (`private_subject`, `source_shared`, `global_shared`, `system`)
- `sensitivity` (`normal`, `restricted`, `secret`)
- `provenance_confidence`

Required indexes:

- (`subject_entity_id`, `status`, `updated_at`)
- (`source_id`, `status`, `updated_at`)
- (`speaker_entity_id`, `status`)
- (`visibility_scope`, `sensitivity`, `status`)
- (`conversation_id`, `created_at`)

## 6. Interface Contracts (Seed Now)

### 6.1 Adapter -> Core envelope

Each inbound event should carry:

- `platform`
- `source_channel`
- `source_conversation_id`
- `source_author_id`
- `source_author_handle` (optional)
- `participants` (optional list)
- `session_id` (if available)

### 6.2 Core -> Ingest normalized identity context

- `viewer_entity_id` (if known)
- `speaker_entity_id` (resolved or unresolved candidate)
- `participant_entity_ids`
- `source_id`
- `source_conversation_id`
- `policy_mode`

### 6.3 Ingest -> Datastore write envelope

- fact payload + provenance
- `speaker_entity_id`
- `subject_entity_id`
- `source_id`
- `source_conversation_id`
- visibility defaults for source type

### 6.4 Recall API contract

Recall/search interfaces must accept:

- `viewer_entity_id`
- `source_id` and/or `source_channel` + `source_conversation_id`
- `participant_entity_ids`
- `subject_entity_id` (optional target)
- `include_unscoped` (policy-controlled)

No retrieval surface may bypass this context once multi-user mode is enabled.

## 7. Retrieval Mux Semantics

Default retrieval plan for request from speaker `U` in source `S` to agent `A`:

1. agent self memory (`A`) within policy,
2. memory about requesting speaker (`U`),
3. source-shared memory for `S`,
4. optional broader shared memory if policy allows.

Fuse and rerank only after policy filtering.

Explicit cross-user question:

- planner identifies target entity,
- performs scoped retrieval for target entity with full policy checks,
- returns provenance-aware results.

## 8. Privacy Policy Contract

Policy decision input tuple:

- (`viewer_entity_id`, `subject_entity_id`, `source_id`, `visibility_scope`, `sensitivity`, `operation`)

Baseline rules:

- `private_subject`: subject + explicitly authorized system actors.
- `source_shared`: active source participants.
- `global_shared`: any allowed identity.
- `restricted` / `secret`: explicit allow required.

Hard requirement:

- policy filtering runs before candidate scoring and again before final output.

## 9. Janitor Contract

Datastore must register identity maintenance routines; janitor executes registration list only.

Example datastore-owned routines:

- alias hygiene and confidence decay,
- unresolved identity queue processing,
- stale participant interval cleanup,
- visibility/sensitivity integrity checks.

Janitor owns:

- scheduling,
- orchestration,
- logging/metrics/notifications,
- retry and run-summary reporting.

## 10. Configuration Shape

Add/keep config keys now:

- `identity.mode`: `single_user` | `multi_user` (default `single_user`)
- `identity.auto_link_threshold`
- `identity.require_review_threshold`
- `privacy.default_scope_dm`
- `privacy.default_scope_group`
- `privacy.enforce_strict_filters` (default `true`)

Policy/fallback rule still applies globally:

- `retrieval.fail_hard=true` blocks degraded fallback behavior.
- `retrieval.fail_hard=false` allows fallback with explicit warning logs.

## 11. Migration and Compatibility Strategy

Prelaunch requirement:

- seed schema/interfaces now, keep runtime default behavior compatible with single-user mode.

Migration invariants:

- additive schema changes only,
- deterministic synthetic defaults for legacy rows,
- no destructive data rewrite,
- no compatibility shims in core boundaries.

## 12. Benchmark and E2E Plan

Add benchmark lane: `multi_user_agentlife`.

Required benchmark scenarios:

- alias continuity across channels,
- group attribution precision (A/B/C separation),
- privacy containment under adversarial prompts,
- rapid source/context switching.

Required E2E suites:

- multi-user source mux,
- privacy gate enforcement,
- participant-aware recall routing,
- identity maintenance convergence through janitor.

## 13. Open Decisions to Resolve Before Implementation

1. default subject assignment when ownership is ambiguous,
2. v1 need for per-fact ACL overrides beyond scope+sensitivity,
3. promotion policy from source-shared to global-shared,
4. representation model for swarm-level collective memory.

## 14. Execution Checklist

1. Keep schema and interfaces seeded in `single_user` mode.
2. Add policy engine + resolver as pluggable datastore/core registrations.
3. Enable `multi_user` mode behind explicit config.
4. Ship benchmark + E2E gates before enabling by default.

## 15. Current Safety Invariants

Current code-level guarantees aligned with this spec:

1. Resolver/policy registration is single-owner (duplicate registration raises).
2. Multi-user write contract is fail-fast on missing required source identity fields.
3. Multi-user read contract is fail-fast on missing `viewer_entity_id`.
4. Core auto-bootstraps datastore-owned default resolver/policy hooks so missing registration does not become a silent runtime gap.

## 16. Principal and Trust Model (Seed Now)

Quaid should model requesters as principals with explicit trust state:

- `human_user`
- `trusted_agent`
- `untrusted_agent`
- `external_unknown`
- `system_internal`
- `owner_admin`

Principal profile fields to seed now:

- `principal_id` (canonical entity ID),
- `principal_type` (`human`/`agent`/`service`),
- `role` (viewer role in current context),
- `trust_tier`,
- `org_id` (optional tenant/org scope),
- `auth_strength` (`anonymous`, `verified`, `strong_verified`),
- `revoked_at` / `disabled`.

Hard rule:

- policy decisions are based on principal identity + trust state, not on claimed handle alone.

## 17. Authentication and Delegation Model (Seed Now)

Authentication methods to support:

- `agent_key` (public/private key challenge-response),
- `password` (human fallback; lower trust),
- `cross_channel_proof` (verify identity over already trusted channel),
- `delegated_assertion` (trusted principal vouches for another).

Group patterns to support:

- 1:1 channels should default to authentication-first handshake.
- Group channels may run in low-friction mode, but sensitive scopes still require elevated auth.
- Private groups can require stronger auth per group policy.
- Delegation/keymaster is allowed only with bounded scope + TTL + audit log.

Seed now (schema/interfaces only):

- `identity_credentials`,
- `identity_sessions`,
- `delegation_grants`,
- `trust_assertions`.

## 18. Datastore Classification and Access Policy

Datastores should declare policy metadata and minimum auth requirements:

- `data_class`: `public` | `internal` | `confidential` | `restricted`
- `default_scope`
- `auth_min_level`
- `supports_multi_user` (capability)
- `supports_policy_metadata` (capability)
- `supports_redaction` (capability)

Examples:

- `product_catalog` -> `public`
- `sales_pipeline` -> `confidential`

Record-level metadata can tighten datastore defaults:

- `visibility_scope`,
- `sensitivity`,
- `subject_entity_id`,
- `speaker_entity_id`,
- `source_id` / `conversation_id`,
- `participant_entity_ids`,
- `org_id`,
- `provenance_confidence`.

Owner-admin behavior:

- `owner_admin` can access all records, but every override must be explicitly audited.

## 19. Plugin Enforcement Contract (Core-Owned Access Layer)

Policy enforcement must be centralized in core, not reimplemented by each datastore plugin.

Required model:

1. Datastore plugins must emit normalized policy metadata per result/write.
2. Core policy engine evaluates allow/deny/redact decisions.
3. Retrieval path: plugin candidate results -> core policy filter -> scoring/rerank -> final output.
4. Write path: core contract validation first, then plugin write.
5. In `identity.mode=multi_user`, core blocks plugins missing required policy capabilities.

This prevents policy drift across external datastores and reduces boundary leakage risk.

## 20. Retrieval Surfaces for Multi-User Mode

Seed API shapes now (behavior rollout can wait):

- `search_self(viewer_entity_id, query, ...)`
- `search_subject(viewer_entity_id, subject_entity_id, query, ...)`
- `search_conversation(viewer_entity_id, source_id|channel+conversation, query, include_both_parties=true, ...)`
- `search_network(viewer_entity_id, query, mode=metadata_only|content, ...)`

Defaults:

- cross-subject and cross-conversation reads are deny-by-default unless policy grants permit.
- `private_subject` is always blocked unless subject/self/system-authorized path.
- sensitive classes (`restricted`, `secret`) require explicit allow.

## 21. Group Conversation Specific Requirements

Group chat requires additional rules beyond DM:

- participant membership must be time-bounded (`active_from`, `active_to`) and checked at event time.
- source boundaries are strict; same user in different groups does not imply access transfer.
- attribution uncertainty must be represented (`provenance_confidence` and optional status like `ambiguous`).
- group-local facts must not auto-promote to global-person facts without policy.
- public-group mode should favor low-friction auth but strict memory containment.

## 22. Sensitive Scenarios (Normative Outcomes)

1. Partner asks for surprise plans in DM/group:
- deny private subject data unless explicit sharing policy allows.

2. Unknown stranger asks for private info about owner:
- deny; optional redacted response only.

3. Trusted agent asks who might know X:
- metadata-only network search allowed by policy; content access still scoped.

4. Sales bot (public catalog + private customer pipeline):
- product datastore can be broadly accessible,
- customer pipeline records require org + role + auth checks,
- employees with grants can read allowed scopes,
- owner-admin may override with audit.

## 23. Additional Cases to Track

Cases often missed in early multi-user designs:

- impersonation via alias collision across channels,
- stale delegation grants and forgotten revocation,
- replayed auth assertions from old sessions,
- policy changes over time requiring deterministic historical replay,
- mixed-agent orchestration where one agent has broader scope than another,
- channel migration (group renamed/recreated) and source continuity,
- redaction bypass via tool output formatting.

## 24. Prelaunch Forward-Compatible Seeding (Must Do Now)

To avoid compatibility shims later, seed these before launch:

1. canonical envelope fields across all write/read paths,
2. additive policy metadata columns across relevant stores (memory/session/docs/journal/snippets/projects),
3. principal/trust/auth/delegation table skeletons,
4. centralized policy decision contract in core (`allow`/`deny`/`allow_redacted`),
5. policy audit log schema for every sensitive decision,
6. datastore plugin capability flags for multi-user/policy compliance,
7. required indexes for subject/source/scope/sensitivity/time access patterns,
8. strict default-deny baseline in multi-user mode.

## 25. Future-Proofing Checklist

- Keep all schema additions additive and nullable with deterministic defaults.
- Freeze API envelope names now; do not rename later.
- Add conformance tests for plugin policy metadata contract.
- Add E2E policy matrix tests for DM, private group, public group, and agent-to-agent channels.
- Require explicit policy reason codes in logs (`deny_reason`, `redaction_reason`, `grant_source`).
- Keep policy evaluation deterministic and side-effect free (auditable replays).
- Version policy engine decisions to support future migration and replay.
- Keep trust/delegation TTL explicit and revocation-first.

## 26. Enterprise/Compliance Forward-Seed

To avoid painful retrofits for enterprise deployments, seed these contracts now:

1. Data residency tagging:
- add optional `region` / `residency_class` metadata for datastores and records.

2. Right-to-delete propagation:
- define deletion/tombstone propagation contract across source facts, derived summaries, edges, and indexes.

3. Consent and purpose binding:
- add `purpose_tag` / `consent_scope` in policy inputs so data collected for one purpose is not reused implicitly.

4. Cache isolation:
- require tenant/principal-aware cache keys for embedding/retrieval/reranker/session caches.

5. Backup/restore scoping:
- define encrypted backup metadata and tenant-scoped restore constraints.

6. Break-glass admin access:
- define explicit override flow with reason, approver, and post-access notification/audit.

7. Output/log redaction sinks:
- require redaction policy at notification/telemetry/debug log boundaries.

8. Decision version pinning:
- every policy decision record should include policy version and ruleset hash for replay/debug.

## 27. Domain Datastore Routing (Preseed Requirement)

Multi-user rollout requires explicit separation between:

- conversational/personal memory (`memorydb`-like stores),
- durable domain/business knowledge stores (for example `salesdb`, `recipesdb`, `productdb`).

Hard rule:

- memory is not a universal dumping ground.

Seed now:

1. Ingest classification contract must emit `target_datastore` (or ordered candidates).
2. Core write path must resolve final datastore via policy + capability checks.
3. Datastore manifests must declare domain + policy class metadata.
4. Retrieval planner must support multi-store query plans with policy-first filtering.
5. Dual-write behavior must be explicit + auditable (no silent fan-out).

Conversation-derived data split guidance:

- personal/session context -> memory datastore,
- business/domain records -> domain datastore,
- ambiguous records -> deterministic policy/routing fallback, never implicit "memory by default".

This significantly reduces future migration pain when adding customer/enterprise use cases.
