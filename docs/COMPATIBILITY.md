# Platform Compatibility

This page has two views:
- product capabilities (what Quaid provides)
- host integration capabilities (what each platform can expose)

## Primary Host Integrations

- OpenClaw (`openclaw` adapter)
- Claude Code (`claude-code` adapter)
- Codex (`codex` adapter)

Quaid also provides a direct operational CLI, but this page focuses on host integrations.

## Product Capability Matrix

| Quaid Capability | OpenClaw | Claude Code | Codex | Notes |
|---|---|---|---|---|
| Memory system (capture, retrieval, maintenance) | Yes | Yes | Yes | Core Quaid engine shared across adapters. |
| Project system (registry, docs ingest, RAG search) | Yes | Yes | Yes | Same project/docs pipeline on all adapters. |
| Multi-agent/session lineage awareness | Yes | Yes | Partial | Strongest on hosts exposing subagent lifecycle hooks. |
| Evolving identity files (`SOUL.md`, `USER.md`, `ENVIRONMENT.md`) | Yes | Yes | Yes | Instance identity files evolve via snippets + janitor distillation. |
| Cross-session persistence and reset resilience | Yes | Yes | Yes | Instance silo model preserves state across sessions/resets. |
| Bounded memory injection (token-efficient recall context) | Yes | Yes | Yes | Core recall pipeline; host controls final prompt assembly timing. |

## Host Integration Matrix

| Capability | OpenClaw | Claude Code | Codex | Notes |
|---|---|---|---|---|
| Core recall/store pipeline | Yes | Yes | Yes | Same core memory engine and datastore paths. |
| Hook-based auto extraction | Yes | Yes | Yes | Codex extraction runs from `Stop` hook (`hook-codex-stop`); OC/CC use daemon signal flow. |
| Compaction extraction trigger | Yes (`before_compaction`) | Yes (`PreCompact`) | No | Codex currently has no pre-compaction extraction hook. |
| **Compaction control (wait for extraction before compact)** | **Yes** | **No** | **No** | OC sets `supports_compaction_control=True`; CC/Codex are async or post-turn extraction flows. |
| Timeout-based extraction path | Yes | Yes | No | Codex uses synchronous stop-hook extraction rather than daemon timeout sweeps. |
| Gateway-managed LLM path (no per-project API key setup) | Yes | No | Yes | OC uses gateway plugin route; Codex uses host-managed `codex app-server` runtime. |
| Claude OAuth token refresh from host credentials | N/A | Yes | N/A | CC reads `~/.claude/.credentials.json` and can refresh `.auth-token` on session init. |
| Live runtime notifications | Yes | Partial | Partial | OC supports live routed notifications; CC/Codex commonly use deferred pending queues. |
| Programmatic compaction RPC | Yes | No | No | OC path supports gateway `sessions.compact`. |

## Practical Guidance

- If you want the most complete launch path today, use **OpenClaw**.
- If your priority is Claude Code workflow integration, use **Claude Code** with the hook-based adapter and expect async extraction around compaction.
- If your priority is Codex workflow integration, use **Codex** with hook-based recall injection and stop-hook extraction.
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
