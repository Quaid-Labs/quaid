import { Type } from "@sinclair/typebox";
import { execSync, spawn } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
import { queueDelayedRequest } from "./delayed-requests.js";
import { createKnowledgeEngine } from "../../core/knowledge-engine.js";
import { createProjectCatalogReader } from "../../core/project-catalog.js";
import { createDatastoreBridge } from "../../core/datastore-bridge.js";
import { createPythonBridgeExecutor } from "./python-bridge.js";
function _resolveWorkspace() {
  const envWorkspace = String(process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envWorkspace) {
    return envWorkspace;
  }
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (fs.existsSync(cfgPath)) {
      const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const list = Array.isArray(cfg?.agents?.list) ? cfg.agents.list : [];
      const mainAgent = list.find((a) => a?.id === "main" || a?.default === true);
      const ws = String(mainAgent?.workspace || cfg?.agents?.defaults?.workspace || "").trim();
      if (ws) {
        return ws;
      }
    }
  } catch {
  }
  return process.cwd();
}
const WORKSPACE = _resolveWorkspace();
const PYTHON_PLUGIN_ROOT = path.join(WORKSPACE, "plugins", "quaid");
const PYTHON_SCRIPT = path.join(WORKSPACE, "plugins/quaid/datastore/memorydb/memory_graph.py");
const EXTRACT_SCRIPT = path.join(WORKSPACE, "plugins/quaid/ingest/extract.py");
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
for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch {
  }
}
let _cachedNodeCount = null;
let _nodeCountTimestamp = 0;
const NODE_COUNT_CACHE_MS = 5 * 60 * 1e3;
let _cachedDatastoreStats = null;
let _datastoreStatsTimestamp = 0;
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
function getDatastoreStatsSync(maxAgeMs = NODE_COUNT_CACHE_MS) {
  const now = Date.now();
  if (_cachedDatastoreStats && now - _datastoreStatsTimestamp < maxAgeMs) {
    return _cachedDatastoreStats;
  }
  try {
    const output = execSync(`python3 "${PYTHON_SCRIPT}" stats`, {
      encoding: "utf-8",
      timeout: 5e3,
      env: buildPythonEnv()
    });
    const parsed = JSON.parse(output);
    if (!parsed || typeof parsed !== "object") {
      return null;
    }
    _cachedDatastoreStats = parsed;
    _datastoreStatsTimestamp = now;
    return parsed;
  } catch {
    return null;
  }
}
function getActiveNodeCount() {
  const now = Date.now();
  if (_cachedNodeCount !== null && now - _nodeCountTimestamp < NODE_COUNT_CACHE_MS) {
    return _cachedNodeCount;
  }
  const stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
  const active = Number(stats?.by_status?.active ?? 0);
  if (Number.isFinite(active) && active > 0) {
    _cachedNodeCount = active;
    _nodeCountTimestamp = now;
    return _cachedNodeCount;
  }
  return _cachedNodeCount ?? 100;
}
function computeDynamicK() {
  const nodeCount = getActiveNodeCount();
  if (nodeCount < 10) return 5;
  const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
  return Math.max(5, Math.min(k, 40));
}
let _memoryConfig = null;
function getMemoryConfig() {
  if (_memoryConfig) {
    return _memoryConfig;
  }
  try {
    _memoryConfig = JSON.parse(fs.readFileSync(path.join(WORKSPACE, "config/memory.json"), "utf8"));
  } catch {
    _memoryConfig = {};
  }
  return _memoryConfig;
}
function isSystemEnabled(system) {
  const config = getMemoryConfig();
  const systems = config.systems || {};
  return systems[system] !== false;
}
function isPreInjectionPassEnabled() {
  const retrieval = getMemoryConfig().retrieval || {};
  if (typeof retrieval.preInjectionPass === "boolean") return retrieval.preInjectionPass;
  if (typeof retrieval.pre_injection_pass === "boolean") return retrieval.pre_injection_pass;
  return true;
}
function getCaptureTimeoutMinutes() {
  const capture = getMemoryConfig().capture || {};
  const raw = capture.inactivityTimeoutMinutes ?? capture.inactivity_timeout_minutes ?? 120;
  const num = Number(raw);
  return Number.isFinite(num) ? Math.max(0, num) : 120;
}
function effectiveNotificationLevel(feature) {
  const notifications = getMemoryConfig().notifications || {};
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
  } catch {
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
  } catch {
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
  } catch (_err) {
    errors.push(`deep reasoning model resolution failed: ${String(err?.message || err)}`);
  }
  try {
    const fast = resolveTierModel("fast");
    console.log(`[quaid][startup] fast model resolved: provider=${fast.provider} model=${fast.model}`);
    const paidProviders = /* @__PURE__ */ new Set(["openai-compatible"]);
    if (paidProviders.has(fast.provider)) {
      console.warn(`[quaid][billing] paid provider active for fast reasoning: ${fast.provider}/${fast.model}`);
    }
  } catch (err2) {
    errors.push(`fast reasoning model resolution failed: ${String(err2?.message || err2)}`);
  }
  try {
    const cfg = getMemoryConfig();
    const maxResults = Number(cfg?.retrieval?.maxResults ?? 0);
    if (!Number.isFinite(maxResults) || maxResults <= 0) {
      errors.push(`invalid retrieval.maxResults=${String(cfg?.retrieval?.maxResults)}`);
    }
  } catch (err2) {
    errors.push(`config load failed: ${String(err2?.message || err2)}`);
  }
  const requiredFiles = [
    path.join(WORKSPACE, "plugins", "quaid", "core", "lifecycle", "janitor.py"),
    path.join(WORKSPACE, "plugins", "quaid", "datastore", "memorydb", "memory_graph.py")
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
function resolveExtractionTrigger(label) {
  const normalized = String(label || "").trim().toLowerCase();
  if (!normalized) {
    return "unknown";
  }
  if (normalized.includes("compact")) {
    return "compaction";
  }
  if (normalized.includes("recover")) {
    return "recovery";
  }
  if (normalized.includes("timeout")) {
    return "timeout";
  }
  if (normalized.includes("new")) {
    return "new";
  }
  if (normalized.includes("reset")) {
    return "reset";
  }
  return "unknown";
}
const configSchema = Type.Object({
  autoCapture: Type.Optional(Type.Boolean({ default: false })),
  autoRecall: Type.Optional(Type.Boolean({ default: true }))
});
let _usersConfig = null;
function getUsersConfig() {
  if (_usersConfig) {
    return _usersConfig;
  }
  try {
    const configPath = path.join(WORKSPACE, "config/memory.json");
    const raw = JSON.parse(fs.readFileSync(configPath, "utf8"));
    _usersConfig = raw.users || { defaultOwner: "quaid", identities: {} };
  } catch {
    _usersConfig = { defaultOwner: "quaid", identities: {} };
  }
  return _usersConfig;
}
function resolveOwner(speaker, channel) {
  const config = getUsersConfig();
  for (const [userId, identity] of Object.entries(config.identities)) {
    if (speaker && identity.speakers.some(
      (s) => s.toLowerCase() === speaker.toLowerCase()
    )) {
      return userId;
    }
    if (channel && identity.channels[channel]) {
      const allowed = identity.channels[channel];
      if (allowed.includes("*")) {
        return userId;
      }
      if (speaker && allowed.some((a) => a.toLowerCase() === speaker.toLowerCase())) {
        return userId;
      }
    }
  }
  return config.defaultOwner;
}
function isInternalQuaidSession(sessionId) {
  const sid = typeof sessionId === "string" ? sessionId.trim() : "";
  if (!sid) return false;
  return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
}
const datastoreBridge = createDatastoreBridge(
  createPythonBridgeExecutor({
    scriptPath: PYTHON_SCRIPT,
    dbPath: DB_PATH,
    workspace: WORKSPACE
  })
);
const _memoryNotes = /* @__PURE__ */ new Map();
const NOTES_DIR = QUAID_NOTES_DIR;
function getNotesPath(sessionId) {
  return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`);
}
function getInjectionLogPath(sessionId) {
  return path.join(QUAID_INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
}
function addMemoryNote(sessionId, text, category) {
  if (!_memoryNotes.has(sessionId)) {
    _memoryNotes.set(sessionId, []);
  }
  _memoryNotes.get(sessionId).push(`[${category}] ${text}`);
  try {
    const notesPath = getNotesPath(sessionId);
    let existing = [];
    try {
      existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
    } catch {
    }
    existing.push(`[${category}] ${text}`);
    fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 384 });
  } catch {
  }
}
function getAndClearMemoryNotes(sessionId) {
  const inMemory = _memoryNotes.get(sessionId) || [];
  let onDisk = [];
  const notesPath = getNotesPath(sessionId);
  try {
    onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
  } catch {
  }
  const all = Array.from(/* @__PURE__ */ new Set([...inMemory, ...onDisk]));
  _memoryNotes.delete(sessionId);
  try {
    fs.unlinkSync(notesPath);
  } catch {
  }
  return all;
}
function getAndClearAllMemoryNotes() {
  const all = [];
  for (const [sid, notes] of _memoryNotes.entries()) {
    all.push(...notes);
    _memoryNotes.delete(sid);
  }
  try {
    const files = fs.readdirSync(NOTES_DIR).filter((f) => f.startsWith("memory-notes-") && f.endsWith(".json"));
    for (const f of files) {
      const fp = path.join(NOTES_DIR, f);
      try {
        const notes = JSON.parse(fs.readFileSync(fp, "utf8"));
        all.push(...notes);
        fs.unlinkSync(fp);
      } catch {
      }
    }
  } catch {
  }
  return Array.from(new Set(all));
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
  const timestampHash = require("crypto").createHash("md5").update(firstTimestamp).digest("hex").substring(0, 12);
  return timestampHash;
}
function getAllConversationMessages(messages) {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg) => {
    if (!msg || msg.role !== "user" && msg.role !== "assistant") return false;
    const text = getMessageText(msg).trim();
    if (!text) return false;
    if (text.startsWith("Extract memorable facts and journal entries from this conversation:")) return false;
    if (isInternalMaintenancePrompt(text)) return false;
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
  if (!Array.isArray(messages) || messages.length === 0) return null;
  const last = messages[messages.length - 1];
  const prev = messages.length > 1 ? messages[messages.length - 2] : null;
  const candidate = last?.role === "user" ? last : prev?.role === "user" ? prev : null;
  if (!candidate) return null;
  const text = getMessageText(candidate).trim().toLowerCase();
  if (!text) return null;
  const normalized = text.replace(/\[\[[^\]]+\]\]\s*/g, "").trim();
  const m = normalized.match(/(?:^|\s)\/(new|reset|restart|compact)(?=\s|$)/);
  if (!m) return null;
  const command = `/${m[1]}`;
  if (command === "/new" || command === "/reset" || command === "/restart") return "ResetSignal";
  if (command === "/compact") return "CompactionSignal";
  return null;
}
function isInternalMaintenancePrompt(text) {
  const t = String(text || "").trim();
  if (!t) return false;
  const s = t.toLowerCase();
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
  return markers.some((m) => s.includes(m));
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
    const out = execSync(
      `openclaw gateway call sessions.compact --json --params '${JSON.stringify({ key })}'`,
      { encoding: "utf-8", timeout: 2e4 }
    );
    const parsed = JSON.parse(String(out || "{}"));
    if (parsed?.ok) {
      console.log(`[quaid][timeout] auto-compaction requested for key=${key} (compacted=${String(parsed?.compacted)})`);
    } else {
      console.warn(`[quaid][timeout] auto-compaction returned non-ok for key=${key}: ${String(out).slice(0, 300)}`);
    }
  } catch (_err) {
    console.warn(`[quaid][timeout] auto-compaction failed for key=${key}: ${String(err?.message || err)}`);
  }
}
const DOCS_UPDATER = path.join(WORKSPACE, "plugins/quaid/core/docs/updater.py");
const DOCS_RAG = path.join(WORKSPACE, "modules/quaid/datastore/docsdb/rag.py");
const DOCS_REGISTRY = path.join(WORKSPACE, "plugins/quaid/core/docs/registry.py");
const PROJECT_UPDATER = path.join(WORKSPACE, "plugins/quaid/core/docs/project_updater.py");
const EVENTS_SCRIPT = path.join(WORKSPACE, "plugins/quaid/core/runtime/events.py");
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
  } catch {
  }
  return void 0;
}
function _getAnthropicCredential() {
  return _getGatewayCredential(["anthropic"]);
}
function _getOpenAICredential() {
  const gatewayKey = _getGatewayCredential(["openai-codex", "openai"]);
  if (gatewayKey) return gatewayKey;
  return void 0;
}
function _getProviderCredential(provider) {
  const normalized = normalizeProvider(provider);
  if (normalized === "openai") {
    return _getOpenAICredential();
  }
  if (normalized === "anthropic") {
    return _getAnthropicCredential();
  }
  return _getGatewayCredential([provider, normalized]);
}
function _readOpenClawConfig() {
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (!fs.existsSync(cfgPath)) {
      return {};
    }
    return JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  } catch {
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
  } catch {
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
  fs.mkdirSync(path.dirname(storePath), { recursive: true });
  fs.writeFileSync(storePath, JSON.stringify(store, null, 2), { mode: 384 });
  return sessionKey;
}
async function callConfiguredLLM(systemPrompt, userMessage, modelTier, maxTokens, timeoutMs = 6e5) {
  const resolved = resolveTierModel(modelTier);
  const provider = normalizeProvider(resolved.provider);
  const started = Date.now();
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
  let gatewayRes;
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
  } catch (err2) {
    const durationMs2 = Date.now() - started;
    console.error(
      `[quaid][llm] gateway_fetch_error tier=${modelTier} duration_ms=${durationMs2} error=${err2?.name || "Error"}:${err2?.message || String(err2)}`
    );
    throw err2;
  }
  const rawBody = await gatewayRes.text();
  let data = null;
  try {
    data = rawBody ? JSON.parse(rawBody) : {};
  } catch (_err) {
    const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
    console.error(
      `[quaid][llm] gateway_parse_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} body_preview=${JSON.stringify(bodyPreview)}`
    );
    throw new Error(`Gateway response parse failed (${gatewayRes.status} ${gatewayRes.statusText})`);
  }
  if (!gatewayRes.ok) {
    const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
    console.error(
      `[quaid][llm] gateway_http_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} body_preview=${JSON.stringify(bodyPreview)}`
    );
    const err2 = data?.error?.message || data?.message || `Gateway OpenResponses error ${gatewayRes.status}`;
    throw new Error(err2);
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
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        proc.kill("SIGTERM");
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
      if (code === 0) {
        resolve(stdout.trim());
      } else {
        reject(new Error(`${label} error: ${stderr || stdout}`));
      }
    });
    proc.on("error", (err2) => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      reject(err2);
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
    const output = await new Promise((resolve, reject) => {
      const proc = spawn("python3", [EXTRACT_SCRIPT, ...args], {
        cwd: WORKSPACE,
        env: buildPythonEnv()
      });
      let stdout = "";
      let stderr = "";
      let settled = false;
      const timeoutMs = 3e5;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        proc.kill("SIGTERM");
        reject(new Error(`extract timeout after ${timeoutMs}ms`));
      }, timeoutMs);
      proc.stdout.on("data", (data) => {
        stdout += data;
      });
      proc.stderr.on("data", (data) => {
        stderr += data;
      });
      proc.on("close", (code) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (code === 0) {
          resolve(stdout.trim());
        } else {
          reject(new Error(`extract error: ${stderr || stdout}`));
        }
      });
      proc.on("error", (err2) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(err2);
      });
    });
    return JSON.parse(output || "{}");
  } finally {
    try {
      fs.unlinkSync(tmpPath);
    } catch {
    }
  }
}
async function callDocsUpdater(command, args = []) {
  const apiKey = _getAnthropicCredential();
  return _spawnWithTimeout(DOCS_UPDATER, command, args, "docs_updater", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE,
    ...apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}
  });
}
async function emitEvent(name, payload, dispatch = "auto") {
  const args = [
    "emit",
    "--name",
    name,
    "--payload",
    JSON.stringify(payload || {}),
    "--source",
    "openclaw_adapter",
    "--dispatch",
    dispatch
  ];
  const out = await _spawnWithTimeout(EVENTS_SCRIPT, "emit", args.slice(1), "events", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  }, 3e5);
  return JSON.parse(out || "{}");
}
async function callDocsRag(command, args = []) {
  return _spawnWithTimeout(DOCS_RAG, command, args, "docs_rag", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  });
}
async function callDocsRegistry(command, args = []) {
  return _spawnWithTimeout(DOCS_REGISTRY, command, args, "docs_registry", {
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE
  });
}
const projectCatalogReader = createProjectCatalogReader({
  workspace: WORKSPACE,
  fs,
  path
});
const getProjectNames = () => projectCatalogReader.getProjectNames();
const getProjectCatalog = () => projectCatalogReader.getProjectCatalog();
function spawnNotifyScript(scriptBody) {
  const tmpFile = path.join(QUAID_NOTIFY_DIR, `notify-${Date.now()}-${Math.random().toString(36).slice(2)}.py`);
  const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
  const preamble = `import sys, os
sys.path.insert(0, ${JSON.stringify(path.join(WORKSPACE, "plugins/quaid"))})
`;
  const cleanup = `
os.unlink(${JSON.stringify(tmpFile)})
`;
  fs.writeFileSync(tmpFile, preamble + scriptBody + cleanup, { mode: 384 });
  const notifyLogFd = fs.openSync(notifyLogFile, "a");
  const proc = spawn("python3", [tmpFile], {
    detached: true,
    stdio: ["ignore", notifyLogFd, notifyLogFd],
    env: buildPythonEnv()
  });
  fs.closeSync(notifyLogFd);
  proc.unref();
}
function _loadJanitorNudgeState() {
  try {
    if (fs.existsSync(JANITOR_NUDGE_STATE_PATH)) {
      return JSON.parse(fs.readFileSync(JANITOR_NUDGE_STATE_PATH, "utf8")) || {};
    }
  } catch {
  }
  return {};
}
function _saveJanitorNudgeState(state) {
  try {
    fs.writeFileSync(JANITOR_NUDGE_STATE_PATH, JSON.stringify(state, null, 2), { mode: 384 });
  } catch {
  }
}
function queueDelayedLlmRequest(message, kind = "janitor", priority = "normal") {
  return queueDelayedRequest(DELAYED_LLM_REQUESTS_PATH, message, kind, priority, "quaid_adapter");
}
function getJanitorHealthIssue() {
  try {
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
  } catch {
    return null;
  }
}
function maybeQueueJanitorHealthAlert() {
  const issue = getJanitorHealthIssue();
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
  } catch {
  }
  try {
    if (fs.existsSync(PENDING_APPROVAL_REQUESTS_PATH) && now - lastApprovalNudge > NUDGE_COOLDOWN_MS) {
      const raw = JSON.parse(fs.readFileSync(PENDING_APPROVAL_REQUESTS_PATH, "utf8"));
      const requests = Array.isArray(raw?.requests) ? raw.requests : [];
      const pendingCount = requests.filter((r) => r?.status === "pending").length;
      if (pendingCount > 0) {
        state.lastApprovalNudgeAt = now;
      }
    }
  } catch {
  }
  _saveJanitorNudgeState(state);
}
function extractFilePaths(messages) {
  const paths = /* @__PURE__ */ new Set();
  for (const msg of messages) {
    const text = typeof msg.content === "string" ? msg.content : msg.content?.map((c) => c.text || "").join(" ") || "";
    const matches = text.match(/(?:^|\s)((?:\/[\w.-]+)+|(?:[\w.-]+\/)+[\w.-]+)/gm);
    if (matches) {
      for (const m of matches) {
        const p = m.trim();
        if (p.includes("/") && !p.startsWith("http") && p.length < 200) {
          paths.add(p);
        }
      }
    }
  }
  return Array.from(paths);
}
async function getQuickProjectSummary(messages) {
  const transcript = buildTranscript(messages);
  if (!transcript || transcript.length < 20) {
    return { project_name: null, text: "" };
  }
  try {
    const llm = await callConfiguredLLM(
      `You summarize coding sessions. Given a conversation, identify: 1) What project was being worked on (use one of the available project names, or null if unclear), 2) Brief summary of what changed/was discussed. Available projects: ${getProjectNames().join(", ")}. Use these EXACT names. Respond with JSON only: {"project_name": "name-or-null", "text": "brief summary"}`,
      `Summarize this session:

${transcript.slice(0, 4e3)}`,
      "fast",
      300
    );
    const output = (llm.text || "").trim();
    const jsonMatch = output.match(/\{[\s\S]*\}/);
    if (jsonMatch) {
      try {
        const parsed = JSON.parse(jsonMatch[0]);
        return {
          project_name: typeof parsed.project_name === "string" ? parsed.project_name : null,
          text: typeof parsed.text === "string" ? parsed.text : ""
        };
      } catch {
      }
    }
  } catch (err2) {
    console.error("[quaid] Quick project summary failed:", err2.message);
  }
  return { project_name: null, text: transcript.slice(0, 500) };
}
async function emitProjectEvent(messages, trigger, sessionId) {
  if (!isSystemEnabled("projects")) {
    return;
  }
  const memConfig = getMemoryConfig();
  if (!memConfig.projects?.enabled) {
    return;
  }
  try {
    const summary = await getQuickProjectSummary(messages);
    const event = {
      project_hint: summary.project_name || null,
      files_touched: extractFilePaths(messages),
      summary: summary.text,
      trigger,
      session_id: sessionId,
      timestamp: (/* @__PURE__ */ new Date()).toISOString()
    };
    const stagingDir = path.join(WORKSPACE, memConfig.projects.stagingDir || "projects/staging/");
    if (!fs.existsSync(stagingDir)) {
      fs.mkdirSync(stagingDir, { recursive: true });
    }
    const eventPath = path.join(stagingDir, `${Date.now()}-${trigger}.json`);
    fs.writeFileSync(eventPath, JSON.stringify(event, null, 2));
    const bgApiKey = _getAnthropicCredential();
    const logFile = path.join(WORKSPACE, "logs/project-updater.log");
    const logDir = path.dirname(logFile);
    if (!fs.existsSync(logDir)) {
      fs.mkdirSync(logDir, { recursive: true });
    }
    const logFd = fs.openSync(logFile, "a");
    try {
      const proc = spawn("python3", [PROJECT_UPDATER, "process-event", eventPath], {
        detached: true,
        stdio: ["ignore", logFd, logFd],
        cwd: WORKSPACE,
        env: buildPythonEnv({ ...bgApiKey ? { ANTHROPIC_API_KEY: bgApiKey } : {} })
      });
      proc.unref();
    } finally {
      fs.closeSync(logFd);
    }
    console.log(`[quaid] Emitted project event: ${trigger} -> ${summary.project_name || "unknown"}`);
  } catch (err2) {
    console.error("[quaid] Failed to emit project event:", err2.message);
  }
}
function buildTranscript(messages) {
  const transcript = [];
  for (const msg of messages) {
    if (msg.role !== "user" && msg.role !== "assistant") {
      continue;
    }
    let text = typeof msg.content === "string" ? msg.content : msg.content?.map((c) => c.text || "").join(" ");
    if (!text) {
      continue;
    }
    text = text.replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "");
    text = text.replace(/\n?\[message_id:\s*\d+\]/gi, "").trim();
    if (text.startsWith("GatewayRestart:") || text.startsWith("System:")) {
      continue;
    }
    if (text.includes('"kind": "restart"')) {
      continue;
    }
    if (text.includes("HEARTBEAT") && text.includes("HEARTBEAT_OK")) {
      continue;
    }
    if (text.replace(/[*_<>\/b\s]/g, "").startsWith("HEARTBEAT_OK")) {
      continue;
    }
    if (!text) {
      continue;
    }
    transcript.push(`${msg.role === "user" ? "User" : "Alfie"}: ${text}`);
  }
  return transcript.join("\n\n");
}
function getMessageText(msg) {
  if (!msg) {
    return "";
  }
  if (typeof msg.content === "string") {
    return msg.content;
  }
  if (Array.isArray(msg.content)) {
    return msg.content.map((c) => c?.text || "").join(" ");
  }
  return "";
}
function isResetBootstrapOnlyConversation(messages) {
  const RESET_BOOTSTRAP_PROMPT = "A new session was started via /new or /reset.";
  const userTexts = messages.filter((m) => m?.role === "user").map((m) => getMessageText(m).trim()).filter(Boolean);
  if (userTexts.length === 0) {
    return false;
  }
  const nonBootstrapUserTexts = userTexts.filter((t) => !t.startsWith(RESET_BOOTSTRAP_PROMPT));
  return nonBootstrapUserTexts.length === 0;
}
async function updateDocsFromTranscript(messages, label, sessionId) {
  if (!isSystemEnabled("workspace")) {
    return;
  }
  const memConfig = getMemoryConfig();
  if (!memConfig.docs?.autoUpdateOnCompact) {
    return;
  }
  const fullTranscript = buildTranscript(messages);
  if (!fullTranscript.trim()) {
    console.log(`[quaid] ${label}: no transcript for doc update`);
    return;
  }
  const tmpPath = path.join(QUAID_TMP_DIR, `docs-ingest-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
  fs.writeFileSync(tmpPath, fullTranscript, { mode: 384 });
  try {
    console.log(`[quaid] ${label}: dispatching docs ingest event...`);
    const startTime = Date.now();
    const out = await emitEvent(
      "docs.ingest_transcript",
      {
        transcript_path: tmpPath,
        label,
        session_id: sessionId || null
      },
      "immediate"
    );
    const result = out?.processed?.details?.[0]?.result?.result || out?.processed?.details?.[0]?.result || {};
    const elapsed = ((Date.now() - startTime) / 1e3).toFixed(1);
    if (result.status === "up_to_date") {
      console.log(`[quaid] ${label}: all docs up-to-date (${elapsed}s)`);
      return;
    }
    if (result.status === "updated") {
      console.log(`[quaid] ${label}: docs updated (${result.updatedDocs || 0}/${result.staleDocs || 0}) (${elapsed}s)`);
      return;
    }
    if (result.status === "disabled" || result.status === "skipped") {
      console.log(`[quaid] ${label}: docs ingest skipped (${result.message || "disabled"})`);
      return;
    }
    console.log(`[quaid] ${label}: docs ingest finished (${elapsed}s)`);
  } catch (err2) {
    console.error(`[quaid] ${label} doc update failed:`, err2.message);
  } finally {
    try {
      fs.unlinkSync(tmpPath);
    } catch {
    }
  }
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
async function recall(query, limit = 5, currentSessionId, compactionTime, expandGraph = true, graphDepth = 1, technicalScope = "any", dateFrom, dateTo) {
  try {
    const args = [query, "--limit", String(limit), "--owner", resolveOwner()];
    args.push("--technical-scope", technicalScope);
    if (!expandGraph) {
      if (currentSessionId) {
        args.push("--current-session-id", currentSessionId);
      }
      if (compactionTime) {
        args.push("--compaction-time", compactionTime);
      }
      if (dateFrom) {
        args.push("--date-from", dateFrom);
      }
      if (dateTo) {
        args.push("--date-to", dateTo);
      }
      const output2 = await datastoreBridge.search(args);
      const results2 = [];
      for (const line of output2.split("\n")) {
        const match = line.match(/\[(\d+\.\d+)\]\s+\[(\w+)\](?:\([^)]*\))?(?:\[[^\]]*\])*\[C:([\d.]+)\]\s*(.+?)(?:\s*\|ID:([^|]+))?(?:\|T:([^|]*))?(?:\|VF:([^|]*))?(?:\|VU:([^|]*))?(?:\|P:([^|]*))?(?:\|O:([^|]*))?(?:\|ST:(.*))?$/);
        if (match) {
          results2.push({
            text: match[4].trim(),
            category: match[2],
            similarity: parseFloat(match[1]),
            extractionConfidence: parseFloat(match[3]),
            id: match[5]?.trim(),
            createdAt: match[6]?.trim() || void 0,
            validFrom: match[7]?.trim() || void 0,
            validUntil: match[8]?.trim() || void 0,
            privacy: match[9]?.trim() || "shared",
            ownerId: match[10]?.trim() || void 0,
            sourceType: match[11]?.trim() || void 0,
            via: "vector"
          });
        }
      }
      return results2.slice(0, limit);
    }
    if (graphDepth > 1) {
      args.push("--depth", String(graphDepth));
    }
    args.push("--json");
    const output = await datastoreBridge.searchGraphAware(args);
    const results = [];
    try {
      const parsed = JSON.parse(output);
      for (const r of parsed.direct_results) {
        results.push({
          text: r.text,
          category: r.category,
          similarity: r.similarity,
          id: r.id,
          extractionConfidence: r.extraction_confidence,
          createdAt: r.created_at,
          validFrom: r.valid_from,
          validUntil: r.valid_until,
          privacy: r.privacy,
          ownerId: r.owner_id,
          sourceType: r.source_type,
          verified: r.verified,
          via: "vector"
        });
      }
      for (const r of parsed.graph_results) {
        let text;
        if (r.direction === "in") {
          text = `${r.name} --${r.relation}--> ${r.source_name}`;
        } else {
          text = `${r.source_name} --${r.relation}--> ${r.name}`;
        }
        results.push({
          text,
          category: "graph",
          similarity: 0.75,
          // Graph results get a fixed medium-high similarity
          id: r.id,
          relation: r.relation,
          direction: r.direction,
          sourceName: r.source_name,
          via: "graph"
        });
      }
    } catch (_parseErr) {
      console.log("[quaid] JSON parse failed, trying line format");
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
          const content = line.substring(7).trim();
          results.push({
            text: content,
            category: "graph",
            similarity: 0.75,
            via: "graph"
          });
        }
      }
    }
    return results;
  } catch (err2) {
    console.error("[quaid] recall error:", err2.message);
    return [];
  }
}
const knowledgeEngine = createKnowledgeEngine({
  workspace: WORKSPACE,
  getMemoryConfig,
  isSystemEnabled,
  getProjectCatalog,
  callFastRouter: async (systemPrompt, userPrompt) => {
    const llm = await callConfiguredLLM(systemPrompt, userPrompt, "fast", 120, 45e3);
    return String(llm?.text || "");
  },
  callDeepRouter: async (systemPrompt, userPrompt) => {
    const llm = await callConfiguredLLM(systemPrompt, userPrompt, "deep", 160, 6e4);
    return String(llm?.text || "");
  },
  recallVector: async (query, limit, scope, dateFrom, dateTo) => {
    const memoryResults = await recall(
      query,
      limit,
      void 0,
      void 0,
      false,
      1,
      scope,
      dateFrom,
      dateTo
    );
    return memoryResults.map((r) => ({ ...r, via: "vector" }));
  },
  recallGraph: async (query, limit, depth, scope, dateFrom, dateTo) => {
    const graphResults = await recall(
      query,
      limit,
      void 0,
      void 0,
      true,
      depth,
      scope,
      dateFrom,
      dateTo
    );
    return graphResults.filter((r) => (r.via || "") === "graph" || r.category === "graph").map((r) => ({ ...r, via: "graph" }));
  },
  recallJournalStore: async (query, limit) => {
    const journalConfig = getMemoryConfig().docs?.journal || {};
    const journalDir = path.join(WORKSPACE, journalConfig.journalDir || "journal");
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
    } catch {
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
      } catch {
      }
    }
    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  },
  recallProjectStore: async (query, limit, project, docs) => {
    try {
      const args = [query, "--limit", String(limit)];
      if (project) args.push("--project", project);
      if (Array.isArray(docs) && docs.length > 0) {
        args.push("--docs", docs.join(","));
      }
      const out = await callDocsRag("search", args);
      if (!out || !out.trim()) return [];
      const results = [];
      const projectVotes = /* @__PURE__ */ new Map();
      const lines = out.split("\n");
      for (const line of lines) {
        const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
        if (!m) continue;
        const sourcePath = m[1];
        const file = sourcePath.split("/").pop() || sourcePath;
        const section = m[2].trim();
        const sim = Number.parseFloat(m[3]) || 0.6;
        const parts = sourcePath.split("/");
        const projIdx = parts.findIndex((p) => p === "projects");
        if (projIdx >= 0 && projIdx + 1 < parts.length) {
          const p = parts[projIdx + 1];
          if (p) {
            projectVotes.set(p, (projectVotes.get(p) || 0) + sim);
          }
        }
        results.push({
          text: `${sourcePath} > ${section}`,
          category: "project",
          similarity: sim,
          via: "project"
        });
      }
      let inferredProject = project;
      if (!inferredProject && projectVotes.size > 0) {
        inferredProject = Array.from(projectVotes.entries()).sort((a, b) => b[1] - a[1])[0]?.[0];
      }
      if (inferredProject) {
        try {
          const cfg = JSON.parse(fs.readFileSync(path.join(WORKSPACE, "config/memory.json"), "utf-8"));
          const homeDir = cfg?.projects?.definitions?.[inferredProject]?.homeDir;
          if (homeDir) {
            const projectMdPath = path.join(WORKSPACE, homeDir, "PROJECT.md");
            if (fs.existsSync(projectMdPath)) {
              const md = fs.readFileSync(projectMdPath, "utf-8").replace(/\s+/g, " ").trim().slice(0, 500);
              if (md) {
                results.unshift({
                  text: `PROJECT.md (${inferredProject}): ${md}`,
                  category: "project",
                  similarity: 0.95,
                  via: "project"
                });
              }
            }
          }
        } catch {
        }
      }
      if (results.length === 0) {
        results.push({
          text: out.replace(/\s+/g, " ").slice(0, 280),
          category: "project",
          similarity: 0.55,
          via: "project"
        });
      }
      return results.slice(0, limit);
    } catch {
      return [];
    }
  }
});
function normalizeKnowledgeDatastores(datastores, expandGraph) {
  return knowledgeEngine.normalizeKnowledgeDatastores(datastores, expandGraph);
}
const recallStoreGuidance = knowledgeEngine.renderKnowledgeDatastoreGuidanceForAgents();
async function totalRecall(query, limit, opts) {
  return knowledgeEngine.totalRecall(query, limit, opts);
}
async function total_recall(query, limit, opts) {
  return knowledgeEngine.total_recall(query, limit, opts);
}
async function getStats() {
  try {
    const output = await datastoreBridge.stats();
    return JSON.parse(output);
  } catch (err2) {
    console.error("[quaid] stats error:", err2.message);
    return null;
  }
}
function formatMemories(memories) {
  if (!memories.length) {
    return "";
  }
  const sorted = [...memories].sort((a, b) => {
    if (!a.createdAt && !b.createdAt) {
      return 0;
    }
    if (!a.createdAt) {
      return -1;
    }
    if (!b.createdAt) {
      return 1;
    }
    return a.createdAt.localeCompare(b.createdAt);
  });
  const graphNodeHits = sorted.filter((m) => isLowInformationEntityNode(m));
  const regularMemories = sorted.filter((m) => !isLowInformationEntityNode(m));
  const lines = regularMemories.map((m) => {
    const conf = m.extractionConfidence ?? 0.5;
    const timestamp = m.createdAt ? ` (${m.createdAt.split("T")[0]})` : "";
    if (conf < 0.4) {
      return `- [${m.category}]${timestamp} (uncertain) ${m.text}`;
    }
    return `- [${m.category}]${timestamp} ${m.text}`;
  });
  if (graphNodeHits.length > 0) {
    const packed = graphNodeHits.slice(0, 8).map((m) => `${m.text} (${Math.round((m.similarity || 0) * 100)}%)`).join(", ");
    lines.push(`- [graph-node-hits] Entity node references (not standalone facts): ${packed}`);
  }
  return `<injected_memories>
AUTOMATED MEMORY SYSTEM: The following memories were automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you. Use them as background context only. Items marked (uncertain) have lower extraction confidence. Dates shown are when the fact was recorded.
INJECTOR CONFIDENCE RULE: Treat injected memories as hints, not final truth. If the answer depends on personal details and the match is not exact/high-confidence, run memory_recall before answering.
${lines.join("\n")}
</injected_memories>`;
}
const quaidPlugin = {
  id: "quaid",
  name: "Memory (Local Graph)",
  description: "Local graph-based memory with SQLite + Ollama embeddings",
  kind: "memory",
  configSchema,
  register(api) {
    console.log("[quaid] Registering local graph memory plugin");
    runStartupSelfCheck();
    const dataDir = path.dirname(DB_PATH);
    if (!fs.existsSync(dataDir)) {
      fs.mkdirSync(dataDir, { recursive: true });
    }
    if (!fs.existsSync(DB_PATH)) {
      console.log("[quaid] Database not found, initializing datastore...");
      try {
        execSync(`python3 "${PYTHON_SCRIPT}" init`, {
          env: buildPythonEnv()
        });
        console.log("[quaid] Datastore initialization complete");
      } catch (err2) {
        console.error("[quaid] Datastore initialization failed:", err2.message);
      }
    }
    void getStats().then((stats) => {
      if (stats) {
        console.log(
          `[quaid] Database ready: ${stats.total_nodes} nodes, ${stats.edges} edges`
        );
      }
    });
    const beforeAgentStartHandler = async (event, ctx) => {
      if (isInternalQuaidSession(ctx?.sessionId)) {
        return;
      }
      try {
        maybeSendJanitorNudges();
      } catch {
      }
      try {
        maybeQueueJanitorHealthAlert();
      } catch {
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
            } catch {
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
              } catch {
              }
            }
            if (journalContent) {
              const header = "[JOURNAL \u2014 Full Soul Mode]\nThese are your recent journal reflections. They are part of your inner life.\n";
              event.prependContext = event.prependContext ? `${event.prependContext}

${header}${journalContent}` : `${header}${journalContent}`;
              console.log(`[quaid] Full soul mode: injected ${journalFiles.length} journal files`);
            }
          }
        } catch (err2) {
          console.log(`[quaid] Journal injection failed (non-fatal): ${err2.message}`);
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
        if (isInternalMaintenancePrompt(query)) {
          return;
        }
        const ACKNOWLEDGMENTS = /^(ok|okay|yes|no|sure|thanks|thank you|got it|sounds good|perfect|great|cool|alright|yep|nope|right|correct|agreed|absolutely|definitely|nice|good|fine|hm+|ah+|oh+)\s*[.!?]?$/i;
        const words = query.trim().split(/\s+/).filter((w) => w.length > 1);
        if (words.length < 3 || ACKNOWLEDGMENTS.test(query.trim())) {
          return;
        }
        const autoInjectK = computeDynamicK();
        const useTotalRecallForInject = isPreInjectionPassEnabled();
        const routerFailOpen = Boolean(
          getMemoryConfig().retrieval?.routerFailOpen ?? getMemoryConfig().retrieval?.router_fail_open ?? true
        );
        const injectLimit = autoInjectK;
        const injectIntent = "general";
        const injectTechnicalScope = "personal";
        const injectDatastores = useTotalRecallForInject ? void 0 : ["vector_basic", "graph"];
        const allMemories = await recallMemories({
          query,
          limit: injectLimit,
          expandGraph: true,
          datastores: injectDatastores,
          routeStores: useTotalRecallForInject,
          intent: injectIntent,
          technicalScope: injectTechnicalScope,
          failOpen: routerFailOpen,
          waitForExtraction: false,
          sourceTag: "auto_inject"
        });
        if (!allMemories.length) return;
        const currentOwner = resolveOwner();
        const filtered = allMemories.filter(
          (m) => !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner)
        );
        const uniqueSessionId = extractSessionId(event.messages || [], ctx);
        const injectionLogPath = getInjectionLogPath(uniqueSessionId);
        let previouslyInjected = [];
        try {
          const logData = JSON.parse(fs.readFileSync(injectionLogPath, "utf8"));
          previouslyInjected = logData.injected || logData.memoryTexts || [];
        } catch {
        }
        const newMemories = filtered.filter((m) => !previouslyInjected.includes(m.id || m.text));
        const toInject = newMemories.slice(0, injectLimit);
        if (!toInject.length) return;
        const formatted = formatMemories(toInject);
        event.prependContext = event.prependContext ? `${event.prependContext}

${formatted}` : formatted;
        console.log(`[quaid] Auto-injected ${toInject.length} memories for "${query.slice(0, 50)}..."`);
        try {
          if (shouldNotifyFeature("retrieval", "summary")) {
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
            spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
            console.log("[quaid] Auto-inject recall notification dispatched");
          }
        } catch (notifyErr) {
          console.log(`[quaid] Auto-inject recall notification skipped: ${notifyErr.message}`);
        }
        try {
          const newIds = toInject.map((m) => m.id || m.text);
          fs.writeFileSync(injectionLogPath, JSON.stringify({
            injected: [...previouslyInjected, ...newIds],
            lastInjectedAt: (/* @__PURE__ */ new Date()).toISOString()
          }), { mode: 384 });
        } catch {
        }
      } catch (error) {
        console.error("[quaid] Auto-injection error:", error);
      }
    };
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    api.on("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });
    const agentEndHandler = async (event, ctx) => {
      if (isInternalQuaidSession(ctx?.sessionId)) return;
      if (!isSystemEnabled("memory")) return;
      const messages = event.messages || [];
      if (messages.length === 0) return;
      const conversationMessages = getAllConversationMessages(messages);
      if (conversationMessages.length === 0) return;
      const timeoutSessionId = ctx?.sessionId || extractSessionId(messages, ctx);
      timeoutManager.setTimeoutMinutes(getCaptureTimeoutMinutes());
      timeoutManager.onAgentEnd(conversationMessages, timeoutSessionId);
      const commandSignal = detectLifecycleCommandSignal(conversationMessages);
      if (commandSignal && timeoutSessionId) {
        timeoutManager.queueExtractionSignal(timeoutSessionId, commandSignal);
        void timeoutManager.processPendingExtractionSignals();
        const trigger = commandSignal === "CompactionSignal" ? "compact" : "reset";
        const transcriptTrigger = trigger === "compact" ? "Compaction" : "Reset";
        void (async () => {
          try {
            await updateDocsFromTranscript(conversationMessages, transcriptTrigger, timeoutSessionId);
          } catch (err2) {
            console.error(`[quaid] ${transcriptTrigger} doc update fallback failed:`, err2.message);
          }
          try {
            await emitProjectEvent(conversationMessages, trigger, timeoutSessionId);
          } catch (err2) {
            console.error(`[quaid] ${transcriptTrigger} project event fallback failed:`, err2.message);
          }
        })();
      }
    };
    console.log("[quaid] Registering agent_end hook for auto-capture");
    api.on("agent_end", agentEndHandler, {
      name: "auto-capture",
      priority: 10
    });
    if (isSystemEnabled("memory")) {
      api.registerTool(
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
              technicalScope: Type.Optional(
                Type.Union([
                  Type.Literal("personal"),
                  Type.Literal("technical"),
                  Type.Literal("any")
                ], { description: "Filter memory type: personal=non-technical only, technical=technical only, any=both (default personal)." })
              ),
              filters: Type.Optional(Type.Object({
                dateFrom: Type.Optional(
                  Type.String({ description: "Only return memories from this date onward (YYYY-MM-DD)." })
                ),
                dateTo: Type.Optional(
                  Type.String({ description: "Only return memories up to this date (YYYY-MM-DD)." })
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
                  technicalScope: Type.Optional(
                    Type.Union([
                      Type.Literal("personal"),
                      Type.Literal("technical"),
                      Type.Literal("any")
                    ])
                  )
                })),
                graph: Type.Optional(Type.Object({
                  depth: Type.Optional(Type.Number()),
                  technicalScope: Type.Optional(
                    Type.Union([
                      Type.Literal("personal"),
                      Type.Literal("technical"),
                      Type.Literal("any")
                    ])
                  )
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
                maxLimit = configData?.retrieval?.maxLimit ?? 50;
              } catch {
              }
              const dynamicK = computeDynamicK();
              const { query, options = {} } = params || {};
              const requestedLimit = options.limit;
              const expandGraph = options.graph?.expand ?? true;
              const graphDepth = options.graph?.depth ?? 1;
              const datastores = options.datastores;
              const routeStores = options.routing?.enabled;
              const reasoning = options.routing?.reasoning ?? "fast";
              const intent = options.routing?.intent ?? "general";
              const technicalScope = options.technicalScope ?? "personal";
              const dateFrom = options.filters?.dateFrom;
              const dateTo = options.filters?.dateTo;
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
              console.log(`[quaid] memory_recall: query="${query?.slice(0, 50)}...", requestedLimit=${requestedLimit}, dynamicK=${dynamicK} (${getActiveNodeCount()} nodes), maxLimit=${maxLimit}, finalLimit=${limit}, expandGraph=${expandGraph}, graphDepth=${depth}, requestedDatastores=${selectedStores.join(",")}, routed=${shouldRouteStores}, reasoning=${reasoning}, intent=${intent}, technicalScope=${technicalScope}, dateFrom=${dateFrom}, dateTo=${dateTo}`);
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
                technicalScope,
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
              const vectorResults = results.filter((r) => (r.via || "vector") === "vector");
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
                if (shouldNotifyFeature("retrieval", "summary") && results.length > 0) {
                  const memoryData = results.map((m) => ({
                    text: m.text,
                    similarity: Math.round((m.similarity || 0) * 100),
                    via: m.via || "vector",
                    category: m.category || ""
                  }));
                  const memoryJson = JSON.stringify(memoryData);
                  const sourceBreakdown = JSON.stringify({
                    vector_count: vectorResults.length,
                    graph_count: graphResults.length,
                    journal_count: journalResults.length,
                    project_count: projectResults.length,
                    query,
                    mode: "tool"
                  });
                  const dataFile2 = path.join(QUAID_TMP_DIR, `recall-data-${Date.now()}.json`);
                  fs.writeFileSync(dataFile2, JSON.stringify({ memories: JSON.parse(memoryJson), source_breakdown: JSON.parse(sourceBreakdown) }), { mode: 384 });
                  spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile2)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile2)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
                }
              } catch (notifyErr) {
                console.log(`[quaid] Memory recall notification skipped: ${notifyErr.message}`);
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
            } catch (err2) {
              console.error("[quaid] memory_recall error:", err2);
              return {
                content: [{ type: "text", text: `Error recalling memories: ${String(err2)}` }],
                details: { error: String(err2) }
              };
            }
          }
        })
      );
      api.registerTool(
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
              const sessionId = ctx?.sessionId || "unknown";
              addMemoryNote(sessionId, text, category);
              console.log(`[quaid] memory_store: queued note for session ${sessionId}: "${text.slice(0, 60)}..."`);
              return {
                content: [{ type: "text", text: `Noted for memory extraction: "${text.slice(0, 100)}${text.length > 100 ? "..." : ""}" \u2014 will be processed with full quality review at next compaction.` }],
                details: { action: "queued", sessionId }
              };
            } catch (err2) {
              console.error("[quaid] memory_store error:", err2);
              return {
                content: [{ type: "text", text: `Error queuing memory note: ${String(err2)}` }],
                details: { error: String(err2) }
              };
            }
          }
        })
      );
      api.registerTool(
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
                await datastoreBridge.forget(["--id", memoryId]);
                return {
                  content: [{ type: "text", text: `Memory ${memoryId} forgotten.` }],
                  details: { action: "deleted", id: memoryId }
                };
              } else if (query) {
                await datastoreBridge.forget([query]);
                return {
                  content: [{ type: "text", text: `Deleted memories matching: "${query}"` }],
                  details: { action: "deleted", query }
                };
              }
              return {
                content: [{ type: "text", text: "Provide query or memoryId." }],
                details: { error: "missing_param" }
              };
            } catch (err2) {
              console.error("[quaid] memory_forget error:", err2);
              return {
                content: [{ type: "text", text: `Error deleting memory: ${String(err2)}` }],
                details: { error: String(err2) }
              };
            }
          }
        })
      );
    }
    api.registerTool(
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
            const searchArgs = [query, "--limit", String(limit)];
            if (project) {
              searchArgs.push("--project", project);
            }
            if (Array.isArray(docs) && docs.length > 0) {
              searchArgs.push("--docs", docs.join(","));
            }
            const results = await callDocsRag("search", searchArgs);
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
              } catch {
              }
            }
            let stalenessWarning = "";
            try {
              const stalenessJson = await callDocsUpdater("check", ["--json"]);
              const staleDocs = JSON.parse(stalenessJson || "{}");
              const staleKeys = Object.keys(staleDocs);
              if (staleKeys.length > 0) {
                const warnings = staleKeys.map(
                  (k) => `  ${k} (${staleDocs[k].gap_hours}h behind: ${staleDocs[k].stale_sources.join(", ")})`
                );
                stalenessWarning = `

STALENESS WARNING: The following docs may be outdated:
${warnings.join("\n")}
Consider running: python3 docs_updater.py update-stale --apply`;
              }
            } catch {
            }
            const text = projectMdContent ? `--- PROJECT.md (${project}) ---
${projectMdContent}

--- Search Results ---
${results || "No results."}${stalenessWarning}` : results ? results + stalenessWarning : "No results found." + stalenessWarning;
            try {
              if (shouldNotifyFeature("retrieval", "summary") && results) {
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
                  spawnNotifyScript(`
import json
from core.runtime.notify import notify_docs_search
with open(${JSON.stringify(dataFile3)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile3)})
notify_docs_search(data['query'], data['results'])
`);
                }
              }
            } catch (notifyErr) {
              console.log(`[quaid] Docs search notification skipped: ${notifyErr.message}`);
            }
            return {
              content: [{ type: "text", text }],
              details: { query, limit }
            };
          } catch (err2) {
            console.error("[quaid] projects_search error:", err2);
            return {
              content: [{ type: "text", text: `Error searching docs: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
      () => ({
        name: "docs_read",
        description: "Read the full content of a registered document by file path or title.",
        parameters: Type.Object({
          identifier: Type.String({ description: "File path (workspace-relative) or document title" })
        }),
        async execute(_toolCallId, params) {
          try {
            const { identifier } = params || {};
            const output = await callDocsRegistry("read", [identifier]);
            return {
              content: [{ type: "text", text: output || "Document not found." }],
              details: { identifier }
            };
          } catch (err2) {
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
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
            const output = await callDocsRegistry("list", args);
            return {
              content: [{ type: "text", text: output || "No documents found." }],
              details: { project: params?.project }
            };
          } catch (err2) {
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
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
            const output = await callDocsRegistry("register", args);
            return {
              content: [{ type: "text", text: output || "Registered." }],
              details: { file_path: params.file_path }
            };
          } catch (err2) {
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
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
            const output = await callDocsRegistry("create-project", args);
            return {
              content: [{ type: "text", text: output || `Project '${params.name}' created.` }],
              details: { name: params.name }
            };
          } catch (err2) {
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
      () => ({
        name: "project_list",
        description: `List all defined projects with their doc counts and metadata. Available projects: ${getProjectNames().join(", ") || "none"}`,
        parameters: Type.Object({}),
        async execute() {
          try {
            const output = await callDocsRegistry("list-projects", ["--json"]);
            return {
              content: [{ type: "text", text: output || "No projects defined." }],
              details: {}
            };
          } catch (err2) {
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    api.registerTool(
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
              extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
            } catch {
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
                  const transcript = buildTranscript(messages);
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
                const factsOutput = await datastoreBridge.search([
                  "*",
                  "--session-id",
                  sid,
                  "--owner",
                  resolveOwner(),
                  "--limit",
                  "20"
                ]);
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
          } catch (err2) {
            console.error("[quaid] session_recall error:", err2);
            return {
              content: [{ type: "text", text: `Error: ${String(err2)}` }],
              details: { error: String(err2) }
            };
          }
        }
      })
    );
    let extractionPromise = null;
    const timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      timeoutMinutes: getCaptureTimeoutMinutes(),
      isBootstrapOnly: isResetBootstrapOnlyConversation,
      logger: (msg) => console.log(msg),
      extract: async (msgs, sid, label) => {
        extractionPromise = (extractionPromise || Promise.resolve()).catch(() => {
        }).then(() => extractMemoriesFromMessages(msgs, label || "Timeout", sid));
        await extractionPromise;
      }
    });
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
        technicalScope = "any",
        dateFrom,
        dateTo,
        docs,
        datastoreOptions,
        waitForExtraction = false,
        sourceTag = "unknown"
      } = opts;
      const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${selectedStores.join(",")} routed=${routeStores} reasoning=${reasoning} intent=${intent} technicalScope=${technicalScope} waitForExtraction=${waitForExtraction}`
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
        } catch {
        } finally {
          if (raceTimer) clearTimeout(raceTimer);
        }
      }
      if (routeStores) {
        return total_recall(query, limit, {
          datastores: selectedStores,
          expandGraph,
          graphDepth,
          reasoning,
          intent,
          ranking,
          technicalScope,
          dateFrom,
          dateTo,
          docs,
          datastoreOptions
        });
      }
      return totalRecall(query, limit, {
        datastores: selectedStores,
        expandGraph,
        graphDepth,
        intent,
        ranking,
        technicalScope,
        dateFrom,
        dateTo,
        docs,
        datastoreOptions
      });
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
        } catch {
        }
      }
      return messages;
    }
    const extractMemoriesFromMessages = async (messages, label, sessionId) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        return;
      }
      const hasRestart = messages.some((m) => {
        const content = typeof m.content === "string" ? m.content : "";
        return content.startsWith("GatewayRestart:");
      });
      if (hasRestart) {
        console.log(`[quaid][extract] ${label}: detected GatewayRestart marker; scheduling recovery scan`);
        void checkForUnextractedSessions().catch((err2) => {
          console.error("[quaid] Recovery scan error:", err2);
        });
      }
      const sessionNotes = sessionId ? getAndClearMemoryNotes(sessionId) : [];
      const globalNotes = getAndClearAllMemoryNotes();
      const allNotes = Array.from(/* @__PURE__ */ new Set([...sessionNotes, ...globalNotes]));
      if (allNotes.length > 0) {
        console.log(`[quaid] ${label}: prepend ${allNotes.length} queued memory note(s)`);
      }
      const fullTranscript = buildTranscript(messages);
      if (!fullTranscript.trim() && allNotes.length === 0) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        return;
      }
      const transcriptForExtraction = allNotes.length > 0 ? `=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===
${allNotes.map((n) => `- ${n}`).join("\n")}
=== END EXPLICIT MEMORY REQUESTS ===

` + fullTranscript : fullTranscript;
      console.log(`[quaid] ${label} transcript: ${messages.length} messages, ${transcriptForExtraction.length} chars`);
      if (getMemoryConfig().notifications?.showProcessingStart !== false && shouldNotifyFeature("extraction", "summary")) {
        const triggerType2 = resolveExtractionTrigger(label);
        const triggerDesc = triggerType2 === "compaction" ? "compaction" : triggerType2 === "recovery" ? "recovery" : triggerType2 === "timeout" ? "timeout" : triggerType2 === "new" ? "/new" : "reset";
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("\u{1F9E0} Processing memories from ${triggerDesc}...")
`);
      }
      const journalConfig = getMemoryConfig().docs?.journal || {};
      const journalEnabled = isSystemEnabled("journal") && journalConfig.enabled !== false;
      const snippetsEnabled = journalEnabled && journalConfig.snippetsEnabled !== false;
      const extracted = await callExtractPipeline({
        transcript: transcriptForExtraction,
        owner: resolveOwner(),
        label: resolveExtractionTrigger(label),
        sessionId,
        writeSnippets: snippetsEnabled,
        writeJournal: journalEnabled
      });
      const stored = Number(extracted?.facts_stored || 0);
      const skipped = Number(extracted?.facts_skipped || 0);
      const edgesCreated = Number(extracted?.edges_created || 0);
      const factDetails = Array.isArray(extracted?.facts) ? extracted.facts : [];
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
      } catch {
      }
      const hasSnippets = Object.keys(snippetDetails).length > 0;
      const hasJournalEntries = Object.keys(journalDetails).length > 0;
      const triggerType = resolveExtractionTrigger(label);
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new") && shouldNotifyFeature("extraction", "summary");
      if ((factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion) && shouldNotifyFeature("extraction", "summary")) {
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
          spawnNotifyScript(`
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
        } catch (notifyErr) {
          console.log(`[quaid] Extraction notification skipped: ${notifyErr.message}`);
        }
      }
      if (triggerType === "timeout") {
        maybeForceCompactionAfterTimeout(sessionId);
      }
      try {
        const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
        let extractionLog = {};
        try {
          extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
        } catch {
        }
        let topicHint = "";
        for (const m of messages) {
          if (m?.role === "user") {
            const cleaned = getMessageText(m).trim();
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
        fs.writeFileSync(extractionLogPath, JSON.stringify(extractionLog, null, 2), { mode: 384 });
      } catch (logErr) {
        console.log(`[quaid] extraction log update failed: ${logErr.message}`);
      }
    };
    async function checkForUnextractedSessions() {
      const flagPath = path.join(QUAID_RUNTIME_DIR, "quaid-recovery-ran.txt");
      try {
        const flagStat = fs.statSync(flagPath);
        const fiveMinAgo = Date.now() - 5 * 60 * 1e3;
        if (flagStat.mtimeMs > fiveMinAgo) {
          console.log("[quaid] Recovery scan already ran recently, skipping");
          return;
        }
      } catch {
      }
      console.log("[quaid] Running recovery scan for unextracted sessions...");
      const sessionsDir = path.join(os.homedir(), ".openclaw", "sessions");
      if (!fs.existsSync(sessionsDir)) {
        console.log("[quaid] No sessions directory found, skipping recovery");
        fs.writeFileSync(flagPath, (/* @__PURE__ */ new Date()).toISOString());
        return;
      }
      const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
      let extractionLog = {};
      try {
        extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
      } catch {
      }
      const sessionFiles = fs.readdirSync(sessionsDir).filter((f) => f.endsWith(".jsonl"));
      let recovered = 0;
      for (const file of sessionFiles) {
        const sessionId = file.replace(".jsonl", "");
        const filePath = path.join(sessionsDir, file);
        try {
          const stat = fs.statSync(filePath);
          if (Date.now() - stat.mtimeMs < 5 * 60 * 1e3) {
            continue;
          }
          const logEntry = extractionLog[sessionId];
          if (logEntry) {
            const extractedAt = new Date(logEntry.last_extracted_at).getTime();
            if (extractedAt >= stat.mtimeMs) {
              continue;
            }
          }
          const messages = readMessagesFromSessionFile(filePath);
          if (messages.length < 4) {
            continue;
          }
          console.log(`[quaid] Recovering unextracted session ${sessionId} (${messages.length} messages)`);
          await extractMemoriesFromMessages(messages, "Recovery", sessionId);
          recovered++;
        } catch (err2) {
          console.error(`[quaid] Recovery failed for session ${sessionId}:`, err2.message);
        }
      }
      console.log(`[quaid] Recovery scan complete: ${recovered} sessions recovered`);
      fs.writeFileSync(flagPath, (/* @__PURE__ */ new Date()).toISOString());
    }
    api.on("before_compaction", async (event, ctx) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        let messages;
        if (event.sessionFile) {
          try {
            messages = readMessagesFromSessionFile(event.sessionFile);
            console.log(`[quaid] before_compaction: read ${messages.length} messages from sessionFile`);
          } catch (readErr) {
            console.warn(`[quaid] before_compaction: sessionFile read failed, falling back to messages array: ${String(readErr)}`);
            messages = event.messages || [];
          }
        } else {
          messages = event.messages || [];
        }
        const sessionId = ctx?.sessionId;
        const conversationMessages = getAllConversationMessages(messages);
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_compaction: skip empty/internal transcript session=${sessionId || "unknown"}`);
          return;
        }
        console.log(`[quaid] before_compaction hook triggered, ${messages.length} messages, session=${sessionId || "unknown"}`);
        const doExtraction = async () => {
          if (isSystemEnabled("memory")) {
            const extractionSessionId = sessionId || extractSessionId(conversationMessages, ctx);
            timeoutManager.queueExtractionSignal(extractionSessionId, "CompactionSignal");
            console.log(`[quaid][signal] queued CompactionSignal session=${extractionSessionId}`);
          } else {
            console.log("[quaid] Compaction: memory extraction skipped \u2014 memory system disabled");
          }
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);
          try {
            await updateDocsFromTranscript(conversationMessages, "Compaction", uniqueSessionId);
          } catch (err2) {
            console.error("[quaid] Compaction doc update failed:", err2.message);
          }
          try {
            await emitProjectEvent(conversationMessages, "compact", uniqueSessionId);
          } catch (err2) {
            console.error("[quaid] Compaction project event failed:", err2.message);
          }
          if (isSystemEnabled("memory") && uniqueSessionId) {
            const logPath = getInjectionLogPath(uniqueSessionId);
            let logData = {};
            try {
              logData = JSON.parse(fs.readFileSync(logPath, "utf8"));
            } catch {
            }
            logData.lastCompactionAt = (/* @__PURE__ */ new Date()).toISOString();
            logData.memoryTexts = [];
            fs.writeFileSync(logPath, JSON.stringify(logData, null, 2), { mode: 384 });
            console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
          }
        };
        extractionPromise = (extractionPromise || Promise.resolve()).catch(() => {
        }).then(() => doExtraction());
      } catch (err2) {
        console.error("[quaid] before_compaction hook failed:", err2);
      }
    }, {
      name: "compaction-memory-extraction",
      priority: 10
    });
    api.on("before_reset", async (event, ctx) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        let messages;
        if (event.sessionFile) {
          try {
            messages = readMessagesFromSessionFile(event.sessionFile);
            console.log(`[quaid] before_reset: read ${messages.length} messages from sessionFile`);
          } catch (readErr) {
            console.warn(`[quaid] before_reset: sessionFile read failed, falling back to messages array: ${String(readErr)}`);
            messages = event.messages || [];
          }
        } else {
          messages = event.messages || [];
        }
        const reason = event.reason || "unknown";
        const sessionId = ctx?.sessionId;
        const conversationMessages = getAllConversationMessages(messages);
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_reset: skip empty/internal transcript session=${sessionId || "unknown"}`);
          return;
        }
        console.log(`[quaid] before_reset hook triggered (reason: ${reason}), ${messages.length} messages, session=${sessionId || "unknown"}`);
        const doExtraction = async () => {
          if (isSystemEnabled("memory")) {
            const extractionSessionId = sessionId || extractSessionId(conversationMessages, ctx);
            timeoutManager.queueExtractionSignal(extractionSessionId, "ResetSignal");
            console.log(`[quaid][signal] queued ResetSignal session=${extractionSessionId}`);
          } else {
            console.log("[quaid] Reset: memory extraction skipped \u2014 memory system disabled");
          }
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);
          try {
            await updateDocsFromTranscript(conversationMessages, "Reset", uniqueSessionId);
          } catch (err2) {
            console.error("[quaid] Reset doc update failed:", err2.message);
          }
          try {
            await emitProjectEvent(conversationMessages, "reset", uniqueSessionId);
          } catch (err2) {
            console.error("[quaid] Reset project event failed:", err2.message);
          }
          console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"}`);
        };
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${extractionPromise ? "yes" : "no"}`);
        extractionPromise = (extractionPromise || Promise.resolve()).catch((chainErr) => {
          console.warn(`[quaid][reset] prior_extraction_chain_error session=${sessionId || "unknown"} err=${String(chainErr?.message || chainErr)}`);
        }).then(() => doExtraction()).catch((doErr) => {
          console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
          throw doErr;
        });
      } catch (err2) {
        console.error("[quaid] before_reset hook failed:", err2);
      }
    }, {
      name: "reset-memory-extraction",
      priority: 10
    });
    api.registerHttpRoute({
      path: "/plugins/quaid/llm",
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
        const { system_prompt, user_message, model_tier, max_tokens = 4e3 } = body;
        if (!system_prompt || !user_message) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "system_prompt and user_message required" }));
          return;
        }
        try {
          const tier = model_tier === "fast" ? "fast" : "deep";
          const data = await callConfiguredLLM(system_prompt, user_message, tier, max_tokens, 6e5);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(data));
        } catch (err2) {
          console.error(`[quaid] LLM proxy error: ${String(err2)}`);
          const msg = String(err2?.message || err2);
          const status = msg.includes("No ") || msg.includes("Unsupported provider") || msg.includes("ReasoningModelClasses") ? 503 : 502;
          res.writeHead(status, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: `LLM proxy error: ${String(err2)}` }));
        }
      }
    });
    api.registerHttpRoute({
      path: "/memory/injected",
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
            } catch (err2) {
              console.error(`[quaid] Failed to read enhanced log: ${String(err2)}`);
            }
          }
          if (!logData && fs.existsSync(tempLogPath)) {
            try {
              const content = fs.readFileSync(tempLogPath, "utf8");
              logData = JSON.parse(content);
            } catch (err2) {
              console.error(`[quaid] Failed to read temp log: ${String(err2)}`);
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
          res.writeHead(200, {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET",
            "Access-Control-Allow-Headers": "Content-Type"
          });
          res.end(JSON.stringify(responseData, null, 2));
        } catch (err2) {
          console.error(`[quaid] HTTP endpoint error: ${String(err2)}`);
          res.writeHead(500, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Internal server error" }));
        }
      }
    });
    console.log("[quaid] Plugin loaded with compaction/reset hooks and HTTP endpoint");
  }
};
var adapter_default = quaidPlugin;
export {
  adapter_default as default
};
