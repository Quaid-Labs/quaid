/**
 * quaid - Total Recall memory system plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */

import type { ClawdbotPluginApi } from "openclaw/plugin-sdk";
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
  validateApiSurface,
} from "./contract-gate.js";


// Configuration
function _normalizeWorkspacePath(rawPath: string): string {
  const trimmed = String(rawPath || "").trim();
  if (!trimmed) {
    return path.resolve(process.cwd());
  }
  const expanded = trimmed.startsWith("~")
    ? path.join(os.homedir(), trimmed.slice(1))
    : trimmed;
  return path.resolve(expanded);
}

function _resolveWorkspace(): string {
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
      const mainAgent = list.find((a: any) => a?.id === "main" || a?.default === true);
      const ws = String(mainAgent?.workspace || cfg?.agents?.defaults?.workspace || "").trim();
      if (ws) {
        return _normalizeWorkspacePath(ws);
      }
    }
  } catch (err: unknown) {
    console.error("[quaid][startup] workspace resolution failed:", (err as Error)?.message || String(err));
  }

  return _normalizeWorkspacePath(process.cwd());
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
const ADAPTER_PLUGIN_MANIFEST_PATH = path.join(PYTHON_PLUGIN_ROOT, "adaptors", "openclaw", "plugin.json");
const EXTRACTION_NOTIFY_DEDUPE_MS = 90_000;
const COMPACTION_NOTIFY_BATCH_MS = _envTimeoutMs("QUAID_COMPACTION_NOTIFY_BATCH_MS", 45_000);
const COMPACTION_NOTIFY_BATCH_MAX_MS = _envTimeoutMs("QUAID_COMPACTION_NOTIFY_BATCH_MAX_MS", 120_000);
const extractionNotifyHistory = new Map<string, number>();
type CompactionNotifyBatchState = {
  startedAtMs: number;
  lastUpdateMs: number;
  sessions: Set<string>;
  sessionsWithFacts: Set<string>;
  stored: number;
  skipped: number;
  edges: number;
  timer: ReturnType<typeof setTimeout> | null;
};
let compactionNotifyBatchState: CompactionNotifyBatchState | null = null;
const ADAPTER_BOOT_TIME_MS = Date.now();
const BACKLOG_NOTIFY_STALE_MS = 90_000;

for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch (err: unknown) {
    console.error(`[quaid][startup] failed to create runtime dir: ${p}`, (err as Error)?.message || String(err));
  }
}

let _memoryConfigErrorLogged = false;
let _memoryConfigMtimeMs = -1;
let _memoryConfigPath = "";

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

// Model resolution — reads from config/memory.json, no hardcoded model IDs
let _memoryConfig: any = null;
function _memoryConfigCandidates(): string[] {
  return [
    path.join(WORKSPACE, "config", "memory.json"),
    path.join(os.homedir(), ".quaid", "memory-config.json"),
    path.join(process.cwd(), "memory-config.json"),
  ];
}

function _resolveMemoryConfigPath(): string {
  for (const candidate of _memoryConfigCandidates()) {
    try {
      if (fs.existsSync(candidate)) {
        return candidate;
      }
    } catch {
      // Ignore filesystem probe failures and continue to next candidate.
    }
  }
  return _memoryConfigCandidates()[0];
}

function _buildFallbackMemoryConfig(): any {
  return {
    models: {
      llmProvider: "default",
      deepReasoning: "default",
      fastReasoning: "default",
      deepReasoningModelClasses: {
        anthropic: "claude-opus-4-6",
        openai: "gpt-5",
        "openai-compatible": "gpt-4.1",
      },
      fastReasoningModelClasses: {
        anthropic: "claude-haiku-4-5",
        openai: "gpt-5-mini",
        "openai-compatible": "gpt-4.1-mini",
      },
    },
    retrieval: {
      maxLimit: 8,
    },
  };
}

function getMemoryConfig(): any {
  const configPath = _resolveMemoryConfigPath();
  if (configPath !== _memoryConfigPath) {
    _memoryConfigMtimeMs = -1;
    _memoryConfigPath = configPath;
  }
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
    if (isMissingFileError(err)) {
      // During gateway reloads, config may be briefly unavailable; keep plugin alive with safe defaults.
      _memoryConfig = _buildFallbackMemoryConfig();
      _memoryConfigMtimeMs = -1;
      return _memoryConfig;
    }
    // Prevent isFailHardEnabled() -> getMemoryConfig() mutual recursion on invalid config.
    _memoryConfig = _buildFallbackMemoryConfig();
    _memoryConfigMtimeMs = mtimeMs;
    if (isFailHardEnabled()) {
      throw err;
    }
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

function isPluginStrictMode(): boolean {
  const plugins = getMemoryConfig().plugins || {};
  const raw = plugins.strict;
  if (raw === undefined) return true;
  if (raw === null) return false;
  if (typeof raw === "number") return raw !== 0;
  if (typeof raw === "string") return raw.length > 0;
  if (Array.isArray(raw)) return raw.length > 0;
  if (typeof raw === "object") return Object.keys(raw).length > 0;
  return !!raw;
}

type AdapterContractDeclarations = {
  enabled: boolean;
  tools: Set<string>;
  events: Set<string>;
  api: Set<string>;
};

function loadAdapterContractDeclarations(): AdapterContractDeclarations {
  try {
    const payload = JSON.parse(fs.readFileSync(ADAPTER_PLUGIN_MANIFEST_PATH, "utf8"));
    const contract = payload?.capabilities?.contract || {};
    return {
      enabled: true,
      tools: normalizeDeclaredExports(contract?.tools?.exports),
      events: normalizeDeclaredExports(contract?.events?.exports),
      api: normalizeDeclaredExports(contract?.api?.exports),
    };
  } catch (err: unknown) {
    const msg = `[quaid][contract] failed reading adapter manifest ${ADAPTER_PLUGIN_MANIFEST_PATH}: ${String((err as Error)?.message || err)}`;
    if (isPluginStrictMode()) {
      throw new Error(msg, { cause: err as Error });
    }
    console.warn(msg);
    return { enabled: false, tools: new Set<string>(), events: new Set<string>(), api: new Set<string>() };
  }
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

function shouldNotifyProjectCreate(): boolean {
  const notifications = getMemoryConfig().notifications || {};
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
    path.join(PYTHON_PLUGIN_ROOT, "core", "lifecycle", "janitor.py"),
    path.join(PYTHON_PLUGIN_ROOT, "datastore", "memorydb", "memory_graph.py"),
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
// Memory Notes — queued for extraction at compaction/reset
// ============================================================================

const MAX_INJECTION_LOG_FILES = 400;
const MAX_INJECTION_IDS_PER_SESSION = 4000;
const MAX_EXTRACTION_LOG_ENTRIES = 800;

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

function resolveSessionIdFromSessionKey(sessionKey: string): string {
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
  } catch {}
  return "";
}

function resolveMostRecentSessionId(): string {
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    if (!fs.existsSync(sessionsPath)) {
      return "";
    }
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const parsed = JSON.parse(raw);
    const entries = Object.values(parsed || {}) as any[];
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
  } catch {}
  return "";
}

function resolveMemoryStoreSessionId(ctx?: any): string {
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

function resolveLifecycleHookSessionId(event: any, ctx: any, messages: any[]): string {
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

function getAllConversationMessages(messages: any[]): any[] {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg: any) => {
    if (!msg || (msg.role !== "user" && msg.role !== "assistant")) return false;
    const text = facade.getMessageText(msg).trim();
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
  const signal = facade.detectLifecycleSignal(messages);
  return signal?.label || null;
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

function queueCompactionNotificationBatch(sessionId: string, stored: number, skipped: number, edges: number): void {
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
    // Force flush now so sustained compaction traffic cannot defer forever.
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
    spawnNotifyScript(`
from core.runtime.notify import notify_user, _resolve_channel
notify_user(${JSON.stringify(summary)}, channel_override=_resolve_channel("extraction"))
`);
  }, flushDelayMs);
  if (typeof (state.timer as any).unref === "function") {
    (state.timer as any).unref();
  }
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

const DOCS_UPDATER = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/updater.py");
const DOCS_RAG = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/rag.py");
const DOCS_REGISTRY = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/registry.py");
const PROJECT_UPDATER = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/project_updater.py");
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
  try {
    _ensureGatewaySessionOverride(modelTier, resolved);
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err);
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
  const preamble = `import sys, os\nsys.path.insert(0, ${JSON.stringify(PYTHON_PLUGIN_ROOT)})\n`;
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

function maybeQueueJanitorHealthAlert(): void {
  const issue = facade.getJanitorHealthIssue();
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

async function emitProjectEvent(messages: any[], trigger: string, sessionId?: string): Promise<void> {
  try {
    const staged = await facade.stageProjectEvent(messages, trigger, sessionId, undefined, QUICK_PROJECT_SUMMARY_TIMEOUT_MS);
    if (!staged) {
      return;
    }

    // Spawn background processor (detached) — gateway-managed credential only
    const bgApiKey = _getAnthropicCredential();
    const logFile = path.join(WORKSPACE, "logs/project-updater.log");
    const logDir = path.dirname(logFile);
    if (!fs.existsSync(logDir)) { fs.mkdirSync(logDir, { recursive: true }); }
    const logFd = fs.openSync(logFile, "a");
    try {
      const proc = spawn("python3", [PROJECT_UPDATER, "process-event", staged.eventPath], {
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

    console.log(`[quaid] Emitted project event: ${trigger} -> ${staged.projectHint || "unknown"}`);
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

// ============================================================================
// Session Status for User Notification
// ============================================================================

// ============================================================================
// Doc Auto-Update from Transcript
// ============================================================================

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

type KnowledgeDatastore = "vector" | "vector_basic" | "vector_technical" | "graph" | "journal" | "project";
type DomainFilter = Record<string, boolean>;

function preprocessTranscriptText(text: string): string {
  return String(text || "")
    .replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "")
    .replace(/\n?\[message_id:\s*\d+\]/gi, "")
    .trim();
}

function shouldSkipTranscriptText(_role: "user" | "assistant", text: string): boolean {
  if (!text) return true;
  if (text.startsWith("GatewayRestart:") || text.startsWith("System:")) return true;
  if (text.includes('"kind": "restart"')) return true;
  if (text.includes("HEARTBEAT") && text.includes("HEARTBEAT_OK")) return true;
  if (text.replace(/[*_<>\/b\s]/g, "").startsWith("HEARTBEAT_OK")) return true;
  return false;
}

// ============================================================================
// Adapter-facing Facade
// ============================================================================
// Single entry point for all core operations. Tool handlers should route
// through the facade instead of reaching directly into bridges/scripts.

const facade = createQuaidFacade({
  workspace: WORKSPACE,
  pluginRoot: PYTHON_PLUGIN_ROOT,
  dbPath: DB_PATH,
  eventSource: "openclaw_adapter",
  execPython: createPythonBridgeExecutor({
    scriptPath: PYTHON_SCRIPT,
    dbPath: DB_PATH,
    workspace: WORKSPACE,
    pluginRoot: PYTHON_PLUGIN_ROOT,
  }),
  execExtractPipeline: (tmpPath, args) =>
    _spawnWithTimeout(EXTRACT_SCRIPT, tmpPath, args, "extract", {}, EXTRACT_PIPELINE_TIMEOUT_MS),
  execDocsRag: (cmd, args) =>
    _spawnWithTimeout(DOCS_RAG, cmd, args, "docs_rag", {
      QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
    }),
  execDocsRegistry: (cmd, args) =>
    _spawnWithTimeout(DOCS_REGISTRY, cmd, args, "docs_registry", {
      QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
    }),
  execDocsUpdater: (cmd, args) => {
    const apiKey = _getAnthropicCredential();
    return _spawnWithTimeout(DOCS_UPDATER, cmd, args, "docs_updater", {
      QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
      ...(apiKey ? { ANTHROPIC_API_KEY: apiKey } : {}),
    });
  },
  execEvents: (cmd, args) =>
    _spawnWithTimeout(EVENTS_SCRIPT, cmd, args, "events", {
      QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE,
    }, EVENTS_EMIT_TIMEOUT_MS),
  callLLM: callConfiguredLLM,
  getMemoryConfig,
  isSystemEnabled,
  isFailHardEnabled,
  resolveOwner: () => resolveOwner(),
  transcriptFormat: {
    preprocessText: preprocessTranscriptText,
    shouldSkipText: shouldSkipTranscriptText,
    speakerLabel: (role: "user" | "assistant") => role === "user" ? "User" : "Alfie",
  },
});
const recallStoreGuidance = facade.renderDatastoreGuidance();
const getProjectNames = () => facade.getProjectNames();

// Shared recall abstraction — used by both memory_recall tool and auto-inject
interface RecallOptions {
  query: string;
  limit?: number;
  expandGraph?: boolean;
  graphDepth?: number;
  domain?: DomainFilter;
  domainBoost?: Record<string, number> | string[];
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
    const contractDecl = loadAdapterContractDeclarations();
    const strictContracts = isPluginStrictMode();
    if (contractDecl.enabled) {
      validateApiSurface(contractDecl.api, strictContracts, (m) => console.warn(m));
    }
    const registeredApi = new Set<string>(["openclaw_adapter_entry"]);
    const onChecked = (eventName: string, handler: any, options?: any) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      return api.on(eventName as any, handler, options);
    };
    const registerInternalHookChecked = (eventName: string, handler: any, options?: any) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      return api.registerHook(eventName as any, handler, options);
    };
    const registerToolChecked = (factory: () => any) => {
      const spec = factory();
      const toolName = String(spec?.name || "").trim();
      if (contractDecl.enabled) {
        assertDeclaredRegistration("tools", toolName, contractDecl.tools, strictContracts, (m) => console.warn(m));
      }
      return api.registerTool(() => spec);
    };
    const registerHttpRouteChecked = (route: { path: string; auth: "gateway" | "plugin"; handler: any }) => {
      const routePath = String(route?.path || "").trim();
      if (contractDecl.enabled) {
        assertDeclaredRegistration("api", routePath, contractDecl.api, strictContracts, (m) => console.warn(m));
      }
      if (routePath) {
        registeredApi.add(routePath);
      }
      return api.registerHttpRoute(route as any);
    };

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
    void facade.getStatsParsed()
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
        const autoInjectK = facade.computeDynamicK();
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

        const formatted = facade.formatMemoriesForInjection(toInject);
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

    // Register lifecycle hooks via registerHook (api.on is for event bus signals).
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    registerInternalHookChecked("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });

    // Lifecycle extraction is hook-driven:
    // - before_compaction => CompactionSignal
    // - command:reset|command:new => ResetSignal (primary gateway path)
    // - session_end => ResetSignal (session replacement path)
    // - before_reset => ResetSignal (compat fallback)
    // We intentionally do NOT enqueue extraction from agent_end.
    console.log("[quaid] agent_end auto-capture disabled; using session_end + compaction hooks");

    // Beta fallback: some gateway agent RPC paths do not emit lifecycle hooks for
    // slash commands. Subscribe to transcript updates and queue lifecycle signals
    // from explicit command/system markers.
    const runtimeEvents = (api as any)?.runtime?.events;
    const parseSessionIdFromTranscriptPath = (sessionFile: string): string => {
      const base = path.basename(String(sessionFile || ""));
      const match = base.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
      return match ? match[0].toLowerCase() : "";
    };
    if (runtimeEvents && typeof runtimeEvents.onSessionTranscriptUpdate === "function") {
      runtimeEvents.onSessionTranscriptUpdate((update: any) => {
        try {
          const sessionFile = String(update?.sessionFile || "").trim();
          if (!sessionFile || !fs.existsSync(sessionFile)) return;
          const messages = readMessagesFromSessionFile(sessionFile);
          if (!Array.isArray(messages) || messages.length === 0) return;
          const detail = facade.detectLifecycleSignal(messages);
          if (!detail) return;
          const sessionId =
            parseSessionIdFromTranscriptPath(sessionFile) ||
            String(update?.sessionId || "").trim();
          if (!sessionId) {
            console.log(`[quaid][signal] transcript_update missing session id file=${sessionFile}`);
            return;
          }
          if (!facade.shouldProcessLifecycleSignal(sessionId, detail)) {
            console.log(`[quaid][signal] suppressed duplicate ${detail.label} session=${sessionId} source=transcript_update`);
            return;
          }
          timeoutManager.queueExtractionSignal(sessionId, detail.label, {
            source: "transcript_update",
          });
          console.log(`[quaid][signal] queued ${detail.label} session=${sessionId} source=transcript_update`);
        } catch (err: unknown) {
          console.error("[quaid] transcript_update fallback failed:", err);
        }
      });
      console.log("[quaid] Registered runtime.events.onSessionTranscriptUpdate lifecycle fallback");
    }

    // Register memory tools (gated by memory system)
    if (isSystemEnabled("memory")) {
    registerToolChecked(
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
              domainBoost: Type.Optional(Type.Union([
                Type.Array(Type.String({ description: "Domain IDs to boost at default x1.3." })),
                Type.Object({}, { additionalProperties: Type.Number({ description: "Domain boost multiplier by domain id (e.g. {\"technical\":1.5})." }) }),
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
              const rawMaxLimit = configData?.retrieval?.maxLimit ?? configData?.retrieval?.max_limit ?? 50;
              const parsedMaxLimit = Number(rawMaxLimit);
              maxLimit = Number.isFinite(parsedMaxLimit) && parsedMaxLimit > 0 ? parsedMaxLimit : 50;
            } catch (err: unknown) {
              console.warn(`[quaid] memory_recall maxLimit config read failed: ${String((err as Error)?.message || err)}`);
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
            const domain = (options.filters?.domain && typeof options.filters.domain === "object")
              ? options.filters.domain
              : { all: true };
            const domainBoost = (Array.isArray(options.filters?.domainBoost) || (options.filters?.domainBoost && typeof options.filters.domainBoost === "object"))
              ? options.filters?.domainBoost as Record<string, number> | string[]
              : undefined;
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

            console.log(`[quaid] memory_recall: query="${query?.slice(0, 50)}...", requestedLimit=${requestedLimit}, dynamicK=${dynamicK} (${facade.getActiveNodeCount()} nodes), maxLimit=${maxLimit}, finalLimit=${limit}, expandGraph=${expandGraph}, graphDepth=${depth}, requestedDatastores=${selectedStores.join(",")}, routed=${shouldRouteStores}, reasoning=${reasoning}, intent=${intent}, domain=${JSON.stringify(domain)}, domainBoost=${JSON.stringify(domainBoost || {})}, project=${project || "any"}, dateFrom=${dateFrom}, dateTo=${dateTo}`);
            const results = await recallMemories({
              query, limit, expandGraph, graphDepth: depth, datastores: selectedStores, routeStores: shouldRouteStores, reasoning, intent, ranking, domain, domainBoost,
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
            const vectorResults = results.filter((r) => facade.isVectorRecallResult(r));
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

    registerToolChecked(
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
            const sessionId = resolveMemoryStoreSessionId(ctx);
            facade.addMemoryNote(sessionId, text, category);
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
          ),
        }),
        async execute(_toolCallId, params) {
          try {
            const { query, memoryId } = params || {};
            if (memoryId) {
              await facade.forget(["--id", memoryId]);
              return {
                content: [{ type: "text", text: `Memory ${memoryId} forgotten.` }],
                details: { action: "deleted", id: memoryId },
              };
            } else if (query) {
              await facade.forget([query]);
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
          ),
        }),
        async execute(_toolCallId, params) {
          try {
            const { query, limit = 5, project, docs } = params || {};

            // RAG search with optional project filter (via facade)
            const searchArgs: string[] = ["--limit", String(limit)];
            if (project) { searchArgs.push("--project", project); }
            if (Array.isArray(docs) && docs.length > 0) { searchArgs.push("--docs", docs.join(",")); }
            const results = await facade.docsSearch(query, searchArgs);

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
              const stalenessJson = await facade.docsCheckStaleness();
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
    registerToolChecked(
      () => ({
        name: "docs_read",
        description: "Read the full content of a registered document by file path or title.",
        parameters: Type.Object({
          identifier: Type.String({ description: "File path (workspace-relative) or document title" }),
        }),
        async execute(_toolCallId, params) {
          try {
            const { identifier } = params || {};
            const output = await facade.docsRead(identifier);
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
    registerToolChecked(
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
            const output = await facade.docsList(args);
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
            const output = await facade.docsRegister(args);
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
    registerToolChecked(
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
            const output = await facade.docsCreateProject(args);
            if (shouldNotifyProjectCreate()) {
              try {
                const notifyPayload = JSON.stringify({
                  name: params.name,
                  label: params.label || "",
                });
                spawnNotifyScript(`
import json
from core.runtime.notify import notify_user
data = json.loads(${JSON.stringify(notifyPayload)})
name = str(data.get("name", "")).strip()
label = str(data.get("label", "")).strip()
project_label = f"{name} ({label})" if label else name
notify_user(f"📁 Project registered: {project_label}")
`);
              } catch (notifyErr: unknown) {
                console.warn(`[quaid] Project-create notification skipped: ${(notifyErr as Error).message}`);
              }
            }
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
    registerToolChecked(
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
                  const transcript = facade.buildTranscript(messages);
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
                const factsOutput = await facade.searchBySession(sid, 20);
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
      isBootstrapOnly: (messages: any[]) => facade.isResetBootstrapOnlyConversation(messages),
      readSessionMessages: (sessionId: string) => readMessagesForTimeoutSession(sessionId),
      listSessionActivity: () => listSessionActivityForTimeout(),
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
    const signalWorkerHeartbeatSecRaw = Number(process.env.QUAID_SIGNAL_WORKER_HEARTBEAT_SECONDS || "30");
    const signalWorkerHeartbeatSec = Number.isFinite(signalWorkerHeartbeatSecRaw) && signalWorkerHeartbeatSecRaw > 0
      ? Math.floor(signalWorkerHeartbeatSecRaw)
      : 30;
    const signalWorkerStarted = timeoutManager.startWorker(signalWorkerHeartbeatSec);
    console.log(
      `[quaid][timeout] signal worker ${signalWorkerStarted ? "started" : "leader_exists"} `
      + `heartbeat_seconds=${signalWorkerHeartbeatSec}`,
    );

    // Shared recall abstraction — used by both memory_recall tool and auto-inject
    async function recallMemories(opts: RecallOptions): Promise<MemoryResult[]> {
      const {
        query, limit = 10, expandGraph = false,
        graphDepth = 1, datastores, routeStores = false, reasoning = "fast", intent = "general", ranking, domain = { all: true }, domainBoost, project, dateFrom, dateTo, docs, datastoreOptions, waitForExtraction = false, sourceTag = "unknown"
      } = opts;
      const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);

      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${selectedStores.join(",")} routed=${routeStores} reasoning=${reasoning} intent=${intent} domain=${JSON.stringify(domain)} domainBoost=${JSON.stringify(domainBoost || {})} project=${project || "any"} waitForExtraction=${waitForExtraction}`
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
        failOpen: opts.failOpen,
      };
      return sourceTag === "tool"
        ? facade.recallWithToolRetry(recallOpts)
        : facade.recall(recallOpts);
    }

    // Read messages from a session JSONL file
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

    function resolveOpenClawSessionStorePath(): string {
      return path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    }

    let openClawSessionStoreCache:
      | { mtimeMs: number; data: Record<string, any> }
      | null = null;

    function loadOpenClawSessionStore(): Record<string, any> {
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
        const data = raw as Record<string, any>;
        openClawSessionStoreCache = { mtimeMs, data };
        return data;
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid][timeout] session store read failed: ${String((err as Error)?.message || err)}`);
        return {};
      }
    }

    function parseSessionUpdatedAtMs(entry: any): number | null {
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

    function resolveSessionTranscriptPath(entry: any, sessionId: string): string | null {
      const pathCandidates = [entry?.sessionFile, entry?.session_file];
      for (const raw of pathCandidates) {
        const p = String(raw || "").trim();
        if (!p) continue;
        if (fs.existsSync(p)) return p;
      }
      const fallbackDirs = [
        path.join(os.homedir(), ".openclaw", "agents", "main", "sessions"),
        path.join(os.homedir(), ".openclaw", "sessions"),
      ];
      for (const dir of fallbackDirs) {
        const candidate = path.join(dir, `${sessionId}.jsonl`);
        if (fs.existsSync(candidate)) return candidate;
      }
      return null;
    }

    function readMessagesForTimeoutSession(sessionId: string): any[] {
      const sid = String(sessionId || "").trim();
      if (!sid) return [];
      const store = loadOpenClawSessionStore();
      const entries = Object.entries(store || {}) as Array<[string, any]>;
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

    function listSessionActivityForTimeout(): Array<{ sessionId: string; lastActivityMs: number }> {
      const store = loadOpenClawSessionStore();
      const rows: Array<{ sessionId: string; lastActivityMs: number }> = [];
      const entries = Object.entries(store || {}) as Array<[string, any]>;
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
        } catch (err: unknown) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.warn(`[quaid][timeout] session mtime read failed for ${sid}: ${String((err as Error)?.message || err)}`);
        }
      }
      return rows;
    }

    // Shared memory extraction logic — used by both compaction and reset hooks
    const extractMemoriesFromMessages = async (messages: any[], label: string, sessionId?: string) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        return;
      }

      const sessionNotes = sessionId ? facade.getAndClearMemoryNotes(sessionId) : [];
      // Avoid cross-session leakage: extraction only consumes notes for the active session.
      const allNotes = Array.from(new Set([...sessionNotes]));
      if (allNotes.length > 0) {
        console.log(`[quaid] ${label}: prepend ${allNotes.length} queued memory note(s)`);
      }

      const fullTranscript = facade.buildTranscript(messages);
      if (!fullTranscript.trim() && allNotes.length === 0) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        return;
      }

      const hasMeaningfulUserContent = messages.some((m: any) => {
        if (m?.role !== "user") return false;
        const text = facade.getMessageText(m).trim();
        if (!text) return false;
        if (text.startsWith("GatewayRestart:")) return false;
        if (text.startsWith("System:")) return false;
        return true;
      });

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
        const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
          messages,
          triggerType,
          Date.now(),
          ADAPTER_BOOT_TIME_MS,
          BACKLOG_NOTIFY_STALE_MS,
        );
        const dedupeSession = sessionId || extractSessionId(messages, {});
        const dedupeKey = `start:${dedupeSession}:${triggerType}`;
        const triggerDesc = triggerType === "compaction"
          ? "compaction"
          : triggerType === "recovery"
            ? "recovery"
            : triggerType === "timeout"
              ? "timeout"
              : triggerType === "new"
                ? "/new"
                : "reset";
        if (triggerType !== "recovery"
          && !suppressBacklogNotify
          && hasMeaningfulUserContent
          && shouldEmitExtractionNotify(dedupeKey)) {
          spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("🧠 Processing memories from ${triggerDesc}...")
`);
        }
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

      const factDetails: Array<{ text: string; status: string; reason?: string; edges?: string[] }> = Array.isArray(extracted?.facts)
        ? extracted.facts
        : [];
      const stored = Number(extracted?.facts_stored || 0);
      const skipped = Number(extracted?.facts_skipped || 0);
      const edgesCreated = Number(extracted?.edges_created || 0);
      const firstFactStatus = factDetails.length > 0 ? String(factDetails[0]?.status || "unknown") : "none";
      console.log(
        `[quaid][extract] payload label=${label} session=${sessionId || "unknown"} ` +
        `facts_len=${factDetails.length} first_status=${firstFactStatus} ` +
        `stored=${stored} skipped=${skipped} edges=${edgesCreated}`,
      );
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
      const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
        messages,
        triggerType,
        Date.now(),
        ADAPTER_BOOT_TIME_MS,
        BACKLOG_NOTIFY_STALE_MS,
      );
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new")
        && hasMeaningfulUserContent
        && shouldNotifyFeature("extraction", "summary");
      const dedupeSession = sessionId || extractSessionId(messages, {});
      const completionDedupeKey = `done:${dedupeSession}:${triggerType}:${stored}:${skipped}:${edgesCreated}`;
      if (!suppressBacklogNotify
        && shouldNotifyFeature("extraction", "summary")
        && triggerType === "compaction") {
        // OpenClaw may emit many compaction-related micro-sessions in bursts.
        // Batch notification output so one user-triggered compact does not spam.
        queueCompactionNotificationBatch(dedupeSession, stored, skipped, edgesCreated);
      } else if (triggerType !== "recovery"
        && !suppressBacklogNotify
        && (factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion)
        && shouldNotifyFeature("extraction", "summary")
        && shouldEmitExtractionNotify(completionDedupeKey)) {
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
            const cleaned = facade.getMessageText(m).trim();
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
    // Register compaction hook — extract memories in parallel with compaction LLM.
    // Source of truth is timeout manager's OpenClaw session reader + local cursor gate.
    registerInternalHookChecked("before_compaction", async (event: any, ctx: any) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages: any[] = event.messages || [];
        const sessionId = ctx?.sessionId;
        const conversationMessages = getAllConversationMessages(messages);
        const extractionSessionId = sessionId || extractSessionId(messages, ctx);
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_compaction: empty/internal hook payload; deferring to timeout source session=${extractionSessionId || "unknown"}`);
        } else {
          console.log(`[quaid] before_compaction hook triggered, ${messages.length} messages, session=${sessionId || "unknown"}`);
        }

        // Wrap extraction in a promise that memory_recall can gate on.
        // This runs async (fire-and-forget from the hook's perspective) but
        // memory_recall will wait for it to finish before querying.
        const doExtraction = async () => {
          // before_compaction hook is unreliable async-wise; queue signal for worker tick.
          if (isSystemEnabled("memory")) {
            if (conversationMessages.length > 0) {
              if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
                label: "CompactionSignal",
                source: "hook",
                signature: "hook:before_compaction",
              })) {
                facade.markLifecycleSignalFromHook(extractionSessionId, "CompactionSignal");
                timeoutManager.queueExtractionSignal(extractionSessionId, "CompactionSignal", {
                  source: "before_compaction",
                  hook_session_id: String(sessionId || ""),
                  extraction_session_id: String(extractionSessionId || ""),
                  event_message_count: messages.length,
                  conversation_message_count: conversationMessages.length,
                  has_system_compacted_notice: conversationMessages.some(
                    (m: any) => String(facade.getMessageText(m) || "").toLowerCase().includes("compacted (")
                  ),
                });
                console.log(`[quaid][signal] queued CompactionSignal session=${extractionSessionId}`);
              } else {
                console.log(`[quaid][signal] suppressed duplicate CompactionSignal session=${extractionSessionId}`);
              }
            } else {
              const extracted = await timeoutManager.extractSessionFromLog(
                extractionSessionId,
                "CompactionSignal",
                messages,
              );
              console.log(
                `[quaid][signal] empty-hook-payload fallback session=${extractionSessionId} extracted=${extracted ? "yes" : "no"}`
              );
            }
          } else {
            console.log("[quaid] Compaction: memory extraction skipped — memory system disabled");
          }

          if (conversationMessages.length === 0) {
            return;
          }

          // Auto-update docs from transcript (non-fatal)
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);

          try {
            await facade.updateDocsFromTranscript(conversationMessages, "Compaction", uniqueSessionId, QUAID_TMP_DIR);
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

    // Primary reset/new capture path for gateway-driven resets.
    for (const commandAction of ["reset", "new"] as const) {
      registerInternalHookChecked(`command:${commandAction}`, async (event: any, ctx: any) => {
        try {
          const messages: any[] = event?.messages || [];
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
            signature,
          })) {
            console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=command:${commandAction}`);
            return;
          }
          facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
          timeoutManager.queueExtractionSignal(sessionId, "ResetSignal", {
            source: "command_hook",
            command: commandAction,
            hook_session_id: sessionId,
            hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
          });
          console.log(`[quaid][signal] queued ResetSignal session=${sessionId} source=command:${commandAction}`);
        } catch (err: unknown) {
          if (isFailHardEnabled()) {
            throw err;
          }
          console.error(`[quaid] command:${commandAction} hook failed:`, err);
        }
      }, {
        name: `command-${commandAction}-memory-extraction`,
        priority: 10,
      });
    }

    // Register reset hook — compatibility fallback for older runtimes.
    // Primary reset/new boundary path is session_end below.
    registerInternalHookChecked("before_reset", async (event: any, ctx: any) => {
      try {
        if (isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages: any[] = event.messages || [];
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
          // before_reset can race with session teardown; queue signal for worker tick.
          if (isSystemEnabled("memory")) {
            if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
              label: "ResetSignal",
              source: "hook",
              signature: "hook:before_reset",
            })) {
              facade.markLifecycleSignalFromHook(extractionSessionId, "ResetSignal");
              timeoutManager.queueExtractionSignal(extractionSessionId, "ResetSignal", {
                source: "before_reset",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || ""),
                reason: String(reason || "unknown"),
                event_message_count: messages.length,
                conversation_message_count: conversationMessages.length,
              });
              console.log(`[quaid][signal] queued ResetSignal session=${extractionSessionId}`);
            } else {
              console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${extractionSessionId}`);
            }
          } else {
            console.log("[quaid] Reset: memory extraction skipped — memory system disabled");
          }

          // Auto-update docs from transcript (non-fatal)
          const uniqueSessionId = extractSessionId(conversationMessages, ctx);

          if (conversationMessages.length > 0) {
            try {
              await facade.updateDocsFromTranscript(conversationMessages, "Reset", uniqueSessionId, QUAID_TMP_DIR);
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

    // Primary reset/new lifecycle capture path.
    // session_end is emitted when OpenClaw replaces/resets a session.
    registerInternalHookChecked("session_end", async (event: any, ctx: any) => {
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
          signature: "hook:session_end",
        })) {
          console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=session_end`);
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        timeoutManager.queueExtractionSignal(sessionId, "ResetSignal", {
          source: "session_end",
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0,
        });
        console.log(
          `[quaid][signal] queued ResetSignal session=${sessionId} source=session_end key=${sessionKey || "unknown"}`
        );
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] session_end hook failed:", err);
      }
    }, {
      name: "session-end-memory-extraction",
      priority: 10,
    });

    // Register HTTP endpoint for LLM proxy (used by Python janitor/extraction)
    // Python code calls this instead of the Anthropic API directly — gateway handles auth.
    registerHttpRouteChecked({
      path: "/plugins/quaid/llm",
      auth: "gateway",
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
    registerHttpRouteChecked({
      path: "/memory/injected",
      auth: "gateway",
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

          const headers: Record<string, string> = {
            'Content-Type': 'application/json',
          };
          const allowedOrigin = String(process.env.QUAID_DASHBOARD_ALLOWED_ORIGIN || "").trim();
          if (allowedOrigin) {
            headers['Access-Control-Allow-Origin'] = allowedOrigin;
            headers['Access-Control-Allow-Methods'] = 'GET';
            headers['Access-Control-Allow-Headers'] = 'Content-Type';
          }
          res.writeHead(200, headers);
          res.end(JSON.stringify(responseData, null, 2));
        } catch (err: unknown) {
          console.error(`[quaid] HTTP endpoint error: ${String(err)}`);
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: "Internal server error" }));
        }
      }
    });

    if (contractDecl.enabled) {
      validateApiRegistrations(contractDecl.api, registeredApi, strictContracts, (m) => console.warn(m));
    }

    console.log("[quaid] Plugin loaded with compaction/reset hooks and HTTP endpoint");
  },
};

export default quaidPlugin;
export const __test = {
  detectLifecycleCommandSignal,
  detectLifecycleSignal: (messages: any[]) => facade.detectLifecycleSignal(messages),
  shouldProcessLifecycleSignal: (
    sessionId: string,
    signal: { label: "ResetSignal" | "CompactionSignal"; source: "user_command" | "system_notice" | "hook"; signature: string },
  ) => facade.shouldProcessLifecycleSignal(sessionId, signal),
  shouldEmitExtractionNotify,
  latestMessageTimestampMs: (messages: any[]) => facade.latestMessageTimestampMs(messages),
  hasExplicitLifecycleUserCommand: (messages: any[]) => facade.hasExplicitLifecycleUserCommand(messages),
  isBacklogLifecycleReplay: (messages: any[], trigger: ExtractionTrigger, nowMs?: number) =>
    facade.isBacklogLifecycleReplay(
      messages,
      trigger,
      nowMs ?? Date.now(),
      ADAPTER_BOOT_TIME_MS,
      BACKLOG_NOTIFY_STALE_MS,
    ),
  markLifecycleSignalFromHook: (sessionId: string, label: "ResetSignal" | "CompactionSignal") =>
    facade.markLifecycleSignalFromHook(sessionId, label),
  clearLifecycleSignalHistory: () => facade.clearLifecycleSignalHistory(),
  clearExtractionNotifyHistory: () => extractionNotifyHistory.clear(),
};
