# EGO Core 0001 (Draft)

Status: Draft  
Scope: Platform-agnostic interchange envelope for portable agent identity/memory packages.

Community discussion thread: https://github.com/Quaid-Labs/quaid/discussions/3

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
      "path": "artifacts/...",
      "size_bytes": 123,
      "sha256": "...",
      "depends_on": [],
      "suggests": []
    }
  ],
  "integrity": {
    "root_hash": "..."
  },
  "signatures": [
    {
      "id": "sig_...",
      "key_id": "key_...",
      "alg": "ed25519",
      "scope": "manifest",
      "sig": "base64..."
    }
  ],
  "signer_identities": [
    {
      "key_id": "key_...",
      "name": "optional human label",
      "issuer": "optional",
      "fingerprint": "optional"
    }
  ],
  "trust_hints": {
    "intended_trust_tier": "optional-non-authoritative"
  }
}
```

`trust_hints` are non-authoritative metadata only. Importers MUST NOT treat
package-provided trust claims as sufficient for trusted execution.

## Artifact Model

Each artifact is type-addressable and versioned:

- `type` describes semantics (for example `memory.graph`, `identity.profile`, `project.bundle`)
- `version` is the artifact schema version for that type
- `depends_on` declares hard artifact/capability dependencies for valid import
- `suggests` declares soft relationships the importer may use for better fidelity

This allows exotic/new artifact types without breaking older importers.

Suggested class split:

- Host-neutral capability artifacts (preferred)
  - examples: `identity.profile`, `memory.graph`, `knowledge.bundle`, `policy.behavior`
- Host-specific artifacts (optional)
  - examples: `host.instructions.claude_md`, `host.instructions.agents_md`

Host-specific artifacts must declare target host/runtime compatibility in
artifact metadata or compatibility profile sections.

## Modular Export + Import

EGO packages are modular by design.

This means two different operations are first-class:

- selective export: exporter may emit only a chosen subset of artifacts
- selective import: importer/operator may apply only a chosen subset of artifacts

Examples:

- project-only package
- memory-only package
- identity/personality-only package
- partial import from a larger full-identity package

Validity is determined by dependency resolution, not by a package-wide notion
that every artifact class must always be present.

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

Importers should also support artifact selection:

- by `artifact_id`
- by `type`
- by profile/capability group

If an operator selects a subset that leaves unresolved hard dependencies, the
importer should fail that selected import set with a clear dependency report.

All imports should emit a machine-readable report of:

- imported artifacts
- skipped artifacts
- conflicts and resolutions
- capability mismatches

Importers should also emit parity notes:

- host-specific artifacts applied
- host-specific artifacts skipped
- expected behavior drift vs source package

## Principal Binding + Artifact-Scoped Migration

To support portability across different local users/environments, EGO should
define identity bindings and transform rules explicitly in manifest metadata.

Baseline model:

- `principals`: package-scoped logical identities (for example `principal.owner`)
- `bindings_required`: principal IDs importer must resolve before apply
- `migrations`: declarative transform rules with artifact selectors

This enables export/import workflows like:

- Export: local owner values are normalized to package principals
- Import: package principals are bound to host/local identities before write

Artifact-scoped migration is required so transforms can target only relevant
types (for example memory artifacts) and skip unrelated types (for example
project bundles).

Example (illustrative only):

```json
{
  "principals": [
    { "id": "principal.owner", "role": "owner" }
  ],
  "bindings_required": ["principal.owner"],
  "migrations": [
    {
      "id": "owner-remap-v1",
      "type": "org.ego.migrate.owner_remap/v1",
      "required": true,
      "targets": {
        "artifact_types": ["memory.graph", "memory.db"],
        "artifact_ids": [],
        "field_paths": ["owner_id", "edges[].owner_id"]
      },
      "params": {
        "from_principal": "principal.owner",
        "to_binding": "host.current_user"
      }
    }
  ]
}
```

Migration robustness rules:

- Unknown required migration types MUST fail import.
- Unknown optional migration types MAY be skipped with warning.
- Import reports MUST include migration actions and field-level counts.

## Stability Contract (Anti-Fragmentation)

To reduce ecosystem breakage from incompatible package variants:

- `ego_version` is mandatory and semver-like.
- Unknown top-level manifest fields are allowed.
- Unknown required capabilities MUST fail import.
- Unknown optional capabilities MUST be skipped with warning.
- Every artifact MUST include `id`, `type`, `version`, `path`, and `sha256`.
- Importers MUST provide deterministic import reports.
- Proposed new artifact types should be namespaced to avoid collisions.
- Packages MUST NOT rely on undeclared implicit migrations.
- Required principal bindings MUST resolve before any apply-mode import.
- Hard artifact dependencies MUST be explicit via `depends_on`.

## Signature + Trust Model

EGO trust is verifier-side, not package-side.

Baseline requirements:

- Signatures must bind to canonical manifest bytes (or canonical package digest).
- `key_id` must map to a verifier-local trust store entry.
- Package-provided signer labels are informational only.
- Invalid signature, unknown key, or revoked key MUST downgrade package to
  untrusted state.

Recommended verifier behavior:

- Maintain a local trusted keyring (pinned public keys / certs).
- Support explicit trust onboarding for unknown keys.
- Support key revocation and key rotation.
- Record signature verification result in import report.

Trust decision source:

- verifier-local policy + cryptographic verification result
- not package-authored trust declarations

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

## External Dependency + Execution Policy

EGO packages should be self-contained by default.

Threat model note: external fetch/execute behavior can convert a reviewed package
into an unreviewed runtime payload chain.

Baseline policy (recommended for core):

- No implicit network fetch during import.
- No implicit package install during import.
- No implicit external code execution during import.
- Any external dependency references must be explicit manifest entries and
  treated as unresolved until operator-approved.

If an implementation chooses to support external references:

- They must be non-default and explicit opt-in.
- They must require digest-pinned targets and trust-policy checks.
- Import reports must enumerate every external reference action.
- Safe mode must still refuse external execution/fetch by default.

## Draft Position

This document is a staking draft for an open direction.
It is intentionally conservative and extensible.
Quaid may implement this profile, but EGO Core is intended to remain platform-agnostic.
