import type * as pathType from "node:path";
import type * as fsType from "node:fs";
import {
  getKnowledgeStoreRegistry,
  getRoutableStoreKeys,
  normalizeKnowledgeStores,
  renderKnowledgeStoreGuidanceForAgents,
} from "../../../core/knowledge-stores.js";
import type {
  KnowledgeStore,
  TechnicalScope,
  RecallIntent,
  SourceType,
} from "../../../core/knowledge-stores.js";

export type TotalRecallOptions = {
  stores: KnowledgeStore[];
  expandGraph: boolean;
  graphDepth: number;
  technicalScope: TechnicalScope;
  intent?: RecallIntent;
  ranking?: {
    sourceTypeBoosts?: Partial<Record<SourceType, number>>;
  };
  dateFrom?: string;
  dateTo?: string;
  project?: string;
  docs?: string[];
  storeOptions?: Partial<Record<KnowledgeStore, Record<string, unknown>>>;
  reasoning?: "fast" | "deep";
};

export type RoutedRecallPlan = {
  query: string;
  stores: KnowledgeStore[];
  project?: string;
};

type KnowledgeEngineDeps<TMemoryResult extends { text: string; similarity: number; id?: string; category: string; via?: string; sourceType?: string }> = {
  workspace: string;
  path: typeof pathType;
  fs: typeof fsType;
  getMemoryConfig: () => any;
  isSystemEnabled: (system: "memory" | "journal" | "projects" | "workspace") => boolean;
  callDocsRag: (command: "search" | "index" | "stats", args: string[]) => Promise<string>;
  callFastRouter: (systemPrompt: string, userPrompt: string) => Promise<string>;
  callDeepRouter?: (systemPrompt: string, userPrompt: string) => Promise<string>;
  getProjectCatalog?: () => Array<{ name: string; description: string }>;
  recallVector: (
    query: string,
    limit: number,
    scope: TechnicalScope,
    dateFrom?: string,
    dateTo?: string
  ) => Promise<TMemoryResult[]>;
  recallGraph: (
    query: string,
    limit: number,
    depth: number,
    scope: TechnicalScope,
    dateFrom?: string,
    dateTo?: string
  ) => Promise<TMemoryResult[]>;
};

export function createKnowledgeEngine<TMemoryResult extends { text: string; similarity: number; id?: string; category: string; via?: string; sourceType?: string }>(
  deps: KnowledgeEngineDeps<TMemoryResult>
) {
  type StoreRecallContext = {
    query: string;
    limit: number;
    opts: TotalRecallOptions;
  };
  type StoreDescriptor = {
    key: KnowledgeStore;
    recall: (ctx: StoreRecallContext) => Promise<TMemoryResult[]>;
  };

  function storeOption(opts: TotalRecallOptions, store: KnowledgeStore, key: string): unknown {
    return opts.storeOptions?.[store]?.[key];
  }

  async function routeKnowledgeStores(query: string, expandGraph: boolean): Promise<KnowledgeStore[]> {
    const allowed = getRoutableStoreKeys();
    const heuristic = (() => {
      const q = String(query || "").toLowerCase();
      const out = new Set<KnowledgeStore>();
      const technicalHint = /(api|schema|database|migration|deploy|docker|auth|test|bug|refactor|config|code|typescript|python|endpoint)/i;
      const relationshipHint = /(family|sister|brother|mother|father|wife|husband|partner|child|kids|relationship|related|who is)/i;
      const reflectiveHint = /(feel|feeling|reflect|journal|inner|tone|vibe|personality)/i;
      if (technicalHint.test(q)) {
        out.add("vector_technical");
        out.add("project");
      } else {
        out.add("vector_basic");
      }
      if (expandGraph && relationshipHint.test(q)) out.add("graph");
      if (reflectiveHint.test(q)) out.add("journal");
      if (!out.size) out.add("vector_basic");
      return Array.from(out);
    })();

    const systemPrompt = `You route a recall query to knowledge stores.
Choose the MINIMAL useful set.
Stores:
- vector_basic: personal/user facts
- vector_technical: technical/code/system facts
- graph: relationship traversal
- journal: reflective journal context
- project: project docs and architecture notes
Return JSON only: {"stores":["vector_basic","graph"]}`;
    const userPrompt = `Query: "${query}"\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}`;
    try {
      const text = await deps.callFastRouter(systemPrompt, userPrompt);
      let payload: any = null;
      try {
        payload = JSON.parse(String(text || "").trim());
      } catch {
        const m = String(text || "").match(/\{[\s\S]*\}/);
        if (m) {
          try { payload = JSON.parse(m[0]); } catch {}
        }
      }
      const stores = normalizeKnowledgeStores(payload?.stores, expandGraph).filter((s) => allowed.includes(s));
      if (stores.length) return stores;
    } catch {}
    return normalizeKnowledgeStores(heuristic, expandGraph);
  }

  async function routeRecallPlan(
    query: string,
    expandGraph: boolean,
    reasoning: "fast" | "deep" = "fast",
    intent: RecallIntent = "general",
  ): Promise<RoutedRecallPlan> {
    const allowed = getRoutableStoreKeys();
    const original = String(query || "").trim();
    const fallbackStores = await routeKnowledgeStores(original, expandGraph);
    const projectCatalog = (deps.getProjectCatalog ? deps.getProjectCatalog() : [])
      .slice(0, 40);
    const projectHints = projectCatalog.length
      ? projectCatalog.map((p) => `- ${p.name}: ${p.description}`).join("\n")
      : "- (none)";
    const allowedProjectNames = new Set(projectCatalog.map((p) => p.name));

    const systemPrompt = `You optimize a memory recall request.
Return JSON only with:
{
  "query": "cleaned query for retrieval",
  "stores": ["vector_basic","graph"],
  "project": "project_name_or_null"
}
Rules:
- Keep the same user intent; do NOT add new facts.
- Use minimal stores needed.
- Stores allowed: vector_basic, vector_technical, graph, journal, project.
- Set project only when the query clearly maps to one known project.
- intent facet:
  - general: broad/default
  - agent_actions: prioritize records of what assistant/agent suggested or did
  - relationship: prioritize people/relationship traversal
  - technical: prioritize technical/project-state retrieval
- Known projects:
${projectHints}
- If unsure, keep query close to original and prefer vector_basic.`;
    const userPrompt = `Query: "${original}"\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}\nintent: ${intent}`;
    try {
      const router = reasoning === "deep" && deps.callDeepRouter
        ? deps.callDeepRouter
        : deps.callFastRouter;
      const text = await router(systemPrompt, userPrompt);
      let payload: any = null;
      try {
        payload = JSON.parse(String(text || "").trim());
      } catch {
        const m = String(text || "").match(/\{[\s\S]*\}/);
        if (m) {
          try { payload = JSON.parse(m[0]); } catch {}
        }
      }
      const cleaned = String(payload?.query || "").trim() || original;
      const stores = normalizeKnowledgeStores(payload?.stores, expandGraph).filter((s) => allowed.includes(s));
      const routedProjectRaw = String(payload?.project || "").trim();
      const routedProject = routedProjectRaw && allowedProjectNames.has(routedProjectRaw)
        ? routedProjectRaw
        : undefined;
      return {
        query: cleaned,
        stores: stores.length ? stores : fallbackStores,
        project: routedProject,
      };
    } catch {
      return { query: original, stores: fallbackStores };
    }
  }

  function normalizeSourceType(value: unknown): SourceType | undefined {
    const raw = String(value || "").trim().toLowerCase();
    if (raw === "agent") return "assistant";
    if (raw === "user" || raw === "assistant" || raw === "both" || raw === "tool" || raw === "import") {
      return raw;
    }
    return undefined;
  }

  function defaultSourceTypeBoosts(intent: RecallIntent): Partial<Record<SourceType, number>> {
    if (intent === "agent_actions") {
      return { assistant: 1.25, both: 1.15, tool: 1.05, user: 0.92 };
    }
    if (intent === "technical") {
      return { tool: 1.1, assistant: 1.04 };
    }
    return {};
  }

  function applySourceTypeBoosts(items: TMemoryResult[], opts: TotalRecallOptions): TMemoryResult[] {
    const intent = opts.intent || "general";
    const boosts = {
      ...defaultSourceTypeBoosts(intent),
      ...(opts.ranking?.sourceTypeBoosts || {}),
    };
    if (Object.keys(boosts).length === 0) return items;

    return items.map((item) => {
      const fromItem = normalizeSourceType((item as any).sourceType || (item as any).source_type);
      if (!fromItem) return item;
      const boost = boosts[fromItem];
      if (!boost || boost === 1) return item;
      const nextSimilarity = Math.max(0, Math.min(0.999, (item.similarity || 0) * boost));
      return { ...item, similarity: nextSimilarity };
    });
  }

  function tokenizeQuery(query: string): string[] {
    const stop = new Set([
      "the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "were",
      "what", "when", "where", "which", "who", "how", "why", "about", "tell", "me", "your",
      "my", "our", "their", "his", "her", "its", "into", "onto", "than", "then",
    ]);
    const tokens = String(query || "")
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .map((t) => t.trim())
      .filter((t) => t.length >= 3 && !stop.has(t));
    return Array.from(new Set(tokens)).slice(0, 16);
  }

  async function recallFromJournalStore(query: string, limit: number): Promise<TMemoryResult[]> {
    if (!deps.isSystemEnabled("journal")) return [];
    const journalConfig = deps.getMemoryConfig().docs?.journal || {};
    const journalDir = deps.path.join(deps.workspace, journalConfig.journalDir || "journal");
    const tokens = tokenizeQuery(query);
    if (!tokens.length) return [];
    let files: string[] = [];
    try {
      files = deps.fs.readdirSync(journalDir).filter((f: string) => f.endsWith(".journal.md"));
    } catch {
      return [];
    }

    const scored: TMemoryResult[] = [];
    for (const file of files) {
      try {
        const fullPath = deps.path.join(journalDir, file);
        const content = deps.fs.readFileSync(fullPath, "utf8");
        const lc = content.toLowerCase();
        let hits = 0;
        for (const t of tokens) {
          if (lc.includes(t)) hits += 1;
        }
        if (hits === 0) continue;
        const excerpt = content.replace(/\s+/g, " ").trim().slice(0, 220);
        const similarity = Math.min(0.95, 0.45 + (hits / Math.max(tokens.length, 1)) * 0.5);
        scored.push({
          text: `${file}: ${excerpt}${content.length > 220 ? "..." : ""}`,
          category: "journal",
          similarity,
          via: "journal",
        } as TMemoryResult);
      } catch {}
    }

    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  }

  async function recallFromProjectStore(
    query: string,
    limit: number,
    project?: string,
    docs?: string[]
  ): Promise<TMemoryResult[]> {
    if (!deps.isSystemEnabled("projects")) return [];
    try {
      const args = [query, "--limit", String(limit)];
      if (project) args.push("--project", project);
      if (Array.isArray(docs) && docs.length > 0) {
        args.push("--docs", docs.join(","));
      }
      const out = await deps.callDocsRag("search", args);
      if (!out || !out.trim()) return [];
      const results: TMemoryResult[] = [];
      const lines = out.split("\n");
      for (const line of lines) {
        const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
        if (!m) continue;
        const file = m[1].split("/").pop() || m[1];
        const section = m[2].trim();
        const sim = Number.parseFloat(m[3]) || 0.6;
        results.push({
          text: `${file} > ${section}`,
          category: "project",
          similarity: sim,
          via: "project",
        } as TMemoryResult);
      }
      if (results.length === 0) {
        results.push({
          text: out.replace(/\s+/g, " ").slice(0, 280),
          category: "project",
          similarity: 0.55,
          via: "project",
        } as TMemoryResult);
      }
      return results.slice(0, limit);
    } catch {
      return [];
    }
  }

  async function totalRecall(query: string, limit: number, opts: TotalRecallOptions): Promise<TMemoryResult[]> {
    const stores = normalizeKnowledgeStores(opts.stores, opts.expandGraph);
    const all: TMemoryResult[] = [];
    const descriptors: Record<KnowledgeStore, StoreDescriptor> = {
      vector: {
        key: "vector",
        recall: async (ctx) => {
          const scopeRaw = storeOption(ctx.opts, "vector", "technicalScope");
          const scope = (scopeRaw === "personal" || scopeRaw === "technical" || scopeRaw === "any")
            ? scopeRaw
            : ctx.opts.technicalScope;
          return deps.recallVector(ctx.query, ctx.limit, scope, ctx.opts.dateFrom, ctx.opts.dateTo);
        },
      },
      vector_basic: {
        key: "vector_basic",
        recall: async (ctx) => deps.recallVector(ctx.query, ctx.limit, "personal", ctx.opts.dateFrom, ctx.opts.dateTo),
      },
      vector_technical: {
        key: "vector_technical",
        recall: async (ctx) => deps.recallVector(ctx.query, ctx.limit, "technical", ctx.opts.dateFrom, ctx.opts.dateTo),
      },
      graph: {
        key: "graph",
        recall: async (ctx) => {
          const depthRaw = Number(storeOption(ctx.opts, "graph", "depth"));
          const depth = Number.isFinite(depthRaw) && depthRaw > 0 ? Math.floor(depthRaw) : ctx.opts.graphDepth;
          const scopeRaw = storeOption(ctx.opts, "graph", "technicalScope");
          const scope = (scopeRaw === "personal" || scopeRaw === "technical" || scopeRaw === "any")
            ? scopeRaw
            : ctx.opts.technicalScope;
          return deps.recallGraph(ctx.query, ctx.limit, depth, scope, ctx.opts.dateFrom, ctx.opts.dateTo);
        },
      },
      journal: {
        key: "journal",
        recall: async (ctx) => recallFromJournalStore(ctx.query, ctx.limit),
      },
      project: {
        key: "project",
        recall: async (ctx) => {
          const projectRaw = storeOption(ctx.opts, "project", "project");
          const docsRaw = storeOption(ctx.opts, "project", "docs");
          const project = typeof projectRaw === "string" && projectRaw.trim()
            ? projectRaw.trim()
            : ctx.opts.project;
          const docs = Array.isArray(docsRaw)
            ? docsRaw.map((d) => String(d || "").trim()).filter(Boolean)
            : ctx.opts.docs;
          return recallFromProjectStore(ctx.query, ctx.limit, project, docs);
        },
      },
    };

    for (const store of stores) {
      const descriptor = descriptors[store];
      if (!descriptor) continue;
      all.push(...(await descriptor.recall({ query, limit, opts })));
    }

    const dedup = new Map<string, TMemoryResult>();
    for (const item of all) {
      const key = item.id
        ? `id:${item.id}`
        : `${String(item.via || "vector").toLowerCase()}::${item.text.toLowerCase().trim()}`;
      const prev = dedup.get(key);
      if (!prev || (item.similarity || 0) > (prev.similarity || 0)) {
        dedup.set(key, item);
      }
    }
    const merged = applySourceTypeBoosts(Array.from(dedup.values()), opts);
    merged.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return merged.slice(0, limit);
  }

  async function total_recall(query: string, limit: number, opts: TotalRecallOptions): Promise<TMemoryResult[]> {
    const plan = await routeRecallPlan(query, opts.expandGraph, opts.reasoning || "fast", opts.intent || "general");
    return totalRecall(plan.query, limit, {
      ...opts,
      stores: plan.stores,
      project: plan.project,
    });
  }

  return {
    normalizeKnowledgeStores,
    getKnowledgeStoreRegistry,
    renderKnowledgeStoreGuidanceForAgents,
    routeKnowledgeStores,
    routeRecallPlan,
    totalRecall,
    total_recall,
  };
}
