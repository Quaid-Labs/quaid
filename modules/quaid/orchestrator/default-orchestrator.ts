import {
  getKnowledgeDatastoreRegistry,
  getRoutableDatastoreKeys,
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents,
} from "../core/knowledge-stores.js";
import type {
  KnowledgeDatastore,
  DomainFilter,
  RecallIntent,
  SourceType,
} from "../core/knowledge-stores.js";

export type TotalRecallOptions = {
  datastores: KnowledgeDatastore[];
  expandGraph: boolean;
  graphDepth: number;
  domain: DomainFilter;
  intent?: RecallIntent;
  ranking?: {
    sourceTypeBoosts?: Partial<Record<SourceType, number>>;
  };
  dateFrom?: string;
  dateTo?: string;
  project?: string;
  docs?: string[];
  datastoreOptions?: Partial<Record<KnowledgeDatastore, Record<string, unknown>>>;
  reasoning?: "fast" | "deep";
  failOpen?: boolean;
};

export type RoutedRecallPlan = {
  query: string;
  datastores: KnowledgeDatastore[];
  project?: string;
};

type KnowledgeEngineDeps<TMemoryResult extends { text: string; similarity: number; id?: string; category: string; via?: string; sourceType?: string }> = {
  workspace: string;
  getMemoryConfig: () => any;
  isSystemEnabled: (system: "memory" | "journal" | "projects" | "workspace") => boolean;
  callFastRouter: (systemPrompt: string, userPrompt: string) => Promise<string>;
  callDeepRouter?: (systemPrompt: string, userPrompt: string) => Promise<string>;
  getProjectCatalog?: () => Array<{ name: string; description: string }>;
  recallVector: (
    query: string,
    limit: number,
    domain: DomainFilter,
    project?: string,
    dateFrom?: string,
    dateTo?: string
  ) => Promise<TMemoryResult[]>;
  recallGraph: (
    query: string,
    limit: number,
    depth: number,
    domain: DomainFilter,
    project?: string,
    dateFrom?: string,
    dateTo?: string
  ) => Promise<TMemoryResult[]>;
  recallJournalStore?: (query: string, limit: number) => Promise<TMemoryResult[]>;
  recallProjectStore?: (
    query: string,
    limit: number,
    project?: string,
    docs?: string[]
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
    key: KnowledgeDatastore;
    recall: (ctx: StoreRecallContext) => Promise<TMemoryResult[]>;
  };

  function storeOption(opts: TotalRecallOptions, store: KnowledgeDatastore, key: string): unknown {
    return opts.datastoreOptions?.[store]?.[key];
  }

  function sanitizeRecallResults(items: unknown[]): TMemoryResult[] {
    const out: TMemoryResult[] = [];
    for (const raw of items || []) {
      if (!raw || typeof raw !== "object") continue;
      const obj = raw as Record<string, unknown>;
      const text = String(obj.text || "").trim();
      if (!text) continue;
      const category = String(obj.category || "fact").trim() || "fact";
      const simRaw = Number(obj.similarity);
      const similarity = Number.isFinite(simRaw) ? Math.max(0, Math.min(1, simRaw)) : 0.5;
      const viaRaw = String(obj.via || "").trim().toLowerCase();
      const via = (viaRaw === "vector" || viaRaw === "graph" || viaRaw === "journal" || viaRaw === "project")
        ? viaRaw
        : "vector";
      const shaped: Record<string, unknown> = {
        text,
        category,
        similarity,
        via,
      };
      if (typeof obj.id === "string" && obj.id.trim()) shaped.id = obj.id.trim();
      if (typeof obj.sourceType === "string" && obj.sourceType.trim()) shaped.sourceType = obj.sourceType.trim();
      if (typeof obj.createdAt === "string" && obj.createdAt.trim()) shaped.createdAt = obj.createdAt.trim();
      if (typeof obj.validFrom === "string" && obj.validFrom.trim()) shaped.validFrom = obj.validFrom.trim();
      if (typeof obj.validUntil === "string" && obj.validUntil.trim()) shaped.validUntil = obj.validUntil.trim();
      if (typeof obj.ownerId === "string" && obj.ownerId.trim()) shaped.ownerId = obj.ownerId.trim();
      if (typeof obj.privacy === "string" && obj.privacy.trim()) shaped.privacy = obj.privacy.trim();
      if (typeof obj.extractionConfidence === "number" && Number.isFinite(obj.extractionConfidence)) {
        shaped.extractionConfidence = obj.extractionConfidence;
      }
      out.push({
        ...(shaped as TMemoryResult),
      });
    }
    return out;
  }

  function parseRoutedDatastores(raw: unknown, allowed: KnowledgeDatastore[]): KnowledgeDatastore[] {
    if (!Array.isArray(raw) || raw.length === 0) {
      throw new Error("router response missing datastores array");
    }
    const out: KnowledgeDatastore[] = [];
    for (const entry of raw) {
      const key = String(entry || "").trim().toLowerCase() as KnowledgeDatastore;
      if (!allowed.includes(key)) continue;
      if (!out.includes(key)) out.push(key);
    }
    if (!out.length) {
      throw new Error("router returned no valid datastores");
    }
    return out;
  }

  function isFailHardEnabled(): boolean {
    try {
      const cfg = deps.getMemoryConfig?.() || {};
      const retrieval = cfg?.retrieval || {};
      if (typeof retrieval.fail_hard === "boolean") return retrieval.fail_hard;
      if (typeof retrieval.failHard === "boolean") return retrieval.failHard;
    } catch (err: unknown) {
      console.warn(`[quaid][recall-router] failed to read failHard config; defaulting to true: ${String((err as Error)?.message || err)}`);
    }
    return true;
  }

  async function routeWithRepair<T>(
    router: (systemPrompt: string, userPrompt: string) => Promise<string>,
    systemPrompt: string,
    userPrompt: string,
    validate: (raw: string) => T,
    label: string,
  ): Promise<T> {
    const first = await router(systemPrompt, userPrompt);
    try {
      return validate(first);
    } catch (err) {
      const reason = (err as Error)?.message || String(err);
      console.warn(`[quaid][recall-router] ${label}: invalid first response; retrying repair (${reason})`);
      const repairSystemPrompt = `${systemPrompt}

Return STRICT JSON only. No prose, no markdown, no comments, no code fences.`;
      const repairUserPrompt = `${userPrompt}

Previous response (invalid):
${first}

Validation error:
${reason}

Rewrite into valid JSON matching the required schema exactly.`;
      const second = await router(repairSystemPrompt, repairUserPrompt);
      try {
        return validate(second);
      } catch (err2) {
        const reason2 = (err2 as Error)?.message || String(err2);
        throw new Error(
          `Fast recall prepass failed to produce valid structured output after retry (${label}). ` +
          `Use a stronger fast model (e.g. Haiku/GPT-class) or set retrieval.routerFailOpen=true. ` +
          `First validation error: ${reason}. ` +
          `Retry validation error: ${reason2}`,
          { cause: err2 instanceof Error ? err2 : new Error(String(err2)) },
        );
      }
    }
  }

  async function routeKnowledgeDatastores(query: string, expandGraph: boolean): Promise<KnowledgeDatastore[]> {
    const allowed = getRoutableDatastoreKeys();

    const systemPrompt = `You route a recall query to knowledge datastores.
Choose the MINIMAL useful set.
Stores:
- vector_basic: personal/user facts (cheapest; prefer this first)
- vector_technical: technical/code/system facts
- graph: relationship traversal
- journal: reflective journal context (more expensive/noisier than memory)
- project: project docs and architecture notes (expensive; use when question needs file-backed project detail)
Cost/latency priority:
1) vector_basic (very cheap, use liberally)
2) vector_technical/graph
3) project/journal (use when needed for precision)
4) broader historical/session retrieval only when prior stores are insufficient
Return JSON only: {"datastores":["vector_basic","graph"]}`;
    const userPrompt = `Query: "${query}"\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}`;
    return routeWithRepair(
      deps.callFastRouter,
      systemPrompt,
      userPrompt,
      (text) => {
        let payload: any = null;
        try {
          payload = JSON.parse(String(text || "").trim());
        } catch {
          const m = String(text || "").match(/\{[\s\S]*\}/);
          if (m) {
            payload = JSON.parse(m[0]);
          } else {
            throw new Error("router response is not JSON");
          }
        }
        if (!payload || typeof payload !== "object") {
          throw new Error("router response is not an object");
        }
        const datastores = parseRoutedDatastores(payload?.datastores, allowed);
        return datastores;
      },
      "routeKnowledgeDatastores",
    );
  }

  async function routeRecallPlan(
    query: string,
    expandGraph: boolean,
    reasoning: "fast" | "deep" = "fast",
    intent: RecallIntent = "general",
  ): Promise<RoutedRecallPlan> {
    const allowed = getRoutableDatastoreKeys();
    const original = String(query || "").trim();
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
  "datastores": ["vector_basic","graph"],
  "project": "project_name_or_null"
}
Rules:
- Keep the same user intent; do NOT add new facts.
- Use minimal datastores needed, but be permissive with vector_basic.
- Stores allowed: vector_basic, vector_technical, graph, journal, project.
- Cost/latency priority:
  1) vector_basic first (cheap)
  2) vector_technical/graph
  3) project/journal when needed for precision
  4) broader historical/session retrieval only if prior stores are insufficient
- Set project when query clearly maps to one known project.
- If project detail is asked but project is uncertain, still include datastore "project" and leave project=null.
- intent facet:
  - general: broad/default
  - agent_actions: prioritize records of what assistant/agent suggested or did
  - relationship: prioritize people/relationship traversal
  - technical: prioritize technical/project-state retrieval
- Known projects:
${projectHints}
- If unsure, keep query close to original and prefer vector_basic.`;
    const userPrompt = `Query: "${original}"\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}\nintent: ${intent}`;
    const router = reasoning === "deep" && deps.callDeepRouter
      ? deps.callDeepRouter
      : deps.callFastRouter;

    return routeWithRepair(
      router,
      systemPrompt,
      userPrompt,
      (text) => {
        let payload: any = null;
        try {
          payload = JSON.parse(String(text || "").trim());
        } catch {
          const m = String(text || "").match(/\{[\s\S]*\}/);
          if (m) {
            payload = JSON.parse(m[0]);
          } else {
            throw new Error("router response is not JSON");
          }
        }
        if (!payload || typeof payload !== "object") {
          throw new Error("router response is not an object");
        }
        const cleaned = String(payload?.query || "").trim();
        if (!cleaned) {
          throw new Error("router response missing query");
        }
        const datastores = parseRoutedDatastores(payload?.datastores, allowed);
        const routedProjectRaw = String(payload?.project || "").trim();
        const routedProject = routedProjectRaw && allowedProjectNames.has(routedProjectRaw)
          ? routedProjectRaw
          : undefined;
        return {
          query: cleaned,
          datastores,
          project: routedProject,
        };
      },
      "routeRecallPlan",
    );
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

  async function recallFromJournalStore(query: string, limit: number): Promise<TMemoryResult[]> {
    if (!deps.isSystemEnabled("journal")) return [];
    if (!deps.recallJournalStore) return [];
    return deps.recallJournalStore(query, limit);
  }

  async function recallFromProjectStore(
    query: string,
    limit: number,
    project?: string,
    docs?: string[]
  ): Promise<TMemoryResult[]> {
    if (!deps.isSystemEnabled("projects")) return [];
    if (!deps.recallProjectStore) return [];
    return deps.recallProjectStore(query, limit, project, docs);
  }

  async function totalRecall(query: string, limit: number, opts: TotalRecallOptions): Promise<TMemoryResult[]> {
    const datastores = normalizeKnowledgeDatastores(opts.datastores, opts.expandGraph);
    const all: TMemoryResult[] = [];
    const descriptors: Record<KnowledgeDatastore, StoreDescriptor> = {
      vector: {
        key: "vector",
        recall: async (ctx) => {
          const domainRaw = storeOption(ctx.opts, "vector", "domain");
          const domain = (domainRaw && typeof domainRaw === "object" && !Array.isArray(domainRaw))
            ? (domainRaw as DomainFilter)
            : (ctx.opts.domain || { all: true });
          const projectRaw = storeOption(ctx.opts, "vector", "project");
          const project = typeof projectRaw === "string" && projectRaw.trim()
            ? projectRaw.trim()
            : ctx.opts.project;
          return deps.recallVector(ctx.query, ctx.limit, domain, project, ctx.opts.dateFrom, ctx.opts.dateTo);
        },
      },
      vector_basic: {
        key: "vector_basic",
        recall: async (ctx) => deps.recallVector(ctx.query, ctx.limit, { personal: true }, ctx.opts.project, ctx.opts.dateFrom, ctx.opts.dateTo),
      },
      vector_technical: {
        key: "vector_technical",
        recall: async (ctx) => deps.recallVector(ctx.query, ctx.limit, { technical: true }, ctx.opts.project, ctx.opts.dateFrom, ctx.opts.dateTo),
      },
      graph: {
        key: "graph",
        recall: async (ctx) => {
          const depthRaw = Number(storeOption(ctx.opts, "graph", "depth"));
          const depth = Number.isFinite(depthRaw) && depthRaw > 0 ? Math.floor(depthRaw) : ctx.opts.graphDepth;
          const domainRaw = storeOption(ctx.opts, "graph", "domain");
          const domain = (domainRaw && typeof domainRaw === "object" && !Array.isArray(domainRaw))
            ? (domainRaw as DomainFilter)
            : (ctx.opts.domain || { all: true });
          const projectRaw = storeOption(ctx.opts, "graph", "project");
          const project = typeof projectRaw === "string" && projectRaw.trim()
            ? projectRaw.trim()
            : ctx.opts.project;
          return deps.recallGraph(ctx.query, ctx.limit, depth, domain, project, ctx.opts.dateFrom, ctx.opts.dateTo);
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

    for (const store of datastores) {
      const descriptor = descriptors[store];
      if (!descriptor) continue;
      try {
        all.push(...(await descriptor.recall({ query, limit, opts })));
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        console.warn(`[quaid][recall] datastore=${store} failed: ${msg}`);
        if (isFailHardEnabled()) {
          throw err;
        }
      }
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
    try {
      const plan = await routeRecallPlan(query, opts.expandGraph, opts.reasoning || "fast", opts.intent || "general");
      const routed = await totalRecall(plan.query, limit, {
        ...opts,
        datastores: plan.datastores,
        project: plan.project,
      });
      return sanitizeRecallResults(routed).slice(0, limit);
    } catch (err) {
      const failHard = isFailHardEnabled();
      if (opts.failOpen && !failHard) {
        const reason = (err as Error)?.message || String(err);
        const fallbackDatastores = normalizeKnowledgeDatastores(undefined, opts.expandGraph);
        console.error(
          `[quaid][recall-router][FAIL-OPEN] Router prepass failed; using deterministic default recall plan. ` +
          `reason="${reason}" datastores=${fallbackDatastores.join(",")}`
        );
        const fallbackResults = await totalRecall(query, limit, {
          ...opts,
          datastores: fallbackDatastores,
        });
        const warning: TMemoryResult = {
          text:
            `[RECALL ROUTER WARNING] Fast prepass failed and fallback recall plan was used. ` +
            `Reason: ${reason}. Consider upgrading the fast model if this repeats.`,
          category: "system_notice",
          similarity: 1.0,
          via: "vector",
        } as TMemoryResult;
        return sanitizeRecallResults([warning, ...fallbackResults]).slice(0, limit);
      }
      if (opts.failOpen && failHard) {
        const reason = (err as Error)?.message || String(err);
        console.error(
          `[quaid][recall-router][FAIL-HARD] Router prepass failed and fallback was blocked by failHard=true. ` +
          `reason="${reason}"`
        );
      }
      throw err;
    }
  }

  return {
    normalizeKnowledgeDatastores,
    getKnowledgeDatastoreRegistry,
    renderKnowledgeDatastoreGuidanceForAgents,
    routeKnowledgeDatastores,
    routeRecallPlan,
    totalRecall,
    total_recall,
  };
}
