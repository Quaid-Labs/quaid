# Notification Strategy

Quaid now supports feature-level notification controls instead of one global behavior.

## Features

- `janitor`: nightly maintenance reports and health warnings
- `extraction`: memory extraction completion messages
- `retrieval`: recall/injection context notifications

Each feature supports:
- `off`
- `summary`
- `full`

## Recommended Preset (Normal)

`normal` is the recommended default for alpha:
- `janitor: summary`
- `extraction: summary`
- `retrieval: off`

This keeps high-signal maintenance and extraction visibility while avoiding per-turn recall noise.

## Other Presets

- `quiet`: all features `off`
- `verbose`: `janitor=full`, `extraction=summary`, `retrieval=summary`
- `debug`: all features `full`

When `notifications.extraction` is `off` (for example `quiet` preset), extraction-completion notifications are fully suppressed, including zero-result `/reset` or timeout completion summaries.

## Delayed Requests (Host-Managed)

For host systems with asynchronous/heartbeat workflows (for example OpenClaw), delayed actionable requests are written to:

- `.quaid/runtime/notes/delayed-llm-requests.json`

Host adapters are responsible for surfacing and resolving these requests in a user-safe channel.

In OpenClaw, the adapter can queue delayed requests and HEARTBEAT instructions can process them at an appropriate time.

## Janitor Health Escalation

If janitor appears unhealthy (never run, or stale), Quaid queues a delayed high-priority request through the adapter-owned delayed-request path instead of spamming immediate extraction notifications.

## Configuration

Use either:

- `quaid config edit` (interactive)
- `quaid config set notifications.<feature>.verbosity <off|summary|full>`

Example:

```bash
quaid config set notifications.janitor.verbosity full
quaid config set notifications.retrieval.verbosity off
```
