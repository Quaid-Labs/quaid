import { createDatastoreBridge } from "./datastore-bridge.js";
import { createProjectCatalogReader } from "./project-catalog.js";
import { createKnowledgeEngine } from "./knowledge-engine.js";
import {
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents,
} from "./knowledge-stores.js";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

const FAST_ROUTER_TIMEOUT_MS = 45000;
const DEEP_ROUTER_TIMEOUT_MS = 60000;
const NODE_COUNT_CACHE_MS = 120000;
const DATASTORE_STATS_TIMEOUT_MS = 30000;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_MEMORY_NOTE_SESSIONS = 200;

function createQuaidFacade(deps) {
  const datastoreBridge = createDatastoreBridge(deps.execPython);

  const projectCatalogReader = createProjectCatalogReader({
    workspace: deps.workspace,
    fs,
    path,
    isFailHardEnabled: deps.isFailHardEnabled,
  });

  const knowledgeEngine = createKnowledgeEngine({
    workspace: deps.workspace,
    getMemoryConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    callFastRouter: async (systemPrompt, userPrompt) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "fast", 120, FAST_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    callDeepRouter: async (systemPrompt, userPrompt) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "deep", 160, DEEP_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    recallVector: async (query, limit, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(query, limit, false, 1, scope, domainBoost, project, dateFrom, dateTo);
      return results.map((r) => ({ ...r, via: "vector" }));
    },
    recallGraph: async (query, limit, depth, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(query, limit, true, depth, scope, domainBoost, project, dateFrom, dateTo);
      return results
        .filter((r) => (r.via || "") === "graph" || r.category === "graph")
        .map((r) => ({ ...r, via: "graph" }));
    },
    recallJournalStore: async (query, limit) => {
      const journalConfig = deps.getMemoryConfig().docs?.journal || {};
      const journalDir = path.join(deps.workspace, journalConfig.journalDir || "journal");
      return recallFromJournal(query, limit, journalDir);
    },
    recallProjectStore: async (query, limit, project, docs) => {
      const args = [query, "--limit", String(limit)];
      if (project) args.push("--project", project);
      if (Array.isArray(docs) && docs.length > 0) args.push("--docs", docs.join(","));
      const out = await deps.execDocsRag("search", args);
      return parseProjectStoreResults(out);
    },
  });

  let _cachedDatastoreStats = null;
  let _datastoreStatsTimestamp = 0;
  let _cachedNodeCount = null;
  let _nodeCountTimestamp = 0;

  function getDatastoreStatsSync(maxAgeMs = NODE_COUNT_CACHE_MS) {
    const now = Date.now();
    if ((now - _datastoreStatsTimestamp) < maxAgeMs) return _cachedDatastoreStats;
    try {
      const pyScript = path.join(deps.pluginRoot, "datastore/memorydb/memory_graph.py");
      const output = execFileSync("python3", [pyScript, "stats"], {
        encoding: "utf-8",
        timeout: DATASTORE_STATS_TIMEOUT_MS,
        env: {
          ...process.env,
          MEMORY_DB_PATH: deps.dbPath,
          QUAID_HOME: deps.workspace,
          CLAWDBOT_WORKSPACE: deps.workspace,
          PYTHONPATH: deps.pluginRoot,
        },
      });
      const parsed = JSON.parse(output);
      if (!parsed || typeof parsed !== "object") {
        _cachedDatastoreStats = null;
        _datastoreStatsTimestamp = now;
        return null;
      }
      _cachedDatastoreStats = parsed;
      _datastoreStatsTimestamp = now;
      return parsed;
    } catch (err) {
      const msg = `[quaid][facade] datastore stats read failed: ${err?.message || String(err)}`;
      if (deps.isFailHardEnabled()) throw new Error(msg, { cause: err instanceof Error ? err : new Error(String(err)) });
      _cachedDatastoreStats = null;
      _datastoreStatsTimestamp = now;
      console.warn(msg);
      return null;
    }
  }

  function getActiveNodeCount() {
    const now = Date.now();
    if (_cachedNodeCount !== null && (now - _nodeCountTimestamp) < NODE_COUNT_CACHE_MS) return _cachedNodeCount;
    const stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
    const active = Number(stats?.active_nodes ?? stats?.by_status?.active ?? 0);
    if (Number.isFinite(active) && active > 0) {
      _cachedNodeCount = active;
      _nodeCountTimestamp = now;
      return _cachedNodeCount;
    }
    if (_cachedNodeCount === null && deps.isFailHardEnabled()) {
      throw new Error("[quaid][facade] unable to derive active node count under failHard");
    }
    return _cachedNodeCount ?? 100;
  }

  function computeDynamicK() {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10) return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
  }

  async function recallFromBridge(query, limit, expandGraph, graphDepth, domain, domainBoost, project, dateFrom, dateTo) {
    try {
      const args = [query, "--limit", String(limit), "--json"];
      if (domain && typeof domain === "object") args.push("--domain", JSON.stringify(domain));
      if (domainBoost) args.push("--domain-boost", JSON.stringify(domainBoost));
      if (project) args.push("--project", project);
      if (dateFrom) args.push("--date-from", dateFrom);
      if (dateTo) args.push("--date-to", dateTo);
      let output;
      if (expandGraph) {
        args.push("--depth", String(graphDepth));
        output = await datastoreBridge.searchGraphAware(args);
      } else {
        output = await datastoreBridge.search(args);
      }
      return parseMemoryResults(output, expandGraph);
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.error("[quaid][facade] recall error:", err?.message);
      return [];
    }
  }

  function parseMemoryResults(output, expandGraph) {
    const results = [];
    if (!output || !output.trim()) return results;
    try {
      const parsed = JSON.parse(output);
      const items = Array.isArray(parsed) ? parsed : (parsed?.results || parsed?.items || []);
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const text = String(item.text || "").trim();
        if (!text) continue;
        let domains;
        if (Array.isArray(item.domains)) domains = item.domains.map((d) => String(d || "").trim()).filter(Boolean);
        else if (typeof item.domains === "string") {
          try { const p = JSON.parse(item.domains); if (Array.isArray(p)) domains = p; } catch {}
        }
        results.push({
          text, category: String(item.category || "fact"), similarity: Number(item.similarity) || 0.5,
          id: item.id ? String(item.id) : undefined, domains,
          sourceType: item.source_type || item.sourceType || undefined,
          extractionConfidence: typeof item.extraction_confidence === "number" ? item.extraction_confidence : undefined,
          createdAt: item.created_at || item.createdAt || undefined,
          validFrom: item.valid_from || item.validFrom || undefined,
          validUntil: item.valid_until || item.validUntil || undefined,
          privacy: item.privacy || undefined, ownerId: item.owner_id || item.ownerId || undefined,
          via: expandGraph ? undefined : "vector",
        });
      }
      if (expandGraph) {
        const rels = parsed?.relationships || parsed?.graph_results || [];
        for (const r of rels) {
          if (!r || typeof r !== "object") continue;
          const id = typeof r.id === "string" ? r.id : (typeof r.id === "number" ? String(r.id) : "");
          const name = typeof r.name === "string" ? r.name : "";
          const relation = typeof r.relation === "string" ? r.relation : "";
          const direction = typeof r.direction === "string" ? r.direction : "out";
          const sourceName = typeof r.source_name === "string" ? r.source_name : "";
          if (!id || !name || !relation || !sourceName) continue;
          const text = direction === "in" ? `${name} --${relation}--> ${sourceName}` : `${sourceName} --${relation}--> ${name}`;
          results.push({ text, category: "graph", similarity: 0.75, id, relation, direction, sourceName, via: "graph" });
        }
      }
    } catch {
      for (const line of output.split("\n")) {
        if (line.startsWith("[direct]")) {
          const match = line.match(/\[direct\]\s+\[(\d+\.\d+)\]\s+\[(\w+)\]\s+(.+)/);
          if (match) results.push({ text: match[3].trim(), category: match[2], similarity: parseFloat(match[1]), via: "vector" });
        } else if (line.startsWith("[graph]")) {
          results.push({ text: line.substring(7).trim(), category: "graph", similarity: 0.75, via: "graph" });
        }
      }
    }
    return results;
  }

  function recallFromJournal(query, limit, journalDir) {
    const stop = new Set(["the","and","for","with","that","this","from","have","has","was","were","what","when","where","which","who","how","why","about","tell","me","your","my","our","their","his","her","its","into","onto","than","then"]);
    const tokens = Array.from(new Set(String(query || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).map((t) => t.trim()).filter((t) => t.length >= 3 && !stop.has(t)))).slice(0, 16);
    if (!tokens.length) return [];
    let files = [];
    try { files = fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md")); }
    catch (err) {
      if (deps.isFailHardEnabled()) throw new Error("[quaid][facade] Journal recall listing failed under failHard", { cause: err });
      console.warn(`[quaid][facade] Journal recall listing failed: ${String(err?.message || err)}`);
      return [];
    }
    const scored = [];
    for (const file of files) {
      try {
        const fullPath = path.join(journalDir, file);
        const content = fs.readFileSync(fullPath, "utf8");
        const lc = content.toLowerCase();
        let hits = 0;
        for (const t of tokens) { if (lc.includes(t)) hits += 1; }
        if (hits === 0) continue;
        const excerpt = content.replace(/\s+/g, " ").trim().slice(0, 220);
        const similarity = Math.min(0.95, 0.45 + (hits / Math.max(tokens.length, 1)) * 0.5);
        scored.push({ text: `${file}: ${excerpt}${content.length > 220 ? "..." : ""}`, category: "journal", similarity, via: "journal" });
      } catch (err) {
        if (deps.isFailHardEnabled()) throw new Error(`[quaid][facade] Journal recall read failed for ${file} under failHard`, { cause: err });
        console.warn(`[quaid][facade] Journal recall read failed for ${file}: ${String(err?.message || err)}`);
      }
    }
    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  }

  function parseProjectStoreResults(out) {
    if (!out || !out.trim()) return [];
    const results = [];
    for (const line of out.split("\n")) {
      const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
      if (!m) continue;
      results.push({ text: `${m[1]} > ${m[2].trim()}`, category: "project", similarity: Number.parseFloat(m[3]) || 0.6, via: "project" });
    }
    return results;
  }

  const _memoryNotes = new Map();
  const _memoryNotesTouchedAt = new Map();
  const NOTES_DIR = path.join(deps.workspace, ".quaid", "runtime", "notes");

  function getNotesPath(sessionId) { return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`); }

  function _sleepMs(ms) {
    const i32 = new Int32Array(new SharedArrayBuffer(4));
    Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
  }

  function withNotesLock(sessionId, fn) {
    const lockPath = `${getNotesPath(sessionId)}.lock`;
    let fd;
    let lastErr;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try { fd = fs.openSync(lockPath, "wx", 0o600); break; }
      catch (err) { if (err?.code !== "EEXIST") throw err; lastErr = err; _sleepMs(10); }
    }
    if (fd === undefined) throw new Error(`failed to acquire memory-notes lock for session=${sessionId}: ${String(lastErr?.message || lastErr)}`);
    try { return fn(); }
    finally { try { fs.closeSync(fd); } catch {} try { fs.unlinkSync(lockPath); } catch {} }
  }

  function addMemoryNote(sessionId, text, category) {
    _memoryNotesTouchedAt.set(sessionId, Date.now());
    if (_memoryNotes.size >= MAX_MEMORY_NOTE_SESSIONS && !_memoryNotes.has(sessionId)) {
      const oldest = Array.from(_memoryNotesTouchedAt.entries()).sort((a, b) => a[1] - b[1])[0]?.[0];
      if (oldest) { _memoryNotes.delete(oldest); _memoryNotesTouchedAt.delete(oldest); }
    }
    if (!_memoryNotes.has(sessionId)) _memoryNotes.set(sessionId, []);
    const noteList = _memoryNotes.get(sessionId);
    noteList.push(`[${category}] ${text}`);
    if (noteList.length > MAX_MEMORY_NOTES_PER_SESSION) noteList.splice(0, noteList.length - MAX_MEMORY_NOTES_PER_SESSION);
    try {
      withNotesLock(sessionId, () => {
        const notesPath = getNotesPath(sessionId);
        let existing = [];
        try { existing = JSON.parse(fs.readFileSync(notesPath, "utf8")); }
        catch (err) { if (!String(err?.message || err).includes("ENOENT") && deps.isFailHardEnabled()) throw err; }
        existing.push(`[${category}] ${text}`);
        fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 0o600 });
      });
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.warn(`[quaid][facade] memory note write failed for session ${sessionId}: ${String(err?.message || err)}`);
    }
  }

  function getAndClearMemoryNotes(sessionId) {
    return withNotesLock(sessionId, () => {
      const inMemory = _memoryNotes.get(sessionId) || [];
      let onDisk = [];
      try { onDisk = JSON.parse(fs.readFileSync(getNotesPath(sessionId), "utf8")); } catch {}
      const merged = [...new Set([...onDisk, ...inMemory])];
      _memoryNotes.delete(sessionId);
      _memoryNotesTouchedAt.delete(sessionId);
      try { fs.unlinkSync(getNotesPath(sessionId)); } catch {}
      return merged;
    });
  }

  async function recall(opts) {
    const {
      query, limit = 10, expandGraph = true, graphDepth = 1,
      datastores, routeStores, reasoning = "fast", intent = "general",
      ranking, domain = { all: true }, domainBoost, project,
      dateFrom, dateTo, docs, datastoreOptions, failOpen,
    } = opts;
    const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
    const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
    const runRecall = (q) => {
      const recallOpts = { datastores: selectedStores, expandGraph, graphDepth, intent, ranking, domain, domainBoost, project, dateFrom, dateTo, docs, datastoreOptions };
      if (shouldRouteStores) return knowledgeEngine.total_recall(q, limit, { ...recallOpts, reasoning, failOpen });
      return knowledgeEngine.totalRecall(q, limit, recallOpts);
    };
    return runRecall(query);
  }

  function notImplemented(name) { throw new Error(`[quaid][facade] ${name} is not yet implemented — scheduled for a future PR`); }

  return {
    getConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    isFailHardEnabled: deps.isFailHardEnabled,
    stats: () => datastoreBridge.stats(),
    store: (args) => datastoreBridge.store(args),
    forget: (args) => datastoreBridge.forget(args),
    searchBySession: (sessionId, limit = 20) => datastoreBridge.search(["*", "--session-id", sessionId, "--owner", deps.resolveOwner(), "--limit", String(limit)]),
    emitEvent: async (name, payload, dispatch = "auto") => {
      const args = ["--name", name, "--payload", JSON.stringify(payload || {}), "--source", "openclaw_adapter", "--dispatch", dispatch];
      const out = await deps.execEvents("emit", args);
      let parsed = null;
      try {
        parsed = JSON.parse(out || "{}");
      } catch (err) {
        const msg = String(err?.message || err);
        throw new Error(`[quaid][facade] events emit returned invalid JSON: ${msg}`);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("[quaid][facade] events emit returned non-object payload");
      }
      return parsed;
    },
    recall,
    computeDynamicK,
    getActiveNodeCount,
    docsSearch: (query, args) => deps.execDocsRag("search", [query, ...args]),
    docsRead: (identifier) => deps.execDocsRegistry("read", [identifier]),
    docsList: (args) => deps.execDocsRegistry("list", args),
    docsRegister: (args) => deps.execDocsRegistry("register", args),
    docsCreateProject: (args) => deps.execDocsRegistry("create-project", args),
    docsListProjects: (args = ["--json"]) => deps.execDocsRegistry("list-projects", args),
    docsCheckStaleness: () => deps.execDocsUpdater("check", ["--json"]),
    addMemoryNote,
    getAndClearMemoryNotes,
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    getProjectNames: () => projectCatalogReader.getProjectNames(),
    renderDatastoreGuidance: renderKnowledgeDatastoreGuidanceForAgents,
    detectLifecycleSignal: () => notImplemented("detectLifecycleSignal"),
    processLifecycleEvent: () => notImplemented("processLifecycleEvent"),
    maybeRunMaintenance: () => notImplemented("maybeRunMaintenance"),
    getJanitorHealthIssue: () => {
      const stats = getDatastoreStatsSync(60 * 1000);
      const completedAt = String(stats?.last_janitor_completed_at || "").trim();
      if (!completedAt) return "[Quaid] Janitor has never run. Please run janitor and ensure schedule is active.";
      const ts = Date.parse(completedAt);
      if (Number.isNaN(ts)) return null;
      const hours = (Date.now() - ts) / (1000 * 60 * 60);
      if (hours > 72) return `[Quaid] Janitor appears unhealthy (last successful run ${Math.floor(hours)}h ago). Diagnose scheduler/run path and run janitor.`;
      if (hours > 48) return `[Quaid] Janitor may be delayed (last successful run ${Math.floor(hours)}h ago). Verify schedule and run status.`;
      return null;
    },
    queueDelayedRequest: () => notImplemented("queueDelayedRequest"),
  };
}

export { createQuaidFacade };
