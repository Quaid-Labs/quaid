import {
  datastoreUsesCandidatePool,
  getDefaultDomainForKnowledgeDatastore,
  getKnowledgeDatastoreRegistry,
  getRoutableDatastoreKeys,
  isVectorKnowledgeDatastore,
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents,
  renderRoutableKnowledgeDatastoreRouterGuidance
} from "./knowledge-stores.js";
function createKnowledgeEngine(deps) {
  const _routerWarningLastByTier = /* @__PURE__ */ new Map();
  function storeOption(opts, store, key) {
    return opts.datastoreOptions?.[store]?.[key];
  }
  function sanitizeRecallResults(items) {
    const out = [];
    for (const raw of items || []) {
      if (!raw || typeof raw !== "object") continue;
      const obj = raw;
      const text = String(obj.text || "").trim();
      if (!text) continue;
      const category = String(obj.category || "fact").trim() || "fact";
      const simRaw = Number(obj.similarity);
      const similarity = Number.isFinite(simRaw) ? Math.max(0, Math.min(1, simRaw)) : 0.5;
      const viaRaw = String(obj.via || "").trim().toLowerCase();
      const via = viaRaw === "vector" || viaRaw === "graph" || viaRaw === "journal" || viaRaw === "project" || viaRaw === "session_chunks" ? viaRaw : "vector";
      const shaped = {
        text,
        category,
        similarity,
        via
      };
      if (typeof obj.id === "string" && obj.id.trim()) shaped.id = obj.id.trim();
      if (typeof obj.sourceType === "string" && obj.sourceType.trim()) shaped.sourceType = obj.sourceType.trim();
      const sourceChunkId = String(obj.sourceChunkId || obj.source_chunk_id || "").trim();
      const chunkId = String(obj.chunkId || obj.chunk_id || "").trim();
      const contentHash = String(obj.contentHash || obj.content_hash || "").trim();
      if (sourceChunkId) shaped.sourceChunkId = sourceChunkId;
      if (chunkId) shaped.chunkId = chunkId;
      if (contentHash) shaped.contentHash = contentHash;
      if (typeof obj.chunkIndex === "number" && Number.isFinite(obj.chunkIndex)) shaped.chunkIndex = obj.chunkIndex;
      if (typeof obj.chunk_index === "number" && Number.isFinite(obj.chunk_index)) shaped.chunkIndex = obj.chunk_index;
      if (typeof obj.tokenCount === "number" && Number.isFinite(obj.tokenCount)) shaped.tokenCount = obj.tokenCount;
      if (typeof obj.token_count === "number" && Number.isFinite(obj.token_count)) shaped.tokenCount = obj.token_count;
      if (typeof obj.outputTokenCount === "number" && Number.isFinite(obj.outputTokenCount)) shaped.outputTokenCount = obj.outputTokenCount;
      if (typeof obj.output_token_count === "number" && Number.isFinite(obj.output_token_count)) shaped.outputTokenCount = obj.output_token_count;
      if (typeof obj.truncated === "boolean") shaped.truncated = obj.truncated;
      if (typeof obj.createdAt === "string" && obj.createdAt.trim()) shaped.createdAt = obj.createdAt.trim();
      if (typeof obj.validFrom === "string" && obj.validFrom.trim()) shaped.validFrom = obj.validFrom.trim();
      if (typeof obj.validUntil === "string" && obj.validUntil.trim()) shaped.validUntil = obj.validUntil.trim();
      if (typeof obj.ownerId === "string" && obj.ownerId.trim()) shaped.ownerId = obj.ownerId.trim();
      if (typeof obj.privacy === "string" && obj.privacy.trim()) shaped.privacy = obj.privacy.trim();
      if (typeof obj.extractionConfidence === "number" && Number.isFinite(obj.extractionConfidence)) {
        shaped.extractionConfidence = obj.extractionConfidence;
      }
      out.push({
        ...shaped
      });
    }
    return out;
  }
  function parseRoutedDatastores(raw, allowed) {
    if (!Array.isArray(raw) || raw.length === 0) {
      throw new Error("router response missing datastores array");
    }
    const out = [];
    for (const entry of raw) {
      const key = String(entry || "").trim().toLowerCase();
      if (!allowed.includes(key)) continue;
      if (!out.includes(key)) out.push(key);
    }
    if (!out.length) {
      throw new Error("router returned no valid datastores");
    }
    return out;
  }
  function isFailHardEnabled() {
    try {
      const cfg = deps.getMemoryConfig?.() || {};
      const retrieval = cfg?.retrieval || {};
      if (typeof retrieval.fail_hard === "boolean") return retrieval.fail_hard;
      if (typeof retrieval.failHard === "boolean") return retrieval.failHard;
    } catch (err) {
      console.warn(`[memory][recall-router] failed to read failHard config; defaulting to true: ${String(err?.message || err)}`);
    }
    return true;
  }
  function routerWarningCooldownMs() {
    try {
      const cfg = deps.getMemoryConfig?.() || {};
      const retrieval = cfg?.retrieval || {};
      const minutesRaw = retrieval.routerWarningCooldownMinutes ?? retrieval.router_warning_cooldown_minutes ?? 15;
      const minutes = Number(minutesRaw);
      if (!Number.isFinite(minutes) || minutes <= 0) return 15 * 60 * 1e3;
      return Math.max(1, Math.round(minutes)) * 60 * 1e3;
    } catch {
      return 15 * 60 * 1e3;
    }
  }
  function shouldEmitRouterWarning(tier) {
    const key = String(tier || "fast").trim().toLowerCase() || "fast";
    const now = Date.now();
    const cooldownMs = routerWarningCooldownMs();
    const lastAt = _routerWarningLastByTier.get(key) || 0;
    if (now - lastAt < cooldownMs) return false;
    _routerWarningLastByTier.set(key, now);
    return true;
  }
  async function routeWithRepair(router, systemPrompt, userPrompt, validate, label) {
    const first = await router(systemPrompt, userPrompt);
    try {
      return validate(first);
    } catch (err) {
      const reason = err?.message || String(err);
      console.warn(`[memory][recall-router] ${label}: invalid first response; retrying repair (${reason})`);
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
        const reason2 = err2?.message || String(err2);
        throw new Error(
          `Fast recall prepass failed to produce valid structured output after retry (${label}). Use a stronger fast model (e.g. Haiku/GPT-class) or set retrieval.routerFailOpen=true. First validation error: ${reason}. Retry validation error: ${reason2}`,
          { cause: err2 instanceof Error ? err2 : new Error(String(err2)) }
        );
      }
    }
  }
  async function routeKnowledgeDatastores(query, expandGraph) {
    const allowed = getRoutableDatastoreKeys();
    const routerGuidance = renderRoutableKnowledgeDatastoreRouterGuidance();
    const systemPrompt = `You route a recall query to knowledge datastores.
Choose the MINIMAL useful set.
${routerGuidance}
Return JSON only: {"datastores":["vector_basic","graph"]}`;
    const userPrompt = `Query: "${query}"
expandGraphAllowed: ${expandGraph ? "true" : "false"}`;
    return routeWithRepair(
      deps.callFastRouter,
      systemPrompt,
      userPrompt,
      (text) => {
        let payload = null;
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
      "routeKnowledgeDatastores"
    );
  }
  function normalizeProjectHintText(value) {
    return String(value || "").toLowerCase().replace(/[-_]+/g, " ").replace(/[^a-z0-9\s]/g, " ").replace(/\s+/g, " ").trim();
  }
  function inferProjectFromQuery(query, projectCatalog) {
    const normalizedQuery = normalizeProjectHintText(query);
    if (!normalizedQuery) return void 0;
    let bestMatch = null;
    for (const entry of projectCatalog) {
      const normalizedName = normalizeProjectHintText(entry.name);
      if (!normalizedName) continue;
      if (!normalizedQuery.includes(normalizedName)) continue;
      const score = normalizedName.length;
      if (!bestMatch || score > bestMatch.score) {
        bestMatch = { name: entry.name, score };
      }
    }
    return bestMatch?.name;
  }
  function shouldAugmentProjectStore(query) {
    const normalized = normalizeProjectHintText(query);
    if (!normalized) return false;
    const detailCues = [
      "version",
      "architecture",
      "api",
      "schema",
      "deploy",
      "deployment",
      "feature",
      "features",
      "bug",
      "bugs",
      "test",
      "tests",
      "readme",
      "docs",
      "documentation",
      "project log",
      "project logs",
      "portfolio",
      "site",
      "projects",
      "file",
      "files",
      "ui",
      "frontend",
      "backend",
      "graphql",
      "rest",
      "database",
      "model",
      "migration",
      "component",
      "implementation",
      "implemented"
    ];
    const temporalCues = ["as of", "latest", "current", "after", "before", "since", "until"];
    return detailCues.some((cue) => normalized.includes(cue)) || temporalCues.some((cue) => normalized.includes(cue));
  }
  async function routeRecallPlan(query, expandGraph, reasoning = "fast", intent = "general") {
    const allowed = getRoutableDatastoreKeys();
    const routerGuidance = renderRoutableKnowledgeDatastoreRouterGuidance();
    const allowedStoreList = allowed.join(", ");
    const original = String(query || "").trim();
    const projectCatalog = (deps.getProjectCatalog ? deps.getProjectCatalog() : []).slice(0, 40);
    const projectHints = projectCatalog.length ? projectCatalog.map((p) => `- ${p.name}: ${p.description}`).join("\n") : "- (none)";
    const allowedProjectNames = new Set(projectCatalog.map((p) => p.name));
    const systemPrompt = `You optimize a memory recall request.
Return JSON only with:
{
  "query": "cleaned query for retrieval",
  "datastores": ["vector_basic","graph"],
  "project": "project_name_or_null",
  "domainBoost": {"work": 1.3}
}
Rules:
- Keep the same user intent; do NOT add new facts.
- Use minimal datastores needed, but be permissive with vector_basic.
- Stores allowed: ${allowedStoreList}.
${routerGuidance}
- Set project when query clearly maps to one known project.
- If project detail is asked but project is uncertain, still include datastore "project" and leave project=null.
- Prefer domainBoost for known-scope recall instead of strict filtering.
  - Example single-domain: {"personal": 1.3}
  - Example multi-domain: {"work": 1.3, "technical": 1.3}
  - If scope is unclear, return {}.
- Temporal anchoring:
  - Preserve explicit dates/times in the cleaned query.
  - If the user asks "current/latest/now/as of", include those terms in the cleaned query.
  - Never invent dates.
- Language fidelity:
  - Preserve the user's language/script in the cleaned query.
  - Do not translate user content into another language unless explicitly requested.
  - If lexical variants are useful, keep them in the same language/script as the user query.
- intent facet:
  - general: broad/default
  - agent_actions: prioritize records of what assistant/agent suggested or did
  - relationship: prioritize people/relationship traversal
  - technical: prioritize technical/project-state retrieval
- Known projects:
${projectHints}
- If unsure, keep query close to original and prefer vector_basic.`;
    const userPrompt = `Query: "${original}"
expandGraphAllowed: ${expandGraph ? "true" : "false"}
intent: ${intent}`;
    const router = reasoning === "deep" && deps.callDeepRouter ? deps.callDeepRouter : deps.callFastRouter;
    return routeWithRepair(
      router,
      systemPrompt,
      userPrompt,
      (text) => {
        let payload = null;
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
        const routedProject = routedProjectRaw && allowedProjectNames.has(routedProjectRaw) ? routedProjectRaw : void 0;
        const inferredProject = inferProjectFromQuery(original, projectCatalog);
        const routedDomainBoostRaw = payload?.domainBoost;
        const routedDomainBoost = {};
        if (routedDomainBoostRaw && typeof routedDomainBoostRaw === "object" && !Array.isArray(routedDomainBoostRaw)) {
          for (const [k, v] of Object.entries(routedDomainBoostRaw)) {
            const key = String(k || "").trim().toLowerCase();
            const factor = Number(v);
            if (!key || !Number.isFinite(factor)) continue;
            routedDomainBoost[key] = Math.max(1, Math.min(2, factor));
          }
        }
        const plan = {
          query: cleaned,
          datastores,
          project: routedProject || inferredProject
        };
        if (inferredProject && shouldAugmentProjectStore(original)) {
          plan.datastores = ["project", ...plan.datastores.filter((store) => store !== "project")];
        }
        if (Object.keys(routedDomainBoost).length) {
          plan.domainBoost = routedDomainBoost;
        }
        return plan;
      },
      "routeRecallPlan"
    );
  }
  function normalizeSourceType(value) {
    const raw = String(value || "").trim().toLowerCase();
    if (raw === "agent") return "assistant";
    if (raw === "user" || raw === "assistant" || raw === "both" || raw === "tool" || raw === "import") {
      return raw;
    }
    return void 0;
  }
  function defaultSourceTypeBoosts(intent) {
    if (intent === "agent_actions") {
      return { assistant: 1.25, both: 1.15, tool: 1.05, user: 0.92 };
    }
    if (intent === "technical") {
      return { tool: 1.1, assistant: 1.04 };
    }
    return {};
  }
  function applySourceTypeBoosts(items, opts) {
    const intent = opts.intent || "general";
    const boosts = {
      ...defaultSourceTypeBoosts(intent),
      ...opts.ranking?.sourceTypeBoosts || {}
    };
    if (Object.keys(boosts).length === 0) return items;
    return items.map((item) => {
      const fromItem = normalizeSourceType(item.sourceType || item.source_type);
      if (!fromItem) return item;
      const boost = boosts[fromItem];
      if (!boost || boost === 1) return item;
      const nextSimilarity = Math.max(0, Math.min(0.999, (item.similarity || 0) * boost));
      return { ...item, similarity: nextSimilarity };
    });
  }
  function recallItemIdentity(item) {
    const id = typeof item.id === "string" ? item.id.trim() : "";
    if (id) return `id:${id}`;
    return `${String(item.via || "vector").toLowerCase()}::${String(item.text || "").trim().toLowerCase()}`;
  }
  function isProjectRecallItem(item) {
    const via = String(item?.via || "").trim().toLowerCase();
    const category = String(item?.category || "").trim().toLowerCase();
    const sourceType = String(item?.sourceType || item?.source_type || "").trim().toLowerCase();
    return via === "project" || category === "project" || sourceType === "docs";
  }
  function preserveRequestedProjectRows(rows, projectRows, limit) {
    if (!Array.isArray(rows) || limit <= 0 || !Array.isArray(projectRows) || projectRows.length === 0) {
      return rows.slice(0, Math.max(1, limit));
    }
    const targetProjectRows = Math.min(
      projectRows.length,
      Math.max(1, Math.min(Math.ceil(limit / 2), limit))
    );
    const out = rows.slice(0, Math.max(1, limit));
    let present = out.filter(isProjectRecallItem).length;
    if (present >= targetProjectRows) return out;
    const seen = new Set(out.map((item) => recallItemIdentity(item)));
    const candidates = [...projectRows].filter((item) => !seen.has(recallItemIdentity(item))).sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    for (const candidate of candidates) {
      if (present >= targetProjectRows) break;
      const dropIndex = out.length >= limit ? (() => {
        for (let i = out.length - 1; i >= 0; i -= 1) {
          if (!isProjectRecallItem(out[i])) return i;
        }
        return -1;
      })() : -1;
      if (out.length >= limit && dropIndex < 0) break;
      if (dropIndex >= 0) out.splice(dropIndex, 1);
      out.push(candidate);
      present += 1;
    }
    out.sort((a, b) => {
      const simDelta = Number(b.similarity || 0) - Number(a.similarity || 0);
      if (Math.abs(simDelta) > 1e-9) return simDelta;
      return Number(isProjectRecallItem(b)) - Number(isProjectRecallItem(a));
    });
    return out.slice(0, Math.max(1, limit));
  }
  async function recallFromJournalStore(query, limit) {
    if (!deps.isSystemEnabled("journal")) return [];
    if (!deps.recallJournalStore) return [];
    return deps.recallJournalStore(query, limit);
  }
  async function recallFromProjectStore(query, limit, project, docs, dateFrom, dateTo) {
    if (!deps.isSystemEnabled("projects")) return [];
    if (!deps.requestProjectStoreRecall) {
      throw new Error("project recall broker request handler is not configured");
    }
    return deps.requestProjectStoreRecall({
      query,
      limit,
      selector: "project",
      store: "docs",
      project,
      docs,
      dateFrom,
      dateTo
    });
  }
  async function _executeStores(query, limit, opts) {
    const datastores = normalizeKnowledgeDatastores(opts.datastores, opts.expandGraph);
    const all = [];
    const projectRows = [];
    const descriptors = {
      vector: {
        key: "vector",
        recall: async (ctx) => {
          const domainRaw = storeOption(ctx.opts, "vector", "domain");
          const domain = domainRaw && typeof domainRaw === "object" && !Array.isArray(domainRaw) ? domainRaw : ctx.opts.domain || { all: true };
          const projectRaw = storeOption(ctx.opts, "vector", "project");
          const project = typeof projectRaw === "string" && projectRaw.trim() ? projectRaw.trim() : ctx.opts.project;
          return deps.recallMemory(ctx.query, ctx.limit, { stores: ["vector"], domain, domainBoost: ctx.opts.domainBoost, project, dateFrom: ctx.opts.dateFrom, dateTo: ctx.opts.dateTo, fast: ctx.opts.fast });
        }
      },
      vector_basic: {
        key: "vector_basic",
        recall: async (ctx) => deps.recallMemory(ctx.query, ctx.limit, { stores: ["vector_basic"], domain: ctx.opts.domain || getDefaultDomainForKnowledgeDatastore("vector_basic"), domainBoost: ctx.opts.domainBoost, project: ctx.opts.project, dateFrom: ctx.opts.dateFrom, dateTo: ctx.opts.dateTo, fast: ctx.opts.fast })
      },
      vector_technical: {
        key: "vector_technical",
        recall: async (ctx) => deps.recallMemory(ctx.query, ctx.limit, { stores: ["vector_technical"], domain: ctx.opts.domain || getDefaultDomainForKnowledgeDatastore("vector_technical"), domainBoost: ctx.opts.domainBoost, project: ctx.opts.project, dateFrom: ctx.opts.dateFrom, dateTo: ctx.opts.dateTo, fast: ctx.opts.fast })
      },
      graph: {
        key: "graph",
        recall: async (ctx) => {
          const depthRaw = Number(storeOption(ctx.opts, "graph", "depth"));
          const depth = Number.isFinite(depthRaw) && depthRaw > 0 ? Math.floor(depthRaw) : ctx.opts.graphDepth;
          const domainRaw = storeOption(ctx.opts, "graph", "domain");
          const domain = domainRaw && typeof domainRaw === "object" && !Array.isArray(domainRaw) ? domainRaw : ctx.opts.domain || { all: true };
          const projectRaw = storeOption(ctx.opts, "graph", "project");
          const project = typeof projectRaw === "string" && projectRaw.trim() ? projectRaw.trim() : ctx.opts.project;
          return deps.recallMemory(ctx.query, ctx.limit, { stores: ["graph"], domain, domainBoost: ctx.opts.domainBoost, depth, project, dateFrom: ctx.opts.dateFrom, dateTo: ctx.opts.dateTo, fast: ctx.opts.fast, candidatePool: ctx.candidatePool });
        }
      },
      journal: {
        key: "journal",
        recall: async (ctx) => recallFromJournalStore(ctx.query, ctx.limit)
      },
      project: {
        key: "project",
        recall: async (ctx) => {
          const projectRaw = storeOption(ctx.opts, "project", "project");
          const docsRaw = storeOption(ctx.opts, "project", "docs");
          const project = typeof projectRaw === "string" && projectRaw.trim() ? projectRaw.trim() : ctx.opts.project;
          const docs = Array.isArray(docsRaw) ? docsRaw.map((d) => String(d || "").trim()).filter(Boolean) : ctx.opts.docs;
          return recallFromProjectStore(ctx.query, ctx.limit, project, docs, ctx.opts.dateFrom, ctx.opts.dateTo);
        }
      },
      session_chunks: {
        key: "session_chunks",
        recall: async (ctx) => {
          const maxChunkTokensRaw = Number(storeOption(ctx.opts, "session_chunks", "max_chunk_tokens"));
          const maxTotalChunkTokensRaw = Number(storeOption(ctx.opts, "session_chunks", "max_total_chunk_tokens"));
          const maxChunkTokens = Number.isFinite(maxChunkTokensRaw) ? maxChunkTokensRaw : ctx.opts.maxChunkTokens;
          const maxTotalChunkTokens = Number.isFinite(maxTotalChunkTokensRaw) ? maxTotalChunkTokensRaw : ctx.opts.maxTotalChunkTokens;
          return deps.recallMemory(ctx.query, ctx.limit, {
            stores: ["session_chunks"],
            domain: ctx.opts.domain || { all: true },
            domainBoost: ctx.opts.domainBoost,
            project: ctx.opts.project,
            dateFrom: ctx.opts.dateFrom,
            dateTo: ctx.opts.dateTo,
            fast: ctx.opts.fast,
            maxChunkTokens,
            maxTotalChunkTokens
          });
        }
      }
    };
    const vectorAccumulated = [];
    for (const store of datastores) {
      const descriptor = descriptors[store];
      if (!descriptor) continue;
      try {
        const candidatePool = datastoreUsesCandidatePool(store) && vectorAccumulated.length > 0 ? [...vectorAccumulated] : void 0;
        const storeResults = await descriptor.recall({ query, limit, opts, candidatePool });
        all.push(...storeResults);
        if (isVectorKnowledgeDatastore(store)) vectorAccumulated.push(...storeResults);
        if (store === "project" && Array.isArray(storeResults) && storeResults.length > 0) {
          projectRows.push(...storeResults);
        }
      } catch (err) {
        const msg = String(err?.message || err);
        console.warn(`[memory][recall] datastore=${store} failed: ${msg}`);
        if (isFailHardEnabled()) {
          throw err;
        }
      }
    }
    const dedup = /* @__PURE__ */ new Map();
    for (const item of all) {
      const key = item.id ? `id:${item.id}` : `${String(item.via || "vector").toLowerCase()}::${item.text.toLowerCase().trim()}`;
      const prev = dedup.get(key);
      if (!prev || (item.similarity || 0) > (prev.similarity || 0)) {
        dedup.set(key, item);
      }
    }
    const merged = applySourceTypeBoosts(Array.from(dedup.values()), opts);
    merged.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    const sliced = merged.slice(0, limit);
    if (datastores.includes("project") && projectRows.length > 0) {
      return preserveRequestedProjectRows(sliced, projectRows, limit);
    }
    return sliced;
  }
  async function recall(query, limit, opts) {
    const hasExplicitStores = Array.isArray(opts.datastores) && opts.datastores.length > 0;
    if (opts.fast || hasExplicitStores) {
      return sanitizeRecallResults(await _executeStores(query, limit, opts)).slice(0, limit);
    }
    try {
      const plan = await routeRecallPlan(query, opts.expandGraph, opts.reasoning || "fast", opts.intent || "general");
      const routed = await _executeStores(plan.query, limit, {
        ...opts,
        datastores: plan.datastores,
        project: plan.project,
        domainBoost: opts.domainBoost ?? plan.domainBoost
      });
      return sanitizeRecallResults(routed).slice(0, limit);
    } catch (err) {
      const failHard = isFailHardEnabled();
      if (opts.failOpen && !failHard) {
        const reason = err?.message || String(err);
        const fallbackDatastores = normalizeKnowledgeDatastores(void 0, opts.expandGraph);
        const tier = String(opts.reasoning || "fast");
        console.error(
          `[memory][recall-router][FAIL-OPEN] Router prepass failed; using deterministic default recall plan. reason="${reason}" datastores=${fallbackDatastores.join(",")}`
        );
        const fallbackResults = await _executeStores(query, limit, {
          ...opts,
          datastores: fallbackDatastores
        });
        if (shouldEmitRouterWarning(tier)) {
          const warning = {
            text: `[RECALL ROUTER WARNING] Fast prepass failed and fallback recall plan was used. Reason: ${reason}. Consider upgrading the fast model if this repeats.`,
            category: "system_notice",
            similarity: 1,
            via: "vector"
          };
          return sanitizeRecallResults([warning, ...fallbackResults]).slice(0, limit);
        }
        return sanitizeRecallResults(fallbackResults).slice(0, limit);
      }
      if (opts.failOpen && failHard) {
        const reason = err?.message || String(err);
        console.error(
          `[memory][recall-router][FAIL-HARD] Router prepass failed and fallback was blocked by failHard=true. reason="${reason}"`
        );
      }
      throw err;
    }
  }
  async function planToolHint(query) {
    const t = deps.trace;
    try {
      const clean = query.trim().replace(/\s+/g, " ");
      if (!clean) {
        t?.("tool_hint.skip", { reason: "empty_query" });
        return null;
      }
      const commands = deps.getCommandRegistry?.() ?? [];
      if (commands.length === 0) {
        t?.("tool_hint.skip", { reason: "empty_registry" });
        return null;
      }
      t?.("tool_hint.calling_llm", { commands: commands.length, query_len: clean.length });
      const commandList = commands.map((c) => `- [${c.id}] ${c.description}
  hint: "${c.hint}"`).join("\n");
      const systemPrompt = "You are a JSON-only router. Your entire response must be exactly one JSON object \u2014 no other characters, no markdown, no explanation.";
      const userMessage = 'Respond with exactly one JSON object and nothing else.\n\nFormat: {"command_id": "<id>"} or {"command_id": null}\n\nAvailable commands:\n' + commandList + "\n\nPick the command whose description best matches the message, or null if none clearly apply.\n\nMessage: " + clean;
      const raw = await deps.callFastRouter(systemPrompt, userMessage);
      t?.("tool_hint.llm_response", { raw_len: raw?.length ?? 0, raw_preview: (raw || "").slice(0, 120) });
      if (!raw) return null;
      const match = raw.match(/\{[\s\S]*?\}/);
      if (!match) return null;
      const data = JSON.parse(match[0]);
      const commandId = data?.command_id;
      if (!commandId || typeof commandId !== "string") {
        t?.("tool_hint.null_result", { command_id: String(commandId ?? "null") });
        return null;
      }
      const entry = commands.find((c) => c.id === commandId);
      if (!entry) {
        t?.("tool_hint.null_result", { reason: "unknown_command_id", command_id: commandId });
        return null;
      }
      t?.("tool_hint.produced", { command_id: commandId });
      return `<tool_hint>${entry.hint}</tool_hint>`;
    } catch (err) {
      t?.("tool_hint.error", { error: String(err?.message || err) });
      return null;
    }
  }
  return {
    normalizeKnowledgeDatastores,
    getKnowledgeDatastoreRegistry,
    renderKnowledgeDatastoreGuidanceForAgents,
    routeKnowledgeDatastores,
    routeRecallPlan,
    recall,
    planToolHint
  };
}
export {
  createKnowledgeEngine
};
