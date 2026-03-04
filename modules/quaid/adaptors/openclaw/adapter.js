import { Type } from "@sinclair/typebox";
import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
import { queueDelayedRequest } from "./delayed-requests.js";
import { normalizeKnowledgeDatastores } from "../../core/knowledge-stores.js";
import { createQuaidFacade } from "../../core/facade.js";
import { PYTHON_BRIDGE_TIMEOUT_MS, createPythonBridgeExecutor } from "./python-bridge.js";
import {
  assertDeclaredRegistration,
  normalizeDeclaredExports,
  validateApiRegistrations,
  validateApiSurface
} from "./contract-gate.js";
function _normalizeWorkspacePath(rawPath) {
  const trimmed = String(rawPath || "").trim();
  if (!trimmed) {
    return path.resolve(process.cwd());
  }
  const expanded = trimmed.startsWith("~") ? path.join(os.homedir(), trimmed.slice(1)) : trimmed;
  return path.resolve(expanded);
}
function _resolveWorkspace() {
  const envWorkspace = String(process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envWorkspace) {
    return _normalizeWorkspacePath(envWorkspace);
  }
  const envQuaidHome = String(process.env.QUAID_HOME || "").trim();
  if (envQuaidHome) {
    return _normalizeWorkspacePath(envQuaidHome);
  }
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (fs.existsSync(cfgPath)) {
      const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const list = Array.isArray(cfg?.agents?.list) ? cfg.agents.list : [];
      const mainAgent = list.find((a) => a?.id === "main" || a?.default === true);
      const ws = String(mainAgent?.workspace || cfg?.agents?.defaults?.workspace || "").trim();
      if (ws) {
        return _normalizeWorkspacePath(ws);
      }
    }
  } catch (err) {
    console.error("[quaid][startup] workspace resolution failed:", err?.message || String(err));
  }
  return _normalizeWorkspacePath(process.cwd());
}
const WORKSPACE = _resolveWorkspace();
function _resolvePythonPluginRoot() {
  const modulesRoot = path.join(WORKSPACE, "modules", "quaid");
  if (fs.existsSync(modulesRoot)) {
    return modulesRoot;
  }
  return path.join(WORKSPACE, "plugins", "quaid");
}
const PYTHON_PLUGIN_ROOT = _resolvePythonPluginRoot();
const PYTHON_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "datastore/memorydb/memory_graph.py");
const EXTRACT_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "ingest/extract.py");
const DB_PATH = path.join(WORKSPACE, "data/memory.db");
const QUAID_RUNTIME_DIR = path.join(WORKSPACE, ".quaid", "runtime");
const QUAID_TMP_DIR = path.join(QUAID_RUNTIME_DIR, "tmp");
const QUAID_NOTES_DIR = path.join(QUAID_RUNTIME_DIR, "notes");
const QUAID_INJECTION_LOG_DIR = path.join(QUAID_RUNTIME_DIR, "injection");
const QUAID_NOTIFY_DIR = path.join(QUAID_RUNTIME_DIR, "notify");
const QUAID_LOGS_DIR = path.join(WORKSPACE, "logs");
const QUAID_JANITOR_DIR = path.join(QUAID_LOGS_DIR, "janitor");
const PENDING_INSTALL_MIGRATION_PATH = path.join(QUAID_JANITOR_DIR, "pending-install-migration.json");
const PENDING_APPROVAL_REQUESTS_PATH = path.join(QUAID_JANITOR_DIR, "pending-approval-requests.json");
const DELAYED_LLM_REQUESTS_PATH = path.join(QUAID_NOTES_DIR, "delayed-llm-requests.json");
const JANITOR_NUDGE_STATE_PATH = path.join(QUAID_NOTES_DIR, "janitor-nudge-state.json");
const ADAPTER_PLUGIN_MANIFEST_PATH = path.join(PYTHON_PLUGIN_ROOT, "adaptors", "openclaw", "plugin.json");
const COMPACTION_NOTIFY_BATCH_MS = _envTimeoutMs("QUAID_COMPACTION_NOTIFY_BATCH_MS", 45e3);
const COMPACTION_NOTIFY_BATCH_MAX_MS = _envTimeoutMs("QUAID_COMPACTION_NOTIFY_BATCH_MAX_MS", 12e4);
let compactionNotifyBatchState = null;
const ADAPTER_BOOT_TIME_MS = Date.now();
const BACKLOG_NOTIFY_STALE_MS = 9e4;
for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch (err) {
    console.error(`[quaid][startup] failed to create runtime dir: ${p}`, err?.message || String(err));
  }
}
let _memoryConfigErrorLogged = false;
let _memoryConfigMtimeMs = -1;
let _memoryConfigPath = "";
function _envTimeoutMs(name, fallbackMs) {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}
const EXTRACT_PIPELINE_TIMEOUT_MS = _envTimeoutMs("QUAID_EXTRACT_PIPELINE_TIMEOUT_MS", 3e5);
const EVENTS_EMIT_TIMEOUT_MS = _envTimeoutMs("QUAID_EVENTS_TIMEOUT_MS", 3e5);
const QUICK_PROJECT_SUMMARY_TIMEOUT_MS = _envTimeoutMs("QUAID_PROJECT_SUMMARY_TIMEOUT_MS", 6e4);
function buildPythonEnv(extra = {}) {
  const sep = process.platform === "win32" ? ";" : ":";
  const existing = String(process.env.PYTHONPATH || "").trim();
  const pyPath = existing ? `${PYTHON_PLUGIN_ROOT}${sep}${existing}` : PYTHON_PLUGIN_ROOT;
  return {
    ...process.env,
    MEMORY_DB_PATH: DB_PATH,
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE,
    PYTHONPATH: pyPath,
    ...extra
  };
}
let _memoryConfig = null;
function _memoryConfigCandidates() {
  return [
    path.join(WORKSPACE, "config", "memory.json"),
    path.join(os.homedir(), ".quaid", "memory-config.json"),
    path.join(process.cwd(), "memory-config.json")
  ];
}
function _resolveMemoryConfigPath() {
  for (const candidate of _memoryConfigCandidates()) {
    try {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    } catch {
    }
  }
  return _memoryConfigCandidates()[0];
}
function _buildFallbackMemoryConfig() {
  return {
    models: {
      llmProvider: "default",
      deepReasoning: "default",
      fastReasoning: "default",
      deepReasoningModelClasses: {
        anthropic: "claude-opus-4-6",
        openai: "gpt-5",
        "openai-compatible": "gpt-4.1"
      },
      fastReasoningModelClasses: {
        anthropic: "claude-haiku-4-5",
        openai: "gpt-5-mini",
        "openai-compatible": "gpt-4.1-mini"
      }
    },
    retrieval: {
      maxLimit: 8
    }
  };
}
function getMemoryConfig() {
  const configPath = _resolveMemoryConfigPath();
  if (configPath !== _memoryConfigPath) {
    _memoryConfigMtimeMs = -1;
    _memoryConfigPath = configPath;
  }
  let mtimeMs = -1;
  try {
    mtimeMs = fs.statSync(configPath).mtimeMs;
  } catch (err) {
    const msg = String(err?.message || err || "");
    if (!msg.includes("ENOENT")) {
      console.warn(`[quaid] memory config stat failed: ${msg}`);
    }
  }
  if (_memoryConfig && mtimeMs >= 0 && _memoryConfigMtimeMs === mtimeMs) {
    return _memoryConfig;
  }
  if (_memoryConfig && mtimeMs < 0) {
    return _memoryConfig;
  }
  try {
    _memoryConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
    _memoryConfigMtimeMs = mtimeMs;
  } catch (err) {
    if (!_memoryConfigErrorLogged) {
      _memoryConfigErrorLogged = true;
      console.error("[quaid] failed to load config/memory.json:", err?.message || String(err));
    }
    if (isMissingFileError(err)) {
      _memoryConfig = _buildFallbackMemoryConfig();
      _memoryConfigMtimeMs = -1;
      return _memoryConfig;
    }
    _memoryConfig = _buildFallbackMemoryConfig();
    _memoryConfigMtimeMs = mtimeMs;
    if (isFailHardEnabled()) {
      throw err;
    }
  }
  return _memoryConfig;
}
function isSystemEnabled(system) {
  const config = getMemoryConfig();
  const systems = config.systems || {};
  return systems[system] !== false;
}
function isPluginStrictMode() {
  const plugins = getMemoryConfig().plugins || {};
  const raw = plugins.strict;
  if (raw === void 0) return true;
  if (raw === null) return false;
  if (typeof raw === "number") return raw !== 0;
  if (typeof raw === "string") return raw.length > 0;
  if (Array.isArray(raw)) return raw.length > 0;
  if (typeof raw === "object") return Object.keys(raw).length > 0;
  return !!raw;
}
function loadAdapterContractDeclarations() {
  try {
    const payload = JSON.parse(fs.readFileSync(ADAPTER_PLUGIN_MANIFEST_PATH, "utf8"));
    const contract = payload?.capabilities?.contract || {};
    return {
      enabled: true,
      tools: normalizeDeclaredExports(contract?.tools?.exports),
      events: normalizeDeclaredExports(contract?.events?.exports),
      api: normalizeDeclaredExports(contract?.api?.exports)
    };
  } catch (err) {
    const msg = `[quaid][contract] failed reading adapter manifest ${ADAPTER_PLUGIN_MANIFEST_PATH}: ${String(err?.message || err)}`;
    if (isPluginStrictMode()) {
      throw new Error(msg, { cause: err });
    }
    console.warn(msg);
    return { enabled: false, tools: /* @__PURE__ */ new Set(), events: /* @__PURE__ */ new Set(), api: /* @__PURE__ */ new Set() };
  }
}
function isPreInjectionPassEnabled() {
  const retrieval = getMemoryConfig().retrieval || {};
  if (typeof retrieval.preInjectionPass === "boolean") return retrieval.preInjectionPass;
  if (typeof retrieval.pre_injection_pass === "boolean") return retrieval.pre_injection_pass;
  return true;
}
function isFailHardEnabled() {
  const retrieval = getMemoryConfig().retrieval || {};
  if (typeof retrieval.fail_hard === "boolean") return retrieval.fail_hard;
  if (typeof retrieval.failHard === "boolean") return retrieval.failHard;
  return true;
}
function isMissingFileError(err) {
  const code = err?.code;
  if (code === "ENOENT") return true;
  const msg = String(err?.message || "");
  return msg.includes("ENOENT");
}
function getCaptureTimeoutMinutes() {
  const capture = getMemoryConfig().capture || {};
  const raw = capture.inactivityTimeoutMinutes ?? capture.inactivity_timeout_minutes ?? 120;
  const num = Number(raw);
  return Number.isFinite(num) ? Math.max(0, num) : 120;
}
function normalizeProvider(provider) {
  return String(provider || "").trim().toLowerCase();
}
function providerClassLookupKey(provider) {
  const normalized = normalizeProvider(provider);
  if (normalized === "openai-codex") return "openai";
  if (normalized === "anthropic-claude-code") return "anthropic";
  return normalized;
}
function getConfiguredTierValue(tier) {
  const key = tier === "fast" ? "fastReasoning" : "deepReasoning";
  const configured = getMemoryConfig().models?.[key];
  if (typeof configured === "string" && configured.trim().length > 0) {
    return configured.trim();
  }
  throw new Error(`Missing models.${key} in config/memory.json`);
}
function getConfiguredTierProvider(tier) {
  const key = tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider";
  const configured = getMemoryConfig().models?.[key];
  if (typeof configured === "string" && configured.trim().length > 0) {
    return normalizeProvider(configured.trim());
  }
  return "default";
}
function parseTierModelClassMap(tier) {
  const models = getMemoryConfig().models || {};
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
function getGatewayDefaultProvider() {
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (fs.existsSync(cfgPath)) {
      const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const primaryModel = String(
        cfg?.agents?.main?.modelPrimary || cfg?.agents?.defaults?.modelPrimary || ""
      ).trim();
      if (primaryModel.includes("/")) {
        const provider = primaryModel.split("/", 1)[0];
        const normalized = normalizeProvider(provider);
        if (normalized) {
          return normalized;
        }
      }
    }
  } catch (err) {
    console.warn(`[quaid] gateway default provider read failed from openclaw.json: ${String(err?.message || err)}`);
  }
  try {
    const profilesPath = path.join(os.homedir(), ".openclaw", "agents", "main", "agent", "auth-profiles.json");
    if (fs.existsSync(profilesPath)) {
      const data = JSON.parse(fs.readFileSync(profilesPath, "utf8"));
      const lastGood = data?.lastGood || {};
      const preferred = ["openai-codex", "openai", "anthropic"];
      for (const key of preferred) {
        if (lastGood[key]) {
          const normalized = normalizeProvider(key);
          if (normalized) {
            return normalized;
          }
        }
      }
      for (const key of Object.keys(lastGood)) {
        const normalized = normalizeProvider(key);
        if (normalized) {
          return normalized;
        }
      }
    }
  } catch (err) {
    console.warn(`[quaid] gateway provider fallback read failed from auth-profiles.json: ${String(err?.message || err)}`);
  }
  return "";
}
function getEffectiveProvider() {
  const configuredProvider = normalizeProvider(String(getMemoryConfig().models?.llmProvider || ""));
  if (configuredProvider && configuredProvider !== "default") {
    return configuredProvider;
  }
  const gatewayProvider = getGatewayDefaultProvider();
  if (gatewayProvider) {
    return gatewayProvider;
  }
  throw new Error(
    "models.llmProvider is 'default' but no active gateway provider was resolved. Set models.llmProvider explicitly (anthropic/openai/openai-compatible/claude-code), or ensure OpenClaw auth profiles exist and lastGood is set in ~/.openclaw/agents/main/agent/auth-profiles.json."
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
function runStartupSelfCheck() {
  const errors = [];
  try {
    const deep = resolveTierModel("deep");
    console.log(`[quaid][startup] deep model resolved: provider=${deep.provider} model=${deep.model}`);
    const paidProviders = /* @__PURE__ */ new Set(["openai-compatible"]);
    if (paidProviders.has(deep.provider)) {
      console.warn(`[quaid][billing] paid provider active for deep reasoning: ${deep.provider}/${deep.model}`);
    }
  } catch (err) {
    errors.push(`deep reasoning model resolution failed: ${String(err?.message || err)}`);
  }
  try {
    const fast = resolveTierModel("fast");
    console.log(`[quaid][startup] fast model resolved: provider=${fast.provider} model=${fast.model}`);
    const paidProviders = /* @__PURE__ */ new Set(["openai-compatible"]);
    if (paidProviders.has(fast.provider)) {
      console.warn(`[quaid][billing] paid provider active for fast reasoning: ${fast.provider}/${fast.model}`);
    }
  } catch (err) {
    errors.push(`fast reasoning model resolution failed: ${String(err?.message || err)}`);
  }
  try {
    const cfg = getMemoryConfig();
    const maxResults = Number(cfg?.retrieval?.maxLimit ?? cfg?.retrieval?.max_limit ?? 0);
    if (!Number.isFinite(maxResults) || maxResults <= 0) {
      errors.push(`invalid retrieval.maxLimit=${String(cfg?.retrieval?.maxLimit ?? cfg?.retrieval?.max_limit)}`);
    }
  } catch (err) {
    errors.push(`config load failed: ${String(err?.message || err)}`);
  }
  const requiredFiles = [
    path.join(PYTHON_PLUGIN_ROOT, "core", "lifecycle", "janitor.py"),
    path.join(PYTHON_PLUGIN_ROOT, "datastore", "memorydb", "memory_graph.py")
  ];
  for (const file of requiredFiles) {
    if (!fs.existsSync(file)) {
      errors.push(`required runtime file missing: ${file}`);
    }
  }
  if (errors.length > 0) {
    const msg = `[quaid][startup] preflight failed:
- ${errors.join("\n- ")}`;
    console.error(msg);
    throw new Error(msg);
  }
}
const configSchema = Type.Object({
  autoCapture: Type.Optional(Type.Boolean({ default: false })),
  autoRecall: Type.Optional(Type.Boolean({ default: true }))
});
function isInternalQuaidSession(sessionId) {
  const sid = typeof sessionId === "string" ? sessionId.trim() : "";
  if (!sid) return false;
  return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
}
const MAX_INJECTION_LOG_FILES = 400;
const MAX_INJECTION_IDS_PER_SESSION = 4e3;
const MAX_EXTRACTION_LOG_ENTRIES = 800;
function getInjectionLogPath(sessionId) {
  return path.join(QUAID_INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
}
function pruneInjectionLogFiles() {
  try {
    const files = fs.readdirSync(QUAID_INJECTION_LOG_DIR).filter((f) => f.startsWith("memory-injection-") && f.endsWith(".log")).map((f) => ({ name: f, full: path.join(QUAID_INJECTION_LOG_DIR, f), mtimeMs: fs.statSync(path.join(QUAID_INJECTION_LOG_DIR, f)).mtimeMs })).sort((a, b) => b.mtimeMs - a.mtimeMs);
    for (const stale of files.slice(MAX_INJECTION_LOG_FILES)) {
      try {
        fs.unlinkSync(stale.full);
      } catch (err) {
        console.warn(`[quaid] Failed pruning stale injection log ${stale.full}: ${String(err?.message || err)}`);
      }
    }
  } catch (err) {
    console.warn(`[quaid] Injection log pruning failed: ${String(err?.message || err)}`);
  }
}
function trimExtractionLogEntries(log, maxEntries = MAX_EXTRACTION_LOG_ENTRIES) {
  const entries = Object.entries(log || {});
  if (entries.length <= maxEntries) {
    return log || {};
  }
  const sorted = entries.map(([sid, payload]) => ({ sid, payload, ts: Date.parse(String(payload?.last_extracted_at || "")) || 0 })).sort((a, b) => b.ts - a.ts).slice(0, maxEntries);
  return Object.fromEntries(sorted.map((row) => [row.sid, row.payload]));
}
function extractSessionId(messages, ctx) {
  if (ctx?.sessionId) {
    return ctx.sessionId;
  }
  let firstTimestamp = "";
  const filteredMessages = messages.filter((m) => {
    if (m.role !== "user") {
      return false;
    }
    let content = "";
    if (typeof m.content === "string") {
      content = m.content;
    } else if (Array.isArray(m.content)) {
      content = m.content.map((c) => c.text || "").join(" ");
    }
    if (content.startsWith("GatewayRestart:")) {
      return false;
    }
    if (content.startsWith("System:")) {
      return false;
    }
    if (content.includes('"kind": "restart"')) {
      return false;
    }
    return true;
  });
  if (filteredMessages.length > 0) {
    const firstMessage = filteredMessages[0];
    if (firstMessage.timestamp) {
      firstTimestamp = String(firstMessage.timestamp);
    } else {
      firstTimestamp = Date.now().toString();
    }
  } else {
    firstTimestamp = Date.now().toString();
  }
  const timestampHash = createHash("md5").update(firstTimestamp).digest("hex").substring(0, 12);
  return timestampHash;
}
function resolveSessionIdFromSessionKey(sessionKey) {
  const key = String(sessionKey || "").trim();
  if (!key) {
    return "";
  }
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    if (!fs.existsSync(sessionsPath)) {
      return "";
    }
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const parsed = JSON.parse(raw);
    const entry = parsed?.[key];
    const sid = String(entry?.sessionId || "").trim();
    if (sid) {
      return sid;
    }
  } catch {
  }
  return "";
}
function resolveMostRecentSessionId() {
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    if (!fs.existsSync(sessionsPath)) {
      return "";
    }
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const parsed = JSON.parse(raw);
    const entries = Object.values(parsed || {});
    let bestId = "";
    let bestUpdated = -1;
    for (const entry of entries) {
      const sid = String(entry?.sessionId || "").trim();
      if (!sid) continue;
      const updatedAt = Number(entry?.updatedAt || 0);
      if (Number.isFinite(updatedAt) && updatedAt >= bestUpdated) {
        bestUpdated = updatedAt;
        bestId = sid;
      }
    }
    return bestId;
  } catch {
  }
  return "";
}
function resolveMemoryStoreSessionId(ctx) {
  const direct = String(ctx?.sessionId || "").trim();
  if (direct) {
    return direct;
  }
  const fromKey = resolveSessionIdFromSessionKey(String(ctx?.sessionKey || ""));
  if (fromKey) {
    return fromKey;
  }
  const mainFallback = resolveSessionIdFromSessionKey("agent:main:main");
  if (mainFallback) {
    return mainFallback;
  }
  const recentFallback = resolveMostRecentSessionId();
  if (recentFallback) {
    return recentFallback;
  }
  return "unknown";
}
function resolveLifecycleHookSessionId(event, ctx, messages) {
  const direct = String(event?.sessionId || ctx?.sessionId || "").trim();
  if (direct) {
    return direct;
  }
  const fromEventEntry = String(event?.sessionEntry?.sessionId || event?.previousSessionEntry?.sessionId || "").trim();
  if (fromEventEntry) {
    return fromEventEntry;
  }
  const eventSessionKey = String(event?.sessionKey || event?.targetSessionKey || "").trim();
  const fromEventKey = resolveSessionIdFromSessionKey(eventSessionKey);
  if (fromEventKey) {
    return fromEventKey;
  }
  const ctxSessionKey = String(ctx?.sessionKey || "").trim();
  const fromCtxKey = resolveSessionIdFromSessionKey(ctxSessionKey);
  if (fromCtxKey) {
    return fromCtxKey;
  }
  return extractSessionId(messages, ctx);
}
function getAllConversationMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg) => {
    if (!msg || msg.role !== "user" && msg.role !== "assistant") return false;
    const text = facade.getMessageText(msg).trim();
    if (!text) return false;
    if (text.startsWith("Extract memorable facts and journal entries from this conversation:")) return false;
    if (facade.isInternalMaintenancePrompt(text)) return false;
    if (msg.role === "assistant") {
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
function detectLifecycleCommandSignal(messages) {
  const signal = facade.detectLifecycleSignal(messages);
  return signal?.label || null;
}
function queueCompactionNotificationBatch(sessionId, stored, skipped, edges) {
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
    spawnNotifyScript(`
from core.runtime.notify import notify_user, _resolve_channel
notify_user(${JSON.stringify(summary)}, channel_override=_resolve_channel("extraction"))
`);
  }, flushDelayMs);
  if (typeof state.timer.unref === "function") {
    state.timer.unref();
  }
}
function resolveSessionKeyForCompaction(sessionId) {
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const data = JSON.parse(raw);
    const entries = Object.entries(data || {}).filter(([_, v]) => v && typeof v === "object");
    if (!entries.length) return null;
    if (sessionId) {
      for (const [key, entry] of entries) {
        if (String(entry?.sessionId || "").trim() === String(sessionId).trim()) {
          return key;
        }
      }
    }
    if (entries.some(([k]) => k === "agent:main:main")) {
      return "agent:main:main";
    }
    return entries[0][0];
  } catch {
    return null;
  }
}
function maybeForceCompactionAfterTimeout(sessionId) {
  const captureCfg = getMemoryConfig().capture || {};
  const enabled = Boolean(
    captureCfg.autoCompactionOnTimeout ?? captureCfg.auto_compaction_on_timeout ?? true
  );
  if (!enabled) return;
  const key = resolveSessionKeyForCompaction(sessionId);
  if (!key) {
    console.warn(`[quaid][timeout] auto-compaction skipped: could not resolve session key (session=${sessionId || "unknown"})`);
    return;
  }
  try {
    const out = execFileSync(
      "openclaw",
      ["gateway", "call", "sessions.compact", "--json", "--params", JSON.stringify({ key })],
      { encoding: "utf-8", timeout: 2e4 }
    );
    const parsed = JSON.parse(String(out || "{}"));
    if (parsed?.ok) {
      console.log(`[quaid][timeout] auto-compaction requested for key=${key} (compacted=${String(parsed?.compacted)})`);
    } else {
      console.warn(`[quaid][timeout] auto-compaction returned non-ok for key=${key}: ${String(out).slice(0, 300)}`);
    }
  } catch (err) {
    if (isFailHardEnabled()) {
      throw err;
    }
    console.warn(`[quaid][timeout] auto-compaction failed for key=${key}: ${String(err?.message || err)}`);
  }
}
const DOCS_UPDATER = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/updater.py");
const DOCS_RAG = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/rag.py");
const DOCS_REGISTRY = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/registry.py");
const PROJECT_UPDATER = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/project_updater.py");
const EVENTS_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "core/runtime/events.py");
function _getGatewayCredential(providers) {
  try {
    const profilesPath = path.join(
      os.homedir(),
      ".openclaw",
      "agents",
      "main",
      "agent",
      "auth-profiles.json"
    );
    if (fs.existsSync(profilesPath)) {
      const data = JSON.parse(fs.readFileSync(profilesPath, "utf8"));
      const profiles = data?.profiles || {};
      const lastGood = data?.lastGood || {};
      for (const provider of providers) {
        const lastGoodId = lastGood[provider];
        if (lastGoodId && profiles[lastGoodId]) {
          const cred = profiles[lastGoodId];
          const key = cred.access || cred.token || cred.key;
          if (key) return key;
        }
      }
    }
  } catch (err) {
    if (isFailHardEnabled()) {
      throw err;
    }
  }
  return void 0;
}
function _getAnthropicCredential() {
  return _getGatewayCredential(["anthropic"]);
}
function _readOpenClawConfig() {
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (!fs.existsSync(cfgPath)) {
      return {};
    }
    return JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  } catch (err) {
    const msg = String(err?.message || err || "");
    if (!msg.includes("ENOENT")) {
      console.warn(`[quaid] openclaw config read failed; using gateway defaults: ${msg}`);
    }
    return {};
  }
}
function _getGatewayBaseUrl() {
  const envUrl = String(process.env.OPENCLAW_GATEWAY_URL || "").trim();
  if (envUrl) {
    return envUrl.replace(/\/+$/, "");
  }
  const cfg = _readOpenClawConfig();
  const port = Number(cfg?.gateway?.port || process.env.OPENCLAW_GATEWAY_PORT || 18789);
  return `http://127.0.0.1:${Number.isFinite(port) && port > 0 ? port : 18789}`;
}
function _getGatewayToken() {
  const envToken = String(process.env.OPENCLAW_GATEWAY_TOKEN || "").trim();
  if (envToken) {
    return envToken;
  }
  const cfg = _readOpenClawConfig();
  const mode = String(cfg?.gateway?.auth?.mode || "").trim().toLowerCase();
  const token = String(cfg?.gateway?.auth?.token || "").trim();
  if (mode === "token" && token) {
    return token;
  }
  return void 0;
}
function _ensureGatewaySessionOverride(tier, resolved) {
  const storePath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
  const sessionKey = `agent:main:quaid-llm-${tier}`;
  const now = Date.now();
  let store = {};
  try {
    if (fs.existsSync(storePath)) {
      store = JSON.parse(fs.readFileSync(storePath, "utf8")) || {};
    }
  } catch (err) {
    console.warn(
      `[quaid][llm] failed to read gateway session override store; recreating: ${String(err?.message || err)}`
    );
    store = {};
  }
  const prev = store[sessionKey] && typeof store[sessionKey] === "object" ? store[sessionKey] : {};
  const sessionId = `quaid-${tier}-${now}`;
  store[sessionKey] = {
    ...prev,
    sessionId,
    updatedAt: now,
    providerOverride: resolved.provider,
    modelOverride: resolved.model
  };
  try {
    fs.mkdirSync(path.dirname(storePath), { recursive: true });
    fs.writeFileSync(storePath, JSON.stringify(store, null, 2), { mode: 384 });
  } catch (err) {
    const msg = String(err?.message || err);
    throw new Error(`[quaid][llm] failed writing gateway session override store: ${msg}`, {
      cause: err
    });
  }
  return sessionKey;
}
async function callConfiguredLLM(systemPrompt, userMessage, modelTier, maxTokens, timeoutMs = 6e5) {
  const resolved = resolveTierModel(modelTier);
  const provider = normalizeProvider(resolved.provider);
  const started = Date.now();
  try {
    _ensureGatewaySessionOverride(modelTier, resolved);
  } catch (err) {
    const msg = String(err?.message || err);
    if (isFailHardEnabled()) {
      throw err;
    }
    console.warn(`[quaid][llm] gateway session override unavailable; continuing without session_id: ${msg}`);
  }
  console.log(
    `[quaid][llm] request tier=${modelTier} provider=${provider} model=${resolved.model} max_tokens=${maxTokens} system_len=${systemPrompt.length} user_len=${userMessage.length}`
  );
  const gatewayUrl = `${_getGatewayBaseUrl()}/v1/responses`;
  const token = _getGatewayToken();
  console.log(
    `[quaid][llm] gateway_prepare tier=${modelTier} gateway_url=${gatewayUrl} auth_token=${token ? "present" : "absent"}`
  );
  const headers = {
    "Content-Type": "application/json"
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const isTransientError = (status, err) => {
    if (typeof status === "number" && (status === 429 || status >= 500)) return true;
    const msg = String(err?.message || err || "").toLowerCase();
    const name = String(err?.name || "").toLowerCase();
    return name.includes("timeout") || msg.includes("timeout") || msg.includes("timed out") || msg.includes("econnreset") || msg.includes("econnrefused") || msg.includes("network") || msg.includes("fetch failed");
  };
  const readBodyWithTimeout = async (resp, bodyTimeoutMs) => {
    let timer = null;
    try {
      return await Promise.race([
        resp.text(),
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new Error(`gateway response body timeout after ${bodyTimeoutMs}ms`)),
            bodyTimeoutMs
          );
        })
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  };
  const maxAttempts = 2;
  let data = null;
  let gatewayRes = null;
  let rawBody = "";
  let lastError = null;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    const attemptStart = Date.now();
    try {
      gatewayRes = await fetch(gatewayUrl, {
        method: "POST",
        headers,
        body: JSON.stringify({
          model: `${resolved.provider}/${resolved.model}`,
          input: [
            { type: "message", role: "system", content: systemPrompt },
            { type: "message", role: "user", content: userMessage }
          ],
          max_output_tokens: maxTokens,
          stream: false
        }),
        signal: AbortSignal.timeout(timeoutMs)
      });
      const elapsedMs = Date.now() - attemptStart;
      const bodyTimeoutMs = Math.max(1, timeoutMs - elapsedMs);
      rawBody = await readBodyWithTimeout(gatewayRes, bodyTimeoutMs);
      try {
        data = rawBody ? JSON.parse(rawBody) : {};
      } catch (err) {
        const parseMsg = String(err?.message || err);
        const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
        console.error(
          `[quaid][llm] gateway_parse_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} parse_error=${JSON.stringify(parseMsg)} body_preview=${JSON.stringify(bodyPreview)}`
        );
        throw new Error(
          `Gateway response parse failed (${gatewayRes.status} ${gatewayRes.statusText}): ${parseMsg}`,
          { cause: err }
        );
      }
      if (!gatewayRes.ok) {
        const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
        console.error(
          `[quaid][llm] gateway_http_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} body_preview=${JSON.stringify(bodyPreview)}`
        );
        const err = data?.error?.message || data?.message || `Gateway OpenResponses error ${gatewayRes.status}`;
        if (attempt < maxAttempts && isTransientError(gatewayRes.status, err)) {
          console.warn(`[quaid][llm] transient gateway error, retrying attempt=${attempt + 1}/${maxAttempts}`);
          await new Promise((r) => setTimeout(r, 200 * attempt));
          continue;
        }
        throw new Error(
          `[quaid][llm] tier=${modelTier} provider=${provider} model=${resolved.model} status=${gatewayRes.status} error=${String(err)}`
        );
      }
      break;
    } catch (err) {
      lastError = err;
      const durationMs2 = Date.now() - started;
      console.error(
        `[quaid][llm] gateway_fetch_error tier=${modelTier} duration_ms=${durationMs2} error=${err?.name || "Error"}:${err?.message || String(err)} attempt=${attempt}/${maxAttempts}`
      );
      if (attempt < maxAttempts && isTransientError(gatewayRes?.status ?? null, err)) {
        await new Promise((r) => setTimeout(r, 200 * attempt));
        continue;
      }
      throw err;
    }
  }
  if (!gatewayRes || !gatewayRes.ok) {
    if (lastError instanceof Error) {
      throw lastError;
    }
    throw new Error(
      `[quaid][llm] gateway call failed with non-Error rejection: ${String(lastError || "unknown")}`,
      { cause: lastError ? new Error(String(lastError)) : void 0 }
    );
  }
  const text = typeof data.output_text === "string" ? data.output_text : Array.isArray(data.output) ? data.output.flatMap((o) => Array.isArray(o?.content) ? o.content : []).filter((c) => (c?.type === "output_text" || c?.type === "text") && typeof c?.text === "string").map((c) => c.text).join("\n") : "";
  const durationMs = Date.now() - started;
  console.log(`[quaid][llm] response provider=${provider} model=${resolved.model} duration_ms=${durationMs} output_len=${text.length} status=${gatewayRes.status}`);
  return {
    text,
    model: resolved.model,
    input_tokens: data?.usage?.input_tokens || 0,
    output_tokens: data?.usage?.output_tokens || 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
    truncated: false
  };
}
function _spawnWithTimeout(script, command, args, label, env, timeoutMs = PYTHON_BRIDGE_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const proc = spawn("python3", [script, command, ...args], {
      cwd: WORKSPACE,
      env: buildPythonEnv(env)
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    let killTimer = null;
    const timer = setTimeout(() => {
      if (!settled) {
        try {
          proc.kill("SIGTERM");
        } catch {
        }
        killTimer = setTimeout(() => {
          if (!settled) {
            try {
              proc.kill("SIGKILL");
            } catch {
            }
          }
        }, 5e3);
        settled = true;
        reject(new Error(`${label} timeout after ${timeoutMs}ms: ${command} ${args.join(" ")}`));
      }
    }, timeoutMs);
    proc.stdout.on("data", (data) => {
      stdout += data;
    });
    proc.stderr.on("data", (data) => {
      stderr += data;
    });
    proc.on("close", (code) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (killTimer) {
        clearTimeout(killTimer);
        killTimer = null;
      }
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        const stderrText = stderr.trim();
        const stdoutText = stdout.trim();
        const detail = [stderrText ? `stderr: ${stderrText}` : "", stdoutText ? `stdout: ${stdoutText}` : ""].filter(Boolean).join(" | ").slice(0, 1e3);
        reject(new Error(`${label} error (exit=${String(code)}): ${detail}`));
      }
    });
    proc.on("error", (err) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      if (killTimer) {
        clearTimeout(killTimer);
        killTimer = null;
      }
      reject(err);
    });
  });
}
async function callExtractPipeline(opts) {
  const tmpPath = path.join(QUAID_TMP_DIR, `extract-input-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
  fs.writeFileSync(tmpPath, opts.transcript, { mode: 384 });
  const args = [
    tmpPath,
    "--owner",
    opts.owner,
    "--label",
    opts.label,
    "--json"
  ];
  if (opts.sessionId) {
    args.push("--session-id", opts.sessionId);
  }
  if (!opts.writeSnippets) {
    args.push("--no-snippets");
  }
  if (!opts.writeJournal) {
    args.push("--no-journal");
  }
  try {
    const output = await _spawnWithTimeout(
      EXTRACT_SCRIPT,
      tmpPath,
      args.slice(1),
      "extract",
      {},
      EXTRACT_PIPELINE_TIMEOUT_MS
    );
    const parsed = JSON.parse(output || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("extract pipeline returned non-object JSON payload");
    }
    return parsed;
  } catch (err) {
    const cause = err instanceof Error ? err : new Error(String(err));
    const msg = String(cause.message || cause);
    throw new Error(
      `[quaid] extract pipeline parse/exec failed: ${msg.slice(0, 500)}`,
      { cause }
    );
  } finally {
    try {
      fs.unlinkSync(tmpPath);
    } catch (err) {
      console.warn(`[quaid] Failed cleaning extraction temp file ${tmpPath}: ${String(err?.message || err)}`);
    }
  }
}
function spawnNotifyScript(scriptBody) {
  const tmpFile = path.join(QUAID_NOTIFY_DIR, `notify-${Date.now()}-${Math.random().toString(36).slice(2)}.py`);
  const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
  const appendNotifyLog = (msg) => {
    try {
      fs.appendFileSync(notifyLogFile, `${(/* @__PURE__ */ new Date()).toISOString()} ${msg}
`);
    } catch {
    }
  };
  const preamble = `import sys, os
sys.path.insert(0, ${JSON.stringify(PYTHON_PLUGIN_ROOT)})
`;
  const cleanup = `
os.unlink(${JSON.stringify(tmpFile)})
`;
  let launched = false;
  let notifyLogFd = null;
  fs.writeFileSync(tmpFile, preamble + scriptBody + cleanup, { mode: 384 });
  try {
    notifyLogFd = fs.openSync(notifyLogFile, "a");
    const proc = spawn("python3", [tmpFile], {
      detached: true,
      stdio: ["ignore", notifyLogFd, notifyLogFd],
      env: buildPythonEnv()
    });
    launched = true;
    proc.on("error", (err) => {
      appendNotifyLog(`[notify-worker-error] spawn failed: ${err.message}`);
      try {
        fs.unlinkSync(tmpFile);
      } catch {
      }
    });
    proc.unref();
  } catch (err) {
    appendNotifyLog(`[notify-worker-error] launch failed: ${String(err?.message || err)}`);
    if (!launched) {
      try {
        fs.unlinkSync(tmpFile);
      } catch {
      }
    }
  } finally {
    if (notifyLogFd !== null) {
      try {
        fs.closeSync(notifyLogFd);
      } catch {
      }
    }
  }
  return launched;
}
function _loadJanitorNudgeState() {
  try {
    if (fs.existsSync(JANITOR_NUDGE_STATE_PATH)) {
      return JSON.parse(fs.readFileSync(JANITOR_NUDGE_STATE_PATH, "utf8")) || {};
    }
  } catch (err) {
    console.warn(`[quaid] Failed to load janitor nudge state: ${String(err?.message || err)}`);
  }
  return {};
}
function _saveJanitorNudgeState(state) {
  try {
    fs.writeFileSync(JANITOR_NUDGE_STATE_PATH, JSON.stringify(state, null, 2), { mode: 384 });
  } catch (err) {
    console.warn(`[quaid] Failed to save janitor nudge state: ${String(err?.message || err)}`);
  }
}
function queueDelayedLlmRequest(message, kind = "janitor", priority = "normal") {
  return queueDelayedRequest(
    DELAYED_LLM_REQUESTS_PATH,
    message,
    kind,
    priority,
    "quaid_adapter",
    isFailHardEnabled()
  );
}
function maybeQueueJanitorHealthAlert() {
  const issue = facade.getJanitorHealthIssue();
  if (!issue) return;
  const now = Date.now();
  const state = _loadJanitorNudgeState();
  const lastAt = Number(state.lastJanitorHealthAlertAt || 0);
  const cooldown = 6 * 60 * 60 * 1e3;
  if (now - lastAt < cooldown && String(state.lastJanitorHealthIssue || "") === issue) return;
  if (queueDelayedLlmRequest(issue, "janitor_health", "high")) {
    state.lastJanitorHealthAlertAt = now;
    state.lastJanitorHealthIssue = issue;
    _saveJanitorNudgeState(state);
  }
}
function maybeSendJanitorNudges() {
  const now = Date.now();
  const state = _loadJanitorNudgeState();
  const lastInstallNudge = Number(state.lastInstallNudgeAt || 0);
  const lastApprovalNudge = Number(state.lastApprovalNudgeAt || 0);
  const NUDGE_COOLDOWN_MS = 6 * 60 * 60 * 1e3;
  try {
    if (fs.existsSync(PENDING_INSTALL_MIGRATION_PATH) && now - lastInstallNudge > NUDGE_COOLDOWN_MS) {
      const raw = JSON.parse(fs.readFileSync(PENDING_INSTALL_MIGRATION_PATH, "utf8"));
      if (raw?.status === "pending") {
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("Hey, I see you just installed Quaid. Want me to help migrate important context into managed memory now?")
`);
        state.lastInstallNudgeAt = now;
      }
    }
  } catch (err) {
    console.warn(`[quaid] Install nudge check failed: ${String(err?.message || err)}`);
  }
  try {
    if (fs.existsSync(PENDING_APPROVAL_REQUESTS_PATH) && now - lastApprovalNudge > NUDGE_COOLDOWN_MS) {
      const raw = JSON.parse(fs.readFileSync(PENDING_APPROVAL_REQUESTS_PATH, "utf8"));
      const requests = Array.isArray(raw?.requests) ? raw.requests : [];
      const pendingCount = requests.filter((r) => r?.status === "pending").length;
      if (pendingCount > 0) {
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("Quaid has ${pendingCount} pending approval request(s). Review pending maintenance approvals.")
`);
        state.lastApprovalNudgeAt = now;
      }
    }
  } catch (err) {
    console.warn(`[quaid] Approval nudge check failed: ${String(err?.message || err)}`);
  }
  _saveJanitorNudgeState(state);
}
async function emitProjectEvent(messages, trigger, sessionId) {
  try {
    const staged = await facade.stageProjectEvent(messages, trigger, sessionId, void 0, QUICK_PROJECT_SUMMARY_TIMEOUT_MS);
    if (!staged) {
      return;
    }
    const bgApiKey = _getAnthropicCredential();
    const logFile = path.join(WORKSPACE, "logs/project-updater.log");
    const logDir = path.dirname(logFile);
    if (!fs.existsSync(logDir)) {
      fs.mkdirSync(logDir, { recursive: true });
    }
    const logFd = fs.openSync(logFile, "a");
    try {
      const proc = spawn("python3", [PROJECT_UPDATER, "process-event", staged.eventPath], {
        detached: true,
        stdio: ["ignore", logFd, logFd],
        cwd: WORKSPACE,
        env: buildPythonEnv({ ...bgApiKey ? { ANTHROPIC_API_KEY: bgApiKey } : {} })
      });
      proc.unref();
    } finally {
      fs.closeSync(logFd);
    }
    console.log(`[quaid] Emitted project event: ${trigger} -> ${staged.projectHint || "unknown"}`);
  } catch (err) {
    console.error("[quaid] Failed to emit project event:", err.message);
    if (isFailHardEnabled()) {
      throw err;
    }
  }
}
function preprocessTranscriptText(text) {
  return String(text || "").replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "").replace(/\n?\[message_id:\s*\d+\]/gi, "").trim();
}
function shouldSkipTranscriptText(_role, text) {
  if (!text) return true;
  if (text.startsWith("GatewayRestart:") || text.startsWith("System:")) return true;
  if (text.includes('"kind": "restart"')) return true;
  if (text.includes("HEARTBEAT") && text.includes("HEARTBEAT_OK")) return true;
  if (text.replace(/[*_<>\/b\s]/g, "").startsWith("HEARTBEAT_OK")) return true;
  return false;
}
const facade = createQuaidFacade({
  workspace: WORKSPACE,
  pluginRoot: PYTHON_PLUGIN_ROOT,
  dbPath: DB_PATH,
  eventSource: "openclaw_adapter",
  execPython: createPythonBridgeExecutor({
    scriptPath: PYTHON_SCRIPT,
    dbPath: DB_PATH,
    workspace: WORKSPACE,
    pluginRoot: PYTHON_PLUGIN_ROOT
  }),
  execExtractPipeline: (tmpPath, args) => _spawnWithTimeout(EXTRACT_SCRIPT, tmpPath, args, "extract", {}, EXTRACT_PIPELINE_TIMEOUT_MS),
  execDocsRag: (cmd, args) => _spawnWithTimeout(DOCS_RAG, cmd, args, "docs_rag", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  }),
  execDocsRegistry: (cmd, args) => _spawnWithTimeout(DOCS_REGISTRY, cmd, args, "docs_registry", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  }),
  execDocsUpdater: (cmd, args) => {
    const apiKey = _getAnthropicCredential();
    return _spawnWithTimeout(DOCS_UPDATER, cmd, args, "docs_updater", {
      QUAID_HOME: WORKSPACE,
      CLAWDBOT_WORKSPACE: WORKSPACE,
      ...apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}
    });
  },
  execEvents: (cmd, args) => _spawnWithTimeout(EVENTS_SCRIPT, cmd, args, "events", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  }, EVENTS_EMIT_TIMEOUT_MS),
  callLLM: callConfiguredLLM,
  getMemoryConfig,
  isSystemEnabled,
  isFailHardEnabled,
  transcriptFormat: {
    preprocessText: preprocessTranscriptText,
    shouldSkipText: shouldSkipTranscriptText,
    speakerLabel: (role) => role === "user" ? "User" : "Alfie"
  }
});
const recallStoreGuidance = facade.renderDatastoreGuidance();
const getProjectNames = () => facade.getProjectNames();
const quaidPlugin = {
  id: "quaid",
  name: "Memory (Local Graph)",
  description: "Local graph-based memory with SQLite + Ollama embeddings",
  kind: "memory",
  configSchema,
  register(api) {
    console.log("[quaid] Registering local graph memory plugin");
    runStartupSelfCheck();
    const contractDecl = loadAdapterContractDeclarations();
    const strictContracts = isPluginStrictMode();
    if (contractDecl.enabled) {
      validateApiSurface(contractDecl.api, strictContracts, (m) => console.warn(m));
    }
    const registeredApi = /* @__PURE__ */ new Set(["openclaw_adapter_entry"]);
    const onChecked = (eventName, handler, options) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      return api.on(eventName, handler, options);
    };
    const registerInternalHookChecked = (eventName, handler, options) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      return api.registerHook(eventName, handler, options);
    };
    const registerToolChecked = (factory) => {
      const spec = factory();
      const toolName = String(spec?.name || "").trim();
      if (contractDecl.enabled) {
        assertDeclaredRegistration("tools", toolName, contractDecl.tools, strictContracts, (m) => console.warn(m));
      }
      return api.registerTool(() => spec);
    };
    const registerHttpRouteChecked = (route) => {
      const routePath = String(route?.path || "").trim();
      if (contractDecl.enabled) {
        assertDeclaredRegistration("api", routePath, contractDecl.api, strictContracts, (m) => console.warn(m));
      }
      if (routePath) {
        registeredApi.add(routePath);
      }
      return api.registerHttpRoute(route);
    };
    const dataDir = path.dirname(DB_PATH);
    if (!fs.existsSync(dataDir)) {
      fs.mkdirSync(dataDir, { recursive: true });
    }
    if (!fs.existsSync(DB_PATH)) {
      console.log("[quaid] Database not found, initializing datastore...");
      try {
        execFileSync("python3", [PYTHON_SCRIPT, "init"], {
          timeout: 2e4,
          env: buildPythonEnv()
        });
        console.log("[quaid] Datastore initialization complete");
      } catch (err) {
        console.error("[quaid] Datastore initialization failed:", err.message);
        if (isFailHardEnabled()) {
          throw err;
        }
      }
    }
    void facade.getStatsParsed().then((stats) => {
      if (stats) {
        console.log(
          `[quaid] Database ready: ${stats.total_nodes} nodes, ${stats.edges} edges`
        );
      }
    }).catch((err) => {
      console.warn(
        `[quaid] stats probe failed: ${String(err?.message || err)}`
      );
    });
    const beforeAgentStartHandler = async (event, ctx) => {
      if (isInternalQuaidSession(ctx?.sessionId)) {
        return;
      }
      try {
        maybeSendJanitorNudges();
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Janitor nudge dispatch failed: ${String(err?.message || err)}`);
      }
      try {
        maybeQueueJanitorHealthAlert();
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Janitor health alert dispatch failed: ${String(err?.message || err)}`);
      }
      timeoutManager.onAgentStart();
      if (!isSystemEnabled("journal")) {
      } else {
        try {
          const journalConfig = getMemoryConfig().docs?.journal || {};
          const journalMode = journalConfig.mode || "distilled";
          if (journalMode === "full") {
            const journalDir = path.join(WORKSPACE, journalConfig.journalDir || "journal");
            let journalFiles = [];
            try {
              journalFiles = fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md")).sort();
            } catch (err) {
              if (isFailHardEnabled()) {
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
                if (isFailHardEnabled()) {
                  throw new Error(`[quaid] Journal injection read failed for ${file} under failHard`, { cause: err });
                }
                console.warn(`[quaid] Journal injection read failed for ${file}: ${String(err?.message || err)}`);
              }
            }
            if (journalContent) {
              const header = "[JOURNAL \u2014 Full Soul Mode]\nThese are your recent journal reflections. They are part of your inner life.\n";
              event.prependContext = event.prependContext ? `${event.prependContext}

${header}${journalContent}` : `${header}${journalContent}`;
              console.log(`[quaid] Full soul mode: injected ${journalFiles.length} journal files`);
            }
          }
        } catch (err) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.warn(`[quaid] Journal injection failed (non-fatal): ${err.message}`);
        }
      }
      const autoInjectEnabled = process.env.MEMORY_AUTO_INJECT === "1" || getMemoryConfig().retrieval?.autoInject === true;
      if (!autoInjectEnabled) {
        return;
      }
      if (!event.prompt || event.prompt.length < 5) {
        return;
      }
      try {
        const rawPrompt = event.prompt;
        let query = rawPrompt.replace(/^System:\s*/i, "").replace(/^\s*(\[.*?\]\s*)+/s, "").replace(/^---\s*/m, "").trim();
        query = query.replace(/Conversation info \(untrusted metadata\):[\s\S]*?```[\s\S]*?```/gi, "").trim();
        if (query.length < 3) {
          query = rawPrompt;
        }
        if (/^(A new session|Read HEARTBEAT|HEARTBEAT|You are being asked to|\/\w)/.test(query)) {
          return;
        }
        if (query.startsWith("Extract memorable facts and journal entries from this conversation:")) {
          return;
        }
        if (facade.isInternalMaintenancePrompt(query)) {
          return;
        }
        const ACKNOWLEDGMENTS = /^(ok|okay|yes|no|sure|thanks|thank you|got it|sounds good|perfect|great|cool|alright|yep|nope|right|correct|agreed|absolutely|definitely|nice|good|fine|hm+|ah+|oh+)\s*[.!?]?$/i;
        const words = query.trim().split(/\s+/).filter((w) => w.length > 1);
        if (words.length < 3 || ACKNOWLEDGMENTS.test(query.trim())) {
          return;
        }
        const autoInjectK = facade.computeDynamicK();
        const useTotalRecallForInject = isPreInjectionPassEnabled();
        const routerFailOpen = Boolean(
          getMemoryConfig().retrieval?.routerFailOpen ?? getMemoryConfig().retrieval?.router_fail_open ?? true
        );
        const injectLimit = autoInjectK;
        const injectIntent = "general";
        const injectDomain = { personal: true };
        const injectDatastores = useTotalRecallForInject ? void 0 : ["vector_basic", "graph"];
        const allMemories = await recallMemories({
          query,
          limit: injectLimit,
          expandGraph: true,
          datastores: injectDatastores,
          routeStores: useTotalRecallForInject,
          intent: injectIntent,
          domain: injectDomain,
          failOpen: routerFailOpen,
          waitForExtraction: false,
          sourceTag: "auto_inject"
        });
        if (!allMemories.length) return;
        const currentOwner = facade.resolveOwner();
        const filtered = allMemories.filter(
          (m) => !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner)
        );
        const uniqueSessionId = extractSessionId(event.messages || [], ctx);
        const injectionLogPath = getInjectionLogPath(uniqueSessionId);
        let previouslyInjected = [];
        try {
          const parsed = JSON.parse(fs.readFileSync(injectionLogPath, "utf8"));
          const logData = parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
          const rawInjected = logData?.injected ?? logData?.memoryTexts;
          previouslyInjected = Array.isArray(rawInjected) ? rawInjected.map((item) => String(item || "").trim()).filter(Boolean) : [];
        } catch (err) {
          console.warn(`[quaid] Injection log read failed for ${injectionLogPath}: ${String(err?.message || err)}`);
        }
        const newMemories = filtered.filter((m) => !previouslyInjected.includes(m.id || m.text));
        const toInject = newMemories.slice(0, injectLimit);
        if (!toInject.length) return;
        const formatted = facade.formatMemoriesForInjection(toInject);
        event.prependContext = event.prependContext ? `${event.prependContext}

${formatted}` : formatted;
        console.log(`[quaid] Auto-injected ${toInject.length} memories for "${query.slice(0, 50)}..."`);
        try {
          if (facade.shouldNotifyFeature("retrieval", "summary")) {
            const vectorInjected = toInject.filter((m) => m.via === "vector" || !m.via && m.category !== "graph");
            const graphInjected = toInject.filter((m) => m.via === "graph" || m.category === "graph");
            const dataFile = path.join(QUAID_TMP_DIR, `auto-inject-recall-${Date.now()}.json`);
            fs.writeFileSync(dataFile, JSON.stringify({
              memories: toInject.map((m) => ({
                text: m.text,
                similarity: Math.round((m.similarity || 0) * 100),
                via: m.via || "vector",
                category: m.category || ""
              })),
              source_breakdown: {
                vector_count: vectorInjected.length,
                graph_count: graphInjected.length,
                query,
                mode: "auto_inject"
              }
            }), { mode: 384 });
            const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
            if (!launchedNotify) {
              try {
                fs.unlinkSync(dataFile);
              } catch {
              }
            }
            console.log("[quaid] Auto-inject recall notification dispatched");
          }
        } catch (notifyErr) {
          console.warn(`[quaid] Auto-inject recall notification skipped: ${notifyErr.message}`);
        }
        try {
          const newIds = toInject.map((m) => m.id || m.text);
          const mergedIds = [...previouslyInjected, ...newIds];
          fs.writeFileSync(injectionLogPath, JSON.stringify({
            injected: mergedIds.slice(-MAX_INJECTION_IDS_PER_SESSION),
            lastInjectedAt: (/* @__PURE__ */ new Date()).toISOString()
          }), { mode: 384 });
          pruneInjectionLogFiles();
        } catch (err) {
          console.warn(`[quaid] Injection log write failed for ${injectionLogPath}: ${String(err?.message || err)}`);
        }
      } catch (error) {
        console.error("[quaid] Auto-injection error:", error);
      }
    };
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    registerInternalHookChecked("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });
    console.log("[quaid] agent_end auto-capture disabled; using session_end + compaction hooks");
    const runtimeEvents = api?.runtime?.events;
    const parseSessionIdFromTranscriptPath = (sessionFile) => {
      const base = path.basename(String(sessionFile || ""));
      const match = base.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
      return match ? match[0].toLowerCase() : "";
    };
    if (runtimeEvents && typeof runtimeEvents.onSessionTranscriptUpdate === "function") {
      runtimeEvents.onSessionTranscriptUpdate((update) => {
        try {
          const sessionFile = String(update?.sessionFile || "").trim();
          if (!sessionFile || !fs.existsSync(sessionFile)) return;
          const messages = readMessagesFromSessionFile(sessionFile);
          if (!Array.isArray(messages) || messages.length === 0) return;
          const detail = facade.detectLifecycleSignal(messages);
          if (!detail) return;
          const sessionId = parseSessionIdFromTranscriptPath(sessionFile) || String(update?.sessionId || "").trim();
          if (!sessionId) {
            console.log(`[quaid][signal] transcript_update missing session id file=${sessionFile}`);
            return;
          }
          if (!facade.shouldProcessLifecycleSignal(sessionId, detail)) {
            console.log(`[quaid][signal] suppressed duplicate ${detail.label} session=${sessionId} source=transcript_update`);
            return;
          }
          timeoutManager.queueExtractionSignal(sessionId, detail.label, {
            source: "transcript_update"
          });
          console.log(`[quaid][signal] queued ${detail.label} session=${sessionId} source=transcript_update`);
        } catch (err) {
          console.error("[quaid] transcript_update fallback failed:", err);
        }
      });
      console.log("[quaid] Registered runtime.events.onSessionTranscriptUpdate lifecycle fallback");
    }
    if (isSystemEnabled("memory")) {
      registerToolChecked(
        () => ({
          name: "memory_recall",
          description: `Search your memory for personal facts, preferences, relationships, project details, and past conversations. Always use this tool when you're unsure about something or need to verify a detail \u2014 if you might know it, search for it.

USE THIS TOOL LIBERALLY. If you're about to say "I don't have information about..." or "I'm not sure...", SEARCH FIRST. It's better to search and find nothing than to miss a memory you have.

COST AWARE RETRIEVAL ORDER:
1) memory_recall (cheap; default first move)
2) projects_search / project docs (more expensive; use for file-backed implementation detail)
3) session_recall (most expensive/noisy; use only when memory+project docs are insufficient)

USE WHEN: Any question about the user, their life, people they know, projects they work on, preferences, history, past decisions, technical details about their projects, or anything that might have come up in a previous conversation.
SKIP WHEN: General knowledge questions, greetings, short acknowledgments.

QUERY TIPS: Use specific names and topics. Try multiple searches with different phrasings if the first doesn't return what you need.
options.graph.depth: Set to 2 for relationship queries (e.g., nephew = sibling's child). Default 1 is usually sufficient.
options.filters.dateFrom/dateTo: Use YYYY-MM-DD format to filter memories by date range.

${recallStoreGuidance}`,
          parameters: Type.Object({
            query: Type.String({ description: "Search query - use entity names and specific topics" }),
            options: Type.Optional(Type.Object({
              limit: Type.Optional(
                Type.Number({ description: "Max results to return. Default reads from config." })
              ),
              datastores: Type.Optional(
                Type.Array(
                  Type.Union([
                    Type.Literal("vector"),
                    Type.Literal("vector_basic"),
                    Type.Literal("vector_technical"),
                    Type.Literal("graph"),
                    Type.Literal("journal"),
                    Type.Literal("project")
                  ]),
                  { description: "Knowledge datastores to query." }
                )
              ),
              graph: Type.Optional(Type.Object({
                expand: Type.Optional(
                  Type.Boolean({ description: "Traverse relationship graph - use for people/family queries (default: true)." })
                ),
                depth: Type.Optional(
                  Type.Number({ description: "Graph traversal depth (default: 1). Use 2 for extended relationships." })
                )
              })),
              routing: Type.Optional(Type.Object({
                enabled: Type.Optional(
                  Type.Boolean({ description: "Enable total_recall planning pass (query cleanup + store routing)." })
                ),
                reasoning: Type.Optional(
                  Type.Union([
                    Type.Literal("fast"),
                    Type.Literal("deep")
                  ], { description: "Reasoning model for routing pass." })
                ),
                intent: Type.Optional(
                  Type.Union([
                    Type.Literal("general"),
                    Type.Literal("agent_actions"),
                    Type.Literal("relationship"),
                    Type.Literal("technical")
                  ], { description: "Intent facet for routing and ranking boosts." })
                ),
                failOpen: Type.Optional(
                  Type.Boolean({ description: "If true, router/prepass failures return no recall instead of throwing an error." })
                )
              })),
              filters: Type.Optional(Type.Object({
                domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean(), description: 'Domain filter map. Example: {"all":true} or {"technical":true}.' })),
                domainBoost: Type.Optional(Type.Union([
                  Type.Array(Type.String({ description: "Domain IDs to boost at default x1.3." })),
                  Type.Object({}, { additionalProperties: Type.Number({ description: 'Domain boost multiplier by domain id (e.g. {"technical":1.5}).' }) })
                ])),
                dateFrom: Type.Optional(
                  Type.String({ description: "Only return memories from this date onward (YYYY-MM-DD)." })
                ),
                dateTo: Type.Optional(
                  Type.String({ description: "Only return memories up to this date (YYYY-MM-DD)." })
                ),
                project: Type.Optional(
                  Type.String({ description: "Optional project/domain filter for technical memory results." })
                ),
                docs: Type.Optional(
                  Type.Array(Type.String({ description: "Optional doc path/name filters when project-store recall is used." }))
                )
              })),
              ranking: Type.Optional(Type.Object({
                sourceTypeBoosts: Type.Optional(Type.Object({
                  user: Type.Optional(Type.Number()),
                  assistant: Type.Optional(Type.Number()),
                  both: Type.Optional(Type.Number()),
                  tool: Type.Optional(Type.Number()),
                  import: Type.Optional(Type.Number())
                }))
              })),
              datastoreOptions: Type.Optional(Type.Object({
                vector: Type.Optional(Type.Object({
                  domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean() })),
                  project: Type.Optional(Type.String())
                })),
                graph: Type.Optional(Type.Object({
                  depth: Type.Optional(Type.Number()),
                  domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean() })),
                  project: Type.Optional(Type.String())
                })),
                project: Type.Optional(Type.Object({
                  project: Type.Optional(Type.String()),
                  docs: Type.Optional(Type.Array(Type.String()))
                })),
                journal: Type.Optional(Type.Object({})),
                vector_basic: Type.Optional(Type.Object({})),
                vector_technical: Type.Optional(Type.Object({}))
              }))
            }))
          }),
          async execute(toolCallId, params) {
            try {
              const configPath = path.join(WORKSPACE, "config/memory.json");
              let maxLimit = 50;
              try {
                const configData = JSON.parse(fs.readFileSync(configPath, "utf-8"));
                const rawMaxLimit = configData?.retrieval?.maxLimit ?? configData?.retrieval?.max_limit ?? 50;
                const parsedMaxLimit = Number(rawMaxLimit);
                maxLimit = Number.isFinite(parsedMaxLimit) && parsedMaxLimit > 0 ? parsedMaxLimit : 50;
              } catch (err) {
                console.warn(`[quaid] memory_recall maxLimit config read failed: ${String(err?.message || err)}`);
              }
              const dynamicK = facade.computeDynamicK();
              const { query, options = {} } = params || {};
              const requestedLimit = options.limit;
              const expandGraph = options.graph?.expand ?? true;
              const graphDepth = options.graph?.depth ?? 1;
              const datastores = options.datastores;
              const routeStores = options.routing?.enabled;
              const reasoning = options.routing?.reasoning ?? "fast";
              const intent = options.routing?.intent ?? "general";
              const domain = options.filters?.domain && typeof options.filters.domain === "object" ? options.filters.domain : { all: true };
              const domainBoost = Array.isArray(options.filters?.domainBoost) || options.filters?.domainBoost && typeof options.filters.domainBoost === "object" ? options.filters?.domainBoost : void 0;
              const dateFrom = options.filters?.dateFrom;
              const dateTo = options.filters?.dateTo;
              const project = options.filters?.project;
              const docs = options.filters?.docs;
              const ranking = options.ranking;
              const datastoreOptions = options.datastoreOptions;
              const routerFailOpen = Boolean(
                options.routing?.failOpen ?? getMemoryConfig().retrieval?.routerFailOpen ?? getMemoryConfig().retrieval?.router_fail_open ?? true
              );
              if (typeof query === "string" && query.trim().startsWith("Extract memorable facts and journal entries from this conversation:")) {
                return {
                  content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                  details: { count: 0, skippedInternalQuery: true }
                };
              }
              const limit = Math.min(requestedLimit ?? dynamicK, maxLimit);
              const depth = Math.min(Math.max(graphDepth, 1), 3);
              const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
              const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
              console.log(`[quaid] memory_recall: query="${query?.slice(0, 50)}...", requestedLimit=${requestedLimit}, dynamicK=${dynamicK} (${facade.getActiveNodeCount()} nodes), maxLimit=${maxLimit}, finalLimit=${limit}, expandGraph=${expandGraph}, graphDepth=${depth}, requestedDatastores=${selectedStores.join(",")}, routed=${shouldRouteStores}, reasoning=${reasoning}, intent=${intent}, domain=${JSON.stringify(domain)}, domainBoost=${JSON.stringify(domainBoost || {})}, project=${project || "any"}, dateFrom=${dateFrom}, dateTo=${dateTo}`);
              const results = await recallMemories({
                query,
                limit,
                expandGraph,
                graphDepth: depth,
                datastores: selectedStores,
                routeStores: shouldRouteStores,
                reasoning,
                intent,
                ranking,
                domain,
                domainBoost,
                project,
                datastoreOptions,
                failOpen: routerFailOpen,
                dateFrom,
                dateTo,
                docs,
                waitForExtraction: true,
                sourceTag: "tool"
              });
              if (results.length === 0) {
                return {
                  content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                  details: { count: 0 }
                };
              }
              const vectorResults = results.filter((r) => facade.isVectorRecallResult(r));
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
                if (vectorResults.length > 0) {
                  text += "\n";
                }
                text += "**Graph Discoveries:**\n";
                graphResults.forEach((r, i) => {
                  text += `${i + 1}. [MEMORY] ${r.text}
`;
                });
              }
              if (journalResults.length > 0) {
                if (vectorResults.length > 0 || graphResults.length > 0) {
                  text += "\n";
                }
                text += "**Journal Signals:**\n";
                journalResults.forEach((r, i) => {
                  text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)
`;
                });
              }
              if (projectResults.length > 0) {
                if (vectorResults.length > 0 || graphResults.length > 0 || journalResults.length > 0) {
                  text += "\n";
                }
                text += "**Project Knowledge:**\n";
                projectResults.forEach((r, i) => {
                  text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)
`;
                });
              }
              try {
                if (facade.shouldNotifyFeature("retrieval", "summary") && results.length > 0) {
                  const memoryData = results.map((m) => ({
                    text: m.text,
                    similarity: Math.round((m.similarity || 0) * 100),
                    via: m.via || "vector",
                    category: m.category || ""
                  }));
                  const sourceBreakdown = {
                    vector_count: vectorResults.length,
                    graph_count: graphResults.length,
                    journal_count: journalResults.length,
                    project_count: projectResults.length,
                    query,
                    mode: "tool"
                  };
                  const dataFile2 = path.join(QUAID_TMP_DIR, `recall-data-${Date.now()}.json`);
                  fs.writeFileSync(dataFile2, JSON.stringify({ memories: memoryData, source_breakdown: sourceBreakdown }), { mode: 384 });
                  const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile2)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile2)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
                  if (!launchedNotify) {
                    try {
                      fs.unlinkSync(dataFile2);
                    } catch {
                    }
                  }
                }
              } catch (notifyErr) {
                console.warn(`[quaid] Memory recall notification skipped: ${notifyErr.message}`);
              }
              return {
                content: [
                  { type: "text", text: text.trim() }
                ],
                details: {
                  count: results.length,
                  memories: results,
                  vectorCount: vectorResults.length,
                  graphCount: graphResults.length,
                  journalCount: journalResults.length,
                  projectCount: projectResults.length
                }
              };
            } catch (err) {
              console.error("[quaid] memory_recall error:", err);
              if (isFailHardEnabled()) {
                throw err;
              }
              const errObj = err instanceof Error ? err : new Error(String(err));
              return {
                content: [{ type: "text", text: `Error recalling memories: ${errObj.message}` }],
                details: {
                  error: errObj.message,
                  error_name: errObj.name,
                  error_cause: errObj.cause ? String(errObj.cause?.message || errObj.cause) : void 0
                }
              };
            }
          }
        })
      );
      registerToolChecked(
        () => ({
          name: "memory_store",
          description: `Queue a fact for memory extraction at next compaction. The fact will go through full quality review (Opus extraction with edges and janitor review) rather than being stored directly.

Only use when the user EXPLICITLY asks you to remember something (e.g., "remember this", "save this"). Do NOT proactively store facts \u2014 auto-extraction at compaction handles that.`,
          parameters: Type.Object({
            text: Type.String({ description: "Information to remember" }),
            category: Type.Optional(
              Type.String({
                enum: ["preference", "fact", "decision", "entity", "other"]
              })
            )
          }),
          async execute(_toolCallId, params, ctx) {
            try {
              const { text, category = "fact" } = params || {};
              const sessionId = resolveMemoryStoreSessionId(ctx);
              facade.addMemoryNote(sessionId, text, category);
              console.log(`[quaid] memory_store: queued note for session ${sessionId}: "${text.slice(0, 60)}..."`);
              return {
                content: [{ type: "text", text: `Noted for memory extraction: "${text.slice(0, 100)}${text.length > 100 ? "..." : ""}" \u2014 will be processed with full quality review at next compaction.` }],
                details: { action: "queued", sessionId }
              };
            } catch (err) {
              console.error("[quaid] memory_store error:", err);
              if (isFailHardEnabled()) {
                throw err;
              }
              return {
                content: [{ type: "text", text: `Error queuing memory note: ${String(err)}` }],
                details: { error: String(err) }
              };
            }
          }
        })
      );
      registerToolChecked(
        () => ({
          name: "memory_forget",
          description: "Delete specific memories",
          parameters: Type.Object({
            query: Type.Optional(
              Type.String({ description: "Search to find memory" })
            ),
            memoryId: Type.Optional(
              Type.String({ description: "Specific memory ID" })
            )
          }),
          async execute(_toolCallId, params) {
            try {
              const { query, memoryId } = params || {};
              if (memoryId) {
                await facade.forget(["--id", memoryId]);
                return {
                  content: [{ type: "text", text: `Memory ${memoryId} forgotten.` }],
                  details: { action: "deleted", id: memoryId }
                };
              } else if (query) {
                await facade.forget([query]);
                return {
                  content: [{ type: "text", text: `Deleted memories matching: "${query}"` }],
                  details: { action: "deleted", query }
                };
              }
              return {
                content: [{ type: "text", text: "Provide query or memoryId." }],
                details: { error: "missing_param" }
              };
            } catch (err) {
              console.error("[quaid] memory_forget error:", err);
              if (isFailHardEnabled()) {
                throw err;
              }
              return {
                content: [{ type: "text", text: `Error deleting memory: ${String(err)}` }],
                details: { error: String(err) }
              };
            }
          }
        })
      );
    }
    registerToolChecked(
      () => ({
        name: "projects_search",
        description: "Search project documentation (architecture, implementation, reference guides). Use TOOLS.md to discover systems, then use this tool to find detailed docs. Returns relevant sections with file paths. Use when memory_recall is insufficient for project-level/file-backed answers. When answering from this tool, cite at least one concrete file/section hit.",
        parameters: Type.Object({
          query: Type.String({ description: "Search query" }),
          limit: Type.Optional(
            Type.Number({ description: "Max results (default 5)" })
          ),
          project: Type.Optional(
            Type.String({ description: `Filter by project name. Available: ${getProjectNames().join(", ") || "none"}` })
          ),
          docs: Type.Optional(
            Type.Array(Type.String({ description: "Optional doc path/name filters for RAG scope" }))
          )
        }),
        async execute(_toolCallId, params) {
          try {
            const { query, limit = 5, project, docs } = params || {};
            const searchArgs = ["--limit", String(limit)];
            if (project) {
              searchArgs.push("--project", project);
            }
            if (Array.isArray(docs) && docs.length > 0) {
              searchArgs.push("--docs", docs.join(","));
            }
            const results = await facade.docsSearch(query, searchArgs);
            let projectMdContent = "";
            if (project) {
              try {
                const cfg = JSON.parse(fs.readFileSync(path.join(WORKSPACE, "config/memory.json"), "utf-8"));
                const homeDir = cfg?.projects?.definitions?.[project]?.homeDir;
                if (homeDir) {
                  const mdPath = path.join(WORKSPACE, homeDir, "PROJECT.md");
                  if (fs.existsSync(mdPath)) {
                    projectMdContent = fs.readFileSync(mdPath, "utf-8");
                  }
                }
              } catch (err) {
                console.warn(`[quaid] projects_search PROJECT.md preload failed: ${String(err?.message || err)}`);
              }
            }
            let stalenessWarning = "";
            try {
              const stalenessJson = await facade.docsCheckStaleness();
              const staleRaw = JSON.parse(stalenessJson || "{}");
              const staleDocs = staleRaw && typeof staleRaw === "object" && !Array.isArray(staleRaw) ? staleRaw : {};
              const staleKeys = Object.keys(staleDocs);
              if (staleKeys.length > 0) {
                const warnings = staleKeys.map(
                  (k) => {
                    const entry = staleDocs[k] && typeof staleDocs[k] === "object" ? staleDocs[k] : {};
                    const gapHours = Number(entry?.gap_hours);
                    const staleSources = Array.isArray(entry?.stale_sources) ? entry.stale_sources : [];
                    return `  ${k} (${Number.isFinite(gapHours) ? gapHours : 0}h behind: ${staleSources.join(", ")})`;
                  }
                );
                stalenessWarning = `

STALENESS WARNING: The following docs may be outdated:
${warnings.join("\n")}
Consider running: python3 docs_updater.py update-stale --apply`;
              }
            } catch (err) {
              console.warn(`[quaid] projects_search staleness check failed: ${String(err?.message || err)}`);
            }
            const text = projectMdContent ? `--- PROJECT.md (${project}) ---
${projectMdContent}

--- Search Results ---
${results || "No results."}${stalenessWarning}` : results ? results + stalenessWarning : "No results found." + stalenessWarning;
            try {
              if (facade.shouldNotifyFeature("retrieval", "summary") && results) {
                const docResults = [];
                const lines = results.split("\n");
                for (const line of lines) {
                  const match = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
                  if (match) {
                    docResults.push({
                      doc: match[1].split("/").pop() || match[1],
                      section: match[2].trim(),
                      score: parseFloat(match[3])
                    });
                  }
                }
                if (docResults.length > 0) {
                  const dataFile3 = path.join(QUAID_TMP_DIR, `docs-search-data-${Date.now()}.json`);
                  fs.writeFileSync(dataFile3, JSON.stringify({ query, results: docResults }), { mode: 384 });
                  const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_docs_search
with open(${JSON.stringify(dataFile3)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile3)})
notify_docs_search(data['query'], data['results'])
`);
                  if (!launchedNotify) {
                    try {
                      fs.unlinkSync(dataFile3);
                    } catch {
                    }
                  }
                }
              }
            } catch (notifyErr) {
              console.warn(`[quaid] Docs search notification skipped: ${notifyErr.message}`);
            }
            return {
              content: [{ type: "text", text }],
              details: { query, limit }
            };
          } catch (err) {
            console.error("[quaid] projects_search error:", err);
            if (isFailHardEnabled()) {
              throw err;
            }
            return {
              content: [{ type: "text", text: `Error searching docs: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "docs_read",
        description: "Read the full content of a registered document by file path or title.",
        parameters: Type.Object({
          identifier: Type.String({ description: "File path (workspace-relative) or document title" })
        }),
        async execute(_toolCallId, params) {
          try {
            const { identifier } = params || {};
            const output = await facade.docsRead(identifier);
            return {
              content: [{ type: "text", text: output || "Document not found." }],
              details: { identifier }
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "docs_list",
        description: "List registered documents, optionally filtered by project or type.",
        parameters: Type.Object({
          project: Type.Optional(Type.String({ description: "Filter by project name" })),
          type: Type.Optional(Type.String({ description: "Filter by asset type (doc, note, reference)" }))
        }),
        async execute(_toolCallId, params) {
          try {
            const args = ["--json"];
            if (params?.project) {
              args.push("--project", params.project);
            }
            if (params?.type) {
              args.push("--type", params.type);
            }
            const output = await facade.docsList(args);
            return {
              content: [{ type: "text", text: output || "No documents found." }],
              details: { project: params?.project }
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "docs_register",
        description: "Register a document for indexing and tracking. Use for external files or docs with source file tracking.",
        parameters: Type.Object({
          file_path: Type.String({ description: "File path (workspace-relative)" }),
          project: Type.Optional(Type.String({ description: "Project name (default: 'default')" })),
          title: Type.Optional(Type.String({ description: "Document title" })),
          description: Type.Optional(Type.String({ description: "Document description" })),
          auto_update: Type.Optional(Type.Boolean({ description: "Auto-update when source files change" })),
          source_files: Type.Optional(Type.Array(Type.String(), { description: "Source files this doc tracks" }))
        }),
        async execute(_toolCallId, params) {
          try {
            const args = [params.file_path];
            if (params.project) {
              args.push("--project", params.project);
            }
            if (params.title) {
              args.push("--title", params.title);
            }
            if (params.description) {
              args.push("--description", params.description);
            }
            if (params.auto_update) {
              args.push("--auto-update");
            }
            if (params.source_files) {
              args.push("--source-files", ...params.source_files);
            }
            args.push("--json");
            const output = await facade.docsRegister(args);
            return {
              content: [{ type: "text", text: output || "Registered." }],
              details: { file_path: params.file_path }
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "project_create",
        description: "Create a new project with a PROJECT.md template. Sets up the directory structure and scaffolding.",
        parameters: Type.Object({
          name: Type.String({ description: "Project name (kebab-case, e.g., 'my-essay')" }),
          label: Type.Optional(Type.String({ description: "Display label (e.g., 'My Essay')" })),
          description: Type.Optional(Type.String({ description: "Project description" })),
          source_roots: Type.Optional(Type.Array(Type.String(), { description: "Source root directories" }))
        }),
        async execute(_toolCallId, params) {
          try {
            const args = [params.name];
            if (params.label) {
              args.push("--label", params.label);
            }
            if (params.description) {
              args.push("--description", params.description);
            }
            if (params.source_roots) {
              args.push("--source-roots", ...params.source_roots);
            }
            const output = await facade.docsCreateProject(args);
            if (facade.shouldNotifyProjectCreate()) {
              try {
                const notifyPayload = JSON.stringify({
                  name: params.name,
                  label: params.label || ""
                });
                spawnNotifyScript(`
import json
from core.runtime.notify import notify_user
data = json.loads(${JSON.stringify(notifyPayload)})
name = str(data.get("name", "")).strip()
label = str(data.get("label", "")).strip()
project_label = f"{name} ({label})" if label else name
notify_user(f"\u{1F4C1} Project registered: {project_label}")
`);
              } catch (notifyErr) {
                console.warn(`[quaid] Project-create notification skipped: ${notifyErr.message}`);
              }
            }
            return {
              content: [{ type: "text", text: output || `Project '${params.name}' created.` }],
              details: { name: params.name }
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "project_list",
        description: `List all defined projects with their doc counts and metadata. Available projects: ${getProjectNames().join(", ") || "none"}`,
        parameters: Type.Object({}),
        async execute() {
          try {
            const output = await facade.docsListProjects(["--json"]);
            return {
              content: [{ type: "text", text: output || "No projects defined." }],
              details: {}
            };
          } catch (err) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    registerToolChecked(
      () => ({
        name: "session_recall",
        description: `List or load recent conversation sessions. Use when the user wants to continue previous work, references a past conversation, or you need context about what was discussed recently.`,
        parameters: Type.Object({
          action: Type.String({ description: '"list" = recent sessions, "load" = specific session transcript' }),
          session_id: Type.Optional(Type.String({ description: "Session ID to load (for action=load)" })),
          limit: Type.Optional(Type.Number({ description: "How many recent sessions to list (default 5, for action=list)" }))
        }),
        async execute(_toolCallId, params) {
          try {
            const { action = "list", session_id: sid, limit: listLimit = 5 } = params || {};
            const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
            let extractionLog = {};
            try {
              const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("extraction log must be a JSON object");
              }
              extractionLog = parsed;
            } catch (err) {
              if (isFailHardEnabled() && !isMissingFileError(err)) {
                throw new Error("[quaid] session_recall extraction log read failed under failHard", { cause: err });
              }
              console.warn(`[quaid] session_recall extraction log read failed: ${String(err?.message || err)}`);
            }
            if (action === "list") {
              const sessions = Object.entries(extractionLog).filter(([, v]) => v && v.last_extracted_at).sort(([, a], [, b]) => (b.last_extracted_at || "").localeCompare(a.last_extracted_at || "")).slice(0, Math.min(listLimit, 20));
              if (sessions.length === 0) {
                return {
                  content: [{ type: "text", text: "No recent sessions found in extraction log." }],
                  details: { count: 0 }
                };
              }
              let text = "Recent sessions:\n";
              sessions.forEach(([id, info], i) => {
                const date = info.last_extracted_at ? new Date(info.last_extracted_at).toLocaleString() : "unknown";
                const msgCount = info.message_count || "?";
                const trigger = info.label || "unknown";
                const topic = info.topic_hint ? ` \u2014 "${info.topic_hint}"` : "";
                text += `${i + 1}. [${date}] ${id} \u2014 ${msgCount} messages, extracted via ${trigger}${topic}
`;
              });
              return {
                content: [{ type: "text", text }],
                details: { count: sessions.length }
              };
            }
            if (action === "load" && sid) {
              if (!/^[a-zA-Z0-9_-]{1,128}$/.test(sid)) {
                return {
                  content: [{ type: "text", text: "Invalid session ID format." }],
                  details: { error: "invalid_session_id" }
                };
              }
              const sessionsDir = path.join(os.homedir(), ".openclaw", "sessions");
              const sessionPath = path.join(sessionsDir, `${sid}.jsonl`);
              if (fs.existsSync(sessionPath)) {
                try {
                  const messages = readMessagesFromSessionFile(sessionPath);
                  const transcript = facade.buildTranscript(messages);
                  const truncated = transcript.length > 1e4 ? "...[truncated]...\n\n" + transcript.slice(-1e4) : transcript;
                  return {
                    content: [{ type: "text", text: `Session ${sid} (${messages.length} messages):

${truncated}` }],
                    details: { session_id: sid, message_count: messages.length, truncated: transcript.length > 1e4 }
                  };
                } catch {
                }
              }
              try {
                const factsOutput = await facade.searchBySession(sid, 20);
                return {
                  content: [{ type: "text", text: `Session file not available. Facts extracted from session ${sid}:
${factsOutput || "No facts found."}` }],
                  details: { session_id: sid, fallback: true }
                };
              } catch {
                return {
                  content: [{ type: "text", text: `Session ${sid} not found. Session file may have been cleaned up and no facts were found.` }],
                  details: { session_id: sid, error: "not_found" }
                };
              }
            }
            return {
              content: [{ type: "text", text: 'Provide action: "list" or "load" (with session_id).' }],
              details: { error: "invalid_action" }
            };
          } catch (err) {
            console.error("[quaid] session_recall error:", err);
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) }
            };
          }
        }
      })
    );
    let extractionPromise = null;
    const queueExtractionTask = (task, source) => {
      const prior = extractionPromise || Promise.resolve();
      extractionPromise = prior.then(
        () => task(),
        async (err) => {
          const msg = err?.message || String(err);
          console.error(`[quaid] extraction chain prior failure (${source}): ${msg}`);
          if (isFailHardEnabled()) {
            throw err;
          }
          await task();
          return;
        }
      );
      return extractionPromise;
    };
    const timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      timeoutMinutes: getCaptureTimeoutMinutes(),
      isBootstrapOnly: (messages) => facade.isResetBootstrapOnlyConversation(messages),
      readSessionMessages: (sessionId) => readMessagesForTimeoutSession(sessionId),
      listSessionActivity: () => listSessionActivityForTimeout(),
      logger: (msg) => {
        const lowered = String(msg || "").toLowerCase();
        if (lowered.includes("fail") || lowered.includes("error")) {
          console.warn(msg);
          return;
        }
        console.log(msg);
      },
      extract: async (msgs, sid, label) => {
        extractionPromise = queueExtractionTask(
          () => extractMemoriesFromMessages(msgs, label || "Timeout", sid),
          "timeout"
        );
        await extractionPromise;
      }
    });
    const signalWorkerHeartbeatSecRaw = Number(process.env.QUAID_SIGNAL_WORKER_HEARTBEAT_SECONDS || "30");
    const signalWorkerHeartbeatSec = Number.isFinite(signalWorkerHeartbeatSecRaw) && signalWorkerHeartbeatSecRaw > 0 ? Math.floor(signalWorkerHeartbeatSecRaw) : 30;
    const signalWorkerStarted = timeoutManager.startWorker(signalWorkerHeartbeatSec);
    console.log(
      `[quaid][timeout] signal worker ${signalWorkerStarted ? "started" : "leader_exists"} heartbeat_seconds=${signalWorkerHeartbeatSec}`
    );
    async function recallMemories(opts) {
      const {
        query,
        limit = 10,
        expandGraph = false,
        graphDepth = 1,
        datastores,
        routeStores = false,
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
        waitForExtraction = false,
        sourceTag = "unknown"
      } = opts;
      const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${selectedStores.join(",")} routed=${routeStores} reasoning=${reasoning} intent=${intent} domain=${JSON.stringify(domain)} domainBoost=${JSON.stringify(domainBoost || {})} project=${project || "any"} waitForExtraction=${waitForExtraction}`
      );
      if (waitForExtraction && extractionPromise) {
        let raceTimer;
        try {
          await Promise.race([
            extractionPromise,
            new Promise((_, rej) => {
              raceTimer = setTimeout(() => rej(new Error("timeout")), 6e4);
            })
          ]);
        } catch (err) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.warn(
            `[quaid][recall] waitForExtraction degraded: ${String(err?.message || err)}`
          );
        } finally {
          if (raceTimer) clearTimeout(raceTimer);
        }
      }
      const recallOpts = {
        query,
        limit,
        expandGraph,
        graphDepth,
        datastores: selectedStores,
        routeStores,
        reasoning,
        intent,
        ranking,
        domain,
        domainBoost,
        project,
        dateFrom,
        dateTo,
        docs,
        datastoreOptions,
        failOpen: opts.failOpen
      };
      return sourceTag === "tool" ? facade.recallWithToolRetry(recallOpts) : facade.recall(recallOpts);
    }
    function readMessagesFromSessionFile(sessionFile) {
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
          console.warn(`[quaid] session file line parse failed: ${String(err?.message || err)}`);
        }
      }
      return messages;
    }
    function resolveOpenClawSessionStorePath() {
      return path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    }
    let openClawSessionStoreCache = null;
    function loadOpenClawSessionStore() {
      const storePath = resolveOpenClawSessionStorePath();
      try {
        if (!fs.existsSync(storePath)) return {};
        const stat = fs.statSync(storePath);
        const mtimeMs = Number.isFinite(stat.mtimeMs) ? stat.mtimeMs : 0;
        if (openClawSessionStoreCache && openClawSessionStoreCache.mtimeMs === mtimeMs) {
          return openClawSessionStoreCache.data;
        }
        const raw = JSON.parse(fs.readFileSync(storePath, "utf8"));
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
        const data = raw;
        openClawSessionStoreCache = { mtimeMs, data };
        return data;
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][timeout] session store read failed: ${String(err?.message || err)}`);
        return {};
      }
    }
    function parseSessionUpdatedAtMs(entry) {
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
    function resolveSessionTranscriptPath(entry, sessionId) {
      const pathCandidates = [entry?.sessionFile, entry?.session_file];
      for (const raw of pathCandidates) {
        const p = String(raw || "").trim();
        if (!p) continue;
        if (fs.existsSync(p)) return p;
      }
      const fallbackDirs = [
        path.join(os.homedir(), ".openclaw", "agents", "main", "sessions"),
        path.join(os.homedir(), ".openclaw", "sessions")
      ];
      for (const dir of fallbackDirs) {
        const candidate = path.join(dir, `${sessionId}.jsonl`);
        if (fs.existsSync(candidate)) return candidate;
      }
      return null;
    }
    function readMessagesForTimeoutSession(sessionId) {
      const sid = String(sessionId || "").trim();
      if (!sid) return [];
      const store = loadOpenClawSessionStore();
      const entries = Object.entries(store || {});
      for (const [, entry] of entries) {
        if (String(entry?.sessionId || "").trim() !== sid) continue;
        const transcriptPath = resolveSessionTranscriptPath(entry, sid);
        if (!transcriptPath) return [];
        return readMessagesFromSessionFile(transcriptPath);
      }
      const fallbackPath = resolveSessionTranscriptPath({}, sid);
      if (!fallbackPath) return [];
      return readMessagesFromSessionFile(fallbackPath);
    }
    function listSessionActivityForTimeout() {
      const store = loadOpenClawSessionStore();
      const rows = [];
      const entries = Object.entries(store || {});
      for (const [, entry] of entries) {
        const sid = String(entry?.sessionId || "").trim();
        if (!sid) continue;
        const updatedAtMs = parseSessionUpdatedAtMs(entry);
        if (updatedAtMs !== null) {
          rows.push({ sessionId: sid, lastActivityMs: updatedAtMs });
          continue;
        }
        const transcriptPath = resolveSessionTranscriptPath(entry, sid);
        if (!transcriptPath) continue;
        try {
          const stat = fs.statSync(transcriptPath);
          if (Number.isFinite(stat.mtimeMs) && stat.mtimeMs > 0) {
            rows.push({ sessionId: sid, lastActivityMs: stat.mtimeMs });
          }
        } catch (err) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.warn(`[quaid][timeout] session mtime read failed for ${sid}: ${String(err?.message || err)}`);
        }
      }
      return rows;
    }
    const extractMemoriesFromMessages = async (messages, label, sessionId) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        return;
      }
      const sessionNotes = sessionId ? facade.getAndClearMemoryNotes(sessionId) : [];
      const allNotes = Array.from(/* @__PURE__ */ new Set([...sessionNotes]));
      if (allNotes.length > 0) {
        console.log(`[quaid] ${label}: prepend ${allNotes.length} queued memory note(s)`);
      }
      const fullTranscript = facade.buildTranscript(messages);
      if (!fullTranscript.trim() && allNotes.length === 0) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        return;
      }
      const hasMeaningfulUserContent = messages.some((m) => {
        if (m?.role !== "user") return false;
        const text = facade.getMessageText(m).trim();
        if (!text) return false;
        if (text.startsWith("GatewayRestart:")) return false;
        if (text.startsWith("System:")) return false;
        return true;
      });
      const transcriptForExtraction = allNotes.length > 0 ? `=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===
${allNotes.map((n) => `- ${n}`).join("\n")}
=== END EXPLICIT MEMORY REQUESTS ===

` + fullTranscript : fullTranscript;
      console.log(`[quaid] ${label} transcript: ${messages.length} messages, ${transcriptForExtraction.length} chars`);
      if (getMemoryConfig().notifications?.showProcessingStart !== false && facade.shouldNotifyFeature("extraction", "summary")) {
        const triggerType2 = facade.resolveExtractionTrigger(label);
        const suppressBacklogNotify2 = facade.isBacklogLifecycleReplay(
          messages,
          triggerType2,
          Date.now(),
          ADAPTER_BOOT_TIME_MS,
          BACKLOG_NOTIFY_STALE_MS
        );
        const dedupeSession2 = sessionId || extractSessionId(messages, {});
        const dedupeKey = `start:${dedupeSession2}:${triggerType2}`;
        const triggerDesc = triggerType2 === "compaction" ? "compaction" : triggerType2 === "recovery" ? "recovery" : triggerType2 === "timeout" ? "timeout" : triggerType2 === "new" ? "/new" : "reset";
        if (triggerType2 !== "recovery" && !suppressBacklogNotify2 && hasMeaningfulUserContent && facade.shouldEmitExtractionNotify(dedupeKey)) {
          spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("\u{1F9E0} Processing memories from ${triggerDesc}...")
`);
        }
      }
      const journalConfig = getMemoryConfig().docs?.journal || {};
      const journalEnabled = isSystemEnabled("journal") && journalConfig.enabled !== false;
      const snippetsEnabled = journalEnabled && journalConfig.snippetsEnabled !== false;
      const triggerLabel = facade.resolveExtractionTrigger(label);
      let extracted;
      try {
        extracted = await callExtractPipeline({
          transcript: transcriptForExtraction,
          owner: facade.resolveOwner(),
          label: triggerLabel,
          sessionId,
          writeSnippets: snippetsEnabled,
          writeJournal: journalEnabled
        });
      } catch (err) {
        const msg = String(err?.message || err);
        console.error(`[quaid] ${label} extraction failed: ${msg}`);
        return;
      }
      const factDetails = Array.isArray(extracted?.facts) ? extracted.facts : [];
      const stored = Number(extracted?.facts_stored || 0);
      const skipped = Number(extracted?.facts_skipped || 0);
      const edgesCreated = Number(extracted?.edges_created || 0);
      const firstFactStatus = factDetails.length > 0 ? String(factDetails[0]?.status || "unknown") : "none";
      console.log(
        `[quaid][extract] payload label=${label} session=${sessionId || "unknown"} facts_len=${factDetails.length} first_status=${firstFactStatus} stored=${stored} skipped=${skipped} edges=${edgesCreated}`
      );
      console.log(`[quaid] ${label} extraction complete: ${stored} stored, ${skipped} skipped, ${edgesCreated} edges`);
      console.log(`[quaid][extract] done label=${label} session=${sessionId || "unknown"} stored=${stored} skipped=${skipped} edges=${edgesCreated}`);
      let snippetDetails = {};
      let journalDetails = {};
      try {
        const journalRaw = extracted?.journal;
        const snippetsRaw = extracted?.snippets;
        const targetFiles = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];
        if (snippetsRaw && typeof snippetsRaw === "object" && !Array.isArray(snippetsRaw)) {
          for (const [filename, snippets] of Object.entries(snippetsRaw)) {
            if (!targetFiles.includes(filename) || !Array.isArray(snippets)) continue;
            const valid = snippets.filter((s) => typeof s === "string" && s.trim().length > 0);
            if (valid.length > 0) {
              snippetDetails[filename] = valid.map((s) => s.trim());
            }
          }
        }
        if (journalRaw && typeof journalRaw === "object" && !Array.isArray(journalRaw)) {
          for (const [filename, entry] of Object.entries(journalRaw)) {
            if (!targetFiles.includes(filename)) continue;
            const text = typeof entry === "string" ? entry : "";
            if (text.trim().length > 0) {
              journalDetails[filename] = [text.trim()];
            }
          }
        }
      } catch (err) {
        if (isFailHardEnabled()) {
          throw new Error("[quaid] extraction snippet/journal parsing failed under failHard", { cause: err });
        }
        console.warn(`[quaid] extraction snippet/journal parsing failed: ${String(err?.message || err)}`);
      }
      const hasSnippets = Object.keys(snippetDetails).length > 0;
      const hasJournalEntries = Object.keys(journalDetails).length > 0;
      const triggerType = facade.resolveExtractionTrigger(label);
      const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
        messages,
        triggerType,
        Date.now(),
        ADAPTER_BOOT_TIME_MS,
        BACKLOG_NOTIFY_STALE_MS
      );
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new") && hasMeaningfulUserContent && facade.shouldNotifyFeature("extraction", "summary");
      const dedupeSession = sessionId || extractSessionId(messages, {});
      const completionDedupeKey = `done:${dedupeSession}:${triggerType}:${stored}:${skipped}:${edgesCreated}`;
      if (!suppressBacklogNotify && facade.shouldNotifyFeature("extraction", "summary") && triggerType === "compaction") {
        queueCompactionNotificationBatch(dedupeSession, stored, skipped, edgesCreated);
      } else if (triggerType !== "recovery" && !suppressBacklogNotify && (factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion) && facade.shouldNotifyFeature("extraction", "summary") && facade.shouldEmitExtractionNotify(completionDedupeKey)) {
        try {
          const trigger = triggerType === "unknown" ? "reset" : triggerType;
          const mergedDetails = {};
          for (const [f, items] of Object.entries(snippetDetails)) {
            mergedDetails[f] = items.map((s) => `[snippet] ${s}`);
          }
          for (const [f, items] of Object.entries(journalDetails)) {
            mergedDetails[f] = [...mergedDetails[f] || [], ...items.map((s) => `[journal] ${s}`)];
          }
          const hasMerged = Object.keys(mergedDetails).length > 0;
          const detailsPath = path.join(QUAID_TMP_DIR, `extraction-details-${Date.now()}.json`);
          fs.writeFileSync(detailsPath, JSON.stringify({
            stored,
            skipped,
            edges_created: edgesCreated,
            trigger,
            details: factDetails,
            snippet_details: hasMerged ? mergedDetails : null,
            always_notify: alwaysNotifyCompletion
          }), { mode: 384 });
          const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_extraction
with open(${JSON.stringify(detailsPath)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(detailsPath)})
notify_memory_extraction(
    data['stored'],
    data['skipped'],
    data['edges_created'],
    data['trigger'],
    data['details'],
    snippet_details=data.get('snippet_details'),
    always_notify=data.get('always_notify', False),
)
`);
          if (!launchedNotify) {
            try {
              fs.unlinkSync(detailsPath);
            } catch {
            }
          }
        } catch (notifyErr) {
          console.warn(`[quaid] Extraction notification skipped: ${notifyErr.message}`);
        }
      }
      if (triggerType === "timeout") {
        maybeForceCompactionAfterTimeout(sessionId);
      }
      try {
        const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
        let extractionLog = {};
        try {
          const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("extraction log must be a JSON object");
          }
          extractionLog = parsed;
        } catch (err) {
          if (isFailHardEnabled() && !isMissingFileError(err)) {
            throw new Error("[quaid] extraction log read failed under failHard", { cause: err });
          }
          console.warn(`[quaid] Extraction log read failed for ${extractionLogPath}: ${String(err?.message || err)}`);
        }
        let topicHint = "";
        for (const m of messages) {
          if (m?.role === "user") {
            const cleaned = facade.getMessageText(m).trim();
            if (cleaned && !cleaned.startsWith("GatewayRestart:") && !cleaned.startsWith("System:")) {
              topicHint = cleaned.slice(0, 120);
              break;
            }
          }
        }
        extractionLog[sessionId || "unknown"] = {
          last_extracted_at: (/* @__PURE__ */ new Date()).toISOString(),
          message_count: messages.length,
          label,
          topic_hint: topicHint
        };
        const trimmed = trimExtractionLogEntries(extractionLog, MAX_EXTRACTION_LOG_ENTRIES);
        fs.writeFileSync(extractionLogPath, JSON.stringify(trimmed, null, 2), { mode: 384 });
      } catch (logErr) {
        const msg = `[quaid] extraction log update failed: ${logErr.message}`;
        if (isFailHardEnabled()) {
          throw new Error(msg);
        }
        console.warn(msg);
      }
    };
    registerInternalHookChecked("before_compaction", async (event, ctx) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages = event.messages || [];
        const sessionId = ctx?.sessionId;
        const conversationMessages = getAllConversationMessages(messages);
        const extractionSessionId = sessionId || extractSessionId(messages, ctx);
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_compaction: empty/internal hook payload; deferring to timeout source session=${extractionSessionId || "unknown"}`);
        } else {
          console.log(`[quaid] before_compaction hook triggered, ${messages.length} messages, session=${sessionId || "unknown"}`);
        }
        const doExtraction = async () => {
          if (isSystemEnabled("memory")) {
            if (conversationMessages.length > 0) {
              if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
                label: "CompactionSignal",
                source: "hook",
                signature: "hook:before_compaction"
              })) {
                facade.markLifecycleSignalFromHook(extractionSessionId, "CompactionSignal");
                timeoutManager.queueExtractionSignal(extractionSessionId, "CompactionSignal", {
                  source: "before_compaction",
                  hook_session_id: String(sessionId || ""),
                  extraction_session_id: String(extractionSessionId || ""),
                  event_message_count: messages.length,
                  conversation_message_count: conversationMessages.length,
                  has_system_compacted_notice: conversationMessages.some(
                    (m) => String(facade.getMessageText(m) || "").toLowerCase().includes("compacted (")
                  )
                });
                console.log(`[quaid][signal] queued CompactionSignal session=${extractionSessionId}`);
              } else {
                console.log(`[quaid][signal] suppressed duplicate CompactionSignal session=${extractionSessionId}`);
              }
            } else {
              const extracted = await timeoutManager.extractSessionFromLog(
                extractionSessionId,
                "CompactionSignal",
                messages
              );
              console.log(
                `[quaid][signal] empty-hook-payload fallback session=${extractionSessionId} extracted=${extracted ? "yes" : "no"}`
              );
            }
          } else {
            console.log("[quaid] Compaction: memory extraction skipped \u2014 memory system disabled");
          }
          if (conversationMessages.length === 0) {
            return;
          }
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);
          try {
            await facade.updateDocsFromTranscript(conversationMessages, "Compaction", uniqueSessionId, QUAID_TMP_DIR);
          } catch (err) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Compaction doc update failed:", err.message);
          }
          try {
            await emitProjectEvent(conversationMessages, "compact", uniqueSessionId);
          } catch (err) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Compaction project event failed:", err.message);
          }
          if (isSystemEnabled("memory") && uniqueSessionId) {
            const logPath = getInjectionLogPath(uniqueSessionId);
            let logData = {};
            try {
              logData = JSON.parse(fs.readFileSync(logPath, "utf8"));
            } catch (err) {
              console.warn(`[quaid] compaction injection log read failed for ${logPath}: ${String(err?.message || err)}`);
            }
            logData.lastCompactionAt = (/* @__PURE__ */ new Date()).toISOString();
            logData.memoryTexts = [];
            fs.writeFileSync(logPath, JSON.stringify(logData, null, 2), { mode: 384 });
            console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
          }
        };
        extractionPromise = queueExtractionTask(doExtraction, "compaction").catch((doErr) => {
          console.error(`[quaid][compaction] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
          if (isFailHardEnabled()) {
            throw doErr;
          }
        });
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_compaction hook failed:", err);
      }
    }, {
      name: "compaction-memory-extraction",
      priority: 10
    });
    for (const commandAction of ["reset", "new"]) {
      registerInternalHookChecked(`command:${commandAction}`, async (event, ctx) => {
        try {
          const messages = event?.messages || [];
          const sessionId = resolveLifecycleHookSessionId(event, ctx, messages);
          if (!sessionId || isInternalQuaidSession(sessionId)) {
            return;
          }
          if (!isSystemEnabled("memory")) {
            return;
          }
          const signature = `hook:command_${commandAction}`;
          if (!facade.shouldProcessLifecycleSignal(sessionId, {
            label: "ResetSignal",
            source: "hook",
            signature
          })) {
            console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=command:${commandAction}`);
            return;
          }
          facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
          timeoutManager.queueExtractionSignal(sessionId, "ResetSignal", {
            source: "command_hook",
            command: commandAction,
            hook_session_id: sessionId,
            hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
          });
          console.log(`[quaid][signal] queued ResetSignal session=${sessionId} source=command:${commandAction}`);
        } catch (err) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.error(`[quaid] command:${commandAction} hook failed:`, err);
        }
      }, {
        name: `command-${commandAction}-memory-extraction`,
        priority: 10
      });
    }
    registerInternalHookChecked("before_reset", async (event, ctx) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages = event.messages || [];
        const reason = event.reason || "unknown";
        const sessionId = ctx?.sessionId;
        const conversationMessages = getAllConversationMessages(messages);
        const extractionSessionId = resolveLifecycleHookSessionId(event, ctx, conversationMessages);
        if (!extractionSessionId) {
          console.log(`[quaid] before_reset: skip unresolved session id session=${sessionId || "unknown"}`);
          return;
        }
        if (conversationMessages.length === 0) {
          console.log(
            `[quaid] before_reset: empty/internal transcript; queueing ResetSignal from source session session=${extractionSessionId}`
          );
        }
        console.log(`[quaid] before_reset hook triggered (reason: ${reason}), ${messages.length} messages, session=${sessionId || "unknown"}`);
        const doExtraction = async () => {
          if (isSystemEnabled("memory")) {
            if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
              label: "ResetSignal",
              source: "hook",
              signature: "hook:before_reset"
            })) {
              facade.markLifecycleSignalFromHook(extractionSessionId, "ResetSignal");
              timeoutManager.queueExtractionSignal(extractionSessionId, "ResetSignal", {
                source: "before_reset",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || ""),
                reason: String(reason || "unknown"),
                event_message_count: messages.length,
                conversation_message_count: conversationMessages.length
              });
              console.log(`[quaid][signal] queued ResetSignal session=${extractionSessionId}`);
            } else {
              console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${extractionSessionId}`);
            }
          } else {
            console.log("[quaid] Reset: memory extraction skipped \u2014 memory system disabled");
          }
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);
          if (conversationMessages.length > 0) {
            try {
              await facade.updateDocsFromTranscript(conversationMessages, "Reset", uniqueSessionId, QUAID_TMP_DIR);
            } catch (err) {
              if (isFailHardEnabled()) {
                throw err;
              }
              console.error("[quaid] Reset doc update failed:", err.message);
            }
            try {
              await emitProjectEvent(conversationMessages, "reset", uniqueSessionId);
            } catch (err) {
              if (isFailHardEnabled()) {
                throw err;
              }
              console.error("[quaid] Reset project event failed:", err.message);
            }
          }
          console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"}`);
        };
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${extractionPromise ? "yes" : "no"}`);
        extractionPromise = queueExtractionTask(doExtraction, "reset").catch((doErr) => {
          console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
          if (isFailHardEnabled()) {
            throw doErr;
          }
        });
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_reset hook failed:", err);
      }
    }, {
      name: "reset-memory-extraction",
      priority: 10
    });
    registerInternalHookChecked("session_end", async (event, ctx) => {
      try {
        const sessionId = String(event?.sessionId || ctx?.sessionId || "").trim();
        const sessionKey = String(event?.sessionKey || ctx?.sessionKey || "").trim();
        const messageCount = Number(event?.messageCount || 0);
        if (!sessionId || isInternalQuaidSession(sessionId)) {
          return;
        }
        if (!isSystemEnabled("memory")) {
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "ResetSignal",
          source: "hook",
          signature: "hook:session_end"
        })) {
          console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=session_end`);
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        timeoutManager.queueExtractionSignal(sessionId, "ResetSignal", {
          source: "session_end",
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0
        });
        console.log(
          `[quaid][signal] queued ResetSignal session=${sessionId} source=session_end key=${sessionKey || "unknown"}`
        );
      } catch (err) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] session_end hook failed:", err);
      }
    }, {
      name: "session-end-memory-extraction",
      priority: 10
    });
    registerHttpRouteChecked({
      path: "/plugins/quaid/llm",
      auth: "gateway",
      handler: async (req, res) => {
        if (req.method !== "POST") {
          res.writeHead(405, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }
        const chunks = [];
        for await (const chunk of req) {
          chunks.push(chunk);
        }
        let body;
        try {
          body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
        } catch {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Invalid JSON body" }));
          return;
        }
        if (!body || typeof body !== "object" || Array.isArray(body)) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "JSON body must be an object" }));
          return;
        }
        const { system_prompt, user_message, model_tier, max_tokens = 4e3 } = body;
        if (typeof system_prompt !== "string" || !system_prompt.trim() || typeof user_message !== "string" || !user_message.trim()) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "system_prompt and user_message required" }));
          return;
        }
        if (model_tier !== void 0 && model_tier !== "fast" && model_tier !== "deep") {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "model_tier must be 'fast' or 'deep'" }));
          return;
        }
        if (typeof max_tokens !== "number" || !Number.isFinite(max_tokens)) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "max_tokens must be a finite number" }));
          return;
        }
        const requestedTokens = Math.trunc(max_tokens);
        if (requestedTokens < 1 || requestedTokens > 1e5) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "max_tokens must be between 1 and 100000" }));
          return;
        }
        try {
          const tier = model_tier === "fast" ? "fast" : "deep";
          const data = await callConfiguredLLM(system_prompt, user_message, tier, requestedTokens, 6e5);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(data));
        } catch (err) {
          console.error(`[quaid] LLM proxy error: ${String(err)}`);
          const msg = String(err?.message || err);
          const status = msg.includes("No ") || msg.includes("Unsupported provider") || msg.includes("ReasoningModelClasses") ? 503 : 502;
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: `LLM proxy error: ${String(err)}` }));
        }
      }
    });
    registerHttpRouteChecked({
      path: "/memory/injected",
      auth: "gateway",
      handler: async (req, res) => {
        try {
          const url = new URL(req.url, "http://localhost");
          const sessionId = url.searchParams.get("sessionId");
          if (!sessionId || !/^[a-f0-9-]{1,64}$/i.test(sessionId)) {
            res.writeHead(400, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "Valid sessionId parameter required" }));
            return;
          }
          const enhancedLogPath = path.join(QUAID_LOGS_DIR, "memory-injection", `session-${sessionId}.log`);
          const tempLogPath = getInjectionLogPath(sessionId);
          let logData = null;
          if (fs.existsSync(enhancedLogPath)) {
            try {
              const content = fs.readFileSync(enhancedLogPath, "utf8");
              logData = JSON.parse(content);
            } catch (err) {
              console.error(`[quaid] Failed to read enhanced log: ${String(err)}`);
            }
          }
          if (!logData && fs.existsSync(tempLogPath)) {
            try {
              const content = fs.readFileSync(tempLogPath, "utf8");
              logData = JSON.parse(content);
            } catch (err) {
              console.error(`[quaid] Failed to read temp log: ${String(err)}`);
            }
          }
          if (!logData) {
            res.writeHead(404, { "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "Session log not found" }));
            return;
          }
          const responseData = {
            sessionId: logData.uniqueSessionId,
            sessionKey: logData.sessionKey,
            timestamp: logData.timestamp,
            memoriesInjected: logData.memoriesInjected,
            totalMemoriesInSession: logData.totalMemoriesInSession,
            injectedMemoriesDetail: logData.injectedMemoriesDetail || [],
            newlyInjected: logData.newlyInjected || []
          };
          const headers = {
            "Content-Type": "application/json"
          };
          const allowedOrigin = String(process.env.QUAID_DASHBOARD_ALLOWED_ORIGIN || "").trim();
          if (allowedOrigin) {
            headers["Access-Control-Allow-Origin"] = allowedOrigin;
            headers["Access-Control-Allow-Methods"] = "GET";
            headers["Access-Control-Allow-Headers"] = "Content-Type";
          }
          res.writeHead(200, headers);
          res.end(JSON.stringify(responseData, null, 2));
        } catch (err) {
          console.error(`[quaid] HTTP endpoint error: ${String(err)}`);
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Internal server error" }));
        }
      }
    });
    if (contractDecl.enabled) {
      validateApiRegistrations(contractDecl.api, registeredApi, strictContracts, (m) => console.warn(m));
    }
    console.log("[quaid] Plugin loaded with compaction/reset hooks and HTTP endpoint");
  }
};
var adapter_default = quaidPlugin;
const __test = {
  detectLifecycleCommandSignal,
  detectLifecycleSignal: (messages) => facade.detectLifecycleSignal(messages),
  shouldProcessLifecycleSignal: (sessionId, signal) => facade.shouldProcessLifecycleSignal(sessionId, signal),
  shouldEmitExtractionNotify: (key, now) => facade.shouldEmitExtractionNotify(key, now),
  latestMessageTimestampMs: (messages) => facade.latestMessageTimestampMs(messages),
  hasExplicitLifecycleUserCommand: (messages) => facade.hasExplicitLifecycleUserCommand(messages),
  isBacklogLifecycleReplay: (messages, trigger, nowMs) => facade.isBacklogLifecycleReplay(
    messages,
    trigger,
    nowMs ?? Date.now(),
    ADAPTER_BOOT_TIME_MS,
    BACKLOG_NOTIFY_STALE_MS
  ),
  markLifecycleSignalFromHook: (sessionId, label) => facade.markLifecycleSignalFromHook(sessionId, label),
  clearLifecycleSignalHistory: () => facade.clearLifecycleSignalHistory(),
  clearExtractionNotifyHistory: () => facade.clearExtractionNotifyHistory()
};
export {
  __test,
  adapter_default as default
};
