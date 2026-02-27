/**
 * quaid - Total Recall memory system plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */

import type { ClawdbotPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { execFileSync, execSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
import { queueDelayedRequest } from "./delayed-requests.js";
import { createKnowledgeEngine } from "../../orchestrator/default-orchestrator.js";
import { createProjectCatalogReader } from "../../core/project-catalog.js";
import { createDatastoreBridge } from "../../core/datastore-bridge.js";
import { createPythonBridgeExecutor } from "./python-bridge.js";


// Configuration
function _resolveWorkspace(): string {
  const envWorkspace = String(process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envWorkspace) {
    return envWorkspace;
  }

  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (fs.existsSync(cfgPath)) {
      const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const list = Array.isArray(cfg?.agents?.list) ? cfg.agents.list : [];
      const mainAgent = list.find((a: any) => a?.id === "main" || a?.default === true);
      const ws = String(mainAgent?.workspace || cfg?.agents?.defaults?.workspace || "").trim();
      if (ws) {
        return ws;
      }
    }
  } catch (err: unknown) {
    console.error("[quaid][startup] workspace resolution failed:", (err as Error)?.message || String(err));
  }

  return process.cwd();
}
const WORKSPACE = _resolveWorkspace();
function _resolvePythonPluginRoot(): string {
  const modulesRoot = path.join(WORKSPACE, "modules", "quaid");
  if (fs.existsSync(modulesRoot)) {
    return modulesRoot;
  }
  // Backward compatibility for older workspace layouts.
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

for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch (err: unknown) {
    console.error(`[quaid][startup] failed to create runtime dir: ${p}`, (err as Error)?.message || String(err));
  }
}

// Dynamic retrieval limit — scales logarithmically with graph size
// Formula: K = 11.5 * ln(N) - 61.7, fitted to K-sweep benchmarks:
//   S-scale (322 nodes): K=5 optimal   → formula gives 4.7 → 5
//   L-scale (1182 nodes): K=20 optimal → formula gives 19.7 → 20
// Clamped to [5, 40]. Ceiling backed by "Lost in the Middle" (Liu 2023).
let _cachedNodeCount: number | null = null;
let _nodeCountTimestamp = 0;
const NODE_COUNT_CACHE_MS = 5 * 60 * 1000; // 5 minutes
let _cachedDatastoreStats: Record<string, any> | null = null;
let _datastoreStatsTimestamp = 0;
let _memoryConfigErrorLogged = false;
let _memoryConfigMtimeMs = -1;

function _envTimeoutMs(name: string, fallbackMs: number): number {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}

const EXTRACT_PIPELINE_TIMEOUT_MS = _envTimeoutMs("QUAID_EXTRACT_PIPELINE_TIMEOUT_MS", 300_000);
const EVENTS_EMIT_TIMEOUT_MS = _envTimeoutMs("QUAID_EVENTS_TIMEOUT_MS", 300_000);
const QUICK_PROJECT_SUMMARY_TIMEOUT_MS = _envTimeoutMs("QUAID_PROJECT_SUMMARY_TIMEOUT_MS", 60_000);
const FAST_ROUTER_TIMEOUT_MS = _envTimeoutMs("QUAID_ROUTER_FAST_TIMEOUT_MS", 45_000);
const DEEP_ROUTER_TIMEOUT_MS = _envTimeoutMs("QUAID_ROUTER_DEEP_TIMEOUT_MS", 60_000);
const DATASTORE_STATS_TIMEOUT_MS = _envTimeoutMs("QUAID_DATASTORE_STATS_TIMEOUT_MS", 5_000);

function buildPythonEnv(extra: Record<string, string | undefined> = {}): Record<string, string | undefined> {
  const sep = process.platform === "win32" ? ";" : ":";
  const existing = String(process.env.PYTHONPATH || "").trim();
  const pyPath = existing ? `${PYTHON_PLUGIN_ROOT}${sep}${existing}` : PYTHON_PLUGIN_ROOT;
  return {
    ...process.env,
    MEMORY_DB_PATH: DB_PATH,
    QUAID_HOME: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE,
    PYTHONPATH: pyPath,
    ...extra,
  };
}

function getDatastoreStatsSync(maxAgeMs: number = NODE_COUNT_CACHE_MS): Record<string, any> | null {
  const now = Date.now();
  if ((now - _datastoreStatsTimestamp) < maxAgeMs) {
    return _cachedDatastoreStats;
  }
  try {
    const output = execFileSync("python3", [PYTHON_SCRIPT, "stats"], {
      encoding: "utf-8",
      timeout: DATASTORE_STATS_TIMEOUT_MS,
      env: buildPythonEnv(),
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
    const msg = `[quaid] datastore stats read failed: ${(err as Error)?.message || String(err)}`;
    if (isFailHardEnabled()) {
      const cause = err instanceof Error ? err : new Error(String(err));
      throw new Error(msg, { cause });
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
  if (_cachedNodeCount === null && isFailHardEnabled()) {
    throw new Error("[quaid] unable to derive active node count under failHard");
  }
  return _cachedNodeCount ?? 100; // use last known or fallback
}

function computeDynamicK(): number {
  const nodeCount = getActiveNodeCount();
  if (nodeCount < 10) return 5;
  const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
  return Math.max(5, Math.min(k, 40)); // floor 5, ceiling 40
}

// Model resolution — reads from config/memory.json, no hardcoded model IDs
let _memoryConfig: any = null;
function getMemoryConfig(): any {
  const configPath = path.join(WORKSPACE, "config/memory.json");
  let mtimeMs = -1;
  try {
    mtimeMs = fs.statSync(configPath).mtimeMs;
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err || "");
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
  } catch (err: unknown) {
    if (!_memoryConfigErrorLogged) {
      _memoryConfigErrorLogged = true;
      console.error("[quaid] failed to load config/memory.json:", (err as Error)?.message || String(err));
    }
    if (isFailHardEnabled()) {
      throw err;
    }
    _memoryConfig = {};
  }
  return _memoryConfig;
}

// System gates — check if a subsystem is enabled in config
function isSystemEnabled(system: "memory" | "journal" | "projects" | "workspace"): boolean {
  const config = getMemoryConfig();
  const systems = config.systems || {};
  // Default to true if not specified
  return systems[system] !== false;
}

function isPreInjectionPassEnabled(): boolean {
  const retrieval = getMemoryConfig().retrieval || {};
  if (typeof retrieval.preInjectionPass === "boolean") return retrieval.preInjectionPass;
  if (typeof retrieval.pre_injection_pass === "boolean") return retrieval.pre_injection_pass;
  return true;
}

function isFailHardEnabled(): boolean {
  const retrieval = getMemoryConfig().retrieval || {};
  if (typeof retrieval.fail_hard === "boolean") return retrieval.fail_hard;
  if (typeof retrieval.failHard === "boolean") return retrieval.failHard;
  return true;
}

function isMissingFileError(err: unknown): boolean {
  const code = (err as NodeJS.ErrnoException | undefined)?.code;
  if (code === "ENOENT") return true;
  const msg = String((err as Error | undefined)?.message || "");
  return msg.includes("ENOENT");
}

function getCaptureTimeoutMinutes(): number {
  const capture = getMemoryConfig().capture || {};
  const raw = capture.inactivityTimeoutMinutes ?? capture.inactivity_timeout_minutes ?? 120;
  const num = Number(raw);
  return Number.isFinite(num) ? Math.max(0, num) : 120;
}

function effectiveNotificationLevel(feature: "janitor" | "extraction" | "retrieval"): string {
  const notifications = getMemoryConfig().notifications || {};
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

type ModelTier = "deep" | "fast";
type ExtractionTrigger = "compaction" | "reset" | "new" | "recovery" | "timeout" | "unknown";

function normalizeProvider(provider: string): string {
  return String(provider || "").trim().toLowerCase();
}

function providerClassLookupKey(provider: string): string {
  const normalized = normalizeProvider(provider);
  if (normalized === "openai-codex") return "openai";
  if (normalized === "anthropic-claude-code") return "anthropic";
  return normalized;
}

function getConfiguredTierValue(tier: ModelTier): string {
  const key = tier === "fast" ? "fastReasoning" : "deepReasoning";
  const configured = getMemoryConfig().models?.[key];
  if (typeof configured === "string" && configured.trim().length > 0) {
    return configured.trim();
  }
  throw new Error(`Missing models.${key} in config/memory.json`);
}

function getConfiguredTierProvider(tier: ModelTier): string {
  const key = tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider";
  const configured = getMemoryConfig().models?.[key];
  if (typeof configured === "string" && configured.trim().length > 0) {
    return normalizeProvider(configured.trim());
  }
  return "default";
}

function parseTierModelClassMap(tier: ModelTier): Record<string, string> {
  const models = getMemoryConfig().models || {};
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

function getGatewayDefaultProvider(): string {
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
        if (normalized) { return normalized; }
      }
    }
  } catch (err: unknown) {
    console.warn(`[quaid] gateway default provider read failed from openclaw.json: ${String((err as Error)?.message || err)}`);
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
          if (normalized) { return normalized; }
        }
      }
      for (const key of Object.keys(lastGood)) {
        const normalized = normalizeProvider(key);
        if (normalized) { return normalized; }
      }
    }
  } catch (err: unknown) {
    console.warn(`[quaid] gateway provider fallback read failed from auth-profiles.json: ${String((err as Error)?.message || err)}`);
  }
  return "";
}

function getEffectiveProvider(): string {
  const configuredProvider = normalizeProvider(String(getMemoryConfig().models?.llmProvider || ""));
  if (configuredProvider && configuredProvider !== "default") { return configuredProvider; }
  const gatewayProvider = getGatewayDefaultProvider();
  if (gatewayProvider) { return gatewayProvider; }
  throw new Error(
    "models.llmProvider is 'default' but no active gateway provider was resolved. " +
    "Set models.llmProvider explicitly (anthropic/openai/openai-compatible/claude-code), " +
    "or ensure OpenClaw auth profiles exist and lastGood is set in ~/.openclaw/agents/main/agent/auth-profiles.json."
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
          `models.${tier === "fast" ? "fastReasoning" : "deepReasoning"} provider "${normalizedProvider}" does not match models.${tier === "fast" ? "fastReasoningProvider" : "deepReasoningProvider"}="${configuredTierProvider}"`
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

function runStartupSelfCheck(): void {
  const errors: string[] = [];

  try {
    const deep = resolveTierModel("deep");
    console.log(`[quaid][startup] deep model resolved: provider=${deep.provider} model=${deep.model}`);
    const paidProviders = new Set(["openai-compatible"]);
    if (paidProviders.has(deep.provider)) {
      console.warn(`[quaid][billing] paid provider active for deep reasoning: ${deep.provider}/${deep.model}`);
    }
  } catch (err: unknown) {
    errors.push(`deep reasoning model resolution failed: ${String((err as Error)?.message || err)}`);
  }

  try {
    const fast = resolveTierModel("fast");
    console.log(`[quaid][startup] fast model resolved: provider=${fast.provider} model=${fast.model}`);
    const paidProviders = new Set(["openai-compatible"]);
    if (paidProviders.has(fast.provider)) {
      console.warn(`[quaid][billing] paid provider active for fast reasoning: ${fast.provider}/${fast.model}`);
    }
  } catch (err: unknown) {
    errors.push(`fast reasoning model resolution failed: ${String((err as Error)?.message || err)}`);
  }

  try {
    const cfg = getMemoryConfig();
    const maxResults = Number(cfg?.retrieval?.maxLimit ?? cfg?.retrieval?.max_limit ?? 0);
    if (!Number.isFinite(maxResults) || maxResults <= 0) {
      errors.push(`invalid retrieval.maxLimit=${String(cfg?.retrieval?.maxLimit ?? cfg?.retrieval?.max_limit)}`);
    }
  } catch (err: unknown) {
    errors.push(`config load failed: ${String((err as Error)?.message || err)}`);
  }

  const requiredFiles = [
    path.join(WORKSPACE, "plugins", "quaid", "core", "lifecycle", "janitor.py"),
    path.join(WORKSPACE, "plugins", "quaid", "datastore", "memorydb", "memory_graph.py"),
  ];
  for (const file of requiredFiles) {
    if (!fs.existsSync(file)) {
      errors.push(`required runtime file missing: ${file}`);
    }
  }

  if (errors.length > 0) {
    const msg = `[quaid][startup] preflight failed:\n- ${errors.join("\n- ")}`;
    console.error(msg);
    throw new Error(msg);
  }
}

function resolveExtractionTrigger(label: string): ExtractionTrigger {
  const normalized = String(label || "").trim().toLowerCase();
  if (!normalized) { return "unknown"; }
  if (normalized.includes("compact")) { return "compaction"; }
  if (normalized.includes("recover")) { return "recovery"; }
  if (normalized.includes("timeout")) { return "timeout"; }
  if (normalized.includes("new")) { return "new"; }
  if (normalized.includes("reset")) { return "reset"; }
  return "unknown";
}

// Config schema
const configSchema = Type.Object({
  autoCapture: Type.Optional(Type.Boolean({ default: false })),
  autoRecall: Type.Optional(Type.Boolean({ default: true })),
});

type PluginConfig = {
  autoCapture?: boolean;
  autoRecall?: boolean;
};

// User identity mapping (loaded from config/memory.json)
type UsersConfig = {
  defaultOwner: string;
  identities: Record<string, {
    channels: Record<string, string[]>;
    speakers: string[];
  }>;
};

let _usersConfig: UsersConfig | null = null;
let _usersConfigMtimeMs = -1;

function getUsersConfig(): UsersConfig {
  const configPath = path.join(WORKSPACE, "config/memory.json");
  let mtimeMs = -1;
  try {
    mtimeMs = fs.statSync(configPath).mtimeMs;
  } catch {
    mtimeMs = -1;
  }
  if (_usersConfig && _usersConfigMtimeMs === mtimeMs) {
    return _usersConfig;
  }
  try {
    const raw = JSON.parse(fs.readFileSync(configPath, "utf8"));
    _usersConfig = raw.users || { defaultOwner: "quaid", identities: {} };
    _usersConfigMtimeMs = mtimeMs;
  } catch (err: unknown) {
    console.error("[quaid] failed to load users config from config/memory.json:", (err as Error)?.message || String(err));
    _usersConfig = { defaultOwner: "quaid", identities: {} };
    _usersConfigMtimeMs = mtimeMs;
  }
  return _usersConfig!;
}

function resolveOwner(speaker?: string, channel?: string): string {
  const config = getUsersConfig();
  for (const [userId, identity] of Object.entries(config.identities)) {
    // Match by speaker name
    if (speaker && identity.speakers.some(s =>
      s.toLowerCase() === speaker.toLowerCase()
    )) {
      return userId;
    }
    // Match by channel
    if (channel && identity.channels[channel]) {
      const allowed = identity.channels[channel];
      if (allowed.includes("*")) { return userId; }
      if (speaker && allowed.some(a => a.toLowerCase() === speaker.toLowerCase())) { return userId; }
    }
  }
  return config.defaultOwner;
}

function isInternalQuaidSession(sessionId: unknown): boolean {
  const sid = typeof sessionId === "string" ? sessionId.trim() : "";
  if (!sid) return false;
  return sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm");
}

// ============================================================================
// Python Bridge
// ============================================================================

const datastoreBridge = createDatastoreBridge(
  createPythonBridgeExecutor({
    scriptPath: PYTHON_SCRIPT,
    dbPath: DB_PATH,
    workspace: WORKSPACE,
  })
);

// ============================================================================
// Memory Notes — queued for extraction at compaction/reset
// ============================================================================

// Session-scoped notes: memory_store writes here instead of directly to DB.
// At compaction/reset, these are prepended to the transcript so Opus extracts
// them with full context, edges, and quality review.
const _memoryNotes = new Map<string, string[]>();
const _memoryNotesTouchedAt = new Map<string, number>();
const MAX_MEMORY_NOTE_SESSIONS = 200;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_INJECTION_LOG_FILES = 400;
const MAX_INJECTION_IDS_PER_SESSION = 4000;
const MAX_EXTRACTION_LOG_ENTRIES = 800;
const NOTES_DIR = QUAID_NOTES_DIR;

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

function getInjectionLogPath(sessionId: string): string {
  return path.join(QUAID_INJECTION_LOG_DIR, `memory-injection-${sessionId}.log`);
}

function pruneInjectionLogFiles(): void {
  try {
    const files = fs.readdirSync(QUAID_INJECTION_LOG_DIR)
      .filter((f: string) => f.startsWith("memory-injection-") && f.endsWith(".log"))
      .map((f: string) => ({ name: f, full: path.join(QUAID_INJECTION_LOG_DIR, f), mtimeMs: fs.statSync(path.join(QUAID_INJECTION_LOG_DIR, f)).mtimeMs }))
      .sort((a, b) => b.mtimeMs - a.mtimeMs);
    for (const stale of files.slice(MAX_INJECTION_LOG_FILES)) {
      try {
        fs.unlinkSync(stale.full);
      } catch (err: unknown) {
        console.warn(`[quaid] Failed pruning stale injection log ${stale.full}: ${String((err as Error)?.message || err)}`);
      }
    }
  } catch (err: unknown) {
    console.warn(`[quaid] Injection log pruning failed: ${String((err as Error)?.message || err)}`);
  }
}

function trimExtractionLogEntries(log: Record<string, any>, maxEntries: number = MAX_EXTRACTION_LOG_ENTRIES): Record<string, any> {
  const entries = Object.entries(log || {});
  if (entries.length <= maxEntries) {
    return log || {};
  }
  const sorted = entries
    .map(([sid, payload]) => ({ sid, payload, ts: Date.parse(String((payload as any)?.last_extracted_at || "")) || 0 }))
    .sort((a, b) => b.ts - a.ts)
    .slice(0, maxEntries);
  return Object.fromEntries(sorted.map((row) => [row.sid, row.payload]));
}

function addMemoryNote(sessionId: string, text: string, category: string): void {
  // Bound in-memory session cache (disk persistence remains source of truth).
  _memoryNotesTouchedAt.set(sessionId, Date.now());
  if (_memoryNotes.size >= MAX_MEMORY_NOTE_SESSIONS && !_memoryNotes.has(sessionId)) {
    const oldest = Array.from(_memoryNotesTouchedAt.entries()).sort((a, b) => a[1] - b[1])[0]?.[0];
    if (oldest) {
      _memoryNotes.delete(oldest);
      _memoryNotesTouchedAt.delete(oldest);
    }
  }

  // In-memory
  if (!_memoryNotes.has(sessionId)) {
    _memoryNotes.set(sessionId, []);
  }
  const noteList = _memoryNotes.get(sessionId)!;
  noteList.push(`[${category}] ${text}`);
  if (noteList.length > MAX_MEMORY_NOTES_PER_SESSION) {
    noteList.splice(0, noteList.length - MAX_MEMORY_NOTES_PER_SESSION);
  }

  // Persist to disk (survives gateway restart)
  try {
    withNotesLock(sessionId, () => {
      const notesPath = getNotesPath(sessionId);
      let existing: string[] = [];
      try {
        existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        if (!msg.includes("ENOENT") && isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] memory note read failed for ${notesPath}: ${msg}`);
      }
      existing.push(`[${category}] ${text}`);
      fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 0o600 });
    });
  } catch (err: unknown) {
    if (isFailHardEnabled()) {
      throw err;
    }
    console.warn(`[quaid] memory note write failed for session ${sessionId}: ${String((err as Error)?.message || err)}`);
  }
}

function getAndClearMemoryNotes(sessionId: string): string[] {
  // Merge in-memory + disk
  return withNotesLock(sessionId, () => {
    const inMemory = _memoryNotes.get(sessionId) || [];
    let onDisk: string[] = [];
    const notesPath = getNotesPath(sessionId);
    try {
      onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
    } catch (err: unknown) {
      console.warn(`[quaid] memory note load failed for ${notesPath}: ${String((err as Error)?.message || err)}`);
    }

    // Deduplicate (in case both sources have the same note)
    const all = Array.from(new Set([...inMemory, ...onDisk]));

    // Clear both
    _memoryNotes.delete(sessionId);
    _memoryNotesTouchedAt.delete(sessionId);
    try {
      fs.unlinkSync(notesPath);
    } catch (err: unknown) {
      console.warn(`[quaid] memory note cleanup failed for ${notesPath}: ${String((err as Error)?.message || err)}`);
    }

    return all;
  });
}

// ============================================================================
// Session ID Helper
// ============================================================================

function extractSessionId(messages: any[], ctx?: any): string {
  // Prefer Pi SDK session UUID if provided by hook context
  if (ctx?.sessionId) {
    return ctx.sessionId;
  }

  // Deterministic fallback when ctx.sessionId is unavailable.
  // Find first user message with timestamp
  let firstTimestamp = "";
  const filteredMessages = messages.filter((m: any) => {
    if (m.role !== "user") { return false; }

    // Skip system-injected messages even if they have timestamps
    let content = '';
    if (typeof m.content === 'string') {
      content = m.content;
    } else if (Array.isArray(m.content)) {
      content = m.content.map((c: any) => c.text || '').join(' ');
    }
    if (content.startsWith("GatewayRestart:")) { return false; }
    if (content.startsWith("System:")) { return false; }
    if (content.includes('"kind": "restart"')) { return false; }

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
  
  // Create session identifier: timestamp hash only (channel agnostic)
  const timestampHash = createHash("md5").update(firstTimestamp).digest("hex").substring(0, 12);
  return timestampHash;
}

function getAllConversationMessages(messages: any[]): any[] {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg: any) => {
    if (!msg || (msg.role !== "user" && msg.role !== "assistant")) return false;
    const text = getMessageText(msg).trim();
    if (!text) return false;
    // Filter synthetic internal extraction traffic that can leak into event.messages.
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
            if (onlyExtractionKeys && Array.isArray((parsed as any).facts)) return false;
          }
        } catch {}
      }
    }
    return true;
  });
}

function detectLifecycleCommandSignal(messages: any[]): "ResetSignal" | "CompactionSignal" | null {
  if (!Array.isArray(messages) || messages.length === 0) return null;
  const last = messages[messages.length - 1];
  const prev = messages.length > 1 ? messages[messages.length - 2] : null;
  const candidate = last?.role === "user" ? last : (prev?.role === "user" ? prev : null);
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

function isInternalMaintenancePrompt(text: string): boolean {
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
    "are these two statements the same fact",
  ];
  return markers.some((m) => s.includes(m));
}

function resolveSessionKeyForCompaction(sessionId?: string): string | null {
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const data = JSON.parse(raw);
    const entries = Object.entries(data || {}).filter(([_, v]) => v && typeof v === "object") as Array<[string, any]>;
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

function maybeForceCompactionAfterTimeout(sessionId?: string): void {
  const captureCfg = getMemoryConfig().capture || {};
  const enabled = Boolean(
    captureCfg.autoCompactionOnTimeout ??
    captureCfg.auto_compaction_on_timeout ??
    true
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
      { encoding: "utf-8", timeout: 20_000 }
    );
    const parsed = JSON.parse(String(out || "{}"));
    if (parsed?.ok) {
      console.log(`[quaid][timeout] auto-compaction requested for key=${key} (compacted=${String(parsed?.compacted)})`);
    } else {
      console.warn(`[quaid][timeout] auto-compaction returned non-ok for key=${key}: ${String(out).slice(0, 300)}`);
    }
  } catch (err: unknown) {
    if (isFailHardEnabled()) {
      throw err;
    }
    console.warn(`[quaid][timeout] auto-compaction failed for key=${key}: ${String((err as Error)?.message || err)}`);
  }
}

// ============================================================================
// Python Bridges (docs_updater, docs_rag)
// ============================================================================

const DOCS_UPDATER = path.join(WORKSPACE, "modules/quaid/datastore/docsdb/updater.py");
const DOCS_RAG = path.join(WORKSPACE, "modules/quaid/datastore/docsdb/rag.py");
const DOCS_REGISTRY = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/registry.py");
const PROJECT_UPDATER = path.join(WORKSPACE, "modules/quaid/datastore/docsdb/project_updater.py");
const EVENTS_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "core/runtime/events.py");

function _getGatewayCredential(providers: string[]): string | undefined {
  try {
    const profilesPath = path.join(
      os.homedir(), ".openclaw", "agents", "main", "agent", "auth-profiles.json"
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
  } catch (err: unknown) {
    if (isFailHardEnabled()) {
      throw err;
    }
    /* auth-profiles not available */
  }
  return undefined;
}

function _getAnthropicCredential(): string | undefined {
  return _getGatewayCredential(["anthropic"]);
}

function _readOpenClawConfig(): any {
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (!fs.existsSync(cfgPath)) { return {}; }
    return JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err || "");
    if (!msg.includes("ENOENT")) {
      console.warn(`[quaid] openclaw config read failed; using gateway defaults: ${msg}`);
    }
    return {};
  }
}

function _getGatewayBaseUrl(): string {
  const envUrl = String(process.env.OPENCLAW_GATEWAY_URL || "").trim();
  if (envUrl) { return envUrl.replace(/\/+$/, ""); }
  const cfg = _readOpenClawConfig();
  const port = Number(cfg?.gateway?.port || process.env.OPENCLAW_GATEWAY_PORT || 18789);
  return `http://127.0.0.1:${Number.isFinite(port) && port > 0 ? port : 18789}`;
}

function _getGatewayToken(): string | undefined {
  const envToken = String(process.env.OPENCLAW_GATEWAY_TOKEN || "").trim();
  if (envToken) { return envToken; }
  const cfg = _readOpenClawConfig();
  const mode = String(cfg?.gateway?.auth?.mode || "").trim().toLowerCase();
  const token = String(cfg?.gateway?.auth?.token || "").trim();
  if (mode === "token" && token) { return token; }
  return undefined;
}

function _ensureGatewaySessionOverride(
  tier: ModelTier,
  resolved: { provider: string; model: string },
): string {
  const storePath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
  const sessionKey = `agent:main:quaid-llm-${tier}`;
  const now = Date.now();

  let store: Record<string, any> = {};
  try {
    if (fs.existsSync(storePath)) {
      store = JSON.parse(fs.readFileSync(storePath, "utf8")) || {};
    }
  } catch (err: unknown) {
    console.warn(
      `[quaid][llm] failed to read gateway session override store; recreating: ${String((err as Error)?.message || err)}`
    );
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

  try {
    fs.mkdirSync(path.dirname(storePath), { recursive: true });
    fs.writeFileSync(storePath, JSON.stringify(store, null, 2), { mode: 0o600 });
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err);
    throw new Error(`[quaid][llm] failed writing gateway session override store: ${msg}`, {
      cause: err as Error,
    });
  }
  return sessionKey;
}

type LLMProxyResponse = {
  text: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  truncated: boolean;
};

async function callConfiguredLLM(
  systemPrompt: string,
  userMessage: string,
  modelTier: ModelTier,
  maxTokens: number,
  timeoutMs: number = 600_000,
): Promise<LLMProxyResponse> {
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
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const isTransientError = (status: number | null, err: unknown): boolean => {
    if (typeof status === "number" && (status === 429 || status >= 500)) return true;
    const msg = String((err as Error)?.message || err || "").toLowerCase();
    const name = String((err as Error)?.name || "").toLowerCase();
    return (
      name.includes("timeout")
      || msg.includes("timeout")
      || msg.includes("timed out")
      || msg.includes("econnreset")
      || msg.includes("econnrefused")
      || msg.includes("network")
      || msg.includes("fetch failed")
    );
  };
  const readBodyWithTimeout = async (resp: Response, bodyTimeoutMs: number): Promise<string> => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      return await Promise.race([
        resp.text(),
        new Promise<string>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error(`gateway response body timeout after ${bodyTimeoutMs}ms`)),
            bodyTimeoutMs
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  };

  const maxAttempts = 2;
  let data: any = null;
  let gatewayRes: Response | null = null;
  let rawBody = "";
  let lastError: unknown = null;
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
            { type: "message", role: "user", content: userMessage },
          ],
          max_output_tokens: maxTokens,
          stream: false,
        }),
        signal: AbortSignal.timeout(timeoutMs),
      });
      const elapsedMs = Date.now() - attemptStart;
      const bodyTimeoutMs = Math.max(1, timeoutMs - elapsedMs);
      rawBody = await readBodyWithTimeout(gatewayRes, bodyTimeoutMs);
      try {
        data = rawBody ? JSON.parse(rawBody) : {};
      } catch (err: unknown) {
        const parseMsg = String((err as Error)?.message || err);
        const bodyPreview = rawBody.slice(0, 500).replace(/\s+/g, " ");
        console.error(
          `[quaid][llm] gateway_parse_error tier=${modelTier} status=${gatewayRes.status} status_text=${gatewayRes.statusText} parse_error=${JSON.stringify(parseMsg)} body_preview=${JSON.stringify(bodyPreview)}`
        );
        throw new Error(
          `Gateway response parse failed (${gatewayRes.status} ${gatewayRes.statusText}): ${parseMsg}`,
          { cause: err as Error }
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
          `[quaid][llm] tier=${modelTier} provider=${provider} model=${resolved.model} `
          + `status=${gatewayRes.status} error=${String(err)}`
        );
      }
      break;
    } catch (err: unknown) {
      lastError = err;
      const durationMs = Date.now() - started;
      console.error(
        `[quaid][llm] gateway_fetch_error tier=${modelTier} duration_ms=${durationMs} error=${(err as Error)?.name || "Error"}:${(err as Error)?.message || String(err)} attempt=${attempt}/${maxAttempts}`
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
      { cause: lastError ? new Error(String(lastError)) : undefined },
    );
  }

  const text = typeof data.output_text === "string"
    ? data.output_text
    : Array.isArray(data.output)
      ? data.output
          .flatMap((o: any) => Array.isArray(o?.content) ? o.content : [])
          .filter((c: any) => (c?.type === "output_text" || c?.type === "text") && typeof c?.text === "string")
          .map((c: any) => c.text)
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

function _spawnWithTimeout(
  script: string, command: string, args: string[],
  label: string, env: Record<string, string | undefined>,
  timeoutMs: number = PYTHON_BRIDGE_TIMEOUT_MS
): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn("python3", [script, command, ...args], {
      cwd: WORKSPACE,
      env: buildPythonEnv(env),
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    let killTimer: ReturnType<typeof setTimeout> | null = null;

    const timer = setTimeout(() => {
      if (!settled) {
        try {
          proc.kill("SIGTERM");
        } catch {
          // best-effort only
        }
        killTimer = setTimeout(() => {
          if (!settled) {
            try {
              proc.kill("SIGKILL");
            } catch {
              // best-effort only
            }
          }
        }, 5_000);
        settled = true;
        reject(new Error(`${label} timeout after ${timeoutMs}ms: ${command} ${args.join(" ")}`));
      }
    }, timeoutMs);

    proc.stdout.on("data", (data: Buffer) => { stdout += data; });
    proc.stderr.on("data", (data: Buffer) => { stderr += data; });
    proc.on("close", (code: number | null) => {
      if (settled) { return; }
      settled = true;
      clearTimeout(timer);
      if (killTimer) {
        clearTimeout(killTimer);
        killTimer = null;
      }
      if (code === 0) { resolve(stdout.trim()); }
      else {
        const stderrText = stderr.trim();
        const stdoutText = stdout.trim();
        const detail = [stderrText ? `stderr: ${stderrText}` : "", stdoutText ? `stdout: ${stdoutText}` : ""]
          .filter(Boolean)
          .join(" | ")
          .slice(0, 1000);
        reject(new Error(`${label} error (exit=${String(code)}): ${detail}`));
      }
    });
    proc.on("error", (err: Error) => {
      if (settled) { return; }
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

async function callExtractPipeline(opts: {
  transcript: string;
  owner: string;
  label: string;
  sessionId?: string;
  writeSnippets: boolean;
  writeJournal: boolean;
}): Promise<any> {
  const tmpPath = path.join(QUAID_TMP_DIR, `extract-input-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
  fs.writeFileSync(tmpPath, opts.transcript, { mode: 0o600 });
  const args = [
    tmpPath,
    "--owner", opts.owner,
    "--label", opts.label,
    "--json",
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
      EXTRACT_PIPELINE_TIMEOUT_MS,
    );
    const parsed = JSON.parse(output || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("extract pipeline returned non-object JSON payload");
    }
    return parsed;
  } catch (err: unknown) {
    const cause = err instanceof Error ? err : new Error(String(err));
    const msg = String(cause.message || cause);
    throw new Error(
      `[quaid] extract pipeline parse/exec failed: ${msg.slice(0, 500)}`,
      { cause },
    );
  } finally {
    try {
      fs.unlinkSync(tmpPath);
    } catch (err: unknown) {
      console.warn(`[quaid] Failed cleaning extraction temp file ${tmpPath}: ${String((err as Error)?.message || err)}`);
    }
  }
}

async function callDocsUpdater(command: string, args: string[] = []): Promise<string> {
  const apiKey = _getAnthropicCredential();
  return _spawnWithTimeout(DOCS_UPDATER, command, args, "docs_updater", {
    QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE, ...(apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}),
  });
}

async function emitEvent(
  name: string,
  payload: Record<string, unknown>,
  dispatch: "auto" | "immediate" | "queued" = "auto"
): Promise<any> {
  const args = [
    "emit",
    "--name", name,
    "--payload", JSON.stringify(payload || {}),
    "--source", "openclaw_adapter",
    "--dispatch", dispatch,
  ];
  const out = await _spawnWithTimeout(EVENTS_SCRIPT, "emit", args.slice(1), "events", {
    QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
  }, EVENTS_EMIT_TIMEOUT_MS);
  let parsed: unknown = null;
  try {
    parsed = JSON.parse(out || "{}");
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err);
    throw new Error(`[quaid] events emit returned invalid JSON: ${msg}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("[quaid] events emit returned non-object payload");
  }
  return parsed;
}

async function callDocsRag(command: string, args: string[] = []): Promise<string> {
  return _spawnWithTimeout(DOCS_RAG, command, args, "docs_rag", {
    QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
  });
}

async function callDocsRegistry(command: string, args: string[] = []): Promise<string> {
  return _spawnWithTimeout(DOCS_REGISTRY, command, args, "docs_registry", {
    QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
  });
}

// ============================================================================
// Project Helpers
// ============================================================================

const projectCatalogReader = createProjectCatalogReader({
  workspace: WORKSPACE,
  fs,
  path,
  isFailHardEnabled,
});
const getProjectNames = () => projectCatalogReader.getProjectNames();
const getProjectCatalog = () => projectCatalogReader.getProjectCatalog();

/**
 * Spawn a fire-and-forget Python notification script safely.
 * Writes code to a temp file to avoid shell injection via inline -c strings.
 * The script auto-deletes its temp file on completion.
 */
function spawnNotifyScript(scriptBody: string): boolean {
  const tmpFile = path.join(QUAID_NOTIFY_DIR, `notify-${Date.now()}-${Math.random().toString(36).slice(2)}.py`);
  const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
  const appendNotifyLog = (msg: string) => {
    try {
      fs.appendFileSync(notifyLogFile, `${new Date().toISOString()} ${msg}\n`);
    } catch {
      // best-effort only
    }
  };
  const preamble = `import sys, os\nsys.path.insert(0, ${JSON.stringify(path.join(WORKSPACE, "plugins/quaid"))})\n`;
  const cleanup = `\nos.unlink(${JSON.stringify(tmpFile)})\n`;
  let launched = false;
  let notifyLogFd: number | null = null;
  fs.writeFileSync(tmpFile, preamble + scriptBody + cleanup, { mode: 0o600 });
  try {
    notifyLogFd = fs.openSync(notifyLogFile, "a");
    const proc = spawn('python3', [tmpFile], {
      detached: true,
      stdio: ['ignore', notifyLogFd, notifyLogFd],
      env: buildPythonEnv(),
    });
    launched = true;
    proc.on("error", (err: Error) => {
      appendNotifyLog(`[notify-worker-error] spawn failed: ${err.message}`);
      // If spawn fails asynchronously after launch, clean up the temp script.
      try {
        fs.unlinkSync(tmpFile);
      } catch {
        // best-effort only
      }
    });
    proc.unref();
  } catch (err: unknown) {
    appendNotifyLog(`[notify-worker-error] launch failed: ${String((err as Error)?.message || err)}`);
    if (!launched) {
      try {
        fs.unlinkSync(tmpFile);
      } catch {
        // best-effort only
      }
    }
  } finally {
    if (notifyLogFd !== null) {
      try {
        fs.closeSync(notifyLogFd);
      } catch {
        // best-effort only
      }
    }
  }
  return launched;
}

function _loadJanitorNudgeState(): Record<string, any> {
  try {
    if (fs.existsSync(JANITOR_NUDGE_STATE_PATH)) {
      return JSON.parse(fs.readFileSync(JANITOR_NUDGE_STATE_PATH, "utf8")) || {};
    }
  } catch (err: unknown) {
    console.warn(`[quaid] Failed to load janitor nudge state: ${String((err as Error)?.message || err)}`);
  }
  return {};
}

function _saveJanitorNudgeState(state: Record<string, any>): void {
  try {
    fs.writeFileSync(JANITOR_NUDGE_STATE_PATH, JSON.stringify(state, null, 2), { mode: 0o600 });
  } catch (err: unknown) {
    console.warn(`[quaid] Failed to save janitor nudge state: ${String((err as Error)?.message || err)}`);
  }
}

function queueDelayedLlmRequest(message: string, kind: string = "janitor", priority: string = "normal"): boolean {
  return queueDelayedRequest(
    DELAYED_LLM_REQUESTS_PATH,
    message,
    kind,
    priority,
    "quaid_adapter",
    isFailHardEnabled(),
  );
}

function getJanitorHealthIssue(): string | null {
  try {
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
  } catch (err: unknown) {
    if (isFailHardEnabled()) {
      throw new Error("[quaid] Failed to evaluate janitor health under failHard", { cause: err as Error });
    }
    console.warn(`[quaid] Failed to evaluate janitor health: ${String((err as Error)?.message || err)}`);
    return null;
  }
}

function maybeQueueJanitorHealthAlert(): void {
  const issue = getJanitorHealthIssue();
  if (!issue) return;
  const now = Date.now();
  const state = _loadJanitorNudgeState();
  const lastAt = Number(state.lastJanitorHealthAlertAt || 0);
  const cooldown = 6 * 60 * 60 * 1000;
  if (now - lastAt < cooldown && String(state.lastJanitorHealthIssue || "") === issue) return;
  if (queueDelayedLlmRequest(issue, "janitor_health", "high")) {
    state.lastJanitorHealthAlertAt = now;
    state.lastJanitorHealthIssue = issue;
    _saveJanitorNudgeState(state);
  }
}

function maybeSendJanitorNudges(): void {
  // Adapter-owned reminders: do not rely on HEARTBEAT for cross-platform behavior.
  const now = Date.now();
  const state = _loadJanitorNudgeState();
  const lastInstallNudge = Number(state.lastInstallNudgeAt || 0);
  const lastApprovalNudge = Number(state.lastApprovalNudgeAt || 0);
  const NUDGE_COOLDOWN_MS = 6 * 60 * 60 * 1000; // 6h

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
  } catch (err: unknown) {
    console.warn(`[quaid] Install nudge check failed: ${String((err as Error)?.message || err)}`);
  }

  try {
    if (fs.existsSync(PENDING_APPROVAL_REQUESTS_PATH) && now - lastApprovalNudge > NUDGE_COOLDOWN_MS) {
      const raw = JSON.parse(fs.readFileSync(PENDING_APPROVAL_REQUESTS_PATH, "utf8"));
      const requests = Array.isArray(raw?.requests) ? raw.requests : [];
      const pendingCount = requests.filter((r: any) => r?.status === "pending").length;
      if (pendingCount > 0) {
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("Quaid has ${pendingCount} pending approval request(s). Review pending maintenance approvals.")
`);
        state.lastApprovalNudgeAt = now;
      }
    }
  } catch (err: unknown) {
    console.warn(`[quaid] Approval nudge check failed: ${String((err as Error)?.message || err)}`);
  }

  _saveJanitorNudgeState(state);
}

// ============================================================================
// Project Event Emitter (compact/reset → background processor)
// ============================================================================

function extractFilePaths(messages: any[]): string[] {
  const paths = new Set<string>();
  for (const msg of messages) {
    const text = typeof msg.content === "string"
      ? msg.content
      : (msg.content as any[])?.map((c: any) => c.text || "").join(" ") || "";
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

async function getQuickProjectSummary(messages: any[]): Promise<{project_name: string | null, text: string}> {
  const transcript = buildTranscript(messages);
  if (!transcript || transcript.length < 20) {
    return { project_name: null, text: "" };
  }

  try {
    const llm = await callConfiguredLLM(
      `You summarize coding sessions. Given a conversation, identify: 1) What project was being worked on (use one of the available project names, or null if unclear), 2) Brief summary of what changed/was discussed. Available projects: ${getProjectNames().join(", ")}. Use these EXACT names. Respond with JSON only: {"project_name": "name-or-null", "text": "brief summary"}`,
      `Summarize this session:\n\n${transcript.slice(0, 4000)}`,
      "fast",
      300,
      QUICK_PROJECT_SUMMARY_TIMEOUT_MS,
    );
    const output = (llm.text || "").trim();
    const jsonMatch = output.match(/\{[\s\S]*\}/);
      if (jsonMatch) {
        try {
          const parsed = JSON.parse(jsonMatch[0]);
          return {
            project_name: typeof parsed.project_name === "string" ? parsed.project_name : null,
            text: typeof parsed.text === "string" ? parsed.text : "",
          };
        } catch (err: unknown) {
          console.warn(`[quaid] Quick project summary JSON parse failed: ${String((err as Error)?.message || err)}`);
        }
      }
  } catch (err: unknown) {
    console.error("[quaid] Quick project summary failed:", (err as Error).message);
    if (isFailHardEnabled()) {
      throw err;
    }
  }

  return { project_name: null, text: transcript.slice(0, 500) };
}

async function emitProjectEvent(messages: any[], trigger: string, sessionId?: string): Promise<void> {
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

    // 3. Spawn background processor (detached) — gateway-managed credential only
    const bgApiKey = _getAnthropicCredential();
    const logFile = path.join(WORKSPACE, "logs/project-updater.log");
    const logDir = path.dirname(logFile);
    if (!fs.existsSync(logDir)) { fs.mkdirSync(logDir, { recursive: true }); }
    const logFd = fs.openSync(logFile, "a");
    try {
      const proc = spawn("python3", [PROJECT_UPDATER, "process-event", eventPath], {
        detached: true,
        stdio: ["ignore", logFd, logFd],
        cwd: WORKSPACE,
        env: buildPythonEnv({ ...(bgApiKey ? { ANTHROPIC_API_KEY: bgApiKey } : {}) }),
      });
      proc.unref();
    } finally {
      // Close FD in parent process — child has its own copy
      fs.closeSync(logFd);
    }

    console.log(`[quaid] Emitted project event: ${trigger} -> ${summary.project_name || "unknown"}`);
  } catch (err: unknown) {
    console.error("[quaid] Failed to emit project event:", (err as Error).message);
    if (isFailHardEnabled()) {
      throw err;
    }
  }
}

// ============================================================================
// Transcript Builder (shared by memory extraction + doc update)
// ============================================================================

function buildTranscript(messages: any[]): string {
  const transcript: string[] = [];
  for (const msg of messages) {
    if (msg.role !== "user" && msg.role !== "assistant") { continue; }
    let text = typeof msg.content === "string"
      ? msg.content
      : (msg.content as any[])?.map((c: any) => c.text || "").join(" ");
    if (!text) { continue; }
    text = text.replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "");
    text = text.replace(/\n?\[message_id:\s*\d+\]/gi, "").trim();
    if (text.startsWith("GatewayRestart:") || text.startsWith("System:")) { continue; }
    if (text.includes('"kind": "restart"')) { continue; }
    if (text.includes("HEARTBEAT") && text.includes("HEARTBEAT_OK")) { continue; }
    if (text.replace(/[*_<>\/b\s]/g, '').startsWith("HEARTBEAT_OK")) { continue; }
    if (!text) { continue; }
    transcript.push(`${msg.role === "user" ? "User" : "Alfie"}: ${text}`);
  }
  return transcript.join("\n\n");
}

function getMessageText(msg: any): string {
  if (!msg) { return ""; }
  if (typeof msg.content === "string") { return msg.content; }
  if (Array.isArray(msg.content)) {
    return msg.content.map((c: any) => c?.text || "").join(" ");
  }
  return "";
}

function isResetBootstrapOnlyConversation(messages: any[]): boolean {
  const RESET_BOOTSTRAP_PROMPT = "A new session was started via /new or /reset.";
  const userTexts = messages
    .filter((m: any) => m?.role === "user")
    .map((m: any) => getMessageText(m).trim())
    .filter(Boolean);
  if (userTexts.length === 0) { return false; }

  const nonBootstrapUserTexts = userTexts.filter((t: string) => !t.startsWith(RESET_BOOTSTRAP_PROMPT));
  return nonBootstrapUserTexts.length === 0;
}

// ============================================================================
// Session Status for User Notification
// ============================================================================

// ============================================================================
// Doc Auto-Update from Transcript
// ============================================================================

async function updateDocsFromTranscript(messages: any[], label: string, sessionId?: string): Promise<void> {
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
  const tmpPath = path.join(QUAID_TMP_DIR, `docs-ingest-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`);
  fs.writeFileSync(tmpPath, fullTranscript, { mode: 0o600 });
  try {
    console.log(`[quaid] ${label}: dispatching docs ingest event...`);
    const startTime = Date.now();
    const out = await emitEvent(
      "docs.ingest_transcript",
      {
        transcript_path: tmpPath,
        label,
        session_id: sessionId || null,
      },
      "immediate",
    );
    const result = out?.processed?.details?.[0]?.result?.result || out?.processed?.details?.[0]?.result || {};
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
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
  } catch (err: unknown) {
    console.error(`[quaid] ${label} doc update failed:`, (err as Error).message);
  } finally {
    try { fs.unlinkSync(tmpPath); } catch {}
  }
}

// ============================================================================
// Memory Operations
// ============================================================================

type MemoryResult = {
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
  relation?: string; // For graph-related nodes
  direction?: string; // "out" or "in" for graph edges
  sourceName?: string; // Source node name for graph edges
  via?: string; // "vector" | "graph" | "journal" | "project"
};

function parseDomainsValue(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.map((d) => String(d || "").trim()).filter(Boolean);
  }
  if (typeof raw === "string") {
    const trimmed = raw.trim();
    if (!trimmed) return [];
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) {
        return parsed.map((d) => String(d || "").trim()).filter(Boolean);
      }
    } catch {
      return [];
    }
  }
  return [];
}

function getConfiguredDomainIds(): string[] {
  try {
    const defs = getMemoryConfig()?.retrieval?.domains;
    if (defs && typeof defs === "object" && !Array.isArray(defs)) {
      return Object.keys(defs).map((k) => String(k).trim()).filter(Boolean).sort();
    }
  } catch {}
  return [];
}

type KnowledgeDatastore = "vector" | "vector_basic" | "vector_technical" | "graph" | "journal" | "project";
type DomainFilter = Record<string, boolean>;

function isLowInformationEntityNode(result: MemoryResult): boolean {
  if ((result.via || "vector") === "graph" || result.category === "graph") return false;
  const category = String(result.category || "").toLowerCase();
  if (!["person", "concept", "event", "entity"].includes(category)) return false;
  const text = String(result.text || "").trim();
  if (!text) return true;
  const words = text.split(/\s+/).filter(Boolean);
  // Entity stub: bare name/token without relational/factual structure.
  if (words.length <= 2 && /^[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?$/.test(text)) return true;
  return false;
}

const RECALL_RETRY_STOPWORDS = new Set([
  "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "how", "i",
  "in", "is", "it", "me", "my", "of", "on", "or", "our", "that", "the", "their", "they",
  "this", "to", "was", "we", "what", "when", "where", "which", "who", "why", "with", "you", "your",
]);

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

  const vectorResults = results.filter((r) => {
    return isVectorRecallResult(r);
  });
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

async function recall(
  query: string,
  limit: number = 5,
  currentSessionId?: string,
  compactionTime?: string,
  expandGraph: boolean = true,
  graphDepth: number = 1,
  domain: DomainFilter = { all: true },
  project?: string,
  dateFrom?: string,
  dateTo?: string
): Promise<MemoryResult[]> {
  try {
    const args = [query, "--limit", String(limit), "--owner", resolveOwner()];
    args.push("--domain-filter", JSON.stringify(domain || { all: true }));
    if (project && String(project).trim()) {
      args.push("--project", String(project).trim());
    }

    // Use search-graph-aware for enhanced graph traversal, or basic search
    if (!expandGraph) {
      // Basic search without graph expansion — accepts --current-session-id, --compaction-time
      args.push("--json");
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
      const output = await datastoreBridge.search(args);
      try {
        const parsed = JSON.parse(output);
        if (!Array.isArray(parsed)) {
          throw new Error("search output JSON must be an array");
        }
        const results: MemoryResult[] = [];
        for (const row of parsed) {
          if (!row || typeof row !== "object") continue;
          const text = typeof row.text === "string" ? row.text.trim() : "";
          const category = typeof row.category === "string" ? row.category : "fact";
          const similarity = typeof row.similarity === "number" ? row.similarity : Number(row.similarity ?? 0);
          if (!text || !Number.isFinite(similarity)) continue;
          const extractionConfidenceRaw = row.extraction_confidence ?? row.extractionConfidence;
          const extractionConfidence = typeof extractionConfidenceRaw === "number"
            ? extractionConfidenceRaw
            : Number(extractionConfidenceRaw ?? 0.5);
          results.push({
            text,
            category,
            similarity,
            domains: parseDomainsValue((row as Record<string, unknown>).domains),
            extractionConfidence: Number.isFinite(extractionConfidence) ? extractionConfidence : 0.5,
            id: typeof row.id === "string" ? row.id : undefined,
            createdAt: typeof row.created_at === "string" ? row.created_at : undefined,
            validFrom: typeof row.valid_from === "string" ? row.valid_from : undefined,
            validUntil: typeof row.valid_until === "string" ? row.valid_until : undefined,
            privacy: typeof row.privacy === "string" ? row.privacy : "shared",
            ownerId: typeof row.owner_id === "string" ? row.owner_id : undefined,
            sourceType: typeof row.source_type === "string" ? row.source_type : undefined,
            via: "vector",
          });
        }
        return results.slice(0, limit);
      } catch (err) {
        throw new Error(`Failed to parse datastore search JSON output: ${(err as Error).message}`);
      }
    }

    // Use graph-aware search with JSON output — accepts --depth
    if (graphDepth > 1) {
      args.push("--depth", String(graphDepth));
    }
    args.push("--json");
    const output = await datastoreBridge.searchGraphAware(args);
    const results: MemoryResult[] = [];

    try {
      const parsedRaw = JSON.parse(output);
      if (!parsedRaw || typeof parsedRaw !== "object" || Array.isArray(parsedRaw)) {
        throw new Error("graph-aware search output must be a JSON object");
      }
      const parsed = parsedRaw as Record<string, unknown>;
      const directResults = Array.isArray(parsed.direct_results) ? parsed.direct_results : [];
      const graphResults = Array.isArray(parsed.graph_results) ? parsed.graph_results : [];

      // Add direct (vector) results
      for (const row of directResults) {
        if (!row || typeof row !== "object") continue;
        const r = row as Record<string, unknown>;
        const text = typeof r.text === "string" ? r.text.trim() : "";
        const category = typeof r.category === "string" ? r.category : "fact";
        const similarity = typeof r.similarity === "number" ? r.similarity : Number(r.similarity ?? 0);
        if (!text || !Number.isFinite(similarity)) continue;
        const extractionConfidence = typeof r.extraction_confidence === "number"
          ? r.extraction_confidence
          : Number(r.extraction_confidence ?? 0.5);
        results.push({
          text,
          category,
          similarity,
          domains: parseDomainsValue(r.domains),
          id: typeof r.id === "string" ? r.id : undefined,
          extractionConfidence: Number.isFinite(extractionConfidence) ? extractionConfidence : 0.5,
          createdAt: typeof r.created_at === "string" ? r.created_at : undefined,
          validFrom: typeof r.valid_from === "string" ? r.valid_from : undefined,
          validUntil: typeof r.valid_until === "string" ? r.valid_until : undefined,
          privacy: typeof r.privacy === "string" ? r.privacy : undefined,
          ownerId: typeof r.owner_id === "string" ? r.owner_id : undefined,
          sourceType: typeof r.source_type === "string" ? r.source_type : undefined,
          verified: typeof r.verified === "boolean" ? r.verified : undefined,
          via: "vector",
        });
      }

      // Add graph results
      for (const row of graphResults) {
        if (!row || typeof row !== "object") continue;
        const r = row as Record<string, unknown>;
        const id = typeof r.id === "string" ? r.id : "";
        const name = typeof r.name === "string" ? r.name : "";
        const relation = typeof r.relation === "string" ? r.relation : "";
        const direction = typeof r.direction === "string" ? r.direction : "out";
        const sourceName = typeof r.source_name === "string" ? r.source_name : "";
        if (!id || !name || !relation || !sourceName) continue;

        // Format: "Source --relation--> Target" or reversed for inbound
        const text = direction === "in"
          ? `${name} --${relation}--> ${sourceName}`
          : `${sourceName} --${relation}--> ${name}`;

        results.push({
          text,
          category: "graph",
          similarity: 0.75, // Graph results get a fixed medium-high similarity
          id,
          relation,
          direction,
          sourceName,
          via: "graph",
        });
      }
    } catch (_parseErr: unknown) {
      // Fallback: parse line-by-line output format if JSON parsing fails
      console.warn(`[quaid] JSON parse failed, trying line format: ${String((_parseErr as Error)?.message || _parseErr)}`);
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
  } catch (err: unknown) {
    if (isFailHardEnabled()) {
      throw err;
    }
    console.error("[quaid] recall error:", (err as Error).message);
    return [];
  }
}

const knowledgeEngine = createKnowledgeEngine<MemoryResult>({
  workspace: WORKSPACE,
  getMemoryConfig,
  isSystemEnabled,
  getProjectCatalog,
  callFastRouter: async (systemPrompt: string, userPrompt: string) => {
    const llm = await callConfiguredLLM(systemPrompt, userPrompt, "fast", 120, FAST_ROUTER_TIMEOUT_MS);
    return String(llm?.text || "");
  },
  callDeepRouter: async (systemPrompt: string, userPrompt: string) => {
    const llm = await callConfiguredLLM(systemPrompt, userPrompt, "deep", 160, DEEP_ROUTER_TIMEOUT_MS);
    return String(llm?.text || "");
  },
  recallVector: async (query, limit, scope, project, dateFrom, dateTo) => {
    const memoryResults = await recall(
      query,
      limit,
      undefined,
      undefined,
      false,
      1,
      scope,
      project,
      dateFrom,
      dateTo
    );
    return memoryResults.map((r) => ({ ...r, via: "vector" as const }));
  },
  recallGraph: async (query, limit, depth, scope, project, dateFrom, dateTo) => {
    const graphResults = await recall(
      query,
      limit,
      undefined,
      undefined,
      true,
      depth,
      scope,
      project,
      dateFrom,
      dateTo
    );
    return graphResults
      .filter((r) => (r.via || "") === "graph" || r.category === "graph")
      .map((r) => ({ ...r, via: "graph" as const }));
  },
  recallJournalStore: async (query, limit) => {
    const journalConfig = getMemoryConfig().docs?.journal || {};
    const journalDir = path.join(WORKSPACE, journalConfig.journalDir || "journal");
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
      if (isFailHardEnabled()) {
        throw new Error("[quaid] Journal recall listing failed under failHard", { cause: err as Error });
      }
      console.warn(`[quaid] Journal recall listing failed: ${String((err as Error)?.message || err)}`);
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
        if (isFailHardEnabled()) {
          throw new Error(`[quaid] Journal recall read failed for ${file} under failHard`, { cause: err as Error });
        }
        console.warn(`[quaid] Journal recall read failed for ${file}: ${String((err as Error)?.message || err)}`);
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
      const results: MemoryResult[] = [];
      const projectVotes = new Map<string, number>();
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
          via: "project",
        });
      }
      // If project was not specified, infer it from strongest RAG hits and pull PROJECT.md
      // so downstream answer synthesis has higher-level architecture context.
      let inferredProject = project;
      if (!inferredProject && projectVotes.size > 0) {
        inferredProject = Array.from(projectVotes.entries())
          .sort((a, b) => b[1] - a[1])[0]?.[0];
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
                  via: "project",
                });
              }
            }
          }
        } catch (err: unknown) {
          console.warn(`[quaid] PROJECT.md context preload failed for ${inferredProject}: ${String((err as Error)?.message || err)}`);
        }
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
    } catch (err: unknown) {
      if (isFailHardEnabled()) {
        throw err;
      }
      console.warn("[quaid] project recall bridge error:", (err as Error)?.message || String(err));
      return [];
    }
  },
});

function normalizeKnowledgeDatastores(datastores: unknown, expandGraph: boolean): KnowledgeDatastore[] {
  return knowledgeEngine.normalizeKnowledgeDatastores(datastores, expandGraph);
}
const recallStoreGuidance = knowledgeEngine.renderKnowledgeDatastoreGuidanceForAgents();

async function totalRecall(
  query: string,
  limit: number,
  opts: {
    datastores: KnowledgeDatastore[];
    expandGraph: boolean;
    graphDepth: number;
    intent?: "general" | "agent_actions" | "relationship" | "technical";
    ranking?: { sourceTypeBoosts?: Record<string, number> };
    domain: DomainFilter;
    project?: string;
    dateFrom?: string;
    dateTo?: string;
    docs?: string[];
    datastoreOptions?: Partial<Record<KnowledgeDatastore, Record<string, unknown>>>;
  }
): Promise<MemoryResult[]> {
  return knowledgeEngine.totalRecall(query, limit, opts);
}

async function total_recall(
  query: string,
  limit: number,
  opts: {
    datastores: KnowledgeDatastore[];
    expandGraph: boolean;
    graphDepth: number;
    reasoning?: "fast" | "deep";
    intent?: "general" | "agent_actions" | "relationship" | "technical";
    ranking?: { sourceTypeBoosts?: Record<string, number> };
    domain: DomainFilter;
    project?: string;
    dateFrom?: string;
    dateTo?: string;
    docs?: string[];
    datastoreOptions?: Partial<Record<KnowledgeDatastore, Record<string, unknown>>>;
  }
): Promise<MemoryResult[]> {
  return knowledgeEngine.total_recall(query, limit, opts);
}

// Shared recall abstraction — used by both memory_recall tool and auto-inject
interface RecallOptions {
  query: string;
  limit?: number;
  expandGraph?: boolean;
  graphDepth?: number;
  domain?: DomainFilter;
  project?: string;
  datastores?: KnowledgeDatastore[];
  routeStores?: boolean;
  reasoning?: "fast" | "deep";
  intent?: "general" | "agent_actions" | "relationship" | "technical";
  ranking?: { sourceTypeBoosts?: Record<string, number> };
  dateFrom?: string;
  dateTo?: string;
  docs?: string[];
  datastoreOptions?: Partial<Record<KnowledgeDatastore, Record<string, unknown>>>;
  failOpen?: boolean;
  waitForExtraction?: boolean;  // wait on extractionPromise (tool=yes, inject=no)
  sourceTag?: "tool" | "auto_inject" | "unknown";
}

// Note: extractionPromise is declared inside register() — this function is
// defined below inside register() as well, so it has access to it.
// (Moved into register() closure when used)

type DatastoreStats = {
  total_nodes: number;
  edges: number;
};

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
  if (!Number.isFinite(totalNodes) || totalNodes < 0) {
    return null;
  }
  if (!Number.isFinite(edges) || edges < 0) {
    return null;
  }
  return {
    total_nodes: totalNodes,
    edges,
  };
}

async function getStats(): Promise<DatastoreStats | null> {
  try {
    const output = await datastoreBridge.stats();
    return parseDatastoreStats(output);
  } catch (err: unknown) {
    console.error("[quaid] stats error:", (err as Error).message);
    if (isFailHardEnabled()) {
      throw err;
    }
    return null;
  }
}

function formatMemories(memories: MemoryResult[]): string {
  if (!memories.length) { return ""; }

  // Sort by created_at ascending (oldest first, most recent last = closest to prompt)
  const sorted = [...memories].sort((a, b) => {
    if (!a.createdAt && !b.createdAt) { return 0; }
    if (!a.createdAt) { return -1; }
    if (!b.createdAt) { return 1; }
    return a.createdAt.localeCompare(b.createdAt);
  });

  const graphNodeHits = sorted.filter((m) => isLowInformationEntityNode(m));
  const regularMemories = sorted.filter((m) => !isLowInformationEntityNode(m));

  const lines = regularMemories.map((m) => {
    const conf = m.extractionConfidence ?? 0.5;
    const timestamp = m.createdAt ? ` (${m.createdAt.split("T")[0]})` : "";
    const domainLabel = Array.isArray(m.domains) && m.domains.length
      ? ` [domains:${m.domains.join(",")}]`
      : "";
    if (conf < 0.4) {
      return `- [${m.category}]${timestamp}${domainLabel} (uncertain) ${m.text}`;
    }
    return `- [${m.category}]${timestamp}${domainLabel} ${m.text}`;
  });

  if (graphNodeHits.length > 0) {
    const packed = graphNodeHits
      .slice(0, 8)
      .map((m) => `${m.text} (${Math.round((m.similarity || 0) * 100)}%)`)
      .join(", ");
    lines.push(`- [graph-node-hits] Entity node references (not standalone facts): ${packed}`);
  }

  const configuredDomains = getConfiguredDomainIds();
  const domainGuidance = configuredDomains.length
    ? `AVAILABLE_DOMAINS: ${configuredDomains.join(", ")}`
    : "AVAILABLE_DOMAINS: (unavailable)";

  return `<injected_memories>
AUTOMATED MEMORY SYSTEM: The following memories were automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you. Use them as background context only. Items marked (uncertain) have lower extraction confidence. Dates shown are when the fact was recorded.
INJECTOR CONFIDENCE RULE: Treat injected memories as hints, not final truth. If the answer depends on personal details and the match is not exact/high-confidence, run memory_recall before answering.
DOMAIN RECALL RULE: Use memory_recall options.filters.domain (map of domain->bool). Example: {"technical": true}. Use domain filters only.
${domainGuidance}
${lines.join("\n")}
</injected_memories>`;
}

// ============================================================================
// Plugin Definition
// ============================================================================

const quaidPlugin = {
  id: "quaid",
  name: "Memory (Local Graph)",
  description: "Local graph-based memory with SQLite + Ollama embeddings",
  kind: "memory" as const,
  configSchema,

  register(api: ClawdbotPluginApi<PluginConfig>) {
    console.log("[quaid] Registering local graph memory plugin");

    // Fail fast on model/provider/config mismatches so runtime doesn't degrade silently.
    runStartupSelfCheck();

    // Ensure database exists
    const dataDir = path.dirname(DB_PATH);
    if (!fs.existsSync(dataDir)) {
      fs.mkdirSync(dataDir, { recursive: true });
    }

    // Check if database needs seeding
    if (!fs.existsSync(DB_PATH)) {
      console.log("[quaid] Database not found, initializing datastore...");
      try {
        execFileSync("python3", [PYTHON_SCRIPT, "init"], {
          timeout: 20_000,
          env: buildPythonEnv(),
        });
        console.log("[quaid] Datastore initialization complete");
      } catch (err: unknown) {
        console.error("[quaid] Datastore initialization failed:", (err as Error).message);
        if (isFailHardEnabled()) {
          throw err;
        }
      }
    }

    // Log stats
    void getStats()
      .then((stats) => {
        if (stats) {
          console.log(
            `[quaid] Database ready: ${stats.total_nodes} nodes, ${stats.edges} edges`
          );
        }
      })
      .catch((err: unknown) => {
        console.warn(
          `[quaid] stats probe failed: ${String((err as Error)?.message || err)}`
        );
      });

    // Register lifecycle hooks.
    const beforeAgentStartHandler = async (event: any, ctx: any) => {
      if (isInternalQuaidSession(ctx?.sessionId)) {
        return;
      }
      try { maybeSendJanitorNudges(); } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Janitor nudge dispatch failed: ${String((err as Error)?.message || err)}`);
      }
      try { maybeQueueJanitorHealthAlert(); } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Janitor health alert dispatch failed: ${String((err as Error)?.message || err)}`);
      }
      // Cancel inactivity timer — agent is active again
      timeoutManager.onAgentStart();

      // Journal injection (full soul mode) — gated by journal system
      if (!isSystemEnabled("journal")) {
        // Skip journal injection entirely
      } else {
      try {
        const journalConfig = getMemoryConfig().docs?.journal || {};
        const journalMode = journalConfig.mode || "distilled";

        if (journalMode === "full") {
          const journalDir = path.join(WORKSPACE, journalConfig.journalDir || "journal");
          let journalFiles: string[] = [];
          try {
            journalFiles = fs.readdirSync(journalDir).filter((f: string) => f.endsWith('.journal.md')).sort();
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw new Error("[quaid] Journal injection listing failed under failHard", { cause: err as Error });
            }
            console.warn(`[quaid] Journal injection listing failed: ${String((err as Error)?.message || err)}`);
          }

          let journalContent = '';
          for (const file of journalFiles) {
            try {
              const content = fs.readFileSync(path.join(journalDir, file), 'utf8');
              if (content.trim()) {
                journalContent += `\n\n--- ${file} ---\n${content}`;
              }
            } catch (err: unknown) {
              if (isFailHardEnabled()) {
                throw new Error(`[quaid] Journal injection read failed for ${file} under failHard`, { cause: err as Error });
              }
              console.warn(`[quaid] Journal injection read failed for ${file}: ${String((err as Error)?.message || err)}`);
            }
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
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Journal injection failed (non-fatal): ${(err as Error).message}`);
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
        if (query.length < 3) { query = rawPrompt; }

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
        // direct datastores. For project/technical prompts, include technical/project
        // sources explicitly so implementation facts are not filtered out.
        // Dynamic K: 2 * log2(nodeCount) — scales with graph size
        const autoInjectK = computeDynamicK();
        const useTotalRecallForInject = isPreInjectionPassEnabled();
        const routerFailOpen = Boolean(
          getMemoryConfig().retrieval?.routerFailOpen ??
          getMemoryConfig().retrieval?.router_fail_open ??
          true
        );
        const injectLimit = autoInjectK;
        const injectIntent: "general" = "general";
        const injectDomain: DomainFilter = { personal: true };
        const injectDatastores: KnowledgeDatastore[] | undefined = useTotalRecallForInject
          ? undefined
          : ["vector_basic", "graph"];
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

        // Privacy filter — allow through if: not private, or owned by current user, or no owner set
        // Note: Python serializes None as string "None", not JSON null
        const currentOwner = resolveOwner();
        const filtered = allMemories.filter(m =>
          !(m.privacy === "private" && m.ownerId && m.ownerId !== "None" && m.ownerId !== currentOwner)
        );

        // Session dedup (don't re-inject same facts within a session)
        const uniqueSessionId = extractSessionId(event.messages || [], ctx);
        const injectionLogPath = getInjectionLogPath(uniqueSessionId);
        let previouslyInjected: string[] = [];
        try {
          const parsed = JSON.parse(fs.readFileSync(injectionLogPath, "utf8"));
          const logData = parsed && typeof parsed === "object" && !Array.isArray(parsed)
            ? (parsed as Record<string, unknown>)
            : null;
          const rawInjected = logData?.injected ?? logData?.memoryTexts;
          previouslyInjected = Array.isArray(rawInjected)
            ? rawInjected.map((item) => String(item || "").trim()).filter(Boolean)
            : [];
        } catch (err: unknown) {
          console.warn(`[quaid] Injection log read failed for ${injectionLogPath}: ${String((err as Error)?.message || err)}`);
        }
        const newMemories = filtered.filter(m => !previouslyInjected.includes(m.id || m.text));

        // Cap and format — use dynamic K for injection cap too
        const toInject = newMemories.slice(0, injectLimit);
        if (!toInject.length) return;

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
            const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
            if (!launchedNotify) {
              try { fs.unlinkSync(dataFile); } catch {}
            }
            console.log("[quaid] Auto-inject recall notification dispatched");
          }
        } catch (notifyErr: unknown) {
          console.warn(`[quaid] Auto-inject recall notification skipped: ${(notifyErr as Error).message}`);
        }

        // Update injection log
        try {
          const newIds = toInject.map(m => m.id || m.text);
          const mergedIds = [...previouslyInjected, ...newIds];
          fs.writeFileSync(injectionLogPath, JSON.stringify({
            injected: mergedIds.slice(-MAX_INJECTION_IDS_PER_SESSION),
            lastInjectedAt: new Date().toISOString()
          }), { mode: 0o600 });
          pruneInjectionLogFiles();
        } catch (err: unknown) {
          console.warn(`[quaid] Injection log write failed for ${injectionLogPath}: ${String((err as Error)?.message || err)}`);
        }
      } catch (error: unknown) {
        console.error("[quaid] Auto-injection error:", error);
      }
    };

    // Register hooks using api.on() for typed hooks (NOT api.registerHook!)
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    api.on("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });

    // End-of-turn hook drives timeout scheduling and extraction signaling.
    const agentEndHandler = async (event: any, ctx: any) => {
      if (isInternalQuaidSession(ctx?.sessionId)) return;
      if (!isSystemEnabled("memory")) return;
      const messages = event.messages || [];
      if (messages.length === 0) return;
      const conversationMessages = getAllConversationMessages(messages);
      if (conversationMessages.length === 0) return;
      const timeoutSessionId = ctx?.sessionId || extractSessionId(messages, ctx);
      timeoutManager.setTimeoutMinutes(getCaptureTimeoutMinutes());
      // Adapter forwards conversation messages; core manages session log lifecycle + dedup.
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
          } catch (err: unknown) {
            console.error(`[quaid] ${transcriptTrigger} doc update fallback failed:`, (err as Error).message);
          }
          try {
            await emitProjectEvent(conversationMessages, trigger, timeoutSessionId);
          } catch (err: unknown) {
            console.error(`[quaid] ${transcriptTrigger} project event fallback failed:`, (err as Error).message);
          }
        })();
      }

    };

    // Register agent_end hook using api.on() for typed hooks
    console.log("[quaid] Registering agent_end hook for auto-capture");
    api.on("agent_end", agentEndHandler, {
      name: "auto-capture",
      priority: 10
    });

    // Register memory tools (gated by memory system)
    if (isSystemEnabled("memory")) {
    api.registerTool(
      () => ({
        name: "memory_recall",
        description: `Search your memory for personal facts, preferences, relationships, project details, and past conversations. Always use this tool when you're unsure about something or need to verify a detail — if you might know it, search for it.

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
                  Type.Literal("project"),
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
              ),
            })),
            routing: Type.Optional(Type.Object({
              enabled: Type.Optional(
                Type.Boolean({ description: "Enable total_recall planning pass (query cleanup + store routing)." })
              ),
              reasoning: Type.Optional(
                Type.Union([
                  Type.Literal("fast"),
                  Type.Literal("deep"),
                ], { description: "Reasoning model for routing pass." })
              ),
              intent: Type.Optional(
                Type.Union([
                  Type.Literal("general"),
                  Type.Literal("agent_actions"),
                  Type.Literal("relationship"),
                  Type.Literal("technical"),
                ], { description: "Intent facet for routing and ranking boosts." })
              ),
              failOpen: Type.Optional(
                Type.Boolean({ description: "If true, router/prepass failures return no recall instead of throwing an error." })
              ),
            })),
            filters: Type.Optional(Type.Object({
              domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean(), description: "Domain filter map. Example: {\"all\":true} or {\"technical\":true}." })),
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
              ),
            })),
            ranking: Type.Optional(Type.Object({
              sourceTypeBoosts: Type.Optional(Type.Object({
                user: Type.Optional(Type.Number()),
                assistant: Type.Optional(Type.Number()),
                both: Type.Optional(Type.Number()),
                tool: Type.Optional(Type.Number()),
                import: Type.Optional(Type.Number()),
              })),
            })),
            datastoreOptions: Type.Optional(Type.Object({
              vector: Type.Optional(Type.Object({
                domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean() })),
                project: Type.Optional(Type.String()),
              })),
              graph: Type.Optional(Type.Object({
                depth: Type.Optional(Type.Number()),
                domain: Type.Optional(Type.Object({}, { additionalProperties: Type.Boolean() })),
                project: Type.Optional(Type.String()),
              })),
              project: Type.Optional(Type.Object({
                project: Type.Optional(Type.String()),
                docs: Type.Optional(Type.Array(Type.String())),
              })),
              journal: Type.Optional(Type.Object({})),
              vector_basic: Type.Optional(Type.Object({})),
              vector_technical: Type.Optional(Type.Object({})),
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
            } catch (err: unknown) {
              console.warn(`[quaid] memory_recall maxLimit config read failed: ${String((err as Error)?.message || err)}`);
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
            const domain = (options.filters?.domain && typeof options.filters.domain === "object")
              ? options.filters.domain
              : { all: true };
            const dateFrom = options.filters?.dateFrom;
            const dateTo = options.filters?.dateTo;
            const project = options.filters?.project;
            const docs = options.filters?.docs;
            const ranking = options.ranking;
            const datastoreOptions = options.datastoreOptions;
            const routerFailOpen = Boolean(
              options.routing?.failOpen ??
              getMemoryConfig().retrieval?.routerFailOpen ??
              getMemoryConfig().retrieval?.router_fail_open ??
              true
            );
            if (typeof query === "string" && query.trim().startsWith("Extract memorable facts and journal entries from this conversation:")) {
              return {
                content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                details: { count: 0, skippedInternalQuery: true },
              };
            }
            const limit = Math.min(requestedLimit ?? dynamicK, maxLimit);
            const depth = Math.min(Math.max(graphDepth, 1), 3); // Clamp between 1 and 3
            const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
            const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);

            console.log(`[quaid] memory_recall: query="${query?.slice(0, 50)}...", requestedLimit=${requestedLimit}, dynamicK=${dynamicK} (${getActiveNodeCount()} nodes), maxLimit=${maxLimit}, finalLimit=${limit}, expandGraph=${expandGraph}, graphDepth=${depth}, requestedDatastores=${selectedStores.join(",")}, routed=${shouldRouteStores}, reasoning=${reasoning}, intent=${intent}, domain=${JSON.stringify(domain)}, project=${project || "any"}, dateFrom=${dateFrom}, dateTo=${dateTo}`);
            const results = await recallMemories({
              query, limit, expandGraph, graphDepth: depth, datastores: selectedStores, routeStores: shouldRouteStores, reasoning, intent, ranking, domain,
              project, datastoreOptions,
              failOpen: routerFailOpen,
              dateFrom, dateTo, docs, waitForExtraction: true, sourceTag: "tool"
            });

            if (results.length === 0) {
              return {
                content: [{ type: "text", text: "No relevant memories found. Try different keywords or entity names." }],
                details: { count: 0 },
              };
            }

            // Group by source type for better formatting
            const vectorResults = results.filter((r) => isVectorRecallResult(r));
            const graphResults = results.filter(r => (r.via || "") === "graph" || r.category === "graph");
            const journalResults = results.filter(r => (r.via || "") === "journal");
            const projectResults = results.filter(r => (r.via || "") === "project");

            // Keep low-confidence warnings conservative; avoid warning on clearly useful recalls.
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
                text += `${i + 1}. [MEMORY] [${r.category}]${dateStr}${superseded} ${r.text} (${Math.round(r.similarity * 100)}%${conf})\n`;
              });
            }

            if (graphResults.length > 0) {
              if (vectorResults.length > 0) { text += "\n"; }
              text += "**Graph Discoveries:**\n";
              graphResults.forEach((r, i) => {
                text += `${i + 1}. [MEMORY] ${r.text}\n`;
              });
            }

            if (journalResults.length > 0) {
              if (vectorResults.length > 0 || graphResults.length > 0) { text += "\n"; }
              text += "**Journal Signals:**\n";
              journalResults.forEach((r, i) => {
                text += `${i + 1}. [MEMORY] ${r.text} (${Math.round((r.similarity || 0) * 100)}%)\n`;
              });
            }

            if (projectResults.length > 0) {
              if (vectorResults.length > 0 || graphResults.length > 0 || journalResults.length > 0) { text += "\n"; }
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
                // Build source breakdown for notification
                const sourceBreakdown = {
                  vector_count: vectorResults.length,
                  graph_count: graphResults.length,
                  journal_count: journalResults.length,
                  project_count: projectResults.length,
                  query: query,
                  mode: "tool",
                };

                // Fire and forget notification
                const dataFile2 = path.join(QUAID_TMP_DIR, `recall-data-${Date.now()}.json`);
                fs.writeFileSync(dataFile2, JSON.stringify({ memories: memoryData, source_breakdown: sourceBreakdown }), { mode: 0o600 });
                const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_memory_recall
with open(${JSON.stringify(dataFile2)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile2)})
notify_memory_recall(data['memories'], source_breakdown=data['source_breakdown'])
`);
                if (!launchedNotify) {
                  try { fs.unlinkSync(dataFile2); } catch {}
                }
              }
            } catch (notifyErr: unknown) {
              // Notification is best-effort
              console.warn(`[quaid] Memory recall notification skipped: ${(notifyErr as Error).message}`);
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
          } catch (err: unknown) {
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
                error_cause: errObj.cause ? String((errObj.cause as Error)?.message || errObj.cause) : undefined,
              },
            };
          }
        },
      }),
    );

    api.registerTool(
      () => ({
        name: "memory_store",
        description: `Queue a fact for memory extraction at next compaction. The fact will go through full quality review (Opus extraction with edges and janitor review) rather than being stored directly.

Only use when the user EXPLICITLY asks you to remember something (e.g., "remember this", "save this"). Do NOT proactively store facts — auto-extraction at compaction handles that.`,
        parameters: Type.Object({
          text: Type.String({ description: "Information to remember" }),
          category: Type.Optional(
            Type.String({
              enum: ["preference", "fact", "decision", "entity", "other"],
            })
          ),
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
          } catch (err: unknown) {
            console.error("[quaid] memory_store error:", err);
            if (isFailHardEnabled()) {
              throw err;
            }
            return {
              content: [{ type: "text", text: `Error queuing memory note: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
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
          ),
        }),
        async execute(_toolCallId, params) {
          try {
            const { query, memoryId } = params || {};
            if (memoryId) {
              await datastoreBridge.forget(["--id", memoryId]);
              return {
                content: [{ type: "text", text: `Memory ${memoryId} forgotten.` }],
                details: { action: "deleted", id: memoryId },
              };
            } else if (query) {
              await datastoreBridge.forget([query]);
              return {
                content: [{ type: "text", text: `Deleted memories matching: "${query}"` }],
                details: { action: "deleted", query },
              };
            }
            return {
              content: [{ type: "text", text: "Provide query or memoryId." }],
              details: { error: "missing_param" },
            };
          } catch (err: unknown) {
            console.error("[quaid] memory_forget error:", err);
            if (isFailHardEnabled()) {
              throw err;
            }
            return {
              content: [{ type: "text", text: `Error deleting memory: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );
    } // end memory system gate

    // Register projects_search tool — semantic search over project documentation
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
          ),
        }),
        async execute(_toolCallId, params) {
          try {
            const { query, limit = 5, project, docs } = params || {};

            // RAG search with optional project filter
            const searchArgs = [query, "--limit", String(limit)];
            if (project) { searchArgs.push("--project", project); }
            if (Array.isArray(docs) && docs.length > 0) { searchArgs.push("--docs", docs.join(",")); }
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
              } catch (err: unknown) {
                console.warn(`[quaid] projects_search PROJECT.md preload failed: ${String((err as Error)?.message || err)}`);
              }
            }

            // Staleness check (lightweight mtime comparison)
            let stalenessWarning = "";
            try {
              const stalenessJson = await callDocsUpdater("check", ["--json"]);
              const staleRaw = JSON.parse(stalenessJson || "{}");
              const staleDocs = staleRaw && typeof staleRaw === "object" && !Array.isArray(staleRaw)
                ? (staleRaw as Record<string, any>)
                : {};
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
                stalenessWarning = `\n\nSTALENESS WARNING: The following docs may be outdated:\n${warnings.join("\n")}\nConsider running: python3 docs_updater.py update-stale --apply`;
              }
            } catch (err: unknown) {
              console.warn(`[quaid] projects_search staleness check failed: ${String((err as Error)?.message || err)}`);
            }

            const text = projectMdContent
              ? `--- PROJECT.md (${project}) ---\n${projectMdContent}\n\n--- Search Results ---\n${results || "No results."}${stalenessWarning}`
              : (results ? results + stalenessWarning : "No results found." + stalenessWarning);

            // Notify user about what docs were searched (if enabled)
            try {
              if (shouldNotifyFeature("retrieval", "summary") && results) {
                // Parse results to extract doc names and scores
                const docResults: Array<{doc: string, section: string, score: number}> = [];
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
                  const launchedNotify = spawnNotifyScript(`
import json
from core.runtime.notify import notify_docs_search
with open(${JSON.stringify(dataFile3)}, 'r') as f:
    data = json.load(f)
os.unlink(${JSON.stringify(dataFile3)})
notify_docs_search(data['query'], data['results'])
`);
                  if (!launchedNotify) {
                    try { fs.unlinkSync(dataFile3); } catch {}
                  }
                }
              }
            } catch (notifyErr: unknown) {
              // Notification is best-effort
              console.warn(`[quaid] Docs search notification skipped: ${(notifyErr as Error).message}`);
            }

            return {
              content: [{ type: "text", text }],
              details: { query, limit },
            };
          } catch (err: unknown) {
            console.error("[quaid] projects_search error:", err);
            if (isFailHardEnabled()) {
              throw err;
            }
            return {
              content: [{ type: "text", text: `Error searching docs: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register docs_read tool — read a document by path or title
    api.registerTool(
      () => ({
        name: "docs_read",
        description: "Read the full content of a registered document by file path or title.",
        parameters: Type.Object({
          identifier: Type.String({ description: "File path (workspace-relative) or document title" }),
        }),
        async execute(_toolCallId, params) {
          try {
            const { identifier } = params || {};
            const output = await callDocsRegistry("read", [identifier]);
            return {
              content: [{ type: "text", text: output || "Document not found." }],
              details: { identifier },
            };
          } catch (err: unknown) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register docs_list tool — list registered docs
    api.registerTool(
      () => ({
        name: "docs_list",
        description: "List registered documents, optionally filtered by project or type.",
        parameters: Type.Object({
          project: Type.Optional(Type.String({ description: "Filter by project name" })),
          type: Type.Optional(Type.String({ description: "Filter by asset type (doc, note, reference)" })),
        }),
        async execute(_toolCallId, params) {
          try {
            const args: string[] = ["--json"];
            if (params?.project) { args.push("--project", params.project); }
            if (params?.type) { args.push("--type", params.type); }
            const output = await callDocsRegistry("list", args);
            return {
              content: [{ type: "text", text: output || "No documents found." }],
              details: { project: params?.project },
            };
          } catch (err: unknown) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register docs_register tool — register a new document
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
          source_files: Type.Optional(Type.Array(Type.String(), { description: "Source files this doc tracks" })),
        }),
        async execute(_toolCallId, params) {
          try {
            const args: string[] = [params.file_path];
            if (params.project) { args.push("--project", params.project); }
            if (params.title) { args.push("--title", params.title); }
            if (params.description) { args.push("--description", params.description); }
            if (params.auto_update) { args.push("--auto-update"); }
            if (params.source_files) { args.push("--source-files", ...params.source_files); }
            args.push("--json");
            const output = await callDocsRegistry("register", args);
            return {
              content: [{ type: "text", text: output || "Registered." }],
              details: { file_path: params.file_path },
            };
          } catch (err: unknown) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register project_create tool — scaffold a new project
    api.registerTool(
      () => ({
        name: "project_create",
        description: "Create a new project with a PROJECT.md template. Sets up the directory structure and scaffolding.",
        parameters: Type.Object({
          name: Type.String({ description: "Project name (kebab-case, e.g., 'my-essay')" }),
          label: Type.Optional(Type.String({ description: "Display label (e.g., 'My Essay')" })),
          description: Type.Optional(Type.String({ description: "Project description" })),
          source_roots: Type.Optional(Type.Array(Type.String(), { description: "Source root directories" })),
        }),
        async execute(_toolCallId, params) {
          try {
            const args: string[] = [params.name];
            if (params.label) { args.push("--label", params.label); }
            if (params.description) { args.push("--description", params.description); }
            if (params.source_roots) { args.push("--source-roots", ...params.source_roots); }
            const output = await callDocsRegistry("create-project", args);
            return {
              content: [{ type: "text", text: output || `Project '${params.name}' created.` }],
              details: { name: params.name },
            };
          } catch (err: unknown) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register project_list tool — enumerate projects
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
              details: {},
            };
          } catch (err: unknown) {
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Register session_recall tool — list/load recent conversation sessions
    api.registerTool(
      () => ({
        name: "session_recall",
        description: `List or load recent conversation sessions. Use when the user wants to continue previous work, references a past conversation, or you need context about what was discussed recently.`,
        parameters: Type.Object({
          action: Type.String({ description: '"list" = recent sessions, "load" = specific session transcript' }),
          session_id: Type.Optional(Type.String({ description: "Session ID to load (for action=load)" })),
          limit: Type.Optional(Type.Number({ description: "How many recent sessions to list (default 5, for action=list)" })),
        }),
        async execute(_toolCallId, params) {
          try {
            const { action = "list", session_id: sid, limit: listLimit = 5 } = params || {};

            const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
            let extractionLog: Record<string, any> = {};
            try {
              const parsed = JSON.parse(fs.readFileSync(extractionLogPath, 'utf8'));
              if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                throw new Error("extraction log must be a JSON object");
              }
              extractionLog = parsed as Record<string, any>;
            } catch (err: unknown) {
              if (isFailHardEnabled() && !isMissingFileError(err)) {
                throw new Error("[quaid] session_recall extraction log read failed under failHard", { cause: err as Error });
              }
              console.warn(`[quaid] session_recall extraction log read failed: ${String((err as Error)?.message || err)}`);
            }

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
              // Validate session_id to prevent path traversal
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
                const factsOutput = await datastoreBridge.search([
                  "*", "--session-id", sid, "--owner", resolveOwner(), "--limit", "20"
                ]);
                return {
                  content: [{ type: "text", text: `Session file not available. Facts extracted from session ${sid}:\n${factsOutput || "No facts found."}` }],
                  details: { session_id: sid, fallback: true },
                };
              } catch {
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
          } catch (err: unknown) {
            console.error("[quaid] session_recall error:", err);
            return {
              content: [{ type: "text", text: `Error: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      })
    );

    // Extraction promise gate — memory_recall waits on this before querying
    // so that facts extracted from the just-compacted session are available.
    let extractionPromise: Promise<void> | null = null;
    const queueExtractionTask = (task: () => Promise<void>, source: string): Promise<void> => {
      const prior = extractionPromise || Promise.resolve();
      extractionPromise = prior.then(
        () => task(),
        async (err: unknown) => {
          const msg = (err as Error)?.message || String(err);
          console.error(`[quaid] extraction chain prior failure (${source}): ${msg}`);
          if (isFailHardEnabled()) {
            throw err;
          }
          await task();
          return;
        },
      );
      return extractionPromise;
    };
    const timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      timeoutMinutes: getCaptureTimeoutMinutes(),
      isBootstrapOnly: isResetBootstrapOnlyConversation,
      logger: (msg: string) => {
        const lowered = String(msg || "").toLowerCase();
        if (lowered.includes("fail") || lowered.includes("error")) {
          console.warn(msg);
          return;
        }
        console.log(msg);
      },
      extract: async (msgs: any[], sid?: string, label?: string) => {
        extractionPromise = queueExtractionTask(
          () => extractMemoriesFromMessages(msgs, label || "Timeout", sid),
          "timeout",
        );
        await extractionPromise;
      },
    });

    // Shared recall abstraction — used by both memory_recall tool and auto-inject
    async function recallMemories(opts: RecallOptions): Promise<MemoryResult[]> {
      const {
        query, limit = 10, expandGraph = false,
        graphDepth = 1, datastores, routeStores = false, reasoning = "fast", intent = "general", ranking, domain = { all: true }, project, dateFrom, dateTo, docs, datastoreOptions, waitForExtraction = false, sourceTag = "unknown"
      } = opts;
      const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);

      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${selectedStores.join(",")} routed=${routeStores} reasoning=${reasoning} intent=${intent} domain=${JSON.stringify(domain)} project=${project || "any"} waitForExtraction=${waitForExtraction}`
      );

      // Wait for in-flight extraction if requested
      if (waitForExtraction && extractionPromise) {
        let raceTimer: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            extractionPromise,
            new Promise<void>((_, rej) => { raceTimer = setTimeout(() => rej(new Error("timeout")), 60_000); })
          ]);
        } catch (err: unknown) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.warn(
            `[quaid][recall] waitForExtraction degraded: ${String((err as Error)?.message || err)}`
          );
        } finally {
          if (raceTimer) clearTimeout(raceTimer);
        }
      }

      const runRecall = (q: string): Promise<MemoryResult[]> => {
        if (routeStores) {
          return total_recall(q, limit, {
            datastores: selectedStores,
            expandGraph,
            graphDepth,
            reasoning,
            intent,
            ranking,
            domain,
            project,
            dateFrom,
            dateTo,
            docs,
            datastoreOptions,
          });
        }
        return totalRecall(q, limit, {
          datastores: selectedStores,
          expandGraph,
          graphDepth,
          intent,
          ranking,
          domain,
          project,
          dateFrom,
          dateTo,
          docs,
          datastoreOptions,
        });
      };

      const primary = await runRecall(query);
      if (sourceTag !== "tool") {
        return primary;
      }
      const retryDecision = shouldRetryRecall(query, primary);
      if (!retryDecision.retry) {
        return primary;
      }
      const expanded = buildExpandedRecallQuery(query);
      if (expanded === query) {
        return primary;
      }

      console.log(
        `[quaid][recall] retry source=${sourceTag} reasons=${retryDecision.reasons.join(",")} expanded="${expanded.slice(0, 160)}"`
      );
      const secondary = await runRecall(expanded);
      return mergeRecallResults(primary, secondary, limit);
    }

    // Read messages from a session JSONL file (same format as recovery scan)
    function readMessagesFromSessionFile(sessionFile: string): any[] {
      const content = fs.readFileSync(sessionFile, 'utf8');
      const lines = content.trim().split('\n');
      const messages: any[] = [];
      for (const line of lines) {
        try {
          const entry = JSON.parse(line);
          // Session JSONL has entries like { type: "message", message: { role, content } }
          // and also direct message objects { role, content }
          if (entry.type === 'message' && entry.message) {
            messages.push(entry.message);
          } else if (entry.role) {
            messages.push(entry);
          }
        } catch (err: unknown) {
          console.warn(`[quaid] session file line parse failed: ${String((err as Error)?.message || err)}`);
        }
      }
      return messages;
    }

    // Shared memory extraction logic — used by both compaction and reset hooks
    const extractMemoriesFromMessages = async (messages: any[], label: string, sessionId?: string) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        return;
      }

      const hasRestart = messages.some((m: any) => {
        const content = typeof m.content === "string" ? m.content : "";
        return content.startsWith("GatewayRestart:");
      });
      if (hasRestart) {
        console.log(`[quaid][extract] ${label}: detected GatewayRestart marker; scheduling recovery scan`);
        void checkForUnextractedSessions().catch((err: unknown) => {
          console.error("[quaid] Recovery scan error:", err);
        });
      }

      const sessionNotes = sessionId ? getAndClearMemoryNotes(sessionId) : [];
      // Avoid cross-session leakage: extraction only consumes notes for the active session.
      const allNotes = Array.from(new Set([...sessionNotes]));
      if (allNotes.length > 0) {
        console.log(`[quaid] ${label}: prepend ${allNotes.length} queued memory note(s)`);
      }

      const fullTranscript = buildTranscript(messages);
      if (!fullTranscript.trim() && allNotes.length === 0) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        return;
      }

      const transcriptForExtraction = allNotes.length > 0
        ? (
          "=== USER EXPLICITLY ASKED TO REMEMBER THESE (extract as high-confidence facts) ===\n" +
          `${allNotes.map((n) => `- ${n}`).join("\n")}\n` +
          "=== END EXPLICIT MEMORY REQUESTS ===\n\n" +
          fullTranscript
        )
        : fullTranscript;
      console.log(`[quaid] ${label} transcript: ${messages.length} messages, ${transcriptForExtraction.length} chars`);

      if (getMemoryConfig().notifications?.showProcessingStart !== false && shouldNotifyFeature("extraction", "summary")) {
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
from core.runtime.notify import notify_user
notify_user("🧠 Processing memories from ${triggerDesc}...")
`);
      }

      const journalConfig = getMemoryConfig().docs?.journal || {};
      const journalEnabled = isSystemEnabled("journal") && journalConfig.enabled !== false;
      const snippetsEnabled = journalEnabled && journalConfig.snippetsEnabled !== false;
      const triggerLabel = resolveExtractionTrigger(label);
      let extracted: any;
      try {
        extracted = await callExtractPipeline({
          transcript: transcriptForExtraction,
          owner: resolveOwner(),
          label: triggerLabel,
          sessionId,
          writeSnippets: snippetsEnabled,
          writeJournal: journalEnabled,
        });
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        console.error(`[quaid] ${label} extraction failed: ${msg}`);
        // Extraction is best-effort at this adapter boundary. Lower layers still
        // enforce failHard semantics where configured, but we avoid crashing the
        // active gateway/session loop on background extraction failures.
        return;
      }

      const stored = Number(extracted?.facts_stored || 0);
      const skipped = Number(extracted?.facts_skipped || 0);
      const edgesCreated = Number(extracted?.edges_created || 0);
      const factDetails: Array<{ text: string; status: string; reason?: string; edges?: string[] }> = Array.isArray(extracted?.facts)
        ? extracted.facts
        : [];
      console.log(`[quaid] ${label} extraction complete: ${stored} stored, ${skipped} skipped, ${edgesCreated} edges`);
      console.log(`[quaid][extract] done label=${label} session=${sessionId || "unknown"} stored=${stored} skipped=${skipped} edges=${edgesCreated}`);

      let snippetDetails: Record<string, string[]> = {};
      let journalDetails: Record<string, string[]> = {};
      try {
        const journalRaw = extracted?.journal;
        const snippetsRaw = extracted?.snippets;
        const targetFiles: string[] = journalConfig.targetFiles || ["SOUL.md", "USER.md", "MEMORY.md"];

        if (snippetsRaw && typeof snippetsRaw === "object" && !Array.isArray(snippetsRaw)) {
          for (const [filename, snippets] of Object.entries(snippetsRaw)) {
            if (!targetFiles.includes(filename) || !Array.isArray(snippets)) continue;
            const valid = snippets.filter((s: unknown) => typeof s === "string" && (s as string).trim().length > 0);
            if (valid.length > 0) {
              snippetDetails[filename] = valid.map((s: string) => s.trim());
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
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw new Error("[quaid] extraction snippet/journal parsing failed under failHard", { cause: err as Error });
        }
        console.warn(`[quaid] extraction snippet/journal parsing failed: ${String((err as Error)?.message || err)}`);
      }

      const hasSnippets = Object.keys(snippetDetails).length > 0;
      const hasJournalEntries = Object.keys(journalDetails).length > 0;
      const triggerType = resolveExtractionTrigger(label);
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new")
        && shouldNotifyFeature("extraction", "summary");
      if ((factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion)
        && shouldNotifyFeature("extraction", "summary")) {
        try {
          const trigger = triggerType === "unknown" ? "reset" : triggerType;
          const mergedDetails: Record<string, string[]> = {};
          for (const [f, items] of Object.entries(snippetDetails)) {
            mergedDetails[f] = items.map((s) => `[snippet] ${s}`);
          }
          for (const [f, items] of Object.entries(journalDetails)) {
            mergedDetails[f] = [...(mergedDetails[f] || []), ...items.map((s) => `[journal] ${s}`)];
          }
          const hasMerged = Object.keys(mergedDetails).length > 0;
          const detailsPath = path.join(QUAID_TMP_DIR, `extraction-details-${Date.now()}.json`);
          fs.writeFileSync(detailsPath, JSON.stringify({
            stored, skipped, edges_created: edgesCreated, trigger, details: factDetails,
            snippet_details: hasMerged ? mergedDetails : null,
            always_notify: alwaysNotifyCompletion,
          }), { mode: 0o600 });
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
            try { fs.unlinkSync(detailsPath); } catch {}
          }
        } catch (notifyErr: unknown) {
          console.warn(`[quaid] Extraction notification skipped: ${(notifyErr as Error).message}`);
        }
      }

      if (triggerType === "timeout") {
        maybeForceCompactionAfterTimeout(sessionId);
      }

      try {
        const extractionLogPath = path.join(WORKSPACE, "data", "extraction-log.json");
        let extractionLog: Record<string, any> = {};
        try {
          const parsed = JSON.parse(fs.readFileSync(extractionLogPath, "utf8"));
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("extraction log must be a JSON object");
          }
          extractionLog = parsed as Record<string, any>;
        } catch (err: unknown) {
          if (isFailHardEnabled() && !isMissingFileError(err)) {
            throw new Error("[quaid] extraction log read failed under failHard", { cause: err as Error });
          }
          console.warn(`[quaid] Extraction log read failed for ${extractionLogPath}: ${String((err as Error)?.message || err)}`);
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
          last_extracted_at: new Date().toISOString(),
          message_count: messages.length,
          label: label,
          topic_hint: topicHint,
        };
        const trimmed = trimExtractionLogEntries(extractionLog, MAX_EXTRACTION_LOG_ENTRIES);
        fs.writeFileSync(extractionLogPath, JSON.stringify(trimmed, null, 2), { mode: 0o600 });
      } catch (logErr: unknown) {
        const msg = `[quaid] extraction log update failed: ${(logErr as Error).message}`;
        if (isFailHardEnabled()) {
          throw new Error(msg);
        }
        console.warn(msg);
      }
    };
    // Recovery scan: detect sessions interrupted by gateway restart before extraction fired
    async function checkForUnextractedSessions(): Promise<void> {
      // Rate limit: only run once per gateway restart
      const flagPath = path.join(QUAID_RUNTIME_DIR, "quaid-recovery-ran.txt");
      try {
        const flagStat = fs.statSync(flagPath);
        const fiveMinAgo = Date.now() - 5 * 60 * 1000;
        if (flagStat.mtimeMs > fiveMinAgo) {
          console.log('[quaid] Recovery scan already ran recently, skipping');
          return;
        }
      } catch (err: unknown) {
        const code = (err as NodeJS.ErrnoException | undefined)?.code;
        const msg = String((err as Error)?.message || err || "");
        if (code !== "ENOENT") {
          console.warn(`[quaid] Recovery scan flag read failed: ${msg}`);
        }
      } // Flag missing is expected on first run.

      console.log('[quaid] Running recovery scan for unextracted sessions...');

      const sessionsDir = path.join(os.homedir(), '.openclaw', 'sessions');
      if (!fs.existsSync(sessionsDir)) {
        console.log('[quaid] No sessions directory found, skipping recovery');
        fs.writeFileSync(flagPath, new Date().toISOString());
        return;
      }

      // Load extraction log
      const extractionLogPath = path.join(WORKSPACE, 'data', 'extraction-log.json');
      let extractionLog: Record<string, { last_extracted_at: string; message_count: number }> = {};
      try {
        const parsed = JSON.parse(fs.readFileSync(extractionLogPath, 'utf8'));
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
          throw new Error("extraction log must be a JSON object");
        }
        extractionLog = parsed as Record<string, { last_extracted_at: string; message_count: number }>;
      } catch (err: unknown) {
        if (isFailHardEnabled() && !isMissingFileError(err)) {
          throw new Error("[quaid] recovery extraction log read failed under failHard", { cause: err as Error });
        }
        console.warn(`[quaid] Recovery scan extraction log read failed: ${String((err as Error)?.message || err)}`);
      }

      const sessionFiles = fs.readdirSync(sessionsDir).filter(f => f.endsWith('.jsonl'));
      let recovered = 0;

      for (const file of sessionFiles) {
        const sessionId = file.replace('.jsonl', '');
        const filePath = path.join(sessionsDir, file);

        try {
          const stat = fs.statSync(filePath);

          // Skip sessions younger than 5 minutes (may be active)
          if (Date.now() - stat.mtimeMs < 5 * 60 * 1000) { continue; }

          // Check if already extracted
          const logEntry = extractionLog[sessionId];
          if (logEntry) {
            const extractedAt = new Date(logEntry.last_extracted_at).getTime();
            if (extractedAt >= stat.mtimeMs) { continue; } // Already extracted
          }

          // Read JSONL and build messages (handles both direct {role} and {type:"message"} formats)
          const messages = readMessagesFromSessionFile(filePath);

          // Skip short sessions
          if (messages.length < 4) { continue; }

          console.log(`[quaid] Recovering unextracted session ${sessionId} (${messages.length} messages)`);
          await extractMemoriesFromMessages(messages, 'Recovery', sessionId);
          recovered++;
        } catch (err: unknown) {
          console.error(`[quaid] Recovery failed for session ${sessionId}:`, (err as Error).message);
          if (isFailHardEnabled()) {
            throw err;
          }
        }
      }

      console.log(`[quaid] Recovery scan complete: ${recovered} sessions recovered`);
      fs.writeFileSync(flagPath, new Date().toISOString());
    }

    // Register compaction hook — extract memories in parallel with compaction LLM.
    // Uses sessionFile (JSONL on disk) when available, else event.messages.
    api.on("before_compaction", async (event: any, ctx: any) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        // Prefer reading from sessionFile (all messages already on disk, runs in
        // parallel with compaction). Fall back to in-memory messages array.
        let messages: any[];
        if (event.sessionFile) {
          try {
            messages = readMessagesFromSessionFile(event.sessionFile);
            console.log(`[quaid] before_compaction: read ${messages.length} messages from sessionFile`);
          } catch (readErr: unknown) {
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

        // Wrap extraction in a promise that memory_recall can gate on.
        // This runs async (fire-and-forget from the hook's perspective) but
        // memory_recall will wait for it to finish before querying.
        const doExtraction = async () => {
          // before_compaction hook is unreliable async-wise; queue signal for worker tick.
          if (isSystemEnabled("memory")) {
            const extractionSessionId = sessionId || extractSessionId(conversationMessages, ctx);
            timeoutManager.queueExtractionSignal(extractionSessionId, "CompactionSignal");
            console.log(`[quaid][signal] queued CompactionSignal session=${extractionSessionId}`);
          } else {
            console.log("[quaid] Compaction: memory extraction skipped — memory system disabled");
          }

          // Auto-update docs from transcript (non-fatal)
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);

          try {
            await updateDocsFromTranscript(conversationMessages, "Compaction", uniqueSessionId);
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Compaction doc update failed:", (err as Error).message);
          }

          // Emit project event for background processing (non-fatal)
          try {
            await emitProjectEvent(conversationMessages, "compact", uniqueSessionId);
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Compaction project event failed:", (err as Error).message);
          }

          // Record compaction timestamp and reset injection dedup list (memory system).
          if (isSystemEnabled("memory") && uniqueSessionId) {
            const logPath = getInjectionLogPath(uniqueSessionId);
            let logData: any = {};
            try {
              logData = JSON.parse(fs.readFileSync(logPath, "utf8"));
            } catch (err: unknown) {
              console.warn(`[quaid] compaction injection log read failed for ${logPath}: ${String((err as Error)?.message || err)}`);
            }
            logData.lastCompactionAt = new Date().toISOString();
            logData.memoryTexts = [];  // Reset — all memories eligible for re-injection
            fs.writeFileSync(logPath, JSON.stringify(logData, null, 2), { mode: 0o600 });
            console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
          }
        };

        // Chain onto any in-flight extraction to avoid overwrite race
        // (if compaction and reset overlap, the .finally() from the first
        // extraction would clear the promise while the second is still running)
        extractionPromise = queueExtractionTask(doExtraction, "compaction")
          .catch((doErr: unknown) => {
            console.error(`[quaid][compaction] extraction_failed session=${sessionId || "unknown"} err=${String((doErr as Error)?.message || doErr)}`);
            if (isFailHardEnabled()) {
              throw doErr;
            }
          });
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_compaction hook failed:", err);
      }
    }, {
      name: "compaction-memory-extraction",
      priority: 10
    });

    // Register reset hook — extract memories before session is cleared by /new or /reset
    // Uses sessionFile when available, falls back to in-memory messages.
    api.on("before_reset", async (event: any, ctx: any) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        // Prefer sessionFile (complete transcript on disk), fall back to in-memory messages
        let messages: any[];
        if (event.sessionFile) {
          try {
            messages = readMessagesFromSessionFile(event.sessionFile);
            console.log(`[quaid] before_reset: read ${messages.length} messages from sessionFile`);
          } catch (readErr: unknown) {
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
          // before_reset can race with session teardown; queue signal for worker tick.
          if (isSystemEnabled("memory")) {
            const extractionSessionId = sessionId || extractSessionId(conversationMessages, ctx);
            timeoutManager.queueExtractionSignal(extractionSessionId, "ResetSignal");
            console.log(`[quaid][signal] queued ResetSignal session=${extractionSessionId}`);
          } else {
            console.log("[quaid] Reset: memory extraction skipped — memory system disabled");
          }

          // Auto-update docs from transcript (non-fatal)
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);

          try {
            await updateDocsFromTranscript(conversationMessages, "Reset", uniqueSessionId);
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Reset doc update failed:", (err as Error).message);
          }

          // Emit project event for background processing (non-fatal)
          try {
            await emitProjectEvent(conversationMessages, "reset", uniqueSessionId);
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Reset project event failed:", (err as Error).message);
          }
          console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"}`);
        };

        // Chain onto any in-flight extraction to avoid overwrite race
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${extractionPromise ? "yes" : "no"}`);
        extractionPromise = queueExtractionTask(doExtraction, "reset")
          .catch((doErr: unknown) => {
            console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String((doErr as Error)?.message || doErr)}`);
            if (isFailHardEnabled()) {
              throw doErr;
            }
          });
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_reset hook failed:", err);
      }
    }, {
      name: "reset-memory-extraction",
      priority: 10
    });

    // Register HTTP endpoint for LLM proxy (used by Python janitor/extraction)
    // Python code calls this instead of the Anthropic API directly — gateway handles auth.
    api.registerHttpRoute({
      path: "/plugins/quaid/llm",
      handler: async (req, res) => {
        if (req.method !== "POST") {
          res.writeHead(405, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Method not allowed" }));
          return;
        }

        // Read request body
        const chunks: Buffer[] = [];
        for await (const chunk of req) { chunks.push(chunk as Buffer); }
        let body: any;
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

        const { system_prompt, user_message, model_tier, max_tokens = 4000 } = body;
        if (typeof system_prompt !== "string" || !system_prompt.trim() ||
            typeof user_message !== "string" || !user_message.trim()) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "system_prompt and user_message required" }));
          return;
        }
        if (model_tier !== undefined && model_tier !== "fast" && model_tier !== "deep") {
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
        if (requestedTokens < 1 || requestedTokens > 100_000) {
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "max_tokens must be between 1 and 100000" }));
          return;
        }

        try {
          const tier: ModelTier = model_tier === "fast" ? "fast" : "deep";
          const data = await callConfiguredLLM(system_prompt, user_message, tier, requestedTokens, 600_000);
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify(data));
        } catch (err: unknown) {
          console.error(`[quaid] LLM proxy error: ${String(err)}`);
          const msg = String((err as Error)?.message || err);
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
          const url = new URL(req.url!, "http://localhost");
          const sessionId = url.searchParams.get("sessionId");
          
          if (!sessionId || !/^[a-f0-9-]{1,64}$/i.test(sessionId)) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: "Valid sessionId parameter required" }));
            return;
          }

          // Try to read enhanced log first (NAS), fall back to temp log
          const enhancedLogPath = path.join(QUAID_LOGS_DIR, "memory-injection", `session-${sessionId}.log`);
          const tempLogPath = getInjectionLogPath(sessionId);
          
          let logData: any = null;
          
          // Try enhanced log first
          if (fs.existsSync(enhancedLogPath)) {
            try {
              const content = fs.readFileSync(enhancedLogPath, 'utf8');
              logData = JSON.parse(content);
            } catch (err: unknown) {
              console.error(`[quaid] Failed to read enhanced log: ${String(err)}`);
            }
          }
          
          // Fall back to temp log
          if (!logData && fs.existsSync(tempLogPath)) {
            try {
              const content = fs.readFileSync(tempLogPath, 'utf8');
              logData = JSON.parse(content);
            } catch (err: unknown) {
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
        } catch (err: unknown) {
          console.error(`[quaid] HTTP endpoint error: ${String(err)}`);
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: "Internal server error" }));
        }
      }
    });

    console.log("[quaid] Plugin loaded with compaction/reset hooks and HTTP endpoint");
  },
};

export default quaidPlugin;
