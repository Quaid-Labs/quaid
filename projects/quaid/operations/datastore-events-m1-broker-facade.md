# Datastore Events M1 Broker Facade

Status: initial M1 slice
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Scope

M1 evolves the existing queue-backed event bus in `core/runtime/events.py`. It does not introduce a second broker, queue, registry, or dispatch loop.

The current production `emit_event()` and `process_events()` paths remain compatible. Producers are not migrated in this milestone.

Solomon direction on 2026-05-15: compatibility is not a goal for migrated datastore refactor paths. The existing emit/process path remains only because M1 does not migrate producers. When a later milestone migrates a producer to the broker, the old direct producer path should be deleted in that milestone unless an operator-approved alpha-user shim is explicitly needed.

## Event Envelope

Newly emitted events use a v1 envelope with these fields:

- `id`
- `name`
- `event_type`
- `event_class` (`domain` or `request`)
- `schema_version`
- `source`
- optional `instance_id`
- optional `project_id`
- optional `session_id`
- optional `owner_id`
- optional `correlation_id`
- optional `idempotency_key`
- `created_at`
- `payload`
- `provenance`
- `priority`
- `status`

During M1, `name` and `event_type` intentionally match so existing queue consumers can continue reading `name`.

Request events are detected by a `request` segment in the event type, for example `recall.memory.request.v1`. Broker-emitted request events receive a correlation id automatically if the caller does not provide one.

## Broker Facade

The M1 facade is intentionally thin:

- `EventBroker.emit(...)`
- `EventBroker.dispatch(...)`
- `emit_broker_event(...)`
- `dispatch_broker_events(...)`
- `validate_event_envelope(...)`

The facade validates the envelope at the broker boundary, then uses the existing queue and handler dispatch underneath.

New M1/M2/M3 work should call the broker facade rather than adding new callers to the legacy-shaped `emit_event()` surface.

## Failure Policy

Invalid broker envelopes raise under `failHard=true`.

With `failHard=false`, invalid broker envelopes are logged loudly and annotated with `validation_errors`. This keeps the fail-soft behavior explicit without hiding a broken event contract.

Handler failures still use the existing `process_events()` failHard behavior.

## Duplicate Filtering

M1 adds queue-resident duplicate filtering for events that provide `idempotency_key`.

Dedup scans all retained queue entries, including `pending`, `processed`, and `failed` events. Queue entries persist across process restarts and session boundaries within the same instance runtime root, and eviction happens only through the `MAX_EVENT_QUEUE` ceiling.

If any retained event with the same `event_type` and `idempotency_key` is still present in the queue file, the new event is not appended and the caller receives the existing event annotated with:

- `duplicate: true`
- `duplicate_of: <existing event id>`

Cross-session suppression is a known behavior of this M1 queue-resident dedupe. Callers that need per-session idempotency must include the session identity in the idempotency key.

This is not a durable replay ledger. Durable idempotency belongs in later datastore listener/base-class milestones.

## Broker Tracing

The existing history file remains `.runtime/events/history.jsonl`.

Legacy emit/process paths keep their current history operations. Broker facade calls add M1-specific trace operations:

- `broker.emitted`
- `broker.duplicate`
- `broker.dispatched`
- `broker.acked`
- `broker.failed`

These traces are for M1 observability and testability. Production producers are not required to call the broker facade yet.

They are not a dual-run compatibility mechanism. Once a producer is migrated, the broker trace becomes the canonical trace for that producer.

## Non-Goals

M1 does not:

- migrate adapters, daemon, janitor, docs, or recall producers
- switch recall to request events
- add a distributed queue or replay log
- add datastore manifests
- move datastore write ownership

Those belong to later milestones.
