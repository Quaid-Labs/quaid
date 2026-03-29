# Platform Compatibility

This page summarizes what Quaid supports on each host integration path today.

## Current Platforms

- OpenClaw (`openclaw` adapter)
- Claude Code (`claude-code` adapter)
- Standalone CLI (`standalone` adapter)

## Capability Matrix

| Capability | OpenClaw | Claude Code | Notes |
|---|---|---|---|
| Core recall/store pipeline | Yes | Yes | Same core memory engine and datastore paths. |
| Hook-based auto extraction | Yes | Yes | Both adapters wire lifecycle hooks into daemon signals. |
| Compaction extraction trigger | Yes (`before_compaction`) | Yes (`PreCompact`) | Trigger exists on both hosts. |
| **Compaction control (wait for extraction before compact)** | **Yes** | **No** | OC sets `supports_compaction_control=True`; CC is fire-and-forget async. |
| Timeout-based extraction path | Yes | Yes | Both use daemon signal flow; OC timeout compaction control is stronger. |
| Gateway-managed LLM path (no per-project API key setup) | Yes | No | OC resolves provider/model through gateway auth and runtime profile. |
| Claude OAuth token refresh from host credentials | N/A | Yes | CC reads `~/.claude/.credentials.json` and can refresh `.auth-token` on session init. |
| Live runtime notifications | Yes | Partial | OC supports live routed notifications; CC commonly uses deferred pending queue. |
| Programmatic compaction RPC | Yes | No | OC path supports gateway `sessions.compact`. |

## Practical Guidance

- If you want the most complete launch path today, use **OpenClaw**.
- If your priority is Claude Code workflow integration, use **Claude Code** with the hook-based adapter and expect async extraction around compaction.
- If you only need direct operations, use the **CLI** and drive extraction/recall manually.

## Related Docs

- [Interface Contract](INTERFACES.md)
- [Architecture Guide](ARCHITECTURE.md)
- [AI Install Guide](AI-INSTALL.md)
- [Hook Session Lifecycle](../projects/quaid/reference/hooks-session-lifecycle.md)
