import { describe, expect, it } from "vitest";
import { __test } from "../adaptors/openclaw/adapter.js";

describe("lifecycle signal detection", () => {
  it("does not treat assistant chatter as auto-compaction", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "I compacted the context after summarizing the thread." },
      { role: "assistant", content: "continuing..." },
    ]);
    expect(signal).toBe(null);
  });

  it("detects manual compact slash commands", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "ok" },
      { role: "user", content: "/compact" },
    ]);
    expect(signal).toBe("CompactionSignal");
  });

  it("detects timestamp-prefixed compact command lines", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "ok" },
      { role: "user", content: "[Tue 2026-03-03 16:08 GMT+8] /compact" },
    ]);
    expect(signal).toBe("CompactionSignal");
  });

  it("does not treat quoted transcript compact mentions as live commands", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "ok" },
      {
        role: "user",
        content:
          "Extract from this chunk:\\nUser: [Tue 2026-03-03 16:08 GMT+8] /compact\\nAssistant: NO_REPLY",
      },
    ]);
    expect(signal).toBe(null);
  });

  it("detects OpenClaw auto-compaction system notices", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "working..." },
      { role: "system", content: "[2026-03-02 14:05:19 GMT+8] Compacted (37k → 5.0k) • Context 5.0k/200k (2%)" },
    ]);
    expect(signal).toBe("CompactionSignal");
  });

  it("keeps reset/new command detection intact", () => {
    const signal = __test.detectLifecycleCommandSignal([
      { role: "assistant", content: "ready" },
      { role: "user", content: "/reset now" },
    ]);
    expect(signal).toBe("ResetSignal");
  });

  it("suppresses duplicate compaction signal signatures", () => {
    __test.clearLifecycleSignalHistory();
    const detail = __test.detectLifecycleSignal([
      { role: "system", content: "[2026-03-02 14:05:19 GMT+8] Compacted (37k → 5.0k) • Context 5.0k/200k (2%)" },
      { role: "assistant", content: "continue" },
    ]);
    expect(detail?.label).toBe("CompactionSignal");
    const first = __test.shouldProcessLifecycleSignal("session-a", detail!);
    const second = __test.shouldProcessLifecycleSignal("session-a", detail!);
    expect(first).toBe(true);
    expect(second).toBe(false);
  });

  it("suppresses immediate hook-followed system compaction duplicates", () => {
    __test.clearLifecycleSignalHistory();
    __test.markLifecycleSignalFromHook("session-b", "CompactionSignal");
    const detail = __test.detectLifecycleSignal([
      { role: "system", content: "[2026-03-02 14:05:19 GMT+8] Compacted (37k → 5.0k) • Context 5.0k/200k (2%)" },
      { role: "assistant", content: "continue" },
    ]);
    const allowed = __test.shouldProcessLifecycleSignal("session-b", detail!);
    expect(allowed).toBe(false);
  });

  it("treats stale reset transcripts as backlog replay for notification suppression", () => {
    const old = new Date(Date.now() - (5 * 60 * 1000)).toISOString();
    const isBacklog = __test.isBacklogLifecycleReplay(
      [{ role: "user", content: "/reset", timestamp: old }],
      "reset",
      Date.now(),
    );
    expect(isBacklog).toBe(true);
  });

  it("does not treat recent compaction transcripts as backlog replay", () => {
    const nowIso = new Date().toISOString();
    const isBacklog = __test.isBacklogLifecycleReplay(
      [{ role: "system", content: "Compacted (10k → 2k)", timestamp: nowIso }],
      "compaction",
      Date.now(),
    );
    expect(isBacklog).toBe(false);
  });

  it("treats timestamp-less implicit reset/recovery as backlog replay", () => {
    const isBacklog = __test.isBacklogLifecycleReplay(
      [{ role: "assistant", content: "resetting session state now" }],
      "reset",
      Date.now(),
    );
    expect(isBacklog).toBe(true);
  });

  it("does not treat timestamp-less explicit /reset command as backlog replay", () => {
    const isBacklog = __test.isBacklogLifecycleReplay(
      [{ role: "user", content: "/reset" }],
      "reset",
      Date.now(),
    );
    expect(isBacklog).toBe(false);
  });

  it("uses config-default auto injection unless explicitly disabled", () => {
    const original = process.env.MEMORY_AUTO_INJECT;
    delete process.env.MEMORY_AUTO_INJECT;

    expect(__test.isAutoInjectEnabled({ retrieval: {} })).toBe(true);
    expect(__test.isAutoInjectEnabled({ retrieval: { autoInject: false } })).toBe(false);
    expect(__test.isAutoInjectEnabled({ retrieval: { autoInject: true } })).toBe(true);

    process.env.MEMORY_AUTO_INJECT = "0";
    expect(__test.isAutoInjectEnabled({ retrieval: { autoInject: true } })).toBe(false);

    process.env.MEMORY_AUTO_INJECT = "1";
    expect(__test.isAutoInjectEnabled({ retrieval: { autoInject: false } })).toBe(true);

    if (original === undefined) {
      delete process.env.MEMORY_AUTO_INJECT;
    } else {
      process.env.MEMORY_AUTO_INJECT = original;
    }
  });

  it("treats openresponses session keys as internal Quaid work", () => {
    expect(__test.isInternalSessionContext(
      { sessionKey: "agent:main:openresponses:abc123" },
      { sessionId: "89003867-ed94-4bb3-8881-289a63e8250c" },
    )).toBe(true);

    expect(__test.isInternalSessionContext(
      { sessionKey: "agent:main:tui-user-session" },
      { sessionId: "86bea2fc-b843-43b8-94bb-7ffb9a0e9d17" },
    )).toBe(false);
  });

  it("extracts auto-inject query from direct event text when prompt/messages are empty", () => {
    const selected = __test.selectAutoInjectQuery(
      {
        text: "What do you know about my dog Baxter?",
        prompt: "",
        messages: [],
      },
      null,
      1_000,
    );
    expect(selected.query).toBe("What do you know about my dog Baxter?");
    expect(selected.source).toBe("event_text_scrubbed");
  });

  it("falls back to fresh message_received cache when prompt/messages are empty", () => {
    const selected = __test.selectAutoInjectQuery(
      {
        prompt: "",
        messages: [],
      },
      { text: "What do you remember about my neighbour?", seenAtMs: 9_500 },
      10_000,
    );
    expect(selected.query).toBe("What do you remember about my neighbour?");
    expect(selected.source).toBe("message_received_cache");
  });

  it("uses the instance silo db path for adapter python calls", () => {
    expect(__test.resolveAdapterMemoryDbPath(
      "/tmp/quaid-home",
      "openclaw-livetest",
      "/tmp/quaid-home/data/memory.db",
    )).toBe("/tmp/quaid-home/openclaw-livetest/data/memory.db");
  });

  it("summarizes recall diagnostics for hook tracing", () => {
    expect(__test.summarizeRecallDiagnostics({
      meta: {
        mode: "fast",
        stop_reason: "quality_gate_complete",
        planned_stores: ["vector"],
        planned_project: null,
        store_runs: [{ store: "vector", result_count: 2, total_ms: 41, selected_path: "vector" }],
        turn_details: [{ planner: { bailout_reason: "preserve_short_exact_query", planner_profile: "fast", queries_count: 1, used_llm: false } }],
        quality_gate: {
          fast_drill_candidate: true,
          fast_drill_enabled: false,
          fast_drill_reasons: ["low_entity_coverage"],
          evaluation: { requirements: ["identity"], covered_terms_ratio: 0.25, top_similarity: 0.44 },
        },
        phases_ms: { total_ms: 41, store_plan_wall_ms: 41 },
      },
    })).toEqual({
      mode: "fast",
      stop_reason: "quality_gate_complete",
      selected_path: undefined,
      planned_stores: ["vector"],
      planned_project: undefined,
      planner: {
        bailout_reason: "preserve_short_exact_query",
        planner_profile: "fast",
        queries_count: 1,
        used_llm: false,
      },
      store_runs: [{ store: "vector", result_count: 2, total_ms: 41, selected_path: "vector" }],
      quality_gate: {
        fast_drill_candidate: true,
        fast_drill_enabled: false,
        fast_drill_reasons: ["low_entity_coverage"],
        requirements: ["identity"],
        covered_terms_ratio: 0.25,
        top_similarity: 0.44,
      },
      phases_ms: {
        total_ms: 41,
        store_plan_wall_ms: 41,
        planner_ms: undefined,
        reranker_ms: undefined,
      },
    });
  });
});
