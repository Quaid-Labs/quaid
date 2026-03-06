/**
 * quaid - Total Recall memory system plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */

import type { ClawdbotPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { execFileSync, spawn } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
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
const JANITOR_NUDGE_STATE_PATH = path.join(QUAID_NOTES_DIR, "janitor-nudge-state.json");
const ADAPTER_PLUGIN_MANIFEST_PATH = path.join(PYTHON_PLUGIN_ROOT, "adaptors", "openclaw", "plugin.json");
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

type AdapterContractDeclarations = {
  enabled: boolean;
  tools: Set<string>;
  events: Set<string>;
  api: Set<string>;
};

function loadAdapterContractDeclarations(strictMode: boolean): AdapterContractDeclarations {
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
    if (strictMode) {
      throw new Error(msg, { cause: err as Error });
    }
    console.warn(msg);
    return { enabled: false, tools: new Set<string>(), events: new Set<string>(), api: new Set<string>() };
  }
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

type ModelTier = "deep" | "fast";
type ExtractionTrigger = "compaction" | "reset" | "new" | "recovery" | "timeout" | "unknown";

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
        const normalized = String(provider || "").trim().toLowerCase();
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
          const normalized = String(key || "").trim().toLowerCase();
          if (normalized) { return normalized; }
        }
      }
      for (const key of Object.keys(lastGood)) {
        const normalized = String(key || "").trim().toLowerCase();
        if (normalized) { return normalized; }
      }
    }
  } catch (err: unknown) {
    console.warn(`[quaid] gateway provider fallback read failed from auth-profiles.json: ${String((err as Error)?.message || err)}`);
  }
  return "";
}

function runStartupSelfCheck(): void {
  const errors: string[] = [];

  try {
    const deep = facade.resolveTierModel("deep");
    console.log(`[quaid][startup] deep model resolved: provider=${deep.provider} model=${deep.model}`);
    const paidProviders = new Set(["openai-compatible"]);
    if (paidProviders.has(deep.provider)) {
      console.warn(`[quaid][billing] paid provider active for deep reasoning: ${deep.provider}/${deep.model}`);
    }
  } catch (err: unknown) {
    errors.push(`deep reasoning model resolution failed: ${String((err as Error)?.message || err)}`);
  }

  try {
    const fast = facade.resolveTierModel("fast");
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

// Config schema
const configSchema = Type.Object({
  autoCapture: Type.Optional(Type.Boolean({ default: false })),
  autoRecall: Type.Optional(Type.Boolean({ default: true })),
});

type PluginConfig = {
  autoCapture?: boolean;
  autoRecall?: boolean;
};

// ============================================================================
// Memory Notes — queued for extraction at compaction/reset
// ============================================================================

const MAX_INJECTION_IDS_PER_SESSION = 4000;

// ============================================================================
// Session ID Helper
// ============================================================================

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

function listCompactionSessions(): Array<{ key: string; sessionId: string }> {
  try {
    const sessionsPath = path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const data = JSON.parse(raw);
    return (Object.entries(data || {}) as Array<[string, any]>)
      .filter(([_, value]) => value && typeof value === "object")
      .map(([key, value]) => ({
        key: String(key || "").trim(),
        sessionId: String(value?.sessionId || "").trim(),
      }))
      .filter((row) => row.key && row.sessionId);
  } catch {
    return [];
  }
}

function requestSessionCompaction(sessionKey: string): { ok: boolean; compacted?: unknown; raw?: string } {
  try {
    const out = execFileSync(
      "openclaw",
      ["gateway", "call", "sessions.compact", "--json", "--params", JSON.stringify({ key: sessionKey })],
      { encoding: "utf-8", timeout: 20_000 }
    );
    const parsed = JSON.parse(String(out || "{}"));
    return { ok: Boolean(parsed?.ok), compacted: parsed?.compacted, raw: String(out || "") };
  } catch (err: unknown) {
    throw err;
  }
}

function parseSessionMessagesJsonl(sessionFile: string): any[] {
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
      console.warn(`[quaid] session file line parse failed: ${String((err as Error)?.message || err)}`);
    }
  }
  return messages;
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
  const resolved = facade.resolveTierModel(modelTier);
  const provider = String(resolved.provider || "").trim().toLowerCase();
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
  emitProjectEventBackground: (eventPath: string, projectHint: string | null) => {
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
        env: buildPythonEnv({ ...(bgApiKey ? { ANTHROPIC_API_KEY: bgApiKey } : {}) }),
      });
      proc.unref();
    } finally {
      fs.closeSync(logFd);
    }
    console.log(`[quaid] Emitted project event -> ${projectHint || "unknown"}`);
  },
  callLLM: callConfiguredLLM,
  getDefaultLLMProvider: getGatewayDefaultProvider,
  providerAliases: {
    "openai-codex": "openai",
    "anthropic-claude-code": "anthropic",
  },
  resolveSessionIdFromSessionKey,
  resolveDefaultSessionId: () => resolveSessionIdFromSessionKey("agent:main:main"),
  resolveMostRecentSessionId,
  timeoutSessionStorePath: () => path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json"),
  timeoutSessionTranscriptDirs: () => [
    path.join(os.homedir(), ".openclaw", "agents", "main", "sessions"),
    path.join(os.homedir(), ".openclaw", "sessions"),
  ],
  readSessionMessagesFile: (sessionFile: string) => parseSessionMessagesJsonl(sessionFile),
  listCompactionSessions,
  requestSessionCompaction,
  getMemoryConfig,
  isSystemEnabled,
  isFailHardEnabled,
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
  waitForExtraction?: boolean;  // wait on facade extraction queue (tool=yes, inject=no)
  sourceTag?: "tool" | "auto_inject" | "unknown";
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
    const strictContracts = facade.isPluginStrictMode();
    const contractDecl = loadAdapterContractDeclarations(strictContracts);
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
      if (facade.isInternalQuaidSession(ctx?.sessionId)) {
        return;
      }
      try {
        const messages = facade.collectJanitorNudges({
          statePath: JANITOR_NUDGE_STATE_PATH,
          pendingInstallMigrationPath: PENDING_INSTALL_MIGRATION_PATH,
          pendingApprovalRequestsPath: PENDING_APPROVAL_REQUESTS_PATH,
        });
        for (const message of messages) {
          spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user(${JSON.stringify(message)})
`);
        }
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.warn(`[quaid] Janitor nudge dispatch failed: ${String((err as Error)?.message || err)}`);
      }
      try {
        facade.maybeQueueJanitorHealthAlert({ statePath: JANITOR_NUDGE_STATE_PATH });
      } catch (err: unknown) {
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
        if (facade.isInternalMaintenancePrompt(query)) {
          return;
        }

        // Query quality gate — skip acknowledgments and short messages
        if (facade.isLowQualityQuery(query)) {
          return;
        }

        // Auto-inject can either use total_recall (fast planning pass) or plain
        // direct datastores. For project/technical prompts, include technical/project
        // sources explicitly so implementation facts are not filtered out.
        // Dynamic K: 2 * log2(nodeCount) — scales with graph size
        const autoInjectK = facade.computeDynamicK();
        const useTotalRecallForInject = facade.isPreInjectionPassEnabled();
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

        const currentOwner = facade.resolveOwner();
        const filtered = facade.filterMemoriesByPrivacy(allMemories, currentOwner);

        // Session dedup (don't re-inject same facts within a session)
        const uniqueSessionId = facade.extractSessionId(event.messages || [], ctx);
        const previouslyInjected = facade.loadInjectedMemoryKeys(uniqueSessionId);
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
              if (facade.shouldNotifyFeature("retrieval", "summary")) {
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

        facade.saveInjectedMemoryKeys(
          uniqueSessionId,
          previouslyInjected,
          toInject,
          MAX_INJECTION_IDS_PER_SESSION,
        );
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
          const messages = parseSessionMessagesJsonl(sessionFile);
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

            const recallFormatted = facade.formatRecallToolResponse(results);
            const text = recallFormatted.text;

            // Notify user about what memories were retrieved (if enabled)
            try {
              if (facade.shouldNotifyFeature("retrieval", "summary") && results.length > 0) {
                const memoryData = results.map(m => ({
                  text: m.text,
                  similarity: Math.round((m.similarity || 0) * 100),
                  via: m.via || "vector",
                  category: m.category || "",
                }));
                // Build source breakdown for notification
                const sourceBreakdown = {
                  vector_count: recallFormatted.breakdown.vector_count,
                  graph_count: recallFormatted.breakdown.graph_count,
                  journal_count: recallFormatted.breakdown.journal_count,
                  project_count: recallFormatted.breakdown.project_count,
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
                vectorCount: recallFormatted.breakdown.vector_count,
                graphCount: recallFormatted.breakdown.graph_count,
                journalCount: recallFormatted.breakdown.journal_count,
                projectCount: recallFormatted.breakdown.project_count,
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
            const sessionId = facade.resolveMemoryStoreSessionId(ctx);
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
              stalenessWarning = await facade.getDocsStalenessWarning();
            } catch (err: unknown) {
              console.warn(`[quaid] projects_search staleness check failed: ${String((err as Error)?.message || err)}`);
            }

            const text = projectMdContent
              ? `--- PROJECT.md (${project}) ---\n${projectMdContent}\n\n--- Search Results ---\n${results || "No results."}${stalenessWarning}`
              : (results ? results + stalenessWarning : "No results found." + stalenessWarning);

            // Notify user about what docs were searched (if enabled)
            try {
              if (facade.shouldNotifyFeature("retrieval", "summary") && results) {
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
            if (facade.shouldNotifyProjectCreate()) {
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

            if (action === "list") {
              const sessions = facade.listRecentSessionsFromExtractionLog(listLimit);

              if (sessions.length === 0) {
                return {
                  content: [{ type: "text", text: "No recent sessions found in extraction log." }],
                  details: { count: 0 },
                };
              }

              let text = "Recent sessions:\n";
              sessions.forEach((session, i) => {
                const date = session.lastExtractedAt ? new Date(session.lastExtractedAt).toLocaleString() : "unknown";
                const msgCount = session.messageCount || "?";
                const trigger = session.label || "unknown";
                const topic = session.topicHint ? ` — "${session.topicHint}"` : "";
                text += `${i + 1}. [${date}] ${session.sessionId} — ${msgCount} messages, extracted via ${trigger}${topic}\n`;
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
                  const messages = parseSessionMessagesJsonl(sessionPath);
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

    // Extraction promise gate is facade-owned so adapters remain swappable.
    const timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      timeoutMinutes: facade.getCaptureTimeoutMinutes(),
      isBootstrapOnly: (messages: any[]) => facade.isResetBootstrapOnlyConversation(messages),
      shouldSkipText: (text: string) => shouldSkipTranscriptText(text),
      readSessionMessages: (sessionId: string) => facade.readTimeoutSessionMessages(sessionId),
      listSessionActivity: () => facade.listTimeoutSessionActivity(),
      logger: (msg: string) => {
        const lowered = String(msg || "").toLowerCase();
        if (lowered.includes("fail") || lowered.includes("error")) {
          console.warn(msg);
          return;
        }
        console.log(msg);
      },
      extract: async (msgs: any[], sid?: string, label?: string) => {
        const queuedExtraction = facade.queueExtraction(
          () => extractMemoriesFromMessages(msgs, label || "Timeout", sid),
          "timeout",
        );
        await queuedExtraction;
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
      const queuedExtraction = facade.getQueuedExtractionPromise();
      if (waitForExtraction && queuedExtraction) {
        let raceTimer: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            queuedExtraction,
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
    // Shared memory extraction logic — used by both compaction and reset hooks
    const extractMemoriesFromMessages = async (messages: any[], label: string, sessionId?: string) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
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

      if (getMemoryConfig().notifications?.showProcessingStart !== false && facade.shouldNotifyFeature("extraction", "summary")) {
        const triggerType = facade.resolveExtractionTrigger(label);
        const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
          messages,
          triggerType,
          Date.now(),
          ADAPTER_BOOT_TIME_MS,
          BACKLOG_NOTIFY_STALE_MS,
        );
        const dedupeSession = sessionId || facade.extractSessionId(messages, {});
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
          && facade.shouldEmitExtractionNotify(dedupeKey)) {
          spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("🧠 Processing memories from ${triggerDesc}...")
`);
        }
      }

      let extractionResult: any = null;
      try {
        extractionResult = await facade.runExtractionPipeline(messages, label, sessionId);
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        console.error(`[quaid] ${label} extraction failed: ${msg}`);
        // Extraction is best-effort at this adapter boundary. Lower layers still
        // enforce failHard semantics where configured, but we avoid crashing the
        // active gateway/session loop on background extraction failures.
        return;
      }

      if (!extractionResult) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        return;
      }
      const factDetails = extractionResult.factDetails || [];
      const stored = Number(extractionResult.stored || 0);
      const skipped = Number(extractionResult.skipped || 0);
      const edgesCreated = Number(extractionResult.edgesCreated || 0);
      const hasMeaningfulFromExtraction = Boolean(extractionResult.hasMeaningfulUserContent);
      const triggerFromExtraction = String(extractionResult.triggerType || facade.resolveExtractionTrigger(label));
      const firstFactStatus = factDetails.length > 0 ? String(factDetails[0]?.status || "unknown") : "none";
      console.log(
        `[quaid][extract] payload label=${label} session=${sessionId || "unknown"} ` +
        `facts_len=${factDetails.length} first_status=${firstFactStatus} ` +
        `stored=${stored} skipped=${skipped} edges=${edgesCreated}`,
      );
      console.log(`[quaid] ${label} extraction complete: ${stored} stored, ${skipped} skipped, ${edgesCreated} edges`);
      console.log(`[quaid][extract] done label=${label} session=${sessionId || "unknown"} stored=${stored} skipped=${skipped} edges=${edgesCreated}`);

      const snippetDetails: Record<string, string[]> = extractionResult.snippetDetails || {};
      const journalDetails: Record<string, string[]> = extractionResult.journalDetails || {};

      const hasSnippets = Object.keys(snippetDetails).length > 0;
      const hasJournalEntries = Object.keys(journalDetails).length > 0;
      const triggerType = triggerFromExtraction as any;
      const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
        messages,
        triggerType,
        Date.now(),
        ADAPTER_BOOT_TIME_MS,
        BACKLOG_NOTIFY_STALE_MS,
      );
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new")
        && (hasMeaningfulFromExtraction || hasMeaningfulUserContent)
        && facade.shouldNotifyFeature("extraction", "summary");
      const dedupeSession = sessionId || facade.extractSessionId(messages, {});
      const completionDedupeKey = `done:${dedupeSession}:${triggerType}:${stored}:${skipped}:${edgesCreated}`;
      if (!suppressBacklogNotify
        && facade.shouldNotifyFeature("extraction", "summary")
        && triggerType === "compaction") {
        // OpenClaw may emit many compaction-related micro-sessions in bursts.
        // Batch notification output so one user-triggered compact does not spam.
        facade.queueCompactionExtractionSummary(
          dedupeSession,
          stored,
          skipped,
          edgesCreated,
          (summary) => {
            spawnNotifyScript(`
from core.runtime.notify import notify_user, _resolve_channel
notify_user(${JSON.stringify(summary)}, channel_override=_resolve_channel("extraction"))
`);
          },
        );
      } else if (triggerType !== "recovery"
        && !suppressBacklogNotify
        && (factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion)
        && facade.shouldNotifyFeature("extraction", "summary")
        && facade.shouldEmitExtractionNotify(completionDedupeKey)) {
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
        facade.maybeForceCompactionAfterTimeout(sessionId);
      }

      try {
        facade.updateExtractionLog(sessionId || "unknown", messages, label);
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
        if (facade.isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages: any[] = event.messages || [];
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const extractionSessionId = sessionId || facade.extractSessionId(messages, ctx);
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
          const uniqueSessionId = facade.extractSessionId(conversationMessages, ctx);

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
            await facade.emitProjectEvent(conversationMessages, "compact", uniqueSessionId, QUICK_PROJECT_SUMMARY_TIMEOUT_MS);
          } catch (err: unknown) {
            if (isFailHardEnabled()) {
              throw err;
            }
            console.error("[quaid] Compaction project event failed:", (err as Error).message);
          }

          // Record compaction timestamp and reset injection dedup list (memory system).
          if (isSystemEnabled("memory") && uniqueSessionId) {
            facade.resetInjectionDedupAfterCompaction(uniqueSessionId);
            console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
          }
        };

        // Chain onto any in-flight extraction to avoid overwrite race
        // (if compaction and reset overlap, the .finally() from the first
        // extraction would clear the promise while the second is still running)
        facade.queueExtraction(doExtraction, "compaction")
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
          const sessionId = facade.resolveLifecycleHookSessionId(event, ctx, messages);
          if (!sessionId || facade.isInternalQuaidSession(sessionId)) {
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
        if (facade.isInternalQuaidSession(ctx?.sessionId)) {
          return;
        }
        const messages: any[] = event.messages || [];
        const reason = event.reason || "unknown";
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const extractionSessionId = facade.resolveLifecycleHookSessionId(event, ctx, conversationMessages);
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
          const uniqueSessionId = facade.extractSessionId(conversationMessages, ctx);

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
              await facade.emitProjectEvent(conversationMessages, "reset", uniqueSessionId, QUICK_PROJECT_SUMMARY_TIMEOUT_MS);
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
        const chainActive = facade.getQueuedExtractionPromise() ? "yes" : "no";
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${chainActive}`);
        facade.queueExtraction(doExtraction, "reset")
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
        if (!sessionId || facade.isInternalQuaidSession(sessionId)) {
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
          const tempLogPath = facade.getInjectionLogPath(sessionId);
          
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
  detectLifecycleCommandSignal: (messages: any[]) => facade.detectLifecycleSignal(messages)?.label || null,
  detectLifecycleSignal: (messages: any[]) => facade.detectLifecycleSignal(messages),
  shouldProcessLifecycleSignal: (
    sessionId: string,
    signal: { label: "ResetSignal" | "CompactionSignal"; source: "user_command" | "system_notice" | "hook"; signature: string },
  ) => facade.shouldProcessLifecycleSignal(sessionId, signal),
  shouldEmitExtractionNotify: (key: string, now?: number) => facade.shouldEmitExtractionNotify(key, now),
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
  clearExtractionNotifyHistory: () => facade.clearExtractionNotifyHistory(),
};
