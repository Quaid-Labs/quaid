# Live Validation Log

Records of manual live validation runs against real adapter instances.

---

## 2026-03-13 — v0.3.0 Prerelease Validation

### Summary

Full M0–M7 live test run against OC 3.11 (alfie.local) and CC (spark/local testbench).

### Environment

| Adapter | Host | Version |
|---------|------|---------|
| OpenClaw | alfie.local | 2026.3.11 |
| Claude Code | spark (local) | current stable |
| Quaid | both | 0.3.0-canary (commit `14d99b20`) |

### Results

| Marker | Description | OC 3.11 | CC | Notes |
|--------|-------------|---------|-----|-------|
| M0 | Fresh install / bootstrap | ✅ | ✅ | |
| M1 | Store and recall | ✅ | ✅ | |
| M2 | Session signal extraction | ✅ | ✅ | hook-extract writes signal; daemon processes async |
| M3 | PreCompact/extraction trigger | ✅ | ✅* | CC has no `/compact`; uses `PreCompact` hook. Signal written + synchronous ingest confirmed (513→518 nodes). |
| M4 | /new session signal | ✅ | N/A | CC has no `/new`; session boundary is new Claude Code session. Not directly testable in-session. |
| M5 | Memory injection | ✅ | ✅ | 6 memories injected via `[Quaid Memory Context]` blocks per message |
| M6 | Deliberate recall | ✅ | ✅ | |
| M7 | Graph edges | ✅ | ✅ | 10 edges verified via `quaid get-edges` CLI; Marcus→Solomon (sibling_of), Marcus→OHSU (works_at), etc. |

*M3 on CC: `PreCompact` hook writes `compaction.json` signal to `data/extraction-signals/`; ingest pipeline confirmed via `run_extract_from_transcript` synchronous call.

### Notes

- **M4 on CC is N/A by design.** OpenClaw exposes `/new` as a command; Claude Code session boundaries are managed by the host. Extraction fires at `SessionEnd` (hook registered in `settings.json`).
- **Extraction daemon PID file** was stale during CC M3 test (PID 3830 was a session-init hook process, not the daemon). Daemon should be started via `quaid daemon start` at boot, not rely on stale PID.
- **M3 OC** tested via user-typed `/compact` in OpenClaw.
- **M7 CC** confirmed both via raw `sqlite3` and `quaid get-edges` CLI on `fe21a6b0` (Marcus node).

---

## 2026-03-13 — Installer Improvements (same session)

Changes shipped in `9240ee2a feat(installer): shared embeddings config, instance ID prompt, and config targeting`:

| Feature | Status |
|---------|--------|
| Instance ID prompt in installer | ✅ Shipped — shows existing instances, per-adapter sharing tips, default = adapter name if not taken |
| Shared embeddings config (`QUAID_HOME/shared/config/memory.json`) | ✅ Shipped — first install wins; subsequent installs inherit |
| `detectSharedEmbeddings()` provider-agnostic check | ✅ Shipped — checks by provider block, stubbed for openai/cohere |
| `quaid instances list` CLI | ✅ Shipped — lists all instances under QUAID_HOME, `--json` flag |
| `quaid config edit --shared / --instance <id>` | ✅ Shipped — both `config_cli.mjs` and `config_cli.py` |
| Legacy flat `QUAID_HOME/config/memory.json` write removed | ✅ Shipped |
| Compatibility matrix populated for v0.3.0 | ✅ Shipped — OC ≥2026.3.7 compatible (tested 3.7/3.8/3.11), CC ≥1.0.0 compatible |

---

## Previous Sessions

### 2026-03-08 — CC Hook Wiring Verification

- `hook-inject`: confirmed `[Quaid Memory Context]` blocks injected per user message
- `PreCompact` hook: signal written correctly
- `SessionEnd` hook: signal written correctly
- Extraction: 47 new memories extracted from live transcript (6 → 53 nodes at the time)
