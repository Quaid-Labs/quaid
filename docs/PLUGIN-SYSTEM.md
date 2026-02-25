# Plugin System (Phase 1 Foundation)

Status: Seeded foundations, runtime takeover not enabled yet.

## Purpose

Quaid must remain small in core and extensible at the boundaries.  
Plugins are the long-term mechanism for extension in three zones:

- adapters
- ingest pipelines
- datastores

MCP/CLI remain interface surfaces. They are not a replacement for internal plugin contracts.

## Contract (v1)

Each plugin provides a `plugin.json` manifest:

- `plugin_api_version` (required, integer)
- `plugin_id` (required, unique, stable)
- `plugin_type` (required: `adapter` | `ingest` | `datastore`)
- `module` (required, import path)
- `entrypoint` (optional, default `register`)
- `capabilities` (optional object)
- `dependencies` (optional array)
- `priority` (optional integer, default `100`)
- `enabled` (optional bool, default `true`)

## Core runtime module

Phase 1 introduces:

- `core/runtime/plugins.py`
  - strict manifest validation
  - manifest discovery from configured plugin paths
  - registry with:
    - plugin ID conflict prevention
    - singleton slot conflict prevention (for single-owner slots)

This module is intentionally dormant by default; it seeds architecture, validation, and tests without changing current production control flow.

## Config (seeded)

`config/memory.json` now supports:

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
      "dataStores": []
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

