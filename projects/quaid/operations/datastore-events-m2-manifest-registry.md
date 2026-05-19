# Datastore Events M2 Manifest Registry

Status: implemented; static first-party manifest registry available
Owner: W1 runtime/datastore
Plan source: `~/quaidcode/util/docs/datastore-events-migration-plan.md`

## Scope

M2 adds a core-owned, static datastore manifest registry. It is metadata-only.

Solomon direction on 2026-05-15: the datastore refactor should prefer clean broker/datastore contracts over preserving legacy callpaths. M2 therefore exposes metadata only; it does not add compatibility adapters around existing direct calls.

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

Revisit in a dedicated sessiondb/source-window slice. M4/M6 separated request-event recall capability selectors from datastore implementation ids without adding `sessiondb` as a first-party manifest.

Post-M14 update: the metadata-only SessionDB first-party manifest slice closed
at `f0574902b` + `522f16e28` and is recorded in
`projects/quaid/operations/datastore-events-m14-sessiondb-manifest-plan.md`.
That closure does not approve lifecycle persistence, `session.ingest_log`
ownership changes, or source-window behavior changes.

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

When later milestones activate these handlers, the corresponding old direct callpath should be removed in the same milestone unless an operator-approved alpha compatibility shim is explicitly required.

## Validation

The registry validates:

- duplicate datastore ids
- missing required fields
- unsupported schema versions
- invalid id/module shapes
- malformed list/object fields
- missing contract version keys

Invalid manifests raise under `failHard=true`. With `failHard=false`, invalid manifests are logged loudly and skipped from the returned registry.
