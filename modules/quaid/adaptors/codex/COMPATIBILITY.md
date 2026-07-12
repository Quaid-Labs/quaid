# Codex Compatibility Notes

## `/new` extraction latency

Symptom: an immediate question after `/new` can miss facts from the prior session.

Cause: Codex does not materialize the session boundary until the first follow-up prompt. Quaid starts async extraction at that point, and recall for the same follow-up can run before extraction lands.

Action: ask again after a moment, or use a sacrificial first prompt after `/new` when testing boundary-sensitive recall.

## Parallel agents in one Codex instance

Symptom: multiple agents sharing one CDX-backed Quaid instance can make extraction appear early or out of phase.

Cause: Codex has no compact/session-boundary hook equivalent, so Quaid relies on async lifecycle signals around `/new`, `Stop`, and follow-up prompts.

Action: for parallel work or isolation-sensitive testing, use separate Quaid instances instead of multiple agents sharing one CDX instance.

## No compact hook

Symptom: updated identity or project context may not appear immediately in a long-lived Codex session.

Cause: Codex does not expose a compaction hook. Quaid refreshes system context on the daemon timeout path instead.

Action: wait for timeout refresh or start a fresh turn after the boundary has materialized.

## Visible injected context

Symptom: Quaid startup or memory context may be visible in the Codex host view.

Cause: Codex displays hook-provided `additionalContext`.

Action: treat it as normal Quaid context, not user-authored text.
