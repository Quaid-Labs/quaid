# Platform Compatibility

This page has two views:
- product capabilities (what Quaid provides)
- host integration capabilities (what each platform can expose)

Compatibility entries are operator-approved records of accepted host/platform
constraints. Do not use this page as a first-line bug fix or a way to paper
over Quaid defects; if behavior can be repaired in Quaid, fix it instead.

## Validated Release Versions

Versions confirmed fully compatible with Quaid through the complete M1–M15 + XP live test suite.

| Quaid | OpenClaw | Claude Code | Codex CLI | Node | Date | Run | Result |
|-------|----------|-------------|-----------|------|------|-----|--------|
| 0.3.0-alpha (`d47e3f11`) | 2026.4.14 (`323493f`) | 2.1.109 | 0.120.0 | v25.9.0 | 2026-04-15 | Run 96 | ✅ All three platforms M1–M15 + XP |

**Run 96 primary fix:** CamelCase CDX hook event names (`SessionStart` / `UserPromptSubmit` / `Stop`) — commit `3921fa602`.

Installer compatibility for OpenClaw is version-gated plus gateway-health-gated.
Do not use bundled JavaScript hook-symbol greps as a hard installer gate:
current OpenClaw releases can bundle or rename lifecycle internals while still
providing the required hook behavior.
Fresh installs may also report a non-zero `plugins uninstall quaid --force`
when no plugin is installed yet; the installer treats an absent `quaid` row in
`openclaw plugins list` as already-clean state and proceeds.
Some OpenClaw builds also omit directly registered plugins from
`openclaw plugins list` after a successful install; Quaid treats that CLI list
as diagnostic when the extension directory, config entry, memory slot binding,
and install path confirm the plugin is registered. The installer still runs its
separate store/recall smoke test before finishing.
OpenClaw plugin registration is repaired through direct config/install-record
writes rather than `openclaw plugins install/uninstall`, because those CLI paths
can block under gateway/plugin-list races. During repair, Quaid stages only
runtime source files and excludes generated caches, tests, logs, and
`node_modules`; production dependencies are copied separately or installed as a
fallback.

---

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
| System-context refresh trigger | Yes (`/compact`) | Yes (`/compact` rebuild + next-turn identity bridge) | Timeout (no compact hook) | Long-lived sessions need to re-pull system-context markdown when projects/identity files evolve. OC refreshes on compaction. CC rebuilds split rules files on `/compact` and sends a bounded identity-only bridge on the next user turn because rewritten rules are not reliably model-visible immediately after compact. Codex has no compaction hook, so its refresh fires on the daemon idle-timeout path instead — meaning Codex sessions only see system-context updates after `inactivityTimeoutMinutes` elapses (default 60). |
| **Compaction control (wait for extraction before compact)** | **Yes** | **No** | **No** | OC sets `supports_compaction_control=True`; CC/Codex are async or post-turn extraction flows. |
| Timeout-based extraction path | Yes | Yes | No | Codex uses synchronous stop-hook extraction rather than daemon timeout sweeps. |
| Adapter-owned auth token required for Quaid LLM calls | Yes | Yes | Yes | OC uses `~/.quaid/adaptors/openclaw/.auth-token`, CC uses `~/.quaid/adaptors/claude-code/.auth-token`, CDX uses `~/.quaid/adaptors/codex/.auth-token`. |
| Host credential auto-refresh | No | No | No | Launch path uses explicit adapter-owned tokens, not host-owned OAuth/session credentials. |
| Live runtime notifications | Yes | Partial | Partial | OC supports live routed notifications; CC/Codex commonly use deferred pending queues. |
| Programmatic compaction RPC | Yes | No | No | OC path supports gateway `sessions.compact`. |

## Practical Guidance

- If you want the most complete launch path today, use **OpenClaw** and provide either an Anthropic or OpenAI token during install.
- If your priority is Claude Code workflow integration, use **Claude Code** with the hook-based adapter and provide an Anthropic token.
- If your priority is Codex workflow integration, use **Codex** with hook-based recall injection, stop-hook extraction, and an OpenAI token.
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

### Model availability — openai-codex OAuth path

**Symptom:** Only `gpt-5.4` is confirmed valid for the Responses API (`/v1/responses`) via the ChatGPT Codex OAuth authentication path. `gpt-5.4-mini`, `gpt-5.2`, and other model variants all return HTTP 400 Bad Request.

**Root cause:** The ChatGPT Codex OAuth path exposes a narrower model selection than the standard OpenAI API key path. At the time of testing, only `gpt-5.4` (full) accepts requests. `gpt-5.1-codex-mini` is available but requires an explicit `reasoning.effort` value (`low`/`medium`/`high`) — it rejects requests where `reasoning.effort` is absent or `none`.

**Impact:** OC and CDX live-test silo configs must use `gpt-5.4` for deep-tier extraction and any LLM call that does not send explicit `reasoning.effort`. `gpt-5.1-codex-mini` can be used for fast-tier calls if `fastReasoningEffort` is set to `medium` in config.

**Current workaround:** Set `deep_reasoning: "gpt-5.4"` (no reasoning.effort) and `fast_reasoning: "gpt-5.1-codex-mini"` with `fastReasoningEffort: "medium"` for both OC and CDX live-test silos.

**Future path:** Raise with OpenAI/Codex team whether additional models (gpt-5.2, etc.) will be exposed on the OAuth path, and whether `gpt-5.4` will continue to be available without reasoning parameters.

---

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

---

### Session-end extraction latency — Codex `/new` boundary

**Status:** Resolved as an accepted Codex platform limitation.

**Symptom:** When a Codex user types `/new`, an immediate follow-up question about content from the prior session can hit stale memory.

**Root cause:** Codex does not materialize the session boundary until the first follow-up prompt. Quaid receives the prior-session signal at that moment and starts async extraction, which usually takes several seconds. The same follow-up prompt's synchronous memory injection can complete before extraction lands.

**Impact:** Immediate-after-`/new` recall can miss facts that Quaid extracts correctly moments later. This is platform-specific to Codex; Claude Code and OpenClaw expose different lifecycle hooks and do not have this exact race.

**Current workaround:** Ask again after a moment. For live testing, send a sacrificial first prompt after `/new` to materialize the boundary, then poll for extraction before grading prior-session recall.

**Future path:** Revisit if Codex exposes a `/new`-side hook or synchronous session-boundary flush. Quaid is not pursuing a local sync barrier for this release.

---

### Rules-file refresh after compaction — Claude Code

**Status:** Mitigated in Quaid by split rules files plus a bounded identity bridge.

**Symptom:** Identity or project context that was appended immediately before
`/compact` may be present on disk in `.claude/rules/quaid-*.md` but not visible
to the model on the first post-compact question.

**Root cause:** Claude Code auto-loads `.claude/rules/*.md` at session start,
but current 2.1.x builds do not reliably reload rewritten rules files into
model-visible context on the `/compact` boundary.

**Impact:** Without a bridge, post-compact questions can miss freshly updated
identity facts even though Quaid rebuilt the rules files correctly.

**Current workaround:** Quaid rebuilds split `quaid-*.md` rules files on
`/compact`, then sends a small identity-only `additionalContext` bridge on the
next user turn. Project/tool context still rides through split rules and normal
recall paths.

**Future path:** Remove the bridge if Claude Code exposes a reliable rules
reload signal or makes rewritten rules model-visible immediately after compact.

---

### Parallel agents in one Codex instance

**Status:** Accepted Codex platform limitation; use separate Quaid instances for isolation-sensitive parallel work.

**Symptom:** Multiple agents sharing one Codex-backed Quaid instance can make extraction appear early, late, or out of phase with the visible agent turn.

**Root cause:** Codex has no compaction hook and no upfront `/new` lifecycle hook. Quaid relies on asynchronous `Stop` and follow-up prompt signals, so concurrent sessions in the same instance can interleave lifecycle observations.

**Impact:** Same-instance parallel agents can confuse tests or user workflows that assume strict extraction ordering. The memory silo is intentionally shared, but lifecycle timing is less deterministic than on platforms with stronger session hooks.

**Current workaround:** Use a separate Quaid instance for each parallel Codex agent when timing, isolation, or extraction ordering matters.

**Future path:** Revisit if Codex exposes stronger per-session lifecycle metadata or compaction hooks.

---

### HyDE recall LLM timeout — Codex adapter

**Symptom:** On the Codex adapter, `quaid recall` queries that trigger HyDE (Hypothetical Document Embedding) generation intermittently time out with `[llm_clients] LLM error: Timed out waiting for Codex turn notifications`. The timeout occurs in the LLM call path for generating the hypothetical document used to improve query embedding quality. The failure degrades recall to unranked vector results, which may be irrelevant.

**Root cause:** Quaid's HyDE step issues an LLM call to generate a hypothetical answer to the recall query before embedding. On the Codex adapter, LLM calls go through the Codex CLI subprocess notification path. For complex multi-term queries at medium reasoning effort, this path exceeds the LLM client timeout. Simpler queries ("my family") complete within the timeout; compound queries ("exercise habits recent plans") do not.

**Impact:** Recall quality degrades silently when the HyDE LLM call times out — the system falls back to raw vector similarity without the HyDE-boosted query, producing weaker results. The issue is intermittent (query-complexity-dependent) and not visible to the user unless they inspect logs.

**Current workaround:** Set `retrieval.useHyde = false` in the CDX silo's instance `config.json`. This disables HyDE entirely for CDX, eliminating the LLM dependency from the recall path. Recall quality is lower without HyDE but stable.

**Future path:** Investigate the Codex CLI LLM call timeout in `core/llm/`. Either raise the timeout for the HyDE step, make HyDE calls async with fallback, or expose a per-adapter HyDE toggle in the adapter config contract.

---

### Injected context visibility — Codex shows Quaid hook context in the host view

**Symptom:** On Codex, Quaid's injected recall/startup context is visible in the host view screen instead of being fully hidden as an internal system-side prompt augmentation.

**Root cause:** Codex surfaces hook-provided `additionalContext` in its own UI flow. Quaid delivers bounded recall and startup context through the supported hook payload, but Codex currently treats that material as visible host context rather than invisible prompt plumbing.

**Impact:** Users can see Quaid-injected memory/project context in Codex's view, which makes the integration feel less silent than OpenClaw. This is cosmetic, but it affects UX and may expose more of Quaid's internal recall framing than desired.

**Current workaround:** None on the Quaid side without weakening or removing injection. As of April 4, 2026, no Codex CLI setting was found in local `codex --help`, `codex debug --help`, `codex features`, or `~/.codex/config.toml` that suppresses visibility of hook-injected `additionalContext`.

**Future path:** If Codex adds a hidden/system hook channel or a visibility control for hook-injected context, the adapter should switch to that path immediately.
