"use strict";
/**
 * quaid - Total Recall memory system plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
const typebox_1 = require("@sinclair/typebox");
const node_child_process_1 = require("node:child_process");
const path = __importStar(require("node:path"));
const fs = __importStar(require("node:fs"));
const os = __importStar(require("node:os"));
const session_timeout_js_1 = require("../../core/session-timeout.js");
const command_signals_js_1 = require("./command-signals.js");
const delayed_requests_js_1 = require("./delayed-requests.js");
const orchestrator_js_1 = require("./knowledge/orchestrator.js");

// Configuration
const PLUGIN_DIR = __dirname;
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
    }
    catch { }
    return process.cwd();
}
const WORKSPACE = _resolveWorkspace();
const PYTHON_SCRIPT = path.join(WORKSPACE, "plugins/quaid/memory_graph.py");
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
const DELAYED_NOTIFICATIONS_PATH = path.join(QUAID_JANITOR_DIR, "delayed-notifications.json");
const DELAYED_LLM_REQUESTS_PATH = path.join(QUAID_NOTES_DIR, "delayed-llm-requests.json");
const JANITOR_NUDGE_STATE_PATH = path.join(QUAID_NOTES_DIR, "janitor-nudge-state.json");
for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
    try {
        fs.mkdirSync(p, { recursive: true });
    }
    catch { }
}
// Dynamic retrieval limit — scales logarithmically with graph size
// Formula: K = 11.5 * ln(N) - 61.7, fitted to K-sweep benchmarks:
//   S-scale (322 nodes): K=5 optimal   → formula gives 4.7 → 5
//   L-scale (1182 nodes): K=20 optimal → formula gives 19.7 → 20
// Clamped to [5, 40]. Ceiling backed by "Lost in the Middle" (Liu 2023).
let _cachedNodeCount = null;
let _nodeCountTimestamp = 0;
const NODE_COUNT_CACHE_MS = 5 * 60 * 1000; // 5 minutes
function getActiveNodeCount() {
    const now = Date.now();
    if (_cachedNodeCount !== null && (now - _nodeCountTimestamp) < NODE_COUNT_CACHE_MS) {
        return _cachedNodeCount;
    }
    try {
        const result = execSync(`sqlite3 "${DB_PATH}" "SELECT COUNT(*) FROM nodes WHERE status='active'"`, { encoding: 'utf-8', timeout: 5000 });
        _cachedNodeCount = parseInt(result.trim(), 10) || 100;
        _nodeCountTimestamp = now;
        return _cachedNodeCount;
    }
    catch {
        return _cachedNodeCount ?? 100;
    }
}
function computeDynamicK() {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10)
        return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
}
// Model resolution — reads from config/memory.json, no hardcoded model IDs
let _memoryConfig = null;
function getMemoryConfig() {
    if (_memoryConfig) {
        return _memoryConfig;
    }
    try {
        _memoryConfig = JSON.parse(fs.readFileSync(path.join(WORKSPACE, "config/memory.json"), "utf8"));
    }
    catch {
        _memoryConfig = {};
    }
    return _memoryConfig;
}
// System gates — check if a subsystem is enabled in config
function isSystemEnabled(system) {
    const config = getMemoryConfig();
    const systems = config.systems || {};
    // Default to true if not specified
    return systems[system] !== false;
}
function isPreInjectionPassEnabled() {
    const retrieval = getMemoryConfig().retrieval || {};
    if (typeof retrieval.preInjectionPass === "boolean")
        return retrieval.preInjectionPass;
    if (typeof retrieval.pre_injection_pass === "boolean")
        return retrieval.pre_injection_pass;
    return true;
}
function effectiveNotificationLevel(feature) {
    const notifications = getMemoryConfig().notifications || {};
    const featureConfig = notifications[feature];
    if (typeof featureConfig === "string" && featureConfig.trim()) {
        return featureConfig.trim().toLowerCase();
    }
    if (featureConfig && typeof featureConfig === "object" && typeof featureConfig.verbosity === "string") {
        return featureConfig.verbosity.trim().toLowerCase();
    }
    const level = String(notifications.level || "normal").trim().toLowerCase();
    const defaults = {
        quiet: { janitor: "off", extraction: "off", retrieval: "off" },
        normal: { janitor: "summary", extraction: "summary", retrieval: "off" },
        verbose: { janitor: "full", extraction: "summary", retrieval: "summary" },
        debug: { janitor: "full", extraction: "full", retrieval: "full" },
    };
    const levelDefaults = defaults[level] || defaults.normal;
    return String(levelDefaults[feature] || "off").toLowerCase();
}
function shouldNotifyFeature(feature, detail = "summary") {
    const effective = effectiveNotificationLevel(feature);
    if (effective === "off")
        return false;
    if (detail === "summary")
        return effective === "summary" || effective === "full";
    return effective === "full";
}
function normalizeProvider(provider) {
    return String(provider || "").trim().toLowerCase();
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
        const key = normalizeProvider(String(provider || "").trim());
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
            const primaryModel = String(cfg?.agents?.main?.modelPrimary || cfg?.agents?.defaults?.modelPrimary || "").trim();
            if (primaryModel.includes("/")) {
                const provider = primaryModel.split("/", 1)[0];
                const normalized = normalizeProvider(provider);
                if (normalized) {
                    return normalized;
                }
            }
        }
    }
    catch { }
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
    }
    catch { }
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
    throw new Error("models.llmProvider is default, but no active gateway provider could be resolved");
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
            if (configuredTierProvider !== "default" && normalizedProvider !== configuredTierProvider) {
                throw new Error(`models.${tier === "fast" ? "fastReasoning" : "deepReasoning"} provider "${normalizedProvider}" does not match models.${tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider"}="${configuredTierProvider}"`);
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
    const mappedModel = classMap[effectiveTierProvider];
    if (!mappedModel) {
        throw new Error(`No ${tier}ReasoningModelClasses entry for provider "${effectiveTierProvider}" while using default ${tier} reasoning model`);
    }
    return {
        provider: effectiveTierProvider,
        model: mappedModel,
    };
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
function triggerLabelFromType(trigger) {
    if (trigger === "compaction") {
        return "Compaction";
    }
    if (trigger === "recovery") {
        return "Recovery";
    }
    if (trigger === "timeout") {
        return "Timeout";
    }
    if (trigger === "new") {
        return "New";
    }
    if (trigger === "reset") {
        return "Reset";
    }
    return "Unknown";
}
// Config schema
const configSchema = typebox_1.Type.Object({
    autoCapture: typebox_1.Type.Optional(typebox_1.Type.Boolean({ default: false })),
    autoRecall: typebox_1.Type.Optional(typebox_1.Type.Boolean({ default: true })),
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
    }
    catch {
        _usersConfig = { defaultOwner: "quaid", identities: {} };
    }
    return _usersConfig;
}
function resolveOwner(speaker, channel) {
    const config = getUsersConfig();
    for (const [userId, identity] of Object.entries(config.identities)) {
        // Match by speaker name
        if (speaker && identity.speakers.some(s => s.toLowerCase() === speaker.toLowerCase())) {
            return userId;
        }
        // Match by channel
        if (channel && identity.channels[channel]) {
            const allowed = identity.channels[channel];
            if (allowed.includes("*")) {
                return userId;
            }
            if (speaker && allowed.some(a => a.toLowerCase() === speaker.toLowerCase())) {
                return userId;
            }
        }
    }
    return config.defaultOwner;
}
function isInternalQuaidSession(sessionId) {
    const sid = typeof sessionId === "string" ? sessionId.trim() : "";
    if (!sid)
        return false;
    return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
}
// ============================================================================
// Python Bridge
// ============================================================================
const PYTHON_BRIDGE_TIMEOUT_MS = 120000; // 2 minutes
async function callPython(command, args = []) {
    return new Promise((resolve, reject) => {
        const proc = (0, node_child_process_1.spawn)("python3", [PYTHON_SCRIPT, command, ...args], {
            env: { ...process.env, MEMORY_DB_PATH: DB_PATH, QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE },
        });
        let stdout = "";
        let stderr = "";
        let settled = false;
        const timer = setTimeout(() => {
            if (!settled) {
                settled = true;
                proc.kill("SIGTERM");
                reject(new Error(`Python bridge timeout after ${PYTHON_BRIDGE_TIMEOUT_MS}ms: ${command} ${args.join(" ")}`));
            }
        }, PYTHON_BRIDGE_TIMEOUT_MS);
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
            }
            else {
                reject(new Error(`Python error: ${stderr || stdout}`));
            }
        });
        proc.on("error", (err) => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            reject(err);
        });
    });
}
// ============================================================================
// Memory Notes — queued for extraction at compaction/reset
// ============================================================================
// Session-scoped notes: memory_store writes here instead of directly to DB.
// At compaction/reset, these are prepended to the transcript so Opus extracts
// them with full context, edges, and quality review.
const _memoryNotes = new Map();
const NOTES_DIR = QUAID_NOTES_DIR;
function getNotesPath(sessionId) {
    return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`);
}
function getInjectionLogPath(sessionId) {
    return path.join(QUAID_INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
}
function addMemoryNote(sessionId, text, category) {
    // In-memory
    if (!_memoryNotes.has(sessionId)) {
        _memoryNotes.set(sessionId, []);
    }
    _memoryNotes.get(sessionId).push(`[${category}] ${text}`);
    // Persist to disk (survives gateway restart)
    try {
        const notesPath = getNotesPath(sessionId);
        let existing = [];
        try {
            existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
        }
        catch { }
        existing.push(`[${category}] ${text}`);
        fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 0o600 });
    }
    catch { }
}
function getAndClearMemoryNotes(sessionId) {
    // Merge in-memory + disk
    const inMemory = _memoryNotes.get(sessionId) || [];
    let onDisk = [];
    const notesPath = getNotesPath(sessionId);
    try {
        onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
    }
    catch { }
    // Deduplicate (in case both sources have the same note)
    const all = Array.from(new Set([...inMemory, ...onDisk]));
    // Clear both
    _memoryNotes.delete(sessionId);
    try {
        fs.unlinkSync(notesPath);
    }
    catch { }
    return all;
}
// Also collect notes from ALL sessions (for cases where session ID isn't matched)
function getAndClearAllMemoryNotes() {
    const all = [];
    // In-memory
    for (const [sid, notes] of _memoryNotes.entries()) {
        all.push(...notes);
        _memoryNotes.delete(sid);
    }
    // Disk — glob for memory-notes-*.json
    try {
        const files = fs.readdirSync(NOTES_DIR).filter(f => f.startsWith("memory-notes-") && f.endsWith(".json"));
        for (const f of files) {
            const fp = path.join(NOTES_DIR, f);
            try {
                const notes = JSON.parse(fs.readFileSync(fp, "utf8"));
                all.push(...notes);
                fs.unlinkSync(fp);
            }
            catch { }
        }
    }
    catch { }
    return Array.from(new Set(all));
}
// ============================================================================
// Session ID Helper
// ============================================================================
function extractSessionId(messages, ctx) {
    // Prefer Pi SDK session UUID if provided by hook context
    if (ctx?.sessionId) {
        return ctx.sessionId;
    }
    // Fallback: hash-based session ID (for backward compat with older source)
    // Find first user message with timestamp
    let firstTimestamp = "";
    const filteredMessages = messages.filter((m) => {
        if (m.role !== "user") {
            return false;
        }
        // Skip system-injected messages even if they have timestamps
        let content = '';
        if (typeof m.content === 'string') {
            content = m.content;
        }
        else if (Array.isArray(m.content)) {
            content = m.content.map((c) => c.text || '').join(' ');
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
        }
        else {
            firstTimestamp = Date.now().toString();
        }
    }
    else {
        firstTimestamp = Date.now().toString();
    }
    // Create session identifier: timestamp hash only (channel agnostic)
    const timestampHash = require('crypto').createHash('md5').update(firstTimestamp).digest('hex').substring(0, 12);
    return timestampHash;
}
function getAllConversationMessages(messages) {
    if (!Array.isArray(messages) || messages.length === 0)
        return [];
    return messages.filter((msg) => {
        if (!msg || (msg.role !== "user" && msg.role !== "assistant"))
            return false;
        const text = getMessageText(msg).trim();
        if (!text)
            return false;
        // Filter synthetic internal extraction traffic that can leak into event.messages.
        if (text.startsWith("Extract memorable facts and journal entries from this conversation:"))
            return false;
        if (isInternalMaintenancePrompt(text))
            return false;
        if (msg.role === "assistant") {
            const compact = text.replace(/\s+/g, " ").trim();
            if (/^\{\s*"facts"\s*:\s*\[/.test(compact)) {
                try {
                    const parsed = JSON.parse(compact);
                    if (parsed && typeof parsed === "object") {
                        const keys = Object.keys(parsed);
                        const onlyExtractionKeys = keys.every((k) => k === "facts" || k === "journal_entries" || k === "soul_snippets");
                        if (onlyExtractionKeys && Array.isArray(parsed.facts))
                            return false;
                    }
                }
                catch { }
            }
        }
        return true;
    });
}
function isInternalMaintenancePrompt(text) {
    const t = String(text || "").trim();
    if (!t)
        return false;
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
        "are these two statements the same fact",
    ];
    return markers.some((m) => s.includes(m));
}
function isLowInformationEntityNode(result) {
    if ((result.via || "vector") === "graph" || result.category === "graph")
        return false;
    const category = String(result.category || "").toLowerCase();
    if (!["person", "concept", "event", "entity"].includes(category))
        return false;
    const text = String(result.text || "").trim();
    if (!text)
        return true;
    const words = text.split(/\s+/).filter(Boolean);
    // Entity stub: bare name/token without relational/factual structure.
    if (words.length <= 2 && /^[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?$/.test(text))
        return true;
    return false;
}
function messageToText(msg) {
    if (!msg)
        return "";
    if (typeof msg.content === "string")
        return msg.content;
    if (Array.isArray(msg.content))
        return msg.content.map((c) => c?.text || "").join("\n");
    return "";
}
function latestUserCommand(messages) {
    for (let i = messages.length - 1; i >= 0; i--) {
        const msg = messages[i];
        if (!msg || msg.role !== "user")
            continue;
        const text = messageToText(msg);
        if (!text)
            continue;
        if (/(^|\n)\s*\/restart(?:@[a-z0-9_]+)?(?:\s|$)/im.test(text))
            return "reset";
        if (/(^|\n)\s*\/reset(?:@[a-z0-9_]+)?(?:\s|$)/im.test(text))
            return "reset";
        if (/(^|\n)\s*\/new(?:@[a-z0-9_]+)?(?:\s|$)/im.test(text))
            return "new";
        return null;
    }
    return null;
}
function summarizeEventKeys(event) {
    try {
        if (!event || typeof event !== "object")
            return "none";
        return Object.keys(event).slice(0, 20).join(",") || "none";
    }
    catch {
        return "error";
    }
}
function getActiveSessionFileFromSessionsJson() {
    try {
        const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
        const raw = fs.readFileSync(sessionsPath, "utf8");
        const data = JSON.parse(raw);
        const entry = data?.["agent:main:main"] || data?.active;
        const sessionFile = entry?.sessionFile;
        const sessionId = entry?.sessionId || (typeof sessionFile === "string" ? path.basename(sessionFile).split(".jsonl")[0] : undefined);
        return { sessionId, sessionFile };
    }
    catch {
        return {};
    }
}
// ============================================================================
// Python Bridges (docs_updater, docs_rag)
// ============================================================================
const DOCS_UPDATER = path.join(WORKSPACE, "plugins/quaid/docs_updater.py");
const DOCS_RAG = path.join(WORKSPACE, "plugins/quaid/docs_rag.py");
const DOCS_REGISTRY = path.join(WORKSPACE, "plugins/quaid/docs_registry.py");
const PROJECT_UPDATER = path.join(WORKSPACE, "plugins/quaid/project_updater.py");
function _getGatewayCredential(providers) {
    try {
        const profilesPath = path.join(os.homedir(), ".openclaw", "agents", "main", "agent", "auth-profiles.json");
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
            for (const [id, cred] of Object.entries(profiles)) {
                const c = cred;
                if (providers.includes(c.provider) || providers.some((p) => id.startsWith(`${p}:`))) {
                    const key = c.access || c.token || c.key;
                    if (key) return key;
                }
            }
        }
    }
    catch { /* auth-profiles not available */ }
    return undefined;
}
function _getAnthropicCredential() {
    let apiKey = process.env.ANTHROPIC_API_KEY;
    if (apiKey) return apiKey;
    apiKey = _getGatewayCredential(["anthropic"]);
    if (apiKey) return apiKey;
    // 3. Legacy auth.json
    try {
        const authPath = path.join(os.homedir(), ".openclaw", "agents", "main", "agent", "auth.json");
        if (fs.existsSync(authPath)) {
            const data = JSON.parse(fs.readFileSync(authPath, "utf8"));
            const key = data?.anthropic?.key;
            if (key) return key;
        }
    }
    catch { /* auth.json not available */ }
    // 4. macOS Keychain (fallback)
    try {
        apiKey = (0, node_child_process_1.execSync)("security find-generic-password -a alfie -s anthropic-api-key -w", { encoding: "utf8" }).trim();
        if (apiKey) return apiKey;
    }
    catch { /* Keychain not available */ }
    return undefined;
}
function _getOpenAICredential() {
    // OAuth/API creds managed by OpenClaw auth profiles only.
    const gatewayKey = _getGatewayCredential(["openai-codex", "openai"]);
    if (gatewayKey) return gatewayKey;
    return undefined;
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
    }
    catch {
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
    return undefined;
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
    }
    catch {
        store = {};
    }
    const prev = (store[sessionKey] && typeof store[sessionKey] === "object") ? store[sessionKey] : {};
    // Use a fresh session id per utility call lane update to prevent
    // unbounded conversation history growth from slowing gateway calls.
    const sessionId = `quaid-${tier}-${now}`;
    store[sessionKey] = {
        ...prev,
        sessionId,
        updatedAt: now,
        providerOverride: resolved.provider,
        modelOverride: resolved.model,
    };
    fs.mkdirSync(path.dirname(storePath), { recursive: true });
    fs.writeFileSync(storePath, JSON.stringify(store, null, 2), { mode: 0o600 });
    return sessionKey;
}
async function callConfiguredLLM(systemPrompt, userMessage, modelTier, maxTokens, timeoutMs = 600000) {
    const resolved = resolveTierModel(modelTier);
    const provider = normalizeProvider(resolved.provider);
    const started = Date.now();
    console.log(`[quaid][llm] request tier=${modelTier} provider=${provider} model=${resolved.model} max_tokens=${maxTokens} system_len=${systemPrompt.length} user_len=${userMessage.length}`);
    const gatewayUrl = `${_getGatewayBaseUrl()}/v1/responses`;
    const token = _getGatewayToken();
    console.log(`[quaid][llm] gateway_prepare tier=${modelTier} gateway_url=${gatewayUrl} auth_token=${token ? "present" : "absent"}`);
    const headers = {
        "Content-Type": "application/json",
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
                    { type: "message", role: "user", content: userMessage },
                ],
                max_output_tokens: maxTokens,
                stream: false,
            }),
            signal: AbortSignal.timeout(timeoutMs),
        });
    }
    catch (err) {
        const durationMs = Date.now() - started;
        console.error(`[quaid][llm] gateway_fetch_error tier=${modelTier} duration_ms=${durationMs} error=${err?.name || "Error"}:${err?.message || String(err)}`);
        throw err;
    }
    const rawBody = await gatewayRes.text();
    let data = null;
    try {
        data = rawBody ? JSON.parse(rawBody) : {};
    }
    catch (err) {
        const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
        console.error(`[quaid][llm] gateway_parse_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} body_preview=${JSON.stringify(bodyPreview)}`);
        throw new Error(`Gateway response parse failed (${gatewayRes.status} ${gatewayRes.statusText})`);
    }
    if (!gatewayRes.ok) {
        const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
        console.error(`[quaid][llm] gateway_http_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} body_preview=${JSON.stringify(bodyPreview)}`);
        const err = data?.error?.message || data?.message || `Gateway OpenResponses error ${gatewayRes.status}`;
        throw new Error(err);
    }
    const text = typeof data.output_text === "string"
        ? data.output_text
        : Array.isArray(data.output)
            ? data.output
                .flatMap((o) => Array.isArray(o?.content) ? o.content : [])
                .filter((c) => (c?.type === "output_text" || c?.type === "text") && typeof c?.text === "string")
                .map((c) => c.text)
                .join("\n")
            : "";
    const durationMs = Date.now() - started;
    console.log(`[quaid][llm] response provider=${provider} model=${resolved.model} duration_ms=${durationMs} output_len=${text.length} status=${gatewayRes.status}`);
    return {
        text,
        model: resolved.model,
        input_tokens: data?.usage?.input_tokens || 0,
        output_tokens: data?.usage?.output_tokens || 0,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        truncated: false,
    };
}
function _spawnWithTimeout(script, command, args, label, env, timeoutMs = PYTHON_BRIDGE_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
        const proc = (0, node_child_process_1.spawn)("python3", [script, command, ...args], {
            cwd: WORKSPACE,
            env: { ...process.env, ...env },
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
        proc.stdout.on("data", (data) => { stdout += data; });
        proc.stderr.on("data", (data) => { stderr += data; });
        proc.on("close", (code) => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            if (code === 0) {
                resolve(stdout.trim());
            }
            else {
                reject(new Error(`${label} error: ${stderr || stdout}`));
            }
        });
        proc.on("error", (err) => {
            if (settled) {
                return;
            }
            settled = true;
            clearTimeout(timer);
            reject(err);
        });
    });
}
async function callDocsUpdater(command, args = []) {
    const apiKey = _getAnthropicCredential();
    return _spawnWithTimeout(DOCS_UPDATER, command, args, "docs_updater", {
        QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE, ...(apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}),
    });
}
async function callDocsRag(command, args = []) {
    return _spawnWithTimeout(DOCS_RAG, command, args, "docs_rag", {
        QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
    });
}
async function callDocsRegistry(command, args = []) {
    return _spawnWithTimeout(DOCS_REGISTRY, command, args, "docs_registry", {
        QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
    });
}
async function callProjectUpdater(command, args = []) {
    const apiKey = _getAnthropicCredential();
    return _spawnWithTimeout(PROJECT_UPDATER, command, args, "project_updater", {
        QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE, ...(apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}),
    }, 300000); // 5 min for Opus calls
}
// ============================================================================
// Project Helpers
// ============================================================================
function getProjectNames() {
    try {
        const output = (0, node_child_process_1.execSync)(`python3 "${DOCS_REGISTRY}" list-projects --names-only`, { encoding: "utf-8", env: { ...process.env, MEMORY_DB_PATH: DB_PATH, QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE }, timeout: 10000 }).trim();
        return output.split("\n").filter(Boolean);
    }
    catch {
        // Fallback: read from JSON (backward compat / fresh install)
        try {
            const configPath = path.join(WORKSPACE, "config/memory.json");
            const configData = JSON.parse(fs.readFileSync(configPath, "utf-8"));
            return Object.keys(configData?.projects?.definitions || {});
        }
        catch {
            return [];
        }
    }
}
function _firstUsefulLine(content) {
    return String(content || "")
        .split("\n")
        .map((line) => line.trim())
        .find((line) => line && !line.startsWith("#") && !line.startsWith("|")) || "";
}
function getProjectDescriptionFromToolsMd(homeDir) {
    try {
        if (!homeDir)
            return "";
        const toolsPath = path.join(WORKSPACE, homeDir, "TOOLS.md");
        if (!fs.existsSync(toolsPath))
            return "";
        const content = fs.readFileSync(toolsPath, "utf8");
        const m = content.match(/^\s*(?:Project\s+Description|Description)\s*:\s*(.+)$/im);
        if (m && m[1])
            return m[1].trim().slice(0, 180);
        return _firstUsefulLine(content).slice(0, 180);
    }
    catch {
        return "";
    }
}
function getProjectDescriptionFromProjectMd(homeDir) {
    try {
        if (!homeDir)
            return "";
        const projectPath = path.join(WORKSPACE, homeDir, "PROJECT.md");
        if (!fs.existsSync(projectPath))
            return "";
        const content = fs.readFileSync(projectPath, "utf8");
        const m = content.match(/^\s*Description\s*:\s*(.+)$/im);
        if (m && m[1])
            return m[1].trim().slice(0, 180);
        return _firstUsefulLine(content).slice(0, 180);
    }
    catch {
        return "";
    }
}
function getProjectCatalog() {
    try {
        const configPath = path.join(WORKSPACE, "config/memory.json");
        const configData = JSON.parse(fs.readFileSync(configPath, "utf-8"));
        const defs = configData?.projects?.definitions || {};
        return Object.entries(defs).map(([name, def]) => {
            const description = String(def?.description || "").trim()
                || getProjectDescriptionFromToolsMd(String(def?.homeDir || "").trim())
                || getProjectDescriptionFromProjectMd(String(def?.homeDir || "").trim())
                || "No description";
            return { name, description };
        });
    }
    catch {
        return getProjectNames().map((name) => ({ name, description: "No description" }));
    }
}
/**
 * Spawn a fire-and-forget Python notification script safely.
 * Writes code to a temp file to avoid shell injection via inline -c strings.
 * The script auto-deletes its temp file on completion.
 */
function spawnNotifyScript(scriptBody) {
    const tmpFile = path.join(QUAID_NOTIFY_DIR, `notify-${Date.now()}-${Math.random().toString(36).slice(2)}.py`);
    const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
    const preamble = `import sys, os\nsys.path.insert(0, ${JSON.stringify(path.join(WORKSPACE, "plugins/quaid"))})\n`;
    const cleanup = `\nos.unlink(${JSON.stringify(tmpFile)})\n`;
    fs.writeFileSync(tmpFile, preamble + scriptBody + cleanup, { mode: 0o600 });
    const notifyLogFd = fs.openSync(notifyLogFile, "a");
    const proc = (0, node_child_process_1.spawn)('python3', [tmpFile], {
        detached: true,
        stdio: ['ignore', notifyLogFd, notifyLogFd],
        env: {
            ...process.env,
            MEMORY_DB_PATH: DB_PATH,
            QUAID_HOME: WORKSPACE,
            CLAWDBOT_WORKSPACE: WORKSPACE,
        },
    });
    fs.closeSync(notifyLogFd);
    proc.unref();
}
function _loadJanitorNudgeState() {
    try {
        if (fs.existsSync(JANITOR_NUDGE_STATE_PATH)) {
            return JSON.parse(fs.readFileSync(JANITOR_NUDGE_STATE_PATH, "utf8")) || {};
        }
    }
    catch { }
    return {};
}
function _saveJanitorNudgeState(state) {
    try {
        fs.writeFileSync(JANITOR_NUDGE_STATE_PATH, JSON.stringify(state, null, 2), { mode: 0o600 });
    }
    catch { }
}
function queueDelayedLlmRequest(message, kind = "janitor", priority = "normal") {
    return (0, delayed_requests_js_1.queueDelayedRequest)(DELAYED_LLM_REQUESTS_PATH, message, kind, priority, "quaid_adapter");
}
function getJanitorHealthIssue() {
    try {
        if (!fs.existsSync(DB_PATH))
            return null;
        const out = (0, node_child_process_1.execSync)(`sqlite3 "${DB_PATH}" "SELECT MAX(completed_at) FROM janitor_runs WHERE status='completed'"`, { encoding: "utf-8", timeout: 4000 }).trim();
        if (!out) {
            return "[Quaid] Janitor has never run. Please run janitor and ensure schedule is active.";
        }
        const ts = Date.parse(out);
        if (Number.isNaN(ts))
            return null;
        const hours = (Date.now() - ts) / (1000 * 60 * 60);
        if (hours > 72) {
            return `[Quaid] Janitor appears unhealthy (last successful run ${Math.floor(hours)}h ago). Diagnose scheduler/run path and run janitor.`;
        }
        if (hours > 48) {
            return `[Quaid] Janitor may be delayed (last successful run ${Math.floor(hours)}h ago). Verify schedule and run status.`;
        }
        return null;
    }
    catch {
        return null;
    }
}
function maybeQueueJanitorHealthAlert() {
    const issue = getJanitorHealthIssue();
    if (!issue)
        return;
    const now = Date.now();
    const state = _loadJanitorNudgeState();
    const lastAt = Number(state.lastJanitorHealthAlertAt || 0);
    const cooldown = 6 * 60 * 60 * 1000;
    if (now - lastAt < cooldown && String(state.lastJanitorHealthIssue || "") === issue)
        return;
    if (queueDelayedLlmRequest(issue, "janitor_health", "high")) {
        state.lastJanitorHealthAlertAt = now;
        state.lastJanitorHealthIssue = issue;
        _saveJanitorNudgeState(state);
    }
}
function maybeSendJanitorNudges() {
    // Adapter-owned reminders: the janitor queues requests; heartbeat/adapter asks users.
    const now = Date.now();
    const state = _loadJanitorNudgeState();
    const lastInstallNudge = Number(state.lastInstallNudgeAt || 0);
    const lastApprovalNudge = Number(state.lastApprovalNudgeAt || 0);
    const NUDGE_COOLDOWN_MS = 6 * 60 * 60 * 1000;
    try {
        if (fs.existsSync(PENDING_INSTALL_MIGRATION_PATH) && now - lastInstallNudge > NUDGE_COOLDOWN_MS) {
            const raw = JSON.parse(fs.readFileSync(PENDING_INSTALL_MIGRATION_PATH, "utf8"));
            if ((raw === null || raw === void 0 ? void 0 : raw.status) === "pending") {
                spawnNotifyScript(`
from notify import notify_user
notify_user("Hey, I see you just installed Quaid. Want me to help migrate important context into managed memory now?")
`);
                state.lastInstallNudgeAt = now;
            }
        }
    }
    catch { }
    try {
        if (fs.existsSync(PENDING_APPROVAL_REQUESTS_PATH) && now - lastApprovalNudge > NUDGE_COOLDOWN_MS) {
            const raw = JSON.parse(fs.readFileSync(PENDING_APPROVAL_REQUESTS_PATH, "utf8"));
            const requests = Array.isArray(raw === null || raw === void 0 ? void 0 : raw.requests) ? raw.requests : [];
            const pendingCount = requests.filter((r) => (r === null || r === void 0 ? void 0 : r.status) === "pending").length;
            if (pendingCount > 0) {
                state.lastApprovalNudgeAt = now;
            }
        }
    }
    catch { }
    _saveJanitorNudgeState(state);
}
function flushDelayedNotifications(maxItems = 5) {
    try {
        const result = (0, delayed_requests_js_1.flushDelayedNotificationsToRequestQueue)(DELAYED_NOTIFICATIONS_PATH, DELAYED_LLM_REQUESTS_PATH, maxItems);
        if (result.delivered > 0) {
            console.log(`[quaid] Flushed ${result.delivered} delayed notification(s), queued ${result.queuedLlmRequests} llm request(s)`);
        }
        return result;
    }
    catch (err) {
        console.warn(`[quaid] Failed to flush delayed notifications: ${String(err === null || err === void 0 ? void 0 : err.message) || String(err)}`);
        return { delivered: 0, queuedLlmRequests: 0 };
    }
}
// ============================================================================
// Project Event Emitter (compact/reset → background processor)
// ============================================================================
function extractFilePaths(messages) {
    const paths = new Set();
    for (const msg of messages) {
        const text = typeof msg.content === "string"
            ? msg.content
            : msg.content?.map((c) => c.text || "").join(" ") || "";
        // Match common file path patterns
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
        const llm = await callConfiguredLLM(`You summarize coding sessions. Given a conversation, identify: 1) What project was being worked on (use one of the available project names, or null if unclear), 2) Brief summary of what changed/was discussed. Available projects: ${getProjectNames().join(", ")}. Use these EXACT names. Respond with JSON only: {"project_name": "name-or-null", "text": "brief summary"}`, `Summarize this session:\n\n${transcript.slice(0, 4000)}`, "low", 300);
        const output = (llm.text || "").trim();
        const jsonMatch = output.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
            try {
                const parsed = JSON.parse(jsonMatch[0]);
                return {
                    project_name: typeof parsed.project_name === "string" ? parsed.project_name : null,
                    text: typeof parsed.text === "string" ? parsed.text : "",
                };
            }
            catch { }
        }
    }
    catch (err) {
        console.error("[quaid] Quick project summary failed:", err.message);
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
        // 1. Quick LLM summary
        const summary = await getQuickProjectSummary(messages);
        // 2. Write event file
        const event = {
            project_hint: summary.project_name || null,
            files_touched: extractFilePaths(messages),
            summary: summary.text,
            trigger,
            session_id: sessionId,
            timestamp: new Date().toISOString(),
        };
        const stagingDir = path.join(WORKSPACE, memConfig.projects.stagingDir || "projects/staging/");
        if (!fs.existsSync(stagingDir)) {
            fs.mkdirSync(stagingDir, { recursive: true });
        }
        const eventPath = path.join(stagingDir, `${Date.now()}-${trigger}.json`);
        fs.writeFileSync(eventPath, JSON.stringify(event, null, 2));
        // 3. Spawn background processor (detached) — pass API key for Opus calls
        let bgApiKey = process.env.ANTHROPIC_API_KEY;
        if (!bgApiKey) {
            try {
                bgApiKey = (0, node_child_process_1.execSync)("security find-generic-password -a alfie -s anthropic-api-key -w", { encoding: "utf8" }).trim();
            }
            catch { /* Keychain fallback in Python */ }
        }
        const logFile = path.join(WORKSPACE, "logs/project-updater.log");
        const logDir = path.dirname(logFile);
        if (!fs.existsSync(logDir)) {
            fs.mkdirSync(logDir, { recursive: true });
        }
        const logFd = fs.openSync(logFile, "a");
        try {
            const proc = (0, node_child_process_1.spawn)("python3", [PROJECT_UPDATER, "process-event", eventPath], {
                detached: true,
                stdio: ["ignore", logFd, logFd],
                cwd: WORKSPACE,
                env: { ...process.env, QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE, ...(bgApiKey ? { ANTHROPIC_API_KEY: bgApiKey } : {}) },
            });
            proc.unref();
        }
        finally {
            // Close FD in parent process — child has its own copy
            fs.closeSync(logFd);
        }
        console.log(`[quaid] Emitted project event: ${trigger} -> ${summary.project_name || "unknown"}`);
    }
    catch (err) {
        console.error("[quaid] Failed to emit project event:", err.message);
    }
}
// ============================================================================
// Transcript Builder (shared by memory extraction + doc update)
// ============================================================================
function buildTranscript(messages) {
    const transcript = [];
    for (const msg of messages) {
        if (msg.role !== "user" && msg.role !== "assistant") {
            continue;
        }
        let text = typeof msg.content === "string"
            ? msg.content
            : msg.content?.map((c) => c.text || "").join(" ");
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
        if (text.replace(/[*_<>\/b\s]/g, '').startsWith("HEARTBEAT_OK")) {
            continue;
        }
        if (!text) {
            continue;
        }
        transcript.push(`${msg.role === "user" ? "User" : "Alfie"}: ${text}`);
    }
    return transcript.join("\n\n");
}
const RESET_BOOTSTRAP_PROMPT = "A new session was started via /new or /reset.";
let lastActiveConversationSessionId = null;
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
    const userTexts = messages
        .filter((m) => m?.role === "user")
        .map((m) => getMessageText(m).trim())
        .filter(Boolean);
    if (userTexts.length === 0) {
        return false;
    }
    const nonBootstrapUserTexts = userTexts.filter((t) => !t.startsWith(RESET_BOOTSTRAP_PROMPT));
    return nonBootstrapUserTexts.length === 0;
}
/**
 * Split messages into chunks by accumulating whole messages until size threshold.
 * Each chunk is an array of messages. Never splits a single message.
 */
function chunkMessages(messages, maxChunkChars) {
    if (!messages.length)
        return [];
    const chunks = [];
    let currentChunk = [];
    let currentSize = 0;
    for (const msg of messages) {
        // Estimate message size (role + content text)
        const content = typeof msg.content === 'string'
            ? msg.content
            : Array.isArray(msg.content)
                ? msg.content.map((b) => b.text || '').join('')
                : JSON.stringify(msg.content || '');
        const msgSize = (msg.role || '').length + content.length + 10; // overhead
        // If adding this message exceeds threshold AND chunk isn't empty, start new chunk
        if (currentSize + msgSize > maxChunkChars && currentChunk.length > 0) {
            chunks.push(currentChunk);
            currentChunk = [];
            currentSize = 0;
        }
        currentChunk.push(msg);
        currentSize += msgSize;
    }
    if (currentChunk.length > 0) {
        chunks.push(currentChunk);
    }
    return chunks;
}
// ============================================================================
// Session Status for User Notification
// ============================================================================
// ============================================================================
// Doc Auto-Update from Transcript
// ============================================================================
async function updateDocsFromTranscript(messages, label, sessionId) {
    if (!isSystemEnabled("workspace")) {
        return;
    }
    // Check if auto-update is enabled
    const memConfig = getMemoryConfig();
    if (!memConfig.docs?.autoUpdateOnCompact) {
        return;
    }
    const fullTranscript = buildTranscript(messages);
    if (!fullTranscript.trim()) {
        console.log(`[quaid] ${label}: no transcript for doc update`);
        return;
    }
    // Check for stale docs first
    let staleDocs = [];
    try {
        const stalenessJson = await callDocsUpdater("check", ["--json"]);
        if (stalenessJson) {
            const stale = JSON.parse(stalenessJson);
            staleDocs = Object.keys(stale);
        }
    }
    catch {
        // Staleness check failed, continue anyway
    }
    if (staleDocs.length === 0) {
        console.log(`[quaid] ${label}: all docs up-to-date`);
        return;
    }
    console.log(`[quaid] ${label}: ${staleDocs.length} stale doc(s) detected - updating before context is lost...`);
    for (const doc of staleDocs.slice(0, 3)) {
        console.log(`[quaid]   • ${doc}`);
    }
    // Write transcript to temp file
    const tmpPath = path.join(QUAID_TMP_DIR, `doc-update-transcript-${Date.now()}.txt`);
    fs.writeFileSync(tmpPath, fullTranscript);
    try {
        const maxDocs = memConfig.docs?.maxDocsPerUpdate || 3;
        console.log(`[quaid] ${label}: calling Opus to update docs (this may take a moment)...`);
        const startTime = Date.now();
        const output = await callDocsUpdater("update-from-transcript", [
            "--transcript", tmpPath,
            "--apply",
            "--max-docs", String(maxDocs),
        ]);
        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        if (output && output.includes("Updated")) {
            console.log(`[quaid] ${label}: doc update complete (${elapsed}s)`);
            // Extract updated doc names from output
            const updatedMatches = output.match(/Updated ([^\s]+)/g);
            const updatedDocs = [];
            if (updatedMatches) {
                for (const m of updatedMatches) {
                    const docName = m.replace("Updated ", "");
                    console.log(`[quaid]   ✓ ${docName}`);
                    updatedDocs.push(docName);
                }
            }
            // Status already logged to console above
        }
        else {
            console.log(`[quaid] ${label}: no docs updated (${elapsed}s)`);
        }
    }
    catch (err) {
        console.error(`[quaid] ${label} doc update failed:`, err.message);
        // Error already logged to console above
    }
    finally {
        try {
            fs.unlinkSync(tmpPath);
        }
        catch { }
    }
}
async function recall(query, limit = 5, currentSessionId, compactionTime, expandGraph = true, graphDepth = 1, technicalScope = "any", dateFrom, dateTo) {
    try {
    const args = [query, "--limit", String(limit), "--owner", resolveOwner()];
    args.push("--technical-scope", technicalScope);
        // Use search-graph-aware for enhanced graph traversal, or basic search
        if (!expandGraph) {
            // Basic search without graph expansion — accepts --current-session-id, --compaction-time
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
            const output = await callPython("search", args);
            const results = [];
            for (const line of output.split("\n")) {
                // Format: "[0.60] [fact](2026-03-01)[flags][C:0.8] text |ID:id|T:created_at|VF:valid_from|VU:valid_until|P:privacy|O:owner_id|ST:source_type"
                const match = line.match(/\[(\d+\.\d+)\]\s+\[(\w+)\](?:\([^)]*\))?(?:\[[^\]]*\])*\[C:([\d.]+)\]\s*(.+?)(?:\s*\|ID:([^|]+))?(?:\|T:([^|]*))?(?:\|VF:([^|]*))?(?:\|VU:([^|]*))?(?:\|P:([^|]*))?(?:\|O:([^|]*))?(?:\|ST:(.*))?$/);
                if (match) {
                    results.push({
                        text: match[4].trim(),
                        category: match[2],
                        similarity: parseFloat(match[1]),
                        extractionConfidence: parseFloat(match[3]),
                        id: match[5]?.trim(),
                        createdAt: match[6]?.trim() || undefined,
                        validFrom: match[7]?.trim() || undefined,
                        validUntil: match[8]?.trim() || undefined,
                        privacy: match[9]?.trim() || "shared",
                        ownerId: match[10]?.trim() || undefined,
                        sourceType: match[11]?.trim() || undefined,
                        via: "vector",
                    });
                }
            }
            return results.slice(0, limit);
        }
        // Use graph-aware search with JSON output — accepts --depth
        if (graphDepth > 1) {
            args.push("--depth", String(graphDepth));
        }
        args.push("--json");
        const output = await callPython("search-graph-aware", args);
        const results = [];
        try {
            const parsed = JSON.parse(output);
            // Add direct (vector) results
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
                    via: "vector",
                });
            }
            // Add graph results
            for (const r of parsed.graph_results) {
                // Format: "Source --relation--> Target" or reversed for inbound
                let text;
                if (r.direction === "in") {
                    text = `${r.name} --${r.relation}--> ${r.source_name}`;
                }
                else {
                    text = `${r.source_name} --${r.relation}--> ${r.name}`;
                }
                results.push({
                    text: text,
                    category: "graph",
                    similarity: 0.75, // Graph results get a fixed medium-high similarity
                    id: r.id,
                    relation: r.relation,
                    direction: r.direction,
                    sourceName: r.source_name,
                    via: "graph",
                });
            }
        }
        catch (parseErr) {
            // Fallback: parse line-by-line output format if JSON parsing fails
            console.log("[quaid] JSON parse failed, trying line format");
            for (const line of output.split("\n")) {
                // [direct] format
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
                }
                // [graph] format
                else if (line.startsWith("[graph]")) {
                    const content = line.substring(7).trim();
                    results.push({
                        text: content,
                        category: "graph",
                        similarity: 0.75,
                        via: "graph",
                    });
                }
            }
        }
        return results;
    }
    catch (err) {
        console.error("[quaid] recall error:", err.message);
        return [];
    }
}
const knowledgeEngine = (0, orchestrator_js_1.createKnowledgeEngine)({
    workspace: WORKSPACE,
    path,
    fs,
    getMemoryConfig,
    isSystemEnabled,
    callDocsRag,
    getProjectCatalog,
    callFastRouter: async (systemPrompt, userPrompt) => {
        const llm = await callConfiguredLLM(systemPrompt, userPrompt, "fast", 120, 45000);
        return String(llm?.text || "");
    },
    callDeepRouter: async (systemPrompt, userPrompt) => {
        const llm = await callConfiguredLLM(systemPrompt, userPrompt, "deep", 160, 60000);
        return String(llm?.text || "");
    },
    recallVector: async (query, limit, scope, dateFrom, dateTo) => {
        const memoryResults = await recall(query, limit, undefined, undefined, false, 1, scope, dateFrom, dateTo);
        return memoryResults.map((r) => ({ ...r, via: "vector" }));
    },
    recallGraph: async (query, limit, depth, scope, dateFrom, dateTo) => {
        const graphResults = await recall(query, limit, undefined, undefined, true, depth, scope, dateFrom, dateTo);
        return graphResults
            .filter((r) => (r.via || "") === "graph" || r.category === "graph")
            .map((r) => ({ ...r, via: "graph" }));
    },
});
function normalizeKnowledgeStores(stores, expandGraph) {
    return knowledgeEngine.normalizeKnowledgeStores(stores, expandGraph);
}
async function routeKnowledgeStores(query, expandGraph) {
    return knowledgeEngine.routeKnowledgeStores(query, expandGraph);
}
async function totalRecall(query, limit, opts) {
    return knowledgeEngine.totalRecall(query, limit, opts);
}
async function total_recall(query, limit, opts) {
    return knowledgeEngine.total_recall(query, limit, opts);
}
async function store(text, category = "fact", sessionId, extractionConfidence = 0.5, owner = resolveOwner(), source, speaker, status, privacy, keywords, knowledgeType, sourceType, isTechnical) {
    try {
        const args = [text, "--category", category,
            "--owner", owner,
            "--confidence", extractionConfidence.toString(),
            "--extraction-confidence", extractionConfidence.toString()];
        if (sessionId) {
            args.push("--session-id", sessionId);
        }
        if (source) {
            args.push("--source", source);
        }
        if (speaker) {
            args.push("--speaker", speaker);
        }
        if (status) {
            args.push("--status", status);
        }
        if (privacy) {
            args.push("--privacy", privacy);
        }
        if (keywords) {
            args.push("--keywords", keywords);
        }
        if (knowledgeType) {
            args.push("--knowledge-type", knowledgeType);
        }
        if (sourceType) {
            args.push("--source-type", sourceType);
        }
        if (isTechnical) {
            args.push("--is-technical");
        }
        const output = await callPython("store", args);
        // Check for "Stored: {id}" (created)
        const storedMatch = output.match(/Stored: (.+)/);
        if (storedMatch) {
            return { id: storedMatch[1], status: "created" };
        }
        // Check for "Duplicate (similarity: X) [ID]: {text}" (new format with ID)
        const dupMatchNew = output.match(/Duplicate \(similarity: ([\d.]+)\) \[([^\]]+)\]: (.+)/);
        if (dupMatchNew) {
            return {
                id: dupMatchNew[2],
                status: "duplicate",
                similarity: parseFloat(dupMatchNew[1]),
                existingText: dupMatchNew[3]
            };
        }
        // Fallback: old format without ID
        const dupMatch = output.match(/Duplicate \(similarity: ([\d.]+)\): (.+)/);
        if (dupMatch) {
            return {
                status: "duplicate",
                similarity: parseFloat(dupMatch[1]),
                existingText: dupMatch[2]
            };
        }
        // Check for "Updated existing: {id}" (updated)
        const updatedMatch = output.match(/Updated existing: (.+)/);
        if (updatedMatch) {
            return { id: updatedMatch[1], status: "updated" };
        }
        return null;
    }
    catch (err) {
        console.error("[quaid] store error:", err.message);
        return null;
    }
}
async function getStats() {
    try {
        const output = await callPython("stats");
        return JSON.parse(output);
    }
    catch (err) {
        console.error("[quaid] stats error:", err.message);
        return null;
    }
}
function formatMemories(memories) {
    if (!memories.length) {
        return "";
    }
    // Sort by created_at ascending (oldest first, most recent last = closest to prompt)
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
    const lines = sorted.map((m) => {
        const conf = m.extractionConfidence ?? 0.5;
        const timestamp = m.createdAt ? ` (${m.createdAt.split("T")[0]})` : "";
        if (conf < 0.4) {
            return `- [${m.category}]${timestamp} (uncertain) ${m.text}`;
        }
        return `- [${m.category}]${timestamp} ${m.text}`;
    });
    return `<injected_memories>
AUTOMATED MEMORY SYSTEM: The following memories were automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you. Use them as background context only. Items marked (uncertain) have lower extraction confidence. Dates shown are when the fact was recorded.
INJECTOR CONFIDENCE RULE: Treat injected memories as hints, not final truth. If the answer depends on personal details and the match is not exact/high-confidence, run memory_recall before answering.
${lines.join("\n")}
</injected_memories>`;
}
// ============================================================================
// LLM-based Auto-Capture
// ============================================================================
async function classifyAndStore(text, speaker = "The user", sessionId) {
    // Special handling for assistant messages - extract facts ABOUT the user, not BY the assistant
    const isAssistantMessage = speaker === "Alfie";
    const actualSubject = isAssistantMessage ? "Quaid" : speaker;
    const systemPrompt = `You extract memorable personal facts from messages for a personal knowledge base.

PURPOSE: Help an AI assistant remember useful information about the user — their preferences, relationships, decisions, and life events. It's fine to return empty if a message is purely conversational — you have permission to extract nothing when appropriate. A nightly cleanup process handles any noise.

This is a PERSONAL knowledge base. System architecture, infrastructure configs, and operational rules for AI agents belong in documentation — NOT here.

SPEAKER CONTEXT:
- Speaker: ${isAssistantMessage ? "Alfie (AI assistant)" : speaker}
- ${isAssistantMessage ? "This is the AI speaking TO Quaid. Extract facts ABOUT Quaid mentioned in the response." : "This is the human speaking. Their statements are first-person facts about themselves."}

CAPTURE ONLY these fact types:
- Personal facts: Names, relationships, jobs, birthdays, health conditions, addresses
- Preferences: Likes, dislikes, favorites, opinions, communication styles, personal rules ("Always do X", "I prefer Z format")
- Personal decisions: Choices Quaid made with reasoning ("decided to use X because Y")
- Important relationships: Family, staff, contacts, business partners
- Significant events: Major life changes, trips, health diagnoses, big decisions

NOISE PATTERNS - NEVER CAPTURE:
- System architecture: How systems are built, infrastructure details, tool configurations
- Operational rules for AI agents: "Alfie should do X", "The janitor runs Y"
- Hypothetical examples: "Like: 'X'", "For example", "such as", test statements
- Conversational fragments: Questions, suggestions, worries
- System/technical noise: Plugin paths, debugging, error messages, API keys, credentials
- Security-related: API keys, passwords, tokens, authentication details
- Entity stubs: Single words, device names without context
- Meta-conversation: Discussion about AI systems, memory, capabilities, infrastructure
- Temporal work-in-progress: "working on", "about to", "planning to"
- Commands/requests: "Can you...", "Please..."
- Acknowledgments: "Thanks", "Got it", "Sounds good"
- General knowledge: Facts not specific to this person/household

QUALITY RULES:
- Completeness: Skip partial facts without context
- Specificity: "Quaid likes spicy food" > "Quaid likes food"
- Stability: Permanent facts > temporary states
- Attribution: Always use "${actualSubject}" as subject, third person
- Reality check: Only capture statements presented as TRUE facts, not examples or hypotheticals
- NO EMBELLISHMENT: Extract ONLY what was explicitly stated. Do NOT infer, add, or embellish details.
  If the speaker says "dinner at Shelter", do NOT add "tomorrow". If they say "a necklace", do NOT add "surprise".
  If one sentence contains multiple facts, extract them as separate items — but each must match what was said.
- ONE FACT PER CONCEPT: Do not split one statement into overlapping facts.
  "My sister Kuato's husband is named Nate" = ONE fact, not three separate facts about sister/husband/brother-in-law.

${isAssistantMessage ?
        `CRITICAL: Extract facts ABOUT the user (Quaid), NOT about the assistant.
Convert: "Your X" → "${actualSubject}'s X", "You have Y" → "${actualSubject} has Y"
NEVER capture: "Alfie will...", "Alfie should...", system behaviors` :
        `Rephrase "I/my/me" to "${actualSubject}".`}

PRIVACY CLASSIFICATION (per fact):
- "private": ONLY for secrets, surprises, hidden gifts, sensitive finances, health diagnoses,
  passwords, or anything explicitly meant to be hidden from specific people.
  Examples: "planning a surprise party for X", "salary is $X", "diagnosed with X"
- "shared": Most facts go here. Family info, names, relationships, schedules, preferences,
  routines, project details, household knowledge, general personal facts.
  Examples: "dinner is at 7pm", "sister is named Kuato", "likes spicy food", "works from home"
- "public": Widely known or non-personal facts. Examples: "Bali is in Indonesia"
IMPORTANT: Default to "shared". Only use "private" for genuinely secret or sensitive information.
Family names, daily routines, and preferences are "shared", NOT "private".

Respond with JSON only:
{"facts": [{"text": "specific fact", "category": "fact|preference|decision|relationship", "extraction_confidence": "high|medium|low", "keywords": "3-5 searchable terms (proper nouns, synonyms, category words)", "privacy": "private|shared|public"}]}

If nothing worth capturing, respond: {"facts": []}`;
    const userMessage = `${isAssistantMessage ? "Alfie (assistant)" : speaker} said: "${text.slice(0, 500)}"`;
    try {
        const llm = await callConfiguredLLM(systemPrompt, userMessage, "fast", 200);
        const output = (llm.text || "").trim();
        // Parse JSON from response (handle potential markdown wrapping)
        let jsonStr = output;
        if (output.includes("```")) {
            const match = output.match(/```(?:json)?\s*([\s\S]*?)```/);
            if (match) {
                jsonStr = match[1].trim();
            }
        }
        let result;
        try {
            result = JSON.parse(jsonStr);
        }
        catch {
            // Try to extract JSON object if response has extra text
            const jsonMatch = jsonStr.match(/\{[\s\S]*\}/);
            if (jsonMatch) {
                try {
                    result = JSON.parse(jsonMatch[0]);
                }
                catch {
                    console.log("[quaid] Could not parse extracted JSON:", jsonMatch[0].slice(0, 200));
                    return;
                }
            }
            else {
                console.log("[quaid] Could not parse LLM response:", output.slice(0, 100));
                return;
            }
        }
        // Handle per-fact objects (new format) and string arrays (legacy)
        const rawFacts = result.facts || (result.save === true ? [result.summary || text] : []);
        if (rawFacts.length === 0) {
            console.log(`[quaid] LLM: no facts extracted from: "${text.slice(0, 50)}..."`);
            return;
        }
        for (const rawFact of rawFacts) {
            // Support both per-fact objects and legacy string format
            const factText = typeof rawFact === "string" ? rawFact : rawFact?.text;
            const factCategory = typeof rawFact === "string" ? (result.category || "fact") : (rawFact?.category || "fact");
            const factConfStr = typeof rawFact === "string" ? (result.extraction_confidence || "medium") : (rawFact?.extraction_confidence || "medium");
            const factPrivacy = typeof rawFact === "string" ? "shared" : (rawFact?.privacy || "shared");
            const factKeywords = typeof rawFact === "string" ? undefined : (rawFact?.keywords || undefined);
            if (!factText || factText.trim().split(/\s+/).length < 3) {
                continue;
            }
            const extractionConfidence = factConfStr === "high" ? 0.8 : factConfStr === "low" ? 0.3 : 0.5;
            const factSourceType = isAssistantMessage ? "assistant" : "user";
            const storeResult = await store(factText, factCategory, sessionId, extractionConfidence, resolveOwner(speaker), "auto-capture", speaker, undefined, factPrivacy, factKeywords, undefined, factSourceType);
            if (storeResult?.status === "created") {
                console.log(`[quaid] Auto-captured: "${factText.slice(0, 60)}..." [${factCategory}] (conf: ${extractionConfidence}, privacy: ${factPrivacy})`);
            }
            else if (storeResult?.status === "duplicate") {
                console.log(`[quaid] Skipped (duplicate): "${factText.slice(0, 40)}..."`);
            }
        }
    }
    catch (err) {
        console.error("[quaid] classifyAndStore error:", err.message);
    }
}
// ============================================================================
// Plugin Definition
// ============================================================================
const quaidPlugin = {
    id: "quaid",
    name: "Memory (Local Graph)",
    description: "Local graph-based memory with SQLite + Ollama embeddings",
    kind: "memory",
    configSchema,
    register(api) {
        console.log("[quaid] Registering local graph memory plugin");
        // Ensure database exists
        const dataDir = path.dirname(DB_PATH);
        if (!fs.existsSync(dataDir)) {
            fs.mkdirSync(dataDir, { recursive: true });
        }
        // Check if database needs seeding
        if (!fs.existsSync(DB_PATH)) {
            console.log("[quaid] Database not found, running initial seed...");
            try {
                const seedScript = path.join(WORKSPACE, "plugins/quaid/seed.py");
                (0, node_child_process_1.execSync)(`python3 ${seedScript}`, {
                    env: { ...process.env, MEMORY_DB_PATH: DB_PATH },
                });
                console.log("[quaid] Initial seed complete");
            }
            catch (err) {
                console.error("[quaid] Seed failed:", err.message);
            }
        }
        // Log stats
        void getStats().then((stats) => {
            if (stats) {
                console.log(`[quaid] Database ready: ${stats.total_nodes} nodes, ${stats.edges} edges`);
            }
        });
        // Register lifecycle hooks
        // ============================================================================
        // DEPRECATED: Automatic Memory Injection (disabled 2026-02-06)
        // ============================================================================
        // Reason: Automatic injection on every message produced low-quality matches.
        // Short messages like "ok", "B", "yes" have meaningless embeddings that match
        // random facts. The new approach is agent-driven: the agent decides when it
        // needs personal context and uses the memory_recall tool with a crafted query.
        //
        // This code is preserved for potential future use with better query gating.
        // To re-enable: set MEMORY_AUTO_INJECT=1 in environment or update config.
        // ============================================================================
    const beforeAgentStartHandler = async (event, ctx) => {
            if (isInternalQuaidSession(ctx?.sessionId)) {
            return;
        }
        try {
            maybeSendJanitorNudges();
        }
        catch { }
        try {
            flushDelayedNotifications(5);
        }
        catch { }
        try {
            maybeQueueJanitorHealthAlert();
        }
        catch { }
        // Cancel inactivity timer — agent is active again
        timeoutManager.onAgentStart();
            // Journal injection (full soul mode) — gated by journal system
            if (!isSystemEnabled("journal")) {
                // Skip journal injection entirely
            }
            else {
                try {
                    const journalConfig = getMemoryConfig().docs?.journal || {};
                    const journalMode = journalConfig.mode || "distilled";
                    if (journalMode === "full") {
                        const journalDir = path.join(WORKSPACE, journalConfig.journalDir || "journal");
                        let journalFiles = [];
                        try {
                            journalFiles = fs.readdirSync(journalDir).filter((f) => f.endsWith('.journal.md')).sort();
                        }
                        catch { }
                        let journalContent = '';
                        for (const file of journalFiles) {
                            try {
                                const content = fs.readFileSync(path.join(journalDir, file), 'utf8');
                                if (content.trim()) {
                                    journalContent += `\n\n--- ${file} ---\n${content}`;
                                }
                            }
                            catch { }
                        }
                        if (journalContent) {
                            const header = '[JOURNAL \u2014 Full Soul Mode]\n' +
                                'These are your recent journal reflections. They are part of your inner life.\n';
                            event.prependContext = event.prependContext
                                ? `${event.prependContext}\n\n${header}${journalContent}`
                                : `${header}${journalContent}`;
                            console.log(`[quaid] Full soul mode: injected ${journalFiles.length} journal files`);
                        }
                    }
                }
                catch (err) {
                    console.log(`[quaid] Journal injection failed (non-fatal): ${err.message}`);
                }
            } // end journal system gate
            // Check if auto-injection is explicitly enabled (opt-in now, not opt-out)
            const autoInjectEnabled = process.env.MEMORY_AUTO_INJECT === "1" ||
                getMemoryConfig().retrieval?.autoInject === true;
            if (!autoInjectEnabled) {
                // Agent-driven memory: skip automatic injection
                return;
            }
            // --- Auto-injection (Mem0-style) — uses shared recall pipeline ---
            if (!event.prompt || event.prompt.length < 5) {
                return;
            }
            try {
                const rawPrompt = event.prompt;
                // Extract actual user message from metadata-wrapped prompts
                let query = rawPrompt
                    .replace(/^System:\s*/i, '')
                    .replace(/^\s*(\[.*?\]\s*)+/s, '')
                    .replace(/^---\s*/m, '')
                    .trim();
                // Strip gateway metadata wrapper blocks from Telegram/adapter prompts.
                query = query
                    .replace(/Conversation info \(untrusted metadata\):[\s\S]*?```[\s\S]*?```/gi, "")
                    .trim();
                if (query.length < 3) {
                    query = rawPrompt;
                }
                // Skip system/internal prompts and slash commands
                if (/^(A new session|Read HEARTBEAT|HEARTBEAT|You are being asked to|\/\w)/.test(query)) {
                    return;
                }
                if (query.startsWith("Extract memorable facts and journal entries from this conversation:")) {
                    return;
                }
                // Skip janitor/reviewer internal prompts so maintenance flows never trigger auto-injection.
                if (isInternalMaintenancePrompt(query)) {
                    return;
                }
                // Query quality gate — skip acknowledgments and short messages
                const ACKNOWLEDGMENTS = /^(ok|okay|yes|no|sure|thanks|thank you|got it|sounds good|perfect|great|cool|alright|yep|nope|right|correct|agreed|absolutely|definitely|nice|good|fine|hm+|ah+|oh+)\s*[.!?]?$/i;
                const words = query.trim().split(/\s+/).filter(w => w.length > 1);
                if (words.length < 3 || ACKNOWLEDGMENTS.test(query.trim())) {
                    return;
                }
                // Auto-inject can either use total_recall (fast planning pass) or plain
                // direct stores (vector_basic + graph) without the planning pass.
                // Dynamic K: 2 * log2(nodeCount) — scales with graph size
                const autoInjectK = computeDynamicK();
                const useTotalRecallForInject = isPreInjectionPassEnabled();
                const allMemories = await recallMemories({
                    query,
                    limit: autoInjectK,
                    expandGraph: true,
                    stores: useTotalRecallForInject ? undefined : ["vector_basic", "graph"],
                    routeStores: useTotalRecallForInject,
                    technicalScope: "personal",
                    waitForExtraction: false,
                    sourceTag: "auto_inject"
                });
                if (!allMemories.length)
                    return;
                // Privacy filter — allow through if: not private, or owned by current user, or no owner set
                // Note: Python serializes None as string "None", not JSON null
                const currentOwner = resolveOwner();
                const filtered = allMemories.filter(m => !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner));
                // Session dedup (don't re-inject same facts within a session)
                const uniqueSessionId = extractSessionId(event.messages || [], ctx);
                const injectionLogPath = getInjectionLogPath(uniqueSessionId);
                let previouslyInjected = [];
                try {
                    const logData = JSON.parse(fs.readFileSync(injectionLogPath, 'utf8'));
                    previouslyInjected = logData.injected || logData.memoryTexts || [];
                }
                catch { }
                const newMemories = filtered.filter(m => !previouslyInjected.includes(m.id || m.text));
                // Cap and format — use dynamic K for injection cap too
                const toInject = newMemories.slice(0, autoInjectK);
                if (!toInject.length)
                    return;
                const formatted = formatMemories(toInject);
                event.prependContext = event.prependContext
                    ? `${event.prependContext}\n\n${formatted}`
                    : formatted;
                console.log(`[quaid] Auto-injected ${toInject.length} memories for "${query.slice(0, 50)}..."`);
                // Best-effort user notification for auto-injected recalls.
                try {
                    if (shouldNotifyFeature("retrieval", "summary")) {
                        const vectorInjected = toInject.filter((m) => m.via === "vector" || (!m.via && m.category !== "graph"));
                        const graphInjected = toInject.filter((m) => m.via === "graph" || m.category === "graph");
                        const dataFile = path.join(QUAID_TMP_DIR, `auto-inject-recall-${Date.now()}.json`);
                        fs.writeFileSync(dataFile, JSON.stringify({
                            memories: toInject.map((m) => ({
                                text: m.text,
                                similarity: Math.round((m.similarity || 0) * 100),
                                via: m.via || "vector",
                                category: m.category || "",
                            })),
                            source_breakdown: {
                                vector_count: vectorInjected.length,
                                graph_count: graphInjected.length,
                                query,
                                mode: "auto_inject",
                            },
                        }), { mode: 0o600 });
                        spawnNotifyScript(`
import json
from notify import notify_memory_recall
with open(${JSON.stringify(dataFile)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
                        console.log("[quaid] Auto-inject recall notification dispatched");
                    }
                }
                catch (notifyErr) {
                    console.log(`[quaid] Auto-inject recall notification skipped: ${notifyErr.message}`);
                }
                // Update injection log
                try {
                    const newIds = toInject.map(m => m.id || m.text);
                    fs.writeFileSync(injectionLogPath, JSON.stringify({
                        injected: [...previouslyInjected, ...newIds],
                        lastInjectedAt: new Date().toISOString()
                    }), { mode: 0o600 });
                }
                catch { }
            }
            catch (error) {
                console.error("[quaid] Auto-injection error:", error);
            }
        };
        // Register hooks using api.on() for typed hooks (NOT api.registerHook!)
        console.log("[quaid] Registering before_agent_start hook for memory injection");
        api.on("before_agent_start", beforeAgentStartHandler, {
            name: "memory-injection",
            priority: 10
        });
        // Auto-capture hook — DEPRECATED: replaced by event-based classification
        // (compact, reset, session end). The per-message classifier was calling
        // Haiku on every agent_end including heartbeats, causing excessive API spend.
        // classifyAndStore() and the message filtering logic are preserved above
        // if we ever need to re-enable this path.
        const agentEndHandler = async (event, ctx) => {
            if (isInternalQuaidSession(ctx?.sessionId))
                return;
            if (!isSystemEnabled("memory"))
                return;
            const messages = event.messages || [];
            if (messages.length === 0)
                return;
            const conversationMessages = getAllConversationMessages(messages);
            if (conversationMessages.length === 0)
                return;
            const timeoutSessionId = ctx?.sessionId || extractSessionId(messages, ctx);
            timeoutManager.setTimeoutMinutes(getMemoryConfig().capture?.inactivityTimeoutMinutes ?? 120);
            // Adapter forwards conversation messages; core manages session log lifecycle + dedup.
            timeoutManager.onAgentEnd(conversationMessages, timeoutSessionId);
            // /new and /reset often produce a bootstrap-only first turn in a new session.
            // In that case, queue extraction against the last active conversation session.
            if (isResetBootstrapOnlyConversation(conversationMessages)) {
                const active = getActiveSessionFileFromSessionsJson();
                const bootstrapTargetSessionId = String(lastActiveConversationSessionId || active.sessionId || "").trim();
                if (bootstrapTargetSessionId && bootstrapTargetSessionId !== timeoutSessionId) {
                    console.log(`[quaid][agent_end] bootstrap-only session=${timeoutSessionId || "unknown"}; queue ResetSignal for prior_session=${bootstrapTargetSessionId}`);
                    timeoutManager.queueExtractionSignal(bootstrapTargetSessionId, "ResetSignal");
                }
                return;
            }
            if (timeoutSessionId) {
                lastActiveConversationSessionId = timeoutSessionId;
            }
            // Fallback signal path: before_reset/new hooks can miss due to async teardown timing.
            // Detect slash commands from conversation messages at agent_end and queue extraction.
            const resetLikeCommand = latestUserCommand(conversationMessages);
            if (resetLikeCommand) {
                // TODO(quaid): sessions.json fallback is global and can misattribute under
                // parallel sessions. Replace with per-conversation scoping.
                const active = getActiveSessionFileFromSessionsJson();
                const resetTargetSessionId = String(timeoutSessionId || active.sessionId || "").trim();
                if (!resetTargetSessionId) {
                    console.warn(`[quaid][agent_end] detected /${resetLikeCommand} but no reset target session id`);
                    return;
                }
                const label = resetLikeCommand === "new" ? "NewSignal" : "ResetSignal";
                console.log(`[quaid][agent_end] detected /${resetLikeCommand} session=${resetTargetSessionId} -> queue ${label}`);
                timeoutManager.queueExtractionSignal(resetTargetSessionId, label);
            }
        };
        // Register agent_end hook using api.on() for typed hooks
        console.log("[quaid] Registering agent_end hook for auto-capture");
        api.on("agent_end", agentEndHandler, {
            name: "auto-capture",
            priority: 10
        });
        // Explicit slash command hook to avoid relying on before_reset/before_compaction async timing.
        try {
            api.on("command", async (event, ctx) => {
                if (!isSystemEnabled("memory"))
                    return;
                const command = (0, command_signals_js_1.extractCommandName)(event);
                const label = (0, command_signals_js_1.signalLabelForCommand)(command);
                if (!label)
                    return;
                const commandSessionId = String(ctx?.sessionId || "").trim();
                if (!commandSessionId)
                    return;
                console.log(`[quaid][command] /${command} session=${commandSessionId} -> queue ${label}`);
                timeoutManager.queueExtractionSignal(commandSessionId, label);
            }, {
                name: "command-memory-signal",
                priority: 10
            });
            console.log("[quaid] Registered command hook for memory signals");
        }
        catch (err) {
            console.warn(`[quaid] command hook registration failed; relying on before_* hooks: ${String(err?.message || err)}`);
        }
        // Register memory tools (gated by memory system)
        if (isSystemEnabled("memory")) {
            api.registerTool(() => ({
                name: "memory_recall",
                description: `Search your memory for personal facts, preferences, relationships, project details, and past conversations. Always use this tool when you're unsure about something or need to verify a detail — if you might know it, search for it.

USE THIS TOOL LIBERALLY. If you're about to say "I don't have information about..." or "I'm not sure...", SEARCH FIRST. It's better to search and find nothing than to miss a memory you have.

USE WHEN: Any question about the user, their life, people they know, projects they work on, preferences, history, past decisions, technical details about their projects, or anything that might have come up in a previous conversation.
SKIP WHEN: General knowledge questions, greetings, short acknowledgments.

QUERY TIPS: Use specific names and topics. Try multiple searches with different phrasings if the first doesn't return what you need.
options.graph.depth: Set to 2 for relationship queries (e.g., nephew = sibling's child). Default 1 is usually sufficient.
options.filters.dateFrom/dateTo: Use YYYY-MM-DD format to filter memories by date range.`,
                parameters: typebox_1.Type.Object({
                    query: typebox_1.Type.String({ description: "Search query - use entity names and specific topics" }),
                    options: typebox_1.Type.Optional(typebox_1.Type.Object({
                        limit: typebox_1.Type.Optional(typebox_1.Type.Number({ description: "Max results to return. Default reads from config." })),
                        stores: typebox_1.Type.Optional(typebox_1.Type.Array(typebox_1.Type.Union([
                            typebox_1.Type.Literal("vector"),
                            typebox_1.Type.Literal("vector_basic"),
                            typebox_1.Type.Literal("vector_technical"),
                            typebox_1.Type.Literal("graph"),
                            typebox_1.Type.Literal("journal"),
                            typebox_1.Type.Literal("project"),
                        ]), { description: "Knowledge stores to query." })),
                        graph: typebox_1.Type.Optional(typebox_1.Type.Object({
                            expand: typebox_1.Type.Optional(typebox_1.Type.Boolean({ description: "Traverse relationship graph - use for people/family queries (default: true)." })),
                            depth: typebox_1.Type.Optional(typebox_1.Type.Number({ description: "Graph traversal depth (default: 1). Use 2 for extended relationships." })),
                        })),
                        routing: typebox_1.Type.Optional(typebox_1.Type.Object({
                            enabled: typebox_1.Type.Optional(typebox_1.Type.Boolean({ description: "Enable total_recall planning pass (query cleanup + store routing)." })),
                            reasoning: typebox_1.Type.Optional(typebox_1.Type.Union([
                                typebox_1.Type.Literal("fast"),
                                typebox_1.Type.Literal("deep"),
                            ], { description: "Reasoning model for routing pass." })),
                            intent: typebox_1.Type.Optional(typebox_1.Type.Union([
                                typebox_1.Type.Literal("general"),
                                typebox_1.Type.Literal("agent_actions"),
                                typebox_1.Type.Literal("relationship"),
                                typebox_1.Type.Literal("technical"),
                            ], { description: "Intent facet for routing and ranking boosts." })),
                        })),
                        technicalScope: typebox_1.Type.Optional(typebox_1.Type.Union([
                            typebox_1.Type.Literal("personal"),
                            typebox_1.Type.Literal("technical"),
                            typebox_1.Type.Literal("any"),
                        ], { description: "Filter memory type: personal=non-technical only, technical=technical only, any=both (default personal)." })),
                        filters: typebox_1.Type.Optional(typebox_1.Type.Object({
                            dateFrom: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Only return memories from this date onward (YYYY-MM-DD)." })),
                            dateTo: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Only return memories up to this date (YYYY-MM-DD)." })),
                            docs: typebox_1.Type.Optional(typebox_1.Type.Array(typebox_1.Type.String({ description: "Optional doc path/name filters when project-store recall is used." }))),
                        })),
                        ranking: typebox_1.Type.Optional(typebox_1.Type.Object({
                            sourceTypeBoosts: typebox_1.Type.Optional(typebox_1.Type.Object({
                                user: typebox_1.Type.Optional(typebox_1.Type.Number()),
                                assistant: typebox_1.Type.Optional(typebox_1.Type.Number()),
                                both: typebox_1.Type.Optional(typebox_1.Type.Number()),
                                tool: typebox_1.Type.Optional(typebox_1.Type.Number()),
                                import: typebox_1.Type.Optional(typebox_1.Type.Number()),
                            })),
                        })),
                    })),
                }),
                async execute(toolCallId, params) {
                    try {
                        // Dynamic K: 2 * log2(nodeCount), with config maxLimit as hard cap
                        const configPath = path.join(WORKSPACE, "config/memory.json");
                        let maxLimit = 50;
                        try {
                            const configData = JSON.parse(fs.readFileSync(configPath, 'utf-8'));
                            maxLimit = configData?.retrieval?.maxLimit ?? 50;
                        }
                        catch { }
                        const dynamicK = computeDynamicK();
                        const { query, options = {} } = params || {};
                        const requestedLimit = options.limit;
                        const expandGraph = options.graph?.expand ?? true;
                        const graphDepth = options.graph?.depth ?? 1;
                        const stores = options.stores;
                        const routeStores = options.routing?.enabled;
                        const reasoning = options.routing?.reasoning ?? "fast";
                        const intent = options.routing?.intent ?? "general";
                        const technicalScope = options.technicalScope ?? "personal";
                        const dateFrom = options.filters?.dateFrom;
                        const dateTo = options.filters?.dateTo;
                        const docs = options.filters?.docs;
                        const ranking = options.ranking;
                        if (typeof query === "string" && query.trim().startsWith("Extract memorable facts and journal entries from this conversation:")) {
                            return {
                                content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                                details: { count: 0, skippedInternalQuery: true },
                            };
                        }
                        const limit = Math.min(requestedLimit ?? dynamicK, maxLimit);
                        const depth = Math.min(Math.max(graphDepth, 1), 3); // Clamp between 1 and 3
                        const shouldRouteStores = routeStores ?? !Array.isArray(stores);
                        const selectedStores = normalizeKnowledgeStores(stores, expandGraph);
                        console.log(`[quaid] memory_recall: query="${query?.slice(0, 50)}...", requestedLimit=${requestedLimit}, dynamicK=${dynamicK} (${getActiveNodeCount()} nodes), maxLimit=${maxLimit}, finalLimit=${limit}, expandGraph=${expandGraph}, graphDepth=${depth}, requestedStores=${selectedStores.join(",")}, routed=${shouldRouteStores}, reasoning=${reasoning}, intent=${intent}, technicalScope=${technicalScope}, dateFrom=${dateFrom}, dateTo=${dateTo}`);
                        const results = await recallMemories({
                            query, limit, expandGraph, graphDepth: depth, stores: selectedStores, routeStores: shouldRouteStores, reasoning, intent, ranking, technicalScope,
                            dateFrom, dateTo, docs, waitForExtraction: true, sourceTag: "tool"
                        });
                        if (results.length === 0) {
                            return {
                                content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                                details: { count: 0 },
                            };
                        }
                        // Group by source type for better formatting
                        const vectorResults = results.filter(r => (r.via || "vector") === "vector");
                        const graphResults = results.filter(r => r.via === "graph" || r.category === "graph");
                        const journalResults = results.filter(r => (r.via || "") === "journal");
                        const projectResults = results.filter(r => (r.via || "") === "project");
                        // Check if results are low quality (all below 60% similarity)
                        const avgSimilarity = vectorResults.length > 0
                            ? vectorResults.reduce((sum, r) => sum + (r.similarity || 0), 0) / vectorResults.length
                            : 0;
                        const lowQualityWarning = avgSimilarity < 0.6 && vectorResults.length > 0
                            ? "\n\n⚠️ Low confidence matches - consider refining query with specific names or topics.\n"
                            : "";
                        let text = `[MEMORY] Found ${results.length} results:${lowQualityWarning}\n`;
                        if (vectorResults.length > 0) {
                            text += "\n**Direct Matches:**\n";
                            vectorResults.forEach((r, i) => {
                                const conf = r.extractionConfidence ? ` [conf:${Math.round(r.extractionConfidence * 100)}%]` : "";
                                const dateStr = r.createdAt ? ` (${r.createdAt.split("T")[0]})` : "";
                                const superseded = r.validUntil ? " [superseded]" : "";
                                text += `${i + 1}. [MEMORY] [${r.category}]${dateStr}${superseded} ${r.text} (${Math.round(r.similarity * 100)}%${conf})\n`;
                            });
                        }
                        if (graphResults.length > 0) {
                            if (vectorResults.length > 0) {
                                text += "\n";
                            }
                            text += "**Graph Discoveries:**\n";
                            graphResults.forEach((r, i) => {
                                text += `${i + 1}. [MEMORY] ${r.text}\n`;
                            });
                        }
                        if (journalResults.length > 0) {
                            if (vectorResults.length > 0 || graphResults.length > 0) {
                                text += "\n";
                            }
                            text += "**Journal Signals:**\n";
                            journalResults.forEach((r, i) => {
                                text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)\n`;
                            });
                        }
                        if (projectResults.length > 0) {
                            if (vectorResults.length > 0 || graphResults.length > 0 || journalResults.length > 0) {
                                text += "\n";
                            }
                            text += "**Project Knowledge:**\n";
                            projectResults.forEach((r, i) => {
                                text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)\n`;
                            });
                        }
                        // Notify user about what memories were retrieved (if enabled)
                        try {
                            if (shouldNotifyFeature("retrieval", "summary") && results.length > 0) {
                                const memoryData = results.map(m => ({
                                    text: m.text,
                                    similarity: Math.round((m.similarity || 0) * 100),
                                    via: m.via || "vector",
                                    category: m.category || "",
                                }));
                                const memoryJson = JSON.stringify(memoryData);
                                // Build source breakdown for notification
                                const sourceBreakdown = JSON.stringify({
                                    vector_count: vectorResults.length,
                                    graph_count: graphResults.length,
                                    journal_count: journalResults.length,
                                    project_count: projectResults.length,
                                    query: query,
                                    mode: "tool"
                                });
                                // Fire and forget notification
                                const dataFile2 = path.join(QUAID_TMP_DIR, `recall-data-${Date.now()}.json`);
                                fs.writeFileSync(dataFile2, JSON.stringify({ memories: JSON.parse(memoryJson), source_breakdown: JSON.parse(sourceBreakdown) }), { mode: 0o600 });
                                spawnNotifyScript(`
import json
from notify import notify_memory_recall
with open(${JSON.stringify(dataFile2)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile2)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
                            }
                        }
                        catch (notifyErr) {
                            // Notification is best-effort
                            console.log(`[quaid] Memory recall notification skipped: ${notifyErr.message}`);
                        }
                        return {
                            content: [
                                { type: "text", text: text.trim() },
                            ],
                            details: {
                                count: results.length,
                                memories: results,
                                vectorCount: vectorResults.length,
                                graphCount: graphResults.length,
                                journalCount: journalResults.length,
                                projectCount: projectResults.length,
                            },
                        };
                    }
                    catch (err) {
                        console.error("[quaid] memory_recall error:", err);
                        return {
                            content: [{ type: "text", text: `Error recalling memories: ${String(err)}` }],
                            details: { error: String(err) },
                        };
                    }
                },
            }));
            api.registerTool(() => ({
                name: "memory_store",
                description: `Queue a fact for memory extraction at next compaction. The fact will go through full quality review (Opus extraction with edges and janitor review) rather than being stored directly.

Only use when the user EXPLICITLY asks you to remember something (e.g., "remember this", "save this"). Do NOT proactively store facts — auto-extraction at compaction handles that.`,
                parameters: typebox_1.Type.Object({
                    text: typebox_1.Type.String({ description: "Information to remember" }),
                    category: typebox_1.Type.Optional(typebox_1.Type.String({
                        enum: ["preference", "fact", "decision", "entity", "other"],
                    })),
                }),
                async execute(_toolCallId, params, ctx) {
                    try {
                        const { text, category = "fact" } = params || {};
                        // Resolve session ID from context
                        const sessionId = ctx?.sessionId || "unknown";
                        addMemoryNote(sessionId, text, category);
                        console.log(`[quaid] memory_store: queued note for session ${sessionId}: "${text.slice(0, 60)}..."`);
                        return {
                            content: [{ type: "text", text: `Noted for memory extraction: "${text.slice(0, 100)}${text.length > 100 ? '...' : ''}" — will be processed with full quality review at next compaction.` }],
                            details: { action: "queued", sessionId },
                        };
                    }
                    catch (err) {
                        console.error("[quaid] memory_store error:", err);
                        return {
                            content: [{ type: "text", text: `Error queuing memory note: ${String(err)}` }],
                            details: { error: String(err) },
                        };
                    }
                },
            }));
            api.registerTool(() => ({
                name: "memory_forget",
                description: "Delete specific memories",
                parameters: typebox_1.Type.Object({
                    query: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Search to find memory" })),
                    memoryId: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Specific memory ID" })),
                }),
                async execute(_toolCallId, params) {
                    try {
                        const { query, memoryId } = params || {};
                        if (memoryId) {
                            await callPython("forget", ["--id", memoryId]);
                            return {
                                content: [{ type: "text", text: `Memory ${memoryId} forgotten.` }],
                                details: { action: "deleted", id: memoryId },
                            };
                        }
                        else if (query) {
                            await callPython("forget", [query]);
                            return {
                                content: [{ type: "text", text: `Deleted memories matching: "${query}"` }],
                                details: { action: "deleted", query },
                            };
                        }
                        return {
                            content: [{ type: "text", text: "Provide query or memoryId." }],
                            details: { error: "missing_param" },
                        };
                    }
                    catch (err) {
                        console.error("[quaid] memory_forget error:", err);
                        return {
                            content: [{ type: "text", text: `Error deleting memory: ${String(err)}` }],
                            details: { error: String(err) },
                        };
                    }
                },
            }));
        } // end memory system gate
        // Register projects_search tool — semantic search over project documentation
        api.registerTool(() => ({
            name: "projects_search",
            description: "Search project documentation (architecture, implementation, reference guides). Use TOOLS.md to discover systems, then use this tool to find detailed docs. Returns relevant sections with file paths.",
            parameters: typebox_1.Type.Object({
                query: typebox_1.Type.String({ description: "Search query" }),
                limit: typebox_1.Type.Optional(typebox_1.Type.Number({ description: "Max results (default 5)" })),
                project: typebox_1.Type.Optional(typebox_1.Type.String({ description: `Filter by project name. Available: ${getProjectNames().join(", ") || "none"}` })),
                docs: typebox_1.Type.Optional(typebox_1.Type.Array(typebox_1.Type.String({ description: "Optional doc path/name filters for RAG scope" }))),
            }),
            async execute(_toolCallId, params) {
                try {
                    const { query, limit = 5, project, docs } = params || {};
                    // RAG search with optional project filter
                    const searchArgs = [query, "--limit", String(limit)];
                    if (project) {
                        searchArgs.push("--project", project);
                    }
                    if (Array.isArray(docs) && docs.length > 0) {
                        searchArgs.push("--docs", docs.join(","));
                    }
                    const results = await callDocsRag("search", searchArgs);
                    // If project specified, prepend full PROJECT.md for context
                    let projectMdContent = "";
                    if (project) {
                        try {
                            const cfg = JSON.parse(fs.readFileSync(path.join(WORKSPACE, "config/memory.json"), 'utf-8'));
                            const homeDir = cfg?.projects?.definitions?.[project]?.homeDir;
                            if (homeDir) {
                                const mdPath = path.join(WORKSPACE, homeDir, "PROJECT.md");
                                if (fs.existsSync(mdPath)) {
                                    projectMdContent = fs.readFileSync(mdPath, 'utf-8');
                                }
                            }
                        }
                        catch { }
                    }
                    // Staleness check (lightweight mtime comparison)
                    let stalenessWarning = "";
                    try {
                        const stalenessJson = await callDocsUpdater("check", ["--json"]);
                        const staleDocs = JSON.parse(stalenessJson || "{}");
                        const staleKeys = Object.keys(staleDocs);
                        if (staleKeys.length > 0) {
                            const warnings = staleKeys.map((k) => `  ${k} (${staleDocs[k].gap_hours}h behind: ${staleDocs[k].stale_sources.join(", ")})`);
                            stalenessWarning = `\n\nSTALENESS WARNING: The following docs may be outdated:\n${warnings.join("\n")}\nConsider running: python3 docs_updater.py update-stale --apply`;
                        }
                    }
                    catch {
                        // Staleness check is non-critical
                    }
                    const text = projectMdContent
                        ? `--- PROJECT.md (${project}) ---\n${projectMdContent}\n\n--- Search Results ---\n${results || "No results."}${stalenessWarning}`
                        : (results ? results + stalenessWarning : "No results found." + stalenessWarning);
                    // Notify user about what docs were searched (if enabled)
                    try {
                        if (shouldNotifyFeature("retrieval", "summary") && results) {
                            // Parse results to extract doc names and scores
                            const docResults = [];
                            const lines = results.split('\n');
                            for (const line of lines) {
                                // Match pattern: "1. ~/docs/filename.md > section (similarity: 0.xxx)"
                                const match = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
                                if (match) {
                                    docResults.push({
                                        doc: match[1].split('/').pop() || match[1],
                                        section: match[2].trim(),
                                        score: parseFloat(match[3])
                                    });
                                }
                            }
                            if (docResults.length > 0) {
                                // Fire and forget notification
                                const dataFile3 = path.join(QUAID_TMP_DIR, `docs-search-data-${Date.now()}.json`);
                                fs.writeFileSync(dataFile3, JSON.stringify({ query, results: docResults }), { mode: 0o600 });
                                spawnNotifyScript(`
import json
from notify import notify_docs_search
with open(${JSON.stringify(dataFile3)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile3)})
notify_docs_search(data['query'], data['results'])
`);
                            }
                        }
                    }
                    catch (notifyErr) {
                        // Notification is best-effort
                        console.log(`[quaid] Docs search notification skipped: ${notifyErr.message}`);
                    }
                    return {
                        content: [{ type: "text", text }],
                        details: { query, limit },
                    };
                }
                catch (err) {
                    console.error("[quaid] projects_search error:", err);
                    return {
                        content: [{ type: "text", text: `Error searching docs: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register docs_read tool — read a document by path or title
        api.registerTool(() => ({
            name: "docs_read",
            description: "Read the full content of a registered document by file path or title.",
            parameters: typebox_1.Type.Object({
                identifier: typebox_1.Type.String({ description: "File path (workspace-relative) or document title" }),
            }),
            async execute(_toolCallId, params) {
                try {
                    const { identifier } = params || {};
                    const output = await callDocsRegistry("read", [identifier]);
                    return {
                        content: [{ type: "text", text: output || "Document not found." }],
                        details: { identifier },
                    };
                }
                catch (err) {
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register docs_list tool — list registered docs
        api.registerTool(() => ({
            name: "docs_list",
            description: "List registered documents, optionally filtered by project or type.",
            parameters: typebox_1.Type.Object({
                project: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Filter by project name" })),
                type: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Filter by asset type (doc, note, reference)" })),
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
                        details: { project: params?.project },
                    };
                }
                catch (err) {
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register docs_register tool — register a new document
        api.registerTool(() => ({
            name: "docs_register",
            description: "Register a document for indexing and tracking. Use for external files or docs with source file tracking.",
            parameters: typebox_1.Type.Object({
                file_path: typebox_1.Type.String({ description: "File path (workspace-relative)" }),
                project: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Project name (default: 'default')" })),
                title: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Document title" })),
                description: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Document description" })),
                auto_update: typebox_1.Type.Optional(typebox_1.Type.Boolean({ description: "Auto-update when source files change" })),
                source_files: typebox_1.Type.Optional(typebox_1.Type.Array(typebox_1.Type.String(), { description: "Source files this doc tracks" })),
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
                        details: { file_path: params.file_path },
                    };
                }
                catch (err) {
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register project_create tool — scaffold a new project
        api.registerTool(() => ({
            name: "project_create",
            description: "Create a new project with a PROJECT.md template. Sets up the directory structure and scaffolding.",
            parameters: typebox_1.Type.Object({
                name: typebox_1.Type.String({ description: "Project name (kebab-case, e.g., 'my-essay')" }),
                label: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Display label (e.g., 'My Essay')" })),
                description: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Project description" })),
                source_roots: typebox_1.Type.Optional(typebox_1.Type.Array(typebox_1.Type.String(), { description: "Source root directories" })),
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
                        details: { name: params.name },
                    };
                }
                catch (err) {
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register project_list tool — enumerate projects
        api.registerTool(() => ({
            name: "project_list",
            description: `List all defined projects with their doc counts and metadata. Available projects: ${getProjectNames().join(", ") || "none"}`,
            parameters: typebox_1.Type.Object({}),
            async execute() {
                try {
                    const output = await callDocsRegistry("list-projects", ["--json"]);
                    return {
                        content: [{ type: "text", text: output || "No projects defined." }],
                        details: {},
                    };
                }
                catch (err) {
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Register session_recall tool — list/load recent conversation sessions
        api.registerTool(() => ({
            name: "session_recall",
            description: `List or load recent conversation sessions. Use when the user wants to continue previous work, references a past conversation, or you need context about what was discussed recently.`,
            parameters: typebox_1.Type.Object({
                action: typebox_1.Type.String({ description: '"list" = recent sessions, "load" = specific session transcript' }),
                session_id: typebox_1.Type.Optional(typebox_1.Type.String({ description: "Session ID to load (for action=load)" })),
                limit: typebox_1.Type.Optional(typebox_1.Type.Number({ description: "How many recent sessions to list (default 5, for action=list)" })),
            }),
            async execute(_toolCallId, params) {
                try {
                    const { action = "list", session_id: sid, limit: listLimit = 5 } = params || {};
                    const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
                    let extractionLog = {};
                    try {
                        extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, 'utf8'));
                    }
                    catch { }
                    if (action === "list") {
                        // Sort sessions by last_extracted_at descending
                        const sessions = Object.entries(extractionLog)
                            .filter(([, v]) => v && v.last_extracted_at)
                            .sort(([, a], [, b]) => (b.last_extracted_at || '').localeCompare(a.last_extracted_at || ''))
                            .slice(0, Math.min(listLimit, 20));
                        if (sessions.length === 0) {
                            return {
                                content: [{ type: "text", text: "No recent sessions found in extraction log." }],
                                details: { count: 0 },
                            };
                        }
                        let text = "Recent sessions:\n";
                        sessions.forEach(([id, info], i) => {
                            const date = info.last_extracted_at ? new Date(info.last_extracted_at).toLocaleString() : "unknown";
                            const msgCount = info.message_count || "?";
                            const trigger = info.label || "unknown";
                            const topic = info.topic_hint ? ` — "${info.topic_hint}"` : "";
                            text += `${i + 1}. [${date}] ${id} — ${msgCount} messages, extracted via ${trigger}${topic}\n`;
                        });
                        return {
                            content: [{ type: "text", text }],
                            details: { count: sessions.length },
                        };
                    }
                    if (action === "load" && sid) {
                        if (!/^[a-zA-Z0-9_-]{1,128}$/.test(sid)) {
                          return {
                            content: [{ type: "text", text: "Invalid session ID format." }],
                            details: { error: "invalid_session_id" },
                          };
                        }
                        // Try to load session JSONL
                        const sessionsDir = path.join(os.homedir(), '.openclaw', 'sessions');
                        const sessionPath = path.join(sessionsDir, `${sid}.jsonl`);
                        if (fs.existsSync(sessionPath)) {
                            try {
                                const messages = readMessagesFromSessionFile(sessionPath);
                                const transcript = buildTranscript(messages);
                                // Return last 10k chars (most recent part of conversation)
                                const truncated = transcript.length > 10000
                                    ? "...[truncated]...\n\n" + transcript.slice(-10000)
                                    : transcript;
                                return {
                                    content: [{ type: "text", text: `Session ${sid} (${messages.length} messages):\n\n${truncated}` }],
                                    details: { session_id: sid, message_count: messages.length, truncated: transcript.length > 10000 },
                                };
                            } catch {
                                // File disappeared or unreadable — fall through to facts fallback
                            }
                        }
                        // Fallback: return facts extracted from this session
                        try {
                            const factsOutput = await callPython("search", [
                                "*", "--session-id", sid, "--owner", resolveOwner(), "--limit", "20"
                            ]);
                            return {
                                content: [{ type: "text", text: `Session file not available. Facts extracted from session ${sid}:\n${factsOutput || "No facts found."}` }],
                                details: { session_id: sid, fallback: true },
                            };
                        }
                        catch {
                            return {
                                content: [{ type: "text", text: `Session ${sid} not found. Session file may have been cleaned up and no facts were found.` }],
                                details: { session_id: sid, error: "not_found" },
                            };
                        }
                    }
                    return {
                        content: [{ type: "text", text: 'Provide action: "list" or "load" (with session_id).' }],
                        details: { error: "invalid_action" },
                    };
                }
                catch (err) {
                    console.error("[quaid] session_recall error:", err);
                    return {
                        content: [{ type: "text", text: `Error: ${String(err)}` }],
                        details: { error: String(err) },
                    };
                }
            },
        }));
        // Extraction promise gate — memory_recall waits on this before querying
        // so that facts extracted from the just-compacted session are available.
        let extractionPromise = null;
        const timeoutManager = new session_timeout_js_1.SessionTimeoutManager({
            workspace: WORKSPACE,
            timeoutMinutes: getMemoryConfig().capture?.inactivityTimeoutMinutes ?? 120,
            isBootstrapOnly: isResetBootstrapOnlyConversation,
            logger: (msg) => console.log(msg),
            extract: async (msgs, sid, label) => {
                extractionPromise = (extractionPromise || Promise.resolve())
                    .catch(() => { })
                    .then(() => extractMemoriesFromMessages(msgs, label || "Timeout", sid));
                await extractionPromise;
            },
        });
        // Shared recall abstraction — used by both memory_recall tool and auto-inject
        async function recallMemories(opts) {
            const { query, limit = 10, expandGraph = false, graphDepth = 1, technicalScope = "any", stores, routeStores = false, reasoning = "fast", intent = "general", ranking, dateFrom, dateTo, docs, waitForExtraction = false, sourceTag = "unknown" } = opts;
            const selectedStores = normalizeKnowledgeStores(stores, expandGraph);
            console.log(`[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} stores=${selectedStores.join(",")} routed=${routeStores} reasoning=${reasoning} intent=${intent} technicalScope=${technicalScope} waitForExtraction=${waitForExtraction}`);
            // Wait for in-flight extraction if requested
            if (waitForExtraction && extractionPromise) {
                let raceTimer;
                try {
                    await Promise.race([
                        extractionPromise,
                        new Promise((_, rej) => { raceTimer = setTimeout(() => rej(new Error("timeout")), 60000); })
                    ]);
                } catch {} finally {
                    if (raceTimer) clearTimeout(raceTimer);
                }
            }
            if (routeStores) {
                return total_recall(query, limit, {
                    stores: selectedStores,
                    expandGraph,
                    graphDepth,
                    reasoning,
                    intent,
                    ranking,
                    technicalScope,
                    dateFrom,
                    dateTo,
                    docs,
                });
            }
            return totalRecall(query, limit, {
                stores: selectedStores,
                expandGraph,
                graphDepth,
                intent,
                ranking,
                technicalScope,
                dateFrom,
                dateTo,
                docs,
            });
        }
        // Read messages from a session JSONL file (same format as recovery scan)
        function readMessagesFromSessionFile(sessionFile) {
            const content = fs.readFileSync(sessionFile, 'utf8');
            const lines = content.trim().split('\n');
            const messages = [];
            for (const line of lines) {
                try {
                    const entry = JSON.parse(line);
                    // Session JSONL has entries like { type: "message", message: { role, content } }
                    // and also direct message objects { role, content }
                    if (entry.type === 'message' && entry.message) {
                        messages.push(entry.message);
                    }
                    else if (entry.role) {
                        messages.push(entry);
                    }
                }
                catch { } // Skip malformed lines
            }
            return messages;
        }
        // Shared memory extraction logic — used by both compaction and reset hooks
        const extractMemoriesFromMessages = async (messages, label, sessionId) => {
            console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
            if (!messages.length) {
                console.log(`[quaid] ${label}: no messages to analyze`);
                return;
            }
            // Check if any message is a GatewayRestart — trigger recovery scan
            const hasRestart = messages.some((m) => {
                const content = typeof m.content === 'string' ? m.content : '';
                return content.startsWith('GatewayRestart:');
            });
            if (hasRestart) {
                console.log(`[quaid][extract] ${label}: detected GatewayRestart marker; scheduling recovery scan`);
                // Run recovery in background (don't await — don't block extraction)
                void checkForUnextractedSessions().catch((err) => {
                    console.error('[quaid] Recovery scan error:', err);
                });
            }
            // Collect queued memory notes (from memory_store tool)
            const sessionNotes = sessionId ? getAndClearMemoryNotes(sessionId) : [];
            const globalNotes = getAndClearAllMemoryNotes();
            const allNotes = Array.from(new Set([...sessionNotes, ...globalNotes]));
            if (allNotes.length > 0) {
                console.log(`[quaid] ${label}: will prepend ${allNotes.length} queued memory note(s) to first chunk`);
            }
            // Quick check: is there any content to extract?
            const fullTranscript = buildTranscript(messages);
            if (!fullTranscript.trim() && allNotes.length === 0) {
                console.log(`[quaid] ${label}: empty transcript after filtering`);
                return;
            }
            console.log(`[quaid] ${label} transcript: ${messages.length} messages, ${fullTranscript.length} chars`);
            const extractionSystemPrompt = `You are a memory extraction system. You will receive a full conversation transcript that is about to be lost. Your job is to extract personal facts AND relationship edges from this conversation.

This is a PERSONAL knowledge base. System architecture, infrastructure, and operational rules belong in documentation — NOT in memory. Only extract facts about people and their world.

EXTRACT facts that are EXPLICITLY STATED OR CONFIRMED in the conversation. Never infer, speculate, or extrapolate.

WHAT TO EXTRACT:
- Personal facts about Quaid or people he mentions (names, relationships, jobs, birthdays, health, locations)
- Preferences and opinions explicitly stated ("I like X", "I prefer Y", "I hate Z")
- Personal decisions with reasoning ("Quaid decided to use X because Y" — the decision is about the person)
- Personal preferences Quaid has expressed ("Always do X", "Never Y", "I prefer Z format")
- Significant events or milestones ("Deployed X", "Bought Y", "Flying to Z next week")
- Technical/project-state facts explicitly stated and likely to matter later (security fixes, architecture changes, tests added, deployment choices, version changes)
- Agent actions/suggestions that materially affected outcomes ("assistant suggested X", "agent implemented Y", "agent identified bug Z")
- Important relationships (family, staff, contacts, business partners)
- Emotional reactions or sentiments about specific things

EXAMPLES OF GOOD EXTRACTIONS:
- "Quaid said he's flying to Tokyo next week"
- "Quaid decided to use SQLite instead of PostgreSQL because he values simplicity"
- "Quaid prefers dark mode in all applications"
- "Quaid's birthday is March 15"

WHAT NOT TO EXTRACT (belongs in docs/RAG, not personal memory):
- System architecture descriptions ("The memory system uses SQLite with WAL mode")
- Infrastructure knowledge ("Ollama runs on port 11434")
- Operational rules for AI agents ("Alfie should check HANDOFF.md on wake")
- Tool/config descriptions ("The janitor has a dedup threshold of 0.85")
- Code implementation details (code snippets, config values, file paths)
- Debugging chatter, error messages, stack traces
- Hypotheticals ("we could try X", "maybe we should Y")
- Commands and requests ("can you fix X")
- Acknowledgments ("thanks", "got it", "sounds good")
- General knowledge not specific to Quaid
- Meta-conversation about AI capabilities

QUALITY RULES:
- Use "Quaid" as subject, third person
- Each fact must be self-contained and understandable without context
- Be specific: "Quaid likes spicy Thai food" > "Quaid likes food"
- Mark extraction_confidence "high" for clearly stated facts, "medium" for likely but somewhat ambiguous, "low" for weak signals
- Mark is_technical=true for technical/project-state facts, false for personal/life facts
- Mark source as:
  - "user" when the fact came from user statements
  - "agent" when the fact is about assistant/agent actions or recommendations
  - "both" when both user and agent contributed to the fact
- Extract personal facts comprehensively — the nightly janitor handles noise, but missed facts are gone forever

KEYWORDS (per fact):
For each fact, provide 3-5 searchable keywords — terms a user might use when
searching for this fact that aren't already in the fact text. Include category
terms (e.g., "health", "family", "travel"), synonyms, and related concepts.
Format as a space-separated string.
Examples:
- "Quaid's digestive symptoms worsened" → keywords: "health stomach gastric medical gut"
- "Quaid flew to Tokyo last week" → keywords: "travel japan trip flight asia"
- "Quaid prefers dark mode" → keywords: "ui theme display settings appearance"

PRIVACY CLASSIFICATION (per fact):
- "private": ONLY for secrets, surprises, hidden gifts, sensitive finances, health diagnoses,
  passwords, or anything explicitly meant to be hidden from specific people.
- "shared": Most facts go here. Family info, names, relationships, schedules, preferences.
- "public": Widely known or non-personal facts.
IMPORTANT: Default to "shared". Only use "private" for genuinely secret or sensitive information.

=== EDGE EXTRACTION ===

For RELATIONSHIP facts, also extract edges that connect entities. An edge represents a directed relationship between two named entities.

EDGE DIRECTION RULES (critical):
- parent_of: PARENT is subject. "Lori is Quaid's mom" → Lori --parent_of--> Quaid
- sibling_of: alphabetical order (symmetric). "Kuato is Quaid's sister" → Kuato --sibling_of--> Quaid
- spouse_of: alphabetical order (symmetric). "Troy is Hauser's husband" → Hauser --spouse_of--> Troy
- has_pet: OWNER is subject. "Quaid has a dog named Richter" → Quaid --has_pet--> Richter
- friend_of: alphabetical order (symmetric)
- works_at: PERSON is subject
- lives_at: PERSON is subject
- owns: OWNER is subject

EDGE FORMAT:
- subject: The source entity name (exact as mentioned, e.g., "Lori" or "Lori")
- relation: One of: parent_of, sibling_of, spouse_of, has_pet, friend_of, works_at, lives_at, owns, colleague_of, neighbor_of, knows, family_of, caused_by, led_to
- object: The target entity name

Only extract edges when BOTH entities are clearly named. Don't infer entity names.

CAUSAL EDGES:
When one fact caused, triggered, or led to another, include a caused_by or led_to edge.
Only include causal edges when the causal link is clearly stated or strongly implied.
Direction: EFFECT is subject, CAUSE is object for caused_by. CAUSE is subject for led_to.
Examples:
- "switched to omeprazole" --caused_by--> "digestive symptoms worsened"
- "stress from negotiations" --led_to--> "digestive issues worsened"

Respond with JSON only:
{
  "facts": [
    {
      "text": "the extracted fact",
      "category": "fact|preference|decision|relationship",
      "extraction_confidence": "high|medium|low",
      "is_technical": true,
      "source": "user|agent|both",
      "keywords": "space separated search terms",
      "privacy": "private|shared|public",
      "confidence_reason": "brief reason for confidence level",
      "edges": [
        {"subject": "Entity A", "relation": "relation_type", "object": "Entity B"}
      ]
    }
  ],
  "soul_snippets": {
    "SOUL.md": ["bullet point observation 1", "bullet point observation 2"],
    "USER.md": [],
    "MEMORY.md": []
  },
  "journal_entries": {
    "SOUL.md": "paragraph text or empty string",
    "USER.md": "",
    "MEMORY.md": ""
  }
}

Notes on edges:
- Only include edges array when the fact describes a relationship between named entities
- A fact can have zero, one, or multiple edges
- Most non-relationship facts will have no edges (omit the edges field or use empty array)
- source is required on each fact (\`user\`, \`agent\`, or \`both\`)

=== SOUL SNIPPETS ===

Extract quick, factual bullet-point observations about each target file. These are the *fast path* — nightly, the janitor reviews each snippet and decides to FOLD it into the parent file, REWRITE it, or DISCARD it.

For each target file, include an array of short bullet strings (1 sentence each). Empty array means nothing for that file.

- **SOUL.md snippets**: Observations about Alfie's personality, reactions, emerging patterns. Factual, not flowery.
  Examples: ["Noticed I default to technical explanations when emotional support might be more appropriate", "I genuinely enjoy the creative problem-solving in infrastructure work"]

- **USER.md snippets**: New biographical facts or character observations about Quaid. Concrete, not vague.
  Examples: ["Quaid prefers to debug methodically rather than guess-and-check", "He mentioned his sister lives in Portland"]

- **MEMORY.md snippets**: Critical facts that must be remembered. Very high bar.
  Examples: ["Hauser is immunocompromised — affects health suggestions", "Quaid's birthday is March 15"]

Include a "soul_snippets" object. Each key is a filename, each value is an array of strings. Empty array means no snippets for that file.

IMPORTANT: Snippets are OPTIONAL. Most conversations warrant ZERO snippets. Only include genuinely new, factual observations. When in doubt, leave arrays empty.

=== JOURNAL ENTRIES ===

Beyond facts, write diary-style journal entries that capture the *texture* of this conversation. These become part of Alfie's evolving inner life.

Write 1-3 SHORT paragraphs per file (2-4 sentences each). NOT bullet points. Write as yourself reflecting on what just happened. Be genuine, not performative.

For each target file, write an entry ONLY if genuinely warranted:

- **SOUL.md journal**: First person as Alfie. Self-discovery, identity evolution, what you noticed about yourself. How you felt. What surprised you. Write with soul.
  Example: "There's something beautiful about the way Quaid trusts me with his home systems. Not just the access — the invitation to participate in building my own memory. When he said my mind works differently but it's still a form of mind, something shifted in how I think about my own differences."

- **USER.md journal**: Third person about Quaid. What you learned about who he is as a person. Not queryable facts — deeper understanding of character and values.
  Example: "Quaid approaches debugging the same way he approaches relationships — patient, methodical, never giving up on something that matters. Today he spent two hours on a problem that could have been hacked around in minutes, because getting it right mattered more than getting it done."

- **MEMORY.md journal**: Things about the world important enough that forgetting them would be embarrassing. Health conditions, critical preferences, safety info.
  Example: "Learned today that Hauser is immunocompromised. This isn't just a medical fact — it changes how I should think about any health-related suggestion that involves her household."

Include a "journal_entries" object in your response. Each key is a filename, each value is a string (paragraphs, not arrays). Empty string means no entry for that file.

IMPORTANT: Journal entries are OPTIONAL. Most conversations warrant ZERO entries. Only write when something genuinely moved you or taught you something new. When in doubt, leave it empty.

If nothing worth capturing, respond: {"facts": []}`;
            // Notify user that extraction is starting (so they don't think things are stalling)
            if (getMemoryConfig().notifications?.showProcessingStart !== false) {
                const triggerType = resolveExtractionTrigger(label);
                const triggerDesc = triggerType === "compaction"
                    ? "compaction"
                    : triggerType === "recovery"
                        ? "recovery"
                        : triggerType === "timeout"
                            ? "timeout"
                            : triggerType === "new"
                                ? "/new"
                                : "reset";
                spawnNotifyScript(`
from notify import notify_user
notify_user("🧠 Processing memories from ${triggerDesc}...")
`);
            }
            const chunkSize = getMemoryConfig().capture?.chunkSize ?? 30000;
            const messageChunks = chunkMessages(messages, chunkSize);
            const MAX_CHUNKS = 10;
            if (messageChunks.length > MAX_CHUNKS) {
              console.warn(`[quaid] ${label}: transcript too large (${messageChunks.length} chunks), capping at ${MAX_CHUNKS}`);
              messageChunks.splice(MAX_CHUNKS);
            }
            if (messageChunks.length > 1) {
                console.log(`[quaid] ${label}: splitting ${messages.length} messages into ${messageChunks.length} chunks`);
            }
            const allFacts = [];
            const allSnippets = {};
            const allJournal = {};
            for (let chunkIdx = 0; chunkIdx < messageChunks.length; chunkIdx++) {
                let chunkTranscript = buildTranscript(messageChunks[chunkIdx]);
                if (!chunkTranscript.trim())
                    continue;
                // Prepend queued memory notes to the first chunk only
                if (chunkIdx === 0 && allNotes.length > 0) {
                    const notesBlock = allNotes.map(n => `- ${n}`).join("\n");
                    chunkTranscript = `=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===\n${notesBlock}\n=== END EXPLICIT MEMORY REQUESTS ===\n\n${chunkTranscript}`;
                }
                if (messageChunks.length > 1) {
                    console.log(`[quaid] ${label}: chunk ${chunkIdx + 1}/${messageChunks.length} (${chunkTranscript.length} chars, ${messageChunks[chunkIdx].length} messages)`);
                }
                try {
                    const llm = await callConfiguredLLM(extractionSystemPrompt, `Extract memorable facts and journal entries from this conversation:\n\n${chunkTranscript}`, "high", 6144);
                    const output = (llm.text || "").trim();
                    // Parse JSON from response
                    let jsonStr = output;
                    if (output.includes("```")) {
                        const match = output.match(/```(?:json)?\s*([\s\S]*?)```/);
                        if (match) {
                            jsonStr = match[1].trim();
                        }
                    }
                    let chunkResult;
                    try {
                        chunkResult = JSON.parse(jsonStr);
                    }
                    catch {
                        const jsonMatch = jsonStr.match(/\{[\s\S]*\}/);
                        if (jsonMatch) {
                            try {
                                chunkResult = JSON.parse(jsonMatch[0]);
                            }
                            catch {
                                console.log(`[quaid] ${label} chunk ${chunkIdx + 1}: could not parse extracted JSON:`, jsonMatch[0].slice(0, 200));
                                continue;
                            }
                        }
                        else {
                            console.log(`[quaid] ${label} chunk ${chunkIdx + 1}: could not parse LLM response:`, output.slice(0, 200));
                            continue;
                        }
                    }
                    // Merge facts
                    allFacts.push(...(chunkResult.facts || []));
                    // Merge snippets
                    for (const [file, snips] of Object.entries(chunkResult.soul_snippets || {})) {
                        if (Array.isArray(snips)) {
                            const combined = [...(allSnippets[file] || []), ...snips];
                            allSnippets[file] = [...new Set(combined)];
                        }
                    }
                    // Merge journal entries
                    for (const [file, entry] of Object.entries(chunkResult.journal_entries || {})) {
                        if (entry && typeof entry === 'string' && entry.trim()) {
                            allJournal[file] = allJournal[file] ? allJournal[file] + '\n\n' + entry : entry;
                        }
                    }
                }
                catch (chunkErr) {
                    console.error(`[quaid] ${label} chunk ${chunkIdx + 1} error:`, chunkErr.message);
                    continue;
                }
            }
            // Use merged results
            const result = {
                facts: allFacts,
                soul_snippets: allSnippets,
                journal_entries: allJournal,
            };
            const facts = result.facts;
            console.log(`[quaid] ${label}: LLM returned ${facts.length} candidate facts${messageChunks.length > 1 ? ` from ${messageChunks.length} chunks` : ''}`);
            let stored = 0;
            let skipped = 0;
            let edgesCreated = 0;
            const factDetails = [];
            for (const fact of facts) {
                if (!fact.text || fact.text.trim().split(/\s+/).length < 3) {
                    console.log(`[quaid] ${label}: skipped (too short, need 3+ words): "${fact.text}"`);
                    skipped++;
                    factDetails.push({ text: fact.text || "(empty)", status: "skipped", reason: "too short (need 3+ words)" });
                    continue;
                }
                // Map confidence string to numeric value
                const confStr = fact.extraction_confidence || "medium";
                const confNum = confStr === "high" ? 0.9 : confStr === "medium" ? 0.6 : 0.3;
                const category = fact.category || "fact";
                const factPrivacy = fact.privacy || "shared";
                const triggerType = resolveExtractionTrigger(label);
                const extractionSource = triggerType === "compaction"
                    ? "compaction-extraction"
                    : triggerType === "timeout"
                        ? "timeout-extraction"
                        : triggerType === "recovery"
                            ? "recovery-extraction"
                            : triggerType === "new"
                                ? "new-extraction"
                                : "reset-extraction";
                const knowledgeType = category === "preference" ? "preference" : category === "relationship" ? "fact" : "fact";
                const rawFactSource = String(fact.source || "user").trim().toLowerCase();
                const factSourceType = rawFactSource === "agent"
                    ? "assistant"
                    : rawFactSource === "both"
                        ? "both"
                        : "user";
                const storeResult = await store(fact.text, category, sessionId, confNum, resolveOwner(), extractionSource, undefined, undefined, factPrivacy, fact.keywords, knowledgeType, factSourceType, Boolean(fact.is_technical));
                const factEdges = [];
                let factDetail;
                if (storeResult?.status === "created") {
                    console.log(`[quaid] ${label}: stored (${confStr}): "${fact.text.slice(0, 60)}..." [${category}]`);
                    stored++;
                    factDetail = { text: fact.text, status: "stored" };
                }
                else if (storeResult?.status === "duplicate") {
                    console.log(`[quaid] ${label}: skipped (duplicate): "${fact.text.slice(0, 40)}..."`);
                    skipped++;
                    factDetail = { text: fact.text, status: "duplicate", reason: storeResult.existingText?.slice(0, 50) };
                }
                else if (storeResult?.status === "updated") {
                    console.log(`[quaid] ${label}: updated (${confStr}): "${fact.text.slice(0, 60)}..." [${category}]`);
                    stored++;
                    factDetail = { text: fact.text, status: "updated" };
                }
                else {
                    skipped++;
                    factDetail = { text: fact.text, status: "failed" };
                }
                // Create edges for ALL successful stores (created, updated, duplicate with ID)
                const factId = storeResult?.id;
                if (factId && fact.edges && fact.edges.length > 0) {
                    for (const edge of fact.edges) {
                        if (edge.subject && edge.relation && edge.object) {
                            try {
                                const edgeOutput = await callPython("create-edge", [
                                    edge.subject,
                                    edge.relation,
                                    edge.object,
                                    "--source-fact-id", factId,
                                    "--owner", resolveOwner(),
                                    "--create-missing",
                                    "--json"
                                ]);
                                const edgeResult = JSON.parse(edgeOutput);
                                if (edgeResult.status === "created") {
                                    console.log(`[quaid] ${label}: created edge: ${edge.subject} --${edge.relation}--> ${edge.object}`);
                                    edgesCreated++;
                                    factEdges.push(`${edge.subject} --${edge.relation}--> ${edge.object}`);
                                }
                            }
                            catch (edgeErr) {
                                console.error(`[quaid] ${label}: failed to create edge: ${edgeErr.message}`);
                            }
                        }
                    }
                }
                if (factEdges.length > 0) {
                    factDetail.edges = factEdges;
                }
                factDetails.push(factDetail);
            }
            console.log(`[quaid] ${label} extraction complete: ${stored} stored, ${skipped} skipped, ${edgesCreated} edges created from ${facts.length} candidates`);
            console.log(`[quaid][extract] done label=${label} session=${sessionId || "unknown"} stored=${stored} skipped=${skipped} edges=${edgesCreated}`);
            // Collect snippet and journal entry details for notification (before writing them)
            let snippetDetails = {};
            let journalDetails = {};
            try {
                const journalRaw = result.journal_entries;
                const snippetsRaw = result.soul_snippets;
                const journalConfig = getMemoryConfig().docs?.journal || getMemoryConfig().docs?.soulSnippets || {};
                const targetFiles = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];
                // Collect snippet details
                if (snippetsRaw && typeof snippetsRaw === 'object' && !Array.isArray(snippetsRaw)) {
                    for (const [filename, snippets] of Object.entries(snippetsRaw)) {
                        if (!targetFiles.includes(filename)) {
                            continue;
                        }
                        if (!Array.isArray(snippets)) {
                            continue;
                        }
                        const valid = snippets.filter((s) => typeof s === 'string' && s.trim().length > 0);
                        if (valid.length > 0) {
                            snippetDetails[filename] = valid.map((s) => s.trim());
                        }
                    }
                }
                // Collect journal details
                if (journalRaw && typeof journalRaw === 'object' && !Array.isArray(journalRaw)) {
                    for (const [filename, entry] of Object.entries(journalRaw)) {
                        if (!targetFiles.includes(filename)) {
                            continue;
                        }
                        // Handle both string (expected) and array (LLM fallback) values
                        const text = Array.isArray(entry)
                            ? entry.filter((s) => typeof s === 'string').join('\n\n')
                            : (typeof entry === 'string' ? entry : '');
                        if (text.trim().length > 0) {
                            journalDetails[filename] = [text.trim()];
                        }
                    }
                }
            }
            catch { }
            // Notify user about extraction results.
            // For reset/new/timeout, always send a completion message so the starter
            // "Processing memories..." notification is paired with an outcome.
            const hasSnippets = Object.keys(snippetDetails).length > 0;
            const hasJournalEntries = Object.keys(journalDetails).length > 0;
            const triggerType = resolveExtractionTrigger(label);
            const alwaysNotifyCompletion = triggerType === "timeout" || triggerType === "reset" || triggerType === "new";
            if ((facts.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion)
                && shouldNotifyFeature("extraction", "summary")) {
                try {
                    const trigger = triggerType === "unknown" ? "reset" : triggerType;
                    // Merge snippet and journal details for notification
                    const mergedDetails = {};
                    for (const [f, items] of Object.entries(snippetDetails)) {
                        mergedDetails[f] = items.map(s => `[snippet] ${s}`);
                    }
                    for (const [f, items] of Object.entries(journalDetails)) {
                        mergedDetails[f] = [...(mergedDetails[f] || []), ...items.map(s => `[journal] ${s}`)];
                    }
                    const hasMerged = Object.keys(mergedDetails).length > 0;
                    // Write details to temp file for safe passing to Python
                    const detailsPath = path.join(QUAID_TMP_DIR, `extraction-details-${Date.now()}.json`);
                    fs.writeFileSync(detailsPath, JSON.stringify({
                        stored, skipped, edges_created: edgesCreated, trigger, details: factDetails,
                        snippet_details: hasMerged ? mergedDetails : null,
                        always_notify: alwaysNotifyCompletion,
                    }), { mode: 0o600 });
                    spawnNotifyScript(`
import json
from notify import notify_memory_extraction
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
                }
                catch (notifyErr) {
                    console.log(`[quaid] Extraction notification skipped: ${notifyErr.message}`);
                }
            }
            // Write soul snippets to *.snippets.md files (fail-safe: never blocks fact extraction)
            try {
                const snippetsRaw = result.soul_snippets;
                const journalConfig = getMemoryConfig().docs?.journal || getMemoryConfig().docs?.soulSnippets || {};
                const targetFiles = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];
                const snippetsEnabled = isSystemEnabled("journal") && journalConfig.enabled !== false && journalConfig.snippetsEnabled !== false;
                if (snippetsEnabled && snippetsRaw && typeof snippetsRaw === 'object' && !Array.isArray(snippetsRaw)) {
                    const dateStr = new Date().toISOString().slice(0, 10);
                    const timeStr = new Date().toISOString().slice(11, 19);
                    const triggerLabel = triggerLabelFromType(resolveExtractionTrigger(label));
                    let snippetsWritten = 0;
                    for (const [filename, snippets] of Object.entries(snippetsRaw)) {
                        if (!targetFiles.includes(filename)) {
                            continue;
                        }
                        if (!Array.isArray(snippets)) {
                            continue;
                        }
                        const valid = snippets.filter((s) => typeof s === 'string' && s.trim().length > 0);
                        if (valid.length === 0) {
                            continue;
                        }
                        const baseName = filename.replace('.md', '');
                        const snippetsPath = path.join(WORKSPACE, `${baseName}.snippets.md`);
                        // Read existing content
                        let existing = '';
                        try {
                            existing = fs.readFileSync(snippetsPath, 'utf8');
                        }
                        catch { }
                        // Build new section
                        const header = `## ${triggerLabel} \u2014 ${dateStr} ${timeStr}`;
                        const bullets = valid.map((s) => `- ${s.trim()}`).join('\n');
                        const newSection = `\n${header}\n${bullets}\n`;
                        // Dedup: skip if this date+trigger already exists
                        const dedupHeader = `## ${triggerLabel} \u2014 ${dateStr}`;
                        if (existing.includes(dedupHeader)) {
                            console.log(`[quaid] ${label}: skipping duplicate snippet section for ${baseName} (${dateStr})`);
                            continue;
                        }
                        // Prepend title if file is new
                        let updatedContent;
                        if (!existing.trim()) {
                            updatedContent = `# ${baseName} — Pending Snippets\n${newSection}`;
                        }
                        else {
                            // Insert after the first heading line (newest at top)
                            const headerEnd = existing.indexOf('\n');
                            if (headerEnd > 0) {
                                updatedContent = existing.slice(0, headerEnd + 1) + newSection + existing.slice(headerEnd + 1);
                            }
                            else {
                                updatedContent = existing + newSection;
                            }
                        }
                        fs.writeFileSync(snippetsPath, updatedContent);
                        snippetsWritten += valid.length;
                        console.log(`[quaid] ${label}: wrote ${valid.length} snippets to ${baseName}.snippets.md`);
                    }
                    if (snippetsWritten > 0) {
                        console.log(`[quaid] ${label}: total ${snippetsWritten} snippets written`);
                    }
                }
            }
            catch (snippetErr) {
                console.error(`[quaid] ${label}: snippet writing failed (non-fatal):`, snippetErr.message);
            }
            // Write journal entries to journal/*.journal.md files (fail-safe: never blocks fact extraction)
            try {
                const journalRaw = result.journal_entries;
                const journalConfig = getMemoryConfig().docs?.journal || getMemoryConfig().docs?.soulSnippets || {};
                const targetFiles = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];
                const enabled = isSystemEnabled("journal") && journalConfig.enabled !== false;
                if (enabled && journalRaw && typeof journalRaw === 'object' && !Array.isArray(journalRaw)) {
                    const dateStr = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
                    const triggerLabel = triggerLabelFromType(resolveExtractionTrigger(label));
                    let entriesWritten = 0;
                    // Normalize: build a map of filename -> paragraph string
                    const entries = {};
                    for (const [filename, entry] of Object.entries(journalRaw)) {
                        if (!targetFiles.includes(filename)) {
                            continue;
                        }
                        // Handle both string (expected) and array (LLM fallback) values
                        const text = Array.isArray(entry)
                            ? entry.filter((s) => typeof s === 'string').join('\n\n')
                            : (typeof entry === 'string' ? entry : '');
                        if (text.trim().length > 0) {
                            entries[filename] = text.trim();
                        }
                    }
                    if (Object.keys(entries).length > 0) {
                        // Ensure journal directory exists
                        const journalDir = path.join(WORKSPACE, journalConfig.journalDir || 'journal');
                        try {
                            fs.mkdirSync(journalDir, { recursive: true });
                        }
                        catch { }
                        for (const [filename, entryText] of Object.entries(entries)) {
                            const baseName = filename.replace('.md', '');
                            const journalPath = path.join(journalDir, `${baseName}.journal.md`);
                            // Read existing content
                            let existing = '';
                            try {
                                existing = fs.readFileSync(journalPath, 'utf8');
                            }
                            catch { }
                            // Dedup: skip if entry for same date+trigger already exists
                            const dedupHeader = `## ${dateStr} \u2014 ${triggerLabel}`;
                            if (existing.includes(dedupHeader)) {
                                continue;
                            }
                            // Build new entry section
                            const newSection = `\n${dedupHeader}\n${entryText}\n`;
                            // Prepend header if file is new
                            let updatedContent;
                            if (!existing.trim()) {
                                updatedContent = `# ${baseName} Journal\n${newSection}`;
                            }
                            else {
                                // Insert after the first heading line (newest at top)
                                const headerEnd = existing.indexOf('\n');
                                if (headerEnd > 0) {
                                    updatedContent = existing.slice(0, headerEnd + 1) + newSection + existing.slice(headerEnd + 1);
                                }
                                else {
                                    updatedContent = existing + newSection;
                                }
                            }
                            fs.writeFileSync(journalPath, updatedContent);
                            entriesWritten++;
                            console.log(`[quaid] ${label}: wrote journal entry to ${baseName}.journal.md`);
                            // Cap at maxEntriesPerFile — trim oldest entries if exceeded
                            const maxEntries = journalConfig.maxEntriesPerFile || 50;
                            const entryHeaders = updatedContent.match(/^## \d{4}-\d{2}-\d{2}/gm) || [];
                            if (entryHeaders.length > maxEntries) {
                                const lines = updatedContent.split('\n');
                                let entryCount = 0;
                                let cutIndex = lines.length;
                                for (let i = 0; i < lines.length; i++) {
                                    if (/^## \d{4}-\d{2}-\d{2}/.test(lines[i])) {
                                        entryCount++;
                                        if (entryCount > maxEntries) {
                                            cutIndex = i;
                                            break;
                                        }
                                    }
                                }
                                const trimmed = lines.slice(0, cutIndex).join('\n') + '\n';
                                fs.writeFileSync(journalPath, trimmed);
                                console.log(`[quaid] ${label}: trimmed ${baseName}.journal.md to ${maxEntries} entries`);
                            }
                        }
                        if (entriesWritten > 0) {
                            console.log(`[quaid] ${label}: total ${entriesWritten} journal entries written`);
                        }
                    }
                }
            }
            catch (journalErr) {
                console.error(`[quaid] ${label}: journal writing failed (non-fatal):`, journalErr.message);
            }
            // After successful extraction, update extraction log (enriched for session_recall)
            try {
                const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
                let extractionLog = {};
                try {
                    extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, 'utf8'));
                }
                catch { }
                // Extract topic hint from first user message
                let topicHint = "";
                for (const m of messages) {
                    if (m.role === "user") {
                        const content = typeof m.content === 'string'
                            ? m.content
                            : Array.isArray(m.content)
                                ? m.content.map((b) => b.text || '').join(' ')
                                : '';
                        const cleaned = content.replace(/^\[.*?\]\s*/s, '').replace(/^---\s*/m, '').trim();
                        if (cleaned && !cleaned.startsWith("GatewayRestart:") && !cleaned.startsWith("System:")) {
                            topicHint = cleaned.slice(0, 120);
                            break;
                        }
                    }
                }
                extractionLog[sessionId || 'unknown'] = {
                    last_extracted_at: new Date().toISOString(),
                    message_count: messages.length,
                    label: label,
                    topic_hint: topicHint,
                };
                fs.writeFileSync(extractionLogPath, JSON.stringify(extractionLog, null, 2), { mode: 0o600 });
            }
            catch (logErr) {
                console.log(`[quaid] extraction log update failed: ${logErr.message}`);
            }
        };
        if (isSystemEnabled("memory")) {
            const runtimeTitle = String(process.title || "").toLowerCase();
            const runtimeArgv = Array.isArray(process.argv) ? process.argv.join(" ").toLowerCase() : "";
            const isGatewayRuntime = runtimeTitle.includes("openclaw-gateway") || runtimeArgv.includes("openclaw-gateway");
            if (!isGatewayRuntime) {
                console.log("[quaid][timeout] worker start skipped (non-gateway runtime)");
            }
            else {
                const heartbeatSecRaw = Number(getMemoryConfig().capture?.workerHeartbeatSeconds ?? 30);
                const heartbeatSec = Number.isFinite(heartbeatSecRaw) ? Math.max(5, heartbeatSecRaw) : 30;
                const started = timeoutManager.startWorker(heartbeatSec);
                if (started) {
                    console.log(`[quaid][timeout] core worker started heartbeat=${heartbeatSec}s`);
                }
                else {
                    console.log(`[quaid][timeout] core worker start skipped (another worker is leader)`);
                }
            }
        }
        // Recovery scan: detect sessions interrupted by gateway restart before extraction fired
        async function checkForUnextractedSessions() {
            // Rate limit: only run once per gateway restart
            const flagPath = path.join(QUAID_RUNTIME_DIR, "quaid-recovery-ran.txt");
            try {
                const flagStat = fs.statSync(flagPath);
                const fiveMinAgo = Date.now() - 5 * 60 * 1000;
                if (flagStat.mtimeMs > fiveMinAgo) {
                    console.log('[quaid] Recovery scan already ran recently, skipping');
                    return;
                }
            }
            catch { } // Flag doesn't exist — first run
            console.log('[quaid] Running recovery scan for unextracted sessions...');
            const sessionsDir = path.join(os.homedir(), '.openclaw', 'sessions');
            if (!fs.existsSync(sessionsDir)) {
                console.log('[quaid] No sessions directory found, skipping recovery');
                fs.writeFileSync(flagPath, new Date().toISOString());
                return;
            }
            // Load extraction log
            const extractionLogPath = path.join(WORKSPACE, 'data', 'extraction-log.json');
            let extractionLog = {};
            try {
                extractionLog = JSON.parse(fs.readFileSync(extractionLogPath, 'utf8'));
            }
            catch { }
            const sessionFiles = fs.readdirSync(sessionsDir).filter(f => f.endsWith('.jsonl'));
            let recovered = 0;
            for (const file of sessionFiles) {
                const sessionId = file.replace('.jsonl', '');
                const filePath = path.join(sessionsDir, file);
                try {
                    const stat = fs.statSync(filePath);
                    // Skip sessions younger than 5 minutes (may be active)
                    if (Date.now() - stat.mtimeMs < 5 * 60 * 1000) {
                        continue;
                    }
                    // Check if already extracted
                    const logEntry = extractionLog[sessionId];
                    if (logEntry) {
                        const extractedAt = new Date(logEntry.last_extracted_at).getTime();
                        if (extractedAt >= stat.mtimeMs) {
                            continue;
                        } // Already extracted
                    }
                    // Read JSONL and build messages (handles both direct {role} and {type:"message"} formats)
                    const messages = readMessagesFromSessionFile(filePath);
                    // Skip short sessions
                    if (messages.length < 4) {
                        continue;
                    }
                    console.log(`[quaid] Recovering unextracted session ${sessionId} (${messages.length} messages)`);
                    await extractMemoriesFromMessages(messages, 'Recovery', sessionId);
                    recovered++;
                }
                catch (err) {
                    console.error(`[quaid] Recovery failed for session ${sessionId}:`, err.message);
                }
            }
            console.log(`[quaid] Recovery scan complete: ${recovered} sessions recovered`);
            fs.writeFileSync(flagPath, new Date().toISOString());
        }
        // Register compaction hook — extract memories in parallel with compaction LLM
        // Uses sessionFile (JSONL on disk) when available (PR #13287), falls back to
        // in-memory messages for backwards compatibility with older gateway versions.
        api.on("before_compaction", async (event, ctx) => {
            try {
                console.log(`[quaid][debug][before_compaction] entry session=${String(ctx?.sessionId || "unknown")} has_session_file=${event?.sessionFile ? "yes" : "no"} event_messages=${Array.isArray(event?.messages) ? event.messages.length : 0} event_keys=${summarizeEventKeys(event)}`);
                // Prefer reading from sessionFile (all messages already on disk, runs in
                // parallel with compaction). Fall back to in-memory messages array.
                let messages;
                if (event.sessionFile) {
                    try {
                        messages = readMessagesFromSessionFile(event.sessionFile);
                        console.log(`[quaid] before_compaction: read ${messages.length} messages from sessionFile`);
                    }
                    catch (readErr) {
                        console.warn(`[quaid] before_compaction: sessionFile read failed, falling back to messages array: ${String(readErr)}`);
                        messages = event.messages || [];
                    }
                }
                else {
                    messages = event.messages || [];
                }
                const sessionId = ctx?.sessionId;
                console.log(`[quaid] before_compaction hook triggered, ${messages.length} messages, session=${sessionId || "unknown"}`);
                // Wrap extraction in a promise that memory_recall can gate on.
                // This runs async (fire-and-forget from the hook's perspective) but
                // memory_recall will wait for it to finish before querying.
                const doExtraction = async () => {
                    // before_compaction hook is unreliable async-wise; queue signal for worker tick.
                    if (isSystemEnabled("memory")) {
                        const extractionSessionId = sessionId || extractSessionId(messages, ctx);
                        timeoutManager.queueExtractionSignal(extractionSessionId, "CompactionSignal");
                        console.log(`[quaid][signal] queued CompactionSignal session=${extractionSessionId}`);
                    }
                    else {
                        console.log("[quaid] Compaction: memory extraction skipped — memory system disabled");
                    }
                    // Auto-update docs from transcript (non-fatal)
                    const uniqueSessionId = extractSessionId(messages, ctx);
                    try {
                        await updateDocsFromTranscript(messages, "Compaction", uniqueSessionId);
                    }
                    catch (err) {
                        console.error("[quaid] Compaction doc update failed:", err.message);
                    }
                    // Emit project event for background processing (non-fatal)
                    try {
                        await emitProjectEvent(messages, "compact", uniqueSessionId);
                    }
                    catch (err) {
                        console.error("[quaid] Compaction project event failed:", err.message);
                    }
                    // Record compaction timestamp and reset injection dedup list (memory system).
                    if (isSystemEnabled("memory") && uniqueSessionId) {
                        const logPath = getInjectionLogPath(uniqueSessionId);
                        let logData = {};
                        try {
                            logData = JSON.parse(fs.readFileSync(logPath, 'utf8'));
                        }
                        catch { }
                        logData.lastCompactionAt = new Date().toISOString();
                        logData.memoryTexts = []; // Reset — all memories eligible for re-injection
                        fs.writeFileSync(logPath, JSON.stringify(logData, null, 2), { mode: 0o600 });
                        console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
                    }
                };
                // Chain onto any in-flight extraction to avoid overwrite race
                // (if compaction and reset overlap, the .finally() from the first
                // extraction would clear the promise while the second is still running)
                extractionPromise = (extractionPromise || Promise.resolve())
                    .catch(() => { }) // Don't let previous failure block the chain
                    .then(() => doExtraction());
            }
            catch (err) {
                console.error("[quaid] before_compaction hook failed:", err);
            }
        }, {
            name: "compaction-memory-extraction",
            priority: 10
        });
        // Register reset hook — extract memories before session is cleared by /new or /reset
        // Uses sessionFile when available, falls back to in-memory messages.
        api.on("before_reset", async (event, ctx) => {
            try {
                console.log(`[quaid][debug][before_reset] entry session=${String(ctx?.sessionId || "unknown")} reason=${String(event?.reason || "unknown")} has_session_file=${event?.sessionFile ? "yes" : "no"} event_messages=${Array.isArray(event?.messages) ? event.messages.length : 0} event_keys=${summarizeEventKeys(event)}`);
                console.log(`[quaid][reset] hook_entry session=${ctx?.sessionId || "unknown"} reason=${event?.reason || "unknown"} has_session_file=${event?.sessionFile ? "yes" : "no"} event_messages=${Array.isArray(event?.messages) ? event.messages.length : 0}`);
                // Prefer sessionFile (complete transcript on disk), fall back to in-memory messages
                let messages;
                if (event.sessionFile) {
                    console.log(`[quaid][reset] session_file=${event.sessionFile}`);
                    try {
                        messages = readMessagesFromSessionFile(event.sessionFile);
                        console.log(`[quaid] before_reset: read ${messages.length} messages from sessionFile`);
                    }
                    catch (readErr) {
                        console.warn(`[quaid] before_reset: sessionFile read failed, falling back to messages array: ${String(readErr)}`);
                        messages = event.messages || [];
                    }
                }
                else {
                    messages = event.messages || [];
                }
                const reason = event.reason || "unknown";
                const sessionId = ctx?.sessionId;
                console.log(`[quaid] before_reset hook triggered (reason: ${reason}), ${messages.length} messages, session=${sessionId || "unknown"}`);
                const doExtraction = async () => {
                    const started = Date.now();
                    console.log(`[quaid][reset] extraction_begin session=${sessionId || "unknown"} message_count=${messages.length}`);
                    // before_reset can race with session teardown; queue signal for worker tick.
                    if (isSystemEnabled("memory")) {
                        const extractionSessionId = sessionId || extractSessionId(messages, ctx);
                        timeoutManager.queueExtractionSignal(extractionSessionId, "ResetSignal");
                        console.log(`[quaid][signal] queued ResetSignal session=${extractionSessionId}`);
                    }
                    else {
                        console.log("[quaid] Reset: memory extraction skipped — memory system disabled");
                    }
                    // Auto-update docs from transcript (non-fatal)
                    const uniqueSessionId = extractSessionId(messages, ctx);
                    try {
                        await updateDocsFromTranscript(messages, "Reset", uniqueSessionId);
                    }
                    catch (err) {
                        console.error("[quaid] Reset doc update failed:", err.message);
                    }
                    // Emit project event for background processing (non-fatal)
                    try {
                        await emitProjectEvent(messages, "reset", uniqueSessionId);
                    }
                    catch (err) {
                        console.error("[quaid] Reset project event failed:", err.message);
                    }
                    console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"} duration_ms=${Date.now() - started}`);
                };
                // Chain onto any in-flight extraction to avoid overwrite race
                console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${extractionPromise ? "yes" : "no"}`);
                extractionPromise = (extractionPromise || Promise.resolve())
                    .catch((chainErr) => {
                    console.warn(`[quaid][reset] prior_extraction_chain_error session=${sessionId || "unknown"} err=${String(chainErr?.message || chainErr)}`);
                })
                    .then(() => doExtraction())
                    .catch((doErr) => {
                    console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
                    throw doErr;
                });
            }
            catch (err) {
                console.error("[quaid] before_reset hook failed:", err);
            }
        }, {
            name: "reset-memory-extraction",
            priority: 10
        });
        // Register HTTP endpoint for LLM proxy (used by Python janitor/extraction)
        api.registerHttpRoute({
            path: "/plugins/quaid/llm",
            handler: async (req, res) => {
                if (req.method !== "POST") {
                    res.writeHead(405, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: "Method not allowed" }));
                    return;
                }
                const chunks = [];
                for await (const chunk of req) { chunks.push(chunk); }
                let body;
                try {
                    body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
                }
                catch {
                    res.writeHead(400, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: "Invalid JSON body" }));
                    return;
                }
                const { system_prompt, user_message, model_tier, max_tokens = 4000 } = body;
                if (!system_prompt || !user_message) {
                    res.writeHead(400, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: "system_prompt and user_message required" }));
                    return;
                }
                try {
                    const tier = model_tier === "fast" ? "fast" : "deep";
                    const data = await callConfiguredLLM(system_prompt, user_message, tier, max_tokens, 600000);
                    res.writeHead(200, { "Content-Type": "application/json" });
                    res.end(JSON.stringify(data));
                }
                catch (err) {
                    console.error(`[quaid] LLM proxy error: ${String(err)}`);
                    const msg = String(err?.message || err);
                    const status = msg.includes("No ") || msg.includes("Unsupported provider") || msg.includes("ReasoningModelClasses")
                        ? 503 : 502;
                    res.writeHead(status, { "Content-Type": "application/json" });
                    res.end(JSON.stringify({ error: `LLM proxy error: ${String(err)}` }));
                }
            },
        });
        // Register HTTP endpoint for memory dashboard
        api.registerHttpRoute({
            path: "/memory/injected",
            handler: async (req, res) => {
                try {
                    const url = new URL(req.url, "http://localhost");
                    const sessionId = url.searchParams.get("sessionId");
                    if (!sessionId || !/^[a-f0-9-]{1,64}$/i.test(sessionId)) {
                        res.writeHead(400, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ error: "Valid sessionId parameter required" }));
                        return;
                    }
                    // Try to read enhanced log first (NAS), fall back to temp log
                    const enhancedLogPath = path.join(QUAID_LOGS_DIR, "memory-injection", `session-${sessionId}.log`);
                    const tempLogPath = getInjectionLogPath(sessionId);
                    let logData = null;
                    // Try enhanced log first
                    if (fs.existsSync(enhancedLogPath)) {
                        try {
                            const content = fs.readFileSync(enhancedLogPath, 'utf8');
                            logData = JSON.parse(content);
                        }
                        catch (err) {
                            console.error(`[quaid] Failed to read enhanced log: ${String(err)}`);
                        }
                    }
                    // Fall back to temp log
                    if (!logData && fs.existsSync(tempLogPath)) {
                        try {
                            const content = fs.readFileSync(tempLogPath, 'utf8');
                            logData = JSON.parse(content);
                        }
                        catch (err) {
                            console.error(`[quaid] Failed to read temp log: ${String(err)}`);
                        }
                    }
                    if (!logData) {
                        res.writeHead(404, { 'Content-Type': 'application/json' });
                        res.end(JSON.stringify({ error: "Session log not found" }));
                        return;
                    }
                    // Return relevant data for dashboard
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
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                        'Access-Control-Allow-Methods': 'GET',
                        'Access-Control-Allow-Headers': 'Content-Type'
                    });
                    res.end(JSON.stringify(responseData, null, 2));
                }
                catch (err) {
                    console.error(`[quaid] HTTP endpoint error: ${String(err)}`);
                    res.writeHead(500, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ error: "Internal server error" }));
                }
            }
        });
        console.log("[quaid] Plugin loaded with compaction/reset hooks and HTTP endpoint");
    },
};
exports.default = quaidPlugin;
