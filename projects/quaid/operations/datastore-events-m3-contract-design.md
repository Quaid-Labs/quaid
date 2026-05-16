# Datastore Events M3 Contract Design

Status: pre-implementation direction
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Direction Change

Solomon direction on 2026-05-15: do not preserve legacy callpaths for the datastore refactor. Prefer clean broker/datastore contracts and delete migrated direct paths instead of carrying compatibility shims.

M3 should therefore define the datastore contract surface as the target runtime interface, not as a second wrapper layer around legacy direct calls.

## M3 Scope

M3 adds:

- datastore contract/base types for manifest metadata, listener registration, request-handler registration, health, validate, maintenance, export, import, and explain
- idempotency helper semantics for datastore listeners
- ack/nack helper semantics
- conformance tests for first-party contract implementations
- first-party contract declarations for `memorydb`, `docsdb`, and `evolutiondb` that align with the M2 manifests and identify replacement targets for each declared handler

M3 does not:

- add no-op wrappers whose only job is to call existing direct functions
- activate production routing before a migration milestone
- keep dual production paths after a path is migrated
- add compatibility aliases beyond explicit alpha-user shims with owner/removal conditions

## Replacement Rule

Each M3 contract wrapper must identify the direct path it is intended to replace before it can be used by a later milestone.

When a later milestone activates that wrapper for production, the old direct callpath should be removed in the same commit series unless an operator-approved compatibility shim is required for installed alpha state.

The M3 contract declarations are intentionally inactive in production. If a caller invokes an inactive M3 handler method directly, it returns a `nacked` result stating `handler not activated in M3`; it does not call the old direct datastore function.

## Testing Rule

Conformance tests should exercise the new contract surface directly. They should not prove that the old direct path still works except where that direct path remains active because the milestone has not migrated it yet.

M3 conformance checks:

- first-party contract ids match M2 manifest ids
- declared domain listeners and request handlers match M2 manifest metadata
- every handler spec identifies at least one direct path it is meant to replace
- ack/nack payload shape is stable
- process-local idempotency helper is scoped by datastore id
