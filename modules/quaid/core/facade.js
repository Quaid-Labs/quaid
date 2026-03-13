import { createDatastoreBridge } from "./datastore-bridge.js";
import { createProjectCatalogReader } from "./project-catalog.js";
import { createKnowledgeEngine } from "./knowledge-engine.js";
import {
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents
} from "./knowledge-stores.js";
import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
const FAST_ROUTER_TIMEOUT_MS = 45e3;
const DEEP_ROUTER_TIMEOUT_MS = 6e4;
const NODE_COUNT_CACHE_MS = 12e4;
const DATASTORE_STATS_TIMEOUT_MS = 3e4;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_MEMORY_NOTE_SESSIONS = 200;
const EXTRACTION_NOTIFY_DEDUPE_MS = 9e4;
const MAX_EXTRACTION_LOG_ENTRIES = 800;
const MAX_INJECTION_LOG_FILES = 400;
const DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS = 50;
const DELAYED_REQUESTS_LOCK_SLEEP_MS = 10;
const COMPACTION_NOTIFY_BATCH_MS = 1e4;
const COMPACTION_NOTIFY_BATCH_MAX_MS = 45e3;
const RECALL_RETRY_STOPWORDS = /* @__PURE__ */ new Set([
  "a",
  "an",
  "and",
  "are",
  "as",
  "at",
  "be",
  "by",
  "do",
  "for",
  "from",
  "how",
  "i",
  "in",
  "is",
  "it",
  "me",
  "my",
  "of",
  "on",
  "or",
  "our",
  "that",
  "the",
  "their",
  "they",
  "this",
  "to",
  "was",
  "we",
  "what",
  "when",
  "where",
  "which",
  "who",
  "why",
  "with",
  "you",
  "your"
]);
function createQuaidFacade(deps) {
  const datastoreBridge = createDatastoreBridge(deps.execPython);
  const projectCatalogReader = createProjectCatalogReader({
    workspace: deps.workspace,
    fs,
    path,
    getMemoryConfig: deps.getMemoryConfig,
    isFailHardEnabled: deps.isFailHardEnabled
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
    recallMemory: async (query, limit, opts) => {
      return recallMemoryFromBridge(query, limit, opts);
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
    }
  });
  let _cachedDatastoreStats = null;
  let _datastoreStatsTimestamp = 0;
  let _cachedNodeCount = null;
  let _nodeCountTimestamp = 0;
  const lifecycleSignalHistory = /* @__PURE__ */ new Map();
  const extractionNotifyHistory = /* @__PURE__ */ new Map();
  let compactionNotifyBatchState = null;
  let extractionPromise = null;
  let timeoutSessionStoreCache = null;
  function queueExtraction(task, source) {
    const prior = extractionPromise || Promise.resolve();
    extractionPromise = prior.then(
      () => task(),
      async (err) => {
        const msg = err?.message || String(err);
        console.error(`[quaid][facade] extraction chain prior failure (${source}): ${msg}`);
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        await task();
        return;
      }
    );
    return extractionPromise;
  }
  function getQueuedExtractionPromise() {
    return extractionPromise;
  }
  function initializeDatastoreIfMissing() {
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
  function resolveOwner(speaker, channel) {
    const usersCfg = deps.getMemoryConfig()?.users;
    const config = usersCfg && typeof usersCfg === "object" && !Array.isArray(usersCfg) ? usersCfg : { defaultOwner: "quaid", identities: {} };
    const identities = config.identities && typeof config.identities === "object" && !Array.isArray(config.identities) ? config.identities : {};
    const defaultOwner = typeof config.defaultOwner === "string" && config.defaultOwner.trim() ? config.defaultOwner.trim() : "quaid";
    for (const [userId, identity] of Object.entries(identities)) {
      if (!identity || typeof identity !== "object") continue;
      const speakers = Array.isArray(identity.speakers) ? identity.speakers : [];
      if (speaker && speakers.some((s) => String(s || "").toLowerCase() === speaker.toLowerCase())) {
        return userId;
      }
      const channels = identity.channels && typeof identity.channels === "object" ? identity.channels : {};
      if (channel && Array.isArray(channels[channel])) {
        const allowed = channels[channel];
        if (allowed.some((a) => String(a || "") === "*")) return userId;
        if (speaker && allowed.some((a) => String(a || "").toLowerCase() === speaker.toLowerCase())) {
          return userId;
        }
      }
    }
    return defaultOwner;
  }
  function getCaptureTimeoutMinutes() {
    const capture = deps.getMemoryConfig().capture || {};
    const raw = capture.inactivityTimeoutMinutes ?? capture.inactivity_timeout_minutes ?? 120;
    const num = Number(raw);
    return Number.isFinite(num) ? Math.max(0, num) : 120;
  }
  function isInternalQuaidSession(sessionId) {
    const sid = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!sid) return false;
    return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
  }
  function normalizeProvider(provider) {
    return String(provider || "").trim().toLowerCase();
  }
  function providerClassLookupKey(provider) {
    const normalized = normalizeProvider(provider);
    const aliases = deps.providerAliases && typeof deps.providerAliases === "object" ? deps.providerAliases : {};
    const mapped = aliases[normalized];
    if (typeof mapped === "string" && mapped.trim()) {
      return normalizeProvider(mapped);
    }
    return normalized;
  }
  function getConfiguredTierValue(tier) {
    const key = tier === "fast" ? "fastReasoning" : "deepReasoning";
    const configured = deps.getMemoryConfig().models?.[key];
    if (typeof configured === "string" && configured.trim().length > 0) {
      return configured.trim();
    }
    throw new Error(`Missing models.${key} in memory config`);
  }
  function getConfiguredTierProvider(tier) {
    const key = tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider";
    const configured = deps.getMemoryConfig().models?.[key];
    if (typeof configured === "string" && configured.trim().length > 0) {
      return normalizeProvider(configured.trim());
    }
    return "default";
  }
  function parseTierModelClassMap(tier) {
    const models = deps.getMemoryConfig().models || {};
    const raw = tier === "fast" ? models.fastReasoningModelClasses : models.deepReasoningModelClasses;
    const out = {};
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
  function getDefaultLLMProvider() {
    try {
      return normalizeProvider(String(deps.getDefaultLLMProvider?.() || ""));
    } catch (err) {
      console.warn(`[quaid][facade] default provider callback failed: ${String(err?.message || err)}`);
      return "";
    }
  }
  function getEffectiveProvider() {
    const configuredProvider = normalizeProvider(String(deps.getMemoryConfig().models?.llmProvider || ""));
    if (configuredProvider && configuredProvider !== "default") {
      return configuredProvider;
    }
    const defaultProvider = getDefaultLLMProvider();
    if (defaultProvider) {
      return defaultProvider;
    }
    throw new Error(
      "models.llmProvider is 'default' but no active default provider was resolved. Set models.llmProvider explicitly (anthropic/openai/openai-compatible/claude-code)."
    );
  }
  function getEffectiveTierProvider(tier) {
    const tierProvider = getConfiguredTierProvider(tier);
    if (tierProvider && tierProvider !== "default") {
      return tierProvider;
    }
    return getEffectiveProvider();
  }
  function resolveTierModel(tier) {
    const rawTierValue = getConfiguredTierValue(tier);
    const configuredTierProvider = getConfiguredTierProvider(tier);
    const effectiveTierProvider = getEffectiveTierProvider(tier);
    if (rawTierValue !== "default") {
      if (rawTierValue.includes("/")) {
        const [provider, ...modelParts] = rawTierValue.split("/");
        const normalizedProvider = normalizeProvider(provider);
        if (configuredTierProvider !== "default" && providerClassLookupKey(normalizedProvider) !== providerClassLookupKey(configuredTierProvider)) {
          throw new Error(
            `models.${tier === "fast" ? "fastReasoning" : "deepReasoning"} provider "${normalizedProvider}" does not match models.${tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider"}="${configuredTierProvider}"`
          );
        }
        return {
          provider: normalizedProvider,
          model: modelParts.join("/").trim()
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
      model: mappedModel
    };
  }
  function effectiveNotificationLevel(feature) {
    const notifications = deps.getMemoryConfig().notifications || {};
    const featureConfig = notifications[feature];
    if (featureConfig && typeof featureConfig === "object" && typeof featureConfig.verbosity === "string") {
      return featureConfig.verbosity.trim().toLowerCase();
    }
    const level = String(notifications.level || "normal").trim().toLowerCase();
    const defaults = {
      quiet: { janitor: "off", extraction: "off", retrieval: "off" },
      normal: { janitor: "summary", extraction: "summary", retrieval: "off" },
      verbose: { janitor: "full", extraction: "summary", retrieval: "summary" },
      debug: { janitor: "full", extraction: "full", retrieval: "full" }
    };
    const levelDefaults = defaults[level] || defaults.normal;
    return String(levelDefaults[feature] || "off").toLowerCase();
  }
  function shouldNotifyFeature(feature, detail = "summary") {
    const effective = effectiveNotificationLevel(feature);
    if (effective === "off") return false;
    if (detail === "summary") return effective === "summary" || effective === "full";
    return effective === "full";
  }
  function shouldNotifyProjectCreate() {
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
  function isPluginStrictMode() {
    const plugins = deps.getMemoryConfig().plugins || {};
    const raw = plugins.strict;
    if (raw === void 0) return true;
    if (raw === null) return false;
    if (typeof raw === "number") return raw !== 0;
    if (typeof raw === "string") return raw.length > 0;
    if (Array.isArray(raw)) return raw.length > 0;
    if (typeof raw === "object") return Object.keys(raw).length > 0;
    return !!raw;
  }
  function isPreInjectionPassEnabled() {
    const retrieval = deps.getMemoryConfig().retrieval || {};
    if (typeof retrieval.preInjectionPass === "boolean") return retrieval.preInjectionPass;
    if (typeof retrieval.pre_injection_pass === "boolean") return retrieval.pre_injection_pass;
    return true;
  }
  function shouldEmitExtractionNotify(key, now = Date.now()) {
    for (const [k, ts] of extractionNotifyHistory.entries()) {
      if (now - ts > EXTRACTION_NOTIFY_DEDUPE_MS) {
        extractionNotifyHistory.delete(k);
      }
    }
    const prior = extractionNotifyHistory.get(key);
    extractionNotifyHistory.set(key, now);
    if (!prior) return true;
    return now - prior > EXTRACTION_NOTIFY_DEDUPE_MS;
  }
  function isMissingFileError(err) {
    const code = err?.code;
    return code === "ENOENT" || code === "ENOTDIR";
  }
  function trimExtractionLogEntries(log, maxEntries = MAX_EXTRACTION_LOG_ENTRIES) {
    const entries = Object.entries(log || {});
    if (entries.length <= maxEntries) {
      return log || {};
    }
    const sorted = entries.map(([sid, payload]) => ({
      sid,
      payload,
      ts: Date.parse(String(payload?.last_extracted_at || "")) || 0
    })).sort((a, b) => b.ts - a.ts).slice(0, maxEntries);
    return Object.fromEntries(sorted.map((row) => [row.sid, row.payload]));
  }
  function listRecentSessionsFromExtractionLog(limit = 5) {
    const extractionLogPath = path.join(deps.workspace, "data", "extraction-log.json");
    let extractionLog = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("extraction log must be a JSON object");
      }
      extractionLog = parsed;
    } catch (err) {
      if (deps.isFailHardEnabled() && !isMissingFileError(err)) {
        throw new Error("[quaid][facade] extraction log read failed under failHard", { cause: err });
      }
      console.warn(`[quaid][facade] extraction log read failed: ${String(err?.message || err)}`);
      return [];
    }
    return Object.entries(extractionLog).filter(([, v]) => v && v.last_extracted_at).sort(([, a], [, b]) => (b.last_extracted_at || "").localeCompare(a.last_extracted_at || "")).slice(0, Math.min(Math.max(Math.floor(Number(limit) || 5), 1), 20)).map(([sessionId, info]) => ({
      sessionId,
      lastExtractedAt: String(info.last_extracted_at || ""),
      messageCount: Number(info.message_count || 0),
      label: String(info.label || "unknown"),
      topicHint: String(info.topic_hint || "")
    }));
  }
  function updateExtractionLog(sessionId, messages, label) {
    const extractionLogPath = path.join(deps.workspace, "data", "extraction-log.json");
    let extractionLog = {};
    try {
      const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("extraction log must be a JSON object");
      }
      extractionLog = parsed;
    } catch (err) {
      if (deps.isFailHardEnabled() && !isMissingFileError(err)) {
        throw new Error("[quaid][facade] extraction log read failed under failHard", { cause: err });
      }
      console.warn(`[quaid][facade] extraction log read failed: ${String(err?.message || err)}`);
    }
    let topicHint = "";
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      if (String(msg.role || "") !== "user") continue;
      const cleaned = getMessageText(msg).trim();
      if (shouldSkipUserText(cleaned)) continue;
      topicHint = cleaned.slice(0, 120);
      break;
    }
    extractionLog[String(sessionId || "unknown")] = {
      last_extracted_at: (/* @__PURE__ */ new Date()).toISOString(),
      message_count: Array.isArray(messages) ? messages.length : 0,
      label,
      topic_hint: topicHint
    };
    const trimmed = trimExtractionLogEntries(extractionLog, MAX_EXTRACTION_LOG_ENTRIES);
    fs.writeFileSync(extractionLogPath, JSON.stringify(trimmed, null, 2), { mode: 384 });
  }
  const INJECTION_LOG_DIR = path.join(deps.workspace, ".quaid", "runtime", "injection");
  function getInjectionLogPath(sessionId) {
    return path.join(INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
  }
  function pruneInjectionLogFiles() {
    try {
      const files = fs.readdirSync(INJECTION_LOG_DIR).filter((f) => f.startsWith("memory-injection-") && f.endsWith(".log")).map((f) => ({
        full: path.join(INJECTION_LOG_DIR, f),
        mtimeMs: fs.statSync(path.join(INJECTION_LOG_DIR, f)).mtimeMs
      })).sort((a, b) => b.mtimeMs - a.mtimeMs);
      for (const stale of files.slice(MAX_INJECTION_LOG_FILES)) {
        try {
          fs.unlinkSync(stale.full);
        } catch (err) {
          console.warn(`[quaid][facade] Failed pruning stale injection log ${stale.full}: ${String(err?.message || err)}`);
        }
      }
    } catch (err) {
      if (isMissingFileError(err)) {
        return;
      }
      console.warn(`[quaid][facade] Injection log pruning failed: ${String(err?.message || err)}`);
    }
  }
  function readDelayedRequestsJson(pathname) {
    try {
      if (!fs.existsSync(pathname)) return null;
      return JSON.parse(fs.readFileSync(pathname, "utf8"));
    } catch (err) {
      console.warn(`[quaid][facade] delayed requests read failed path=${pathname}: ${String(err?.message || err)}`);
      return null;
    }
  }
  function writeDelayedRequestsJson(pathname, payload) {
    const tmpPath = `${pathname}.tmp-${process.pid}-${Date.now()}`;
    fs.mkdirSync(path.dirname(pathname), { recursive: true });
    fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), { mode: 384 });
    fs.renameSync(tmpPath, pathname);
  }
  function withDelayedRequestsLock(requestsPath, fn) {
    const lockPath = `${requestsPath}.lock`;
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    let fd;
    let lastErr;
    for (let attempt = 0; attempt < DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS; attempt += 1) {
      try {
        fd = fs.openSync(lockPath, "wx", 384);
        break;
      } catch (err) {
        const code = err?.code;
        if (code !== "EEXIST") throw err;
        lastErr = err;
        _sleepMs(DELAYED_REQUESTS_LOCK_SLEEP_MS);
      }
    }
    if (fd === void 0) {
      throw new Error(`failed to acquire delayed-requests lock: ${String(lastErr?.message || lastErr)}`);
    }
    try {
      return fn();
    } finally {
      try {
        fs.closeSync(fd);
      } catch {
      }
      try {
        fs.unlinkSync(lockPath);
      } catch {
      }
    }
  }
  function makeDelayedRequestId(kind, message) {
    return `${kind}-${Buffer.from(message).toString("base64").slice(0, 16)}`;
  }
  function queueDelayedRequest(request) {
    const requestsPath = String(
      request?.requestsPath || path.join(deps.workspace, ".quaid", "runtime", "notes", "delayed-llm-requests.json")
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
        const payload = loaded && typeof loaded === "object" && !Array.isArray(loaded) ? loaded : { version: 1, requests: [] };
        const requests = Array.isArray(payload.requests) ? payload.requests : [];
        const id = makeDelayedRequestId(kind, message);
        if (requests.some((r) => r && String(r.id || "") === id && r.status === "pending")) {
          return false;
        }
        requests.push({
          id,
          created_at: (/* @__PURE__ */ new Date()).toISOString(),
          source,
          kind,
          priority,
          status: "pending",
          message
        });
        payload.version = 1;
        payload.requests = requests;
        writeDelayedRequestsJson(requestsPath, payload);
        return true;
      });
    } catch (err) {
      const detail = `[quaid][facade] delayed requests queue failed path=${requestsPath}: ${String(err?.message || err)}`;
      if (deps.isFailHardEnabled()) {
        const cause = err instanceof Error ? err : new Error(String(err));
        throw new Error(detail, { cause });
      }
      console.warn(detail);
      return false;
    }
  }
  function readObjectFile(filePath) {
    try {
      if (!fs.existsSync(filePath)) {
        return {};
      }
      const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        return {};
      }
      return parsed;
    } catch (err) {
      console.warn(`[quaid][facade] failed reading JSON state ${filePath}: ${String(err?.message || err)}`);
      return {};
    }
  }
  function writeObjectFile(filePath, state) {
    try {
      fs.mkdirSync(path.dirname(filePath), { recursive: true });
      fs.writeFileSync(filePath, JSON.stringify(state, null, 2), { mode: 384 });
    } catch (err) {
      console.warn(`[quaid][facade] failed writing JSON state ${filePath}: ${String(err?.message || err)}`);
    }
  }
  function getDatastoreStatsSync(maxAgeMs = NODE_COUNT_CACHE_MS) {
    const now = Date.now();
    if (now - _datastoreStatsTimestamp < maxAgeMs) {
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
    } catch (err) {
      const msg = `[quaid][facade] datastore stats read failed: ${err?.message || String(err)}`;
      if (deps.isFailHardEnabled()) {
        throw new Error(msg, { cause: err instanceof Error ? err : new Error(String(err)) });
      }
      _cachedDatastoreStats = null;
      _datastoreStatsTimestamp = now;
      console.warn(msg);
      return null;
    }
  }
  function getActiveNodeCount() {
    const now = Date.now();
    if (_cachedNodeCount !== null && now - _nodeCountTimestamp < NODE_COUNT_CACHE_MS) {
      return _cachedNodeCount;
    }
    let stats = null;
    try {
      stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
    } catch (err) {
      console.warn(`[quaid][facade] active node stats probe failed; using fallback=100: ${String(err?.message || err)}`);
    }
    const active = Number(stats?.active_nodes ?? stats?.by_status?.active ?? 0);
    if (Number.isFinite(active) && active > 0) {
      _cachedNodeCount = active;
      _nodeCountTimestamp = now;
      return _cachedNodeCount;
    }
    if (_cachedNodeCount === null) {
      console.warn("[quaid][facade] unable to derive active node count; using fallback=100");
    }
    return _cachedNodeCount ?? 100;
  }
  function getJanitorHealthIssue() {
    const stats = getDatastoreStatsSync(60 * 1e3);
    const completedAt = String(stats?.last_janitor_completed_at || "").trim();
    if (!completedAt) {
      return "[Quaid] Janitor has never run. Please run janitor and ensure schedule is active.";
    }
    const ts = Date.parse(completedAt);
    if (Number.isNaN(ts)) return null;
    const hours = (Date.now() - ts) / (1e3 * 60 * 60);
    if (hours > 72) {
      return `[Quaid] Janitor appears unhealthy (last successful run ${Math.floor(hours)}h ago). Diagnose scheduler/run path and run janitor.`;
    }
    if (hours > 48) {
      return `[Quaid] Janitor may be delayed (last successful run ${Math.floor(hours)}h ago). Verify schedule and run status.`;
    }
    return null;
  }
  function maybeQueueJanitorHealthAlert(options) {
    const issue = getJanitorHealthIssue();
    if (!issue) return false;
    const now = Number(options?.nowMs || Date.now());
    const cooldown = Math.max(1, Number(options?.cooldownMs || 6 * 60 * 60 * 1e3));
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
      source: String(deps.adapterName || "adapter")
    });
    if (!queued) return false;
    state.lastJanitorHealthAlertAt = now;
    state.lastJanitorHealthIssue = issue;
    writeObjectFile(statePath, state);
    return true;
  }
  function collectJanitorNudges(options) {
    const now = Number(options?.nowMs || Date.now());
    const cooldown = Math.max(1, Number(options?.cooldownMs || 6 * 60 * 60 * 1e3));
    const statePath = String(options?.statePath || "").trim();
    if (!statePath) return [];
    const state = readObjectFile(statePath);
    const nudges = [];
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
    } catch (err) {
      console.warn(`[quaid][facade] install nudge check failed: ${String(err?.message || err)}`);
    }
    try {
      if (fs.existsSync(options.pendingApprovalRequestsPath)) {
        const raw = readObjectFile(options.pendingApprovalRequestsPath);
        const requests = Array.isArray(raw?.requests) ? raw.requests : [];
        const pendingCount = requests.filter((r) => r?.status === "pending").length;
        const lastApprovalNudge = Number(state.lastApprovalNudgeAt || 0);
        if (pendingCount > 0 && now - lastApprovalNudge > cooldown) {
          nudges.push(`Quaid has ${pendingCount} pending approval request(s). Review pending maintenance approvals.`);
          state.lastApprovalNudgeAt = now;
          changed = true;
        }
      }
    } catch (err) {
      console.warn(`[quaid][facade] approval nudge check failed: ${String(err?.message || err)}`);
    }
    if (changed) {
      writeObjectFile(statePath, state);
    }
    return nudges;
  }
  function parseDatastoreStats(raw) {
    let parsed = null;
    try {
      parsed = JSON.parse(raw || "{}");
    } catch {
      return null;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    const totalNodes = Number(parsed.total_nodes);
    const edges = Number(parsed.edges);
    const activeNodes = Number(parsed.active_nodes);
    if (!Number.isFinite(totalNodes) || totalNodes < 0) {
      return null;
    }
    if (!Number.isFinite(edges) || edges < 0) {
      return null;
    }
    return {
      total_nodes: totalNodes,
      edges,
      ...Number.isFinite(activeNodes) && activeNodes >= 0 ? { active_nodes: activeNodes } : {}
    };
  }
  async function getStatsParsed() {
    try {
      const output = await datastoreBridge.stats();
      return parseDatastoreStats(output);
    } catch (err) {
      console.error("[quaid][facade] stats error:", err.message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      return null;
    }
  }
  function getMessageText(msg) {
    if (!msg || typeof msg !== "object") return "";
    const candidate = msg.content ?? msg.text ?? msg.message;
    if (typeof candidate === "string") return candidate;
    if (Array.isArray(candidate)) {
      return candidate.map((part) => {
        if (!part || typeof part !== "object") return "";
        const text = part.text;
        return typeof text === "string" ? text : "";
      }).join(" ");
    }
    return "";
  }
  function shouldSkipUserText(text) {
    const candidate = String(text || "");
    if (!candidate.trim()) return true;
    const skip = deps.transcriptFormat?.shouldSkipText;
    if (typeof skip === "function") {
      try {
        return skip("user", candidate);
      } catch (err) {
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][facade] transcript skip callback failed: ${String(err?.message || err)}`);
      }
    }
    if (candidate.includes('"kind": "restart"')) return true;
    return false;
  }
  function extractSessionId(messages, ctx) {
    const context = ctx && typeof ctx === "object" ? ctx : {};
    const direct = String(context.sessionId || "").trim();
    if (direct) {
      return direct;
    }
    let firstTimestamp = "";
    const filteredMessages = Array.isArray(messages) ? messages.filter((msg) => {
      if (!msg || typeof msg !== "object") return false;
      if (String(msg.role || "") !== "user") return false;
      const content = getMessageText(msg).trim();
      return !shouldSkipUserText(content);
    }) : [];
    if (filteredMessages.length > 0) {
      const firstMessage = filteredMessages[0];
      const rawTs = firstMessage.timestamp;
      firstTimestamp = rawTs ? String(rawTs) : Date.now().toString();
    } else {
      firstTimestamp = Date.now().toString();
    }
    return createHash("md5").update(firstTimestamp).digest("hex").substring(0, 12);
  }
  function parseSessionIdFromTranscriptPath(sessionFile) {
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
  function resolveMemoryStoreSessionId(ctx) {
    const context = ctx && typeof ctx === "object" ? ctx : {};
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
  function resolveLifecycleHookSessionId(event, ctx, messages) {
    const eventObj = event && typeof event === "object" ? event : {};
    const context = ctx && typeof ctx === "object" ? ctx : {};
    const direct = String(eventObj.sessionId || context.sessionId || "").trim();
    if (direct) {
      return direct;
    }
    const fromEventEntry = String(
      eventObj.sessionEntry?.sessionId || eventObj.previousSessionEntry?.sessionId || ""
    ).trim();
    if (fromEventEntry) {
      return fromEventEntry;
    }
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
    return extractSessionId(messages, ctx);
  }
  function readMessagesFromSessionJsonl(sessionFile) {
    const content = fs.readFileSync(sessionFile, "utf8");
    const lines = content.trim().split("\n");
    const messages = [];
    for (const line of lines) {
      try {
        const entry = JSON.parse(line);
        if (entry.type === "message" && entry.message) {
          messages.push(entry.message);
        } else if (entry.role) {
          messages.push(entry);
        }
      } catch (err) {
        console.warn(`[quaid][facade] session JSONL parse failed: ${String(err?.message || err)}`);
      }
    }
    return messages;
  }
  function readTimeoutSessionStore() {
    const storePath = String(deps.timeoutSessionStorePath?.() || "").trim();
    if (!storePath) return {};
    try {
      if (!fs.existsSync(storePath)) return {};
      const stat = fs.statSync(storePath);
      const mtimeMs = Number.isFinite(stat.mtimeMs) ? stat.mtimeMs : 0;
      if (timeoutSessionStoreCache && timeoutSessionStoreCache.path === storePath && timeoutSessionStoreCache.mtimeMs === mtimeMs) {
        return timeoutSessionStoreCache.data;
      }
      const raw = JSON.parse(fs.readFileSync(storePath, "utf8"));
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
      const data = raw;
      timeoutSessionStoreCache = { path: storePath, mtimeMs, data };
      return data;
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid][timeout] session store read failed: ${String(err?.message || err)}`);
      return {};
    }
  }
  function parseTimeoutSessionUpdatedAtMs(entry) {
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
  function resolveTimeoutSessionTranscriptPath(entry, sessionId) {
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
  function readTimeoutSessionMessages(sessionId) {
    const sid = String(sessionId || "").trim();
    if (!sid) return [];
    const store = readTimeoutSessionStore();
    const entries = Object.entries(store || {});
    for (const [, entry] of entries) {
      if (String(entry?.sessionId || "").trim() !== sid) continue;
      const transcriptPath = resolveTimeoutSessionTranscriptPath(entry, sid);
      if (!transcriptPath) {
        console.log(`[quaid][timeout][source] session=${sid} transcript=none store_entry=true`);
        return [];
      }
      const rows2 = deps.readSessionMessagesFile?.(transcriptPath) || readMessagesFromSessionJsonl(transcriptPath);
      const count2 = Array.isArray(rows2) ? rows2.length : 0;
      console.log(`[quaid][timeout][source] session=${sid} transcript=${transcriptPath} store_entry=true rows=${count2}`);
      return rows2;
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
  function listTimeoutSessionActivity() {
    const store = readTimeoutSessionStore();
    const rows = [];
    const seen = /* @__PURE__ */ new Set();
    const entries = Object.entries(store || {});
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
      } catch (err) {
        if (deps.isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][timeout] session mtime read failed for ${sid}: ${String(err?.message || err)}`);
      }
    }
    for (const dirRaw of deps.timeoutSessionTranscriptDirs?.() || []) {
      const dir = String(dirRaw || "").trim();
      if (!dir) continue;
      let names = [];
      try {
        names = fs.readdirSync(dir);
      } catch (err) {
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
        } catch (err) {
          continue;
        }
      }
    }
    return rows;
  }
  function resolveSessionForCompaction(sessionId) {
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
  function maybeForceCompactionAfterTimeout(sessionId) {
    const captureCfg = deps.getMemoryConfig().capture || {};
    const enabled = Boolean(
      captureCfg.autoCompactionOnTimeout ?? captureCfg.auto_compaction_on_timeout ?? true
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
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid][timeout] auto-compaction failed for key=${key}: ${String(err?.message || err)}`);
    }
  }
  function filterConversationMessages(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return [];
    return messages.filter((msg) => {
      if (!msg || typeof msg !== "object") return false;
      const role = String(msg.role || "");
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
              if (onlyExtractionKeys && Array.isArray(parsed.facts)) return false;
            }
          } catch {
          }
        }
      }
      return true;
    });
  }
  function buildTranscript(messages) {
    const transcript = [];
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg;
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
      const speaker = typeof speakerLabel === "function" ? speakerLabel(role) : role === "user" ? "User" : "Assistant";
      transcript.push(`${speaker}: ${text}`);
    }
    return transcript.join("\n\n");
  }
  function isResetBootstrapOnlyConversation(messages, bootstrapPrompt = "A new session was started via /new or /reset.") {
    const userTexts = messages.filter((msg) => msg && typeof msg === "object" && String(msg.role || "") === "user").map((msg) => getMessageText(msg).trim()).filter(Boolean);
    if (userTexts.length === 0) return false;
    const nonBootstrapUserTexts = userTexts.filter((text) => !text.startsWith(bootstrapPrompt));
    return nonBootstrapUserTexts.length === 0;
  }
  async function updateDocsFromTranscript(messages, label, sessionId, tempDir = path.join(deps.workspace, ".quaid", "tmp")) {
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
      `docs-ingest-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`
    );
    fs.writeFileSync(tmpPath, fullTranscript, { mode: 384 });
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
          session_id: sessionId || null
        }),
        "--source",
        String(deps.eventSource || "adapter"),
        "--dispatch",
        "immediate"
      ]);
      let parsed = {};
      try {
        const candidate = JSON.parse(out || "{}");
        if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
          parsed = candidate;
        }
      } catch {
      }
      const processed = parsed.processed;
      const details = Array.isArray(processed?.details) ? processed?.details : [];
      const first = details[0] && typeof details[0] === "object" ? details[0] : {};
      const resultObj = first.result && typeof first.result === "object" ? first.result : {};
      const nested = resultObj.result && typeof resultObj.result === "object" ? resultObj.result : resultObj;
      const status = String(nested.status || "").trim();
      const elapsed = ((Date.now() - startTime) / 1e3).toFixed(1);
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
    } catch (err) {
      console.error(`[quaid][facade] ${label} doc update failed:`, err.message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
    } finally {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
      }
    }
  }
  function detectExplicitLifecycleUserCommand(text) {
    if (!text) return null;
    const lines = String(text).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (lines.length !== 1) return null;
    const normalized = lines[0].replace(/\[\[[^\]]+\]\]\s*/g, "").trim();
    const m = normalized.match(/^(?:\[[^\]]+\]\s*)?\/(new|reset|restart|compact)(?=\s|$)/i);
    if (!m) return null;
    return `/${m[1].toLowerCase()}`;
  }
  function latestMessageTimestampMs(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    let latest = null;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg;
      const raw = rec.timestamp ?? rec.createdAt ?? rec.time ?? null;
      if (raw == null) continue;
      let ts = null;
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
  function hasExplicitLifecycleUserCommand(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return false;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      if (String(msg.role || "") !== "user") continue;
      const text = getMessageText(msg);
      if (!text) continue;
      if (detectExplicitLifecycleUserCommand(text)) return true;
    }
    return false;
  }
  function isBacklogLifecycleReplay(messages, trigger, nowMs, bootTimeMs, staleMs) {
    if (trigger !== "reset" && trigger !== "new" && trigger !== "recovery") return false;
    const latestTs = latestMessageTimestampMs(messages);
    if (latestTs == null) {
      return !hasExplicitLifecycleUserCommand(messages);
    }
    return latestTs < Math.min(nowMs, bootTimeMs) - staleMs;
  }
  function detectLifecycleSignal(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    const tail = messages.slice(-8);
    const tailOffset = messages.length - tail.length;
    for (let i = tail.length - 1; i >= 0; i--) {
      const msg = tail[i];
      if (!msg || typeof msg !== "object") continue;
      const role = String(msg.role || "").trim();
      const text = getMessageText(msg).trim();
      if (!text) continue;
      const normalized = text.replace(/\[\[[^\]]+\]\]\s*/g, "").replace(/^\[[^\]]+\]\s*/, "").trim();
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
            messageIndex: tailOffset + i
          };
        }
        if (command === "/compact") {
          return {
            label: "CompactionSignal",
            source: "user_command",
            signature: `cmd:${command}`,
            messageIndex: tailOffset + i
          };
        }
      }
      if (/^\/(new|reset|restart)(?:\s|$)/i.test(normalized)) {
        const command = normalized.match(/\/(new|reset|restart)/i)?.[0]?.toLowerCase() || "/new";
        return {
          label: "ResetSignal",
          source: "system_notice",
          signature: `cmd:${command}`,
          messageIndex: tailOffset + i
        };
      }
      if (/^\/compact(?:\s|$)/i.test(normalized)) {
        return {
          label: "CompactionSignal",
          source: "system_notice",
          signature: "cmd:/compact",
          messageIndex: tailOffset + i
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
            messageIndex: tailOffset + i
          };
        }
        if (normalizedLc.includes("new session was started via /new or /reset") || normalizedLc.includes("a new session was started via /new or /reset") || normalizedLc.includes("new session was started") && normalizedLc.includes("/new") || normalizedLc.includes("session startup sequence") && normalizedLc.includes("/new")) {
          return {
            label: "ResetSignal",
            source: "system_notice",
            signature: "system:new_session_started",
            messageIndex: tailOffset + i
          };
        }
      }
      return null;
    }
    return null;
  }
  function lifecycleSignalKey(sessionId, label) {
    return `${sessionId}:${label}`;
  }
  function shouldProcessLifecycleSignal(sessionId, signal, suppressMs = 15e3, retentionMs = 10 * 6e4) {
    const now = Date.now();
    for (const [key2, value] of lifecycleSignalHistory.entries()) {
      if (now - value.seenAt > retentionMs) {
        lifecycleSignalHistory.delete(key2);
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
  function markLifecycleSignalFromHook(sessionId, label) {
    lifecycleSignalHistory.set(lifecycleSignalKey(sessionId, label), {
      source: "hook",
      signature: `hook:${label}`,
      seenAt: Date.now()
    });
  }
  function isInternalMaintenancePrompt(text) {
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
      "are these two statements the same fact"
    ];
    return markers.some((marker) => normalized.includes(marker));
  }
  function resolveExtractionTrigger(label) {
    const normalized = String(label || "").trim().toLowerCase();
    if (!normalized) return "unknown";
    if (normalized.includes("compact")) return "compaction";
    if (normalized.includes("recover")) return "recovery";
    if (normalized.includes("timeout")) return "timeout";
    if (normalized.includes("new")) return "new";
    if (normalized.includes("reset")) return "reset";
    return "unknown";
  }
  function isLowQualityQuery(query) {
    const ACKNOWLEDGMENTS = /^(ok|okay|yes|no|sure|thanks|thank you|got it|sounds good|perfect|great|cool|alright|yep|nope|right|correct|agreed|absolutely|definitely|nice|good|fine|hm+|ah+|oh+)\s*[.!?]?$/i;
    const words = String(query || "").trim().split(/\s+/).filter((w) => w.length > 1);
    return words.length < 3 || ACKNOWLEDGMENTS.test(String(query || "").trim());
  }
  function filterMemoriesByPrivacy(memories, currentOwner) {
    return memories.filter(
      (m) => !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner)
    );
  }
  function readInjectionLog(sessionId) {
    const injectionLogPath = getInjectionLogPath(sessionId);
    try {
      const parsed = JSON.parse(fs.readFileSync(injectionLogPath, "utf8"));
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed;
      }
    } catch (err) {
      if (!isMissingFileError(err)) {
        console.warn(`[quaid][facade] Injection log read failed for ${injectionLogPath}: ${String(err?.message || err)}`);
      }
    }
    return {};
  }
  function writeInjectionLog(sessionId, payload, pretty = false) {
    const injectionLogPath = getInjectionLogPath(sessionId);
    try {
      const encoded = pretty ? JSON.stringify(payload, null, 2) : JSON.stringify(payload);
      fs.writeFileSync(injectionLogPath, encoded, { mode: 384 });
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw new Error(`[quaid][facade] Injection log write failed for ${injectionLogPath}`, { cause: err });
      }
      console.warn(`[quaid][facade] Injection log write failed for ${injectionLogPath}: ${String(err?.message || err)}`);
    }
  }
  function loadInjectedMemoryKeys(sessionId) {
    const logData = readInjectionLog(sessionId);
    const rawInjected = logData.injected ?? logData.memoryTexts;
    return Array.isArray(rawInjected) ? rawInjected.map((item) => String(item || "").trim()).filter(Boolean) : [];
  }
  function saveInjectedMemoryKeys(sessionId, previousKeys, memories, maxEntries) {
    const newKeys = memories.map((m) => m.id || m.text);
    const merged = [...previousKeys, ...newKeys].map((k) => String(k || "").trim()).filter(Boolean).slice(-Math.max(1, Number(maxEntries) || 1));
    writeInjectionLog(sessionId, {
      injected: merged,
      lastInjectedAt: (/* @__PURE__ */ new Date()).toISOString()
    });
    pruneInjectionLogFiles();
    return merged;
  }
  function resetInjectionDedupAfterCompaction(sessionId) {
    const current = readInjectionLog(sessionId);
    writeInjectionLog(sessionId, {
      ...current,
      lastCompactionAt: (/* @__PURE__ */ new Date()).toISOString(),
      injected: [],
      memoryTexts: []
    }, true);
  }
  function queueCompactionExtractionSummary(sessionId, stored, skipped, edges, notify) {
    const now = Date.now();
    if (!compactionNotifyBatchState) {
      compactionNotifyBatchState = {
        startedAtMs: now,
        lastUpdateMs: now,
        sessions: /* @__PURE__ */ new Set(),
        sessionsWithFacts: /* @__PURE__ */ new Set(),
        stored: 0,
        skipped: 0,
        edges: 0,
        timer: null
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
    const flushDelayMs = state.startedAtMs === 0 ? 0 : Math.max(0, Math.min(COMPACTION_NOTIFY_BATCH_MS, COMPACTION_NOTIFY_BATCH_MAX_MS - (now - state.startedAtMs)));
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
      const durationSec = Math.max(1, Math.round((flushState.lastUpdateMs - flushState.startedAtMs) / 1e3));
      const summary = [
        "**[Quaid]** \u{1F4BE} **Compaction extraction summary:**",
        "",
        `\u2022 Sessions processed: ${sessionCount}`,
        `\u2022 Facts stored: ${flushState.stored}`,
        `\u2022 Facts skipped: ${flushState.skipped}`,
        `\u2022 Edges created: ${flushState.edges}`,
        `\u2022 Sessions with new facts: ${flushState.sessionsWithFacts.size}`,
        `\u2022 Window: ${durationSec}s`
      ].join("\n");
      notify(summary);
    }, flushDelayMs);
    if (typeof state.timer.unref === "function") {
      state.timer.unref();
    }
  }
  async function getDocsStalenessWarning() {
    const stalenessJson = await deps.execDocsUpdater("check", ["--json"]);
    const staleRaw = JSON.parse(stalenessJson || "{}");
    const staleDocs = staleRaw && typeof staleRaw === "object" && !Array.isArray(staleRaw) ? staleRaw : {};
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
    return `

STALENESS WARNING: The following docs may be outdated:
${warnings.join("\n")}
Consider running: docs staleness updater (update-stale --apply)`;
  }
  function buildDocsSearchNotificationPayload(query, results) {
    const docResults = [];
    const lines = String(results || "").split("\n");
    for (const line of lines) {
      const match = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
      if (!match) continue;
      docResults.push({
        doc: match[1].split("/").pop() || match[1],
        section: match[2].trim(),
        score: parseFloat(match[3])
      });
    }
    return {
      query: String(query || ""),
      results: docResults
    };
  }
  function loadProjectMarkdown(project) {
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
  function computeDynamicK() {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10) return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
  }
  async function recallMemoryFromBridge(query, limit, opts) {
    const rawStores = opts.stores || ["vector"];
    const expandGraph = rawStores.includes("graph");
    const normalizedStores = [...new Set(rawStores.map(
      (s) => s === "vector_basic" || s === "vector_technical" ? "vector" : s
    ))];
    let domain = opts.domain;
    if (!domain) {
      if (rawStores.includes("vector_technical") && !rawStores.includes("vector_basic")) {
        domain = { technical: true };
      } else if (rawStores.includes("vector_basic") && !rawStores.includes("vector_technical")) {
        domain = { personal: true };
      }
    }
    const cfg = { stores: normalizedStores, limit };
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
      const args = [query, JSON.stringify(cfg), "--json"];
      const output = await datastoreBridge.recall(args);
      const results = parseMemoryResults(output, expandGraph);
      if (!expandGraph) return results.map((r) => ({ ...r, via: "vector" }));
      return results.filter((r) => (r.via || "") === "graph" || r.category === "graph");
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.error("[quaid][facade] recall error:", err.message);
      return [];
    }
  }
  function parseMemoryResults(output, expandGraph) {
    const results = [];
    if (!output || !output.trim()) return results;
    try {
      const parsed = JSON.parse(output);
      const items = Array.isArray(parsed) ? parsed : parsed?.results || parsed?.items || parsed?.direct_results || [];
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const text = String(item.text || "").trim();
        if (!text) continue;
        const domains = (() => {
          if (Array.isArray(item.domains)) return item.domains.map((d) => String(d || "").trim()).filter(Boolean);
          if (typeof item.domains === "string") {
            try {
              const p = JSON.parse(item.domains);
              if (Array.isArray(p)) return p;
            } catch {
            }
          }
          return void 0;
        })();
        results.push({
          text,
          category: String(item.category || "fact"),
          similarity: Number(item.similarity) || 0.5,
          id: item.id ? String(item.id) : void 0,
          domains,
          sourceType: item.source_type || item.sourceType || void 0,
          extractionConfidence: typeof item.extraction_confidence === "number" ? item.extraction_confidence : void 0,
          createdAt: item.created_at || item.createdAt || void 0,
          validFrom: item.valid_from || item.validFrom || void 0,
          validUntil: item.valid_until || item.validUntil || void 0,
          privacy: item.privacy || void 0,
          ownerId: item.owner_id || item.ownerId || void 0,
          via: expandGraph ? void 0 : "vector"
        });
      }
      if (expandGraph) {
        const rels = parsed?.relationships || parsed?.graph_results || [];
        for (const r of rels) {
          if (!r || typeof r !== "object") continue;
          const id = typeof r.id === "string" ? r.id : typeof r.id === "number" ? String(r.id) : "";
          const name = typeof r.name === "string" ? r.name : "";
          const relation = typeof r.relation === "string" ? r.relation : "";
          const direction = typeof r.direction === "string" ? r.direction : "out";
          const sourceName = typeof r.source_name === "string" ? r.source_name : "";
          if (!id || !name || !relation || !sourceName) continue;
          const text = direction === "in" ? `${name} --${relation}--> ${sourceName}` : `${sourceName} --${relation}--> ${name}`;
          results.push({
            text,
            category: "graph",
            similarity: 0.75,
            id,
            relation,
            direction,
            sourceName,
            via: "graph"
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
              via: "vector"
            });
          }
        } else if (line.startsWith("[graph]")) {
          results.push({
            text: line.substring(7).trim(),
            category: "graph",
            similarity: 0.75,
            via: "graph"
          });
        }
      }
    }
    return results;
  }
  function recallFromJournal(query, limit, journalDir) {
    const stop = /* @__PURE__ */ new Set([
      "the",
      "and",
      "for",
      "with",
      "that",
      "this",
      "from",
      "have",
      "has",
      "was",
      "were",
      "what",
      "when",
      "where",
      "which",
      "who",
      "how",
      "why",
      "about",
      "tell",
      "me",
      "your",
      "my",
      "our",
      "their",
      "his",
      "her",
      "its",
      "into",
      "onto",
      "than",
      "then"
    ]);
    const tokens = Array.from(new Set(
      String(query || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).map((t) => t.trim()).filter((t) => t.length >= 3 && !stop.has(t))
    )).slice(0, 16);
    if (!tokens.length) return [];
    let files = [];
    try {
      files = fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md"));
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw new Error("[quaid][facade] Journal recall listing failed under failHard", { cause: err });
      }
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
        for (const t of tokens) {
          if (lc.includes(t)) hits += 1;
        }
        if (hits === 0) continue;
        const excerpt = content.replace(/\s+/g, " ").trim().slice(0, 220);
        const similarity = Math.min(0.95, 0.45 + hits / Math.max(tokens.length, 1) * 0.5);
        scored.push({
          text: `${file}: ${excerpt}${content.length > 220 ? "..." : ""}`,
          category: "journal",
          similarity,
          via: "journal"
        });
      } catch (err) {
        if (deps.isFailHardEnabled()) {
          throw new Error(`[quaid][facade] Journal recall read failed for ${file} under failHard`, { cause: err });
        }
        console.warn(`[quaid][facade] Journal recall read failed for ${file}: ${String(err?.message || err)}`);
      }
    }
    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  }
  function parseProjectStoreResults(out, _query, _limit, _project) {
    if (!out || !out.trim()) return [];
    const results = [];
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
        via: "project"
      });
    }
    return results;
  }
  const _memoryNotes = /* @__PURE__ */ new Map();
  const _memoryNotesTouchedAt = /* @__PURE__ */ new Map();
  const NOTES_DIR = path.join(deps.workspace, ".quaid", "runtime", "notes");
  function getNotesPath(sessionId) {
    return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`);
  }
  function _sleepMs(ms) {
    const i32 = new Int32Array(new SharedArrayBuffer(4));
    Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
  }
  function withNotesLock(sessionId, fn) {
    const lockPath = `${getNotesPath(sessionId)}.lock`;
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    let fd;
    let lastErr;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        fd = fs.openSync(lockPath, "wx", 384);
        break;
      } catch (err) {
        const code = err?.code;
        if (code !== "EEXIST") throw err;
        lastErr = err;
        _sleepMs(10);
      }
    }
    if (fd === void 0) {
      throw new Error(`failed to acquire memory-notes lock for session=${sessionId}: ${String(lastErr?.message || lastErr)}`);
    }
    try {
      return fn();
    } finally {
      try {
        fs.closeSync(fd);
      } catch {
      }
      try {
        fs.unlinkSync(lockPath);
      } catch {
      }
    }
  }
  function addMemoryNote(sessionId, text, category) {
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
    const noteList = _memoryNotes.get(sessionId);
    noteList.push(`[${category}] ${text}`);
    if (noteList.length > MAX_MEMORY_NOTES_PER_SESSION) {
      noteList.splice(0, noteList.length - MAX_MEMORY_NOTES_PER_SESSION);
    }
    try {
      withNotesLock(sessionId, () => {
        const notesPath = getNotesPath(sessionId);
        let existing = [];
        try {
          existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
        } catch (err) {
          const msg = String(err?.message || err);
          if (!msg.includes("ENOENT") && deps.isFailHardEnabled()) throw err;
        }
        existing.push(`[${category}] ${text}`);
        fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 384 });
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
      const notesPath = getNotesPath(sessionId);
      try {
        onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
      } catch {
      }
      const merged = [.../* @__PURE__ */ new Set([...onDisk, ...inMemory])];
      _memoryNotes.delete(sessionId);
      _memoryNotesTouchedAt.delete(sessionId);
      try {
        fs.unlinkSync(notesPath);
      } catch {
      }
      return merged;
    });
  }
  function hasPendingMemoryNotes(sessionId) {
    const sid = String(sessionId || "").trim();
    if (!sid) return false;
    const inMemory = _memoryNotes.get(sid);
    if (Array.isArray(inMemory) && inMemory.length > 0) return true;
    try {
      const onDisk = JSON.parse(fs.readFileSync(getNotesPath(sid), "utf8"));
      return Array.isArray(onDisk) && onDisk.some((n) => typeof n === "string" && n.trim().length > 0);
    } catch {
      return false;
    }
  }
  async function runExtractionPipeline(messages, label, sessionId) {
    const rows = Array.isArray(messages) ? messages : [];
    const sessionNotes = sessionId ? getAndClearMemoryNotes(sessionId) : [];
    const allNotes = Array.from(/* @__PURE__ */ new Set([...sessionNotes]));
    if (rows.length === 0 && allNotes.length === 0) {
      return null;
    }
    const fullTranscript = buildTranscript(rows);
    if (!fullTranscript.trim() && allNotes.length === 0) {
      return null;
    }
    const hasMeaningfulUserContent = rows.some((m) => {
      if (!m || typeof m !== "object") return false;
      const role = String(m.role || "");
      if (role !== "user") return false;
      const text = getMessageText(m).trim();
      if (!text) return false;
      if (typeof deps.transcriptFormat?.shouldSkipText === "function") {
        return !deps.transcriptFormat.shouldSkipText("user", text);
      }
      return true;
    });
    const transcriptForExtraction = allNotes.length > 0 ? `=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===
${allNotes.map((n) => `- ${n}`).join("\n")}
=== END EXPLICIT MEMORY REQUESTS ===

` + fullTranscript : fullTranscript;
    const journalConfig = deps.getMemoryConfig().docs?.journal || {};
    const journalEnabled = deps.isSystemEnabled("journal") && journalConfig.enabled !== false;
    const snippetsEnabled = journalEnabled && journalConfig.snippetsEnabled !== false;
    const triggerType = resolveExtractionTrigger(label);
    const tmpDir = path.join(deps.workspace, ".quaid", "tmp");
    fs.mkdirSync(tmpDir, { recursive: true });
    const tmpPath = path.join(tmpDir, `extract-input-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
    fs.writeFileSync(tmpPath, transcriptForExtraction, { mode: 384 });
    const args = [
      "--owner",
      resolveOwner(),
      "--label",
      triggerType,
      "--json"
    ];
    if (sessionId) args.push("--session-id", sessionId);
    if (!snippetsEnabled) args.push("--no-snippets");
    if (!journalEnabled) args.push("--no-journal");
    let extracted = {};
    try {
      const output = await deps.execExtractPipeline(tmpPath, args);
      extracted = JSON.parse(output || "{}");
    } catch (err) {
      const msg = String(err?.message || err);
      throw new Error(`[quaid][facade] extract pipeline failed: ${msg}`);
    } finally {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
      }
    }
    const factDetails = Array.isArray(extracted?.facts) ? extracted.facts : [];
    const stored = Number(extracted?.facts_stored || 0);
    const skipped = Number(extracted?.facts_skipped || 0);
    const edgesCreated = Number(extracted?.edges_created || 0);
    const snippetDetails = {};
    const journalDetails = {};
    const targetFiles = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];
    const snippetsRaw = extracted?.snippets;
    if (snippetsRaw && typeof snippetsRaw === "object" && !Array.isArray(snippetsRaw)) {
      for (const [filename, snippets] of Object.entries(snippetsRaw)) {
        if (!targetFiles.includes(filename) || !Array.isArray(snippets)) continue;
        const valid = snippets.filter((s) => typeof s === "string" && s.trim().length > 0);
        if (valid.length > 0) snippetDetails[filename] = valid.map((s) => s.trim());
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
      journalDetails
    };
  }
  async function recall(opts) {
    const {
      query,
      limit = 10,
      expandGraph = true,
      graphDepth = 1,
      datastores,
      routeStores,
      reasoning = "fast",
      intent = "general",
      ranking,
      domain = { all: true },
      domainBoost,
      project,
      dateFrom,
      dateTo,
      docs,
      datastoreOptions,
      failOpen
    } = opts;
    const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
    const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
    const runRecall = (q) => {
      const recallOpts = {
        datastores: shouldRouteStores ? [] : selectedStores,
        expandGraph,
        graphDepth,
        intent,
        ranking,
        domain,
        domainBoost,
        project,
        dateFrom,
        dateTo,
        docs,
        datastoreOptions
      };
      return knowledgeEngine.recall(q, limit, {
        ...recallOpts,
        reasoning,
        failOpen
      });
    };
    return runRecall(query);
  }
  function isLowInformationEntityNode(result) {
    if ((result.via || "vector") === "graph" || result.category === "graph") return false;
    const category = String(result.category || "").toLowerCase();
    if (!["person", "concept", "event", "entity"].includes(category)) return false;
    const text = String(result.text || "").trim();
    if (!text) return true;
    const words = text.split(/\s+/).filter(Boolean);
    if (words.length <= 2 && /^[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?$/.test(text)) return true;
    return false;
  }
  function getConfiguredDomainIds() {
    try {
      const defs = deps.getMemoryConfig()?.retrieval?.domains;
      if (defs && typeof defs === "object" && !Array.isArray(defs)) {
        return Object.keys(defs).map((k) => String(k).trim()).filter(Boolean).sort();
      }
    } catch {
    }
    return [];
  }
  function normalizeToken(raw) {
    return String(raw || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }
  function stemToken(token) {
    if (token.length > 6 && token.endsWith("ing")) return token.slice(0, -3);
    if (token.length > 5 && token.endsWith("ed")) return token.slice(0, -2);
    if (token.length > 4 && token.endsWith("s")) return token.slice(0, -1);
    return token;
  }
  function tokenizeQuery(query) {
    return String(query || "").split(/\s+/).map((part) => normalizeToken(part)).map((token) => stemToken(token)).filter((token) => token.length >= 3 && !RECALL_RETRY_STOPWORDS.has(token));
  }
  function temporalCuePresent(query) {
    const lowered = String(query || "").toLowerCase();
    const cues = ["latest", "current", "currently", "still", "now", "as of", "when", "last", "updated"];
    return cues.some((cue) => lowered.includes(cue));
  }
  function attributionCuePresent(query) {
    const lowered = String(query || "").toLowerCase();
    const cues = ["who", "whose", "did", "does", "said", "asked", "told", "mentioned", "attributed"];
    return cues.some((cue) => lowered.includes(cue));
  }
  function isVectorRecallResult(result) {
    const via = String(result.via || "").toLowerCase();
    return via === "vector" || via === "vector_basic" || via === "vector_technical";
  }
  function computeEntityCoverage(query, results) {
    const resultBlob = results.map((r) => `${String(r.text || "").toLowerCase()} ${String(r.sourceName || "").toLowerCase()}`).join(" ");
    const tokens = tokenizeQuery(query);
    if (!tokens.length) return 1;
    const matched = tokens.filter((token) => resultBlob.includes(token)).length;
    return matched / tokens.length;
  }
  function buildExpandedRecallQuery(query) {
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
  function shouldRetryRecall(query, results) {
    const reasons = [];
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
  function mergeRecallResults(primary, secondary, limit) {
    const merged = /* @__PURE__ */ new Map();
    const upsert = (row) => {
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
    return Array.from(merged.values()).sort((a, b) => Number(b.similarity || 0) - Number(a.similarity || 0)).slice(0, Math.max(1, limit));
  }
  async function recallWithToolRetry(opts) {
    const primary = await recall(opts);
    const query = String(opts.query || "");
    const limit = Math.max(1, Number(opts.limit || 10));
    const retryDecision = shouldRetryRecall(query, primary);
    if (!retryDecision.retry) return primary;
    const expanded = buildExpandedRecallQuery(query);
    if (expanded === query) return primary;
    const retryBudgetRaw = Number(((deps.getMemoryConfig() || {}).retrieval || {}).retry_budget_ms ?? 3e3);
    const retryBudgetMs = Number.isFinite(retryBudgetRaw) ? Math.max(0, Math.floor(retryBudgetRaw)) : 3e3;
    if (retryBudgetMs <= 0) {
      console.log("[quaid][facade][recall] retry skipped (retry_budget_ms=0)");
      return primary;
    }
    const retryStartedAt = Date.now();
    console.log(
      `[quaid][facade][recall] retry reasons=${retryDecision.reasons.join(",")} expanded="${expanded.slice(0, 160)}" budget_ms=${retryBudgetMs}`
    );
    try {
      const elapsed = Date.now() - retryStartedAt;
      const remainingMs = Math.max(1, retryBudgetMs - elapsed);
      const secondary = await Promise.race([
        recall({ ...opts, query: expanded }),
        new Promise(
          (_, reject) => setTimeout(() => reject(new Error(`retry budget exceeded (${retryBudgetMs}ms)`)), remainingMs)
        )
      ]);
      return mergeRecallResults(primary, secondary, limit);
    } catch (err) {
      console.warn(
        `[quaid][facade][recall] retry bailed: ${String(err?.message || err)}; returning primary`
      );
      return primary;
    }
  }
  function formatMemoriesForInjection(memories) {
    if (!memories.length) return "";
    const sorted = [...memories].sort((a, b) => {
      if (!a.createdAt && !b.createdAt) return 0;
      if (!a.createdAt) return -1;
      if (!b.createdAt) return 1;
      return a.createdAt.localeCompare(b.createdAt);
    });
    const graphNodeHits = sorted.filter((m) => isLowInformationEntityNode(m));
    const regularMemories = sorted.filter((m) => !isLowInformationEntityNode(m));
    const lines = regularMemories.map((m) => {
      const conf = m.extractionConfidence ?? 0.5;
      const timestamp = m.createdAt ? ` (${m.createdAt.split("T")[0]})` : "";
      const domainLabel = Array.isArray(m.domains) && m.domains.length ? ` [domains:${m.domains.join(",")}]` : "";
      if (conf < 0.4) {
        return `- [${m.category}]${timestamp}${domainLabel} (uncertain) ${m.text}`;
      }
      return `- [${m.category}]${timestamp}${domainLabel} ${m.text}`;
    });
    if (graphNodeHits.length > 0) {
      const packed = graphNodeHits.slice(0, 8).map((m) => `${m.text} (${Math.round((m.similarity || 0) * 100)}%)`).join(", ");
      lines.push(`- [graph-node-hits] Entity node references (not standalone facts): ${packed}`);
    }
    return `<injected_memories>
AUTOMATED MEMORY SYSTEM: The following memories were automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you. Use them as background context only. Items marked (uncertain) have lower extraction confidence. Dates shown are when the fact was recorded.
INJECTOR CONFIDENCE RULE: Treat injected memories as hints, not final truth. If the answer depends on personal details and the match is not exact/high-confidence, run memory_recall before answering.
TOPIC LICENSING RULE: Knowing a sensitive detail does not make it on-topic. For light prompts, acknowledgments, or vague openings, do not volunteer private health, finances, conflicts, or emotionally loaded history unless the user clearly opens that topic.
${lines.join("\n")}
</injected_memories>`;
  }
  function formatRecallToolResponse(results) {
    const vectorResults = results.filter((r) => isVectorRecallResult(r));
    const graphResults = results.filter((r) => (r.via || "") === "graph" || r.category === "graph");
    const journalResults = results.filter((r) => (r.via || "") === "journal");
    const projectResults = results.filter((r) => (r.via || "") === "project");
    const avgSimilarity = vectorResults.length > 0 ? vectorResults.reduce((sum, r) => sum + (r.similarity || 0), 0) / vectorResults.length : 0;
    const maxSimilarity = vectorResults.length > 0 ? Math.max(...vectorResults.map((r) => Number(r.similarity || 0))) : 0;
    const hasHighExtractionConfidence = vectorResults.some((r) => Number(r.extractionConfidence || 0) >= 0.8);
    const lowQualityWarning = vectorResults.length > 0 && avgSimilarity < 0.45 && maxSimilarity < 0.55 && !hasHighExtractionConfidence ? "\n\n\u26A0\uFE0F Low confidence matches - consider refining query with specific names or topics.\n" : "";
    let text = `[MEMORY] Found ${results.length} results:${lowQualityWarning}
`;
    if (vectorResults.length > 0) {
      text += "\n**Direct Matches:**\n";
      vectorResults.forEach((r, i) => {
        const conf = r.extractionConfidence ? ` [conf:${Math.round(r.extractionConfidence * 100)}%]` : "";
        const dateStr = r.createdAt ? ` (${r.createdAt.split("T")[0]})` : "";
        const superseded = r.validUntil ? " [superseded]" : "";
        text += `${i + 1}. [MEMORY] [${r.category}]${dateStr}${superseded} ${r.text} (${Math.round(r.similarity * 100)}%${conf})
`;
      });
    }
    if (graphResults.length > 0) {
      if (vectorResults.length > 0) text += "\n";
      text += "**Graph Discoveries:**\n";
      graphResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text}
`;
      });
    }
    if (journalResults.length > 0) {
      if (vectorResults.length > 0 || graphResults.length > 0) text += "\n";
      text += "**Journal Signals:**\n";
      journalResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)
`;
      });
    }
    if (projectResults.length > 0) {
      if (vectorResults.length > 0 || graphResults.length > 0 || journalResults.length > 0) text += "\n";
      text += "**Project Knowledge:**\n";
      projectResults.forEach((r, i) => {
        text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)
`;
      });
    }
    return {
      text,
      breakdown: {
        vector_count: vectorResults.length,
        graph_count: graphResults.length,
        journal_count: journalResults.length,
        project_count: projectResults.length
      }
    };
  }
  function buildRecallNotificationPayload(results, query, mode, breakdown) {
    const classified = results.map((r) => {
      const via = String(r.via || "").trim().toLowerCase();
      const category = String(r.category || "").trim().toLowerCase();
      if (via === "graph" || category === "graph") return "graph";
      if (via === "journal") return "journal";
      if (via === "project") return "project";
      return "vector";
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
        category: m.category || ""
      })),
      source_breakdown: {
        vector_count: Number(breakdown?.vector_count ?? vectorCount),
        graph_count: Number(breakdown?.graph_count ?? graphCount),
        journal_count: Number(breakdown?.journal_count ?? journalCount),
        project_count: Number(breakdown?.project_count ?? projectCount),
        query,
        mode
      }
    };
  }
  function prepareAutoInjectionContext(params) {
    const {
      allMemories,
      eventMessages,
      context,
      existingPrependContext,
      injectLimit,
      maxInjectionIdsPerSession
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
    const prependContext = existingPrependContext ? `${existingPrependContext}

${formatted}` : formatted;
    saveInjectedMemoryKeys(
      uniqueSessionId,
      previouslyInjected,
      toInject,
      maxInjectionIdsPerSession
    );
    return { prependContext, toInject, uniqueSessionId };
  }
  function buildExtractionCompletionNotificationPayload(params) {
    const mergedDetails = {};
    for (const [f, items] of Object.entries(params.snippetDetails || {})) {
      mergedDetails[f] = items.map((s) => `[snippet] ${s}`);
    }
    for (const [f, items] of Object.entries(params.journalDetails || {})) {
      mergedDetails[f] = [...mergedDetails[f] || [], ...items.map((s) => `[journal] ${s}`)];
    }
    const hasMerged = Object.keys(mergedDetails).length > 0;
    return {
      stored: Number(params.stored || 0),
      skipped: Number(params.skipped || 0),
      edges_created: Number(params.edgesCreated || 0),
      trigger: String(params.triggerType || "") === "unknown" ? "reset" : String(params.triggerType || "reset"),
      details: Array.isArray(params.factDetails) ? params.factDetails : [],
      snippet_details: hasMerged ? mergedDetails : null,
      always_notify: Boolean(params.alwaysNotifyCompletion)
    };
  }
  function getExtractionStartTriggerDescription(triggerType) {
    if (triggerType === "compaction") return "compaction";
    if (triggerType === "recovery") return "recovery";
    if (triggerType === "timeout") return "timeout";
    if (triggerType === "new") return "/new";
    return "reset";
  }
  function shouldNotifyExtractionStart(params) {
    if (!params.showProcessingStart || !shouldNotifyFeature("extraction", "summary")) return null;
    const triggerType = resolveExtractionTrigger(params.label);
    const suppressBacklogNotify = isBacklogLifecycleReplay(
      params.messages,
      triggerType,
      Date.now(),
      params.bootTimeMs,
      params.backlogNotifyStaleMs
    );
    const dedupeSession = params.sessionId || extractSessionId(params.messages, {});
    const dedupeKey = `start:${dedupeSession}:${triggerType}`;
    if (triggerType === "recovery") return null;
    if (suppressBacklogNotify) return null;
    if (!params.hasMeaningfulUserContent) return null;
    if (!shouldEmitExtractionNotify(dedupeKey)) return null;
    return { triggerDesc: getExtractionStartTriggerDescription(triggerType) };
  }
  function injectFullJournalContext(existingContext) {
    let prepend = existingContext;
    if (!deps.isSystemEnabled("journal")) return prepend;
    try {
      const journalConfig = deps.getMemoryConfig().docs?.journal || {};
      const journalMode = journalConfig.mode || "distilled";
      if (journalMode !== "full") return prepend;
      const journalDir = path.join(deps.workspace, journalConfig.journalDir || "journal");
      let journalFiles = [];
      try {
        journalFiles = fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md")).sort();
      } catch (err) {
        if (deps.isFailHardEnabled()) {
          throw new Error("[quaid] Journal injection listing failed under failHard", { cause: err });
        }
        console.warn(`[quaid] Journal injection listing failed: ${String(err?.message || err)}`);
      }
      let journalContent = "";
      for (const file of journalFiles) {
        try {
          const content = fs.readFileSync(path.join(journalDir, file), "utf8");
          if (content.trim()) {
            journalContent += `

--- ${file} ---
${content}`;
          }
        } catch (err) {
          if (deps.isFailHardEnabled()) {
            throw new Error(`[quaid] Journal injection read failed for ${file} under failHard`, { cause: err });
          }
          console.warn(`[quaid] Journal injection read failed for ${file}: ${String(err?.message || err)}`);
        }
      }
      if (journalContent) {
        const header = "[JOURNAL - Full Soul Mode]\nThese are your recent journal reflections. They are part of your inner life.\n";
        prepend = prepend ? `${prepend}

${header}${journalContent}` : `${header}${journalContent}`;
        console.log(`[quaid] Full soul mode: injected ${journalFiles.length} journal files`);
      }
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      console.warn(`[quaid] Journal injection failed (non-fatal): ${err.message}`);
    }
    return prepend;
  }
  function notImplemented(name) {
    throw new Error(`[quaid][facade] ${name} is not yet implemented \u2014 scheduled for a future PR`);
  }
  function injectProjectContext(existingContext) {
    let prepend = existingContext;
    try {
      const projectsDir = path.join(deps.workspace, "projects");
      let subdirs = [];
      try {
        subdirs = fs.readdirSync(projectsDir).filter((name) => {
          try {
            return fs.statSync(path.join(projectsDir, name)).isDirectory() && !name.startsWith(".");
          } catch {
            return false;
          }
        }).sort((a, b) => a === "quaid" ? -1 : b === "quaid" ? 1 : a.localeCompare(b));
      } catch {
        return prepend;
      }
      const sections = [];
      for (const projectName of subdirs) {
        for (const docFile of ["TOOLS.md", "AGENTS.md"]) {
          const filePath = path.join(projectsDir, projectName, docFile);
          if (fs.existsSync(filePath)) {
            try {
              const content = fs.readFileSync(filePath, "utf8").trim();
              if (content) sections.push(`--- ${projectName}/${docFile} ---
${content}`);
            } catch {
            }
          }
        }
      }
      if (sections.length === 0) return prepend;
      const combined = "# Quaid Project Context\n\n" + sections.join("\n\n") + "\n";
      prepend = prepend ? `${prepend}

${combined}` : combined;
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.warn(`[quaid] Project context injection failed: ${err?.message || String(err)}`);
    }
    return prepend;
  }
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
    searchBySession: (sessionId, limit = 20) => datastoreBridge.recall([
      "*",
      "--session-id",
      sessionId,
      "--owner",
      resolveOwner(),
      "--limit",
      String(limit)
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
        dispatch
      ];
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
    // Recall
    recall,
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
    docsSearch: (query, args) => deps.execDocsRag("search", [query, ...args]),
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
    readSessionMessagesFile: (sessionFile) => deps.readSessionMessagesFile?.(sessionFile) || readMessagesFromSessionJsonl(sessionFile),
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
    resolveExtractionTrigger
  };
}
export {
  createQuaidFacade
};
