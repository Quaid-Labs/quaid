<p align="center">
  <img src="assets/quaid-logo.png" alt="Quaid" width="500">
  <br>
  <em>"If I'm not me, then who the hell am I?"</em>
</p>

### A Knowledge Layer for Agentic Systems

> **Early alpha** — launched 2026, active daily development.

Quaid is not another memory plugin — it is an **active knowledge layer** for agents that need to operate over time. Local-first, cross-platform, and built for long-running agents, it captures, maintains, and retrieves knowledge across sessions while keeping your data on your machine. Quaid is designed to solve the failure modes that show up once agents leave a single context window behind: resets, stale facts, project drift, and rising token cost. To study that regime, we built AgentLife — a benchmark for persistence, recall under resets, and cross-session coherence.

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh | bash
```

Or just point your agent here:
```text
https://github.com/quaid-labs/quaid/blob/main/docs/AI-INSTALL.md
```

After install, start here:
- [User Guide](projects/quaid/USER-GUIDE.md) — day-1 usage, project system basics, and where Quaid stores its files

---

## How Quaid Is Different

- **Local-first by default:** memory graph, embeddings, and maintenance run on your machine.
- **Cross-platform and multi-agent:** per-instance silos keep personal memory separate by default, while the project space can stay shared across agents and hosts.
- **You own the data:** SQLite DBs, identity files, and project docs stay inspectable and portable.
- **Three knowledge areas:** facts, core personality, and project knowledge are treated differently instead of flattened into one store.
- **Lifecycle maintenance, not just storage:** nightly janitor pipeline continuously reviews, deduplicates, and decays stale knowledge.
- **Dual learning evolution system:** fast updates plus slower journal distillation for long-term synthesis.
- **Project system with shadow git:** project knowledge is tracked through a shadow git-backed docs pipeline instead of being dumped into personal memory.
- **Tested for scale and cost:** long-horizon benchmark and live study data show the system stays practical as history grows, while bounded recall and compaction reduce token spend.
- **System-agnostic design:** the architecture is built around pluggable adapter contracts rather than a single host.

**Platform Compatibility (Quick View)**

| Capability | OpenClaw | Claude Code | Codex |
|---|---|---|---|
| Memory + janitor lifecycle | Yes | Yes | Yes |
| Project docs + RAG system | Yes | Yes | Yes |
| Evolving `SOUL/USER/ENVIRONMENT` | Yes | Yes | Yes |
| Hook-based auto extraction | Yes | Yes | Yes |
| Timeout Triggered Compaction (harvests token savings) | **Yes** | **No** | **No** |
| Gateway-managed LLM path (no per-project API key setup) | **Yes** | No | **Yes** |

Full matrix: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)

## A New Open Standard For Portability

_“We Can Remember It for You Wholesale.”_

Skills give an agent behavior. Quaid gives it understanding.

A long-term goal is portable agent identity: a complete package of domain knowledge, project context, and learned behavior that can be exported from one runtime and imported into another with high parity.

We are currently forming an open draft standard for this and asking for community feedback before implementation, especially from security experts.

Draft standard direction: [EGO Core 0001 (Draft)](docs/specs/EGO-CORE-0001-draft.md)
Community discussion: [github.com/Quaid-Labs/quaid/discussions/3](https://github.com/Quaid-Labs/quaid/discussions/3)

This is vision, not a released feature.

## Benchmarks

Quaid's benchmark program is **AgentLife**, maintained in a dedicated public repo so benchmark docs and runbooks have a single source of truth.

Context-window baselines like FC are included here as short-horizon upper bounds, not as the target operating model. The question is not "can memory beat raw transcript in every short horizon case," but whether a persistent system can stay competitive while surviving resets, controlling cost growth, and preserving continuity across sessions.

Terminology:
- `AL-S`: clean core AgentLife lane
- `AL-L`: long/noisy lane with filler sessions
- `AL-L OBD`: `AL-L` compressed into one operational day, simulates a power user
- `FC`: full-context baseline without a memory system
- `Tokens`: minimum eval tokens to answer all 283 benchmark questions

Headline launch summary:

| Metric | Quaid | FC Sonnet | OpenClaw Native |
| --- | ---: | ---: | ---: |
| AL-S | 87.69% | 92.90% | 69.40% |
| Tokens | 5.75M | 29.83M | unknown |
| AL-L | 87.10% | 87.70% | 63.06% |
| Tokens | 6.46M | 34.60M | unknown |
| AL-L OBD | 86.04% | 87.70% | unknown |
| Tokens | 6.08M | 34.60M | unknown |

Quaid was measured with Haiku fast, Sonnet deep, and a Sonnet agent running eval. `AL-L` and `AL-L OBD` are chosen here as the best representation of real use data; `AL-S` remains the cleaner, more idealized lane. `Sonnet/Haiku` remains the flagship configuration on cleanliness and overall benchmark tradeoffs. `Opus` was evaluated, but underperformed `Sonnet` overall and is not the recommended launch configuration. On `AL-L` and `AL-L OBD`, FC is forced to compact, and the drop in FC quality reflects that compaction plus the added noise in the larger corpus. OpenClaw Native tokens remain unknown due to telemetry restrictions. Token counts here are the minimum tokens used to answer the full set of 283 eval questions.

Benchmark note: AgentLife uses synthetic high-density conversations designed to stress memory systems. Current public rows are single-run per lane/configuration; informal repeat variance on stable configs has typically been about `+-1pp`.

Use these canonical links:
- [AgentLife GitHub Repo](https://github.com/quaid-labs/agentlife)
- [AgentLife Technical Report](https://github.com/quaid-labs/agentlife/blob/main/published/runbooks/AGENTLIFE_TECHNICAL_REPORT.md)

---

## Design Philosophy: LLM-First

Almost every decision in Quaid is algorithm-assisted but ultimately arbitrated by an LLM appropriate for the task. The system splits work between a **deep-reasoning LLM** (fact review, contradiction resolution, journal distillation) and a **fast-reasoning LLM** (reranking, dedup verification, query expansion) to balance quality against cost and speed. The fast-reasoning model isn't just cheaper — it's fast. Memory recall needs to feel instant, not take three seconds waiting on a premium model to rerank results.

Because the system leans heavily on LLM reasoning, Quaid naturally scales with AI models — as reasoning capabilities improve, every decision in the pipeline gets better without code changes.

---

## Requirements

- Node.js 18+
- Python 3.10+
- SQLite 3.35+
- [Ollama](https://ollama.ai) (for local embeddings)
- RAM for `nomic-embed-text` embeddings: ~1.5GB model footprint; recommend ~4GB available system RAM for stable local operation
- For OpenClaw integration: [OpenClaw](https://github.com/openclaw/openclaw) gateway
- Gateway-managed provider auth (OAuth/API key) when running inside an agentic host like OpenClaw
- Optional standalone auth/config when running via CLI outside a host gateway

---

## Early Alpha

Quaid is in early alpha. LLM routing is adapter- and config-driven (`deep_reasoning` / `fast_reasoning`), with provider/model resolution handled through the gateway provider layer. Ollama remains the default embeddings path.

Known limitations for **v0.2.15-alpha**:
- Parallel-session targeting for `/new` and `/reset` extraction still has edge cases.
- Multi-user workloads are partially supported but not fully hardened under heavy concurrency.
- Windows is not supported. macOS and Linux only.
- Host integrations are still maturing across platforms; OpenClaw, Claude Code, and Codex are supported today, with broader host coverage still in progress.

The system is backed by over 2,500 tests in the default gate (2,236 selected pytest + 333 vitest), 15 automated installer scenarios covering fresh installs, dirty upgrades, data preservation, migration, missing dependencies, and provider combinations, plus ongoing AgentLife benchmark evaluation.

GitHub Actions CI runs automated checks on pushes/PRs including runtime pair sync, docs/release consistency, linting, runtime build, isolated Python unit suites, and the full gate (`run-all-tests --full`).

We're actively testing and refining the system against benchmarks and welcome collaboration. If you're interested in contributing, testing, or just have ideas — open an issue or reach out.

---

## Learn More

- [Architecture Guide](docs/ARCHITECTURE.md) — How Quaid works under the hood
- [User Guide](projects/quaid/USER-GUIDE.md) — Day-1 usage, project system basics, and file locations
- [Adapter Authoring](docs/ADAPTER-AUTHORING.md) — How to integrate Quaid with your own host platform
- [AgentLife Repository](https://github.com/quaid-labs/agentlife) — Benchmark source, datasets, and runbooks
- [AgentLife Technical Report](https://github.com/quaid-labs/agentlife/blob/main/published/runbooks/AGENTLIFE_TECHNICAL_REPORT.md) — Full matrix, run IDs, and methodology
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
- [v0.2.15-alpha Notes](docs/releases/v0.2.15-alpha.md) — Latest published release highlights and known limitations
- [Roadmap](ROADMAP.md) — What's coming next

---

## Author

**Solomon Steadman** —[@steadman](https://x.com/steadman) | [github.com/solstead](https://github.com/solstead)

## License

Apache 2.0 — see [LICENSE](LICENSE).
