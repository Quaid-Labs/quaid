# OpenClaw Compatibility Notes

Source: `docs/COMPATIBILITY.md`. Keep this short and action-oriented.

## Compaction-controlled refresh

Symptom: identity or project context can look stale until the session refreshes.

Cause: OpenClaw supports Quaid's compaction-control path, so `/compact` is the preferred explicit refresh boundary.

Action: after editing identity or project context, use `/compact` before relying on the refreshed context.

## Codex-family extraction models

Symptom: facts are stored but relationship-heavy graph recall can be weak immediately after extraction when OpenClaw is configured with OpenAI Codex-family OAuth models.

Cause: those models do not always emit relationship hints in Quaid's structured extraction format.

Action: use direct recall for exact facts; janitor backfill can restore graph edges from stored facts.
