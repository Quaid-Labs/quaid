import { describe, expect, it, vi } from "vitest";
import { createKnowledgeEngine } from "../adapters/openclaw/knowledge/orchestrator.js";

type Result = {
  text: string;
  category: string;
  similarity: number;
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

    expect(engine.normalizeKnowledgeStores(undefined, true)).toEqual([
      "vector_basic",
      "graph",
      "journal",
      "project",
    ]);
    expect(engine.normalizeKnowledgeStores(["vector_basic", "nope", "graph"], false)).toEqual([
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

    const stores = await engine.routeKnowledgeStores("Tell me about family relationships", true);
    expect(stores).toContain("vector_basic");
    expect(stores).toContain("graph");
  });

  it("aggregates and deduplicates across stores", async () => {
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
      callFastRouter: vi.fn(async () => '{"stores":["vector_basic"]}'),
      recallVector: vi.fn(async () => [
        { id: "a", text: "Alpha", category: "fact", similarity: 0.7, via: "vector" },
        { id: "a", text: "Alpha", category: "fact", similarity: 0.9, via: "vector" },
      ]),
      recallGraph: vi.fn(async () => [
        { text: "Alpha --related--> Beta", category: "graph", similarity: 0.75, via: "graph" },
      ]),
    });

    const results = await engine.totalRecall("alpha", 10, {
      stores: ["vector_basic", "graph"],
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
      callFastRouter: vi.fn(async () => '{"stores":["project"]}'),
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const results = await engine.totalRecall("architecture", 5, {
      stores: ["project"],
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

  it("uses deep router for total_recall when reasoning=deep and accepts known project", async () => {
    const callFastRouter = vi.fn(async () => '{"stores":["vector_basic"]}');
    const callDeepRouter = vi.fn(async () => JSON.stringify({
      query: "quaid architecture docs",
      stores: ["project"],
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
      stores: [],
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
        stores: ["project"],
        project: "not-a-known-project",
      })),
      getProjectCatalog: () => [{ name: "quaid", description: "Main project" }],
      recallVector: vi.fn(async () => []),
      recallGraph: vi.fn(async () => []),
    });

    const plan = await engine.routeRecallPlan("x", false, "fast");
    expect(plan.project).toBeUndefined();
    expect(plan.stores).toEqual(["project"]);
  });
});
