# Datastore Events M2 Manifest Registry

Status: initial M2 slice
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Scope

M2 adds a core-owned, static datastore manifest registry. It is metadata-only.

This milestone does not:

- activate datastore handlers
- migrate recall routing
- migrate monitor writes
- replace plugin manifests
- add dynamic third-party datastore loading

## Runtime Surface

The registry lives in `core/datastore_registry.py`.

Debug CLI:

```bash
quaid datastore list
quaid datastore list --json
quaid datastore show memorydb --json
quaid datastore capabilities --json
```

The CLI reads static manifest metadata only. It does not open datastore files or import datastore implementation modules.

## First-Party Manifests

Registered first-party datastore ids:

- `memorydb`
- `docsdb`
- `evolutiondb`

`evolutiondb` is the canonical datastore id for the current `datastore.notedb` runtime implementation. The runtime package is not renamed in M2; the manifest records `runtime_aliases: ["notedb"]`.

## SessionDB Decision

`sessiondb` is not registered as a first-party datastore manifest in this M2 slice.

Reason: the original migration plan covers memory/docs/evolution first. SessionDB is durable and has a CLI namespace, but today it participates through `core.services.session_memory_bridge` as internal transcript/provenance plumbing. Adding it as a first-party manifest would widen M2 before request-event routing and capability ownership are proven.

Revisit before M4/M6 when request-event recall capability routing starts to distinguish datastore implementation ids from user-facing store/capability names such as `session_chunks`.

## Manifest Fields

Each manifest declares:

- `id`
- `display_name`
- `description`
- `module`
- `plugin_id`
- `schema_version`
- `capabilities`
- `accepted_events`
- `request_handlers`
- `produced_events`
- `maintenance_tasks`
- `migrations`
- `worker_specs`
- `resource_budgets`
- `fail_hard_policy`
- `contracts`

In M2, `accepted_events` and `request_handlers` describe the manifest contract metadata the broker will use in later milestones. They are not active dispatch registrations yet.

## Validation

The registry validates:

- duplicate datastore ids
- missing required fields
- unsupported schema versions
- invalid id/module shapes
- malformed list/object fields
- missing contract version keys

Invalid manifests raise under `failHard=true`. With `failHard=false`, invalid manifests are logged loudly and skipped from the returned registry.
