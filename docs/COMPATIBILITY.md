# Platform Compatibility

This page has two views:
- product capabilities (what Quaid provides)
- host integration capabilities (what each platform can expose)

## Primary Host Integrations

- OpenClaw (`openclaw` adapter)
- Claude Code (`claude-code` adapter)

Quaid also provides a direct operational CLI, but this page focuses on host integrations.

## Product Capability Matrix

| Quaid Capability | OpenClaw | Claude Code | Notes |
|---|---|---|---|
| Memory system (capture, retrieval, maintenance) | Yes | Yes | Core Quaid engine shared across adapters. |
| Project system (registry, docs ingest, RAG search) | Yes | Yes | Same project/docs pipeline on all adapters. |
| Multi-agent/session lineage awareness | Yes | Yes | Best with host lifecycle hooks. |
| Evolving identity files (`SOUL.md`, `USER.md`, `ENVIRONMENT.md`) | Yes | Yes | Instance identity files evolve via snippets + janitor distillation. |
| Cross-session persistence and reset resilience | Yes | Yes | Instance silo model preserves state across sessions/resets. |
| Bounded memory injection (token-efficient recall context) | Yes | Yes | Core recall pipeline; host controls final prompt assembly timing. |

## Host Integration Matrix

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

## For Host Integrators

If you are building a host integration, the highest-value capabilities to expose are:
1. reliable lifecycle hooks (prompt, compaction, reset/session-end)
2. explicit compaction-control handshake
3. stable auth/provider routing for deep/fast model tiers
4. clear notification routing primitives

Hosts that implement these interfaces get the full Quaid lifecycle behavior with minimal adapter-specific code.
