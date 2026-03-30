<p align="center">
  <img src="assets/quaid-logo.png" alt="Quaid" width="500">
  <br>
  <em>"If I'm not me, then who the hell am I?"</em>
</p>

### A Knowledge Layer for Agentic Systems

> **Early alpha** — launched 2026, active daily development.

Most agents still treat long-term context as replay: re-inject old chat and hope retrieval lands. Quaid is not another memory plugin; it is an **active knowledge layer**. It continuously captures, structures, and maintains knowledge, then serves only what matters at query time.

Context-window-based memory is an upper bound, not the target. It grows linearly in cost, does not persist across resets, and gets weaker as long-running workflows sprawl. Quaid targets a different regime: persistent knowledge with bounded recall cost and cross-session continuity.

Quaid is local-first: your memory database, identity files, project docs, and embeddings stay on your machine. You own the data, can back it up, move it, inspect it, and run it without a hosted memory service.

Every session starts ready to work. Project docs, architecture decisions, tool guidance, and codebase context are tracked and kept current automatically. Through a dual learning evolution system, the layer evolves with use: it doesn't just retain facts, it builds durable understanding of users, workflows, and projects over time.

**Why teams use it:**
- **Cross-platform and multi-agent:** OpenClaw and Claude Code are supported now, with Codex next on the roadmap. Agents can keep personal memory siloed per instance, intentionally share a memory silo, or share only the common project space.
- **Shared projects without forced identity sharing:** project docs and registry live in a shared workspace, so different agents and platforms can work from the same project context even when their personal memories stay separate.
- **Local-first and portable:** SQLite + local files + local embeddings mean you own and can move the data.
- **Low operating cost:** bounded recall, local embeddings, and compaction-aware flows are built to reduce brute-force context replay. On long-running agents, the memory layer can cost less than the context spend it saves.
- **Self-evolving agents:** `SOUL.md`, `USER.md`, and `ENVIRONMENT.md` are updated over time from usage. The agent keeps a curated journal of its surroundings so it can better understand them in the future.
- **Tested for long-horizon use:** AgentLife and the real-world scalability study both stress long-running memory behavior, not just short QA recall.

**What it remembers:**
- **Facts** — names, relationships, preferences, decisions, life events
- **Projects** — documentation, architecture, tools, tracked from git changes
- **Personality** — who your user is, who the agent is, how they interact — evolving over time

**What it does with them:**
- Extracts memories from conversations automatically
- Retrieves the right ones at the right time (hybrid search + LLM reranking)
- Runs a nightly janitor that reviews, deduplicates, and decays stale memories
- Keeps project docs and personality files current without manual maintenance
- Lets multiple agents share a project space while keeping personal memory siloed by default

Quaid is a system-agnostic knowledge layer by design, with adapters handling host-specific runtime details. [OpenClaw](https://github.com/openclaw/openclaw) and Claude Code are supported now, with Codex adapter support next on the roadmap.

**Interface surfaces:**
- **OpenClaw adapter** — lifecycle hooks + tool integration
- **Claude Code adapter** — hook-driven integration with durable session-init and daemon signaling
- **CLI** — direct operational control for extraction, recall, janitor, docs, and events
- **Adapter architecture** — manifest-driven, pluggable host integration model so other agentic systems can add Quaid support without forking core behavior

**Platform compatibility (quick view):**

| Capability | OpenClaw | Claude Code |
|---|---|---|
| Memory + janitor lifecycle | Yes | Yes |
| Project docs + RAG system | Yes | Yes |
| Evolving `SOUL/USER/ENVIRONMENT` | Yes | Yes |
| Hook-based auto extraction | Yes | Yes |
| Timeout Triggered Compaction (harvests token savings) | **Yes** | **No** |
| Gateway-managed LLM path (no per-project API key setup) | **Yes** | No |

Full matrix: [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md)

Quaid also includes a direct `quaid` CLI for operational control, but OC/CC are the primary host integrations.

Runtime event capabilities are discoverable via `quaid event capabilities` so orchestration can adapt to host/runtime support instead of assuming fixed behavior.

---

## Install

The guided installer sets up Quaid with knowledge capture, janitor scheduling, and host integration.

**Tell your agent about it:**
```text
https://github.com/quaid-labs/quaid/blob/main/docs/AI-INSTALL.md
```
If you're an agent, go there first and do not use the commands below.

**Mac / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/quaid-labs/quaid/main/install.sh | bash
```

**Manual:**
```bash
git clone https://github.com/quaid-labs/quaid.git
cd quaid && node setup-quaid.mjs
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

## Benchmarks

Quaid's benchmark program is **AgentLife**, maintained in a dedicated public repo so benchmark docs and runbooks have a single source of truth.

FC is included here as an upper-bound baseline, not as the target operating model. The question is not "can memory beat raw transcript in every short horizon case," but whether a persistent system can stay competitive while surviving resets and reducing long-run token cost.

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
- Host integrations are still maturing across platforms; OpenClaw and Claude Code are supported today, with broader host coverage still in progress.

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
- [Platform Compatibility](docs/COMPATIBILITY.md) — OC vs CC capability matrix
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
