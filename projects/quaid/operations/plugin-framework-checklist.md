# Plugin Framework Implementation Checklist

Purpose: convert the plugin framework notes into an execution plan with clear prelaunch-safe scope.

## Current State (Already Done)

- `core/runtime/plugins.py` exists with:
  - strict manifest validation,
  - discovery by configured paths,
  - conflict-safe registry/singleton activation.
- Config already seeds plugin controls in `config/memory.json` via `config.py`:
  - `plugins.enabled`, `plugins.strict`, `plugins.apiVersion`,
  - `plugins.paths`, `plugins.allowList`, `plugins.slots`.
- Datastore manifests are already enforced to declare:
  - `supports_multi_user`,
  - `supports_policy_metadata`,
  - `supports_redaction`.

## Phase 1 (Prelaunch Safe, Do Now)

1. Plugin Boot Preflight (no runtime takeover)
- Add a boot/preflight check that discovers manifests and validates them.
- Keep behavior non-owning: no switching active control flow to plugin runtime yet.
- Fail behavior:
  - `plugins.strict=true`: hard fail on invalid manifest/conflict.
  - `plugins.strict=false`: continue with loud structured warnings.

2. First-Party Manifest Coverage
- Add/verify first-party `plugin.json` files for:
  - OpenClaw adapter,
  - primary ingest path,
  - memory/docs/note datastores.
- Ensure IDs are stable and slot-compatible.

3. Conformance Test Baseline
- Add contract tests for each plugin type:
  - manifest required fields and capability typing,
  - slot conflict rejection,
  - discovery allowlist behavior,
  - strict vs non-strict error handling.

4. Observability
- Emit startup report:
  - discovered plugins,
  - enabled/disabled set,
  - slot assignments,
  - validation errors/warnings.
- Keep this in logs only (no behavior change).

## Phase 2 (Prelaunch Optional, Low Risk)

1. Registry-Backed Read-Only Wiring
- Resolve configured plugin slots through registry at startup.
- Still call current first-party implementations directly (no dynamic import takeover yet).
- This validates config+registry coherence without migration risk.

2. Capability Gate Checks
- On startup, assert required capabilities for active datastore slots.
- Hard-fail only when `plugins.strict=true`.

## Phase 3 (Postlaunch / When Ready)

1. Runtime Takeover
- Move adapter/ingest/datastore activation to registry-driven dynamic loading.
- Remove remaining hardcoded built-in activation paths.

2. External Plugin Support
- Open documented third-party plugin path.
- Add signed/distribution guidance, compatibility policy, and migration/version policy.

## Non-Goals Right Now

- Do not add broad plugin execution sandboxes prelaunch.
- Do not implement multi-tenant plugin permission engines prelaunch.
- Do not migrate all call sites to dynamic loading before benchmark/release stabilization.

## Validation Gate (Required Before Advancing Beyond Phase 1)

- `npm run -s check:boundaries` passes.
- Plugin contract tests pass.
- Existing janitor/failHard/provider suites remain green.
- E2E janitor dry-run and one apply-mode run remain green with no plugin-induced behavior drift.

## Files To Use As Source of Truth

- `docs/PLUGIN-SYSTEM.md`
- `modules/quaid/core/runtime/plugins.py`
- `modules/quaid/config.py`
- `docs/MULTI-USER-MEMORY-SPEC.md` (policy/capability requirements)
