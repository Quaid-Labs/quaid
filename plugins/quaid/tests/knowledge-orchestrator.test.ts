import { describe, expect, it, vi } from "vitest";
import { createKnowledgeEngine } from "../adapters/openclaw/knowledge/orchestrator.js";

type Result = {
  text: string;
  category: string;
  similarity: number;
  sourceType?: string;
  id?: string;
  via?: string;
};

describe("knowledge orchestrator", () => {
  it("normalizes store defaults and removes invalid entries", () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => ""),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
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
  });

  it("routes with heuristic fallback when router fails", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => {
        throw new Error("offline");
      }),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const datastores = await engine.routeKnowledgeDatastores("Tell me about family relationships", true);
    expect(datastores).toContain("vector_basic");
    expect(datastores).toContain("graph");
  });

  it("skips router when datastores are explicitly supplied to totalRecall", async () => {
    const callFastRouter = vi.fn(async () => '{"datastores":["graph"]}');
    const recallVector = vi.fn(async () => [
      { text: "alpha", category: "fact", similarity: 0.8, via: "vector" },
    ]);

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter,
      recallVector,
      recallGraph: vi.fn(async () => []),
    });

    const out = await engine.totalRecall("alpha", 3, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "personal",
    });

    expect(callFastRouter).not.toHaveBeenCalled();
    expect(recallVector).toHaveBeenCalledTimes(1);
    expect(out.length).toBe(1);
  });

  it("aggregates and deduplicates across datastores", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {
        join: (...parts: string[]) => parts.join("/"),
      } as any,
      fs: {
        readdirSync: vi.fn(() => []),
        readFileSync: vi.fn(() => ""),
      } as any,
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallVector: vi.fn(async () => [
        { id: "a", text: "Alpha", category: "fact", similarity: 0.7, via: "vector" },
        { id: "a", text: "Alpha", category: "fact", similarity: 0.9, via: "vector" },
      ]),
      recallGraph: vi.fn(async () => [
        { text: "Alpha --related--> Beta", category: "graph", similarity: 0.75, via: "graph" },
      ]),
    });

    const results = await engine.totalRecall("alpha", 10, {
      datastores: ["vector_basic", "graph"],
      expandGraph: true,
      graphDepth: 1,
      technicalScope: "personal",
    });

    expect(results.length).toBe(2);
    expect(results[0].similarity).toBe(0.9);
    expect(results.some((r) => r.category === "graph")).toBe(true);
  });

  it("passes project/docs filters through project store recall", async () => {
    const callDocsRag = vi.fn(async () => "1. ~/projects/quaid/PROJECT.md > Overview (similarity: 0.88)");
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {
        join: (...parts: string[]) => parts.join("/"),
      } as any,
      fs: {
        readdirSync: vi.fn(() => []),
        readFileSync: vi.fn(() => ""),
      } as any,
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      callDocsRag,
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const results = await engine.totalRecall("architecture", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "any",
      project: "quaid",
      docs: ["PROJECT.md", "reference/memory-local-implementation.md"],
    });

    expect(callDocsRag).toHaveBeenCalledWith("search", [
      "architecture",
      "--limit",
      "5",
      "--project",
      "quaid",
      "--docs",
      "PROJECT.md,reference/memory-local-implementation.md",
    ]);
    expect(results.length).toBe(1);
    expect(results[0].category).toBe("project");
  });

  it("applies datastoreOptions override for project store scope", async () => {
    const callDocsRag = vi.fn(async () => "1. ~/projects/quaid/PROJECT.md > Overview (similarity: 0.88)");
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: { join: (...parts: string[]) => parts.join("/") } as any,
      fs: { readdirSync: vi.fn(() => []), readFileSync: vi.fn(() => "") } as any,
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      callDocsRag,
      callFastRouter: vi.fn(async () => '{"datastores":["project"]}'),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    await engine.totalRecall("architecture", 5, {
      datastores: ["project"],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "any",
      project: "wrong-default",
      docs: ["wrong.md"],
      datastoreOptions: {
        project: {
          project: "quaid",
          docs: ["PROJECT.md"],
        },
      },
    });

    expect(callDocsRag).toHaveBeenCalledWith("search", [
      "architecture",
      "--limit",
      "5",
      "--project",
      "quaid",
      "--docs",
      "PROJECT.md",
    ]);
  });

  it("applies datastoreOptions override for vector technical scope", async () => {
    const recallVector = vi.fn(async () => []);
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => '{"datastores":["vector"]}'),
      recallVector,
      recallGraph: vi.fn(async () => []),
    });

    await engine.totalRecall("api limits", 3, {
      datastores: ["vector"],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "personal",
      datastoreOptions: {
        vector: { technicalScope: "technical" },
      },
    });

    expect(recallVector).toHaveBeenCalledWith("api limits", 3, "technical", undefined, undefined);
  });

  it("handles total_recall planning within latency budget for mocked dependencies", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => '{"query":"alpha","datastores":["vector_basic","graph"]}'),
      recallVector: vi.fn(async () => [{ text: "alpha", category: "fact", similarity: 0.8, via: "vector" }]),
      recallGraph: vi.fn(async () => [{ text: "alpha->beta", category: "graph", similarity: 0.7, via: "graph" }]),
    });

    const started = Date.now();
    const out = await engine.total_recall("alpha", 5, {
      datastores: [],
      expandGraph: true,
      graphDepth: 1,
      technicalScope: "any",
      reasoning: "fast",
    });
    const elapsedMs = Date.now() - started;

    expect(out.length).toBeGreaterThan(0);
    expect(elapsedMs).toBeLessThan(2000);
  });

  it("uses deep router for total_recall when reasoning=deep and accepts known project", async () => {
    const callFastRouter = vi.fn(async () => '{"datastores":["vector_basic"]}');
    const callDeepRouter = vi.fn(async () => JSON.stringify({
      query: "quaid architecture docs",
      datastores: ["project"],
      project: "quaid",
    }));
    const callDocsRag = vi.fn(async () => "1. ~/projects/quaid/PROJECT.md > Overview (similarity: 0.9)");

    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: { join: (...parts: string[]) => parts.join("/") } as any,
      fs: { readdirSync: vi.fn(() => []), readFileSync: vi.fn(() => "") } as any,
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: (name) => name === "projects",
      callDocsRag,
      callFastRouter,
      callDeepRouter,
      getProjectCatalog: () => [{ name: "quaid", description: "Knowledge layer project docs." }],
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const results = await engine.total_recall("tell me about quaid architecture", 5, {
      datastores: [],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "any",
      reasoning: "deep",
    });

    expect(callDeepRouter).toHaveBeenCalledTimes(1);
    expect(callFastRouter).toHaveBeenCalledTimes(1); // fallback store routing pass
    expect(callDocsRag).toHaveBeenCalledWith("search", [
      "quaid architecture docs",
      "--limit",
      "5",
      "--project",
      "quaid",
    ]);
    expect(results.length).toBe(1);
  });

  it("drops unknown routed project names from plan", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => JSON.stringify({
        query: "x",
        datastores: ["project"],
        project: "not-a-known-project",
      })),
      getProjectCatalog: () => [{ name: "quaid", description: "Main project" }],
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const plan = await engine.routeRecallPlan("x", false, "fast");
    expect(plan.project).toBeUndefined();
    expect(plan.datastores).toEqual(["project"]);
  });

  it("applies source-type boosts for agent_actions intent", async () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: { join: (...parts: string[]) => parts.join("/") } as any,
      fs: { readdirSync: vi.fn(() => []), readFileSync: vi.fn(() => "") } as any,
      getMemoryConfig: () => ({ docs: { journal: { journalDir: "journal" } } }),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => '{"datastores":["vector_basic"]}'),
      recallVector: vi.fn(async () => [
        { text: "User mentioned snacks", category: "fact", similarity: 0.82, sourceType: "user", via: "vector" },
        { text: "Agent suggested a split test", category: "fact", similarity: 0.79, sourceType: "assistant", via: "vector" },
      ]),
      recallGraph: vi.fn(async () => []),
    });

    const results = await engine.totalRecall("what did the assistant suggest", 5, {
      datastores: ["vector_basic"],
      expandGraph: false,
      graphDepth: 1,
      technicalScope: "any",
      intent: "agent_actions",
    });

    expect(results[0].text).toContain("Agent suggested");
  });

  it("passes intent facet into routeRecallPlan prompt", async () => {
    const callFastRouter = vi.fn(async () => '{"query":"x","datastores":["vector_basic"]}');
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter,
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    await engine.routeRecallPlan("what did the assistant do", true, "fast", "agent_actions");
    const prompts = callFastRouter.mock.calls.map((c) => String(c?.[1] || ""));
    expect(prompts.some((p) => p.includes("intent: agent_actions"))).toBe(true);
  });

  it("exposes store registry metadata from core", () => {
    const engine = createKnowledgeEngine<Result>({
      workspace: "/tmp",
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => ""),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
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
      path: {} as any,
      fs: {} as any,
      getMemoryConfig: () => ({}),
      isSystemEnabled: () => false,
      callDocsRag: vi.fn(async () => ""),
      callFastRouter: vi.fn(async () => ""),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const text = engine.renderKnowledgeDatastoreGuidanceForAgents();
    expect(text).toContain("Knowledge datastores:");
    expect(text).toContain("vector_basic");
    expect(text).toContain("project");
    expect(text).toContain("depth");
  });
});
