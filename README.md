<p align="center">
  <img src="assets/quaid-logo.png" alt="Quaid" width="500">
  <br>
  <em>"If I'm not me, then who the hell am I?"</em>
</p>

### A Knowledge Layer for Agentic Systems

**Long term memory for AI agents**

> **Early alpha** — launched 2026, active daily development.

Quaid helps your agent remember who you are, what you are working on, and what already happened, so you do not have to reteach it after every reset, new session, or context loss.

If you use coding agents a lot, you have probably seen the failure mode already: the agent forgets decisions, loses project context, repeats mistakes, or needs the same background explained again. Quaid is the layer that stores that knowledge, keeps it organized, and feeds back the right parts when the agent needs them.

Under the hood, Quaid is an **active knowledge layer** for long-running agents. It is local-first, cross-platform, and built to capture, maintain, and retrieve knowledge across sessions while keeping your data on your machine. To study that regime, we built AgentLife — a benchmark for persistence, recall under resets, and cross-session coherence.

## How to Install

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh | bash
```

**Installing with an AI agent:**

Give this to your agent:

```text
Install Quaid for me.

First read and follow https://github.com/quaid-labs/quaid/blob/main/docs/AI-INSTALL.md exactly. Run the mandatory pre-install survey before installing anything. Use the platform/adapter for the agent you are currently running in unless I specify another one. Use my name as the owner name; if you do not know it, ask me before the survey. Show me the survey, wait for my approval, then install Quaid and run the health checks.
```

Agent instructions live in [AI-INSTALL.md](docs/AI-INSTALL.md).

After install, start here:
- [User Guide](projects/quaid/USER-GUIDE.md) — day-1 usage, project system basics, and where Quaid stores its files

---

## Benchmarks

**Headline: on AgentLife, Quaid matches full-context Sonnet quality at roughly one-third of the evaluation token cost. On the OpenClaw execution surface, Quaid also substantially outperforms OpenClaw native memory.**

AgentLife tests long-running agent memory across sessions, resets, stale facts, project continuity, and context pressure. Full Context (FC) is an upper-bound baseline where the answer model sees the transcript directly; it is useful for comparison, but it is not persistent memory.

### Clean Harness Headline Rows

| Surface | Quaid | FC Sonnet | Quaid Tokens | FC Tokens |
| --- | ---: | ---: | ---: | ---: |
| AgentLife Short | 93.64% | 93.11% | 7.95M | 29.83M |
| AgentLife Long | 88.52% | 88.69% | 9.64M | 26.50M |
| AgentLife Long OBD | 88.69% | 88.69% | 8.45M | 26.50M |

`AgentLife Short` is the clean core lane. `AgentLife Long` adds long/noisy filler sessions. `AgentLife Long OBD` compresses the long lane into one operational day. Quaid rows use Sonnet deep reasoning, Haiku fast reasoning, and Sonnet answer re-evaluation.

### OpenClaw Execution Surface

| Surface | OpenClaw Native | Quaid on OpenClaw |
| --- | ---: | ---: |
| AgentLife Short | 26.49% | 80.97% |
| AgentLife Long | 31.72% | pending refresh |

The clean harness table is the core Quaid reference surface. The OpenClaw table measures host execution-path tax, so the two are intentionally separated. OpenClaw Native was run with OpenClaw's built-in memory plugins enabled: `memory-core`, `session-memory`, and `session-index`.

### Notes

- Public token rows are non-judge evaluation tokens: answer model + recall/tool + preinject, excluding judge spend.
- Results are single-run per lane/configuration; informal repeat variance on stable configs is typically about `+-1pp`.
- Full methodology and run IDs live in the AgentLife repo: [latest technical report](https://github.com/quaid-labs/agentlife/blob/main/published/runbooks/AGENTLIFE_TECHNICAL_REPORT_20260430.md) and [runbooks folder](https://github.com/quaid-labs/agentlife/tree/main/published/runbooks).

---

## How Quaid Is Different

### Three knowledge systems and one maintenance lifecycle

Quaid is built around these four components. When you load Quaid, this is the model to expect:

- **Knowledge system 1: Personal memory graph**  
  Conversation-derived facts and relationships, stored for recall across resets and session boundaries.
- **Knowledge system 2: Identity files (`SOUL.md`, `USER.md`, `ENVIRONMENT.md`)**  
  Long-lived identity and operating context that evolves through controlled distillation instead of ad hoc prompt drift.
- **Knowledge system 3: Project knowledge (`PROJECT.md`, docs registry, docs RAG)**  
  Project-scoped context and documentation kept separate from personal memory so work context stays structured and shareable.
- **Maintenance lifecycle: Janitor**  
  Scheduled maintenance that reviews, deduplicates, rewrites, and decays stale knowledge so the three systems stay coherent over time.

- **Local-first by default:** memory graph, embeddings, and maintenance run on your machine.
- **Cross-platform and multi-agent:** per-instance silos keep personal memory separate by default, while the project space can stay shared across agents and hosts.
- **You own the data:** SQLite DBs, identity files, and project docs stay inspectable and portable.
- **Dual learning evolution system:** fast updates plus slower journal distillation for long-term synthesis.
- **Project system with shadow git:** project memory keeps filtered durable facts, while the shadow git-backed docs pipeline keeps the exhaustive project record.
- **Tested for scale and cost:** long-horizon benchmark and live study data show the system stays practical as history grows, while bounded recall and compaction reduce token spend.
- **System-agnostic design:** the architecture is built around pluggable adapter contracts rather than a single host.

**Platform Compatibility (Quick View)**

| Capability | OpenClaw | Claude Code | Codex |
|---|---|---|---|
| Memory + janitor lifecycle | ✅ Yes | ✅ Yes | ✅ Yes |
| Project docs + RAG system | ✅ Yes | ✅ Yes | ✅ Yes |
| Evolving `SOUL/USER/ENVIRONMENT` | ✅ Yes | ✅ Yes | ✅ Yes |
| Hook-based auto extraction | ✅ Yes | ✅ Yes | ✅ Yes |
| Timeout Triggered Compaction (harvests token savings) | ✅ Yes | ❌ No | ❌ No |

OpenAI-backed lanes remain available in alpha, but they are currently experimental and benchmark materially below Anthropic for Quaid memory quality. Anthropic is the recommended backend unless you are blocked on credentials.

Full matrix: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)

## Design Philosophy: LLM-First

Almost every decision in Quaid is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. The system splits work between a **deep-reasoning LLM** (fact review, contradiction resolution, journal distillation) and a **fast-reasoning LLM** (reranking, dedup verification, query expansion) to balance quality against cost and speed. The fast-reasoning model isn't just cheaper — it's fast. Memory recall needs to feel instant, not take three seconds waiting on a premium model to rerank results.

Because the system leans heavily on LLM reasoning, Quaid naturally scales with AI models — as reasoning capabilities improve, every decision in the pipeline gets better without code changes.

---

## Future Forward

### Portable Agent State

_“We Can Remember It for You Wholesale.”_

Skills give an agent behavior. Quaid gives it understanding.

A long-term goal is portable agent state: domain knowledge, project context, and learned behavior that can move between runtimes with high parity.

This is vision, not a released feature.

### Extensible Data Layer

Quaid is being designed to be extensible not only at the platform-adaptor layer, but eventually at the datastore and ingest layers as well. The long-term direction is for Quaid to act as an open coordination layer for AI knowledge systems: accepting multiple datastore types and input pipelines, enforcing clear boundaries between them in core, and making those boundaries portable across runtimes.

This would allow Quaid to accept third-party memory solutions through the same core harness, and would make future datatypes, such as spatial telemetry for robotics, easy to plug into the architecture.

This depends on stronger plugin and datastore contracts than exist today, and is part of the post-launch direction rather than a completed surface.

## Requirements

- Node.js 18+
- Python 3.10+
- SQLite 3.35+
- [Ollama](https://ollama.ai) (for local embeddings)
- RAM for `nomic-embed-text` embeddings: ~1.5GB model footprint; recommend ~4GB available system RAM for stable local operation
- For OpenClaw integration: [OpenClaw](https://github.com/openclaw/openclaw) gateway
- Shared Quaid provider credentials for host installs
- Optional standalone auth/config when running via CLI outside a host gateway

---

## Release Status

Quaid `v0.3.2` is the current installable public release. LLM routing is adapter- and config-driven (`deep_reasoning` / `fast_reasoning`), with provider/model resolution handled through the gateway provider layer. Ollama remains the default embeddings path.

Known limitations for **v0.3.2**:
- Parallel-session targeting for `/new` and `/reset` extraction still has edge cases.
- Multi-user workloads are partially supported but not fully hardened under heavy concurrency.
- Windows is not supported. macOS and Linux only.
- Host integrations are still maturing across platforms; OpenClaw, Claude Code, and Codex are supported today, with broader host coverage still in progress.
- Quaid is designed LLM-first and avoids English-specific special casing where possible. Multilingual support has been tested and the system does work with non-English content when the configured LLM provider supports that language, but coverage is still not thorough. Initial multilingual tests showed a quality regression of about 10 percentage points compared with English lanes.

The system is backed by over 2,500 tests in the default gate (2,236 selected pytest + 333 vitest), 15 automated installer scenarios covering fresh installs, dirty upgrades, data preservation, migration, missing dependencies, and provider combinations, plus ongoing AgentLife benchmark evaluation.

GitHub Actions CI runs automated checks on pushes/PRs including runtime pair sync, docs/release consistency, linting, runtime build, isolated Python unit suites, and the full gate (`run-all-tests --full`).

We're actively testing and refining the system against benchmarks and welcome collaboration. If you're interested in contributing, testing, or just have ideas — open an issue or reach out.

---

## Learn More

- [Architecture Guide](docs/ARCHITECTURE.md) — How Quaid works under the hood
- [User Guide](projects/quaid/USER-GUIDE.md) — Day-1 usage, project system basics, and file locations
- [Adapter Authoring](docs/ADAPTER-AUTHORING.md) — How to integrate Quaid with your own host platform
- [AgentLife Repository](https://github.com/quaid-labs/agentlife) — Benchmark source, datasets, and runbooks
- [AgentLife Technical Reports](https://github.com/quaid-labs/agentlife/tree/main/published/runbooks) — Public matrices, run IDs, and methodology reports
- [Platform Compatibility](docs/COMPATIBILITY.md) — OpenClaw, Claude Code, and Codex capability matrix
- [Vision](VISION.md) — Project scope, guardrails, and non-goals
- [AI Agent Reference](docs/AI-REFERENCE.md) — Complete system index for AI assistants
- [Interface Contract](docs/INTERFACES.md) — CLI/adapter capability model and event contract
- [Notification Strategy](docs/NOTIFICATIONS.md) — Feature-level notification model and delayed request flow
- [Provider Modes](docs/PROVIDER-MODES.md) — Provider routing and cost-safety guidance
- [Security Policy](SECURITY.md) — Private vulnerability reporting guidance
- [Release Workflow](docs/RELEASE.md) — Pre-push checks and ownership guard
- [Maintainer Lifecycle](docs/MAINTAINER-LIFECYCLE.md) — Safe branch/release model for post-user operation
- [Contributing](CONTRIBUTING.md) — PR expectations, validation, and AI-assisted contribution policy
- [Good First Issues](docs/GOOD-FIRST-ISSUES.md) — Small scoped tasks for new contributors
- [v0.3.2 Notes](docs/releases/v0.3.2.md) — Release highlights, compatibility, and known limitations
- [Roadmap](ROADMAP.md) — What's coming next
- [AI Install Guide](docs/AI-INSTALL.md) — If you're an AI that's been asked to install Quaid, read and follow these instructions

---

## Author

**Solomon Steadman** —[@steadman](https://x.com/steadman) | [github.com/solstead](https://github.com/solstead)

## License

Apache 2.0 — see [LICENSE](LICENSE).
