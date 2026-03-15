import { Type } from "@sinclair/typebox";
import { execFileSync } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
import {
  createQuaidFacade
} from "../../core/facade.js";
import { spawnWithTimeout } from "../../core/spawn-with-timeout.js";
import { spawnDetachedScript } from "../../core/spawn-detached-script.js";
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
  const envQuaidHome = String(process.env.QUAID_HOME || "").trim();
  if (envQuaidHome) {
    return _normalizeWorkspacePath(envQuaidHome);
  }
  const envQuaidWorkspace = String(process.env.QUAID_WORKSPACE || "").trim();
  if (envQuaidWorkspace) {
    return _normalizeWorkspacePath(envQuaidWorkspace);
  }
  const envLegacyWorkspace = String(process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envLegacyWorkspace) {
    return _normalizeWorkspacePath(envLegacyWorkspace);
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
const QUAID_HOOK_TRACE_PATH = path.join(QUAID_LOGS_DIR, "quaid-hook-trace.jsonl");
const QUAID_JANITOR_DIR = path.join(QUAID_LOGS_DIR, "janitor");
const PENDING_INSTALL_MIGRATION_PATH = path.join(QUAID_JANITOR_DIR, "pending-install-migration.json");
const PENDING_APPROVAL_REQUESTS_PATH = path.join(QUAID_JANITOR_DIR, "pending-approval-requests.json");
const JANITOR_NUDGE_STATE_PATH = path.join(QUAID_NOTES_DIR, "janitor-nudge-state.json");
const ADAPTER_PLUGIN_MANIFEST_PATH = path.join(PYTHON_PLUGIN_ROOT, "adaptors", "openclaw", "plugin.json");
const ADAPTER_BOOT_TIME_MS = Date.now();
const BACKLOG_NOTIFY_STALE_MS = 9e4;
const _QUAID_INSTANCE = String(process.env.QUAID_INSTANCE || "").trim();
const _QUAID_PREFIX = _QUAID_INSTANCE.endsWith("-main") ? _QUAID_INSTANCE.slice(0, -5) : _QUAID_INSTANCE;
function getInstanceId(agentLabel = "main") {
  const label = String(agentLabel || "main").trim().toLowerCase() || "main";
  return _QUAID_PREFIX ? `${_QUAID_PREFIX}-${label}` : label;
}
function getDaemonSignalDir(agentId = "main") {
  const instanceId = getInstanceId(agentId);
  return instanceId ? path.join(WORKSPACE, instanceId, "data", "extraction-signals") : path.join(WORKSPACE, "data", "extraction-signals");
}
const DAEMON_SIGNAL_DIR = getDaemonSignalDir("main");
function readInstalledAtMs() {
  try {
    const instanceId = getInstanceId("main");
    const p = instanceId ? path.join(WORKSPACE, instanceId, "data", "installed-at.json") : path.join(WORKSPACE, "data", "installed-at.json");
    const raw = JSON.parse(fs.readFileSync(p, "utf8"));
    const ts = String(raw.installedAt || "").trim();
    if (ts) return new Date(ts).getTime();
  } catch {
  }
  return 0;
}
const sessionTranscriptPaths = /* @__PURE__ */ new Map();
const sessionIdToAgentId = /* @__PURE__ */ new Map();
const QUAID_SESSION_PRESERVE_DIR = path.join(QUAID_LOGS_DIR, "quaid", "sessions");
const SESSION_INDEX_POLL_MS = 1e3;
let sessionIndexWatcherStarted = false;
let sessionIndexWatcherTimer = null;
function getOpenClawSessionsBaseDir() {
  return path.dirname(getOpenClawSessionsPath());
}
function getOpenClawSessionFile(sessionId) {
  return path.join(getOpenClawSessionsBaseDir(), `${sessionId}.jsonl`);
}
function getPreservedSessionFile(sessionId) {
  return path.join(QUAID_SESSION_PRESERVE_DIR, `${sessionId}.jsonl`);
}
function isAutoInjectEnabled(config = getMemoryConfig()) {
  const envValue = String(process.env.MEMORY_AUTO_INJECT ?? "").trim().toLowerCase();
  if (envValue === "1" || envValue === "true" || envValue === "yes" || envValue === "on") {
    return true;
  }
  if (envValue === "0" || envValue === "false" || envValue === "no" || envValue === "off") {
    return false;
  }
  const configured = config?.retrieval?.autoInject;
  return configured !== false;
}
function readSessionsIndex() {
  try {
    const sessionsPath = getOpenClawSessionsPath();
    if (!fs.existsSync(sessionsPath)) {
      return {};
    }
    return JSON.parse(fs.readFileSync(sessionsPath, "utf8")) || {};
  } catch {
    return {};
  }
}
function resolveSessionKeyForSessionId(sessionId) {
  const sid = String(sessionId || "").trim();
  if (!sid) return "";
  const data = readSessionsIndex();
  for (const [key, row] of Object.entries(data || {})) {
    if (String(row?.sessionId || "").trim() === sid) {
      return String(key || "").trim();
    }
  }
  return "";
}
function isInternalSessionContext(event, ctx) {
  const sessionId = String(ctx?.sessionId || event?.sessionId || "").trim();
  if (facade.isInternalQuaidSession(sessionId)) {
    return true;
  }
  const sessionKey = String(
    ctx?.sessionKey || event?.sessionKey || event?.targetSessionKey || resolveSessionKeyForSessionId(sessionId)
  ).trim().toLowerCase();
  return Boolean(sessionKey) && (sessionKey.includes("quaid-llm") || sessionKey.includes("openresponses:"));
}
function pickActiveInteractiveSession(data) {
  const entries = Object.entries(data || {}).filter(([key, row]) => row && typeof row === "object" && typeof row?.sessionId === "string" && key.startsWith("agent:main:")).map(([key, row]) => {
    const sessionId = String(row?.sessionId || "").trim();
    const sessionFile = getOpenClawSessionFile(sessionId);
    let mtimeMs = 0;
    try {
      mtimeMs = fs.statSync(sessionFile).mtimeMs;
    } catch {
    }
    return {
      key,
      sessionId,
      sessionFile,
      mtimeMs,
      updatedAt: Number(row?.updatedAt || 0),
      lastChannel: String(row?.lastChannel || "").trim(),
      lastTo: String(row?.lastTo || "").trim()
    };
  }).filter((row) => row.sessionId);
  const TIER_STALENESS_THRESHOLD_MS = 5 * 60 * 1e3;
  const mainEntry = entries.find((e) => e.key === "agent:main:main");
  const isHighTierKey = (key) => key.startsWith("agent:main:tui-") || key.startsWith("agent:main:telegram:");
  const highTierEntries = entries.filter((e) => isHighTierKey(e.key));
  const bestHighTierUpdatedAt = highTierEntries.reduce(
    (max, e) => Math.max(max, e.updatedAt),
    0
  );
  const suppressTierBoost = mainEntry != null && mainEntry.updatedAt - bestHighTierUpdatedAt > TIER_STALENESS_THRESHOLD_MS;
  const sessionTier = (key) => !suppressTierBoost && isHighTierKey(key) ? 1 : 0;
  entries.sort((a, b) => {
    const tierDiff = sessionTier(a.key) - sessionTier(b.key);
    if (tierDiff !== 0) return tierDiff;
    const uDiff = a.updatedAt - b.updatedAt;
    if (uDiff !== 0) return uDiff;
    return a.mtimeMs - b.mtimeMs;
  });
  if (entries.length > 0) {
    return entries[entries.length - 1];
  }
  try {
    const dir = getOpenClawSessionsBaseDir();
    const names = fs.readdirSync(dir).filter(
      (n) => n.endsWith(".jsonl") && !n.includes(".jsonl.") && n.length > 6
    );
    if (!names.length) return null;
    let best = null;
    for (const name of names) {
      const sessionId = name.slice(0, -6);
      const sessionFile = path.join(dir, name);
      let mtimeMs = 0;
      try {
        mtimeMs = fs.statSync(sessionFile).mtimeMs;
      } catch {
      }
      if (!best || mtimeMs > best.mtimeMs) {
        best = { sessionId, sessionFile, mtimeMs };
      }
    }
    if (!best) return null;
    return {
      key: "agent:main:filesystem-fallback",
      sessionId: best.sessionId,
      sessionFile: best.sessionFile,
      mtimeMs: best.mtimeMs,
      updatedAt: best.mtimeMs,
      lastChannel: "",
      lastTo: ""
    };
  } catch {
    return null;
  }
}
function latestResetBackup(sessionId) {
  const prefix = `${sessionId}.jsonl.reset.`;
  try {
    const names = fs.readdirSync(getOpenClawSessionsBaseDir()).filter((name) => name.startsWith(prefix));
    if (!names.length) return null;
    names.sort();
    return path.join(getOpenClawSessionsBaseDir(), names[names.length - 1]);
  } catch {
    return null;
  }
}
function preserveSessionTranscript(sessionId, preferredPath, reason) {
  const candidates = [];
  const preferred = String(preferredPath || "").trim();
  if (preferred) {
    candidates.push(preferred);
  }
  candidates.push(getOpenClawSessionFile(sessionId));
  const resetBackup = latestResetBackup(sessionId);
  if (resetBackup) {
    candidates.push(resetBackup);
  }
  const deduped = candidates.filter((candidate, index) => candidate && candidates.indexOf(candidate) === index);
  const sourcePath = deduped.find((candidate) => fs.existsSync(candidate));
  if (!sourcePath) {
    writeHookTrace("session_index.transcript_preserve_missing", {
      session_id: sessionId,
      reason,
      candidates: deduped
    });
    return null;
  }
  const destPath = getPreservedSessionFile(sessionId);
  try {
    fs.mkdirSync(path.dirname(destPath), { recursive: true });
    fs.copyFileSync(sourcePath, destPath);
    sessionTranscriptPaths.set(sessionId, destPath);
    writeHookTrace("session_index.transcript_preserved", {
      session_id: sessionId,
      reason,
      source_path: sourcePath,
      dest_path: destPath
    });
    return destPath;
  } catch (err) {
    writeHookTrace("session_index.transcript_preserve_error", {
      session_id: sessionId,
      reason,
      source_path: sourcePath,
      error: String(err?.message || err)
    });
    return null;
  }
}
function extractSessionMessageText(message) {
  if (!message) return "";
  if (typeof message.text === "string") return message.text;
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content.map((part) => typeof part?.text === "string" ? part.text : "").filter(Boolean).join(" ").trim();
  }
  return "";
}
function writeDaemonSignal(sessionId, signalType, meta) {
  if (!sessionId) return null;
  const transcriptPath = sessionTranscriptPaths.get(sessionId) || "";
  if (!transcriptPath) {
    const candidates = [
      path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", `${sessionId}.jsonl`),
      path.join(os.homedir(), ".openclaw", "sessions", `${sessionId}.jsonl`),
      path.join(WORKSPACE, "logs", "quaid", "sessions", `${sessionId}.jsonl`)
    ];
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) {
        sessionTranscriptPaths.set(sessionId, candidate);
        break;
      }
    }
  }
  let resolvedPath = sessionTranscriptPaths.get(sessionId) || "";
  if (!resolvedPath && signalType === "reset") {
    const backup = latestResetBackup(sessionId);
    if (backup) {
      resolvedPath = backup;
      sessionTranscriptPaths.set(sessionId, backup);
    }
  }
  if (!resolvedPath) {
    console.warn(`[quaid][daemon-signal] no transcript path for session ${sessionId}, skipping signal`);
    return null;
  }
  if (signalType === "reset") {
    try {
      const stat = fs.statSync(resolvedPath);
      if (stat.size === 0) {
        const backup = latestResetBackup(sessionId);
        if (backup) {
          resolvedPath = backup;
        }
      }
    } catch {
      const backup = latestResetBackup(sessionId);
      if (backup) {
        resolvedPath = backup;
      }
    }
  }
  const agentLabel = sessionIdToAgentId.get(sessionId) || "main";
  const signalDir = getDaemonSignalDir(agentLabel);
  try {
    fs.mkdirSync(signalDir, { recursive: true });
  } catch {
  }
  const payload = {
    type: signalType,
    session_id: sessionId,
    transcript_path: resolvedPath,
    adapter: "openclaw",
    supports_compaction_control: true,
    timestamp: (/* @__PURE__ */ new Date()).toISOString(),
    meta: meta || {}
  };
  const fname = `${Date.now()}_${process.pid}_${signalType}.json`;
  const sigPath = path.join(signalDir, fname);
  try {
    fs.writeFileSync(sigPath, JSON.stringify(payload), { mode: 384 });
    console.log(`[quaid][daemon-signal] wrote ${signalType} signal for session=${sessionId} path=${sigPath}`);
    return sigPath;
  } catch (err) {
    console.error(`[quaid][daemon-signal] write failed: ${String(err?.message || err)}`);
    return null;
  }
}
function ensureDaemonAlive() {
  try {
    const quaidBin = path.join(PYTHON_PLUGIN_ROOT, "quaid");
    execFileSync(quaidBin, ["daemon", "start"], {
      encoding: "utf-8",
      timeout: 1e4,
      env: buildPythonEnv()
    });
  } catch (err) {
    console.warn(`[quaid][daemon] ensure_alive failed: ${String(err?.message || err)}`);
  }
}
for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch (err) {
    console.error(`[quaid][startup] failed to create runtime dir: ${p}`, err?.message || String(err));
  }
}
function _jsonSafe(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return '"[unserializable]"';
  }
}
function writeHookTrace(event, data = {}) {
  const payload = {
    ts: (/* @__PURE__ */ new Date()).toISOString(),
    event,
    ...data
  };
  try {
    fs.appendFileSync(QUAID_HOOK_TRACE_PATH, `${_jsonSafe(payload)}
`, "utf8");
  } catch (err) {
    console.warn(
      `[quaid][trace] write failed event=${event} err=${String(err?.message || err)}`
    );
  }
}
function _envTimeoutMs(name, fallbackMs) {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}
const EXTRACT_PIPELINE_TIMEOUT_MS = _envTimeoutMs("QUAID_EXTRACT_PIPELINE_TIMEOUT_MS", 3e5);
const EVENTS_EMIT_TIMEOUT_MS = _envTimeoutMs("QUAID_EVENTS_TIMEOUT_MS", 3e5);
function buildPythonEnv(extra = {}) {
  const sep = process.platform === "win32" ? ";" : ":";
  const existing = String(process.env.PYTHONPATH || "").trim();
  const pyPath = existing ? `${PYTHON_PLUGIN_ROOT}${sep}${existing}` : PYTHON_PLUGIN_ROOT;
  return {
    ...process.env,
    MEMORY_DB_PATH: DB_PATH,
    MEMORY_RUNTIME_DIR: QUAID_RUNTIME_DIR,
    QUAID_HOME: WORKSPACE,
    QUAID_WORKSPACE: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE,
    PYTHONPATH: pyPath,
    ...extra
  };
}
function getDatastoreStatsSync() {
  try {
    const output = execFileSync("python3", [PYTHON_SCRIPT, "stats"], {
      encoding: "utf-8",
      timeout: 3e4,
      env: buildPythonEnv()
    });
    const parsed = JSON.parse(output || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    return parsed;
  } catch (err) {
    const msg = `[quaid] datastore stats read failed: ${String(err?.message || err)}`;
    const retrieval = memoryConfigResolver.getMemoryConfig().retrieval || {};
    const failHard = typeof retrieval.fail_hard === "boolean" ? retrieval.fail_hard : typeof retrieval.failHard === "boolean" ? retrieval.failHard : true;
    if (failHard) {
      throw new Error(msg, { cause: err });
    }
    console.warn(msg);
    return null;
  }
}
function buildFallbackMemoryConfig() {
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
function createAdapterMemoryConfigResolver() {
  let memoryConfigErrorLogged = false;
  let memoryConfigMtimeMs = -1;
  let memoryConfigPath = "";
  let memoryConfig = null;
  function memoryConfigCandidates() {
    const candidates = [];
    const instance = String(process.env.QUAID_INSTANCE || "").trim();
    if (instance) {
      candidates.push(path.join(WORKSPACE, instance, "config", "memory.json"));
    }
    candidates.push(
      path.join(WORKSPACE, "shared", "config", "memory.json"),
      path.join(WORKSPACE, "config", "memory.json"),
      path.join(os.homedir(), ".quaid", "memory-config.json"),
      path.join(process.cwd(), "memory-config.json")
    );
    return candidates;
  }
  function resolveMemoryConfigPath() {
    for (const candidate of memoryConfigCandidates()) {
      try {
        if (fs.existsSync(candidate)) {
          return candidate;
        }
      } catch {
      }
    }
    return memoryConfigCandidates()[0];
  }
  function getMemoryConfig2() {
    const configPath = resolveMemoryConfigPath();
    if (configPath !== memoryConfigPath) {
      memoryConfigMtimeMs = -1;
      memoryConfigPath = configPath;
    }
    let mtimeMs = -1;
    try {
      mtimeMs = fs.statSync(configPath).mtimeMs;
    } catch (err) {
      const msg = String(err?.message || err || "");
      if (!msg.includes("ENOENT")) {
        console.warn(`[memory] memory config stat failed: ${msg}`);
      }
    }
    if (memoryConfig && mtimeMs >= 0 && memoryConfigMtimeMs === mtimeMs) {
      return memoryConfig;
    }
    if (memoryConfig && mtimeMs < 0) {
      return memoryConfig;
    }
    try {
      memoryConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
      memoryConfigMtimeMs = mtimeMs;
    } catch (err) {
      if (!memoryConfigErrorLogged) {
        memoryConfigErrorLogged = true;
        console.error(`[memory] failed to load memory config (${configPath}): ${err?.message || String(err)}`);
      }
      if (isMissingFileError(err)) {
        memoryConfig = buildFallbackMemoryConfig();
        memoryConfigMtimeMs = -1;
        return memoryConfig;
      }
      memoryConfig = buildFallbackMemoryConfig();
      memoryConfigMtimeMs = mtimeMs;
      if (isFailHardEnabled()) {
        throw err;
      }
    }
    return memoryConfig;
  }
  return {
    getMemoryConfig: getMemoryConfig2,
    resolveMemoryConfigPath
  };
}
const memoryConfigResolver = createAdapterMemoryConfigResolver();
function getMemoryConfig() {
  return memoryConfigResolver.getMemoryConfig();
}
function isSystemEnabled(system) {
  const config = getMemoryConfig();
  const systems = config.systems || {};
  return systems[system] !== false;
}
function loadAdapterContractDeclarations(strictMode) {
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
    if (strictMode) {
      throw new Error(msg, { cause: err });
    }
    console.warn(msg);
    return { enabled: false, tools: /* @__PURE__ */ new Set(), events: /* @__PURE__ */ new Set(), api: /* @__PURE__ */ new Set() };
  }
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
function getGatewayDefaultProvider() {
  try {
    const cfg = _readOpenClawConfig();
    const primaryModel = String(
      cfg?.agents?.main?.modelPrimary || cfg?.agents?.defaults?.modelPrimary || ""
    ).trim();
    if (primaryModel.includes("/")) {
      const provider = primaryModel.split("/", 1)[0];
      const normalized = String(provider || "").trim().toLowerCase();
      if (normalized) {
        return normalized;
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
          const normalized = String(key || "").trim().toLowerCase();
          if (normalized) {
            return normalized;
          }
        }
      }
      for (const key of Object.keys(lastGood)) {
        const normalized = String(key || "").trim().toLowerCase();
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
function runStartupSelfCheck() {
  const errors = [];
  try {
    const deep = facade.resolveTierModel("deep");
    console.log(`[quaid][startup] deep model resolved: provider=${deep.provider} model=${deep.model}`);
    const paidProviders = /* @__PURE__ */ new Set(["openai-compatible"]);
    if (paidProviders.has(deep.provider)) {
      console.log(`[quaid][billing] paid provider active for deep reasoning: ${deep.provider}/${deep.model}`);
    }
  } catch (err) {
    errors.push(`deep reasoning model resolution failed: ${String(err?.message || err)}`);
  }
  try {
    const fast = facade.resolveTierModel("fast");
    console.log(`[quaid][startup] fast model resolved: provider=${fast.provider} model=${fast.model}`);
    const paidProviders = /* @__PURE__ */ new Set(["openai-compatible"]);
    if (paidProviders.has(fast.provider)) {
      console.log(`[quaid][billing] paid provider active for fast reasoning: ${fast.provider}/${fast.model}`);
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
const MAX_INJECTION_IDS_PER_SESSION = 4e3;
function getOpenClawSessionsPath() {
  return path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
}
function resolveSessionIdFromSessionKey(sessionKey) {
  const key = String(sessionKey || "").trim();
  if (!key) {
    return "";
  }
  try {
    const sessionsPath = getOpenClawSessionsPath();
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
    const sessionsPath = getOpenClawSessionsPath();
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
function listCompactionSessions() {
  try {
    const sessionsPath = getOpenClawSessionsPath();
    const raw = fs.readFileSync(sessionsPath, "utf8");
    const data = JSON.parse(raw);
    return Object.entries(data || {}).filter(([_, value]) => value && typeof value === "object").map(([key, value]) => ({
      key: String(key || "").trim(),
      sessionId: String(value?.sessionId || "").trim()
    })).filter((row) => row.key && row.sessionId);
  } catch {
    return [];
  }
}
async function requestSessionCompaction(sessionKey) {
  const out = await spawnWithTimeout({
    cwd: WORKSPACE,
    env: process.env,
    timeoutMs: 2e4,
    label: "[quaid][gateway] sessions.compact",
    argv: [
      "openclaw",
      "gateway",
      "call",
      "sessions.compact",
      "--json",
      "--params",
      JSON.stringify({ key: sessionKey })
    ]
  });
  const parsed = JSON.parse(String(out || "{}"));
  return { ok: Boolean(parsed?.ok), compacted: parsed?.compacted, raw: String(out || "") };
}
function parseSessionMessagesJsonl(sessionFile) {
  let content;
  try {
    content = fs.readFileSync(sessionFile, "utf8");
  } catch {
    return [];
  }
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
const DOCS_UPDATER = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/updater.py");
const DOCS_RAG = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/rag.py");
const DOCS_REGISTRY = path.join(PYTHON_PLUGIN_ROOT, "datastore/docsdb/registry.py");
const EVENTS_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "core/runtime/events.py");
const _sessionModelOverrideCache = /* @__PURE__ */ new Map();
function _getGatewayCredential(providers) {
  for (const provider of providers) {
    const normalized = String(provider || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "_");
    if (!normalized) continue;
    const directKey = String(process.env[`${normalized}_API_KEY`] || "").trim();
    if (directKey) return directKey;
    const directToken = String(process.env[`${normalized}_TOKEN`] || "").trim();
    if (directToken) return directToken;
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
async function _ensureGatewaySessionOverride(tier, resolved) {
  const sessionKey = `agent:main:quaid-llm-${tier}`;
  const modelRef = `${resolved.provider}/${resolved.model}`;
  const cached = _sessionModelOverrideCache.get(sessionKey);
  if (cached === modelRef) {
    return sessionKey;
  }
  const patchOut = await spawnWithTimeout({
    cwd: WORKSPACE,
    env: process.env,
    timeoutMs: 2e4,
    label: "[quaid][gateway] sessions.patch",
    argv: [
      "openclaw",
      "gateway",
      "call",
      "sessions.patch",
      "--json",
      "--params",
      JSON.stringify({ key: sessionKey, model: modelRef })
    ]
  });
  const patchParsed = JSON.parse(String(patchOut || "{}"));
  if (patchParsed?.ok) {
    _sessionModelOverrideCache.set(sessionKey, modelRef);
    return sessionKey;
  }
  const resetOut = await spawnWithTimeout({
    cwd: WORKSPACE,
    env: process.env,
    timeoutMs: 2e4,
    label: "[quaid][gateway] sessions.reset",
    argv: [
      "openclaw",
      "gateway",
      "call",
      "sessions.reset",
      "--json",
      "--params",
      JSON.stringify({ key: sessionKey, reason: "new" })
    ]
  });
  const resetParsed = JSON.parse(String(resetOut || "{}"));
  if (!resetParsed?.ok) {
    throw new Error(`[quaid][llm] sessions.reset failed for ${sessionKey}`);
  }
  const patchAfterResetOut = await spawnWithTimeout({
    cwd: WORKSPACE,
    env: process.env,
    timeoutMs: 2e4,
    label: "[quaid][gateway] sessions.patch",
    argv: [
      "openclaw",
      "gateway",
      "call",
      "sessions.patch",
      "--json",
      "--params",
      JSON.stringify({ key: sessionKey, model: modelRef })
    ]
  });
  const patchAfterResetParsed = JSON.parse(String(patchAfterResetOut || "{}"));
  if (!patchAfterResetParsed?.ok) {
    throw new Error(`[quaid][llm] sessions.patch failed for ${sessionKey} model=${modelRef}`);
  }
  _sessionModelOverrideCache.set(sessionKey, modelRef);
  return sessionKey;
}
async function callConfiguredLLM(systemPrompt, userMessage, modelTier, maxTokens, timeoutMs = 6e5) {
  const resolved = facade.resolveTierModel(modelTier);
  const provider = String(resolved.provider || "").trim().toLowerCase();
  const started = Date.now();
  try {
    await _ensureGatewaySessionOverride(modelTier, resolved);
  } catch (err) {
    const msg = String(err?.message || err);
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
    cache_read_tokens: data?.usage?.cache_read_input_tokens || 0,
    cache_creation_tokens: data?.usage?.cache_creation_input_tokens || 0,
    truncated: false
  };
}
function _spawnWithTimeout(script, command, args, label, env, timeoutMs = PYTHON_BRIDGE_TIMEOUT_MS) {
  return spawnWithTimeout({
    cwd: WORKSPACE,
    env: buildPythonEnv(env),
    timeoutMs,
    label,
    argv: ["python3", script, command, ...args]
  });
}
function spawnNotifyScript(scriptBody) {
  const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
  const preamble = `import sys, os
sys.path.insert(0, ${JSON.stringify(PYTHON_PLUGIN_ROOT)})
`;
  return spawnDetachedScript({
    scriptDir: QUAID_NOTIFY_DIR,
    logFile: notifyLogFile,
    scriptPrefix: preamble,
    scriptBody,
    env: buildPythonEnv(),
    interpreter: "python3",
    filePrefix: "notify",
    fileExtension: ".py"
  });
}
function preprocessTranscriptText(text) {
  return String(text || "").replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "").replace(/\n?\[message_id:\s*\d+\]/gi, "").trim();
}
function shouldSkipTranscriptText(roleOrText, maybeText) {
  const text = typeof maybeText === "string" ? maybeText : String(roleOrText || "");
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
  // emitProjectEventBackground removed — project events now emitted from Python extraction.
  callLLM: callConfiguredLLM,
  getDefaultLLMProvider: getGatewayDefaultProvider,
  adapterName: "openclaw_adapter",
  defaultOwner: "quaid",
  isSystemSession: (sid) => sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm"),
  runtimeDir: QUAID_RUNTIME_DIR,
  providerAliases: {
    "openai-codex": "openai",
    "anthropic-claude-code": "anthropic"
  },
  resolveSessionIdFromSessionKey,
  resolveDefaultSessionId: () => resolveSessionIdFromSessionKey("agent:main:main"),
  resolveMostRecentSessionId,
  timeoutSessionStorePath: () => path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json"),
  timeoutSessionTranscriptDirs: () => [
    path.join(os.homedir(), ".openclaw", "agents", "main", "sessions"),
    path.join(os.homedir(), ".openclaw", "sessions"),
    // Keep runtime log transcripts as a last-resort fallback only.
    path.join(WORKSPACE, "logs", "quaid", "sessions")
  ],
  readSessionMessagesFile: (sessionFile) => parseSessionMessagesJsonl(sessionFile),
  listCompactionSessions,
  requestSessionCompaction,
  initDatastore: () => {
    execFileSync("python3", [PYTHON_SCRIPT, "init"], {
      timeout: 2e4,
      env: buildPythonEnv()
    });
  },
  getDatastoreStatsSync,
  getMemoryConfig,
  isSystemEnabled,
  isFailHardEnabled,
  transcriptFormat: {
    preprocessText: preprocessTranscriptText,
    shouldSkipText: shouldSkipTranscriptText,
    speakerLabel: (role) => role === "user" ? "User" : "Alfie"
  }
});
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
    const strictContracts = facade.isPluginStrictMode();
    const contractDecl = loadAdapterContractDeclarations(strictContracts);
    if (contractDecl.enabled) {
      validateApiSurface(contractDecl.api, strictContracts, (m) => console.warn(m));
    }
    const registeredApi = /* @__PURE__ */ new Set(["openclaw_adapter_entry"]);
    const getMemoryConfig2 = () => facade.getConfig();
    const isSystemEnabled2 = (system) => facade.isSystemEnabled(system);
    const isFailHardEnabled2 = () => facade.isFailHardEnabled();
    const readSessionMessagesFile = (sessionFile) => facade.readSessionMessagesFile(sessionFile);
    const wrapHookHandler = (registrationType, eventName, handler) => {
      return async (...args) => {
        const event = args?.[0];
        const ctx = args?.[1];
        const sessionId = String(event?.sessionId || ctx?.sessionId || "").trim();
        const messageCount = Array.isArray(event?.messages) ? event.messages.length : 0;
        writeHookTrace("hook.debug.invoke", {
          registration_type: registrationType,
          hook_event: eventName,
          session_id: sessionId,
          message_count: messageCount,
          has_event: Boolean(event),
          has_ctx: Boolean(ctx)
        });
        console.log(
          `[quaid][debug][hook.invoke] registration=${registrationType} event=${eventName} session=${sessionId || "unknown"} messages=${messageCount}`
        );
        try {
          const out = await handler(...args);
          writeHookTrace("hook.debug.complete", {
            registration_type: registrationType,
            hook_event: eventName,
            session_id: sessionId
          });
          return out;
        } catch (err) {
          writeHookTrace("hook.debug.error", {
            registration_type: registrationType,
            hook_event: eventName,
            session_id: sessionId,
            error: String(err?.message || err)
          });
          throw err;
        }
      };
    };
    const onChecked = (eventName, handler, options) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      console.log(
        `[quaid][debug][hook.register] registration=on event=${eventName} name=${String(options?.name || "")} priority=${String(options?.priority || "")}`
      );
      writeHookTrace("hook.register", {
        registration_type: "on",
        hook_event: eventName,
        name: String(options?.name || ""),
        priority: Number(options?.priority || 0)
      });
      return api.on(eventName, wrapHookHandler("on", eventName, handler), options);
    };
    const registerInternalHookChecked = (eventName, handler, options) => {
      if (contractDecl.enabled) {
        assertDeclaredRegistration("events", eventName, contractDecl.events, strictContracts, (m) => console.warn(m));
      }
      console.log(
        `[quaid][debug][hook.register] event=${eventName} name=${String(options?.name || "")} priority=${String(options?.priority || "")}`
      );
      writeHookTrace("hook.register", {
        registration_type: "registerHook",
        hook_event: eventName,
        name: String(options?.name || ""),
        priority: Number(options?.priority || 0)
      });
      return api.registerHook(eventName, wrapHookHandler("registerHook", eventName, handler), options);
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
    try {
      const initialized = facade.initializeDatastoreIfMissing();
      if (initialized) {
        console.log("[quaid] Datastore initialization complete");
      }
    } catch (err) {
      console.error("[quaid] Datastore initialization failed:", err.message);
      if (isFailHardEnabled2()) {
        throw err;
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
    let timeoutManager = null;
    const beforeAgentStartHandler = async (event, ctx) => {
      if (isInternalSessionContext(event, ctx)) {
        return;
      }
      try {
        const messages = facade.collectJanitorNudges({
          statePath: JANITOR_NUDGE_STATE_PATH,
          pendingInstallMigrationPath: PENDING_INSTALL_MIGRATION_PATH,
          pendingApprovalRequestsPath: PENDING_APPROVAL_REQUESTS_PATH
        });
        for (const message of messages) {
          spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user(${JSON.stringify(message)})
`);
        }
      } catch (err) {
        if (isFailHardEnabled2()) {
          throw err;
        }
        console.warn(`[quaid] Janitor nudge dispatch failed: ${String(err?.message || err)}`);
      }
      try {
        facade.maybeQueueJanitorHealthAlert({ statePath: JANITOR_NUDGE_STATE_PATH });
      } catch (err) {
        if (isFailHardEnabled2()) {
          throw err;
        }
        console.warn(`[quaid] Janitor health alert dispatch failed: ${String(err?.message || err)}`);
      }
      if (timeoutManager) {
        timeoutManager.onAgentStart(resolveActiveUserSessionId(event, ctx));
      } else {
        writeHookTrace("hook.before_agent_start.skipped", {
          reason: "timeout_manager_uninitialized",
          hook_session_id: String(ctx?.sessionId || "")
        });
      }
      const autoInjectEnabled = isAutoInjectEnabled(getMemoryConfig2());
      if (!autoInjectEnabled) {
        return { prependContext: event.prependContext };
      }
      return { prependContext: event.prependContext };
    };
    const projectDocsInjectedSessions = /* @__PURE__ */ new Set();
    const beforePromptBuildHandler = async (event, ctx) => {
      if (isInternalSessionContext(event, ctx)) return;
      {
        const _bpbSid = String(ctx?.sessionId || event?.sessionId || "").trim();
        const _bpbKey = String(
          ctx?.sessionKey || event?.sessionKey || event?.targetSessionKey || resolveSessionKeyForSessionId(_bpbSid)
        ).trim().toLowerCase();
        const _bpbInteractive = _bpbKey === "agent:main:main" || _bpbKey.startsWith("agent:main:tui-") || _bpbKey.startsWith("agent:main:telegram:") || !_bpbKey && // Empty key: allow if this is the current known interactive session,
        // or if no interactive session is tracked yet (initial state).
        (!currentInteractiveSession?.sessionId || _bpbSid === currentInteractiveSession.sessionId);
        if (!_bpbInteractive) {
          writeHookTrace("hook.before_prompt_build.non_interactive_skip", {
            session_id: _bpbSid,
            session_key: _bpbKey || "(empty)"
          });
          return;
        }
      }
      const autoInjectEnabled = isAutoInjectEnabled(getMemoryConfig2());
      if (!autoInjectEnabled) return { prependContext: event.prependContext };
      const rawPrompt = String(event.prompt || "").trim();
      if (rawPrompt.length < 5) {
        return { prependContext: event.prependContext };
      }
      try {
        let query = rawPrompt.replace(/^System:\s*/i, "").replace(/^\s*(\[.*?\]\s*)+/s, "").replace(/^---\s*/m, "").trim();
        query = query.replace(/Conversation info \(untrusted metadata\):[\s\S]*?```[\s\S]*?```/gi, "").trim();
        if (query.length < 3) {
          query = rawPrompt;
        }
        if (/^(A new session|Read HEARTBEAT|HEARTBEAT|You are being asked to|\/\w|Exec failed)/.test(query)) {
          return { prependContext: event.prependContext };
        }
        if (query.startsWith("Extract memorable facts and journal entries from this conversation:")) {
          return { prependContext: event.prependContext };
        }
        if (facade.isInternalMaintenancePrompt(query)) {
          return { prependContext: event.prependContext };
        }
        if (facade.isLowQualityQuery(query)) {
          return { prependContext: event.prependContext };
        }
        const autoInjectK = facade.computeDynamicK();
        const injectLimit = autoInjectK;
        const injectIntent = "general";
        const injectDomain = { personal: true };
        const allMemories = await recallMemories({
          query,
          limit: injectLimit,
          expandGraph: true,
          datastores: ["vector_basic", "graph"],
          routeStores: false,
          intent: injectIntent,
          domain: injectDomain,
          failOpen: true,
          waitForExtraction: false,
          sourceTag: "auto_inject"
        });
        const injection = facade.prepareAutoInjectionContext({
          allMemories,
          eventMessages: event.messages || [],
          context: ctx,
          existingPrependContext: event.prependContext,
          injectLimit,
          maxInjectionIdsPerSession: MAX_INJECTION_IDS_PER_SESSION
        });
        if (!injection) return { prependContext: event.prependContext };
        const { toInject, prependContext } = injection;
        event.prependContext = prependContext;
        console.log(`[quaid] Auto-injected ${toInject.length} memories for "${query.slice(0, 50)}..."`);
        try {
          if (facade.shouldNotifyFeature("retrieval", "summary")) {
            const payload = facade.buildRecallNotificationPayload(toInject, query, "auto_inject");
            const dataFile = path.join(QUAID_TMP_DIR, `auto-inject-recall-${Date.now()}.json`);
            fs.writeFileSync(dataFile, JSON.stringify(payload), { mode: 384 });
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
      } catch (error) {
        console.error("[quaid] Auto-injection error:", error);
      }
      let appendSystemContext;
      const sessionKey = String(ctx?.sessionId || ctx?.session?.id || "");
      if (sessionKey && !projectDocsInjectedSessions.has(sessionKey) && isSystemEnabled2("projects")) {
        try {
          const projectDocs = facade.injectProjectContext(void 0);
          if (projectDocs) {
            appendSystemContext = projectDocs;
            projectDocsInjectedSessions.add(sessionKey);
            writeHookTrace("hook.project_docs_injected", { session_id: sessionKey, len: projectDocs.length });
          }
        } catch (err) {
          console.warn(`[quaid] Project docs appendSystemContext injection failed: ${err?.message || String(err)}`);
        }
      }
      return {
        prependContext: event.prependContext,
        ...appendSystemContext ? { appendSystemContext } : {}
      };
    };
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    onChecked("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });
    onChecked("before_prompt_build", beforePromptBuildHandler, {
      name: "memory-injection-prompt-build",
      priority: 10
    });
    console.log("[quaid] agent_end auto-capture disabled; using session_end + compaction hooks");
    const transcriptLifecycleCursor = /* @__PURE__ */ new Map();
    let lastTranscriptSessionHint = null;
    let currentInteractiveSession = null;
    const runtimeEvents = api?.runtime?.events;
    if (runtimeEvents && typeof runtimeEvents.onSessionTranscriptUpdate === "function") {
      runtimeEvents.onSessionTranscriptUpdate((update) => {
        try {
          const sessionFile = String(update?.sessionFile || "").trim();
          if (!sessionFile || !fs.existsSync(sessionFile)) return;
          const trackSessionId = String(update?.sessionId || "").trim();
          if (trackSessionId) sessionTranscriptPaths.set(trackSessionId, sessionFile);
          const messages = readSessionMessagesFile(sessionFile);
          if (!Array.isArray(messages) || messages.length === 0) return;
          const sessionId = facade.parseSessionIdFromTranscriptPath(sessionFile) || facade.resolveLifecycleHookSessionId(
            {
              sessionId: String(update?.sessionId || "").trim(),
              sessionKey: String(update?.sessionKey || update?.targetSessionKey || "").trim()
            },
            void 0,
            []
          ) || String(update?.sessionId || "").trim();
          const sessionKey = String(
            update?.sessionKey || update?.targetSessionKey || resolveSessionKeyForSessionId(sessionId) || ""
          ).trim();
          let timeoutActivitySessionId = sessionId;
          if (sessionKey === "agent:main:main" && currentInteractiveSession?.sessionId && currentInteractiveSession.sessionId !== sessionId) {
            timeoutActivitySessionId = currentInteractiveSession.sessionId;
            writeHookTrace("hook.transcript_update.timeout_rerouted", {
              session_file: sessionFile,
              parsed_session_id: sessionId,
              parsed_session_key: sessionKey,
              rerouted_session_id: timeoutActivitySessionId,
              rerouted_session_key: currentInteractiveSession.key
            });
          }
          if (sessionId) sessionTranscriptPaths.set(sessionId, sessionFile);
          if (timeoutActivitySessionId && timeoutManager && !isInternalSessionContext(
            { sessionId: timeoutActivitySessionId, sessionKey },
            { sessionId: timeoutActivitySessionId, sessionKey }
          )) {
            timeoutManager.onAgentEnd(messages, timeoutActivitySessionId, { source: "transcript_update" });
          } else if (sessionId) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: timeoutManager ? "internal_session" : "timeout_manager_uninitialized",
              parsed_session_id: sessionId,
              timeout_activity_session_id: timeoutActivitySessionId,
              parsed_session_key: sessionKey,
              session_file: sessionFile
            });
          }
          const hasExtractionPrompt = messages.some(
            (m) => /^Extract memorable facts and journal entries from this conversation chunk:/i.test(
              String(facade.getMessageText(m) || "").trim()
            )
          );
          if (hasExtractionPrompt) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: "internal_extraction_transcript",
              session_file: sessionFile,
              message_count: messages.length
            });
            return;
          }
          writeHookTrace("hook.transcript_update.received", {
            update_session_id: String(update?.sessionId || ""),
            session_file: sessionFile,
            message_count: messages.length
          });
          const detail = facade.detectLifecycleSignal(messages);
          const conversationMessages = facade.filterConversationMessages(messages);
          const bootstrapOnlyConversation = facade.isResetBootstrapOnlyConversation(conversationMessages);
          const hasLifecycleUserCommand = facade.hasExplicitLifecycleUserCommand(conversationMessages);
          if (!detail) {
            const tail = messages.slice(-5).map((m) => ({
              role: String(m?.role || ""),
              text: String(facade.getMessageText(m) || "").slice(0, 200)
            }));
            writeHookTrace("hook.transcript_update.no_signal", {
              update_session_id: String(update?.sessionId || ""),
              session_file: sessionFile,
              message_count: messages.length,
              tail
            });
            return;
          }
          writeHookTrace("hook.transcript_update.detected", {
            update_session_id: String(update?.sessionId || ""),
            detected_label: String(detail.label || ""),
            detected_source: String(detail.source || ""),
            detected_signature: String(detail.signature || ""),
            detected_message_index: Number.isFinite(detail.messageIndex) ? Number(detail.messageIndex) : -1,
            parsed_session_id: sessionId,
            session_file: sessionFile,
            message_count: messages.length,
            tail: messages.slice(-5).map((m) => ({
              role: String(m?.role || ""),
              text: String(facade.getMessageText(m) || "").slice(0, 200)
            }))
          });
          if (!sessionId) {
            console.log(`[quaid][signal] transcript_update missing session id file=${sessionFile}`);
            return;
          }
          const detectedMessageIndex = Number.isFinite(detail.messageIndex) ? Number(detail.messageIndex) : messages.length - 1;
          const replayCursorKey = `${sessionId}:${detail.label}:${detail.signature}`;
          const priorMessageIndex = transcriptLifecycleCursor.get(replayCursorKey);
          if (priorMessageIndex != null && detectedMessageIndex <= priorMessageIndex) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: "transcript_signal_replay",
              detected_label: String(detail.label || ""),
              detected_signature: String(detail.signature || ""),
              detected_message_index: detectedMessageIndex,
              prior_message_index: priorMessageIndex,
              session_file: sessionFile
            });
            console.log(
              `[quaid][signal] skipped replay ${detail.label} session=${sessionId} source=transcript_update index=${detectedMessageIndex} prior=${priorMessageIndex}`
            );
            return;
          }
          transcriptLifecycleCursor.set(replayCursorKey, detectedMessageIndex);
          if (!facade.shouldProcessLifecycleSignal(sessionId, detail)) {
            console.log(`[quaid][signal] suppressed duplicate ${detail.label} session=${sessionId} source=transcript_update`);
            return;
          }
          if (conversationMessages.length > 0 && !bootstrapOnlyConversation && !hasLifecycleUserCommand) {
            lastTranscriptSessionHint = { sessionId, seenAtMs: Date.now() };
          }
          const daemonType = detail.label.toLowerCase().includes("reset") ? "reset" : "compaction";
          writeDaemonSignal(sessionId, daemonType, { source: "transcript_update" });
          console.log(`[quaid][signal] daemon signal ${daemonType} session=${sessionId} source=transcript_update`);
        } catch (err) {
          console.error("[quaid] transcript_update fallback failed:", err);
        }
      });
      console.log("[quaid] Registered runtime.events.onSessionTranscriptUpdate lifecycle fallback");
    }
    const sessionIndexMessageCounts = /* @__PURE__ */ new Map();
    const seenSessionIndexCommandKeys = /* @__PURE__ */ new Set();
    const sessionKeyLastSeen = /* @__PURE__ */ new Map();
    const sessionLastActivityMs = /* @__PURE__ */ new Map();
    const startSessionIndexWatcher = () => {
      if (sessionIndexWatcherStarted) {
        return;
      }
      sessionIndexWatcherStarted = true;
      const installedAtMs = readInstalledAtMs();
      const watcherStartMs = Date.now();
      let initialSnapshotDone = false;
      const pendingOrphanChecks = /* @__PURE__ */ new Map();
      const ORPHAN_CHECK_DEADLINE_MS = 6e4;
      const tickSessionIndex = () => {
        try {
          const data = readSessionsIndex();
          const recognizedEntries = [];
          for (const [key, row] of Object.entries(data || {})) {
            if (!row || typeof row !== "object" || typeof row?.sessionId !== "string" || !key.startsWith("agent:")) {
              continue;
            }
            const sessionId = String(row.sessionId || "").trim();
            if (!sessionId) continue;
            const keyParts = key.split(":");
            const agentLabel = keyParts.length >= 3 ? keyParts[1].trim() || "main" : "main";
            sessionIdToAgentId.set(sessionId, agentLabel);
            recognizedEntries.push({
              key,
              sessionId,
              sessionFile: getOpenClawSessionFile(sessionId),
              updatedAt: Number(row.updatedAt || 0)
            });
          }
          for (const entry of recognizedEntries) {
            const { key, sessionId, sessionFile, updatedAt } = entry;
            const prevSessionId = sessionKeyLastSeen.get(key);
            if (prevSessionId && prevSessionId !== sessionId) {
              writeHookTrace("session_index.key_transition", {
                key,
                from_session_id: prevSessionId,
                to_session_id: sessionId
              });
              const prevFile = getOpenClawSessionFile(prevSessionId);
              preserveSessionTranscript(prevSessionId, prevFile, "session-key-transition");
              if (!isInternalSessionContext({ sessionKey: key }, { sessionId: prevSessionId }) && isSystemEnabled2("memory") && facade.shouldProcessLifecycleSignal(prevSessionId, {
                label: "ResetSignal",
                source: "session_index",
                signature: `session_index:key_transition:${key}`
              })) {
                facade.markLifecycleSignalFromHook(prevSessionId, "ResetSignal");
                writeDaemonSignal(prevSessionId, "reset", {
                  source: "session_index_key_transition",
                  session_key: key,
                  next_session_id: sessionId
                });
                writeHookTrace("session_index.signal_queued", {
                  signal: "reset",
                  source: "key-transition",
                  session_id: prevSessionId,
                  session_key: key
                });
              }
              if (isSystemEnabled2("memory") && !isInternalSessionContext({ sessionKey: key }, { sessionId: prevSessionId })) {
                pendingOrphanChecks.set(prevSessionId, Date.now());
              }
              sessionIndexMessageCounts.delete(prevSessionId);
            } else if (!prevSessionId && initialSnapshotDone && isSystemEnabled2("memory") && !isInternalSessionContext({ sessionKey: key }, { sessionId })) {
              writeHookTrace("session_index.new_key_detected", { key, session_id: sessionId, watcher_start_ms: watcherStartMs });
              const currentSids = new Set(recognizedEntries.map((e) => e.sessionId));
              for (const [priorKey, priorSid] of sessionKeyLastSeen.entries()) {
                if (!currentSids.has(priorSid)) {
                  writeHookTrace("session_index.new_key_skip", { reason: "not_in_current_sessions", prior_sid: priorSid, prior_key: priorKey });
                  continue;
                }
                if (/^agent:[^:]+:hook:/.test(priorKey)) continue;
                if (priorSid === sessionId) continue;
                if (isInternalSessionContext({ sessionKey: priorKey }, { sessionId: priorSid })) continue;
                const mtimeFloorMs = installedAtMs > 0 ? installedAtMs : watcherStartMs;
                let priorSize = -1;
                let priorMtime = 0;
                try {
                  const st = fs.statSync(getOpenClawSessionFile(priorSid));
                  priorSize = st.size;
                  priorMtime = st.mtimeMs;
                } catch {
                }
                if (priorSize <= 0) {
                  writeHookTrace("session_index.new_key_skip", { reason: "empty", prior_sid: priorSid, prior_key: priorKey, prior_size: priorSize });
                  continue;
                }
                if (priorMtime <= mtimeFloorMs) {
                  writeHookTrace("session_index.new_key_skip", { reason: "mtime", prior_sid: priorSid, prior_key: priorKey, prior_mtime: priorMtime, installed_at_ms: installedAtMs, watcher_start_ms: watcherStartMs });
                  continue;
                }
                if (!facade.shouldProcessLifecycleSignal(priorSid, {
                  label: "ResetSignal",
                  source: "session_index",
                  signature: `session_index:new_key:${key}`
                })) continue;
                facade.markLifecycleSignalFromHook(priorSid, "ResetSignal");
                writeDaemonSignal(priorSid, "reset", {
                  source: "session_index_new_key",
                  new_key: key,
                  new_session_id: sessionId
                });
                writeHookTrace("session_index.signal_queued", {
                  signal: "reset",
                  source: "new-key",
                  session_id: priorSid,
                  session_key: priorKey,
                  new_key: key
                });
              }
            }
            sessionKeyLastSeen.set(key, sessionId);
            sessionTranscriptPaths.set(sessionId, sessionFile);
            const rows = parseSessionMessagesJsonl(sessionFile);
            const priorCount = sessionIndexMessageCounts.get(sessionId) || 0;
            sessionIndexMessageCounts.set(sessionId, rows.length);
            if (rows.length <= priorCount) {
              continue;
            }
            const fresh = rows.slice(priorCount);
            sessionLastActivityMs.set(sessionId, Date.now());
            for (let i = 0; i < fresh.length; i += 1) {
              const rawText = extractSessionMessageText(fresh[i]).trim();
              if (!rawText) continue;
              const rawLines = rawText.split("\n").filter((l) => l.trim());
              const lastLine = (rawLines[rawLines.length - 1] || "").trim();
              const stripped = lastLine.replace(/^\[.*?\]\s*/, "").trim();
              const text = (stripped || rawText).toLowerCase();
              const commandKey = `${sessionId}:${priorCount + i}:${text}`;
              if (seenSessionIndexCommandKeys.has(commandKey)) {
                continue;
              }
              let daemonType = null;
              let lifecycleSignal = null;
              let commandName = null;
              if (text === "/new" || text.startsWith("/new ")) {
                daemonType = "reset";
                lifecycleSignal = "ResetSignal";
                commandName = "new";
              } else if (text === "/reset" || text.startsWith("/reset ")) {
                daemonType = "reset";
                lifecycleSignal = "ResetSignal";
                commandName = "reset";
              } else if (text === "/compact" || text.startsWith("/compact ")) {
                daemonType = "compaction";
                lifecycleSignal = "CompactionSignal";
                commandName = "compact";
              }
              if (!daemonType || !lifecycleSignal || !commandName) {
                continue;
              }
              seenSessionIndexCommandKeys.add(commandKey);
              writeHookTrace("session_index.command_detected", {
                session_id: sessionId,
                session_key: key,
                command: commandName,
                text: text.slice(0, 120)
              });
              if (isInternalSessionContext({ sessionKey: key }, { sessionId }) || !isSystemEnabled2("memory")) {
                continue;
              }
              preserveSessionTranscript(sessionId, sessionFile, `command-${commandName}`);
              if (!facade.shouldProcessLifecycleSignal(sessionId, {
                label: lifecycleSignal,
                source: "session_index",
                signature: `session_index:command_${commandName}`
              })) {
                writeHookTrace("session_index.signal_suppressed", {
                  session_id: sessionId,
                  session_key: key,
                  command: commandName,
                  reason: "duplicate"
                });
                continue;
              }
              facade.markLifecycleSignalFromHook(sessionId, lifecycleSignal);
              writeDaemonSignal(sessionId, daemonType, {
                source: `session_index_command_${commandName}`,
                command: commandName,
                session_key: key
              });
              writeHookTrace("session_index.signal_queued", {
                signal: daemonType,
                source: `command-${commandName}`,
                session_id: sessionId,
                session_key: key
              });
            }
          }
          const active = pickActiveInteractiveSession(data);
          if (active) {
            currentInteractiveSession = active;
          }
          if (pendingOrphanChecks.size > 0) {
            const nowMs = Date.now();
            for (const [sid, armedAt] of pendingOrphanChecks) {
              if (nowMs - armedAt > ORPHAN_CHECK_DEADLINE_MS) {
                pendingOrphanChecks.delete(sid);
                writeHookTrace("session_index.orphan_check_expired", { session_id: sid });
                continue;
              }
              try {
                const backup = latestResetBackup(sid);
                if (!backup) continue;
                let origSize = -1;
                try {
                  origSize = fs.statSync(getOpenClawSessionFile(sid)).size;
                } catch {
                }
                if (origSize > 0) {
                  pendingOrphanChecks.delete(sid);
                  continue;
                }
                if (!facade.shouldProcessLifecycleSignal(sid, {
                  label: "ResetSignal",
                  source: "watcher_scan",
                  signature: "hook:ResetSignal"
                })) {
                  pendingOrphanChecks.delete(sid);
                  continue;
                }
                pendingOrphanChecks.delete(sid);
                facade.markLifecycleSignalFromHook(sid, "ResetSignal");
                writeDaemonSignal(sid, "reset", { source: "orphan_reset_check" });
                writeHookTrace("session_index.orphan_reset_detected", { session_id: sid });
                console.log(`[quaid][signal] orphan reset detected session=${sid}`);
              } catch {
              }
            }
          }
        } catch (err) {
          writeHookTrace("session_index.error", {
            error: String(err?.message || err)
          });
        }
        initialSnapshotDone = true;
      };
      void tickSessionIndex();
      sessionIndexWatcherTimer = setInterval(tickSessionIndex, SESSION_INDEX_POLL_MS);
      writeHookTrace("session_index.watcher_started", {
        poll_ms: SESSION_INDEX_POLL_MS,
        sessions_path: getOpenClawSessionsPath()
      });
      console.log(`[quaid] session index watcher started pollMs=${SESSION_INDEX_POLL_MS}`);
    };
    const resolveActiveUserSessionId = (event, ctx, messages = []) => {
      const direct = facade.resolveLifecycleHookSessionId(event, ctx, messages);
      if (direct && !isInternalSessionContext(event, { ...ctx || {}, sessionId: direct })) {
        return direct;
      }
      if (currentInteractiveSession?.sessionId) {
        return currentInteractiveSession.sessionId;
      }
      const hint = lastTranscriptSessionHint;
      if (hint?.sessionId) {
        const ageMs = Date.now() - Number(hint.seenAtMs || 0);
        if (ageMs >= 0 && ageMs <= 5 * 6e4) {
          return hint.sessionId;
        }
      }
      return direct;
    };
    const handleSlashLifecycleFromMessage = async (event, ctx, sourceEvent) => {
      try {
        const rawText = String(
          facade.getMessageText(event?.message || event) || event?.text || event?.content || ""
        ).trim();
        if (!rawText) return;
        const text = rawText.replace(/^\[.*?\]\s*/, "").trim() || rawText;
        const normalized = text.toLowerCase();
        let commandAction = null;
        let lifecycleSignal = null;
        if (normalized === "/new" || normalized.startsWith("/new ")) {
          commandAction = "new";
          lifecycleSignal = "ResetSignal";
        } else if (normalized === "/reset" || normalized.startsWith("/reset ")) {
          commandAction = "reset";
          lifecycleSignal = "ResetSignal";
        } else if (normalized === "/compact" || normalized.startsWith("/compact ")) {
          commandAction = "compact";
          lifecycleSignal = "CompactionSignal";
        }
        if (!commandAction || !lifecycleSignal) return;
        const hookMessages = event?.message ? [event.message] : [];
        const sessionId = resolveActiveUserSessionId(event, ctx, hookMessages);
        writeHookTrace("hook.message.command_detected", {
          source_event: sourceEvent,
          command: commandAction,
          text: text.slice(0, 120),
          hook_session_id: sessionId || ""
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled2("memory")) {
          return;
        }
        const signature = `msg:${sourceEvent}:command_${commandAction}`;
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: lifecycleSignal,
          source: "hook",
          signature
        })) {
          writeHookTrace("hook.message.signal_suppressed", {
            source_event: sourceEvent,
            command: commandAction,
            hook_session_id: sessionId,
            reason: "duplicate"
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, lifecycleSignal);
        const daemonSigType = lifecycleSignal.toLowerCase().includes("reset") ? "reset" : "compaction";
        writeDaemonSignal(sessionId, daemonSigType, {
          source: sourceEvent,
          command: commandAction,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
        });
        console.log(`[quaid][signal] daemon signal ${daemonSigType} session=${sessionId} source=${sourceEvent} command=${commandAction}`);
        writeHookTrace("hook.message.signal_queued", {
          source_event: sourceEvent,
          command: commandAction,
          hook_session_id: sessionId
        });
      } catch (err) {
        if (isFailHardEnabled2()) throw err;
        console.error(`[quaid] ${sourceEvent} command detector failed:`, err);
        writeHookTrace("hook.message.error", {
          source_event: sourceEvent,
          error: String(err?.message || err)
        });
      }
    };
    onChecked("message_received", async (event, ctx) => {
      await handleSlashLifecycleFromMessage(event, ctx, "message:received");
    }, {
      name: "message-received-command-memory-extraction",
      priority: 10
    });
    const resolveLifecycleCommandTargetSessionId = (action, event, ctx) => {
      if (action === "new" || action === "reset") {
        const previousSessionId = String(
          // OC stores session data under event.context (nested), not top-level.
          // Read both paths: context.previousSessionEntry (preferred), context.sessionEntry
          // (fallback), context.sessionId (explicit field added by our OC patch), and
          // legacy top-level fields for older OC versions.
          event?.context?.previousSessionEntry?.sessionId || event?.context?.sessionEntry?.sessionId || event?.context?.sessionId || event?.previousSessionEntry?.sessionId || event?.previousSessionId || ""
        ).trim();
        if (previousSessionId) {
          return previousSessionId;
        }
        const hint = lastTranscriptSessionHint;
        if (hint?.sessionId) {
          const ageMs = Date.now() - Number(hint.seenAtMs || 0);
          if (ageMs >= 0 && ageMs <= 5 * 6e4) {
            return hint.sessionId;
          }
        }
      }
      return facade.resolveLifecycleHookSessionId(event, ctx);
    };
    const handleLifecycleCommandHook = async (action, event, ctx) => {
      try {
        const sessionId = resolveLifecycleCommandTargetSessionId(action, event, ctx);
        writeHookTrace("hook.command.received", {
          action,
          hook_session_id: sessionId || "",
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
          previous_session_entry_id: String(event?.previousSessionEntry?.sessionId || ""),
          previous_session_id: String(event?.previousSessionId || ""),
          transcript_hint_session_id: String(lastTranscriptSessionHint?.sessionId || "")
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled2("memory")) {
          return;
        }
        const signature = `hook:command_${action}`;
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "ResetSignal",
          source: "hook",
          signature
        })) {
          writeHookTrace("hook.command.signal_suppressed", {
            action,
            hook_session_id: sessionId,
            reason: "duplicate"
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        writeDaemonSignal(sessionId, "reset", {
          source: `command:${action}`,
          command: action,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
        });
        console.log(`[quaid][signal] daemon signal reset session=${sessionId} source=command:${action}`);
        writeHookTrace("hook.command.signal_queued", {
          action,
          hook_session_id: sessionId
        });
      } catch (err) {
        if (isFailHardEnabled2()) throw err;
        console.error(`[quaid] command:${action} hook failed:`, err);
        writeHookTrace("hook.command.error", {
          action,
          error: String(err?.message || err)
        });
      }
    };
    registerInternalHookChecked("command", async (event, ctx) => {
      const action = String(event?.action || "").trim().toLowerCase();
      if (action === "new" || action === "reset") {
        await handleLifecycleCommandHook(action, event, ctx);
        return;
      }
      if (action !== "compact") {
        return;
      }
      try {
        const sessionId = resolveLifecycleCommandTargetSessionId("compact", event, ctx);
        writeHookTrace("hook.command.received", {
          action,
          hook_session_id: sessionId || "",
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled2("memory")) {
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "CompactionSignal",
          source: "hook",
          signature: "hook:command_compact"
        })) {
          writeHookTrace("hook.command.signal_suppressed", {
            action,
            hook_session_id: sessionId,
            reason: "duplicate"
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "CompactionSignal");
        writeDaemonSignal(sessionId, "compaction", {
          source: "command:compact",
          command: action,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
        });
        console.log(`[quaid][signal] daemon signal compaction session=${sessionId} source=command:${action}`);
        writeHookTrace("hook.command.signal_queued", {
          action,
          hook_session_id: sessionId
        });
      } catch (err) {
        if (isFailHardEnabled2()) throw err;
        console.error("[quaid] command:compact hook failed:", err);
        writeHookTrace("hook.command.error", {
          action,
          error: String(err?.message || err)
        });
      }
    }, {
      name: "command-memory-extraction",
      priority: 10
    });
    registerInternalHookChecked("command:new", async (event, ctx) => {
      await handleLifecycleCommandHook("new", event, ctx);
    }, {
      name: "command-new-memory-extraction",
      priority: 10
    });
    registerInternalHookChecked("command:reset", async (event, ctx) => {
      await handleLifecycleCommandHook("reset", event, ctx);
    }, {
      name: "command-reset-memory-extraction",
      priority: 10
    });
    registerInternalHookChecked("session", async (event, ctx) => {
      try {
        const action = String(event?.action || "").trim().toLowerCase();
        if (action !== "compact:before") {
          return;
        }
        const sessionId = facade.resolveLifecycleHookSessionId(event, ctx);
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled2("memory")) {
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "CompactionSignal",
          source: "hook",
          signature: "hook:session_action_compact_before"
        })) {
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "CompactionSignal");
        writeDaemonSignal(sessionId, "compaction", {
          source: "session:compact:before",
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || "")
        });
        console.log(`[quaid][signal] daemon signal compaction session=${sessionId} source=session action=compact:before`);
      } catch (err) {
        if (isFailHardEnabled2()) throw err;
        console.error("[quaid] session hook failed:", err);
      }
    }, {
      name: "session-memory-extraction",
      priority: 10
    });
    void registerToolChecked;
    timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      logDir: path.join(QUAID_LOGS_DIR, "quaid"),
      timeoutMinutes: facade.getCaptureTimeoutMinutes(),
      failHardEnabled: () => isFailHardEnabled2(),
      isBootstrapOnly: (messages) => facade.isResetBootstrapOnlyConversation(messages),
      shouldSkipText: (text) => shouldSkipTranscriptText(text),
      readSessionMessages: (sessionId) => facade.readTimeoutSessionMessages(sessionId),
      listSessionActivity: () => facade.listTimeoutSessionActivity(),
      hasPendingSessionNotes: (sessionId) => facade.hasPendingMemoryNotes(sessionId),
      logger: (msg) => {
        const lowered = String(msg || "").toLowerCase();
        if (lowered.includes("fail") || lowered.includes("error")) {
          console.warn(msg);
          return;
        }
        console.log(msg);
      },
      extract: async (_msgs, sid, label) => {
        if (sid) {
          writeDaemonSignal(sid, "compaction", {
            source: "timeout_extract",
            label: label || "Timeout"
          });
          console.log(`[quaid][timeout] daemon signal for idle session=${sid} label=${label || "Timeout"}`);
        }
      }
    });
    ensureDaemonAlive();
    console.log("[quaid][daemon] extraction daemon ensure_alive called at boot");
    startSessionIndexWatcher();
    onChecked("before_agent_start", async (event, ctx) => {
      if (isInternalSessionContext(event, ctx)) return;
      const newSessionId = String(ctx?.sessionId || event?.sessionId || "").trim();
      if (!newSessionId) return;
      writeHookTrace("hook.before_agent_start.session_seen", { session_id: newSessionId });
      const newSessionKey = String(
        ctx?.sessionKey || event?.sessionKey || event?.targetSessionKey || resolveSessionKeyForSessionId(newSessionId)
      ).trim().toLowerCase();
      const isInteractiveKey = !newSessionKey || newSessionKey === "agent:main:main" || newSessionKey.startsWith("agent:main:tui-") || newSessionKey.startsWith("agent:main:telegram:");
      if (!isInteractiveKey) return;
      const isAlreadyTracked = Array.from(sessionKeyLastSeen.values()).includes(newSessionId);
      if (!isAlreadyTracked && isSystemEnabled2("memory")) {
        const RECENT_RESET_WINDOW_MS = 12e4;
        const nowMs = Date.now();
        let bestPriorSessionId = null;
        let detectionMethod = "mtime";
        try {
          const baseDir = getOpenClawSessionsBaseDir();
          const allFiles = fs.readdirSync(baseDir);
          let bestResetMtimeMs = 0;
          for (const fname of allFiles) {
            const dotIdx = fname.indexOf(".jsonl.reset.");
            if (dotIdx < 0) continue;
            const sid = fname.slice(0, dotIdx);
            if (!sid) continue;
            try {
              const backupStat = fs.statSync(path.join(baseDir, fname));
              const age = nowMs - backupStat.mtimeMs;
              if (age >= 0 && age < RECENT_RESET_WINDOW_MS && backupStat.mtimeMs > bestResetMtimeMs) {
                bestResetMtimeMs = backupStat.mtimeMs;
                bestPriorSessionId = sid;
                detectionMethod = sid === newSessionId ? "self_reset" : "reset_signature";
              }
            } catch {
            }
          }
        } catch {
        }
        if (!bestPriorSessionId) {
          let bestMtimeMs = 0;
          for (const [key, sid] of sessionKeyLastSeen.entries()) {
            if (/^agent:[^:]+:hook:/.test(key)) continue;
            if (sid === newSessionId) continue;
            try {
              const mtimeMs = fs.statSync(getOpenClawSessionFile(sid)).mtimeMs;
              if (mtimeMs > bestMtimeMs) {
                bestMtimeMs = mtimeMs;
                bestPriorSessionId = sid;
              }
            } catch {
            }
          }
        }
        if (bestPriorSessionId) {
          const priorKey = Array.from(sessionKeyLastSeen.entries()).find(([k, v]) => v === bestPriorSessionId && !/^agent:[^:]+:hook:/.test(k))?.[0] || "agent:main:tui-unknown";
          writeHookTrace("hook.before_agent_start.fallback_transition", {
            new_session_id: newSessionId,
            prior_session_id: bestPriorSessionId,
            prior_key: priorKey,
            detection_method: detectionMethod
          });
          if (!isInternalSessionContext({ sessionKey: priorKey }, { sessionId: bestPriorSessionId }) && facade.shouldProcessLifecycleSignal(bestPriorSessionId, {
            label: "ResetSignal",
            source: "hook",
            signature: `before_agent_start:fallback:${bestPriorSessionId}`
          })) {
            facade.markLifecycleSignalFromHook(bestPriorSessionId, "ResetSignal");
            writeDaemonSignal(bestPriorSessionId, "reset", {
              source: "before_agent_start_fallback",
              prior_session_id: bestPriorSessionId,
              new_session_id: newSessionId
            });
            console.log(
              `[quaid][signal] daemon signal reset session=${bestPriorSessionId} source=before_agent_start_fallback`
            );
          }
        }
        sessionKeyLastSeen.set(`agent:main:hook:${newSessionId}`, newSessionId);
      }
    }, {
      name: "before-agent-start-session-transition",
      priority: 5
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
      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${Array.isArray(datastores) ? datastores.join(",") : "auto"} routed=${routeStores} reasoning=${reasoning} intent=${intent} domain=${JSON.stringify(domain)} domainBoost=${JSON.stringify(domainBoost || {})} project=${project || "any"} waitForExtraction=${waitForExtraction}`
      );
      const queuedExtraction = facade.getQueuedExtractionPromise();
      if (waitForExtraction && queuedExtraction) {
        const waitStartedAt = Date.now();
        writeHookTrace("recall.wait_for_extraction.start", {
          source: sourceTag,
          query_preview: String(query || "").slice(0, 160)
        });
        let raceTimer;
        try {
          await Promise.race([
            queuedExtraction,
            new Promise((_, rej) => {
              raceTimer = setTimeout(() => rej(new Error("timeout")), 6e4);
            })
          ]);
          writeHookTrace("recall.wait_for_extraction.done", {
            source: sourceTag,
            wait_ms: Date.now() - waitStartedAt
          });
        } catch (err) {
          writeHookTrace("recall.wait_for_extraction.error", {
            source: sourceTag,
            wait_ms: Date.now() - waitStartedAt,
            error: String(err?.message || err)
          });
          if (isFailHardEnabled2()) {
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
        datastores,
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
    const extractMemoriesFromMessages = async (messages, label, sessionId) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      writeHookTrace("extract.start", {
        label,
        session_id: sessionId || "",
        message_count: messages.length
      });
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        writeHookTrace("extract.skip_empty_messages", {
          label,
          session_id: sessionId || ""
        });
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
      const startNotify = facade.shouldNotifyExtractionStart({
        messages,
        label,
        sessionId,
        hasMeaningfulUserContent,
        bootTimeMs: ADAPTER_BOOT_TIME_MS,
        backlogNotifyStaleMs: BACKLOG_NOTIFY_STALE_MS,
        showProcessingStart: getMemoryConfig2().notifications?.showProcessingStart !== false
      });
      if (startNotify) {
        writeHookTrace("extract.notify_start", {
          label,
          session_id: sessionId || "",
          trigger: startNotify.triggerDesc
        });
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("\u{1F9E0} Processing memories from ${startNotify.triggerDesc}...")
`);
      }
      let extractionResult = null;
      try {
        extractionResult = await facade.runExtractionPipeline(messages, label, sessionId);
      } catch (err) {
        const msg = String(err?.message || err);
        console.error(`[quaid] ${label} extraction failed: ${msg}`);
        writeHookTrace("extract.pipeline_error", {
          label,
          session_id: sessionId || "",
          error: msg
        });
        return;
      }
      if (!extractionResult) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        writeHookTrace("extract.skip_empty_after_filter", {
          label,
          session_id: sessionId || ""
        });
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
        `[quaid][extract] payload label=${label} session=${sessionId || "unknown"} facts_len=${factDetails.length} first_status=${firstFactStatus} stored=${stored} skipped=${skipped} edges=${edgesCreated}`
      );
      writeHookTrace("extract.pipeline_done", {
        label,
        session_id: sessionId || "",
        fact_count: factDetails.length,
        stored,
        skipped,
        edges_created: edgesCreated,
        trigger_type: triggerFromExtraction
      });
      console.log(`[quaid] ${label} extraction complete: ${stored} stored, ${skipped} skipped, ${edgesCreated} edges`);
      console.log(`[quaid][extract] done label=${label} session=${sessionId || "unknown"} stored=${stored} skipped=${skipped} edges=${edgesCreated}`);
      const snippetDetails = extractionResult.snippetDetails || {};
      const journalDetails = extractionResult.journalDetails || {};
      const hasSnippets = Object.keys(snippetDetails).length > 0;
      const hasJournalEntries = Object.keys(journalDetails).length > 0;
      const triggerType = triggerFromExtraction;
      const suppressBacklogNotify = facade.isBacklogLifecycleReplay(
        messages,
        triggerType,
        Date.now(),
        ADAPTER_BOOT_TIME_MS,
        BACKLOG_NOTIFY_STALE_MS
      );
      const alwaysNotifyCompletion = (triggerType === "timeout" || triggerType === "reset" || triggerType === "new") && (hasMeaningfulFromExtraction || hasMeaningfulUserContent) && facade.shouldNotifyFeature("extraction", "summary");
      const dedupeSession = sessionId || facade.extractSessionId(messages, {});
      const completionDedupeKey = `done:${dedupeSession}:${triggerType}:${stored}:${skipped}:${edgesCreated}`;
      if (!suppressBacklogNotify && facade.shouldNotifyFeature("extraction", "summary") && triggerType === "compaction") {
        writeHookTrace("extract.notify_compaction_batched", {
          session_id: dedupeSession,
          trigger_type: triggerType,
          stored,
          skipped,
          edges_created: edgesCreated
        });
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
          }
        );
      } else if (triggerType !== "recovery" && !suppressBacklogNotify && (factDetails.length > 0 || hasSnippets || hasJournalEntries || alwaysNotifyCompletion) && facade.shouldNotifyFeature("extraction", "summary") && facade.shouldEmitExtractionNotify(completionDedupeKey)) {
        writeHookTrace("extract.notify_completion", {
          session_id: dedupeSession,
          trigger_type: triggerType,
          stored,
          skipped,
          edges_created: edgesCreated,
          has_snippets: hasSnippets,
          has_journal_entries: hasJournalEntries,
          always_notify_completion: alwaysNotifyCompletion
        });
        try {
          const payload = facade.buildExtractionCompletionNotificationPayload({
            stored,
            skipped,
            edgesCreated,
            triggerType: String(triggerType),
            factDetails,
            snippetDetails,
            journalDetails,
            alwaysNotifyCompletion
          });
          const detailsPath = path.join(QUAID_TMP_DIR, `extraction-details-${Date.now()}.json`);
          fs.writeFileSync(detailsPath, JSON.stringify(payload), { mode: 384 });
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
          writeHookTrace("extract.notify_completion_error", {
            session_id: dedupeSession,
            trigger_type: triggerType,
            error: String(notifyErr?.message || notifyErr)
          });
        }
      } else {
        writeHookTrace("extract.notify_completion_suppressed", {
          session_id: dedupeSession,
          trigger_type: triggerType,
          suppress_backlog_notify: suppressBacklogNotify,
          should_notify_feature: facade.shouldNotifyFeature("extraction", "summary"),
          fact_count: factDetails.length,
          has_snippets: hasSnippets,
          has_journal_entries: hasJournalEntries,
          always_notify_completion: alwaysNotifyCompletion
        });
      }
      if (triggerType === "timeout") {
        await facade.maybeForceCompactionAfterTimeout(sessionId);
      }
      try {
        facade.updateExtractionLog(sessionId || "unknown", messages, label);
      } catch (logErr) {
        const msg = `[quaid] extraction log update failed: ${logErr.message}`;
        if (isFailHardEnabled2()) {
          throw new Error(msg);
        }
        console.warn(msg);
      }
    };
    onChecked("before_compaction", async (event, ctx) => {
      try {
        if (isInternalSessionContext(event, ctx)) {
          return;
        }
        const messages = event.messages || [];
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const fallbackInteractiveSessionId = currentInteractiveSession?.sessionId || "";
        const extractionSessionId = sessionId || (conversationMessages.length === 0 ? fallbackInteractiveSessionId : "") || facade.extractSessionId(messages, ctx) || "";
        writeHookTrace("hook.before_compaction.received", {
          hook_session_id: sessionId || "",
          extraction_session_id: extractionSessionId || "",
          fallback_interactive_session_id: fallbackInteractiveSessionId,
          event_message_count: messages.length,
          conversation_message_count: conversationMessages.length
        });
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_compaction: empty/internal hook payload; deferring to timeout source session=${extractionSessionId || "unknown"}`);
          writeHookTrace("hook.before_compaction.empty_payload", {
            extraction_session_id: extractionSessionId || ""
          });
        } else {
          console.log(`[quaid] before_compaction hook triggered, ${messages.length} messages, session=${sessionId || "unknown"}`);
        }
        const doExtraction = async () => {
          if (isSystemEnabled2("memory")) {
            if (conversationMessages.length > 0) {
              if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
                label: "CompactionSignal",
                source: "hook",
                signature: "hook:before_compaction"
              })) {
                facade.markLifecycleSignalFromHook(extractionSessionId, "CompactionSignal");
                writeDaemonSignal(extractionSessionId, "compaction", {
                  source: "before_compaction",
                  hook_session_id: String(sessionId || ""),
                  extraction_session_id: String(extractionSessionId || ""),
                  event_message_count: messages.length,
                  conversation_message_count: conversationMessages.length,
                  has_system_compacted_notice: conversationMessages.some(
                    (m) => String(facade.getMessageText(m) || "").toLowerCase().includes("compacted (")
                  )
                });
                console.log(`[quaid][signal] daemon signal compaction session=${extractionSessionId}`);
                writeHookTrace("hook.before_compaction.signal_queued", {
                  extraction_session_id: extractionSessionId || "",
                  source: "before_compaction"
                });
              } else {
                console.log(`[quaid][signal] suppressed duplicate CompactionSignal session=${extractionSessionId}`);
                writeHookTrace("hook.before_compaction.signal_suppressed", {
                  extraction_session_id: extractionSessionId || "",
                  reason: "duplicate"
                });
              }
            } else {
              const sigPath = writeDaemonSignal(extractionSessionId, "compaction", {
                source: "before_compaction_empty_payload",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || "")
              });
              console.log(
                `[quaid][signal] daemon signal compaction (empty-payload) session=${extractionSessionId} wrote=${sigPath ? "yes" : "no"}`
              );
              writeHookTrace("hook.before_compaction.empty_payload_daemon_signal", {
                extraction_session_id: extractionSessionId || "",
                signal_written: Boolean(sigPath)
              });
            }
          } else {
            console.log("[quaid] Compaction: memory extraction skipped \u2014 memory system disabled");
            writeHookTrace("hook.before_compaction.skip_memory_disabled", {
              extraction_session_id: extractionSessionId || ""
            });
          }
          if (conversationMessages.length === 0) {
            return;
          }
          const uniqueSessionId = facade.extractSessionId(conversationMessages, ctx);
          try {
            await facade.updateDocsFromTranscript(conversationMessages, "Compaction", uniqueSessionId, QUAID_TMP_DIR);
          } catch (err) {
            if (isFailHardEnabled2()) {
              throw err;
            }
            console.error("[quaid] Compaction doc update failed:", err.message);
          }
          if (isSystemEnabled2("memory") && uniqueSessionId) {
            facade.resetInjectionDedupAfterCompaction(uniqueSessionId);
            console.log(`[quaid] Recorded compaction timestamp for session ${uniqueSessionId}, reset injection dedup`);
          }
        };
        facade.queueExtraction(doExtraction, "compaction").catch((doErr) => {
          console.error(`[quaid][compaction] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
          writeHookTrace("hook.before_compaction.extraction_failed", {
            hook_session_id: sessionId || "",
            extraction_session_id: extractionSessionId || "",
            error: String(doErr?.message || doErr)
          });
          if (isFailHardEnabled2()) {
            console.error(`[quaid] extraction failed (fail-hard): ${doErr}`);
          }
        });
      } catch (err) {
        if (isFailHardEnabled2()) {
          throw err;
        }
        console.error("[quaid] before_compaction hook failed:", err);
        writeHookTrace("hook.before_compaction.error", {
          hook_session_id: String(ctx?.sessionId || ""),
          error: String(err?.message || err)
        });
      }
    }, {
      name: "compaction-memory-extraction",
      priority: 10
    });
    onChecked("before_reset", async (event, ctx) => {
      try {
        if (isInternalSessionContext(event, ctx)) {
          return;
        }
        const messages = event.messages || [];
        const reason = event.reason || "unknown";
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const extractionSessionId = facade.resolveLifecycleHookSessionId(event, ctx, conversationMessages);
        writeHookTrace("hook.before_reset.received", {
          hook_session_id: sessionId || "",
          extraction_session_id: extractionSessionId || "",
          reason: String(reason || "unknown"),
          event_message_count: messages.length,
          conversation_message_count: conversationMessages.length
        });
        if (!extractionSessionId) {
          console.log(`[quaid] before_reset: skip unresolved session id session=${sessionId || "unknown"}`);
          writeHookTrace("hook.before_reset.skipped", {
            hook_session_id: sessionId || "",
            reason: "unresolved_session_id"
          });
          return;
        }
        if (conversationMessages.length === 0) {
          console.log(
            `[quaid] before_reset: empty/internal transcript; queueing ResetSignal from source session session=${extractionSessionId}`
          );
        }
        console.log(`[quaid] before_reset hook triggered (reason: ${reason}), ${messages.length} messages, session=${sessionId || "unknown"}`);
        const doExtraction = async () => {
          if (isSystemEnabled2("memory")) {
            if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
              label: "ResetSignal",
              source: "hook",
              signature: "hook:before_reset"
            })) {
              facade.markLifecycleSignalFromHook(extractionSessionId, "ResetSignal");
              writeDaemonSignal(extractionSessionId, "reset", {
                source: "before_reset",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || ""),
                reason: String(reason || "unknown"),
                event_message_count: messages.length,
                conversation_message_count: conversationMessages.length
              });
              console.log(`[quaid][signal] daemon signal reset session=${extractionSessionId}`);
              writeHookTrace("hook.before_reset.signal_queued", {
                extraction_session_id: extractionSessionId,
                reason: String(reason || "unknown")
              });
            } else {
              console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${extractionSessionId}`);
              writeHookTrace("hook.before_reset.signal_suppressed", {
                extraction_session_id: extractionSessionId,
                reason: "duplicate"
              });
            }
          } else {
            console.log("[quaid] Reset: memory extraction skipped \u2014 memory system disabled");
            writeHookTrace("hook.before_reset.skip_memory_disabled", {
              extraction_session_id: extractionSessionId
            });
          }
          const uniqueSessionId = facade.extractSessionId(conversationMessages, ctx);
          if (conversationMessages.length > 0) {
            try {
              await facade.updateDocsFromTranscript(conversationMessages, "Reset", uniqueSessionId, QUAID_TMP_DIR);
            } catch (err) {
              if (isFailHardEnabled2()) {
                throw err;
              }
              console.error("[quaid] Reset doc update failed:", err.message);
            }
          }
          console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"}`);
        };
        const chainActive = facade.getQueuedExtractionPromise() ? "yes" : "no";
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${chainActive}`);
        facade.queueExtraction(doExtraction, "reset").catch((doErr) => {
          console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String(doErr?.message || doErr)}`);
          writeHookTrace("hook.before_reset.extraction_failed", {
            hook_session_id: sessionId || "",
            extraction_session_id: extractionSessionId,
            error: String(doErr?.message || doErr)
          });
          if (isFailHardEnabled2()) {
            console.error(`[quaid] extraction failed (fail-hard): ${doErr}`);
          }
        });
      } catch (err) {
        if (isFailHardEnabled2()) {
          throw err;
        }
        console.error("[quaid] before_reset hook failed:", err);
        writeHookTrace("hook.before_reset.error", {
          hook_session_id: String(ctx?.sessionId || ""),
          error: String(err?.message || err)
        });
      }
    }, {
      name: "reset-memory-extraction",
      priority: 10
    });
    onChecked("session_end", async (event, ctx) => {
      try {
        const sessionId = String(event?.sessionId || ctx?.sessionId || "").trim();
        const sessionKey = String(event?.sessionKey || ctx?.sessionKey || "").trim();
        const messageCount = Number(event?.messageCount || 0);
        writeHookTrace("hook.session_end.received", {
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0
        });
        if (!sessionId || isInternalSessionContext(event, ctx)) {
          writeHookTrace("hook.session_end.skipped", {
            hook_session_id: sessionId,
            reason: "invalid_or_internal_session"
          });
          return;
        }
        if (!isSystemEnabled2("memory")) {
          writeHookTrace("hook.session_end.skipped", {
            hook_session_id: sessionId,
            reason: "memory_disabled"
          });
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "ResetSignal",
          source: "hook",
          signature: "hook:session_end"
        })) {
          console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=session_end`);
          writeHookTrace("hook.session_end.signal_suppressed", {
            hook_session_id: sessionId,
            reason: "duplicate"
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        writeDaemonSignal(sessionId, "session_end", {
          source: "session_end",
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0
        });
        console.log(
          `[quaid][signal] daemon signal session_end session=${sessionId} key=${sessionKey || "unknown"}`
        );
        writeHookTrace("hook.session_end.signal_queued", {
          hook_session_id: sessionId,
          hook_session_key: sessionKey
        });
      } catch (err) {
        if (isFailHardEnabled2()) {
          throw err;
        }
        console.error("[quaid] session_end hook failed:", err);
        writeHookTrace("hook.session_end.error", {
          hook_session_id: String(event?.sessionId || ctx?.sessionId || ""),
          error: String(err?.message || err)
        });
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
          const sessionLogPath = facade.getInjectionLogPath(sessionId);
          let logData = null;
          if (fs.existsSync(sessionLogPath)) {
            try {
              const content = fs.readFileSync(sessionLogPath, "utf8");
              logData = JSON.parse(content);
            } catch (err) {
              console.error(`[quaid] Failed to read session log: ${String(err)}`);
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
  detectLifecycleCommandSignal: (messages) => facade.detectLifecycleSignal(messages)?.label || null,
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
  clearExtractionNotifyHistory: () => facade.clearExtractionNotifyHistory(),
  isAutoInjectEnabled,
  isInternalSessionContext
};
export {
  __test,
  adapter_default as default
};
