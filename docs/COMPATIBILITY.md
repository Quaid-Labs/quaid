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

## Known Platform Quirks

This section records behaviors discovered during live testing that materially change what Quaid can deliver on a given platform. These are not bugs in Quaid's core — they are host/model behaviors that constrain or shape what the platform integration can provide. This list is intended to grow over time and may eventually become a list of issues to raise with platform providers.

### Relationship edge extraction — OpenClaw (openai-codex/gpt-5.4) and Codex

**Symptom:** Quaid's extraction pipeline produces zero relationship edges at store time on both the OpenClaw adapter (when using `openai-codex/gpt-5.4` via OAuth) and the Codex adapter. The memory facts are stored correctly; only the graph edges (relationships between entities) are missing immediately after extraction.

**Root cause:** Quaid's extraction LLM call includes a prompt section that asks the model to emit relationship hints in a structured format. The OpenAI Codex-family models (`openai-codex/gpt-5.4`, `gpt-5.4-mini`) do not reliably produce output in that format. This appears to be a model behavior difference compared to Claude models, which do follow the format consistently.

**Impact:** Recall queries that depend on graph traversal (multi-hop relationships, entity connections) produce no results until the janitor backfill task runs. Single-hop vector recall is unaffected.

**Current workaround:** The janitor `--task all --apply` run performs edge backfill from existing facts and restores graph connectivity. Scheduling regular janitor runs mitigates the gap. This is a known limitation of OpenAI Codex OAuth model integration.

**Future path:** Investigate whether a different extraction prompt format or a post-extraction edge-generation pass can improve edge coverage without depending on in-format model output.

---

### Session identity — Codex `/new` command does not expose a session ID

**Symptom:** On Codex, starting a new conversation context (equivalent to `/new` on other platforms) does not provide a stable session ID that Quaid can use to link extraction output to the initiating session. The Quaid daemon can detect the session boundary and trigger extraction, but cannot associate the extracted facts with a named session in the session log.

**Root cause:** Codex does not expose a session ID as part of its hook payloads or command lifecycle. The stop hook fires correctly, but the session context passed to Quaid does not include a stable identifier for the new session.

**Impact:** `quaid session list` and `quaid session load` may show incomplete or unlinked session entries for Codex-originated sessions. Cross-session lineage tracking (which session produced which facts) is weakened on Codex compared to OpenClaw and Claude Code.

**Current workaround:** None. Extraction and recall still work correctly; only the session provenance metadata is affected.

**Future path:** If Codex exposes a session ID in a future hook update, the adapter can be updated to forward it. Otherwise, a content-based session fingerprinting approach could substitute.
