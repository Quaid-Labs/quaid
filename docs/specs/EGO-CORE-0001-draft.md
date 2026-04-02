# EGO Core 0001 (Draft)

Status: Draft  
Scope: Platform-agnostic interchange envelope for portable agent identity/memory packages.

## Intent

`EGO` is proposed as an open, host-neutral package format for moving agent identity state between runtimes.
It is not tied to a single product implementation.

Goals:

- Portable across systems and hosts
- Extensible via typed artifacts and capability declarations
- Auditable and deterministic to import
- Compatible with partial import when unknown optional types are present

Non-goals in this draft:

- Defining every memory schema for every system
- Mandating one merge policy for all runtimes
- Committing to a fixed compression/encryption stack forever

## Core Structure

An `.ego` package is a container with:

1. `manifest.json` (required)
2. `artifacts/` (required; typed payload blobs/files)
3. `signatures/` (optional)
4. `README.md` (optional human-facing notes)

The container encoding (zip/tar/other) is intentionally not fixed in this draft.
The manifest is the canonical contract.

## Compatibility Goal

Primary target behavior:

- An identity package exported from one host/runtime (for example Codex) should
  be importable into a different host/runtime (for example Claude Code) with
  equivalent agent behavior as closely as possible.

Core principle:

- EGO defines portable capability artifacts.
- Host-specific assets are allowed, but must be explicit and typed, never hidden.

## Manifest (Core Fields)

```json
{
  "ego_version": "0.1.0-draft",
  "package_id": "ego_...",
  "created_at": "2026-04-02T00:00:00Z",
  "created_by": {
    "name": "tool-or-author",
    "uri": "optional"
  },
  "provenance": {
    "source_system": "optional",
    "source_system_version": "optional",
    "export_tool": "optional",
    "export_tool_version": "optional"
  },
  "compatibility": {
    "required_capabilities": [],
    "optional_capabilities": [],
    "profiles": [],
    "host_targets": []
  },
  "artifacts": [
    {
      "id": "artifact_...",
      "type": "memory.graph",
      "version": "1",
      "required": false,
      "path": "artifacts/...",
      "size_bytes": 123,
      "sha256": "..."
    }
  ],
  "integrity": {
    "root_hash": "..."
  }
}
```

## Artifact Model

Each artifact is type-addressable and versioned:

- `type` describes semantics (for example `memory.graph`, `identity.profile`, `project.bundle`)
- `version` is the artifact schema version for that type
- `required=true` means importer must support it or fail import
- `required=false` means importer may skip it and continue

This allows exotic/new artifact types without breaking older importers.

Suggested class split:

- Host-neutral capability artifacts (preferred)
  - examples: `identity.profile`, `memory.graph`, `knowledge.bundle`, `policy.behavior`
- Host-specific artifacts (optional)
  - examples: `host.instructions.claude_md`, `host.instructions.agents_md`

Host-specific artifacts must declare target host/runtime compatibility in
artifact metadata or compatibility profile sections.

## Capability + Profile Model

- `required_capabilities`: importer must support these
- `optional_capabilities`: importer can ignore safely
- `profiles`: namespaced implementation mappings (for example `quaid/v1`) without making EGO product-specific
- `host_targets`: declared intended hosts/runtimes for parity expectations

Profile role:

- A profile maps host-neutral capability artifacts into concrete host/runtime
  surfaces while preserving core semantics.
- Importers should prefer host-neutral artifacts first, then apply host-specific
  overlays when compatible.

## Import Semantics (Baseline)

Importers should expose at least these modes:

- `safe`: no destructive overwrite, preserve existing state
- `merge`: deterministic merge with conflict reporting
- `replace`: explicit destructive restore (operator-confirmed)

All imports should emit a machine-readable report of:

- imported artifacts
- skipped artifacts
- conflicts and resolutions
- capability mismatches

Importers should also emit parity notes:

- host-specific artifacts applied
- host-specific artifacts skipped
- expected behavior drift vs source package

## Stability Contract (Anti-Fragmentation)

To reduce ecosystem breakage from incompatible package variants:

- `ego_version` is mandatory and semver-like.
- Unknown top-level manifest fields are allowed.
- Unknown required capabilities MUST fail import.
- Unknown optional capabilities MUST be skipped with warning.
- Every artifact MUST include `type`, `version`, `path`, and `sha256`.
- Importers MUST provide deterministic import reports.
- Proposed new artifact types should be namespaced to avoid collisions.

## Security + Privacy Hooks

Core supports (but does not mandate in this draft):

- detached signatures
- provenance attestations
- sanitization/redaction summary metadata
- optional encrypted-at-rest transport

Security stance:

- Loading an EGO package should be treated as high-risk content handling.
- Importers should default to dry-run preview + explicit operator confirmation.
- Unsafe host instruction artifacts should never auto-activate without clear consent.

Privacy stance:

- Export flows should emit sanitization/redaction metadata.
- Importers should surface sensitivity labels and redaction provenance to operators.

## Draft Position

This document is a staking draft for an open direction.
It is intentionally conservative and extensible.
Quaid may implement this profile, but EGO Core is intended to remain platform-agnostic.
