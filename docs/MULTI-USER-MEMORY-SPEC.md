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
- Computes deterministic `source_id` / `conversation_id` keys.
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
- `conversation_id`
- `policy_mode`

### 6.3 Ingest -> Datastore write envelope

- fact payload + provenance
- `speaker_entity_id`
- `subject_entity_id`
- `source_id`
- `conversation_id`
- visibility defaults for source type

### 6.4 Recall API contract

Recall/search interfaces must accept:

- `viewer_entity_id`
- `source_id` and/or `source_channel` + `conversation_id`
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
