# GitHub Discussion Draft: EGO Portable Agent Identity Standard (Proposal)

Title:

`Proposal: EGO (.ego) — Open, platform-agnostic package format for portable agent identity`

Body:

We are proposing an open draft standard called **EGO** (`.ego`) for portable agent identity packages.

Goal:

- Export an agent identity package from one runtime/host (for example Codex)
- Import it into another runtime/host (for example Claude Code)
- Preserve behavior + knowledge parity as closely as possible

This is a proposal thread for open discussion, not a released feature commitment.
Quaid implementation is planned on the roadmap, but EGO is intended to be **platform-agnostic**.

## Why

Current memory/identity systems are fragmented and runtime-specific.
We want a neutral interchange envelope for:

- personality/behavior policy
- long-term memory structures
- project/domain knowledge bundles
- future capability artifacts not yet invented

## Draft Spec

Draft document:

- [EGO Core 0001 (Draft)](./EGO-CORE-0001-draft.md)

Highlights:

- Manifest-first contract
- Typed, versioned artifacts
- Modular export and modular import
- Dependency-driven validation instead of all-or-nothing package assumptions
- Signature-based signer identity (verifier-side trust)
- Required vs optional capability negotiation
- Principal binding + artifact-scoped migration rules
- Deterministic import reports
- Explicit host-specific artifacts (no hidden coupling)
- Default-deny external dependency/execution posture

## Cross-Platform Parity Requirement

A core target is cross-host parity:

- Build/export on Host A
- Import on Host B
- Preserve equivalent behavior and knowledge where possible

Host-specific instruction files are allowed as explicit typed artifacts, while
host-neutral capability artifacts remain the preferred baseline.

## Security + Privacy Position

- Loading `.ego` should be treated as high-risk content handling.
- Dry-run preview and explicit confirmation should be the default import posture.
- Privacy/sanitization metadata should be part of package provenance.

We especially want security and privacy experts to challenge this model early.

## Open Questions

1. What minimum manifest fields should be strictly required for ecosystem stability?
2. How should we registry-govern artifact types to avoid fragmentation?
3. What import safety defaults should be mandatory vs implementation-defined?
4. What parity guarantees are realistic across very different hosts?
5. What sanitization attestations are practical and verifiable?

If this direction resonates, feedback on the draft schema and threat model is the most valuable next step.
