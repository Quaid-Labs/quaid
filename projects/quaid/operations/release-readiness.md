# Release Readiness

## Positioning

Quaid is close to a **public alpha OSS release** with a clear preferred backend,
explicit known issues, and a release gate that is now mostly operational rather
than architectural.

Recommended framing:
- "Strong public alpha: local-first memory that is stable enough to use, with active hardening still in progress."
- "Anthropic-backed lanes are the recommended default today; OpenAI-backed lanes remain available but experimental."

## Go / Hold

### Go now (public alpha)
- Deterministic integration and mock-core tests are in place.
- Installer and runtime flows now cover all three launch adapters:
  - OpenClaw
  - Claude Code
  - Codex
- Shared auth plumbing is now simpler:
  - first install provisions shared credentials
  - later installs reuse them instead of asking lane-by-lane
- Anthropic-backed service calls are the recommended path based on current live
  and benchmark quality.
- OpenAI-backed service calls remain supported for alpha, but should be clearly
  labeled experimental in install and release messaging.
- Final release approval still requires:
  - full current test bar
  - full current live suite clear, using the current definition in
    `modules/quaid/tests/livetest/livetest-guide/`
  - release evidence recorded for `unit`, `ci`, and `xp`
  - SHA comparison between the cleared run and current `HEAD`, with Solomon
    deciding whether any post-clear changes are acceptable

### Hold for broader release until
- Command API path fully replaces slash-text fallback paths.
- Janitor modes split (`memory-maintenance` vs `workspace-editing`) to avoid incidental doc rewrites.
- OpenAI-backed lanes close more of the current quality gap versus Anthropic, or are narrowed further in public positioning.
- Remaining compatibility shims are removed from active import paths (notably `core/docs/*` shim entrypoints).

## Release Gate Doc

- Canonical go/no-go checklist: `projects/quaid/operations/release-checklist.md`

## Known Issues To Publish

- Anthropic is the recommended Quaid backend today. OpenAI-backed lanes remain available, but current tests show materially worse memory quality and they should be labeled experimental.
- Some runtime/gateway restore flows can still require a restart retry.
- Janitor scope currently includes workspace editing unless explicitly constrained.
- OpenClaw typed plugin hooks can miss `before_reset` across bundle boundaries (upstream: https://github.com/openclaw/openclaw/issues/23895). Quaid mitigates reset/new extraction via internal workspace command hooks (`command:new`, `command:reset`) while compaction remains on `before_compaction`.
- Janitor apply-mode E2E can block on approval-policy `ask`; this is expected unless policy is pinned to non-interactive behavior for the run.
- Legacy contradiction task surface is retained for compatibility, but contradiction detection/resolution is decommissioned in active janitor `--task all` flow (stale handling is supersession/recency based).
- Current release readiness is blocked on fresh evidence, not missing release infrastructure:
  - record `unit`, `ci`, `xp`
  - finish the current live run
  - approve or rerun any post-clear delta

## Notification Posture

- Keep recommended default as `normal`:
  - `janitor: summary`
  - `extraction: summary`
  - `retrieval: off`
- Use feature-level overrides for power users instead of a single global toggle.
- Route asynchronous actionable janitor health requests through adapter-managed delayed request queues.

## Immediate Post-Release Priorities

1. Tighten OpenAI-backed service quality or narrow public support posture further.
2. Command-API-only control flow.
3. Retrieval quality uplift for relationship/family queries (fact + graph parallel composition).
4. Cloud embeddings option (lower setup friction).
5. Graph/config UX surfaces (visualizer + dashboard).
6. Project-link hygiene: add an automated stale-link cleanup path that can suggest/perform unlink for projects an instance no longer queries over a sustained window (with explicit opt-in guardrails).

## Contributor Call

Good first contribution tracks:
- Session control and hook reliability.
- Retrieval result shaping and graph traversal quality.
- Provider matrix automation and CI hardening.
- Janitor mode boundaries and safety controls.
