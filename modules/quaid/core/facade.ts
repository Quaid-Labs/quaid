/**
 * Adapter-facing facade — the single entry point between any adapter and the
 * Quaid core/orchestrator internals.
 *
 * Adapters create a facade via `createQuaidFacade(deps)` and call its methods
 * instead of reaching directly into datastore, ingest, or runtime modules.
 */

import type { PythonBridgeExec } from "./datastore-bridge.js";
import { COMMAND_REGISTRY } from "./command-registry.js";
import { createDatastoreBridge } from "./datastore-bridge.js";
import { createProjectCatalogReader } from "./project-catalog.js";
import { createKnowledgeEngine } from "./knowledge-engine.js";
import type { TotalRecallOptions, RecallMemoryOpts } from "../orchestrator/default-orchestrator.js";
import {
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents,
} from "./knowledge-stores.js";
import type { KnowledgeDatastore, DomainFilter, RecallIntent } from "./knowledge-stores.js";
import { createHash } from "node:crypto";
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
  /** Instance root: QUAID_HOME/QUAID_INSTANCE. Used for identity file injection. */
  instanceRoot?: string;
  pluginRoot: string;
  dbPath: string;
  eventSource?: string;
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
  getDefaultLLMProvider?: () => string;
  adapterName?: string;
  providerAliases?: Record<string, string>;
  getDatastoreStatsSync?: () => Record<string, any> | null;
  initDatastore?: () => void;
  resolveSessionIdFromSessionKey?: (sessionKey: string) => string;
  resolveDefaultSessionId?: () => string;
  resolveMostRecentSessionId?: () => string;
  timeoutSessionStorePath?: () => string;
  timeoutSessionTranscriptDirs?: () => string[];
  readSessionMessagesFile?: (sessionFile: string) => any[];
  listCompactionSessions?: () => Array<{ key: string; sessionId: string }>;
  requestSessionCompaction?: (sessionKey: string) => { ok: boolean; compacted?: unknown; raw?: string };
  getMemoryConfig: () => any;
  isSystemEnabled: (system: "memory" | "journal" | "projects" | "workspace") => boolean;
  isFailHardEnabled: () => boolean;
  /** Optional trace emitter (e.g. writeHookTrace) for diagnostic events. */
  trace?: (event: string, payload?: Record<string, unknown>) => void;
  transcriptFormat?: {
    preprocessText?: (text: string) => string;
    shouldSkipText?: (role: "user" | "assistant", text: string) => boolean;
    speakerLabel?: (role: "user" | "assistant") => string;
  };
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
  viaRelation?: string;
  hopDepth?: number;
  graphPath?: string;
  graphExpansionAnchorId?: string;
  graphExpansionAnchorText?: string;
  graphExpansionAnchorSimilarity?: number;
  graphExpansionAnchorCategory?: string;
  speaker?: string;
};

export type DatastoreStats = {
  total_nodes: number;
  edges: number;
  active_nodes?: number;
};

export type LifecycleSignal = {
  label: "ResetSignal" | "CompactionSignal";
  source: "user_command" | "system_notice" | "hook";
  signature: string;
  messageIndex?: number;
};

export type ExtractionTrigger = "compaction" | "reset" | "new" | "recovery" | "timeout" | "unknown";

export type DelayedRequestInput = {
  message: string;
  kind?: string;
  priority?: string;
  source?: string;
  requestsPath?: string;
};

export type ExtractionPipelineResult = {
  hasMeaningfulUserContent: boolean;
  triggerType: ExtractionTrigger;
  factDetails: Array<{ text: string; status: string; reason?: string; edges?: string[] }>;
  stored: number;
  skipped: number;
  edgesCreated: number;
  snippetDetails: Record<string, string[]>;
  journalDetails: Record<string, string[]>;
};

export type JanitorHealthAlertOptions = {
  statePath: string;
  cooldownMs?: number;
  nowMs?: number;
};

export type AutoInjectionPreparation = {
  prependContext: string;
  toInject: MemoryResult[];
  uniqueSessionId: string;
};

export type RecallDiagnostics = {
  meta: Record<string, unknown> | null;
};

export type JanitorNudgeOptions = {
  statePath: string;
  pendingInstallMigrationPath: string;
  pendingApprovalRequestsPath: string;
  cooldownMs?: number;
  nowMs?: number;
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
  getCaptureTimeoutMinutes: () => number;
  isInternalQuaidSession: (sessionId: unknown) => boolean;
  resolveTierModel: (tier: ModelTier) => { provider: string; model: string };
  resolveOwner: (speaker?: string, channel?: string) => string;
  shouldNotifyFeature: (
    feature: "janitor" | "extraction" | "retrieval",
    detail?: "summary" | "full",
  ) => boolean;
  shouldNotifyProjectCreate: () => boolean;
  isPluginStrictMode: () => boolean;
  isPreInjectionPassEnabled: () => boolean;
  shouldEmitExtractionNotify: (key: string, now?: number) => boolean;
  clearExtractionNotifyHistory: () => void;
  listRecentSessionsFromExtractionLog: (limit?: number) => Array<{
    sessionId: string;
    lastExtractedAt: string;
    messageCount: number;
    label: string;
    topicHint: string;
  }>;
  updateExtractionLog: (sessionId: string, messages: unknown[], label: string) => void;
  getInjectionLogPath: (sessionId: string) => string;
  pruneInjectionLogFiles: () => void;
  initializeDatastoreIfMissing: () => boolean;

  // --- Datastore stats ---
  stats: () => Promise<string>;
  getStatsParsed: () => Promise<DatastoreStats | null>;

  // --- Write / delete ---
  store: (args: string[]) => Promise<string>;
  forget: (args: string[]) => Promise<string>;
  searchBySession: (sessionId: string, limit?: number) => Promise<string>;

  // --- Events ---
  emitEvent: (
    name: string,
    payload: Record<string, unknown>,
    dispatch?: "auto" | "immediate" | "queued",
  ) => Promise<Record<string, unknown>>;

  // --- Recall (routed via orchestrator) ---
  recall: (opts: FacadeRecallOptions) => Promise<MemoryResult[]>;
  recallWithDiagnostics: (opts: FacadeRecallOptions) => Promise<{ results: MemoryResult[]; diagnostics: RecallDiagnostics | null }>;
  recallWithToolRetry: (opts: FacadeRecallOptions) => Promise<MemoryResult[]>;
  formatMemoriesForInjection: (memories: MemoryResult[]) => string;
  formatRecallToolResponse: (results: MemoryResult[]) => {
    text: string;
    breakdown: {
      vector_count: number;
      graph_count: number;
      journal_count: number;
      project_count: number;
    };
  };
  buildRecallNotificationPayload: (
    results: MemoryResult[],
    query: string,
    mode: "tool" | "auto_inject",
    breakdown?: {
      vector_count: number;
      graph_count: number;
      journal_count?: number;
      project_count?: number;
    },
  ) => {
    memories: Array<{
      text: string;
      similarity: number;
      via: string;
      category: string;
    }>;
    source_breakdown: {
      vector_count: number;
      graph_count: number;
      journal_count: number;
      project_count: number;
      query: string;
      mode: "tool" | "auto_inject";
    };
  };
  isLowQualityQuery: (query: string) => boolean;
  filterMemoriesByPrivacy: (memories: MemoryResult[], currentOwner: string) => MemoryResult[];
  loadInjectedMemoryKeys: (sessionId: string) => string[];
  saveInjectedMemoryKeys: (
    sessionId: string,
    previousKeys: string[],
    memories: MemoryResult[],
    maxEntries: number,
  ) => string[];
  resetInjectionDedupAfterCompaction: (sessionId: string) => void;
  queueCompactionExtractionSummary: (
    sessionId: string,
    stored: number,
    skipped: number,
    edges: number,
    notify: (summary: string) => void,
  ) => void;

  // --- Dynamic K ---
  computeDynamicK: () => number;
  getActiveNodeCount: () => number;

  // --- Docs (Python bridge wrapped) ---
  docsRead: (identifier: string) => Promise<string>;
  docsList: (args: string[]) => Promise<string>;
  docsRegister: (args: string[]) => Promise<string>;
  docsCreateProject: (args: string[]) => Promise<string>;
  docsListProjects: (args?: string[]) => Promise<string>;
  docsCheckStaleness: () => Promise<string>;
  getDocsStalenessWarning: () => Promise<string>;
  loadProjectMarkdown: (project: string) => string;
  buildDocsSearchNotificationPayload: (
    query: string,
    results: string,
  ) => {
    query: string;
    results: Array<{ doc: string; section: string; score: number }>;
  };

  // --- Memory notes (state management) ---
  addMemoryNote: (sessionId: string, text: string, category: string) => void;
  hasPendingMemoryNotes: (sessionId: string) => boolean;
  getAndClearMemoryNotes: (sessionId: string) => string[];
  runExtractionPipeline: (
    messages: unknown[],
    label: string,
    sessionId?: string,
  ) => Promise<ExtractionPipelineResult | null>;

  // --- Project catalog ---
  getProjectCatalog: () => Array<{ name: string; description: string }>;
  getProjectNames: () => string[];
  injectProjectContext: (existingContext?: string) => Promise<string | undefined>;

  // --- Guidance rendering ---
  renderDatastoreGuidance: () => string;

  // --- Transcript/message utilities ---
  getMessageText: (message: unknown) => string;
  extractSessionId: (messages: unknown[], ctx?: unknown) => string;
  parseSessionIdFromTranscriptPath: (sessionFile: string) => string;
  readSessionMessagesFile: (sessionFile: string) => unknown[];
  resolveMemoryStoreSessionId: (ctx?: unknown) => string;
  resolveLifecycleHookSessionId: (event: unknown, ctx: unknown, messages: unknown[]) => string;
  readTimeoutSessionMessages: (sessionId: string) => unknown[];
  listTimeoutSessionActivity: () => Array<{ sessionId: string; lastActivityMs: number }>;
  resolveSessionForCompaction: (sessionId?: string) => string | null;
  maybeForceCompactionAfterTimeout: (sessionId?: string) => void;
  filterConversationMessages: (messages: unknown[]) => unknown[];
  buildTranscript: (messages: unknown[]) => string;
  isResetBootstrapOnlyConversation: (messages: unknown[], bootstrapPrompt?: string) => boolean;
  isVectorRecallResult: (result: MemoryResult) => boolean;
  updateDocsFromTranscript: (
    messages: unknown[],
    label: string,
    sessionId?: string,
    tempDir?: string,
  ) => Promise<void>;
  // stageProjectEvent and emitProjectEvent removed — project events are now
  // emitted from extract_from_transcript() in Python (ingest/extract.py).
  // The extraction call already identifies projects and produces summaries,
  // eliminating the redundant summarizeProjectSession LLM call.

  // --- Stubs (typed, not yet implemented) ---
  detectLifecycleSignal: (messages: unknown[]) => LifecycleSignal | null;
  latestMessageTimestampMs: (messages: unknown[]) => number | null;
  hasExplicitLifecycleUserCommand: (messages: unknown[]) => boolean;
  isBacklogLifecycleReplay: (
    messages: unknown[],
    trigger: string,
    nowMs: number,
    bootTimeMs: number,
    staleMs: number,
  ) => boolean;
  shouldProcessLifecycleSignal: (
    sessionId: string,
    signal: LifecycleSignal,
    suppressMs?: number,
    retentionMs?: number,
  ) => boolean;
  markLifecycleSignalFromHook: (sessionId: string, label: "ResetSignal" | "CompactionSignal") => void;
  clearLifecycleSignalHistory: () => void;
  processLifecycleEvent: (signal: unknown, context: unknown) => never;
  maybeRunMaintenance: (sessionId: string) => never;
  getJanitorHealthIssue: () => string | null;
  queueExtraction: (task: () => Promise<void>, source: string) => Promise<void>;
  getQueuedExtractionPromise: () => Promise<void> | null;
  queueDelayedRequest: (request: DelayedRequestInput) => boolean;
  maybeQueueJanitorHealthAlert: (options: JanitorHealthAlertOptions) => boolean;
  collectJanitorNudges: (options: JanitorNudgeOptions) => string[];
  isInternalMaintenancePrompt: (text: string) => boolean;
  resolveExtractionTrigger: (label: string) => ExtractionTrigger;
};

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

const FAST_ROUTER_TIMEOUT_MS = 10_000;
const DEEP_ROUTER_TIMEOUT_MS = 60_000;
const NODE_COUNT_CACHE_MS = 120_000;
const DATASTORE_STATS_TIMEOUT_MS = 30_000;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_MEMORY_NOTE_SESSIONS = 200;
const EXTRACTION_NOTIFY_DEDUPE_MS = 90_000;
const MAX_EXTRACTION_LOG_ENTRIES = 800;
const MAX_INJECTION_LOG_FILES = 400;
const DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS = 50;
const DELAYED_REQUESTS_LOCK_SLEEP_MS = 10;
const COMPACTION_NOTIFY_BATCH_MS = 10_000;
const COMPACTION_NOTIFY_BATCH_MAX_MS = 45_000;
const TOOLS_DOMAIN_BLOCK_RE = /<!-- AUTO-GENERATED:DOMAIN-LIST:START -->[\s\S]*?<!-- AUTO-GENERATED:DOMAIN-LIST:END -->\n*/g;
const RECALL_RETRY_STOPWORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how", "i",
  "in", "is", "it", "me", "my", "of", "on", "or", "our", "that", "the", "their", "they",
  "this", "to", "was", "we", "what", "when", "where", "which", "who", "why", "with", "you", "your",
]);

export function createQuaidFacade(deps: QuaidFacadeDeps): QuaidFacade {
  // -------------------------------------------------------------------------
  // Internal bridges (adapter no longer touches these directly)
  // -------------------------------------------------------------------------

  const datastoreBridge = createDatastoreBridge(deps.execPython);

  const projectCatalogReader = createProjectCatalogReader({
    workspace: deps.workspace,
    fs,
    path,
    getMemoryConfig: deps.getMemoryConfig,
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
    trace: deps.trace,
    getCommandRegistry: () => {
      // Resolve instance name: prefer instanceRoot, fall back to scanning for misc-- dir
      let instanceName = deps.instanceRoot ? path.basename(deps.instanceRoot) : null;
      if (!instanceName) {
        try {
          const projectsDir = path.join(deps.workspace, "shared", "projects");
          const found = fs.readdirSync(projectsDir).find(d => d.startsWith("misc--"));
          if (found) instanceName = found.replace(/^misc--/, "");
        } catch { /* no projects dir */ }
      }
      const miscPath = instanceName
        ? path.join(deps.workspace, "shared", "projects", `misc--${instanceName}`)
        : null;
      return COMMAND_REGISTRY.map(entry => ({
        ...entry,
        hint: miscPath
          ? entry.hint.replace(/\{misc_path\}/g, miscPath).replace(/\{instance\}/g, instanceName!)
          : entry.hint,
      }));
    },
    recallMemory: async (query, limit, opts) => {
      return recallMemoryFromBridge(query, limit, opts as RecallMemoryOpts);
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
  const lifecycleSignalHistory = new Map<string, {
    source: "user_command" | "system_notice" | "hook";
    signature: string;
    seenAt: number;
  }>();
  const extractionNotifyHistory = new Map<string, number>();
  let compactionNotifyBatchState: {
    startedAtMs: number;
    lastUpdateMs: number;
    sessions: Set<string>;
    sessionsWithFacts: Set<string>;
    stored: number;
    skipped: number;
    edges: number;
    timer: NodeJS.Timeout | null;
  } | null = null;
  let extractionPromise: Promise<void> | null = null;
  let timeoutSessionStoreCache:
    | { path: string; mtimeMs: number; data: Record<string, any> }
    | null = null;

  function queueExtraction(task: () => Promise<void>, source: string): Promise<void> {
    const prior = extractionPromise || Promise.resolve();
    extractionPromise = prior.then(
      () => task(),
      async (err: unknown) => {
        const msg = (err as Error)?.message || String(err);
        console.error(`[quaid][facade] extraction chain prior failure (${source}): ${msg}`);
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        await task();
        return;
      },
    );
    return extractionPromise;
  }

  function getQueuedExtractionPromise(): Promise<void> | null {
    return extractionPromise;
  }

  function initializeDatastoreIfMissing(): boolean {
    const dataDir = path.dirname(deps.dbPath);
    fs.mkdirSync(dataDir, { recursive: true });
    if (fs.existsSync(deps.dbPath)) {
      return false;
    }
    const initDatastore = deps.initDatastore;
    if (typeof initDatastore !== "function") {
      const msg = "[quaid][facade] datastore initialization callback is not configured";
      if (deps.isFailHardEnabled()) {
        throw new Error(msg);
      }
      console.warn(msg);
      return false;
    }
    initDatastore();
    return true;
  }

  function resolveOwner(speaker?: string, channel?: string): string {
    const usersCfg = deps.getMemoryConfig()?.users;
    const config = usersCfg && typeof usersCfg === "object" && !Array.isArray(usersCfg)
      ? usersCfg as Record<string, any>
      : { defaultOwner: "quaid", identities: {} };
    const identities = config.identities && typeof config.identities === "object" && !Array.isArray(config.identities)
      ? config.identities as Record<string, any>
      : {};
    const defaultOwner = typeof config.defaultOwner === "string" && config.defaultOwner.trim()
      ? config.defaultOwner.trim()
      : "quaid";

    for (const [userId, identity] of Object.entries(identities)) {
      if (!identity || typeof identity !== "object") continue;
      const speakers = Array.isArray(identity.speakers) ? identity.speakers : [];
      if (speaker && speakers.some((s: unknown) => String(s || "").toLowerCase() === speaker.toLowerCase())) {
        return userId;
      }

      const channels = identity.channels && typeof identity.channels === "object"
        ? identity.channels as Record<string, unknown>
        : {};
      if (channel && Array.isArray(channels[channel])) {
        const allowed = channels[channel] as unknown[];
        if (allowed.some((a: unknown) => String(a || "") === "*")) return userId;
        if (speaker && allowed.some((a: unknown) => String(a || "").toLowerCase() === speaker.toLowerCase())) {
          return userId;
        }
      }
    }
    return defaultOwner;
  }

  function getCaptureTimeoutMinutes(): number {
    const capture = deps.getMemoryConfig().capture || {};
    const raw = capture.inactivityTimeoutMinutes ?? capture.inactivity_timeout_minutes ?? 120;
    const num = Number(raw);
    return Number.isFinite(num) ? Math.max(0, num) : 120;
  }

  function isInternalQuaidSession(sessionId: unknown): boolean {
    const sid = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!sid) return false;
    return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
  }

  function normalizeProvider(provider: string): string {
    return String(provider || "").trim().toLowerCase();
  }

  function providerClassLookupKey(provider: string): string {
    const normalized = normalizeProvider(provider);
    const aliases = (deps.providerAliases && typeof deps.providerAliases === "object")
      ? deps.providerAliases
      : {};
    const mapped = aliases[normalized];
    if (typeof mapped === "string" && mapped.trim()) {
      return normalizeProvider(mapped);
    }
    return normalized;
  }

  function getConfiguredTierValue(tier: ModelTier): string {
    const key = tier === "fast" ? "fastReasoning" : "deepReasoning";
    const configured = deps.getMemoryConfig().models?.[key];
    if (typeof configured === "string" && configured.trim().length > 0) {
      return configured.trim();
    }
    throw new Error(`Missing models.${key} in memory config`);
  }

  function getConfiguredTierProvider(tier: ModelTier): string {
    const key = tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider";
    const configured = deps.getMemoryConfig().models?.[key];
    if (typeof configured === "string" && configured.trim().length > 0) {
      return normalizeProvider(configured.trim());
    }
    return "default";
  }

  function parseTierModelClassMap(tier: ModelTier): Record<string, string> {
    const models = deps.getMemoryConfig().models || {};
    const raw = tier === "fast" ? models.fastReasoningModelClasses : models.deepReasoningModelClasses;
    const out: Record<string, string> = {};
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      return out;
    }
    for (const [provider, model] of Object.entries(raw)) {
      const key = providerClassLookupKey(String(provider || "").trim());
      const value = String(model || "").trim();
      if (key && value) {
        out[key] = value;
      }
    }
    return out;
  }

  function getDefaultLLMProvider(): string {
    try {
      return normalizeProvider(String(deps.getDefaultLLMProvider?.() || ""));
    } catch (err: unknown) {
      console.warn(`[quaid][facade] default provider callback failed: ${String((err as Error)?.message || err)}`);
      return "";
    }
  }

  function getEffectiveProvider(): string {
    const configuredProvider = normalizeProvider(String(deps.getMemoryConfig().models?.llmProvider || ""));
    if (configuredProvider && configuredProvider !== "default") {
      return configuredProvider;
    }
    const defaultProvider = getDefaultLLMProvider();
    if (defaultProvider) {
      return defaultProvider;
    }
    throw new Error(
      "models.llmProvider is 'default' but no active default provider was resolved. " +
      "Set models.llmProvider explicitly (anthropic/openai/openai-compatible/claude-code).",
    );
  }

  function getEffectiveTierProvider(tier: ModelTier): string {
    const tierProvider = getConfiguredTierProvider(tier);
    if (tierProvider && tierProvider !== "default") {
      return tierProvider;
    }
    return getEffectiveProvider();
  }

  function resolveTierModel(tier: ModelTier): { provider: string; model: string } {
    const rawTierValue = getConfiguredTierValue(tier);
    const configuredTierProvider = getConfiguredTierProvider(tier);
    const effectiveTierProvider = getEffectiveTierProvider(tier);
    if (rawTierValue !== "default") {
      if (rawTierValue.includes("/")) {
        const [provider, ...modelParts] = rawTierValue.split("/");
        const normalizedProvider = normalizeProvider(provider);
        if (
          configuredTierProvider !== "default"
          && providerClassLookupKey(normalizedProvider) !== providerClassLookupKey(configuredTierProvider)
        ) {
          throw new Error(
            `models.${tier === "fast" ? "fastReasoning" : "deepReasoning"} provider "${normalizedProvider}" does not match models.${tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider"}="${configuredTierProvider}"`,
          );
        }
        return {
          provider: normalizedProvider,
          model: modelParts.join("/").trim(),
        };
      }
      if (!effectiveTierProvider) {
        throw new Error(`Cannot resolve provider for models.${tier === "fast" ? "fastReasoning" : "deepReasoning"}=${rawTierValue}`);
      }
      return { provider: effectiveTierProvider, model: rawTierValue };
    }

    if (!effectiveTierProvider) {
      throw new Error(`No provider resolved for default ${tier} reasoning model`);
    }

    const classMap = parseTierModelClassMap(tier);
    const mappedModel = classMap[providerClassLookupKey(effectiveTierProvider)];
    if (!mappedModel) {
      throw new Error(`No ${tier}ReasoningModelClasses entry for provider "${effectiveTierProvider}" while using default ${tier} reasoning model`);
    }
    return {
      provider: effectiveTierProvider,
      model: mappedModel,
    };
  }

  function effectiveNotificationLevel(feature: "janitor" | "extraction" | "retrieval"): string {
    const notifications = deps.getMemoryConfig().notifications || {};
    const featureConfig = notifications[feature];
    if (featureConfig && typeof featureConfig === "object" && typeof featureConfig.verbosity === "string") {
      return featureConfig.verbosity.trim().toLowerCase();
    }
    const level = String(notifications.level || "normal").trim().toLowerCase();
    const defaults: Record<string, Record<string, string>> = {
      quiet: { janitor: "off", extraction: "off", retrieval: "off" },
      normal: { janitor: "summary", extraction: "summary", retrieval: "off" },
      verbose: { janitor: "full", extraction: "summary", retrieval: "summary" },
      debug: { janitor: "full", extraction: "full", retrieval: "full" },
    };
    const levelDefaults = defaults[level] || defaults.normal;
    return String(levelDefaults[feature] || "off").toLowerCase();
  }

  function shouldNotifyFeature(feature: "janitor" | "extraction" | "retrieval", detail: "summary" | "full" = "summary"): boolean {
    const effective = effectiveNotificationLevel(feature);
    if (effective === "off") return false;
    if (detail === "summary") return effective === "summary" || effective === "full";
    return effective === "full";
  }

  function shouldNotifyProjectCreate(): boolean {
    const notifications = deps.getMemoryConfig().notifications || {};
    const snake = notifications.project_create;
    if (snake && typeof snake === "object" && typeof snake.enabled === "boolean") {
      return snake.enabled;
    }
    const camel = notifications.projectCreate;
    if (camel && typeof camel === "object" && typeof camel.enabled === "boolean") {
      return camel.enabled;
    }
    return true;
  }

  function isPluginStrictMode(): boolean {
    const plugins = deps.getMemoryConfig().plugins || {};
    const raw = (plugins as any).strict;
    if (raw === undefined) return true;
    if (raw === null) return false;
    if (typeof raw === "number") return raw !== 0;
    if (typeof raw === "string") return raw.length > 0;
    if (Array.isArray(raw)) return raw.length > 0;
    if (typeof raw === "object") return Object.keys(raw).length > 0;
    return !!raw;
  }

  function isPreInjectionPassEnabled(): boolean {
    const retrieval = deps.getMemoryConfig().retrieval || {};
    if (typeof (retrieval as any).preInjectionPass === "boolean") return (retrieval as any).preInjectionPass;
    if (typeof (retrieval as any).pre_injection_pass === "boolean") return (retrieval as any).pre_injection_pass;
    return true;
  }

  function shouldEmitExtractionNotify(key: string, now: number = Date.now()): boolean {
    for (const [k, ts] of extractionNotifyHistory.entries()) {
      if ((now - ts) > EXTRACTION_NOTIFY_DEDUPE_MS) {
        extractionNotifyHistory.delete(k);
      }
    }
    const prior = extractionNotifyHistory.get(key);
    extractionNotifyHistory.set(key, now);
    if (!prior) return true;
    return (now - prior) > EXTRACTION_NOTIFY_DEDUPE_MS;
  }

  function isMissingFileError(err: unknown): boolean {
    const code = (err as NodeJS.ErrnoException | undefined)?.code;
    return code === "ENOENT" || code === "ENOTDIR";
  }

  function trimExtractionLogEntries(log: Record<string, any>, maxEntries: number = MAX_EXTRACTION_LOG_ENTRIES): Record<string, any> {
    const entries = Object.entries(log || {});
    if (entries.length <= maxEntries) {
      return log || {};
    }
    const sorted = entries
      .map(([sid, payload]) => ({
        sid,
        payload,
        ts: Date.parse(String((payload as any)?.last_extracted_at || "")) || 0,
      }))
      .sort((a, b) => b.ts - a.ts)
      .slice(0, maxEntries);
    return Object.fromEntries(sorted.map((row) => [row.sid, row.payload]));
  }

  function listRecentSessionsFromExtractionLog(limit: number = 5): Array<{
    sessionId: string;
    lastExtractedAt: string;
    messageCount: number;
    label: string;
    topicHint: string;
  }> {
    const extractionLogPath = path.join(deps.workspace, "data", "extraction-log.json");
    let extractionLog: Record<string, any> = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("extraction log must be a JSON object");
      }
      extractionLog = parsed as Record<string, any>;
    } catch (err: unknown) {
      if (deps.isFailHardEnabled() && !isMissingFileError(err)) {
        throw new Error("[quaid][facade] extraction log read failed under failHard", { cause: err as Error });
      }
      console.warn(`[quaid][facade] extraction log read failed: ${String((err as Error)?.message || err)}`);
      return [];
    }
    return Object.entries(extractionLog)
      .filter(([, v]) => v && v.last_extracted_at)
      .sort(([, a], [, b]) => (b.last_extracted_at || "").localeCompare(a.last_extracted_at || ""))
      .slice(0, Math.min(Math.max(Math.floor(Number(limit) || 5), 1), 20))
      .map(([sessionId, info]) => ({
        sessionId,
        lastExtractedAt: String((info as any).last_extracted_at || ""),
        messageCount: Number((info as any).message_count || 0),
        label: String((info as any).label || "unknown"),
        topicHint: String((info as any).topic_hint || ""),
      }));
  }

  function updateExtractionLog(sessionId: string, messages: unknown[], label: string): void {
    const extractionLogPath = path.join(deps.workspace, "data", "extraction-log.json");
    let extractionLog: Record<string, any> = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("extraction log must be a JSON object");
      }
      extractionLog = parsed as Record<string, any>;
    } catch (err: unknown) {
      if (deps.isFailHardEnabled() && !isMissingFileError(err)) {
        throw new Error("[quaid][facade] extraction log read failed under failHard", { cause: err as Error });
      }
      console.warn(`[quaid][facade] extraction log read failed: ${String((err as Error)?.message || err)}`);
    }

    let topicHint = "";
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      if (String((msg as Record<string, unknown>).role || "") !== "user") continue;
      const cleaned = getMessageText(msg).trim();
      if (shouldSkipUserText(cleaned)) continue;
      topicHint = cleaned.slice(0, 120);
      break;
    }

    extractionLog[String(sessionId || "unknown")] = {
      last_extracted_at: new Date().toISOString(),
      message_count: Array.isArray(messages) ? messages.length : 0,
      label,
      topic_hint: topicHint,
    };
    const trimmed = trimExtractionLogEntries(extractionLog, MAX_EXTRACTION_LOG_ENTRIES);
    fs.writeFileSync(extractionLogPath, JSON.stringify(trimmed, null, 2), { mode: 0o600 });
  }

  const INJECTION_LOG_DIR = path.join(deps.workspace, ".quaid", "runtime", "injection");

  function getInjectionLogPath(sessionId: string): string {
    return path.join(INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
  }

  function pruneInjectionLogFiles(): void {
    try {
      const files = fs.readdirSync(INJECTION_LOG_DIR)
        .filter((f: string) => f.startsWith("memory-injection-") && f.endsWith(".log"))
        .map((f: string) => ({
          full: path.join(INJECTION_LOG_DIR, f),
          mtimeMs: fs.statSync(path.join(INJECTION_LOG_DIR, f)).mtimeMs,
        }))
        .sort((a, b) => b.mtimeMs - a.mtimeMs);

      for (const stale of files.slice(MAX_INJECTION_LOG_FILES)) {
        try {
          fs.unlinkSync(stale.full);
        } catch (err: unknown) {
          console.warn(`[quaid][facade] Failed pruning stale injection log ${stale.full}: ${String((err as Error)?.message || err)}`);
        }
      }
    } catch (err: unknown) {
      if (isMissingFileError(err)) {
        return;
      }
      console.warn(`[quaid][facade] Injection log pruning failed: ${String((err as Error)?.message || err)}`);
    }
  }

  type DelayedRequestItem = {
    id: string;
    created_at: string;
    source: string;
    kind: string;
    priority: string;
    status: "pending" | "resolved";
    message: string;
    resolved_at?: string;
    resolution_note?: string;
  };

  type DelayedRequestsPayload = {
    version: number;
    requests: DelayedRequestItem[];
  };

  function readDelayedRequestsJson(pathname: string): unknown {
    try {
      if (!fs.existsSync(pathname)) return null;
      return JSON.parse(fs.readFileSync(pathname, "utf8"));
    } catch (err: unknown) {
      console.warn(`[quaid][facade] delayed requests read failed path=${pathname}: ${String((err as Error)?.message || err)}`);
      return null;
    }
  }

  function writeDelayedRequestsJson(pathname: string, payload: unknown): void {
    const tmpPath = `${pathname}.tmp-${process.pid}-${Date.now()}`;
    fs.mkdirSync(path.dirname(pathname), { recursive: true });
    fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), { mode: 0o600 });
    fs.renameSync(tmpPath, pathname);
  }

  function withDelayedRequestsLock<T>(requestsPath: string, fn: () => T): T {
    const lockPath = `${requestsPath}.lock`;
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    let fd: number | undefined;
    let lastErr: unknown;
    for (let attempt = 0; attempt < DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS; attempt += 1) {
      try {
        fd = fs.openSync(lockPath, "wx", 0o600);
        break;
      } catch (err: unknown) {
        const code = (err as NodeJS.ErrnoException)?.code;
        if (code !== "EEXIST") throw err;
        lastErr = err;
        _sleepMs(DELAYED_REQUESTS_LOCK_SLEEP_MS);
      }
    }
    if (fd === undefined) {
      throw new Error(`failed to acquire delayed-requests lock: ${String((lastErr as Error)?.message || lastErr)}`);
    }
    try {
      return fn();
    } finally {
      try { fs.closeSync(fd); } catch {}
      try { fs.unlinkSync(lockPath); } catch {}
    }
  }

  function makeDelayedRequestId(kind: string, message: string): string {
    return `${kind}-${Buffer.from(message).toString("base64").slice(0, 16)}`;
  }

  function queueDelayedRequest(request: DelayedRequestInput): boolean {
    const requestsPath = String(
      request?.requestsPath || path.join(deps.workspace, ".quaid", "runtime", "notes", "delayed-llm-requests.json"),
    );
    const message = String(request?.message || "").trim();
    const kind = String(request?.kind || "janitor");
    const priority = String(request?.priority || "normal");
    const source = String(request?.source || deps.adapterName || "adapter");

    if (!message) return false;

    try {
      return withDelayedRequestsLock(requestsPath, () => {
        const loaded = readDelayedRequestsJson(requestsPath);
        if (loaded === null && deps.isFailHardEnabled() && fs.existsSync(requestsPath)) {
          throw new Error(`delayed requests file is unreadable or malformed: ${requestsPath}`);
        }
        const payload = (loaded && typeof loaded === "object" && !Array.isArray(loaded)
          ? loaded
          : { version: 1, requests: [] }) as DelayedRequestsPayload;
        const requests = Array.isArray(payload.requests) ? payload.requests : [];
        const id = makeDelayedRequestId(kind, message);
        if (requests.some((r) => r && String(r.id || "") === id && r.status === "pending")) {
          return false;
        }
        requests.push({
          id,
          created_at: new Date().toISOString(),
          source,
          kind,
          priority,
          status: "pending",
          message,
        });
        payload.version = 1;
        payload.requests = requests;
        writeDelayedRequestsJson(requestsPath, payload);
        return true;
      });
    } catch (err: unknown) {
      const detail = `[quaid][facade] delayed requests queue failed path=${requestsPath}: ${String((err as Error)?.message || err)}`;
      if (deps.isFailHardEnabled()) {
        const cause = err instanceof Error ? err : new Error(String(err));
        throw new Error(detail, { cause });
      }
      console.warn(detail);
      return false;
    }
  }

  function readObjectFile(filePath: string): Record<string, any> {
    try {
      if (!fs.existsSync(filePath)) {
        return {};
      }
      const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return {};
      }
      return parsed as Record<string, any>;
    } catch (err: unknown) {
      console.warn(`[quaid][facade] failed reading JSON state ${filePath}: ${String((err as Error)?.message || err)}`);
      return {};
    }
  }

  function writeObjectFile(filePath: string, state: Record<string, any>): void {
    try {
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      fs.writeFileSync(filePath, JSON.stringify(state, null, 2), { mode: 0o600 });
    } catch (err: unknown) {
      console.warn(`[quaid][facade] failed writing JSON state ${filePath}: ${String((err as Error)?.message || err)}`);
    }
  }

  function getDatastoreStatsSync(maxAgeMs: number = NODE_COUNT_CACHE_MS): Record<string, any> | null {
    const now = Date.now();
    if ((now - _datastoreStatsTimestamp) < maxAgeMs) {
      return _cachedDatastoreStats;
    }
    const readStats = deps.getDatastoreStatsSync;
    if (typeof readStats !== "function") {
      _cachedDatastoreStats = null;
      _datastoreStatsTimestamp = now;
      return null;
    }
    try {
      const parsed = readStats();
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
    let stats: Record<string, any> | null = null;
    try {
      stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
    } catch (err: unknown) {
      console.warn(`[quaid][facade] active node stats probe failed; using fallback=100: ${String((err as Error)?.message || err)}`);
    }
    const active = Number(stats?.active_nodes ?? stats?.by_status?.active ?? 0);
    if (Number.isFinite(active) && active > 0) {
      _cachedNodeCount = active;
      _nodeCountTimestamp = now;
      return _cachedNodeCount;
    }
    if (_cachedNodeCount === null) {
      // Dynamic K is a heuristic; fallback is safer than failing tool calls.
      console.warn("[quaid][facade] unable to derive active node count; using fallback=100");
    }
    return _cachedNodeCount ?? 100;
  }

  function getJanitorHealthIssue(): string | null {
    const stats = getDatastoreStatsSync(60 * 1000);
    const completedAt = String(stats?.last_janitor_completed_at || "").trim();
    if (!completedAt) {
      return "[Quaid] Janitor has never run. Please run janitor and ensure schedule is active.";
    }
    const ts = Date.parse(completedAt);
    if (Number.isNaN(ts)) return null;
    const hours = (Date.now() - ts) / (1000 * 60 * 60);
    if (hours > 72) {
      return `[Quaid] Janitor appears unhealthy (last successful run ${Math.floor(hours)}h ago). Diagnose scheduler/run path and run janitor.`;
    }
    if (hours > 48) {
      return `[Quaid] Janitor may be delayed (last successful run ${Math.floor(hours)}h ago). Verify schedule and run status.`;
    }
    return null;
  }

  function maybeQueueJanitorHealthAlert(options: JanitorHealthAlertOptions): boolean {
    const issue = getJanitorHealthIssue();
    if (!issue) return false;
    const now = Number(options?.nowMs || Date.now());
    const cooldown = Math.max(1, Number(options?.cooldownMs || 6 * 60 * 60 * 1000));
    const statePath = String(options?.statePath || "").trim();
    if (!statePath) return false;
    const state = readObjectFile(statePath);
    const lastAt = Number(state.lastJanitorHealthAlertAt || 0);
    if (now - lastAt < cooldown && String(state.lastJanitorHealthIssue || "") === issue) {
      return false;
    }
    const queued = queueDelayedRequest({
      message: issue,
      kind: "janitor_health",
      priority: "high",
      source: String(deps.adapterName || "adapter"),
    });
    if (!queued) return false;
    state.lastJanitorHealthAlertAt = now;
    state.lastJanitorHealthIssue = issue;
    writeObjectFile(statePath, state);
    return true;
  }

  function collectJanitorNudges(options: JanitorNudgeOptions): string[] {
    const now = Number(options?.nowMs || Date.now());
    const cooldown = Math.max(1, Number(options?.cooldownMs || 6 * 60 * 60 * 1000));
    const statePath = String(options?.statePath || "").trim();
    if (!statePath) return [];
    const state = readObjectFile(statePath);
    const nudges: string[] = [];
    let changed = false;

    try {
      if (fs.existsSync(options.pendingInstallMigrationPath)) {
        const raw = readObjectFile(options.pendingInstallMigrationPath);
        const lastInstallNudge = Number(state.lastInstallNudgeAt || 0);
        if (raw?.status === "pending" && now - lastInstallNudge > cooldown) {
          nudges.push("Hey, I see you just installed Quaid. Want me to help migrate important context into managed memory now?");
          state.lastInstallNudgeAt = now;
          changed = true;
        }
      }
    } catch (err: unknown) {
      console.warn(`[quaid][facade] install nudge check failed: ${String((err as Error)?.message || err)}`);
    }

    try {
      if (fs.existsSync(options.pendingApprovalRequestsPath)) {
        const raw = readObjectFile(options.pendingApprovalRequestsPath);
        const requests = Array.isArray(raw?.requests) ? raw.requests : [];
        const pendingCount = requests.filter((r: any) => r?.status === "pending").length;
        const lastApprovalNudge = Number(state.lastApprovalNudgeAt || 0);
        if (pendingCount > 0 && now - lastApprovalNudge > cooldown) {
          nudges.push(`Quaid has ${pendingCount} pending approval request(s). Review pending maintenance approvals.`);
          state.lastApprovalNudgeAt = now;
          changed = true;
        }
      }
    } catch (err: unknown) {
      console.warn(`[quaid][facade] approval nudge check failed: ${String((err as Error)?.message || err)}`);
    }

    if (changed) {
      writeObjectFile(statePath, state);
    }
    return nudges;
  }

  function parseDatastoreStats(raw: string): DatastoreStats | null {
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(raw || "{}");
    } catch {
      return null;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    const totalNodes = Number((parsed as Record<string, unknown>).total_nodes);
    const edges = Number((parsed as Record<string, unknown>).edges);
    const activeNodes = Number((parsed as Record<string, unknown>).active_nodes);
    if (!Number.isFinite(totalNodes) || totalNodes < 0) {
      return null;
    }
    if (!Number.isFinite(edges) || edges < 0) {
      return null;
    }
    return {
      total_nodes: totalNodes,
      edges,
      ...(Number.isFinite(activeNodes) && activeNodes >= 0 ? { active_nodes: activeNodes } : {}),
    };
  }

  async function getStatsParsed(): Promise<DatastoreStats | null> {
    try {
      const output = await datastoreBridge.stats();
      return parseDatastoreStats(output);
    } catch (err: unknown) {
      console.error("[quaid][facade] stats error:", (err as Error).message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      return null;
    }
  }

  function getMessageText(msg: unknown): string {
    if (!msg || typeof msg !== "object") return "";
    const candidate = (msg as Record<string, unknown>).content
      ?? (msg as Record<string, unknown>).text
      ?? (msg as Record<string, unknown>).message;
    if (typeof candidate === "string") return candidate;
    if (Array.isArray(candidate)) {
      return candidate
        .map((part) => {
          if (!part || typeof part !== "object") return "";
          const text = (part as Record<string, unknown>).text;
          return typeof text === "string" ? text : "";
        })
        .join(" ");
    }
    return "";
  }

  function shouldSkipUserText(text: string): boolean {
    const candidate = String(text || "");
    if (!candidate.trim()) return true;
    const skip = deps.transcriptFormat?.shouldSkipText;
    if (typeof skip === "function") {
      try {
        return skip("user", candidate);
      } catch (err: unknown) {
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][facade] transcript skip callback failed: ${String((err as Error)?.message || err)}`);
      }
    }
    if (candidate.includes('"kind": "restart"')) return true;
    return false;
  }

  function extractSessionId(messages: unknown[], ctx?: unknown): string {
    const context = ctx && typeof ctx === "object"
      ? ctx as Record<string, unknown>
      : {};
    const direct = String(context.sessionId || "").trim();
    if (direct) {
      return direct;
    }
    let firstTimestamp = "";
    const filteredMessages = Array.isArray(messages)
      ? messages.filter((msg: unknown) => {
        if (!msg || typeof msg !== "object") return false;
        if (String((msg as Record<string, unknown>).role || "") !== "user") return false;
        const content = getMessageText(msg).trim();
        return !shouldSkipUserText(content);
      })
      : [];
    if (filteredMessages.length > 0) {
      const firstMessage = filteredMessages[0] as Record<string, unknown>;
      const rawTs = firstMessage.timestamp;
      firstTimestamp = rawTs ? String(rawTs) : Date.now().toString();
    } else {
      firstTimestamp = Date.now().toString();
    }
    return createHash("md5").update(firstTimestamp).digest("hex").substring(0, 12);
  }

  function parseSessionIdFromTranscriptPath(sessionFile: string): string {
    const base = path.basename(String(sessionFile || ""));
    const match = base.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    if (match) {
      return match[0].toLowerCase();
    }
    const stem = base.replace(/\.jsonl$/i, "").trim();
    if (!stem || /\s/.test(stem)) {
      return "";
    }
    return stem.toLowerCase();
  }

  function resolveMemoryStoreSessionId(ctx?: unknown): string {
    const context = ctx && typeof ctx === "object"
      ? ctx as Record<string, unknown>
      : {};
    const direct = String(context.sessionId || "").trim();
    if (direct) {
      return direct;
    }
    const fromKey = deps.resolveSessionIdFromSessionKey?.(String(context.sessionKey || "")) || "";
    if (fromKey) {
      return fromKey;
    }
    const mainFallback = deps.resolveDefaultSessionId?.() || "";
    if (mainFallback) {
      return mainFallback;
    }
    const recentFallback = deps.resolveMostRecentSessionId?.() || "";
    if (recentFallback) {
      return recentFallback;
    }
    return "unknown";
  }

  function resolveLifecycleHookSessionId(event: unknown, ctx: unknown, messages: unknown[]): string {
    const eventObj = (event && typeof event === "object") ? event as Record<string, unknown> : {};
    const context = (ctx && typeof ctx === "object") ? ctx as Record<string, unknown> : {};

    // Prefer sessions.json key lookup — it maps OC session keys to transcript file UUIDs,
    // which are authoritative. Some OC versions emit a different sessionId in hook events
    // than the UUID used for the transcript filename; sessions.json is always correct.
    const eventSessionKey = String(eventObj.sessionKey || eventObj.targetSessionKey || "").trim();
    const fromEventKey = deps.resolveSessionIdFromSessionKey?.(eventSessionKey) || "";
    if (fromEventKey) {
      return fromEventKey;
    }
    const ctxSessionKey = String(context.sessionKey || "").trim();
    const fromCtxKey = deps.resolveSessionIdFromSessionKey?.(ctxSessionKey) || "";
    if (fromCtxKey) {
      return fromCtxKey;
    }

    // Fall back to direct sessionId from event (may differ from transcript UUID in some OC builds)
    const direct = String(eventObj.sessionId || context.sessionId || "").trim();
    if (direct) {
      return direct;
    }
    const fromEventEntry = String(
      (eventObj.sessionEntry as Record<string, unknown> | undefined)?.sessionId
      || (eventObj.previousSessionEntry as Record<string, unknown> | undefined)?.sessionId
      || "",
    ).trim();
    if (fromEventEntry) {
      return fromEventEntry;
    }
    return extractSessionId(messages, ctx);
  }

  function readMessagesFromSessionJsonl(sessionFile: string): any[] {
    const content = fs.readFileSync(sessionFile, "utf8");
    const lines = content.trim().split("\n");
    const messages: any[] = [];
    for (const line of lines) {
      try {
        const entry = JSON.parse(line);
        if (entry.type === "message" && entry.message) {
          messages.push(entry.message);
        } else if (entry.role) {
          messages.push(entry);
        }
      } catch (err: unknown) {
        console.warn(`[quaid][facade] session JSONL parse failed: ${String((err as Error)?.message || err)}`);
      }
    }
    return messages;
  }

  function readTimeoutSessionStore(): Record<string, any> {
    const storePath = String(deps.timeoutSessionStorePath?.() || "").trim();
    if (!storePath) return {};
    try {
      if (!fs.existsSync(storePath)) return {};
      const stat = fs.statSync(storePath);
      const mtimeMs = Number.isFinite(stat.mtimeMs) ? stat.mtimeMs : 0;
      if (
        timeoutSessionStoreCache
        && timeoutSessionStoreCache.path === storePath
        && timeoutSessionStoreCache.mtimeMs === mtimeMs
      ) {
        return timeoutSessionStoreCache.data;
      }
      const raw = JSON.parse(fs.readFileSync(storePath, "utf8"));
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
      const data = raw as Record<string, any>;
      timeoutSessionStoreCache = { path: storePath, mtimeMs, data };
      return data;
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid][timeout] session store read failed: ${String((err as Error)?.message || err)}`);
      return {};
    }
  }

  function parseTimeoutSessionUpdatedAtMs(entry: any): number | null {
    const candidates = [entry?.updatedAt, entry?.updated_at, entry?.lastMessageAt, entry?.last_message_at];
    for (const raw of candidates) {
      if (typeof raw === "number" && Number.isFinite(raw)) return raw;
      if (typeof raw === "string") {
        const asNum = Number(raw);
        if (Number.isFinite(asNum)) return asNum;
        const parsed = Date.parse(raw);
        if (Number.isFinite(parsed)) return parsed;
      }
    }
    return null;
  }

  function resolveTimeoutSessionTranscriptPath(entry: any, sessionId: string): string | null {
    const pathCandidates = [entry?.sessionFile, entry?.session_file];
    for (const raw of pathCandidates) {
      const p = String(raw || "").trim();
      if (!p) continue;
      if (fs.existsSync(p)) return p;
    }
    const fallbackDirs = deps.timeoutSessionTranscriptDirs?.() || [];
    for (const dirRaw of fallbackDirs) {
      const dir = String(dirRaw || "").trim();
      if (!dir) continue;
      const candidate = path.join(dir, `${sessionId}.jsonl`);
      if (fs.existsSync(candidate)) return candidate;
    }
    return null;
  }

  function readTimeoutSessionMessages(sessionId: string): unknown[] {
    const sid = String(sessionId || "").trim();
    if (!sid) return [];
    const store = readTimeoutSessionStore();
    const entries = Object.entries(store || {}) as Array<[string, any]>;
    for (const [, entry] of entries) {
      if (String(entry?.sessionId || "").trim() !== sid) continue;
      const transcriptPath = resolveTimeoutSessionTranscriptPath(entry, sid);
      if (!transcriptPath) {
        console.log(`[quaid][timeout][source] session=${sid} transcript=none store_entry=true`);
        return [];
      }
      const rows = deps.readSessionMessagesFile?.(transcriptPath) || readMessagesFromSessionJsonl(transcriptPath);
      const count = Array.isArray(rows) ? rows.length : 0;
      console.log(`[quaid][timeout][source] session=${sid} transcript=${transcriptPath} store_entry=true rows=${count}`);
      return rows;
    }
    const fallbackPath = resolveTimeoutSessionTranscriptPath({}, sid);
    if (!fallbackPath) {
      console.log(`[quaid][timeout][source] session=${sid} transcript=none store_entry=false`);
      return [];
    }
    const rows = deps.readSessionMessagesFile?.(fallbackPath) || readMessagesFromSessionJsonl(fallbackPath);
    const count = Array.isArray(rows) ? rows.length : 0;
    console.log(`[quaid][timeout][source] session=${sid} transcript=${fallbackPath} store_entry=false rows=${count}`);
    return rows;
  }

  function listTimeoutSessionActivity(): Array<{ sessionId: string; lastActivityMs: number }> {
    const store = readTimeoutSessionStore();
    const rows: Array<{ sessionId: string; lastActivityMs: number }> = [];
    const seen = new Set<string>();
    const entries = Object.entries(store || {}) as Array<[string, any]>;
    for (const [, entry] of entries) {
      const sid = String(entry?.sessionId || "").trim();
      if (!sid) continue;
      const updatedAtMs = parseTimeoutSessionUpdatedAtMs(entry);
      if (updatedAtMs !== null) {
        rows.push({ sessionId: sid, lastActivityMs: updatedAtMs });
        seen.add(sid);
        continue;
      }
      const transcriptPath = resolveTimeoutSessionTranscriptPath(entry, sid);
      if (!transcriptPath) continue;
      try {
        const stat = fs.statSync(transcriptPath);
        if (Number.isFinite(stat.mtimeMs) && stat.mtimeMs > 0) {
          rows.push({ sessionId: sid, lastActivityMs: stat.mtimeMs });
          seen.add(sid);
        }
      } catch (err: unknown) {
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][timeout] session mtime read failed for ${sid}: ${String((err as Error)?.message || err)}`);
      }
    }
    // Fallback for sessions that are present as transcript JSONL files but absent
    // from sessions.json (observed in some OpenClaw pathways with explicit session IDs).
    for (const dirRaw of deps.timeoutSessionTranscriptDirs?.() || []) {
      const dir = String(dirRaw || "").trim();
      if (!dir) continue;
      let names: string[] = [];
      try {
        names = fs.readdirSync(dir);
      } catch (err: unknown) {
        // Transcript fallback directories are optional across OpenClaw versions/install shapes.
        // Never fail this sweep because one optional directory is missing/unreadable.
        continue;
      }
      for (const name of names) {
        if (!name.endsWith(".jsonl")) continue;
        const sid = String(name.slice(0, -6) || "").trim();
        if (!sid || seen.has(sid)) continue;
        const transcriptPath = path.join(dir, name);
        try {
          const stat = fs.statSync(transcriptPath);
          if (Number.isFinite(stat.mtimeMs) && stat.mtimeMs > 0) {
            rows.push({ sessionId: sid, lastActivityMs: stat.mtimeMs });
            seen.add(sid);
          }
        } catch (err: unknown) {
          // File may disappear between readdir and stat; ignore and continue scanning.
          continue;
        }
      }
    }
    return rows;
  }

  function resolveSessionForCompaction(sessionId?: string): string | null {
    const rows = deps.listCompactionSessions?.() || [];
    if (!Array.isArray(rows) || rows.length === 0) {
      return null;
    }
    const targetSessionId = String(sessionId || "").trim();
    if (targetSessionId) {
      for (const row of rows) {
        const key = String(row?.key || "").trim();
        const sid = String(row?.sessionId || "").trim();
        if (key && sid && sid === targetSessionId) {
          return key;
        }
      }
    }
    const defaultSessionId = String(deps.resolveDefaultSessionId?.() || "").trim();
    if (defaultSessionId) {
      for (const row of rows) {
        const key = String(row?.key || "").trim();
        const sid = String(row?.sessionId || "").trim();
        if (key && sid && sid === defaultSessionId) {
          return key;
        }
      }
    }
    const fallbackKey = String(rows[0]?.key || "").trim();
    return fallbackKey || null;
  }

  function maybeForceCompactionAfterTimeout(sessionId?: string): void {
    const captureCfg = deps.getMemoryConfig().capture || {};
    const enabled = Boolean(
      captureCfg.autoCompactionOnTimeout
      ?? captureCfg.auto_compaction_on_timeout
      ?? true,
    );
    if (!enabled) return;

    const key = resolveSessionForCompaction(sessionId);
    if (!key) {
      console.warn(`[quaid][timeout] auto-compaction skipped: could not resolve session key (session=${sessionId || "unknown"})`);
      return;
    }
    const compact = deps.requestSessionCompaction;
    if (typeof compact !== "function") {
      console.warn(`[quaid][timeout] auto-compaction skipped: no requestSessionCompaction callback (key=${key})`);
      return;
    }

    try {
      const result = compact(key);
      if (result?.ok) {
        console.log(`[quaid][timeout] auto-compaction requested for key=${key} (compacted=${String(result?.compacted)})`);
      } else {
        const raw = String(result?.raw || "");
        console.warn(`[quaid][timeout] auto-compaction returned non-ok for key=${key}: ${raw}`);
      }
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid][timeout] auto-compaction failed for key=${key}: ${String((err as Error)?.message || err)}`);
    }
  }

  function filterConversationMessages(messages: unknown[]): unknown[] {
    if (!Array.isArray(messages) || messages.length === 0) return [];
    return messages.filter((msg: unknown) => {
      if (!msg || typeof msg !== "object") return false;
      const role = String((msg as Record<string, unknown>).role || "");
      if (role !== "user" && role !== "assistant") return false;
      const text = getMessageText(msg).trim();
      if (!text) return false;
      if (text.startsWith("Extract memorable facts and journal entries from this conversation:")) return false;
      if (isInternalMaintenancePrompt(text)) return false;
      if (role === "assistant") {
        const compact = text.replace(/\s+/g, " ").trim();
        if (/^\{\s*"facts"\s*:\s*\[/.test(compact)) {
          try {
            const parsed = JSON.parse(compact);
            if (parsed && typeof parsed === "object") {
              const keys = Object.keys(parsed);
              const onlyExtractionKeys = keys.every((k) => k === "facts" || k === "journal_entries" || k === "soul_snippets");
              if (onlyExtractionKeys && Array.isArray((parsed as any).facts)) return false;
            }
          } catch {}
        }
      }
      return true;
    });
  }

  function buildTranscript(messages: unknown[]): string {
    const transcript: string[] = [];
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg as Record<string, unknown>;
      const role = String(rec.role || "");
      if (role !== "user" && role !== "assistant") continue;
      let text = getMessageText(msg);
      if (!text) continue;
      if (typeof deps.transcriptFormat?.preprocessText === "function") {
        text = deps.transcriptFormat.preprocessText(text);
      }
      const shouldSkip = deps.transcriptFormat?.shouldSkipText;
      if (typeof shouldSkip === "function" && shouldSkip(role, text)) continue;
      if (!text) continue;
      const speakerLabel = deps.transcriptFormat?.speakerLabel;
      const speaker = typeof speakerLabel === "function"
        ? speakerLabel(role)
        : (role === "user" ? "User" : "Assistant");
      transcript.push(`${speaker}: ${text}`);
    }
    return transcript.join("\n\n");
  }

  // extractFilePaths, summarizeProjectSession, stageProjectEvent, and
  // emitProjectEvent removed. Project event emission is now handled by
  // extract_from_transcript() in Python (ingest/extract.py), which already
  // has the project name, summary, and file paths from the extraction output.
  // This eliminates a redundant LLM call per compact/reset.

  function isResetBootstrapOnlyConversation(
    messages: unknown[],
    bootstrapPrompt: string = "A new session was started via /new or /reset.",
  ): boolean {
    const userTexts = messages
      .filter((msg) => msg && typeof msg === "object" && String((msg as Record<string, unknown>).role || "") === "user")
      .map((msg) => getMessageText(msg).trim())
      .filter(Boolean);
    if (userTexts.length === 0) return false;
    const nonBootstrapUserTexts = userTexts.filter((text) => !text.startsWith(bootstrapPrompt));
    return nonBootstrapUserTexts.length === 0;
  }

  async function updateDocsFromTranscript(
    messages: unknown[],
    label: string,
    sessionId?: string,
    tempDir: string = path.join(deps.workspace, ".quaid", "tmp"),
  ): Promise<void> {
    if (!deps.isSystemEnabled("workspace")) {
      return;
    }
    const memConfig = deps.getMemoryConfig();
    if (!memConfig.docs?.autoUpdateOnCompact) {
      return;
    }
    const fullTranscript = buildTranscript(messages);
    if (!fullTranscript.trim()) {
      console.log(`[quaid][facade] ${label}: no transcript for doc update`);
      return;
    }
    const tmpPath = path.join(
      tempDir,
      `docs-ingest-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`,
    );
    fs.writeFileSync(tmpPath, fullTranscript, { mode: 0o600 });
    try {
      console.log(`[quaid][facade] ${label}: dispatching docs ingest event...`);
      const startTime = Date.now();
      const out = await deps.execEvents("emit", [
        "--name",
        "docs.ingest_transcript",
        "--payload",
        JSON.stringify({
          transcript_path: tmpPath,
          label,
          session_id: sessionId || null,
        }),
        "--source",
        String(deps.eventSource || "adapter"),
        "--dispatch",
        "immediate",
      ]);
      let parsed: Record<string, unknown> = {};
      try {
        const candidate = JSON.parse(out || "{}");
        if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
          parsed = candidate as Record<string, unknown>;
        }
      } catch {}
      const processed = parsed.processed as Record<string, unknown> | undefined;
      const details = Array.isArray(processed?.details) ? processed?.details as unknown[] : [];
      const first = details[0] && typeof details[0] === "object" ? details[0] as Record<string, unknown> : {};
      const resultObj = first.result && typeof first.result === "object"
        ? first.result as Record<string, unknown>
        : {};
      const nested = resultObj.result && typeof resultObj.result === "object"
        ? resultObj.result as Record<string, unknown>
        : resultObj;
      const status = String(nested.status || "").trim();
      const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
      if (status === "up_to_date") {
        console.log(`[quaid][facade] ${label}: all docs up-to-date (${elapsed}s)`);
        return;
      }
      if (status === "updated") {
        const updatedDocs = Number(nested.updatedDocs || 0);
        const staleDocs = Number(nested.staleDocs || 0);
        console.log(`[quaid][facade] ${label}: docs updated (${updatedDocs}/${staleDocs}) (${elapsed}s)`);
        return;
      }
      if (status === "disabled" || status === "skipped") {
        console.log(`[quaid][facade] ${label}: docs ingest skipped (${String(nested.message || "disabled")})`);
        return;
      }
      console.log(`[quaid][facade] ${label}: docs ingest finished (${elapsed}s)`);
    } catch (err: unknown) {
      console.error(`[quaid][facade] ${label} doc update failed:`, (err as Error).message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
    } finally {
      try { fs.unlinkSync(tmpPath); } catch {}
    }
  }


  function detectExplicitLifecycleUserCommand(text: string): "/new" | "/reset" | "/restart" | "/compact" | null {
    if (!text) return null;
    const lines = String(text).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (lines.length !== 1) return null;
    const normalized = lines[0]
      .replace(/\[\[[^\]]+\]\]\s*/g, "")
      .trim();
    const m = normalized.match(/^(?:\[[^\]]+\]\s*)?\/(new|reset|restart|compact)(?=\s|$)/i);
    if (!m) return null;
    return `/${m[1].toLowerCase()}` as "/new" | "/reset" | "/restart" | "/compact";
  }

  function latestMessageTimestampMs(messages: unknown[]): number | null {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    let latest: number | null = null;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg as Record<string, unknown>;
      const raw = rec.timestamp ?? rec.createdAt ?? rec.time ?? null;
      if (raw == null) continue;
      let ts: number | null = null;
      if (typeof raw === "number" && Number.isFinite(raw)) ts = raw;
      else {
        const parsed = Date.parse(String(raw));
        if (Number.isFinite(parsed)) ts = parsed;
      }
      if (ts == null) continue;
      latest = latest == null ? ts : Math.max(latest, ts);
    }
    return latest;
  }

  function hasExplicitLifecycleUserCommand(messages: unknown[]): boolean {
    if (!Array.isArray(messages) || messages.length === 0) return false;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      if (String((msg as Record<string, unknown>).role || "") !== "user") continue;
      const text = getMessageText(msg);
      if (!text) continue;
      if (detectExplicitLifecycleUserCommand(text)) return true;
    }
    return false;
  }

  function isBacklogLifecycleReplay(
    messages: unknown[],
    trigger: string,
    nowMs: number,
    bootTimeMs: number,
    staleMs: number,
  ): boolean {
    if (trigger !== "reset" && trigger !== "new" && trigger !== "recovery") return false;
    const latestTs = latestMessageTimestampMs(messages);
    if (latestTs == null) {
      return !hasExplicitLifecycleUserCommand(messages);
    }
    return latestTs < (Math.min(nowMs, bootTimeMs) - staleMs);
  }

  function detectLifecycleSignal(messages: unknown[]): LifecycleSignal | null {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    const tail = messages.slice(-8);
    const tailOffset = messages.length - tail.length;
    for (let i = tail.length - 1; i >= 0; i--) {
      const msg = tail[i];
      if (!msg || typeof msg !== "object") continue;
      const role = String((msg as Record<string, unknown>).role || "").trim();
      const text = getMessageText(msg).trim();
      if (!text) continue;
      const normalized = text
        .replace(/\[\[[^\]]+\]\]\s*/g, "")
        .replace(/^\[[^\]]+\]\s*/, "")
        .trim();
      const normalizedLc = normalized.toLowerCase();
      const hasSlashLifecycleCommand = /(^|\s)\/(new|reset|restart|compact)(\s|$)/i.test(normalized);
      const isLifecycleCandidateRole = role === "user" || role === "system";
      if (!isLifecycleCandidateRole && !hasSlashLifecycleCommand) continue;

      if (role === "user") {
        const command = detectExplicitLifecycleUserCommand(text);
        if (command === "/new" || command === "/reset" || command === "/restart") {
          return {
            label: "ResetSignal",
            source: "user_command",
            signature: `cmd:${command}`,
            messageIndex: tailOffset + i,
          };
        }
        if (command === "/compact") {
          return {
            label: "CompactionSignal",
            source: "user_command",
            signature: `cmd:${command}`,
            messageIndex: tailOffset + i,
          };
        }
      }

      // Some runtime paths persist slash-command markers outside user-role messages.
      if (/^\/(new|reset|restart)(?:\s|$)/i.test(normalized)) {
        const command = normalized.match(/\/(new|reset|restart)/i)?.[0]?.toLowerCase() || "/new";
        return {
          label: "ResetSignal",
          source: "system_notice",
          signature: `cmd:${command}`,
          messageIndex: tailOffset + i,
        };
      }
      if (/^\/compact(?:\s|$)/i.test(normalized)) {
        return {
          label: "CompactionSignal",
          source: "system_notice",
          signature: "cmd:/compact",
          messageIndex: tailOffset + i,
        };
      }

      if (role === "system") {
        const hasCompacted = /\bcompacted\b/i.test(normalized);
        const hasDelta = /\(\s*[\d.]+k?\s*(?:->|→)\s*[\d.]+k?\s*\)/i.test(normalized);
        const hasContext = /\bcontext\b/i.test(normalized);
        if (hasCompacted && (hasDelta || hasContext)) {
          return {
            label: "CompactionSignal",
            source: "system_notice",
            signature: `system:${normalized.toLowerCase()}`,
            messageIndex: tailOffset + i,
          };
        }
        // OpenClaw /new session reset can surface as a system bootstrap notice.
        if (
          normalizedLc.includes("new session was started via /new or /reset")
          || normalizedLc.includes("a new session was started via /new or /reset")
          || (normalizedLc.includes("new session was started") && normalizedLc.includes("/new"))
          || (normalizedLc.includes("session startup sequence") && normalizedLc.includes("/new"))
        ) {
          return {
            label: "ResetSignal",
            source: "system_notice",
            signature: "system:new_session_started",
            messageIndex: tailOffset + i,
          };
        }
      }
      // Only evaluate the most recent lifecycle-candidate line.
      // This avoids replaying stale /new or /compact commands that are still in transcript tail.
      return null;
    }
    return null;
  }

  function lifecycleSignalKey(sessionId: string, label: "ResetSignal" | "CompactionSignal"): string {
    return `${sessionId}:${label}`;
  }

  function shouldProcessLifecycleSignal(
    sessionId: string,
    signal: LifecycleSignal,
    suppressMs: number = 15_000,
    retentionMs: number = 10 * 60_000,
  ): boolean {
    const now = Date.now();
    for (const [key, value] of lifecycleSignalHistory.entries()) {
      if ((now - value.seenAt) > retentionMs) {
        lifecycleSignalHistory.delete(key);
      }
    }
    const key = lifecycleSignalKey(sessionId, signal.label);
    const prior = lifecycleSignalHistory.get(key);
    lifecycleSignalHistory.set(key, { source: signal.source, signature: signal.signature, seenAt: now });
    if (!prior) return true;
    const ageMs = now - prior.seenAt;
    if (prior.signature === signal.signature && ageMs < suppressMs) return false;
    if (ageMs < suppressMs && prior.source === "hook" && signal.source === "system_notice") {
      return false;
    }
    return true;
  }

  function markLifecycleSignalFromHook(sessionId: string, label: "ResetSignal" | "CompactionSignal"): void {
    lifecycleSignalHistory.set(lifecycleSignalKey(sessionId, label), {
      source: "hook",
      signature: `hook:${label}`,
      seenAt: Date.now(),
    });
  }

  function isInternalMaintenancePrompt(text: string): boolean {
    const normalized = String(text || "").trim().toLowerCase();
    if (!normalized) return false;
    const markers = [
      "review batch",
      "review the following",
      "you are reviewing",
      "you are checking",
      "respond with a json array",
      "json array only:",
      "fact a:",
      "fact b:",
      "log id:",
      "similarity:",
      "llm_reasoning",
      "candidate duplicate pairs",
      "dedup rejections",
      "journal entries to decide",
      "pending soul snippets",
      "are these two statements the same fact",
    ];
    return markers.some((marker) => normalized.includes(marker));
  }

  function resolveExtractionTrigger(label: string): ExtractionTrigger {
    const normalized = String(label || "").trim().toLowerCase();
    if (!normalized) return "unknown";
    if (normalized.includes("compact")) return "compaction";
    if (normalized.includes("recover")) return "recovery";
    if (normalized.includes("timeout")) return "timeout";
    if (normalized.includes("new")) return "new";
    if (normalized.includes("reset")) return "reset";
    return "unknown";
  }

  function isLowQualityQuery(query: string): boolean {
    const ACKNOWLEDGMENTS = /^(ok|okay|yes|no|sure|thanks|thank you|got it|sounds good|perfect|great|cool|alright|yep|nope|right|correct|agreed|absolutely|definitely|nice|good|fine|hm+|ah+|oh+)\s*[.!?]?$/i;
    const words = String(query || "").trim().split(/\s+/).filter((w) => w.length > 1);
    return words.length < 3 || ACKNOWLEDGMENTS.test(String(query || "").trim());
  }

  function filterMemoriesByPrivacy(memories: MemoryResult[], currentOwner: string): MemoryResult[] {
    return memories.filter((m) =>
      !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner)
    );
  }

  function readInjectionLog(sessionId: string): Record<string, unknown> {
    const injectionLogPath = getInjectionLogPath(sessionId);
    try {
      const parsed = JSON.parse(fs.readFileSync(injectionLogPath, "utf8"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch (err: unknown) {
      if (!isMissingFileError(err)) {
        console.warn(`[quaid][facade] Injection log read failed for ${injectionLogPath}: ${String((err as Error)?.message || err)}`);
      }
    }
    return {};
  }

  function writeInjectionLog(sessionId: string, payload: Record<string, unknown>, pretty: boolean = false): void {
    const injectionLogPath = getInjectionLogPath(sessionId);
    try {
      const encoded = pretty ? JSON.stringify(payload, null, 2) : JSON.stringify(payload);
      fs.writeFileSync(injectionLogPath, encoded, { mode: 0o600 });
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw new Error(`[quaid][facade] Injection log write failed for ${injectionLogPath}`, { cause: err as Error });
      }
      console.warn(`[quaid][facade] Injection log write failed for ${injectionLogPath}: ${String((err as Error)?.message || err)}`);
    }
  }

  function loadInjectedMemoryKeys(sessionId: string): string[] {
    const logData = readInjectionLog(sessionId);
    const rawInjected = logData.injected ?? logData.memoryTexts;
    return Array.isArray(rawInjected)
      ? rawInjected.map((item) => String(item || "").trim()).filter(Boolean)
      : [];
  }

  function saveInjectedMemoryKeys(
    sessionId: string,
    previousKeys: string[],
    memories: MemoryResult[],
    maxEntries: number,
  ): string[] {
    const newKeys = memories.map((m) => m.id || m.text);
    const merged = [...previousKeys, ...newKeys]
      .map((k) => String(k || "").trim())
      .filter(Boolean)
      .slice(-Math.max(1, Number(maxEntries) || 1));
    writeInjectionLog(sessionId, {
      injected: merged,
      lastInjectedAt: new Date().toISOString(),
    });
    pruneInjectionLogFiles();
    return merged;
  }

  function resetInjectionDedupAfterCompaction(sessionId: string): void {
    const current = readInjectionLog(sessionId);
    writeInjectionLog(sessionId, {
      ...current,
      lastCompactionAt: new Date().toISOString(),
      injected: [],
      memoryTexts: [],
    }, true);
  }

  function queueCompactionExtractionSummary(
    sessionId: string,
    stored: number,
    skipped: number,
    edges: number,
    notify: (summary: string) => void,
  ): void {
    const now = Date.now();
    if (!compactionNotifyBatchState) {
      compactionNotifyBatchState = {
        startedAtMs: now,
        lastUpdateMs: now,
        sessions: new Set<string>(),
        sessionsWithFacts: new Set<string>(),
        stored: 0,
        skipped: 0,
        edges: 0,
        timer: null,
      };
    }
    const state = compactionNotifyBatchState;
    const sid = String(sessionId || "").trim() || `unknown-${now}`;
    state.sessions.add(sid);
    if (stored > 0) {
      state.sessionsWithFacts.add(sid);
    }
    state.stored += Math.max(0, Number(stored || 0));
    state.skipped += Math.max(0, Number(skipped || 0));
    state.edges += Math.max(0, Number(edges || 0));
    state.lastUpdateMs = now;

    const batchAgeMs = now - state.startedAtMs;
    if (batchAgeMs >= COMPACTION_NOTIFY_BATCH_MAX_MS) {
      if (state.timer) {
        clearTimeout(state.timer);
        state.timer = null;
      }
      state.startedAtMs = 0;
      state.lastUpdateMs = now;
    }
    if (state.timer) {
      clearTimeout(state.timer);
      state.timer = null;
    }
    const flushDelayMs = state.startedAtMs === 0
      ? 0
      : Math.max(0, Math.min(COMPACTION_NOTIFY_BATCH_MS, COMPACTION_NOTIFY_BATCH_MAX_MS - (now - state.startedAtMs)));
    state.timer = setTimeout(() => {
      const flushState = compactionNotifyBatchState;
      if (!flushState) return;
      compactionNotifyBatchState = null;
      if (flushState.timer) {
        clearTimeout(flushState.timer);
        flushState.timer = null;
      }
      const sessionCount = flushState.sessions.size;
      if (sessionCount <= 0) return;
      const durationSec = Math.max(1, Math.round((flushState.lastUpdateMs - flushState.startedAtMs) / 1000));
      const summary = [
        "**[Quaid]** 💾 **Compaction extraction summary:**",
        "",
        `• Sessions processed: ${sessionCount}`,
        `• Facts stored: ${flushState.stored}`,
        `• Facts skipped: ${flushState.skipped}`,
        `• Edges created: ${flushState.edges}`,
        `• Sessions with new facts: ${flushState.sessionsWithFacts.size}`,
        `• Window: ${durationSec}s`,
      ].join("\n");
      notify(summary);
    }, flushDelayMs);
    if (typeof (state.timer as any).unref === "function") {
      (state.timer as any).unref();
    }
  }

  async function getDocsStalenessWarning(): Promise<string> {
    const stalenessJson = await deps.execDocsUpdater("check", ["--json"]);
    const staleRaw = JSON.parse(stalenessJson || "{}");
    const staleDocs = staleRaw && typeof staleRaw === "object" && !Array.isArray(staleRaw)
      ? (staleRaw as Record<string, any>)
      : {};
    const staleKeys = Object.keys(staleDocs);
    if (staleKeys.length === 0) {
      return "";
    }
    const warnings = staleKeys.map((k) => {
      const entry = staleDocs[k] && typeof staleDocs[k] === "object" ? staleDocs[k] : {};
      const gapHours = Number(entry?.gap_hours);
      const staleSources = Array.isArray(entry?.stale_sources) ? entry.stale_sources : [];
      return `  ${k} (${Number.isFinite(gapHours) ? gapHours : 0}h behind: ${staleSources.join(", ")})`;
    });
    return `\n\nSTALENESS WARNING: The following docs may be outdated:\n${warnings.join("\n")}\nConsider running: docs staleness updater (update-stale --apply)`;
  }

  function buildDocsSearchNotificationPayload(
    query: string,
    results: string,
  ): {
    query: string;
    results: Array<{ doc: string; section: string; score: number }>;
  } {
    const docResults: Array<{ doc: string; section: string; score: number }> = [];
    const lines = String(results || "").split("\n");
    for (const line of lines) {
      const match = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
      if (!match) continue;
      docResults.push({
        doc: match[1].split("/").pop() || match[1],
        section: match[2].trim(),
        score: parseFloat(match[3]),
      });
    }
    return {
      query: String(query || ""),
      results: docResults,
    };
  }

  function loadProjectMarkdown(project: string): string {
    const projectName = String(project || "").trim();
    if (!projectName) return "";
    try {
      const cfg = deps.getMemoryConfig();
      const homeDir = String(cfg?.projects?.definitions?.[projectName]?.homeDir || "").trim();
      if (!homeDir) return "";
      const mdPath = path.join(deps.workspace, homeDir, "PROJECT.md");
      if (!fs.existsSync(mdPath)) return "";
      return fs.readFileSync(mdPath, "utf-8");
    } catch {
      return "";
    }
  }

  function computeDynamicK(): number {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10) return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
  }

  // -------------------------------------------------------------------------
  // Bridge recall helper (calls datastoreBridge.recall with JSON config)
  // -------------------------------------------------------------------------

  function parseMemoryBridgePayload(output: string, expandGraph: boolean): {
    results: MemoryResult[];
    meta: Record<string, unknown> | null;
  } {
    const results: MemoryResult[] = [];
    let meta: Record<string, unknown> | null = null;
    if (!output || !output.trim()) return { results, meta };

    try {
      const parsed = JSON.parse(output);
      meta = parsed && typeof parsed === "object" && !Array.isArray(parsed) && parsed.meta && typeof parsed.meta === "object"
        ? parsed.meta as Record<string, unknown>
        : null;
      const items = Array.isArray(parsed) ? parsed : (parsed?.results || parsed?.items || parsed?.direct_results || []);
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
          speaker: item.speaker || undefined,
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

    return { results, meta };
  }

  async function recallMemoryFromBridgeDetailed(
    query: string,
    limit: number,
    opts: RecallMemoryOpts,
  ): Promise<{ results: MemoryResult[]; meta: Record<string, unknown> | null }> {
    const rawStores = opts.stores || ["vector"];
    const expandGraph = rawStores.includes("graph");

    // Normalize legacy vector_basic/vector_technical → vector + domain_filter.
    // These are internal routing names; the CLI only knows "vector" and "graph".
    const normalizedStores = [...new Set(rawStores.map((s) =>
      (s === "vector_basic" || s === "vector_technical") ? "vector" : s
    ))];

    let domain = opts.domain;
    if (!domain) {
      if (rawStores.includes("vector_technical") && !rawStores.includes("vector_basic")) {
        domain = { technical: true };
      } else if (rawStores.includes("vector_basic") && !rawStores.includes("vector_technical")) {
        domain = { personal: true };
      }
    }

    // Build JSON config — all retrieval options in one object
    const cfg: Record<string, unknown> = { stores: normalizedStores, limit };
    if (domain) cfg["domain_filter"] = domain;
    if (opts.domainBoost) cfg["domain_boost"] = opts.domainBoost;
    if (opts.project) cfg["project"] = opts.project;
    if (opts.dateFrom) cfg["date_from"] = opts.dateFrom;
    if (opts.dateTo) cfg["date_to"] = opts.dateTo;
    if (opts.fast) cfg["fast"] = true;
    if (expandGraph && opts.depth) cfg["depth"] = opts.depth;
    if (opts.candidatePool && Array.isArray(opts.candidatePool) && opts.candidatePool.length > 0) {
      cfg["candidate_pool"] = opts.candidatePool;
    }

    try {
      const args: string[] = [query, JSON.stringify(cfg), "--json"];
      const output = await datastoreBridge.recall(args);
      const payload = parseMemoryBridgePayload(output, expandGraph);
      const results = !expandGraph
        ? payload.results.map((r) => ({ ...r, via: "vector" as const }))
        : payload.results.filter((r) => (r.via || "") === "graph" || r.category === "graph");
      return { results, meta: payload.meta };
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) throw err;
      console.error("[quaid][facade] recall error:", (err as Error).message);
      return { results: [], meta: null };
    }
  }

  async function recallMemoryFromBridge(
    query: string,
    limit: number,
    opts: RecallMemoryOpts,
  ): Promise<MemoryResult[]> {
    const { results } = await recallMemoryFromBridgeDetailed(query, limit, opts);
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
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
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

  function hasPendingMemoryNotes(sessionId: string): boolean {
    const sid = String(sessionId || "").trim();
    if (!sid) return false;
    const inMemory = _memoryNotes.get(sid);
    if (Array.isArray(inMemory) && inMemory.length > 0) return true;
    try {
      const onDisk = JSON.parse(fs.readFileSync(getNotesPath(sid), "utf8"));
      return Array.isArray(onDisk) && onDisk.some((n: unknown) => typeof n === "string" && n.trim().length > 0);
    } catch {
      return false;
    }
  }

  async function runExtractionPipeline(
    messages: unknown[],
    label: string,
    sessionId?: string,
  ): Promise<ExtractionPipelineResult | null> {
    const rows = Array.isArray(messages) ? messages : [];
    const sessionNotes = sessionId ? getAndClearMemoryNotes(sessionId) : [];
    const allNotes = Array.from(new Set([...sessionNotes]));
    if (rows.length === 0 && allNotes.length === 0) {
      return null;
    }
    const fullTranscript = buildTranscript(rows);
    if (!fullTranscript.trim() && allNotes.length === 0) {
      return null;
    }
    const hasMeaningfulUserContent = rows.some((m: unknown) => {
      if (!m || typeof m !== "object") return false;
      const role = String((m as Record<string, unknown>).role || "");
      if (role !== "user") return false;
      const text = getMessageText(m).trim();
      if (!text) return false;
      if (typeof deps.transcriptFormat?.shouldSkipText === "function") {
        return !deps.transcriptFormat.shouldSkipText("user", text);
      }
      return true;
    });
    const transcriptForExtraction = allNotes.length > 0
      ? (
        "=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===\n"
        + `${allNotes.map((n) => `- ${n}`).join("\n")}\n`
        + "=== END EXPLICIT MEMORY REQUESTS ===\n\n"
        + fullTranscript
      )
      : fullTranscript;

    const journalConfig = deps.getMemoryConfig().docs?.journal || {};
    const journalEnabled = deps.isSystemEnabled("journal") && journalConfig.enabled !== false;
    const snippetsEnabled = journalEnabled && journalConfig.snippetsEnabled !== false;
    const triggerType = resolveExtractionTrigger(label);
    const tmpDir = path.join(deps.workspace, ".quaid", "tmp");
    fs.mkdirSync(tmpDir, { recursive: true });
    const tmpPath = path.join(tmpDir, `extract-input-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
    fs.writeFileSync(tmpPath, transcriptForExtraction, { mode: 0o600 });
    const args = [
      "--owner", resolveOwner(),
      "--label", triggerType,
      "--json",
    ];
    if (sessionId) args.push("--session-id", sessionId);
    if (!snippetsEnabled) args.push("--no-snippets");
    if (!journalEnabled) args.push("--no-journal");

    let extracted: any = {};
    try {
      const output = await deps.execExtractPipeline(tmpPath, args);
      extracted = JSON.parse(output || "{}");
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err);
      throw new Error(`[quaid][facade] extract pipeline failed: ${msg}`);
    } finally {
      try { fs.unlinkSync(tmpPath); } catch {}
    }

    const factDetails: Array<{ text: string; status: string; reason?: string; edges?: string[] }> = Array.isArray(extracted?.facts)
      ? extracted.facts
      : [];
    const stored = Number(extracted?.facts_stored || 0);
    const skipped = Number(extracted?.facts_skipped || 0);
    const edgesCreated = Number(extracted?.edges_created || 0);

    const snippetDetails: Record<string, string[]> = {};
    const journalDetails: Record<string, string[]> = {};
    const targetFiles: string[] = journalConfig.targetFiles || ["SOUL.md", "USER.md", "ENVIRONMENT.md"];
    const snippetsRaw = extracted?.snippets;
    if (snippetsRaw && typeof snippetsRaw === "object" && !Array.isArray(snippetsRaw)) {
      for (const [filename, snippets] of Object.entries(snippetsRaw)) {
        if (!targetFiles.includes(filename) || !Array.isArray(snippets)) continue;
        const valid = snippets.filter((s: unknown) => typeof s === "string" && (s as string).trim().length > 0);
        if (valid.length > 0) snippetDetails[filename] = valid.map((s: string) => s.trim());
      }
    }
    const journalRaw = extracted?.journal;
    if (journalRaw && typeof journalRaw === "object" && !Array.isArray(journalRaw)) {
      for (const [filename, entry] of Object.entries(journalRaw)) {
        if (!targetFiles.includes(filename)) continue;
        const text = typeof entry === "string" ? entry : "";
        if (text.trim().length > 0) journalDetails[filename] = [text.trim()];
      }
    }

    return {
      hasMeaningfulUserContent,
      triggerType,
      factDetails,
      stored,
      skipped,
      edgesCreated,
      snippetDetails,
      journalDetails,
    };
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
        datastores: shouldRouteStores ? [] : selectedStores,
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
      return knowledgeEngine.recall(q, limit, {
        ...recallOpts,
        reasoning,
        failOpen,
      });
    };

    return runRecall(query);
  }

  async function recallWithDiagnostics(opts: FacadeRecallOptions): Promise<{ results: MemoryResult[]; diagnostics: RecallDiagnostics | null }> {
    const {
      query, limit = 10, expandGraph = true, graphDepth = 1,
      datastores, routeStores, reasoning = "fast", domain = { all: true }, domainBoost, project,
      dateFrom, dateTo,
    } = opts;
    const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
    const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
    const bridgeOnlyStores = new Set(["vector", "vector_basic", "vector_technical", "graph"]);
    if (!shouldRouteStores && selectedStores.length > 0 && selectedStores.every((store) => bridgeOnlyStores.has(store))) {
      const { results, meta } = await recallMemoryFromBridgeDetailed(query, limit, {
        stores: selectedStores,
        domain,
        domainBoost,
        project,
        dateFrom,
        dateTo,
        depth: graphDepth,
        fast: reasoning === "fast",
      });
      return { results, diagnostics: { meta } };
    }
    const results = await recall(opts);
    return { results, diagnostics: null };
  }

  function isLowInformationEntityNode(result: MemoryResult): boolean {
    if ((result.via || "vector") === "graph" || result.category === "graph") return false;
    const category = String(result.category || "").toLowerCase();
    if (!["person", "concept", "event", "entity"].includes(category)) return false;
    const text = String(result.text || "").trim();
    if (!text) return true;
    const words = text.split(/\s+/).filter(Boolean);
    if (words.length <= 2 && /^[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?$/.test(text)) return true;
    return false;
  }

  function isGraphAnchorExpansion(result: MemoryResult): boolean {
    return String(result.via || "").toLowerCase() === "graph_anchor_expansion";
  }

  function getConfiguredDomainIds(): string[] {
    try {
      const defs = deps.getMemoryConfig()?.retrieval?.domains;
      if (defs && typeof defs === "object" && !Array.isArray(defs)) {
        return Object.keys(defs).map((k) => String(k).trim()).filter(Boolean).sort();
      }
    } catch {}
    return [];
  }

  function normalizeToken(raw: string): string {
    return String(raw || "")
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "");
  }

  function stemToken(token: string): string {
    if (token.length > 6 && token.endsWith("ing")) return token.slice(0, -3);
    if (token.length > 5 && token.endsWith("ed")) return token.slice(0, -2);
    if (token.length > 4 && token.endsWith("s")) return token.slice(0, -1);
    return token;
  }

  function tokenizeQuery(query: string): string[] {
    return String(query || "")
      .split(/\s+/)
      .map((part) => normalizeToken(part))
      .map((token) => stemToken(token))
      .filter((token) => token.length >= 3 && !RECALL_RETRY_STOPWORDS.has(token));
  }

  function temporalCuePresent(query: string): boolean {
    const lowered = String(query || "").toLowerCase();
    const cues = ["latest", "current", "currently", "still", "now", "as of", "when", "last", "updated"];
    return cues.some((cue) => lowered.includes(cue));
  }

  function attributionCuePresent(query: string): boolean {
    const lowered = String(query || "").toLowerCase();
    const cues = ["who", "whose", "did", "does", "said", "asked", "told", "mentioned", "attributed"];
    return cues.some((cue) => lowered.includes(cue));
  }

  function isVectorRecallResult(result: MemoryResult): boolean {
    const via = String(result.via || "").toLowerCase();
    return via === "vector" || via === "vector_basic" || via === "vector_technical";
  }

  function computeEntityCoverage(query: string, results: MemoryResult[]): number {
    const resultBlob = results
      .map((r) => `${String(r.text || "").toLowerCase()} ${String(r.sourceName || "").toLowerCase()}`)
      .join(" ");
    const tokens = tokenizeQuery(query);
    if (!tokens.length) return 1;
    const matched = tokens.filter((token) => resultBlob.includes(token)).length;
    return matched / tokens.length;
  }

  function buildExpandedRecallQuery(query: string): string {
    const tokens = tokenizeQuery(query);
    const expanded = new Set(tokens);
    for (const token of tokens) {
      expanded.add(stemToken(token));
    }
    if (temporalCuePresent(query)) {
      ["latest", "current", "timeline", "asof", "status"].forEach((t) => expanded.add(t));
    }
    if (attributionCuePresent(query)) {
      ["person", "speaker", "attribution"].forEach((t) => expanded.add(t));
    }
    const expansionTail = Array.from(expanded).slice(0, 16).join(" ");
    if (!expansionTail) return query;
    return `${query} ${expansionTail}`;
  }

  function shouldRetryRecall(query: string, results: MemoryResult[]): { retry: boolean; reasons: string[] } {
    const reasons: string[] = [];
    if (!results.length) {
      reasons.push("no_results");
      return { retry: true, reasons };
    }

    const vectorResults = results.filter((r) => isVectorRecallResult(r));
    if (!vectorResults.length) {
      reasons.push("no_vector_hits");
      return { retry: true, reasons };
    }

    const avgSimilarity = vectorResults.reduce((sum, r) => sum + Number(r.similarity || 0), 0) / vectorResults.length;
    const maxSimilarity = Math.max(...vectorResults.map((r) => Number(r.similarity || 0)));
    if (avgSimilarity < 0.48 && maxSimilarity < 0.62) {
      reasons.push("low_similarity");
    }

    const coverage = computeEntityCoverage(query, results);
    if (coverage < 0.35) {
      reasons.push("low_entity_coverage");
    }

    if (temporalCuePresent(query)) {
      const hasTemporalFields = results.some((r) => Boolean(r.createdAt || r.validFrom || r.validUntil));
      if (!hasTemporalFields) reasons.push("missing_temporal_context");
    }

    return { retry: reasons.length > 0, reasons };
  }

  function mergeRecallResults(primary: MemoryResult[], secondary: MemoryResult[], limit: number): MemoryResult[] {
    const merged = new Map<string, MemoryResult>();
    const upsert = (row: MemoryResult) => {
      const key = String(row.id || `${row.category}:${row.text}`).trim();
      const current = merged.get(key);
      if (!current) {
        merged.set(key, row);
        return;
      }
      if (Number(row.similarity || 0) > Number(current.similarity || 0)) {
        merged.set(key, row);
      }
    };
    primary.forEach(upsert);
    secondary.forEach(upsert);
    return Array.from(merged.values())
      .sort((a, b) => Number(b.similarity || 0) - Number(a.similarity || 0))
      .slice(0, Math.max(1, limit));
  }

  async function recallWithToolRetry(opts: FacadeRecallOptions): Promise<MemoryResult[]> {
    const primary = await recall(opts);
    const query = String(opts.query || "");
    const limit = Math.max(1, Number(opts.limit || 10));
    const retryDecision = shouldRetryRecall(query, primary);
    if (!retryDecision.retry) return primary;
    const expanded = buildExpandedRecallQuery(query);
    if (expanded === query) return primary;
    const retryBudgetRaw = Number(((deps.getMemoryConfig() || {}).retrieval || {}).retry_budget_ms ?? 3000);
    const retryBudgetMs = Number.isFinite(retryBudgetRaw) ? Math.max(0, Math.floor(retryBudgetRaw)) : 3000;
    if (retryBudgetMs <= 0) {
      console.log("[quaid][facade][recall] retry skipped (retry_budget_ms=0)");
      return primary;
    }
    const retryStartedAt = Date.now();
    console.log(
      `[quaid][facade][recall] retry reasons=${retryDecision.reasons.join(",")} expanded="${expanded.slice(0, 160)}" budget_ms=${retryBudgetMs}`,
    );
    try {
      const elapsed = Date.now() - retryStartedAt;
      const remainingMs = Math.max(1, retryBudgetMs - elapsed);
      const secondary = await Promise.race<MemoryResult[]>([
        recall({ ...opts, query: expanded }),
        new Promise<MemoryResult[]>((_, reject) =>
          setTimeout(() => reject(new Error(`retry budget exceeded (${retryBudgetMs}ms)`)), remainingMs),
        ),
      ]);
      return mergeRecallResults(primary, secondary, limit);
    } catch (err) {
      const msg = String((err as Error)?.message || err);
      // Only swallow retry budget timeouts — these are expected when the expansion
      // window runs out. Any other failure (including planner failHard RuntimeErrors)
      // must propagate so the caller sees a real error, not a silent degradation.
      if (msg.includes("retry budget exceeded")) {
        console.warn(`[quaid][facade][recall] retry bailed: ${msg}; returning primary`);
        return primary;
      }
      throw err;
    }
  }

  function formatMemoriesForInjection(memories: MemoryResult[]): string {
    if (!memories.length) return "";
    const firstRecallMeta = memories.find((m) => {
      const meta = (m as any)?._recall_meta;
      return meta && typeof meta === "object" && !Array.isArray(meta);
    }) as any;
    const memoryQuality = firstRecallMeta?._recall_meta?.memory_quality
      && typeof firstRecallMeta._recall_meta.memory_quality === "object"
      && !Array.isArray(firstRecallMeta._recall_meta.memory_quality)
      ? firstRecallMeta._recall_meta.memory_quality
      : null;
    const qualityNote = typeof memoryQuality?.note === "string"
      ? memoryQuality.note.trim()
      : "";
    const formatTemporalLabel = (memory: MemoryResult): string => {
      if (memory.validFrom || memory.validUntil) {
        const from = memory.validFrom ? memory.validFrom.split("T")[0] : "";
        const until = memory.validUntil ? memory.validUntil.split("T")[0] : "";
        if (from && until) return ` | valid ${from} until ${until}`;
        if (from) return ` | valid ${from}`;
        if (until) return ` | valid until ${until}`;
      }
      if (memory.createdAt) return ` | recorded ${memory.createdAt.split("T")[0]}`;
      return "";
    };
    const sortTimestampDesc = (value?: string): number => {
      if (!value) return Number.NEGATIVE_INFINITY;
      const parsed = Date.parse(value);
      return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
    };
    const sorted = [...memories].sort((a, b) => {
      const simDelta = Number(b.similarity || 0) - Number(a.similarity || 0);
      if (Math.abs(simDelta) > 1e-9) return simDelta;
      return sortTimestampDesc(b.createdAt) - sortTimestampDesc(a.createdAt);
    });
    const normalizeGraphText = (text: string): string => text.replace(/\s*→\s*/g, " -> ");
    const formatMemoryLine = (m: MemoryResult, label = m.category): string => {
      const temporalLabel = formatTemporalLabel(m);
      const projectLabel = m.project ? ` | project ${m.project}` : "";
      return `- ${label} | ${m.text}${projectLabel}${temporalLabel}`;
    };
    const graphAnchorExpansions = sorted.filter((m) => isGraphAnchorExpansion(m));
    const graphExpansionAnchorKeys = new Set(
      graphAnchorExpansions.map((m) => String(m.graphExpansionAnchorId || m.graphExpansionAnchorText || "").trim()).filter(Boolean),
    );
    const graphNodeHits = sorted.filter((m) =>
      isLowInformationEntityNode(m)
      && !isGraphAnchorExpansion(m)
      && !graphExpansionAnchorKeys.has(String(m.id || m.text || "").trim()),
    );
    const regularMemories = sorted.filter((m) => !isLowInformationEntityNode(m) && !isGraphAnchorExpansion(m));
    const lines = regularMemories.map((m) => formatMemoryLine(m));
    if (qualityNote) {
      lines.unshift(`- [memory-quality] ${qualityNote}`);
    }
    if (graphAnchorExpansions.length > 0) {
      const grouped = new Map<string, { anchorText: string; anchorSimilarity?: number; rows: MemoryResult[] }>();
      for (const row of graphAnchorExpansions) {
        const key = String(row.graphExpansionAnchorId || row.graphExpansionAnchorText || row.text || "").trim();
        if (!key) continue;
        const existing = grouped.get(key);
        if (existing) {
          existing.rows.push(row);
          if ((row.graphExpansionAnchorSimilarity || 0) > (existing.anchorSimilarity || 0)) {
            existing.anchorSimilarity = row.graphExpansionAnchorSimilarity;
          }
          continue;
        }
        grouped.set(key, {
          anchorText: String(row.graphExpansionAnchorText || row.sourceName || row.text || "").trim(),
          anchorSimilarity: row.graphExpansionAnchorSimilarity,
          rows: [row],
        });
      }
      if (grouped.size > 0) {
        for (const group of grouped.values()) {
          const anchor = String(group.anchorText || "").trim();
          if (!anchor) continue;
          lines.push(`<graph_expansion:${anchor}>`);
          if (Number.isFinite(Number(group.totalConnections)) && Number.isFinite(Number(group.shownConnections))) {
            lines.push(`<Showing top ${Math.round(Number(group.shownConnections || 0))} of ${Math.round(Number(group.totalConnections || 0))} graph relations>`);
          }
          for (const row of group.rows) {
            lines.push(`  ${normalizeGraphText(String(row.text || ""))}`);
          }
          lines.push(`</graph_expansion>`);
        }
      }
    }
    if (graphNodeHits.length > 0) {
      graphNodeHits.slice(0, 8).forEach((m) => {
        lines.push(`- graph_node_hit | ${m.text}`);
      });
    }
    return `<injected_memories>
${lines.join("\n")}
</injected_memories>`;
  }

  function formatRecallToolResponse(results: MemoryResult[]): {
    text: string;
    breakdown: {
      vector_count: number;
      graph_count: number;
      journal_count: number;
      project_count: number;
    };
  } {
    const vectorResults = results.filter((r) => isVectorRecallResult(r));
    const graphExpansionResults = results.filter((r) => isGraphAnchorExpansion(r));
    const graphResults = results.filter((r) => ((r.via || "") === "graph" || r.category === "graph") && !isGraphAnchorExpansion(r));
    const journalResults = results.filter((r) => (r.via || "") === "journal");
    const projectResults = results.filter((r) => (r.via || "") === "project");

    const avgSimilarity = vectorResults.length > 0
      ? vectorResults.reduce((sum, r) => sum + (r.similarity || 0), 0) / vectorResults.length
      : 0;
    const maxSimilarity = vectorResults.length > 0
      ? Math.max(...vectorResults.map((r) => Number(r.similarity || 0)))
      : 0;
    const hasHighExtractionConfidence = vectorResults.some((r) => Number(r.extractionConfidence || 0) >= 0.8);
    const lowQualityWarning = (
      vectorResults.length > 0
      && avgSimilarity < 0.45
      && maxSimilarity < 0.55
      && !hasHighExtractionConfidence
    )
      ? "\n\n⚠️ Low confidence matches - consider refining query with specific names or topics.\n"
      : "";

    let text = `[MEMORY] Found ${results.length} results:${lowQualityWarning}\n`;
    if (vectorResults.length > 0) {
      text += "\n**Direct Matches:**\n";
      vectorResults.forEach((r, i) => {
        const conf = r.extractionConfidence ? ` [conf:${Math.round(r.extractionConfidence * 100)}%]` : "";
        const dateStr = r.createdAt ? ` (${r.createdAt.split("T")[0]})` : "";
        const superseded = r.validUntil ? " [superseded]" : "";
        const speakerTag = r.speaker === "agent" ? " [agent-attributed: treat personal facts with lower confidence]" : "";
        text += `${i + 1}. [MEMORY] [${r.category}]${dateStr}${superseded}${speakerTag} ${r.text} (${Math.round(r.similarity * 100)}%${conf})\n`;
      });
    }
    if (graphResults.length > 0) {
      if (vectorResults.length > 0) text += "\n";
      text += "**Graph Discoveries:**\n";
      graphResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text}\n`;
      });
    }
    if (graphExpansionResults.length > 0) {
      if (vectorResults.length > 0 || graphResults.length > 0) text += "\n";
      text += "**Graph Anchor Expansions:**\n";
      graphExpansionResults.forEach((r, i) => {
        const anchor = r.graphExpansionAnchorText || r.sourceName || "anchor";
        text += `${i + 1}. [MEMORY] [anchor:${anchor}] ${r.text}\n`;
      });
    }
    if (journalResults.length > 0) {
      if (vectorResults.length > 0 || graphResults.length > 0 || graphExpansionResults.length > 0) text += "\n";
      text += "**Journal Signals:**\n";
      journalResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)\n`;
      });
    }
    if (projectResults.length > 0) {
      if (vectorResults.length > 0 || graphResults.length > 0 || graphExpansionResults.length > 0 || journalResults.length > 0) text += "\n";
      text += "**Project Knowledge:**\n";
      projectResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)\n`;
      });
    }
    return {
      text,
      breakdown: {
        vector_count: vectorResults.length,
        graph_count: graphResults.length + graphExpansionResults.length,
        journal_count: journalResults.length,
        project_count: projectResults.length,
      },
    };
  }

  function buildRecallNotificationPayload(
    results: MemoryResult[],
    query: string,
    mode: "tool" | "auto_inject",
    breakdown?: {
      vector_count: number;
      graph_count: number;
      journal_count?: number;
      project_count?: number;
    },
  ): {
    memories: Array<{
      text: string;
      similarity: number;
      via: string;
      category: string;
    }>;
    source_breakdown: {
      vector_count: number;
      graph_count: number;
      journal_count: number;
      project_count: number;
      query: string;
      mode: "tool" | "auto_inject";
    };
  } {
    const classified = results.map((r) => {
      const via = String(r.via || "").trim().toLowerCase();
      const category = String(r.category || "").trim().toLowerCase();
      if (via === "graph" || via === "graph_anchor_expansion" || category === "graph") return "graph" as const;
      if (via === "journal") return "journal" as const;
      if (via === "project") return "project" as const;
      // Backward-compatible default: missing/unknown via counts as vector.
      return "vector" as const;
    });
    const vectorCount = classified.filter((k) => k === "vector").length;
    const graphCount = classified.filter((k) => k === "graph").length;
    const journalCount = classified.filter((k) => k === "journal").length;
    const projectCount = classified.filter((k) => k === "project").length;
    return {
      memories: results.map((m) => ({
        text: m.text,
        similarity: Math.round((m.similarity || 0) * 100),
        via: m.via || "vector",
        category: m.category || "",
      })),
      source_breakdown: {
        vector_count: Number(breakdown?.vector_count ?? vectorCount),
        graph_count: Number(breakdown?.graph_count ?? graphCount),
        journal_count: Number(breakdown?.journal_count ?? journalCount),
        project_count: Number(breakdown?.project_count ?? projectCount),
        query,
        mode,
      },
    };
  }

  function prepareAutoInjectionContext(params: {
    allMemories: MemoryResult[];
    eventMessages: any[];
    context?: any;
    existingPrependContext?: string;
    injectLimit: number;
    maxInjectionIdsPerSession: number;
  }): AutoInjectionPreparation | null {
    const {
      allMemories,
      eventMessages,
      context,
      existingPrependContext,
      injectLimit,
      maxInjectionIdsPerSession,
    } = params;
    if (!Array.isArray(allMemories) || allMemories.length === 0) return null;
    const currentOwner = resolveOwner();
    const filtered = filterMemoriesByPrivacy(allMemories, currentOwner);
    if (!filtered.length) return null;

    const uniqueSessionId = extractSessionId(eventMessages || [], context);
    const previouslyInjected = loadInjectedMemoryKeys(uniqueSessionId);
    const newMemories = filtered.filter((m) => !previouslyInjected.includes(m.id || m.text));
    const toInject = newMemories.slice(0, injectLimit);
    if (!toInject.length) return null;

    const formatted = formatMemoriesForInjection(toInject);
    const prependContext = existingPrependContext
      ? `${existingPrependContext}\n\n${formatted}`
      : formatted;

    saveInjectedMemoryKeys(
      uniqueSessionId,
      previouslyInjected,
      toInject,
      maxInjectionIdsPerSession,
    );
    return { prependContext, toInject, uniqueSessionId };
  }

  function buildExtractionCompletionNotificationPayload(params: {
    stored: number;
    skipped: number;
    edgesCreated: number;
    triggerType: string;
    factDetails: Array<{ text: string; status: string; reason?: string; edges?: string[] }>;
    snippetDetails: Record<string, string[]>;
    journalDetails: Record<string, string[]>;
    alwaysNotifyCompletion: boolean;
  }): {
    stored: number;
    skipped: number;
    edges_created: number;
    trigger: string;
    details: Array<{ text: string; status: string; reason?: string; edges?: string[] }>;
    snippet_details: Record<string, string[]> | null;
    always_notify: boolean;
  } {
    const mergedDetails: Record<string, string[]> = {};
    for (const [f, items] of Object.entries(params.snippetDetails || {})) {
      mergedDetails[f] = items.map((s) => `[snippet] ${s}`);
    }
    for (const [f, items] of Object.entries(params.journalDetails || {})) {
      mergedDetails[f] = [...(mergedDetails[f] || []), ...items.map((s) => `[journal] ${s}`)];
    }
    const hasMerged = Object.keys(mergedDetails).length > 0;
    return {
      stored: Number(params.stored || 0),
      skipped: Number(params.skipped || 0),
      edges_created: Number(params.edgesCreated || 0),
      trigger: String(params.triggerType || "") === "unknown" ? "reset" : String(params.triggerType || "reset"),
      details: Array.isArray(params.factDetails) ? params.factDetails : [],
      snippet_details: hasMerged ? mergedDetails : null,
      always_notify: Boolean(params.alwaysNotifyCompletion),
    };
  }

  function getExtractionStartTriggerDescription(triggerType: string): string {
    if (triggerType === "compaction") return "compaction";
    if (triggerType === "recovery") return "recovery";
    if (triggerType === "timeout") return "timeout";
    if (triggerType === "new") return "/new";
    return "reset";
  }

  function shouldNotifyExtractionStart(params: {
    messages: any[];
    label: string;
    sessionId?: string;
    hasMeaningfulUserContent: boolean;
    bootTimeMs: number;
    backlogNotifyStaleMs: number;
    showProcessingStart: boolean;
  }): { triggerDesc: string } | null {
    if (!params.showProcessingStart || !shouldNotifyFeature("extraction", "summary")) return null;
    const triggerType = resolveExtractionTrigger(params.label);
    const suppressBacklogNotify = isBacklogLifecycleReplay(
      params.messages,
      triggerType,
      Date.now(),
      params.bootTimeMs,
      params.backlogNotifyStaleMs,
    );
    const dedupeSession = params.sessionId || extractSessionId(params.messages, {});
    const dedupeKey = `start:${dedupeSession}:${triggerType}`;
    if (triggerType === "recovery") return null;
    if (suppressBacklogNotify) return null;
    if (!params.hasMeaningfulUserContent) return null;
    if (!shouldEmitExtractionNotify(dedupeKey)) return null;
    return { triggerDesc: getExtractionStartTriggerDescription(triggerType) };
  }

  function injectFullJournalContext(existingContext?: string): string | undefined {
    let prepend = existingContext;
    if (!deps.isSystemEnabled("journal")) return prepend;
    try {
      const journalConfig = deps.getMemoryConfig().docs?.journal || {};
      const journalMode = journalConfig.mode || "distilled";
      if (journalMode !== "full") return prepend;

      const journalDir = path.join(deps.workspace, journalConfig.journalDir || "journal");
      let journalFiles: string[] = [];
      try {
        journalFiles = fs.readdirSync(journalDir).filter((f: string) => f.endsWith(".journal.md")).sort();
      } catch (err: unknown) {
        if (deps.isFailHardEnabled()) {
          throw new Error("[quaid] Journal injection listing failed under failHard", { cause: err as Error });
        }
        console.warn(`[quaid] Journal injection listing failed: ${String((err as Error)?.message || err)}`);
      }

      let journalContent = "";
      for (const file of journalFiles) {
        try {
          const content = fs.readFileSync(path.join(journalDir, file), "utf8");
          if (content.trim()) {
            journalContent += `\n\n--- ${file} ---\n${content}`;
          }
        } catch (err: unknown) {
          if (deps.isFailHardEnabled()) {
            throw new Error(`[quaid] Journal injection read failed for ${file} under failHard`, { cause: err as Error });
          }
          console.warn(`[quaid] Journal injection read failed for ${file}: ${String((err as Error)?.message || err)}`);
        }
      }

      if (journalContent) {
        const header = "[JOURNAL - Full Soul Mode]\n"
          + "These are your recent journal reflections. They are part of your inner life.\n";
        prepend = prepend ? `${prepend}\n\n${header}${journalContent}` : `${header}${journalContent}`;
        console.log(`[quaid] Full soul mode: injected ${journalFiles.length} journal files`);
      }
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid] Journal injection failed (non-fatal): ${(err as Error).message}`);
    }
    return prepend;
  }

  // -------------------------------------------------------------------------
  // Stub helper
  // -------------------------------------------------------------------------

  function notImplemented(name: string): never {
    throw new Error(`[quaid][facade] ${name} is not yet implemented — scheduled for a future PR`);
  }

  // -------------------------------------------------------------------------
  // Project context injection for OC before_agent_start
  // -------------------------------------------------------------------------

  function _stripInjectedToolsDomainBlock(docFile: string, content: string): string {
    if (docFile !== "TOOLS.md") {
      return content;
    }
    return content.replace(TOOLS_DOMAIN_BLOCK_RE, "").replace(/\n{3,}/g, "\n\n").trim();
  }

  async function _buildRuntimeContextBlock(): Promise<string> {
    try {
      return String(await deps.execPython("system-context-metadata", []) || "").trim();
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      return "";
    }
  }

  async function injectProjectContext(existingContext?: string): Promise<string | undefined> {
    let prepend = existingContext;
    try {
      const sections: string[] = [];

      // Identity files: USER.md, SOUL.md, ENVIRONMENT.md from the instance identity dir.
      const identityDir = path.join(deps.instanceRoot || deps.workspace, "identity");
      for (const idFile of ["USER.md", "SOUL.md", "ENVIRONMENT.md"]) {
        const filePath = path.join(identityDir, idFile);
        if (fs.existsSync(filePath)) {
          try {
            const content = fs.readFileSync(filePath, "utf8").trim();
            if (content) sections.push(`--- ${idFile} ---\n${content}`);
          } catch { /* skip unreadable */ }
        }
      }

      // Project docs: TOOLS.md + AGENTS.md for every project in projects/.
      const projectsDir = path.join(deps.workspace, "shared", "projects");
      let subdirs: string[] = [];
      try {
        subdirs = fs.readdirSync(projectsDir)
          .filter((name: string) => {
            try { return fs.statSync(path.join(projectsDir, name)).isDirectory() && !name.startsWith("."); }
            catch { return false; }
          })
          .sort((a: string, b: string) => a === "quaid" ? -1 : b === "quaid" ? 1 : a.localeCompare(b));
      } catch { /* no projects dir yet */ }

      for (const projectName of subdirs) {
        for (const docFile of ["TOOLS.md", "AGENTS.md"]) {
          const filePath = path.join(projectsDir, projectName, docFile);
          if (fs.existsSync(filePath)) {
            try {
              const content = _stripInjectedToolsDomainBlock(docFile, fs.readFileSync(filePath, "utf8").trim());
              if (content) sections.push(`--- ${projectName}/${docFile} ---\n${content}`);
            } catch { /* skip unreadable */ }
          }
        }
      }

      if (sections.length === 0) return prepend;
      const runtimeMeta = await _buildRuntimeContextBlock();
      const combined = "# Quaid Context\n\n" + runtimeMeta + "\n" + sections.join("\n\n") + "\n";
      prepend = prepend ? `${prepend}\n\n${combined}` : combined;
    } catch (err: unknown) {
      if (deps.isFailHardEnabled()) throw err;
      console.warn(`[quaid] Project context injection failed: ${(err as Error)?.message || String(err)}`);
    }
    return prepend;
  }

  // -------------------------------------------------------------------------
  // Build and return the facade object
  // -------------------------------------------------------------------------

  return {
    // Pass-through
    getConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    isFailHardEnabled: deps.isFailHardEnabled,
    initializeDatastoreIfMissing,
    getCaptureTimeoutMinutes,
    isInternalQuaidSession,
    resolveTierModel,
    resolveOwner,
    shouldNotifyFeature,
    shouldNotifyProjectCreate,
    isPluginStrictMode,
    isPreInjectionPassEnabled,
    shouldEmitExtractionNotify,
    clearExtractionNotifyHistory: () => extractionNotifyHistory.clear(),
    listRecentSessionsFromExtractionLog,
    updateExtractionLog,
    getInjectionLogPath,
    pruneInjectionLogFiles,

    // Datastore
    stats: () => datastoreBridge.stats(),
    getStatsParsed,
    store: (args) => datastoreBridge.store(args),
    forget: (args) => datastoreBridge.forget(args),

    // Tool hint planner — delegates to knowledge engine (orchestration layer).
    planToolHint: (query: string): Promise<string | null> => knowledgeEngine.planToolHint(query),
    searchBySession: (sessionId, limit = 20) => datastoreBridge.recall([
      "*",
      "--session-id",
      sessionId,
      "--owner",
      resolveOwner(),
      "--limit",
      String(limit),
    ]),

    // Events
    emitEvent: async (name, payload, dispatch = "auto") => {
      const args = [
        "--name",
        name,
        "--payload",
        JSON.stringify(payload || {}),
        "--source",
        String(deps.eventSource || "adapter"),
        "--dispatch",
        dispatch,
      ];
      const out = await deps.execEvents("emit", args);
      let parsed: unknown = null;
      try {
        parsed = JSON.parse(out || "{}");
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        throw new Error(`[quaid][facade] events emit returned invalid JSON: ${msg}`);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("[quaid][facade] events emit returned non-object payload");
      }
      return parsed as Record<string, unknown>;
    },

    // Recall
    recall,
    recallWithDiagnostics,
    recallWithToolRetry,
    formatMemoriesForInjection,
    formatRecallToolResponse,
    buildRecallNotificationPayload,
    prepareAutoInjectionContext,
    buildExtractionCompletionNotificationPayload,
    shouldNotifyExtractionStart,
    injectFullJournalContext,
    injectProjectContext,
    isLowQualityQuery,
    filterMemoriesByPrivacy,
    loadInjectedMemoryKeys,
    saveInjectedMemoryKeys,
    resetInjectionDedupAfterCompaction,
    queueCompactionExtractionSummary,

    // Dynamic K
    computeDynamicK,
    getActiveNodeCount,

    // Docs
    docsRead: (identifier) => deps.execDocsRegistry("read", [identifier]),
    docsList: (args) => deps.execDocsRegistry("list", args),
    docsRegister: (args) => deps.execDocsRegistry("register", args),
    docsCreateProject: (args) => deps.execDocsRegistry("create-project", args),
    docsListProjects: (args = ["--json"]) => deps.execDocsRegistry("list-projects", args),
    docsCheckStaleness: () => deps.execDocsUpdater("check", ["--json"]),
    getDocsStalenessWarning,
    loadProjectMarkdown,
    buildDocsSearchNotificationPayload,

    // Memory notes
    addMemoryNote,
    hasPendingMemoryNotes,
    getAndClearMemoryNotes,
    runExtractionPipeline,

    // Project catalog
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    getProjectNames: () => projectCatalogReader.getProjectNames(),

    // Guidance
    renderDatastoreGuidance: renderKnowledgeDatastoreGuidanceForAgents,
    getMessageText,
    extractSessionId,
    parseSessionIdFromTranscriptPath,
    readSessionMessagesFile: (sessionFile: string) => deps.readSessionMessagesFile?.(sessionFile) || readMessagesFromSessionJsonl(sessionFile),
    resolveMemoryStoreSessionId,
    resolveLifecycleHookSessionId,
    readTimeoutSessionMessages,
    listTimeoutSessionActivity,
    resolveSessionForCompaction,
    maybeForceCompactionAfterTimeout,
    filterConversationMessages,
    buildTranscript,
    isResetBootstrapOnlyConversation,
    isVectorRecallResult,
    updateDocsFromTranscript,

    // Stubs
    detectLifecycleSignal,
    latestMessageTimestampMs,
    hasExplicitLifecycleUserCommand,
    isBacklogLifecycleReplay,
    shouldProcessLifecycleSignal,
    markLifecycleSignalFromHook,
    clearLifecycleSignalHistory: () => lifecycleSignalHistory.clear(),
    processLifecycleEvent: () => notImplemented("processLifecycleEvent"),
    maybeRunMaintenance: () => notImplemented("maybeRunMaintenance"),
    getJanitorHealthIssue,
    queueExtraction,
    getQueuedExtractionPromise,
    queueDelayedRequest,
    maybeQueueJanitorHealthAlert,
    collectJanitorNudges,
    isInternalMaintenancePrompt,
    resolveExtractionTrigger,
  };
}
