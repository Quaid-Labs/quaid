# Livetest Agent-Messaging API

Tiny fictional project used as M4 test fixture. Exposes a thin Python API for
routing text messages between named agents. Purpose: give M4 Part A a real-looking
registered source file so `TOOLS.md` population, doc chunking, and docs recall can
be exercised end-to-end against something that looks like a small real codebase.

The project name used in M4 is `livetest-agentmsg-<PLATFORM>` (platform prefix so
concurrent per-platform runs don't collide in the global docs index).

## Files

- `README.md` — this overview (high-level, non-API content).
- `agentmsg/__init__.py` — package marker with a short module docstring.
- `agentmsg/api.py` — public API surface (`send`, `subscribe`, `Mailbox`) that
  TOOLS.md should pick up.
- `agentmsg/examples.md` — hand-written usage examples; gives docs recall a
  distinct chunk to match.

## Distinctive keywords

M4 / M8 probes search for:

- `Mailbox.deliver` (method name on the Mailbox class)
- `cobalt-postage` (invented codeword in examples.md for docs recall probes)
- `agentmsg.api.send` (dotted path form)
