/**
 * Adapter-facing facade — the single entry point between any adapter and the
 * Quaid core/orchestrator internals.
 *
 * Adapters create a facade via `createQuaidFacade(deps)` and call its methods
 * instead of reaching directly into datastore, ingest, or runtime modules.
 */

import type { PythonBridgeExec } from "./datastore-bridge.js";
import { createDatastoreBridge } from "./datastore-bridge.js";
import { createProjectCatalogReader } from "./project-catalog.js";
import { createKnowledgeEngine } from "./knowledge-engine.js";
import type { TotalRecallOptions } from "../orchestrator/default-orchestrator.js";
import {
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents,
} from "./knowledge-stores.js";
import type { KnowledgeDatastore, DomainFilter, RecallIntent } from "./knowledge-stores.js";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ModelTier = "deep" | "fast";

export type LLMCallResult = {
  text: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  truncated: boolean;
};

/** Platform-specific dependencies that any adapter must provide. */
export type QuaidFacadeDeps = {
  workspace: string;
  pluginRoot: string;
  dbPath: string;
  execPython: PythonBridgeExec;
  execExtractPipeline: (tmpPath: string, args: string[]) => Promise<string>;
  execDocsRag: (command: string, args: string[]) => Promise<string>;
  execDocsRegistry: (command: string, args: string[]) => Promise<string>;
  execDocsUpdater: (command: string, args: string[]) => Promise<string>;
  execEvents: (command: string, args: string[]) => Promise<string>;
  callLLM: (
    systemPrompt: string,
    userPrompt: string,
    tier: ModelTier,
    maxTokens: number,
    timeoutMs?: number,
  ) => Promise<LLMCallResult | null>;
  getMemoryConfig: () => any;
  isSystemEnabled: (system: "memory" | "journal" | "projects" | "workspace") => boolean;
  isFailHardEnabled: () => boolean;
  resolveOwner: () => string;
};

/** Memory result shape returned by recall operations. */
export type MemoryResult = {
  text: string;
  category: string;
  similarity: number;
  domains?: string[];
  sourceType?: string;
  verified?: boolean;
  id?: string;
  extractionConfidence?: number;
  createdAt?: string;
  validFrom?: string;
  validUntil?: string;
  privacy?: string;
  ownerId?: string;
  relation?: string;
  direction?: string;
  sourceName?: string;
  via?: string;
};

/** Options for facade-level recall. */
export type FacadeRecallOptions = {
  query: string;
  limit?: number;
  expandGraph?: boolean;
  graphDepth?: number;
  datastores?: KnowledgeDatastore[];
  routeStores?: boolean;
  reasoning?: "fast" | "deep";
  intent?: RecallIntent;
  ranking?: { sourceTypeBoosts?: Partial<Record<string, number>> };
  domain?: DomainFilter;
  domainBoost?: Record<string, number> | string[];
  project?: string;
  dateFrom?: string;
  dateTo?: string;
  docs?: string[];
  datastoreOptions?: Partial<Record<KnowledgeDatastore, Record<string, unknown>>>;
  failOpen?: boolean;
};

/** The business contract adapters call. */
export type QuaidFacade = {
  // --- Pass-through to config / core ---
  getConfig: () => any;
  isSystemEnabled: (system: "memory" | "journal" | "projects" | "workspace") => boolean;
  isFailHardEnabled: () => boolean;

  // --- Datastore stats ---
  stats: () => Promise<string>;

  // --- Write / delete ---
  store: (args: string[]) => Promise<string>;
  forget: (args: string[]) => Promise<string>;
  searchBySession: (sessionId: string, limit?: number) => Promise<string>;

  // --- Events ---
  emitEvent: (command: string, args: string[]) => Promise<string>;

  // --- Recall (routed via orchestrator) ---
  recall: (opts: FacadeRecallOptions) => Promise<MemoryResult[]>;

  // --- Dynamic K ---
  computeDynamicK: () => number;
  getActiveNodeCount: () => number;

  // --- Docs (Python bridge wrapped) ---
  docsSearch: (query: string, args: string[]) => Promise<string>;
  docsRead: (identifier: string) => Promise<string>;
  docsList: (args: string[]) => Promise<string>;
  docsRegister: (args: string[]) => Promise<string>;
  docsCreateProject: (args: string[]) => Promise<string>;
  docsListProjects: (args?: string[]) => Promise<string>;
  docsCheckStaleness: () => Promise<string>;

  // --- Memory notes (state management) ---
  addMemoryNote: (sessionId: string, text: string, category: string) => void;
  getAndClearMemoryNotes: (sessionId: string) => string[];

  // --- Project catalog ---
  getProjectCatalog: () => Array<{ name: string; description: string }>;
  getProjectNames: () => string[];

  // --- Guidance rendering ---
  renderDatastoreGuidance: () => string;

  // --- Stubs (typed, not yet implemented) ---
  detectLifecycleSignal: (messages: unknown[]) => never;
  processLifecycleEvent: (signal: unknown, context: unknown) => never;
  maybeRunMaintenance: (sessionId: string) => never;
  getJanitorHealthIssue: () => never;
  queueDelayedRequest: (request: unknown) => never;
};

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

const FAST_ROUTER_TIMEOUT_MS = 45_000;
const DEEP_ROUTER_TIMEOUT_MS = 60_000;
const NODE_COUNT_CACHE_MS = 120_000;
const DATASTORE_STATS_TIMEOUT_MS = 30_000;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_MEMORY_NOTE_SESSIONS = 200;

export function createQuaidFacade(deps: QuaidFacadeDeps): QuaidFacade {
  // -------------------------------------------------------------------------
  // Internal bridges (adapter no longer touches these directly)
  // -------------------------------------------------------------------------

  const datastoreBridge = createDatastoreBridge(deps.execPython);

  const projectCatalogReader = createProjectCatalogReader({
    workspace: deps.workspace,
    fs,
    path,
    isFailHardEnabled: deps.isFailHardEnabled,
  });

  // -------------------------------------------------------------------------
  // Knowledge engine (orchestrator)
  // -------------------------------------------------------------------------

  const knowledgeEngine = createKnowledgeEngine<MemoryResult>({
    workspace: deps.workspace,
    getMemoryConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    callFastRouter: async (systemPrompt: string, userPrompt: string) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "fast", 120, FAST_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    callDeepRouter: async (systemPrompt: string, userPrompt: string) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "deep", 160, DEEP_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    recallVector: async (query, limit, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(
        query, limit, false, 1, scope, domainBoost, project, dateFrom, dateTo,
      );
      return results.map((r) => ({ ...r, via: "vector" as const }));
    },
    recallGraph: async (query, limit, depth, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(
        query, limit, true, depth, scope, domainBoost, project, dateFrom, dateTo,
      );
      return results
        .filter((r) => (r.via || "") === "graph" || r.category === "graph")
        .map((r) => ({ ...r, via: "graph" as const }));
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
      return parseProjectStoreResults(out, query, limit, project);
    },
  });

  // -------------------------------------------------------------------------
  // Datastore stats cache (for computeDynamicK)
  // -------------------------------------------------------------------------

  let _cachedDatastoreStats: Record<string, any> | null = null;
  let _datastoreStatsTimestamp = 0;
  let _cachedNodeCount: number | null = null;
  let _nodeCountTimestamp = 0;

  function getDatastoreStatsSync(maxAgeMs: number = NODE_COUNT_CACHE_MS): Record<string, any> | null {
    const now = Date.now();
    if ((now - _datastoreStatsTimestamp) < maxAgeMs) {
      return _cachedDatastoreStats;
    }
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
    } catch (err: unknown) {
      const msg = `[quaid][facade] datastore stats read failed: ${(err as Error)?.message || String(err)}`;
      if (deps.isFailHardEnabled()) {
        throw new Error(msg, { cause: err instanceof Error ? err : new Error(String(err)) });
      }
      _cachedDatastoreStats = null;
      _datastoreStatsTimestamp = now;
      console.warn(msg);
      return null;
    }
  }

  function getActiveNodeCount(): number {
    const now = Date.now();
    if (_cachedNodeCount !== null && (now - _nodeCountTimestamp) < NODE_COUNT_CACHE_MS) {
      return _cachedNodeCount;
    }
    const stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
    const active = Number(stats?.by_status?.active ?? 0);
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

  function computeDynamicK(): number {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10) return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
  }

  // -------------------------------------------------------------------------
  // Bridge recall helper (calls datastoreBridge.search / searchGraphAware)
  // -------------------------------------------------------------------------

  async function recallFromBridge(
    query: string,
    limit: number,
    expandGraph: boolean,
    graphDepth: number,
    domain: DomainFilter,
    domainBoost?: Record<string, number> | string[],
    project?: string,
    dateFrom?: string,
    dateTo?: string,
  ): Promise<MemoryResult[]> {
    try {
      const args: string[] = [
        query, "--limit", String(limit), "--json",
      ];
      if (domain && typeof domain === "object") {
        args.push("--domain", JSON.stringify(domain));
      }
      if (domainBoost) {
        args.push("--domain-boost", JSON.stringify(domainBoost));
      }
      if (project) {
        args.push("--project", project);
      }
      if (dateFrom) {
        args.push("--date-from", dateFrom);
      }
      if (dateTo) {
        args.push("--date-to", dateTo);
      }

      let output: string;
      if (expandGraph) {
        args.push("--depth", String(graphDepth));
        output = await datastoreBridge.searchGraphAware(args);
      } else {
        output = await datastoreBridge.search(args);
      }

      return parseMemoryResults(output, expandGraph);
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) throw err;
      console.error("[quaid][facade] recall error:", (err as Error).message);
      return [];
    }
  }

  function parseMemoryResults(output: string, expandGraph: boolean): MemoryResult[] {
    const results: MemoryResult[] = [];
    if (!output || !output.trim()) return results;

    try {
      const parsed = JSON.parse(output);
      const items = Array.isArray(parsed) ? parsed : (parsed?.results || parsed?.items || []);
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const text = String(item.text || "").trim();
        if (!text) continue;

        const domains = (() => {
          if (Array.isArray(item.domains)) return item.domains.map((d: unknown) => String(d || "").trim()).filter(Boolean);
          if (typeof item.domains === "string") {
            try { const p = JSON.parse(item.domains); if (Array.isArray(p)) return p; } catch {}
          }
          return undefined;
        })();

        results.push({
          text,
          category: String(item.category || "fact"),
          similarity: Number(item.similarity) || 0.5,
          id: item.id ? String(item.id) : undefined,
          domains,
          sourceType: item.source_type || item.sourceType || undefined,
          extractionConfidence: typeof item.extraction_confidence === "number" ? item.extraction_confidence : undefined,
          createdAt: item.created_at || item.createdAt || undefined,
          validFrom: item.valid_from || item.validFrom || undefined,
          validUntil: item.valid_until || item.validUntil || undefined,
          privacy: item.privacy || undefined,
          ownerId: item.owner_id || item.ownerId || undefined,
          via: expandGraph ? undefined : "vector",
        });
      }

      // Parse graph relationships if present
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

          const text = direction === "in"
            ? `${name} --${relation}--> ${sourceName}`
            : `${sourceName} --${relation}--> ${name}`;

          results.push({
            text,
            category: "graph",
            similarity: 0.75,
            id,
            relation,
            direction,
            sourceName,
            via: "graph",
          });
        }
      }
    } catch {
      // Fallback: line format
      for (const line of output.split("\n")) {
        if (line.startsWith("[direct]")) {
          const match = line.match(/\[direct\]\s+\[(\d+\.\d+)\]\s+\[(\w+)\]\s+(.+)/);
          if (match) {
            results.push({
              text: match[3].trim(),
              category: match[2],
              similarity: parseFloat(match[1]),
              via: "vector",
            });
          }
        } else if (line.startsWith("[graph]")) {
          results.push({
            text: line.substring(7).trim(),
            category: "graph",
            similarity: 0.75,
            via: "graph",
          });
        }
      }
    }

    return results;
  }

  // -------------------------------------------------------------------------
  // Journal store recall
  // -------------------------------------------------------------------------

  function recallFromJournal(query: string, limit: number, journalDir: string): MemoryResult[] {
    const stop = new Set([
      "the", "and", "for", "with", "that", "this", "from", "have", "has", "was", "were",
      "what", "when", "where", "which", "who", "how", "why", "about", "tell", "me", "your",
      "my", "our", "their", "his", "her", "its", "into", "onto", "than", "then",
    ]);
    const tokens = Array.from(new Set(
      String(query || "")
        .toLowerCase()
        .replace(/[^a-z0-9\s]/g, " ")
        .split(/\s+/)
        .map((t) => t.trim())
        .filter((t) => t.length >= 3 && !stop.has(t))
    )).slice(0, 16);
    if (!tokens.length) return [];

    let files: string[] = [];
    try {
      files = fs.readdirSync(journalDir).filter((f: string) => f.endsWith(".journal.md"));
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw new Error("[quaid][facade] Journal recall listing failed under failHard", { cause: err as Error });
      }
      console.warn(`[quaid][facade] Journal recall listing failed: ${String((err as Error)?.message || err)}`);
      return [];
    }

    const scored: MemoryResult[] = [];
    for (const file of files) {
      try {
        const fullPath = path.join(journalDir, file);
        const content = fs.readFileSync(fullPath, "utf8");
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
        });
      } catch (err: unknown) {
        if (deps.isFailHardEnabled()) {
          throw new Error(`[quaid][facade] Journal recall read failed for ${file} under failHard`, { cause: err as Error });
        }
        console.warn(`[quaid][facade] Journal recall read failed for ${file}: ${String((err as Error)?.message || err)}`);
      }
    }
    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  }

  // -------------------------------------------------------------------------
  // Project store result parser
  // -------------------------------------------------------------------------

  function parseProjectStoreResults(
    out: string,
    _query: string,
    _limit: number,
    _project?: string,
  ): MemoryResult[] {
    if (!out || !out.trim()) return [];
    const results: MemoryResult[] = [];
    const lines = out.split("\n");
    for (const line of lines) {
      const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
      if (!m) continue;
      const sourcePath = m[1];
      const section = m[2].trim();
      const sim = Number.parseFloat(m[3]) || 0.6;
      results.push({
        text: `${sourcePath} > ${section}`,
        category: "project",
        similarity: sim,
        via: "project",
      });
    }
    return results;
  }

  // -------------------------------------------------------------------------
  // Memory notes queue (session-scoped)
  // -------------------------------------------------------------------------

  const _memoryNotes = new Map<string, string[]>();
  const _memoryNotesTouchedAt = new Map<string, number>();
  const NOTES_DIR = path.join(deps.workspace, ".quaid", "runtime", "notes");

  function getNotesPath(sessionId: string): string {
    return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`);
  }

  function _sleepMs(ms: number): void {
    const i32 = new Int32Array(new SharedArrayBuffer(4));
    Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
  }

  function withNotesLock<T>(sessionId: string, fn: () => T): T {
    const lockPath = `${getNotesPath(sessionId)}.lock`;
    let fd: number | undefined;
    let lastErr: unknown;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        fd = fs.openSync(lockPath, "wx", 0o600);
        break;
      } catch (err: unknown) {
        const code = (err as NodeJS.ErrnoException)?.code;
        if (code !== "EEXIST") throw err;
        lastErr = err;
        _sleepMs(10);
      }
    }
    if (fd === undefined) {
      throw new Error(`failed to acquire memory-notes lock for session=${sessionId}: ${String((lastErr as Error)?.message || lastErr)}`);
    }
    try {
      return fn();
    } finally {
      try { fs.closeSync(fd); } catch {}
      try { fs.unlinkSync(lockPath); } catch {}
    }
  }

  function addMemoryNote(sessionId: string, text: string, category: string): void {
    _memoryNotesTouchedAt.set(sessionId, Date.now());
    if (_memoryNotes.size >= MAX_MEMORY_NOTE_SESSIONS && !_memoryNotes.has(sessionId)) {
      const oldest = Array.from(_memoryNotesTouchedAt.entries()).sort((a, b) => a[1] - b[1])[0]?.[0];
      if (oldest) {
        _memoryNotes.delete(oldest);
        _memoryNotesTouchedAt.delete(oldest);
      }
    }

    if (!_memoryNotes.has(sessionId)) {
      _memoryNotes.set(sessionId, []);
    }
    const noteList = _memoryNotes.get(sessionId)!;
    noteList.push(`[${category}] ${text}`);
    if (noteList.length > MAX_MEMORY_NOTES_PER_SESSION) {
      noteList.splice(0, noteList.length - MAX_MEMORY_NOTES_PER_SESSION);
    }

    try {
      withNotesLock(sessionId, () => {
        const notesPath = getNotesPath(sessionId);
        let existing: string[] = [];
        try {
          existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
        } catch (err: unknown) {
          const msg = String((err as Error)?.message || err);
          if (!msg.includes("ENOENT") && deps.isFailHardEnabled()) throw err;
        }
        existing.push(`[${category}] ${text}`);
        fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 0o600 });
      });
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) throw err;
      console.warn(`[quaid][facade] memory note write failed for session ${sessionId}: ${String((err as Error)?.message || err)}`);
    }
  }

  function getAndClearMemoryNotes(sessionId: string): string[] {
    return withNotesLock(sessionId, () => {
      const inMemory = _memoryNotes.get(sessionId) || [];
      let onDisk: string[] = [];
      const notesPath = getNotesPath(sessionId);
      try {
        onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
      } catch {
        // file may not exist
      }

      const merged = [...new Set([...onDisk, ...inMemory])];
      _memoryNotes.delete(sessionId);
      _memoryNotesTouchedAt.delete(sessionId);
      try { fs.unlinkSync(notesPath); } catch {}
      return merged;
    });
  }

  // -------------------------------------------------------------------------
  // Facade-level recall (wraps orchestrator)
  // -------------------------------------------------------------------------

  async function recall(opts: FacadeRecallOptions): Promise<MemoryResult[]> {
    const {
      query, limit = 10, expandGraph = true, graphDepth = 1,
      datastores, routeStores, reasoning = "fast", intent = "general",
      ranking, domain = { all: true }, domainBoost, project,
      dateFrom, dateTo, docs, datastoreOptions, failOpen,
    } = opts;

    const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
    const shouldRouteStores = routeStores ?? !Array.isArray(datastores);

    const runRecall = (q: string): Promise<MemoryResult[]> => {
      const recallOpts: TotalRecallOptions = {
        datastores: selectedStores,
        expandGraph,
        graphDepth,
        intent,
        ranking: ranking as any,
        domain,
        domainBoost,
        project,
        dateFrom,
        dateTo,
        docs,
        datastoreOptions,
      };
      if (shouldRouteStores) {
        return knowledgeEngine.total_recall(q, limit, {
          ...recallOpts,
          reasoning,
          failOpen,
        });
      }
      return knowledgeEngine.totalRecall(q, limit, recallOpts);
    };

    return runRecall(query);
  }

  // -------------------------------------------------------------------------
  // Stub helper
  // -------------------------------------------------------------------------

  function notImplemented(name: string): never {
    throw new Error(`[quaid][facade] ${name} is not yet implemented — scheduled for a future PR`);
  }

  // -------------------------------------------------------------------------
  // Build and return the facade object
  // -------------------------------------------------------------------------

  return {
    // Pass-through
    getConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    isFailHardEnabled: deps.isFailHardEnabled,

    // Datastore
    stats: () => datastoreBridge.stats(),
    store: (args) => datastoreBridge.store(args),
    forget: (args) => datastoreBridge.forget(args),
    searchBySession: (sessionId, limit = 20) => datastoreBridge.search([
      "*",
      "--session-id",
      sessionId,
      "--owner",
      deps.resolveOwner(),
      "--limit",
      String(limit),
    ]),

    // Events
    emitEvent: (command, args) => deps.execEvents(command, args),

    // Recall
    recall,

    // Dynamic K
    computeDynamicK,
    getActiveNodeCount,

    // Docs
    docsSearch: (query, args) => deps.execDocsRag("search", [query, ...args]),
    docsRead: (identifier) => deps.execDocsRegistry("read", [identifier]),
    docsList: (args) => deps.execDocsRegistry("list", args),
    docsRegister: (args) => deps.execDocsRegistry("register", args),
    docsCreateProject: (args) => deps.execDocsRegistry("create-project", args),
    docsListProjects: (args = ["--json"]) => deps.execDocsRegistry("list-projects", args),
    docsCheckStaleness: () => deps.execDocsUpdater("check", ["--json"]),

    // Memory notes
    addMemoryNote,
    getAndClearMemoryNotes,

    // Project catalog
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    getProjectNames: () => projectCatalogReader.getProjectNames(),

    // Guidance
    renderDatastoreGuidance: renderKnowledgeDatastoreGuidanceForAgents,

    // Stubs
    detectLifecycleSignal: () => notImplemented("detectLifecycleSignal"),
    processLifecycleEvent: () => notImplemented("processLifecycleEvent"),
    maybeRunMaintenance: () => notImplemented("maybeRunMaintenance"),
    getJanitorHealthIssue: () => notImplemented("getJanitorHealthIssue"),
    queueDelayedRequest: () => notImplemented("queueDelayedRequest"),
  };
}
