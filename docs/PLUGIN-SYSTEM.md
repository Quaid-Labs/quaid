# Plugin System (Phase 1 Foundation)

Status: Config-driven preflight is active; runtime takeover is not enabled yet.

Execution checklist: `projects/quaid/operations/plugin-framework-checklist.md`

## Purpose

Quaid must remain small in core and extensible at the boundaries.  
Plugins are the long-term mechanism for extension in three zones:

- adapters
- ingest pipelines
- datastores

CLI and adapter surfaces remain external interfaces. They are not a replacement for internal plugin contracts.

## Contract (v1)

Each plugin provides a `plugin.json` manifest:

- `plugin_api_version` (required, integer)
- `plugin_id` (required, unique, stable)
- `plugin_type` (required: `adapter` | `ingest` | `datastore`)
- `module` (required, dotted import path)
- `capabilities` (required object)
  - `display_name` (required string)
  - `contract` (required object — one entry per contract surface, each with `mode` and `handler` or `exports`)
- `dependencies` (optional array of plugin_id strings)
- `priority` (optional integer, default `100`)
- `enabled` (optional bool, default `true`)

### Contract surfaces

Every manifest must declare all contract surfaces under `capabilities.contract`.

**Executable surfaces** (mode must be `"hook"`, provide a `handler` reference):

- `init`
- `config`
- `status`
- `dashboard` (mode may be `"tbd"` until implemented)
- `maintenance`
- `tool_runtime`
- `health`

**Declared surfaces** (mode must be `"declared"`, provide an `exports` array):

- `tools`
- `api`
- `events`
- `ingest_triggers`
- `auth_requirements`
- `migrations`
- `notifications`

**Datastore plugins** additionally require top-level capability flags:
`supports_multi_user`, `supports_policy_metadata`, `supports_redaction` (all boolean).

## Core runtime module

Phase 1 introduces:

- `core/runtime/plugins.py`
  - strict manifest validation
  - manifest discovery from configured plugin paths
  - registry with:
    - plugin ID conflict prevention
    - singleton slot conflict prevention (for single-owner slots)

Runtime preflight is invoked from config boot when `plugins.enabled=true`. It validates discovery, registration, and configured slots without taking over adapter/ingest/datastore activation yet.

## Config (optional template)

`config/memory.json` supports this `plugins` block when explicitly configured:

```json
{
  "plugins": {
    "enabled": true,
    "strict": true,
    "apiVersion": 1,
    "paths": ["plugins"],
    "allowList": [],
    "slots": {
      "adapter": "",
      "ingest": [],
      "datastores": []
    }
  }
}
```

## Safety rules

- `strict=true`: malformed manifests or registration conflicts are boot errors.
- `strict=false`: discovery may continue with non-fatal errors; errors must still be surfaced loudly.
- singleton slots (for example active adapter) cannot have multiple active owners.

## Next phases

1. Register first-party built-ins through plugin contracts.
2. Move janitor lifecycle registration to plugin capability wiring.
3. Add conformance suite for adapter/ingest/datastore plugin contracts.
4. Open external plugin support only after first-party parity is complete.
