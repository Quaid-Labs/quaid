"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.createKnowledgeEngine = createKnowledgeEngine;
const knowledge_stores_js_1 = require("../../../core/knowledge-stores.js");
function createKnowledgeEngine(deps) {
    async function routeWithRepair(router, systemPrompt, userPrompt, validate, label) {
        const first = await router(systemPrompt, userPrompt);
        try {
            return validate(first);
        }
        catch (err) {
            const reason = err?.message || String(err);
            console.warn(`[quaid][recall-router] ${label}: invalid first response; retrying repair (${reason})`);
            const repairSystemPrompt = `${systemPrompt}\n\nReturn STRICT JSON only. No prose, no markdown, no comments, no code fences.`;
            const repairUserPrompt = `${userPrompt}\n\nPrevious response (invalid):\n${first}\n\nValidation error:\n${reason}\n\nRewrite into valid JSON matching the required schema exactly.`;
            const second = await router(repairSystemPrompt, repairUserPrompt);
            try {
                return validate(second);
            }
            catch (err2) {
                const reason2 = err2?.message || String(err2);
                throw new Error(`Fast recall prepass failed to produce valid structured output after retry (${label}). Use a stronger fast model (e.g. Haiku/GPT-class) or set retrieval.routerFailOpen=true. Last validation error: ${reason2}`);
            }
        }
    }
    function storeOption(opts, store, key) {
        return opts.datastoreOptions?.[store]?.[key];
    }
    function sanitizeRecallResults(items) {
        const out = [];
        for (const raw of items || []) {
            if (!raw || typeof raw !== "object")
                continue;
            const obj = raw;
            const text = String(obj.text || "").trim();
            if (!text)
                continue;
            const category = String(obj.category || "fact").trim() || "fact";
            const simRaw = Number(obj.similarity);
            const similarity = Number.isFinite(simRaw) ? Math.max(0, Math.min(1, simRaw)) : 0.5;
            const viaRaw = String(obj.via || "").trim().toLowerCase();
            const via = (viaRaw === "vector" || viaRaw === "graph" || viaRaw === "journal" || viaRaw === "project")
                ? viaRaw
                : "vector";
            out.push({
                ...obj,
                text,
                category,
                similarity,
                via,
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
            if (!allowed.includes(key))
                continue;
            if (!out.includes(key))
                out.push(key);
        }
        if (!out.length) {
            throw new Error("router returned no valid datastores");
        }
        return out;
    }
    const normalizeKnowledgeDatastores = knowledge_stores_js_1.normalizeKnowledgeDatastores;
    async function routeKnowledgeDatastores(query, expandGraph) {
        const allowed = (0, knowledge_stores_js_1.getRoutableDatastoreKeys)();
        const systemPrompt = `You route a recall query to knowledge datastores.
Choose the MINIMAL useful set.
Stores:
- vector_basic: personal/user facts
- vector_technical: technical/code/system facts
- graph: relationship traversal
- journal: reflective journal context
- project: project docs and architecture notes
Return JSON only: {"datastores":["vector_basic","graph"]}`;
        const userPrompt = `Query: "${query}"\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}`;
        return routeWithRepair(deps.callFastRouter, systemPrompt, userPrompt, (text) => {
            let payload = null;
            try {
                payload = JSON.parse(String(text || "").trim());
            }
            catch {
                const m = String(text || "").match(/\{[\s\S]*\}/);
                if (m) {
                    payload = JSON.parse(m[0]);
                }
                else {
                    throw new Error("router response is not JSON");
                }
            }
            if (!payload || typeof payload !== "object") {
                throw new Error("router response is not an object");
            }
            const datastores = parseRoutedDatastores(payload?.datastores, allowed);
            return datastores;
        }, "routeKnowledgeDatastores");
    }
    async function routeRecallPlan(query, expandGraph, reasoning = "fast", intent = "general") {
        const allowed = (0, knowledge_stores_js_1.getRoutableDatastoreKeys)();
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
- Use minimal datastores needed.
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
        const userPrompt = `Query: "${original}"\\nexpandGraphAllowed: ${expandGraph ? "true" : "false"}\\nintent: ${intent}`;
        const router = reasoning === "deep" && deps.callDeepRouter
            ? deps.callDeepRouter
            : deps.callFastRouter;
        return routeWithRepair(router, systemPrompt, userPrompt, (text) => {
            let payload = null;
            try {
                payload = JSON.parse(String(text || "").trim());
            }
            catch {
                const m = String(text || "").match(/\{[\s\S]*\}/);
                if (m) {
                    payload = JSON.parse(m[0]);
                }
                else {
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
        }, "routeRecallPlan");
    }
    function normalizeSourceType(value) {
        const raw = String(value || "").trim().toLowerCase();
        if (raw === "agent")
            return "assistant";
        if (raw === "user" || raw === "assistant" || raw === "both" || raw === "tool" || raw === "import") {
            return raw;
        }
        return undefined;
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
            ...(opts.ranking?.sourceTypeBoosts || {}),
        };
        if (Object.keys(boosts).length === 0)
            return items;
        return items.map((item) => {
            const fromItem = normalizeSourceType(item.sourceType || item.source_type);
            if (!fromItem)
                return item;
            const boost = boosts[fromItem];
            if (!boost || boost === 1)
                return item;
            const nextSimilarity = Math.max(0, Math.min(0.999, (item.similarity || 0) * boost));
            return { ...item, similarity: nextSimilarity };
        });
    }
    function tokenizeQuery(query) {
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
    async function recallFromJournalStore(query, limit) {
        if (!deps.isSystemEnabled("journal"))
            return [];
        const journalConfig = deps.getMemoryConfig().docs?.journal || {};
        const journalDir = deps.path.join(deps.workspace, journalConfig.journalDir || "journal");
        const tokens = tokenizeQuery(query);
        if (!tokens.length)
            return [];
        let files = [];
        try {
            files = deps.fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md"));
        }
        catch {
            return [];
        }
        const scored = [];
        for (const file of files) {
            try {
                const fullPath = deps.path.join(journalDir, file);
                const content = deps.fs.readFileSync(fullPath, "utf8");
                const lc = content.toLowerCase();
                let hits = 0;
                for (const t of tokens) {
                    if (lc.includes(t))
                        hits += 1;
                }
                if (hits === 0)
                    continue;
                const excerpt = content.replace(/\s+/g, " ").trim().slice(0, 220);
                const similarity = Math.min(0.95, 0.45 + (hits / Math.max(tokens.length, 1)) * 0.5);
                scored.push({
                    text: `${file}: ${excerpt}${content.length > 220 ? "..." : ""}`,
                    category: "journal",
                    similarity,
                    via: "journal",
                });
            }
            catch { }
        }
        scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
        return scored.slice(0, limit);
    }
    async function recallFromProjectStore(query, limit, project, docs) {
        if (!deps.isSystemEnabled("projects"))
            return [];
        try {
            const args = [query, "--limit", String(limit)];
            if (project)
                args.push("--project", project);
            if (Array.isArray(docs) && docs.length > 0) {
                args.push("--docs", docs.join(","));
            }
            const out = await deps.callDocsRag("search", args);
            if (!out || !out.trim())
                return [];
            const results = [];
            const lines = out.split("\n");
            for (const line of lines) {
                const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
                if (!m)
                    continue;
                const file = m[1].split("/").pop() || m[1];
                const section = m[2].trim();
                const sim = Number.parseFloat(m[3]) || 0.6;
                results.push({
                    text: `${file} > ${section}`,
                    category: "project",
                    similarity: sim,
                    via: "project",
                });
            }
            if (results.length === 0) {
                results.push({
                    text: out.replace(/\s+/g, " ").slice(0, 280),
                    category: "project",
                    similarity: 0.55,
                    via: "project",
                });
            }
            return results.slice(0, limit);
        }
        catch {
            return [];
        }
    }
    async function totalRecall(query, limit, opts) {
        const datastores = normalizeKnowledgeDatastores(opts.datastores, opts.expandGraph);
        const all = [];
        const descriptors = {
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
        for (const store of datastores) {
            const descriptor = descriptors[store];
            if (!descriptor)
                continue;
            all.push(...(await descriptor.recall({ query, limit, opts })));
        }
        const dedup = new Map();
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
    async function total_recall(query, limit, opts) {
        try {
            const plan = await routeRecallPlan(query, opts.expandGraph, opts.reasoning || "fast", opts.intent || "general");
            const routed = await totalRecall(plan.query, limit, {
                ...opts,
                datastores: plan.datastores,
                project: plan.project,
            });
            return sanitizeRecallResults(routed).slice(0, limit);
        }
        catch (err) {
            if (opts.failOpen) {
                const reason = err?.message || String(err);
                const fallbackDatastores = normalizeKnowledgeDatastores(undefined, opts.expandGraph);
                console.error(`[quaid][recall-router][FAIL-OPEN] Router prepass failed; using deterministic default recall plan. ` +
                    `reason="${reason}" datastores=${fallbackDatastores.join(",")}`);
                const fallbackResults = await totalRecall(query, limit, {
                    ...opts,
                    datastores: fallbackDatastores,
                });
                const warning = {
                    text: `[RECALL ROUTER WARNING] Fast prepass failed and fallback recall plan was used. ` +
                        `Reason: ${reason}. Consider upgrading the fast model if this repeats.`,
                    category: "system_notice",
                    similarity: 1.0,
                    via: "vector",
                };
                return sanitizeRecallResults([warning, ...fallbackResults]).slice(0, limit);
            }
            throw err;
        }
    }
    return {
        normalizeKnowledgeDatastores,
        getKnowledgeDatastoreRegistry: knowledge_stores_js_1.getKnowledgeDatastoreRegistry,
        renderKnowledgeDatastoreGuidanceForAgents: knowledge_stores_js_1.renderKnowledgeDatastoreGuidanceForAgents,
        routeKnowledgeDatastores,
        routeRecallPlan,
        totalRecall,
        total_recall,
    };
}
