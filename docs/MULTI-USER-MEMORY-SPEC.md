# Multi-User and Group Memory Spec (Design Only)

Status: Draft (no implementation)
Owner: Quaid maintainers
Scope: Architecture and contracts for multi-user, multi-conversation identity-aware memory

## 1) Problem Statement

Quaid is currently optimized for single-user/single-agent memory. Real deployments now involve:

- Multi-user direct messages.
- Group chats with changing participants.
- Agent swarms where multiple agent identities talk to each other.
- Identity aliases across channels (same entity, different handle).

Current risk if we do nothing: cross-entity memory leakage, low retrieval precision, ambiguous attribution, and weak privacy guarantees.

## 2) Goals and Non-Goals

Goals:

- Distinguish entities reliably across channels and aliases.
- Route extraction, storage, and retrieval through explicit conversation and participant context.
- Preserve subsystem boundaries:
  - adaptor reports source metadata
  - core owns orchestration/routing
  - ingest performs normalization
  - datastore owns persistence/query/maintenance internals
- Make privacy/visibility enforceable at query time.
- Keep datastore modular/swappable.
- Provide benchmarkable behavior for multi-user lifecycle quality.

Non-goals (for this phase):

- Full shared-memory conflict resolution among autonomous agents.
- Cross-instance federated identity sync.
- Automatic identity linking with no human/operator override path.

## 3) Core Concepts

- `source`: communication channel/session locus (DM, group room, thread, workspace).
- `speaker`: actor that produced a message/fact in that source.
- `subject`: entity the fact is about.
- `viewer`: entity requesting recall (often the running agent persona).
- `scope`: visibility domain for a memory record.

Design rule:

- `source` models where data came from.
- `speaker` models who said it.
- `subject` models who/what it is about.

These must be first-class fields and passed end-to-end.

## 4) Target Architecture (Boundary-Correct)

Adaptor (host-specific):

- Emits normalized event context only:
  - source identifier(s)
  - speaker handle
  - platform user id/agent id if available
  - participant list for group messages
  - conversation path/thread id
- May optionally provide transcript path; no business logic for identity resolution.

Core:

- Owns registration of identity resolvers and recall planners.
- Resolves runtime request context (`viewer`, `source`, `participants`, `policy mode`).
- Calls ingest and datastore APIs through stable contracts.

Ingest:

- Normalizes event payloads into canonical identity descriptors.
- Computes deterministic source/conversation keys.
- Produces extraction envelopes tagged with identity context.

Datastore(s):

- Own all storage/query/maintenance logic for identity and scope.
- Provide search APIs with context parameters.
- Keep private/internal helper APIs internal.

Janitor:

- Orchestrates registered maintenance tasks only.
- No per-datastore identity business logic.
- Enforces global invariants and reports failures.

## 5) Data Model Additions (Proposed)

Add identity layer tables (datastore-owned):

1. `entities`
- `entity_id` (pk)
- `entity_type` (`human`, `agent`, `org`, `unknown`)
- `canonical_name`
- `created_at`, `updated_at`

2. `entity_aliases`
- `alias_id` (pk)
- `entity_id` (fk)
- `platform` (telegram/openclaw/discord/...)
- `source_id` (nullable, alias may be source-specific)
- `handle`
- `display_name`
- `confidence`
- unique candidate key: (`platform`, `source_id`, `handle`)

3. `sources`
- `source_id` (pk)
- `source_type` (`dm`, `group`, `thread`, `workspace`)
- `platform`
- `external_id`
- `parent_source_id` (nullable)
- `created_at`

4. `source_participants`
- `source_id`
- `entity_id`
- `role` (`member`, `owner`, `agent`, `observer`)
- `active_from`, `active_to`

Extend memory records with:

- `subject_entity_id`
- `speaker_entity_id`
- `source_id`
- `conversation_id` (thread/session key)
- `visibility_scope` (`private_subject`, `source_shared`, `global_shared`, `system`)
- `sensitivity` (`normal`, `restricted`, `secret`)
- `provenance_confidence`

Index strategy (initial):

- Composite btree:
  - (`subject_entity_id`, `status`, `updated_at`)
  - (`source_id`, `status`, `updated_at`)
  - (`speaker_entity_id`, `status`)
  - (`visibility_scope`, `sensitivity`, `status`)
- FTS/vector metadata filters include `subject_entity_id`, `source_id`, `visibility_scope`.

## 6) Identity Resolution Contract

Core registers exactly one active identity resolver at a time (same anti-double-registration rule used elsewhere).

Resolver inputs:

- platform metadata from adaptor
- speaker handle/display id
- participant roster
- existing alias links

Resolver outputs:

- `speaker_entity_id`
- `participant_entity_ids`
- candidate merges/links (with confidence)
- unresolved identities for review queue

Policy:

- High-confidence auto-link allowed.
- Mid/low confidence creates pending alias links requiring approval.
- Never silently merge two established entities with conflicting strong evidence.

## 7) Retrieval Semantics (Mux Rules)

Default recall for incoming user prompt in source `S` from speaker `U` to agent `A`:

1. Search self memory for `A` (agent operational/user-profile memory allowed by policy).
2. Search subject memory for `U` (what agent knows about that user).
3. Search shared source memory for `S` (group/channel context).
4. Optional broader shared memory if allowed (`global_shared`).
5. Fuse + rerank with strict visibility filters.

Group chat behavior:

- Query context includes full participant set.
- Retrieval bias:
  - high priority: requesting speaker + direct addressees mentioned in turn
  - medium: source-shared facts
  - low: unrelated participants unless explicitly referenced

Explicit cross-user ask ("what did B say?"):

- Planner detects target entity B.
- Performs scoped retrieval for B where visibility permits.
- Returns privacy-filtered summary with provenance notes.

## 8) Privacy and Access Policy

Policy check runs before final recall return.

Decision tuple:

- (`viewer_entity_id`, `subject_entity_id`, `source_id`, `visibility_scope`, `sensitivity`, `requested_operation`)

Rules baseline:

- `private_subject`: visible only to subject + authorized system actors.
- `source_shared`: visible to active participants of source.
- `global_shared`: visible to all allowed identities.
- `restricted/secret`: requires explicit policy permit.

Hard requirement:

- Policy filters apply both pre-rerank candidate gathering and post-rerank output.

## 9) Extraction Semantics in Multi-User Context

For each extracted fact:

- Set `speaker_entity_id` from turn speaker.
- Infer/resolve `subject_entity_id` from content (self-reference, named mention, pronoun resolution).
- Attach `source_id` and `conversation_id`.
- Assign default `visibility_scope` from source type and policy.

Ambiguous ownership:

- Store as pending with low provenance confidence.
- Queue for janitor review task `identity_disambiguation` (datastore-owned maintenance routine).

## 10) Janitor Responsibilities (Post-Refactor Aligned)

Janitor keeps orchestration only. Datastore provides identity-aware maintenance routines, e.g.:

- alias hygiene and stale alias pruning
- unresolved identity disambiguation queue
- cross-source duplicate merge recommendations
- visibility/sensitivity integrity checks

Core lifecycle registry provides task list; janitor executes and reports.

## 11) Configuration Model

Proposed config blocks:

- `identity.mode`: `single_user` | `multi_user`
- `identity.auto_link_threshold`
- `identity.require_review_threshold`
- `privacy.default_scope_dm`
- `privacy.default_scope_group`
- `privacy.enforce_strict_filters` (default true)

Migration toggle:

- Start with `single_user` default for backward compatibility.

## 12) Migration Plan (No Code Yet)

Phase 0: Spec + invariants
- finalize contracts and schema
- define policy matrix tests

Phase 1: Schema introduction
- status: implemented
- add identity/source tables + nullable columns
- backfill existing records to default synthetic entity/source

Phase 2: Write-path tagging
- status: partially implemented
- adaptor -> core -> ingest -> datastore carries identity context
- implemented now: session lifecycle log ingest metadata flow (`source_channel`, `conversation_id`, participants)
- implemented now: extraction pipeline carries source attribution (`source_channel`, `source_conversation_id`, `source_author_id`, actor/subject IDs)
- remaining: robust canonical identity resolution for actor/subject assignment (policy-driven, not heuristic-only)

Phase 3: Read-path mux
- status: partially implemented
- retrieval APIs accept viewer/source/participants
- implemented now: scoped filters in recall API/MCP (`actor_id`, `subject_entity_id`, `source_*`, `include_unscoped`)
- remaining: viewer-aware privacy policy gates and participant-aware ranking policy

Phase 4: Janitor identity maintenance
- register datastore-owned identity maintenance routine(s)
- enforce invariants and reporting

Phase 5: hardening
- E2E suites + multi-user benchmarks
- remove deprecated single-user shortcuts

## 13) Benchmark and Test Plan

New benchmark class: `multi_user_agentlife`.

Scenarios:

- DM alias continuity: same user with different handles across channels.
- Group attribution: facts from A/B/C must be recalled per speaker correctly.
- Privacy containment: forbidden cross-user recall attempts must fail safely.
- Context switching: rapid source changes with correct memory mux.
- Conflict resolution: contradictory statements by different users tracked separately.

Metrics:

- attribution precision/recall
- leakage rate (must trend to 0)
- alias-link precision
- recall relevance under mux constraints
- janitor identity queue convergence time

E2E additions (planned):

- multi-source synthetic conversation generator
- deterministic alias mapping fixtures
- privacy gate assertions

## 14) Risks and Mitigations

Risk: index/query latency increase with added filters.
Mitigation: composite indexes + candidate caps + staged retrieval.

Risk: incorrect alias merges.
Mitigation: confidence thresholds + review queue + reversible links.

Risk: privacy regressions.
Mitigation: policy test matrix + deny-by-default for unknown scope.

Risk: adapter metadata inconsistency across hosts.
Mitigation: strict normalized ingest envelope with required/optional fields.

## 15) Open Questions

- Should subject default to speaker when ownership inference is unclear, or always require explicit confidence threshold?
- Do we need per-fact ACL overrides in v1, or can scope+sensitivity handle launch needs?
- Should source-shared memory ever be promoted to global automatically?
- How should agent-to-agent swarm memory ownership be represented (individual vs collective entity)?

## 16) Recommended Immediate Next Steps

1. Review and lock this spec with benchmark + adaptor owners.
2. Write an ADR for identity model and privacy decision matrix.
3. Define minimal v1 scope for post-benchmark implementation cut.
4. Design `multi_user_agentlife` benchmark harness in parallel with implementation prep.
