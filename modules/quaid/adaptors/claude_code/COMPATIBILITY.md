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

## Rules refresh after `/compact`

Symptom: freshly edited identity files may not appear through `.claude/rules/quaid-*.md` alone on the first post-compact question.

Cause: Claude Code loads rules files at session start, but current 2.1.x builds do not reliably reload rewritten rules files into model-visible context immediately after `/compact`.

Action: Quaid sends a small identity-only bridge on the next turn after `/compact`; answer from that identity context when relevant. If project/tool context still looks stale, ask again after a moment or start a fresh session.
