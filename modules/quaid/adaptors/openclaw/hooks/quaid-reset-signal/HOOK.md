---
name: quaid-reset-signal
description: "Queue Quaid reset extraction signals from reliable command hooks."
metadata:
  {
    "openclaw":
      {
        "emoji": "🧠",
        "events": ["command:new", "command:reset"]
      }
  }
---
# Quaid Reset Signal

Queues a `ResetSignal` for the previous session when `/new` or `/reset` runs.
Uses OpenClaw internal command hooks (`command:new`, `command:reset`) so it
works even when typed plugin `before_reset` hooks are skipped across bundle
boundaries.
