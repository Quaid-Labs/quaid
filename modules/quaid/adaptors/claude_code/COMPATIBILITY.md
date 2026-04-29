# Claude Code Compatibility Notes

Source: `docs/COMPATIBILITY.md`. Keep this short and action-oriented.

## Async lifecycle extraction

Symptom: a recall immediately after `/compact`, `/clear`, or session end can miss facts from the just-ended content.

Cause: Claude Code does not provide compaction-control blocking. Quaid queues extraction asynchronously through hooks and the daemon.

Action: if a just-ended fact is missing, wait a few seconds and retry recall.

## Deferred notices

Symptom: Quaid provider, janitor, or project-doc notices can appear on a later turn instead of live.

Cause: Claude Code does not provide the same live notification channel as OpenClaw.

Action: when Quaid injects a notice, relay it briefly to the user before answering.
