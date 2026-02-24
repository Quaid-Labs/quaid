---
name: quaid-reset-signal
description: "Queue Quaid reset extraction signals from reliable command hooks."
metadata:
  {
    "openclaw":
      {
        "emoji": "🧠",
        "events": ["command:new", "command:reset", "command:compact"]
      }
  }
---
# Quaid Reset Signal

Queues extraction signals from command hooks:
- `/new` and `/reset` => `ResetSignal`
- `/compact` => `CompactionSignal`

Uses OpenClaw internal command hooks (`command:*`) so extraction still runs
when typed plugin lifecycle hooks are skipped across bundle boundaries.
