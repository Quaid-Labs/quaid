import { describe, expect, it, vi } from "vitest";
import { createKnowledgeEngine } from "../orchestrator/default-orchestrator.js";
import {
  datastoreUsesCandidatePool,
  getBridgeEligibleDatastoreKeys,
  getDefaultDomainForKnowledgeDatastore,
  getHandlerStoreForKnowledgeDatastore,
  getRoutableDatastoreKeys,
  renderRoutableKnowledgeDatastoreRouterGuidance,
} from "../core/knowledge-stores.js";

type Result = {
  text: string;
  category: string;
  similarity: number;
  sourceType?: string;
  sourceChunkId?: string;
  chunkId?: string;
  outputTokenCount?: number;
  truncated?: boolean;
  id?: string;
  via?: string;
};

describe("knowledge orchestrator", () => {
  it("normalizes store defaults and removes invalid entries", () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => ""),
      recallMemory: vi.fn(async () => []),
    });

    expect(engine.normalizeKnowledgeDatastores(undefined, true)).toEqual([
      "vector_basic",
      "graph",
      "journal",
      "project",
    ]);
    expect(engine.normalizeKnowledgeDatastores(["vector_basic", "nope", "graph"], false)).toEqual([
      "vector_basic",
      "graph",
    ]);
    expect(engine.normalizeKnowledgeDatastores(["session_chunks"], false)).toEqual([
      "session_chunks",
    ]);
    expect(engine.normalizeKnowledgeDatastores(["source_chunks"], false)).toEqual([
      "session_chunks",
    ]);
    expect(engine.normalizeKnowledgeDatastores(undefined, false)).toEqual([
      "vector_basic",
      "journal",
      "project",
    ]);
  });

  it("preserves exact M6.1 routed registry snapshots", () => {
    expect(getRoutableDatastoreKeys()).toEqual([
      "vector_basic",
      "vector_technical",
      "graph",
      "journal",
      "project",
    ]);
    expect(getBridgeEligibleDatastoreKeys()).toEqual([
      "vector",
      "vector_basic",
      "vector_technical",
      "graph",
      "project",
      "session_chunks",
    ]);
    expect(renderRoutableKnowledgeDatastoreRouterGuidance()).toBe([
      "Stores:",
      "- vector_basic: Personal facts, preferences, and relationship-adjacent memory facts.",
      "- vector_technical: Technical and project-state facts (bugs, tests, versions, architecture changes).",
      "- graph: Relationship and entity graph traversal (multi-hop links).",
      "- journal: Distilled reflective context from journal files.",
      "- project: Project documentation recall from docs index.",
      "Cost/latency priority:",
      "1) vector_basic first (cheap; use liberally)",
      "2) vector_technical/graph",
      "3) project/journal when needed for precision",
      "4) broader historical/session retrieval only when prior stores are insufficient",
    ].join("\n"));
  });

  it("preserves M6.1 bridge mapping and datastore execution metadata", () => {
    expect(getHandlerStoreForKnowledgeDatastore("vector_basic")).toBe("vector");
    expect(getHandlerStoreForKnowledgeDatastore("vector_technical")).toBe("vector");
    expect(getHandlerStoreForKnowledgeDatastore("project")).toBe("docs");
    expect(getHandlerStoreForKnowledgeDatastore("session_chunks")).toBe("session_chunks");
    expect(getHandlerStoreForKnowledgeDatastore("source_chunks")).toBe("session_chunks");
    expect(getDefaultDomainForKnowledgeDatastore("vector_basic")).toEqual({ personal: true });
    expect(getDefaultDomainForKnowledgeDatastore("vector_technical")).toEqual({ technical: true });
    expect(datastoreUsesCandidatePool("graph")).toBe(true);
    expect(datastoreUsesCandidatePool("project")).toBe(false);
  });

  it("throws when router fails and fail-open is not enabled", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => {
        throw new Error("offline");
      }),
      recallMemory: vi.fn(async () => []),
    });

    await expect(engine.routeKnowledgeDatastores("Tell me about family relationships", true))
      .rejects.toThrow("offline");
  });

  it("uses deterministic default recall plan when router fails and fail-open is enabled", async () => {
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        return [{ text: "A --related--> B", category: "graph", similarity: 0.7, via: "graph" }];
      }
      return [{ text: "fallback-hit", category: "fact", similarity: 0.81, via: "vector" }];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => {
        throw new Error("offline");
      }),
      recallMemory,
    });

    const out = await engine.recall("Tell me about family relationships", 5, {
      datastores: [],
      expandGraph: true,
      graphDepth: 1,
      domain: { personal: true },
      reasoning: "fast",
      failOpen: true,
    });

    expect(recallMemory).toHaveBeenCalled();
    expect(out.length).toBeGreaterThan(0);
    expect(out[0].text).toContain("[RECALL ROUTER WARNING]");
  });

  it("executes fail-open flat default stores in registry order", async () => {
    const calls: string[] = [];
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      calls.push(String(opts.stores?.[0] || ""));
      return [{ text: "vector default", category: "fact", similarity: 0.83, via: "vector_basic" }];
    });
    const recallJournalStore = vi.fn(async () => {
      calls.push("journal");
      return [{ text: "journal default", category: "journal", similarity: 0.74, via: "journal" }];
    });
    const recallProjectStore = vi.fn(async () => {
      calls.push("project");
      return [{ text: "project default", category: "project", similarity: 0.72, via: "project" }];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false } }),
      isSystemEnabled: (name) => name === "journal" || name === "projects",
      callFastRouter: vi.fn(async () => {
        throw new Error("offline");
      }),
      recallMemory,
      recallJournalStore,
      recallProjectStore,
    });

    await engine.recall("default flat order", 5, {
      datastores: [],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      reasoning: "fast",
      failOpen: true,
    });

    expect(calls).toEqual(["vector_basic", "journal", "project"]);
  });

  it("throttles repeated router fallback warning notices per reasoning tier", async () => {
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        return [{ text: "graph-hit", category: "graph", similarity: 0.72, via: "graph" }];
      }
      return [{ text: "fallback-hit", category: "fact", similarity: 0.81, via: "vector" }];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false, routerWarningCooldownMinutes: 15 } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => {
        throw new Error("offline");
      }),
      recallMemory,
    });

    const first = await engine.recall("Tell me about family relationships", 5, {
      datastores: [],
      expandGraph: true,
      graphDepth: 1,
      domain: { personal: true },
      reasoning: "fast",
      failOpen: true,
    });
    const second = await engine.recall("Tell me about family relationships", 5, {
      datastores: [],
      expandGraph: true,
      graphDepth: 1,
      domain: { personal: true },
      reasoning: "fast",
      failOpen: true,
    });

    expect(first.some((row) => row.text.includes("[RECALL ROUTER WARNING]"))).toBe(true);
    expect(second.some((row) => row.text.includes("[RECALL ROUTER WARNING]"))).toBe(false);
    expect(second.some((row) => row.text.includes("fallback-hit"))).toBe(true);
  });

  it("rejects invalid datastore arrays from router plan", async () => {
    const callFastRouter = vi
      .fn(async () => '{"query":"one","datastores":["not_real"]}')
      .mockResolvedValueOnce('{"query":"two","datastores":["still_wrong"]}');

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory: vi.fn(async () => []),
    });

    await expect(engine.routeRecallPlan("x", false, "fast"))
      .rejects.toThrow("failed to produce valid structured output");
    expect(callFastRouter).toHaveBeenCalledTimes(2);
  });

  it("rejects session_chunks when returned by the LLM router", async () => {
    const callFastRouter = vi
      .fn(async () => '{"query":"one","datastores":["session_chunks"]}')
      .mockResolvedValueOnce('{"query":"two","datastores":["session_chunks"]}');

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory: vi.fn(async () => []),
    });

    await expect(engine.routeRecallPlan("x", false, "fast"))
      .rejects.toThrow("router returned no valid datastores");
    expect(callFastRouter).toHaveBeenCalledTimes(2);
  });

  it("builds router prompts from core datastore registry guidance", async () => {
    const callFastRouter = vi.fn(async () => '{"datastores":["vector_basic"]}');
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory: vi.fn(async () => []),
    });

    await engine.routeKnowledgeDatastores("x", false);

    const systemPrompt = callFastRouter.mock.calls[0][0];
    const registryGuidance = renderRoutableKnowledgeDatastoreRouterGuidance();
    expect(systemPrompt).toContain(registryGuidance);
    expect(systemPrompt).not.toContain("session_chunks");
  });

  it("preserves first and retry validation errors from router repair flow", async () => {
    const callFastRouter = vi
      .fn(async () => '{"query":"one","datastores":["not_real"]}')
      .mockResolvedValueOnce('{"query":"two","datastores":["still_wrong"]}');

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory: vi.fn(async () => []),
    });

    try {
      await engine.routeRecallPlan("x", false, "fast");
      throw new Error("expected routeRecallPlan to fail");
    } catch (err: unknown) {
      const asError = err as Error;
      const msg = String(asError?.message || err);
      expect(msg).toContain("First validation error: router returned no valid datastores");
      expect(msg).toContain("Retry validation error: router returned no valid datastores");
      expect(String((asError?.cause as Error)?.message || asError?.cause || "")).toContain(
        "router returned no valid datastores",
      );
    }
  });

  it("skips router when datastores are explicitly supplied to recall", async () => {
    const callFastRouter = vi.fn(async () => '{"datastores":["graph"]}');
    const recallMemory = vi.fn(async () => [
      { text: "alpha", category: "fact", similarity: 0.8, via: "vector" },
    ]);

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory,
    });

    const out = await engine.recall("alpha", 3, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      domain: { personal: true },
    });

    expect(callFastRouter).not.toHaveBeenCalled();
    expect(recallMemory).toHaveBeenCalledTimes(1);
    expect(out.length).toBe(1);
  });

  it("aggregates and deduplicates across datastores", async () => {
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        return [{ text: "Alpha --related--> Beta", category: "graph", similarity: 0.75, via: "graph" }];
      }
      return [
        { id: "a", text: "Alpha", category: "fact", similarity: 0.7, via: "vector" },
        { id: "a", text: "Alpha", category: "fact", similarity: 0.9, via: "vector" },
      ];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallMemory,
    });

    const results = await engine.recall("alpha", 10, {
      datastores: ["vector_basic", "graph"],
      expandGraph: true,
      graphDepth: 1,
      domain: { personal: true },
    });

    expect(results.length).toBe(2);
    expect(results[0].similarity).toBe(0.9);
    expect(results.some((r) => r.category === "graph")).toBe(true);
  });

  it("keeps graph on direct memory descriptor with vector candidate pool", async () => {
    const vectorRows = [
      { id: "v1", text: "Alpha vector anchor", category: "fact", similarity: 0.91, via: "vector" },
    ];
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        return [{ text: "Alpha --related--> Beta", category: "graph", similarity: 0.75, via: "graph" }];
      }
      return vectorRows;
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic","graph"]}'),
      recallMemory,
    });

    await engine.recall("alpha", 10, {
      datastores: ["vector_basic", "graph"],
      expandGraph: true,
      graphDepth: 2,
      domain: { personal: true },
    });

    expect(recallMemory).toHaveBeenNthCalledWith(
      2,
      "alpha",
      10,
      expect.objectContaining({
        stores: ["graph"],
        depth: 2,
        candidatePool: vectorRows,
      }),
    );
  });

  it("preserves partial recall results when one datastore fails and failHard is disabled", async () => {
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        throw new Error("graph backend unavailable");
      }
      return [{ text: "vector survives", category: "fact", similarity: 0.8, via: "vector" }];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic","graph"]}'),
      recallMemory,
    });

    const results = await engine.recall("alpha", 5, {
      datastores: ["vector_basic", "graph"],
      expandGraph: true,
      graphDepth: 1,
      domain: { all: true },
    });

    expect(results.some((r) => r.text === "vector survives")).toBe(true);
  });

  it("logs and preserves vector rows when project descriptor fails without failHard", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const recallMemory = vi.fn(async () => [
      { text: "vector survives", category: "fact", similarity: 0.8, via: "vector" },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: false }, docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore: vi.fn(async () => {
        throw new Error("project backend unavailable");
      }),
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic","project"]}'),
      recallMemory,
    });

    try {
      const results = await engine.recall("alpha", 5, {
        datastores: ["vector_basic", "project"],
        expandGraph: false,
        graphDepth: 1,
        domain: { all: true },
      });

      expect(results.map((r) => r.text)).toContain("vector survives");
      expect(warn).toHaveBeenCalledWith(expect.stringContaining("datastore=project failed: project backend unavailable"));
    } finally {
      warn.mockRestore();
    }
  });

  it("raises project descriptor failures when failHard is enabled", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ retrieval: { failHard: true }, docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore: vi.fn(async () => {
        throw new Error("project backend unavailable");
      }),
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallMemory: vi.fn(async () => []),
    });

    await expect(engine.recall("alpha", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
    })).rejects.toThrow("project backend unavailable");
  });

  it("passes project/docs filters through project store recall", async () => {
    const recallProjectStore = vi.fn(async () => [
      { text: "PROJECT.md > Overview", category: "project", similarity: 0.88, via: "project" },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore,
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallMemory: vi.fn(async () => []),
    });

    const results = await engine.recall("architecture", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      project: "quaid",
      docs: ["PROJECT.md", "reference/memory-local-implementation.md"],
    });

    expect(recallProjectStore).toHaveBeenCalledWith(
      "architecture",
      5,
      "quaid",
      ["PROJECT.md", "reference/memory-local-implementation.md"],
      undefined,
      undefined,
    );
    expect(results.length).toBe(1);
    expect(results[0].category).toBe("project");
  });

  it("passes project date bounds and preserves project row metadata", async () => {
    const recallProjectStore = vi.fn(async () => [
      {
        text: "PROJECT.log > 2026-03-15: Recipe App shipped",
        category: "project",
        similarity: 0.92,
        via: "project",
        sourceType: "docs",
      },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore,
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallMemory: vi.fn(async () => []),
    });

    const results = await engine.recall("recipe app shipped", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      project: "recipe-app",
      docs: ["PROJECT.log"],
      dateFrom: "2026-03-01",
      dateTo: "2026-03-31",
    });

    expect(recallProjectStore).toHaveBeenCalledWith(
      "recipe app shipped",
      5,
      "recipe-app",
      ["PROJECT.log"],
      "2026-03-01",
      "2026-03-31",
    );
    expect(results[0]).toMatchObject({
      text: "PROJECT.log > 2026-03-15: Recipe App shipped",
      category: "project",
      via: "project",
      sourceType: "docs",
    });
  });

  it("preserves explicit project rows in mixed-store recall", async () => {
    const recallProjectStore = vi.fn(async () => [
      {
        text: "~/projects/portfolio-site/PROJECT.log: Projects on the site: Recipe App; TechFlow Platform Redesign",
        category: "project",
        similarity: 0.52,
        via: "project",
        sourceType: "docs",
      },
    ]);
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("vector_basic")) {
        return [
          { text: "Maya asked about what projects were on the portfolio site as of 2026-03-15", category: "fact", similarity: 0.99, via: "vector" },
          { text: "The assistant did not have specific project names for the portfolio site's Projects section as of 2026-03-15", category: "fact", similarity: 0.98, via: "vector" },
        ];
      }
      return [];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore,
      callFastRouter: vi.fn(async () => '{"datastores":["project","vector_basic"]}'),
      recallMemory,
    });

    const results = await engine.recall("As of 2026-03-15, what projects were on the portfolio site?", 2, {
      datastores: ["project", "vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      project: "portfolio-site",
    });

    expect(results).toHaveLength(2);
    const projectResult = results.find((item) => item.via === "project");
    expect(projectResult).toMatchObject({ category: "project", sourceType: "docs" });
    expect(projectResult?.text).toContain("Projects on the site: Recipe App");
  });

  it("applies datastoreOptions override for project store scope", async () => {
    const recallProjectStore = vi.fn(async () => [
      { text: "PROJECT.md > Overview", category: "project", similarity: 0.88, via: "project" },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore,
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallMemory: vi.fn(async () => []),
    });

    await engine.recall("architecture", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      project: "wrong-default",
      docs: ["wrong.md"],
      datastoreOptions: {
        project: {
          project: "quaid",
          docs: ["PROJECT.md"],
        },
      },
    });

    expect(recallProjectStore).toHaveBeenCalledWith(
      "architecture",
      5,
      "quaid",
      ["PROJECT.md"],
      undefined,
      undefined,
    );
  });

  it("keeps aggregate vector on direct memory descriptor with datastoreOptions override", async () => {
    const recallMemory = vi.fn(async () => []);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector"]}'),
      recallMemory,
    });

    await engine.recall("api limits", 3, {
      datastores: ["vector"],
      expandGraph: false,
      graphDepth: 1,
      domain: { personal: true },
      project: "quaid",
      dateFrom: "2026-01-01",
      dateTo: "2026-01-31",
      datastoreOptions: {
        vector: { domain: { technical: true } },
      },
    });

    expect(recallMemory).toHaveBeenCalledWith(
      "api limits",
      3,
      expect.objectContaining({
        stores: ["vector"],
        domain: { technical: true },
        project: "quaid",
        dateFrom: "2026-01-01",
        dateTo: "2026-01-31",
      }),
    );
  });

  it("keeps vector_basic and vector_technical on direct memory descriptors with default domains", async () => {
    const recallMemory = vi.fn(async () => []);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallMemory,
    });

    await engine.recall("personal preference", 3, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
    });
    await engine.recall("api limit", 3, {
      datastores: ["vector_technical"],
      expandGraph: false,
      graphDepth: 1,
    });

    expect(recallMemory).toHaveBeenNthCalledWith(
      1,
      "personal preference",
      3,
      expect.objectContaining({
        stores: ["vector_basic"],
        domain: { personal: true },
      }),
    );
    expect(recallMemory).toHaveBeenNthCalledWith(
      2,
      "api limit",
      3,
      expect.objectContaining({
        stores: ["vector_technical"],
        domain: { technical: true },
      }),
    );
  });

  it("limits memory selector overrides to current top-level descriptor options", async () => {
    const recallMemory = vi.fn(async () => []);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallMemory,
    });

    await engine.recall("personal scoped", 3, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      domainBoost: { personal: 1.4 },
      project: "quaid",
      dateFrom: "2026-02-01",
      dateTo: "2026-02-28",
      fast: true,
      datastoreOptions: {
        vector_basic: { domain: { technical: true }, project: "wrong-project" },
      },
    });
    await engine.recall("technical scoped", 3, {
      datastores: ["vector_technical"],
      expandGraph: false,
      graphDepth: 1,
      domain: { technical: true },
      domainBoost: { technical: 1.7 },
      project: "quaid-runtime",
      dateFrom: "2026-03-01",
      dateTo: "2026-03-31",
      fast: true,
      datastoreOptions: {
        vector_technical: { domain: { personal: true }, project: "wrong-project" },
      },
    });

    expect(recallMemory).toHaveBeenNthCalledWith(
      1,
      "personal scoped",
      3,
      expect.objectContaining({
        stores: ["vector_basic"],
        domain: { all: true },
        domainBoost: { personal: 1.4 },
        project: "quaid",
        dateFrom: "2026-02-01",
        dateTo: "2026-02-28",
        fast: true,
      }),
    );
    expect(recallMemory).toHaveBeenNthCalledWith(
      2,
      "technical scoped",
      3,
      expect.objectContaining({
        stores: ["vector_technical"],
        domain: { technical: true },
        domainBoost: { technical: 1.7 },
        project: "quaid-runtime",
        dateFrom: "2026-03-01",
        dateTo: "2026-03-31",
        fast: true,
      }),
    );
  });

  it("keeps memory selectors from consuming candidate pools while seeding graph", async () => {
    const basicRows = [
      { id: "basic-1", text: "Personal anchor", category: "fact", similarity: 0.91, via: "vector_basic" },
    ];
    const technicalRows = [
      { id: "tech-1", text: "Technical anchor", category: "fact", similarity: 0.89, via: "vector_technical" },
    ];
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("vector_basic")) return basicRows;
      if (opts.stores?.includes("vector_technical")) return technicalRows;
      if (opts.stores?.includes("graph")) {
        return [{ text: "Personal anchor --related--> technical anchor", category: "graph", similarity: 0.72, via: "graph" }];
      }
      return [];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic","vector_technical","graph"]}'),
      recallMemory,
    });

    await engine.recall("anchor", 4, {
      datastores: ["vector_basic", "vector_technical", "graph"],
      expandGraph: true,
      graphDepth: 2,
      domain: { all: true },
    });

    expect(recallMemory.mock.calls[0][2]).toEqual(expect.objectContaining({
      stores: ["vector_basic"],
      domain: { all: true },
    }));
    expect(recallMemory.mock.calls[0][2]).not.toHaveProperty("candidatePool");
    expect(recallMemory.mock.calls[1][2]).toEqual(expect.objectContaining({
      stores: ["vector_technical"],
      domain: { all: true },
    }));
    expect(recallMemory.mock.calls[1][2]).not.toHaveProperty("candidatePool");
    expect(recallMemory.mock.calls[2][2]).toEqual(expect.objectContaining({
      stores: ["graph"],
      depth: 2,
      candidatePool: [...basicRows, ...technicalRows],
    }));
  });

  it("keeps journal on direct journal store descriptor", async () => {
    const recallMemory = vi.fn(async () => {
      throw new Error("journal must not use memory recall");
    });
    const recallJournalStore = vi.fn(async () => [
      { text: "journal reflection", category: "journal", similarity: 0.7, via: "journal" },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "journal",
      callFastRouter: vi.fn(async () => '{"datastores":["journal"]}'),
      recallMemory,
      recallJournalStore,
    });

    const out = await engine.recall("reflection", 3, {
      datastores: ["journal"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
    });

    expect(recallJournalStore).toHaveBeenCalledWith("reflection", 3);
    expect(recallMemory).not.toHaveBeenCalled();
    expect(out[0]).toMatchObject({ category: "journal", via: "journal" });
  });

  it("runs session_chunks/source_chunks only when explicitly requested and preserves chunk metadata", async () => {
    const recallMemory = vi.fn(async () => [
      {
        text: "[session_chunk] session-1#0: User: exact transcript context",
        category: "session_chunk",
        similarity: 0.94,
        sourceType: "session_chunk",
        sourceChunkId: "sch_test",
        chunkId: "sch_test",
        outputTokenCount: 4,
        truncated: false,
        via: "session_chunks",
      },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector"]}'),
      recallMemory,
    });

    const out = await engine.recall("exact transcript context", 3, {
      datastores: ["session_chunks"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      maxChunkTokens: 12,
      maxTotalChunkTokens: 20,
    });

    expect(recallMemory).toHaveBeenCalledWith(
      "exact transcript context",
      3,
      expect.objectContaining({
        stores: ["session_chunks"],
        maxChunkTokens: 12,
        maxTotalChunkTokens: 20,
      }),
    );
    expect(out[0]).toMatchObject({
      category: "session_chunk",
      via: "session_chunks",
      sourceChunkId: "sch_test",
      chunkId: "sch_test",
      outputTokenCount: 4,
      truncated: false,
    });

    await engine.recall("exact transcript alias", 2, {
      datastores: ["source_chunks" as any],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      maxChunkTokens: 8,
      maxTotalChunkTokens: 13,
    });

    expect(recallMemory).toHaveBeenLastCalledWith(
      "exact transcript alias",
      2,
      expect.objectContaining({
        stores: ["session_chunks"],
        maxChunkTokens: 8,
        maxTotalChunkTokens: 13,
      }),
    );
  });

  it("handles recall planning within latency budget for mocked dependencies", async () => {
    const recallMemory = vi.fn(async (_query: string, _limit: number, opts: any) => {
      if (opts.stores?.includes("graph")) {
        return [{ text: "alpha->beta", category: "graph", similarity: 0.7, via: "graph" }];
      }
      return [{ text: "alpha", category: "fact", similarity: 0.8, via: "vector" }];
    });
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"query":"alpha","datastores":["vector_basic","graph"]}'),
      recallMemory,
    });

    const started = Date.now();
    const out = await engine.recall("alpha", 5, {
      datastores: [],
      expandGraph: true,
      graphDepth: 1,
      domain: { all: true },
      reasoning: "fast",
    });
    const elapsedMs = Date.now() - started;

    expect(out.length).toBeGreaterThan(0);
    expect(elapsedMs).toBeLessThan(2000);
  });

  it("uses deep router for recall when reasoning=deep and accepts known project", async () => {
    const callFastRouter = vi.fn(async () => '{"datastores":["vector_basic"]}');
    const callDeepRouter = vi.fn(async () => JSON.stringify({
      query: "quaid architecture docs",
      datastores: ["project"],
      project: "quaid",
    }));
    const recallProjectStore = vi.fn(async () => [
      { text: "PROJECT.md > Overview", category: "project", similarity: 0.9, via: "project" },
    ]);

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      recallProjectStore,
      callFastRouter,
      callDeepRouter,
      getProjectCatalog: () => [{ name: "quaid", description: "Knowledge layer project docs." }],
      recallMemory: vi.fn(async () => []),
    });

    const results = await engine.recall("tell me about quaid architecture", 5, {
      datastores: [],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      reasoning: "deep",
    });

    expect(callDeepRouter).toHaveBeenCalledTimes(1);
    // Single prepass policy: no extra fast-router fallback call.
    expect(callFastRouter).toHaveBeenCalledTimes(0);
    expect(recallProjectStore).toHaveBeenCalledWith(
      "quaid architecture docs",
      5,
      "quaid",
      undefined,
      undefined,
      undefined,
    );
    expect(results.length).toBe(1);
  });

  it("drops unknown routed project names from plan", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => JSON.stringify({
        query: "x",
        datastores: ["project"],
        project: "not-a-known-project",
      })),
      getProjectCatalog: () => [{ name: "quaid", description: "Main project" }],
      recallMemory: vi.fn(async () => []),
    });

    const plan = await engine.routeRecallPlan("x", false, "fast");
    expect(plan.project).toBeUndefined();
    expect(plan.datastores).toEqual(["project"]);
  });

  it("augments routed plans with project store for explicit known project detail queries", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => JSON.stringify({
        query: "As of 2026-03-15, what projects were on Maya's portfolio site?",
        datastores: ["vector_basic"],
        project: null,
      })),
      getProjectCatalog: () => [{ name: "portfolio-site", description: "Maya portfolio site" }],
      recallMemory: vi.fn(async () => []),
    });

    const plan = await engine.routeRecallPlan(
      "As of 2026-03-15, what projects were on Maya's portfolio site?",
      false,
      "fast",
    );

    expect(plan.project).toBe("portfolio-site");
    expect(plan.datastores).toEqual(["project", "vector_basic"]);
  });

  it("applies source-type boosts for agent_actions intent", async () => {
    const recallMemory = vi.fn(async () => [
      { text: "User mentioned snacks", category: "fact", similarity: 0.82, sourceType: "user", via: "vector" },
      { text: "Agent suggested a split test", category: "fact", similarity: 0.79, sourceType: "assistant", via: "vector" },
    ]);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallMemory,
    });

    const results = await engine.recall("what did the assistant suggest", 5, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      domain: { all: true },
      intent: "agent_actions",
    });

    expect(results[0].text).toContain("Agent suggested");
  });

  it("passes intent facet into routeRecallPlan prompt", async () => {
    const callFastRouter = vi.fn(async () => '{"query":"x","datastores":["vector_basic"]}');
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter,
      recallMemory: vi.fn(async () => []),
    });

    await engine.routeRecallPlan("what did the assistant do", true, "fast", "agent_actions");
    const userPrompts = callFastRouter.mock.calls.map((c) => String(c?.[1] || ""));
    const systemPrompts = callFastRouter.mock.calls.map((c) => String(c?.[0] || ""));
    expect(userPrompts.some((p) => p.includes("intent: agent_actions"))).toBe(true);
    expect(systemPrompts.some((p) => p.includes("Language fidelity:"))).toBe(true);
  });

  it("exposes store registry metadata from core", () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => ""),
      recallMemory: vi.fn(async () => []),
    });

    const datastores = engine.getKnowledgeDatastoreRegistry();
    expect(datastores.some((s) => s.key === "vector_basic")).toBe(true);
    expect(datastores.some((s) => s.key === "project")).toBe(true);
    const graph = datastores.find((s) => s.key === "graph");
    expect(graph?.options.some((o) => o.key === "depth")).toBe(true);
  });

  it("renders agent-facing store guidance from registry metadata", () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callFastRouter: vi.fn(async () => ""),
      recallMemory: vi.fn(async () => []),
    });

    const text = engine.renderKnowledgeDatastoreGuidanceForAgents();
    expect(text).toContain("Knowledge datastores:");
    expect(text).toContain("vector_basic");
    expect(text).toContain("project");
    expect(text).toContain("depth");
  });
});
