/**
 * quaid - Total Recall memory system plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */

import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { Type } from "@sinclair/typebox";
import { execFileSync, spawn } from "node:child_process";
import * as path from "node:path";
import * as fs from "node:fs";
import * as os from "node:os";
import { SessionTimeoutManager } from "../../core/session-timeout.js";
import {
  type DomainFilter,
  type KnowledgeDatastore,
} from "../../core/knowledge-stores.js";
import {
  createQuaidFacade,
  type ExtractionTrigger,
  type FacadeRecallOptions,
  type MemoryResult,
  type ModelTier,
} from "../../core/facade.js";
import { spawnWithTimeout } from "../../core/spawn-with-timeout.js";
import { spawnDetachedScript } from "../../core/spawn-detached-script.js";
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
  const envQuaidHome = String(process.env.QUAID_HOME || "").trim();
  if (envQuaidHome) {
    return _normalizeWorkspacePath(envQuaidHome);
  }
  const envQuaidWorkspace = String(process.env.QUAID_WORKSPACE || "").trim();
  if (envQuaidWorkspace) {
    return _normalizeWorkspacePath(envQuaidWorkspace);
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
// When QUAID_INSTANCE is set, the Python bridge must point MEMORY_DB_PATH at the
// instance silo DB, not the root WORKSPACE DB.  Python reads MEMORY_DB_PATH first
// (lib/config.py:get_db_path) before falling back to config — so setting the wrong
// path here causes auto-inject recall to query an empty root-silo database instead
// of the active instance silo.
const _instanceForDbPath = String(process.env.QUAID_INSTANCE || "").trim();
const DB_PATH = _instanceForDbPath
  ? path.join(WORKSPACE, _instanceForDbPath, "data", "memory.db")
  : path.join(WORKSPACE, "data", "memory.db");
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
const BACKLOG_NOTIFY_STALE_MS = 90_000;

// Daemon signal infrastructure — writes extraction signals for the shared Python daemon.
// Use the instance-specific path when QUAID_INSTANCE is set, mirroring the Python
// daemon's _signal_dir() = _instance_root() / "data" / "extraction-signals".
// QUAID_INSTANCE is the current (primary) agent's full instance ID, e.g. "openclaw-main".
const _QUAID_INSTANCE = String(process.env.QUAID_INSTANCE || "").trim();
// Prefix: the adapter name portion of QUAID_INSTANCE (e.g. "openclaw", "claude-code").
// Strip the known adapter prefix from the front rather than stripping "-main" from the back,
// so non-main instance labels (e.g. "openclaw-livetest") compute the right prefix too.
// "openclaw-main"     → "openclaw"    (same as before)
// "openclaw-livetest" → "openclaw"    (was broken: produced "openclaw-livetest")
// "claude-code-main"  → "claude-code" (same as before)
const _KNOWN_ADAPTER_PREFIXES = ["claude-code", "openclaw", "standalone"];
const _QUAID_PREFIX = (() => {
  for (const pfx of _KNOWN_ADAPTER_PREFIXES) {
    if (_QUAID_INSTANCE.startsWith(`${pfx}-`) || _QUAID_INSTANCE === pfx) return pfx;
  }
  // Fallback for legacy or unknown formats: strip "-main" as before.
  return _QUAID_INSTANCE.endsWith("-main") ? _QUAID_INSTANCE.slice(0, -5) : _QUAID_INSTANCE;
})();

/**
 * Derive the Quaid instance ID for a given OC agent label.
 *
 * Produces "<prefix>-<label>" (e.g. "openclaw-coding") for multi-agent routing.
 * Do NOT use this for "the current running instance" — use _QUAID_INSTANCE directly.
 * When QUAID_INSTANCE is not set (legacy flat layout), returns the label as-is.
 */
function getInstanceId(agentLabel: string = "main"): string {
  const label = String(agentLabel || "main").trim().toLowerCase() || "main";
  return _QUAID_PREFIX ? `${_QUAID_PREFIX}-${label}` : label;
}

/** Daemon signal directory for a given agent's Quaid silo. */
function getDaemonSignalDir(agentId: string = "main"): string {
  const instanceId = getInstanceId(agentId);
  return instanceId
    ? path.join(WORKSPACE, instanceId, "data", "extraction-signals")
    : path.join(WORKSPACE, "data", "extraction-signals");
}

// Primary instance signal dir — use _QUAID_INSTANCE directly so non-main
// instance labels (e.g. "openclaw-livetest") resolve to their own silo.
const DAEMON_SIGNAL_DIR = _QUAID_INSTANCE
  ? path.join(WORKSPACE, _QUAID_INSTANCE, "data", "extraction-signals")
  : path.join(WORKSPACE, "data", "extraction-signals");

// In-process dedup for reset signals: prevents multiple gateway processes (or
// multiple bursts within the same process) from writing redundant reset signals
// for the same session within a single process lifetime. The filesystem marker
// in writeDaemonSignal handles cross-process dedup after gateway restarts; this
// Set handles same-process bursts where multiple callers race before the marker
// is visible on disk.
const _recentResetSignalsWritten = new Map<string, number>(); // session_id → timestamp

/**
 * Read the install-time lower bound from data/installed-at.json.
 * Mirrors Python's _read_installed_at() in core/extraction_daemon.py.
 * Returns 0 if the file is missing or unreadable (no floor applied).
 */
function readInstalledAtMs(): number {
  try {
    const p = _QUAID_INSTANCE
      ? path.join(WORKSPACE, _QUAID_INSTANCE, "data", "installed-at.json")
      : path.join(WORKSPACE, "data", "installed-at.json");
    const raw = JSON.parse(fs.readFileSync(p, "utf8")) as Record<string, unknown>;
    const ts = String(raw.installedAt || "").trim();
    if (ts) return new Date(ts).getTime();
  } catch {}
  return 0;
}

const sessionTranscriptPaths = new Map<string, string>();
// Maps sessionId → agentId for multi-agent daemon signal routing.
// Populated by the session index watcher as sessions are discovered.
const sessionIdToAgentId = new Map<string, string>();
const QUAID_SESSION_PRESERVE_DIR = path.join(QUAID_LOGS_DIR, "quaid", "sessions");
const SESSION_INDEX_POLL_MS = 1000;
let sessionIndexWatcherStarted = false;
let sessionIndexWatcherTimer: NodeJS.Timeout | null = null;

type ActiveInteractiveSession = {
  key: string;
  sessionId: string;
  sessionFile: string;
  mtimeMs: number;
  updatedAt: number;
  lastChannel: string;
  lastTo: string;
};

function getOpenClawSessionsBaseDir(): string {
  return path.dirname(getOpenClawSessionsPath());
}

function getOpenClawSessionFile(sessionId: string): string {
  return path.join(getOpenClawSessionsBaseDir(), `${sessionId}.jsonl`);
}

function getPreservedSessionFile(sessionId: string): string {
  return path.join(QUAID_SESSION_PRESERVE_DIR, `${sessionId}.jsonl`);
}

function isAutoInjectEnabled(config: Record<string, any> = getMemoryConfig()): boolean {
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

type LastUserMessageQuery = { text: string; seenAtMs: number } | null;

function scrubAutoInjectQuery(raw: string): string {
  return String(raw || "")
    // Strip our own prior injections that OC persists back into future turns
    .replace(/<tool_hint>[\s\S]*?<\/tool_hint>/gi, "")
    .replace(/<injected_memories>[\s\S]*?<\/injected_memories>/gi, "")
    // Strip OC metadata block BEFORE bracket-strip so that when raw starts
    // with "Sender (untrusted metadata):...json...[Wed...] message", the [Wed...]
    // prefix becomes leading after metadata removal and is caught by the bracket step.
    .replace(/\w[\w\s]* \(untrusted metadata\):[\s\S]*?```[\s\S]*?```/gi, "")
    // Strip leading code fence blocks (OC JSON metadata wrapper)
    .replace(/^```[\w]*\r?\n[\s\S]*?```\s*/i, "")
    // Strip OC metadata prefix patterns (timestamp brackets, separators)
    .replace(/^System:\s*/i, "")
    .replace(/^\s*(\[.*?\]\s*)+/s, "")
    .replace(/^---\s*/m, "")
    .trim();
}

function selectAutoInjectQuery(
  event: any,
  lastUserMessageQuery: LastUserMessageQuery,
  nowMs: number = Date.now(),
): { query: string; source: string; rawPrompt: string } {
  const rawPrompt = String(event?.prompt || "").trim();
  const eventMessages: any[] = Array.isArray(event?.messages) ? event.messages : [];

  const eventTextRaw = String(
    facade.getMessageText(event?.message || event) ||
    event?.text ||
    event?.content ||
    ""
  ).trim();
  const eventTextScrubbed = scrubAutoInjectQuery(eventTextRaw);

  const extractFromOCPromptJson = (raw: string): string => {
    try {
      const m = raw.match(/^```[\w]*\r?\n([\s\S]+?)\r?\n```/m);
      if (!m) return "";
      const obj = JSON.parse(m[1]);
      const msgs: any[] = obj?.messages ?? obj?.prompt?.messages ?? [];
      const last = [...msgs].reverse().find((x: any) => x?.role === "user");
      if (!last) return "";
      const c = last.content;
      const text = typeof c === "string" ? c
        : Array.isArray(c) ? c.filter((b: any) => b?.type === "text").map((b: any) => String(b.text || "")).join("")
        : "";
      return scrubAutoInjectQuery(text).slice(0, 500);
    } catch {
      return "";
    }
  };

  // Prefer direct hook payload text first — this recovers fresh-session OC turns
  // where before_prompt_build arrives with messages=0 and event.prompt is empty/stale.
  if (eventTextScrubbed.length >= 3 && !eventTextScrubbed.startsWith("/")) {
    return { query: eventTextScrubbed.slice(0, 500), source: "event_text_scrubbed", rawPrompt };
  }

  // Next prefer the most recent user text captured by message_received if it is fresh.
  if (lastUserMessageQuery && (nowMs - lastUserMessageQuery.seenAtMs) <= 10_000 && lastUserMessageQuery.text.length >= 3) {
    return {
      query: lastUserMessageQuery.text.slice(0, 500),
      source: "message_received_cache",
      rawPrompt,
    };
  }

  const scrubbed = scrubAutoInjectQuery(rawPrompt);
  if (scrubbed.length >= 3) {
    return { query: scrubbed.slice(0, 500), source: "rawPrompt_scrubbed", rawPrompt };
  }

  const jsonExtracted = extractFromOCPromptJson(rawPrompt);
  if (jsonExtracted.length >= 3) {
    return { query: jsonExtracted, source: "oc_prompt_json", rawPrompt };
  }

  const lastUserMsg = eventMessages.slice().reverse().find((m: any) => m?.role === "user");
  if (lastUserMsg) {
    const c = lastUserMsg.content;
    const raw = typeof c === "string" ? c
      : Array.isArray(c) ? c.filter((b: any) => b?.type === "text").map((b: any) => String(b.text || "")).join("\n")
      : "";
    const query = scrubAutoInjectQuery(raw).slice(0, 500);
    if (query.length >= 3) {
      return { query, source: "event.messages", rawPrompt };
    }
  }

  return {
    query: rawPrompt.slice(0, 500),
    source: rawPrompt ? "rawPrompt_raw" : "empty",
    rawPrompt,
  };
}

function readSessionsIndex(): Record<string, any> {
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

function resolveSessionKeyForSessionId(sessionId: string): string {
  const sid = String(sessionId || "").trim();
  if (!sid) return "";
  const data = readSessionsIndex();
  for (const [key, row] of Object.entries(data || {})) {
    if (String((row as any)?.sessionId || "").trim() === sid) {
      return String(key || "").trim();
    }
  }
  return "";
}

function isInternalSessionContext(event: any, ctx: any): boolean {
  const sessionId = String(ctx?.sessionId || event?.sessionId || "").trim();
  if (facade.isInternalQuaidSession(sessionId)) {
    return true;
  }
  const sessionKey = String(
    ctx?.sessionKey
      || event?.sessionKey
      || event?.targetSessionKey
      || resolveSessionKeyForSessionId(sessionId)
  ).trim().toLowerCase();
  return Boolean(sessionKey) && (sessionKey.includes("quaid-llm") || sessionKey.includes("openresponses:"));
}

function pickActiveInteractiveSession(data: Record<string, any>): ActiveInteractiveSession | null {
  const entries = (Object.entries(data || {}) as Array<[string, any]>)
    .filter(([key, row]) => (
      row
      && typeof row === "object"
      && typeof row?.sessionId === "string"
      && key.startsWith("agent:main:")
    ))
    .map(([key, row]) => {
      const sessionId = String(row?.sessionId || "").trim();
      const sessionFile = getOpenClawSessionFile(sessionId);
      let mtimeMs = 0;
      try {
        mtimeMs = fs.statSync(sessionFile).mtimeMs;
      } catch {}
      return {
        key,
        sessionId,
        sessionFile,
        mtimeMs,
        updatedAt: Number(row?.updatedAt || 0),
        lastChannel: String(row?.lastChannel || "").trim(),
        lastTo: String(row?.lastTo || "").trim(),
      };
    })
    .filter((row) => row.sessionId);
  // Sort priority:
  // 1. TUI/telegram sessions (agent:main:tui-*, agent:main:telegram:*) outrank
  //    agent:main:main.  When a user is active in the TUI, OC may still refresh
  //    agent:main:main's updatedAt for background/relay purposes, causing it to
  //    win on timestamp alone and making the watcher track the wrong session.
  //    EXCEPTION: if all TUI/telegram entries are stale (>5 min older than main),
  //    fall back to recency comparison — the TUI is no longer actively registered
  //    and main holds the genuine current session.
  // 2. Within the same tier, prefer newest updatedAt; break ties with transcript mtimeMs.
  const TIER_STALENESS_THRESHOLD_MS = 5 * 60 * 1000; // 5 minutes
  const mainEntry = entries.find((e) => e.key === "agent:main:main");
  const isHighTierKey = (key: string): boolean =>
    key.startsWith("agent:main:tui-") || key.startsWith("agent:main:telegram:");
  const highTierEntries = entries.filter((e) => isHighTierKey(e.key));
  const bestHighTierUpdatedAt = highTierEntries.reduce(
    (max, e) => Math.max(max, e.updatedAt),
    0,
  );
  // If main is significantly newer than every TUI/telegram entry, suppress tier boost.
  const suppressTierBoost =
    mainEntry != null
    && mainEntry.updatedAt - bestHighTierUpdatedAt > TIER_STALENESS_THRESHOLD_MS;
  const sessionTier = (key: string): number =>
    (!suppressTierBoost && isHighTierKey(key)) ? 1 : 0;
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

  // Fallback: sessions.json absent or has no recognized entries (e.g. fresh TUI install).
  // Scan the sessions directory for the most recently modified .jsonl transcript file.
  try {
    const dir = getOpenClawSessionsBaseDir();
    const names = fs.readdirSync(dir).filter((n) =>
      n.endsWith(".jsonl") && !n.includes(".jsonl.") && n.length > 6
    );
    if (!names.length) return null;
    let best: { sessionId: string; sessionFile: string; mtimeMs: number } | null = null;
    for (const name of names) {
      const sessionId = name.slice(0, -6); // strip .jsonl
      const sessionFile = path.join(dir, name);
      let mtimeMs = 0;
      try { mtimeMs = fs.statSync(sessionFile).mtimeMs; } catch {}
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
      lastTo: "",
    };
  } catch {
    return null;
  }
}

function latestResetBackup(sessionId: string): string | null {
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

function preserveSessionTranscript(sessionId: string, preferredPath: string | null | undefined, reason: string): string | null {
  const candidates: string[] = [];
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
      candidates: deduped,
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
      dest_path: destPath,
    });
    return destPath;
  } catch (err: unknown) {
    writeHookTrace("session_index.transcript_preserve_error", {
      session_id: sessionId,
      reason,
      source_path: sourcePath,
      error: String((err as Error)?.message || err),
    });
    return null;
  }
}

function extractSessionMessageText(message: any): string {
  if (!message) return "";
  if (typeof message.text === "string") return message.text;
  if (typeof message.content === "string") return message.content;
  if (Array.isArray(message.content)) {
    return message.content
      .map((part: any) => (typeof part?.text === "string" ? part.text : ""))
      .filter(Boolean)
      .join(" ")
      .trim();
  }
  return "";
}

function writeDaemonSignal(
  sessionId: string,
  signalType: "compaction" | "reset" | "session_end",
  meta?: Record<string, any>,
): string | null {
  if (!sessionId) return null;
  const transcriptPath = sessionTranscriptPaths.get(sessionId) || "";
  if (!transcriptPath) {
    // Try to resolve from OC sessions directories (multiple locations)
    const candidates = [
      path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", `${sessionId}.jsonl`),
      path.join(os.homedir(), ".openclaw", "sessions", `${sessionId}.jsonl`),
      path.join(WORKSPACE, "logs", "quaid", "sessions", `${sessionId}.jsonl`),
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
    // Original JSONL not found — OC may have moved it entirely (not just emptied it).
    // Try the .reset.* backup directly so the daemon still gets content to extract.
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

  // For reset signals, OC may have already moved the transcript content to a
  // .reset.* snapshot before this signal fires. If the resolved path is empty
  // (0 bytes) or missing (ENOENT) and a .reset.* backup exists, use the backup
  // so the daemon has content to extract from.
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
      // File missing (ENOENT) — OC moved it entirely to a .reset.* backup.
      const backup = latestResetBackup(sessionId);
      if (backup) {
        resolvedPath = backup;
      }
    }
  }

  // Route to the agent's own Quaid silo if known, otherwise the primary instance.
  // For non-primary OC agents (multi-agent), derive the silo from the agent label.
  // For the primary instance (label absent or "main"), use DAEMON_SIGNAL_DIR directly.
  const agentLabel = sessionIdToAgentId.get(sessionId);
  const signalDir = (!agentLabel || agentLabel === "main")
    ? DAEMON_SIGNAL_DIR
    : getDaemonSignalDir(agentLabel);
  try {
    fs.mkdirSync(signalDir, { recursive: true });
  } catch {}

  // Filesystem-based dedup for reset signals: OC restarts its gateway process
  // several times after /reset, each restart clearing the in-memory
  // shouldProcessLifecycleSignal state and re-queuing signals. A marker file
  // records the last time a reset signal was written for this session so
  // the dedup survives gateway restarts. 5-minute window is well above the
  // longest extraction run and well below any realistic re-use of the same
  // session ID.
  if (signalType === "reset") {
    const RECENT_RESET_SUPPRESS_MS = 5 * 60 * 1000;
    // In-process dedup: check Map before hitting the filesystem. Prevents bursts
    // within a single process from writing multiple signals before the marker
    // file is visible to concurrent callers.
    const _inProcLast = _recentResetSignalsWritten.get(sessionId);
    if (_inProcLast !== undefined && Date.now() - _inProcLast < RECENT_RESET_SUPPRESS_MS) {
      console.log(`[quaid][daemon-signal] suppressed duplicate reset signal for session=${sessionId} (in-process dedup)`);
      writeHookTrace("session_index.signal_suppressed", { reason: "in_process_dedup", session_id: sessionId });
      return null;
    }
    const markerPath = path.join(signalDir, `.last_reset_signal.${sessionId}`);
    try {
      const markerStat = fs.statSync(markerPath);
      if (Date.now() - markerStat.mtimeMs < RECENT_RESET_SUPPRESS_MS) {
        console.log(`[quaid][daemon-signal] suppressed duplicate reset signal for session=${sessionId} (recent marker exists)`);
        writeHookTrace("session_index.signal_suppressed", { reason: "recent_reset_marker", session_id: sessionId });
        return null;
      }
    } catch {
      // Marker absent or unreadable — proceed to write signal.
    }
    try { fs.writeFileSync(markerPath, sessionId, { mode: 0o600 }); } catch {}
    _recentResetSignalsWritten.set(sessionId, Date.now());
  }

  const payload = {
    type: signalType,
    session_id: sessionId,
    transcript_path: resolvedPath,
    adapter: "openclaw",
    supports_compaction_control: true,
    timestamp: new Date().toISOString(),
    meta: meta || {},
  };
  const fname = `${Date.now()}_${process.pid}_${signalType}.json`;
  const sigPath = path.join(signalDir, fname);
  try {
    fs.writeFileSync(sigPath, JSON.stringify(payload), { mode: 0o600 });
    console.log(`[quaid][daemon-signal] wrote ${signalType} signal for session=${sessionId} path=${sigPath}`);
    return sigPath;
  } catch (err: unknown) {
    console.error(`[quaid][daemon-signal] write failed: ${String((err as Error)?.message || err)}`);
    return null;
  }
}

function ensureDaemonAlive(): void {
  try {
    const quaidBin = path.join(PYTHON_PLUGIN_ROOT, "quaid");
    execFileSync(quaidBin, ["daemon", "start"], {
      encoding: "utf-8",
      timeout: 10_000,
      env: buildPythonEnv(),
    });
  } catch (err: unknown) {
    console.warn(`[quaid][daemon] ensure_alive failed: ${String((err as Error)?.message || err)}`);
  }
}

for (const p of [QUAID_RUNTIME_DIR, QUAID_TMP_DIR, QUAID_NOTES_DIR, QUAID_INJECTION_LOG_DIR, QUAID_NOTIFY_DIR, QUAID_LOGS_DIR]) {
  try {
    fs.mkdirSync(p, { recursive: true });
  } catch (err: unknown) {
    console.error(`[quaid][startup] failed to create runtime dir: ${p}`, (err as Error)?.message || String(err));
  }
}

function _jsonSafe(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return '"[unserializable]"';
  }
}

function writeHookTrace(event: string, data: Record<string, unknown> = {}): void {
  const payload = {
    ts: new Date().toISOString(),
    event,
    ...data,
  };
  try {
    fs.appendFileSync(QUAID_HOOK_TRACE_PATH, `${_jsonSafe(payload)}\n`, "utf8");
  } catch (err: unknown) {
    console.warn(
      `[quaid][trace] write failed event=${event} err=${String((err as Error)?.message || err)}`
    );
  }
}

function summarizeRecallResults(results: any[], limit: number = 5): Array<Record<string, unknown>> {
  return (Array.isArray(results) ? results : [])
    .slice(0, Math.max(1, limit))
    .map((row: any) => ({
      id: typeof row?.id === "string" ? row.id : undefined,
      text: String(row?.text || "").trim().slice(0, 180),
      similarity: Number.isFinite(Number(row?.similarity)) ? Number(Number(row.similarity).toFixed(3)) : undefined,
      category: typeof row?.category === "string" ? row.category : undefined,
      via: typeof row?.via === "string" ? row.via : undefined,
      extraction_confidence: Number.isFinite(Number(row?.extractionConfidence))
        ? Number(Number(row.extractionConfidence).toFixed(3))
        : undefined,
      created_at: typeof row?.createdAt === "string" ? row.createdAt : undefined,
    }));
}

function summarizeRecallDiagnostics(diagnostics: unknown): Record<string, unknown> | null {
  const meta = diagnostics && typeof diagnostics === "object" && !Array.isArray(diagnostics)
    ? (diagnostics as Record<string, any>).meta
    : null;
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    return null;
  }
  const qualityGate = meta.quality_gate && typeof meta.quality_gate === "object" && !Array.isArray(meta.quality_gate)
    ? meta.quality_gate
    : {};
  const evaluation = qualityGate.evaluation && typeof qualityGate.evaluation === "object" && !Array.isArray(qualityGate.evaluation)
    ? qualityGate.evaluation
    : {};
  const turnDetails = Array.isArray(meta.turn_details) ? meta.turn_details : [];
  const firstTurn = turnDetails.length > 0 && turnDetails[0] && typeof turnDetails[0] === "object" ? turnDetails[0] : {};
  const planner = firstTurn.planner && typeof firstTurn.planner === "object" && !Array.isArray(firstTurn.planner)
    ? firstTurn.planner
    : {};
  const storeRuns = Array.isArray(meta.store_runs) ? meta.store_runs : [];
  const phases = meta.phases_ms && typeof meta.phases_ms === "object" && !Array.isArray(meta.phases_ms)
    ? meta.phases_ms
    : {};
  return {
    mode: typeof meta.mode === "string" ? meta.mode : undefined,
    stop_reason: typeof meta.stop_reason === "string" ? meta.stop_reason : undefined,
    selected_path: typeof meta.selected_path === "string" ? meta.selected_path : undefined,
    planned_stores: Array.isArray(meta.planned_stores) ? meta.planned_stores.slice(0, 8) : undefined,
    planned_project: typeof meta.planned_project === "string" ? meta.planned_project : undefined,
    planner: {
      bailout_reason: typeof planner.bailout_reason === "string" ? planner.bailout_reason : undefined,
      planner_profile: typeof planner.planner_profile === "string" ? planner.planner_profile : undefined,
      queries_count: Number.isFinite(Number(planner.queries_count)) ? Number(planner.queries_count) : undefined,
      used_llm: typeof planner.used_llm === "boolean" ? planner.used_llm : undefined,
    },
    store_runs: storeRuns.slice(0, 6).map((run: any) => ({
      store: typeof run?.store === "string" ? run.store : undefined,
      result_count: Number.isFinite(Number(run?.result_count)) ? Number(run.result_count) : undefined,
      total_ms: Number.isFinite(Number(run?.total_ms)) ? Number(run.total_ms) : undefined,
      selected_path: typeof run?.selected_path === "string" ? run.selected_path : undefined,
    })),
    quality_gate: {
      fast_drill_candidate: typeof qualityGate.fast_drill_candidate === "boolean" ? qualityGate.fast_drill_candidate : undefined,
      fast_drill_enabled: typeof qualityGate.fast_drill_enabled === "boolean" ? qualityGate.fast_drill_enabled : undefined,
      fast_drill_reasons: Array.isArray(qualityGate.fast_drill_reasons) ? qualityGate.fast_drill_reasons.slice(0, 8) : undefined,
      requirements: Array.isArray(evaluation.requirements) ? evaluation.requirements.slice(0, 8) : undefined,
      covered_terms_ratio: Number.isFinite(Number(evaluation.covered_terms_ratio))
        ? Number(Number(evaluation.covered_terms_ratio).toFixed(3))
        : undefined,
      top_similarity: Number.isFinite(Number(evaluation.top_similarity))
        ? Number(Number(evaluation.top_similarity).toFixed(3))
        : undefined,
    },
    phases_ms: {
      total_ms: Number.isFinite(Number(phases.total_ms)) ? Number(phases.total_ms) : undefined,
      store_plan_wall_ms: Number.isFinite(Number(phases.store_plan_wall_ms)) ? Number(phases.store_plan_wall_ms) : undefined,
      planner_ms: Number.isFinite(Number(phases.planner_ms)) ? Number(phases.planner_ms) : undefined,
      reranker_ms: Number.isFinite(Number(phases.reranker_ms)) ? Number(phases.reranker_ms) : undefined,
    },
  };
}

function _envTimeoutMs(name: string, fallbackMs: number): number {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}

const EXTRACT_PIPELINE_TIMEOUT_MS = _envTimeoutMs("QUAID_EXTRACT_PIPELINE_TIMEOUT_MS", 300_000);
const EVENTS_EMIT_TIMEOUT_MS = _envTimeoutMs("QUAID_EVENTS_TIMEOUT_MS", 300_000);
// QUICK_PROJECT_SUMMARY_TIMEOUT_MS removed — project events now emitted from Python extraction.

function resolveAdapterMemoryDbPath(
  workspace: string,
  instanceId: string,
  legacyDbPath: string,
): string {
  const normalizedInstance = String(instanceId || "").trim();
  return normalizedInstance
    ? path.join(workspace, normalizedInstance, "data", "memory.db")
    : legacyDbPath;
}

function buildPythonEnv(extra: Record<string, string | undefined> = {}): Record<string, string | undefined> {
  const sep = process.platform === "win32" ? ";" : ":";
  const existing = String(process.env.PYTHONPATH || "").trim();
  const pyPath = existing ? `${PYTHON_PLUGIN_ROOT}${sep}${existing}` : PYTHON_PLUGIN_ROOT;
  // When QUAID_INSTANCE is set, Python resolves the DB path from QUAID_HOME/QUAID_INSTANCE
  // via get_adapter().instance_root(). Do NOT override with MEMORY_DB_PATH — that would
  // bypass the silo-specific path and point at the workspace-root legacy DB instead.
  // Only set MEMORY_DB_PATH in the legacy flat-layout case (no QUAID_INSTANCE).
  const memoryDbPath = resolveAdapterMemoryDbPath(WORKSPACE, _QUAID_INSTANCE, DB_PATH);
  return {
    ...process.env,
    MEMORY_DB_PATH: memoryDbPath,
    MEMORY_RUNTIME_DIR: QUAID_RUNTIME_DIR,
    QUAID_HOME: WORKSPACE,
    QUAID_WORKSPACE: WORKSPACE,
    CLAWDBOT_WORKSPACE: WORKSPACE,
    // Explicitly set QUAID_INSTANCE so Python subprocesses always know which
    // agent silo they are serving. Callers pass agent-specific overrides via
    // extra (e.g. getInstanceId(agentLabel)) when routing to a non-primary agent.
    QUAID_INSTANCE: _QUAID_INSTANCE || undefined,
    PYTHONPATH: pyPath,
    ...extra,
  };
}

function getDatastoreStatsSync(): Record<string, any> | null {
  try {
    const output = execFileSync("python3", [PYTHON_SCRIPT, "stats"], {
      encoding: "utf-8",
      timeout: 30_000,
      env: buildPythonEnv(),
    });
    const parsed = JSON.parse(output || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    return parsed as Record<string, any>;
  } catch (err: unknown) {
    const msg = `[quaid] datastore stats read failed: ${String((err as Error)?.message || err)}`;
    const retrieval = memoryConfigResolver.getMemoryConfig().retrieval || {};
    const failHard = typeof retrieval.fail_hard === "boolean"
      ? retrieval.fail_hard
      : typeof retrieval.failHard === "boolean"
        ? retrieval.failHard
        : true;
    if (failHard) {
      throw new Error(msg, { cause: err as Error });
    }
    console.warn(msg);
    return null;
  }
}

type AdapterMemoryConfigResolver = {
  getMemoryConfig: () => any;
  resolveMemoryConfigPath: () => string;
};

function buildFallbackMemoryConfig(): any {
  return {
    models: {
      llmProvider: "default",
      deepReasoning: "default",
      fastReasoning: "default",
      deepReasoningModelClasses: {
        anthropic: "claude-sonnet-4-6",
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

function createAdapterMemoryConfigResolver(): AdapterMemoryConfigResolver {
  let memoryConfigErrorLogged = false;
  let memoryConfigMtimeMs = -1;
  let memoryConfigPath = "";
  let memoryConfig: any = null;

  function memoryConfigCandidates(): string[] {
    const candidates: string[] = [];
    // Instance-specific config wins: QUAID_HOME/QUAID_INSTANCE/config/memory.json
    const instance = String(process.env.QUAID_INSTANCE || "").trim();
    if (instance) {
      candidates.push(path.join(WORKSPACE, instance, "config", "memory.json"));
    }
    // Shared config as fallback (embeddings + cross-instance settings)
    candidates.push(
      path.join(WORKSPACE, "shared", "config", "memory.json"),
      path.join(WORKSPACE, "config", "memory.json"),
      path.join(os.homedir(), ".quaid", "memory-config.json"),
      path.join(process.cwd(), "memory-config.json"),
    );
    return candidates;
  }

  function resolveMemoryConfigPath(): string {
    for (const candidate of memoryConfigCandidates()) {
      try {
        if (fs.existsSync(candidate)) {
          return candidate;
        }
      } catch {
        // Ignore probe errors and continue.
      }
    }
    return memoryConfigCandidates()[0];
  }

  function getMemoryConfig(): any {
    const configPath = resolveMemoryConfigPath();
    if (configPath !== memoryConfigPath) {
      memoryConfigMtimeMs = -1;
      memoryConfigPath = configPath;
    }
    let mtimeMs = -1;
    try {
      mtimeMs = fs.statSync(configPath).mtimeMs;
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err || "");
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
    } catch (err: unknown) {
      if (!memoryConfigErrorLogged) {
        memoryConfigErrorLogged = true;
        console.error(`[memory] failed to load memory config (${configPath}): ${(err as Error)?.message || String(err)}`);
      }
      if (isMissingFileError(err)) {
        memoryConfig = buildFallbackMemoryConfig();
        memoryConfigMtimeMs = -1;
        return memoryConfig;
      }
      // Prevent mutual recursion with isFailHardEnabled() while preserving old behavior.
      memoryConfig = buildFallbackMemoryConfig();
      memoryConfigMtimeMs = mtimeMs;
      if (isFailHardEnabled()) {
        throw err;
      }
    }
    return memoryConfig;
  }

  return {
    getMemoryConfig,
    resolveMemoryConfigPath,
  };
}

const memoryConfigResolver = createAdapterMemoryConfigResolver();

function getMemoryConfig(): any {
  return memoryConfigResolver.getMemoryConfig();
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

function getGatewayDefaultProvider(): string {
  try {
    const cfg = _readOpenClawConfig();
    const primaryModel = String(
      cfg?.agents?.main?.modelPrimary || cfg?.agents?.defaults?.modelPrimary || ""
    ).trim();
    if (primaryModel.includes("/")) {
      const provider = primaryModel.split("/", 1)[0];
      const normalized = String(provider || "").trim().toLowerCase();
      if (normalized) { return normalized; }
    }
  } catch {
    // _readOpenClawConfig already handles warnings and fail-open behavior.
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
      console.log(`[quaid][billing] paid provider active for deep reasoning: ${deep.provider}/${deep.model}`);
    }
  } catch (err: unknown) {
    errors.push(`deep reasoning model resolution failed: ${String((err as Error)?.message || err)}`);
  }

  try {
    const fast = facade.resolveTierModel("fast");
    console.log(`[quaid][startup] fast model resolved: provider=${fast.provider} model=${fast.model}`);
    const paidProviders = new Set(["openai-compatible"]);
    if (paidProviders.has(fast.provider)) {
      console.log(`[quaid][billing] paid provider active for fast reasoning: ${fast.provider}/${fast.model}`);
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
const BEFORE_PROMPT_BUILD_DEADLINE_MS = 35_000;

function getOpenClawSessionsPath(): string {
  return path.join(os.homedir(), ".openclaw", "agents", "main", "sessions", "sessions.json");
}

// ============================================================================
// Session ID Helper
// ============================================================================

function resolveSessionIdFromSessionKey(sessionKey: string): string {
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
  } catch {}
  return "";
}

function resolveMostRecentSessionId(): string {
  try {
    const sessionsPath = getOpenClawSessionsPath();
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
    const sessionsPath = getOpenClawSessionsPath();
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

async function requestSessionCompaction(sessionKey: string): Promise<{ ok: boolean; compacted?: unknown; raw?: string }> {
  const out = await spawnWithTimeout({
    cwd: WORKSPACE,
    env: process.env,
    timeoutMs: 20_000,
    label: "[quaid][gateway] sessions.compact",
    argv: [
      "openclaw",
      "gateway",
      "call",
      "sessions.compact",
      "--json",
      "--params",
      JSON.stringify({ key: sessionKey }),
    ],
  });
  const parsed = JSON.parse(String(out || "{}"));
  return { ok: Boolean(parsed?.ok), compacted: parsed?.compacted, raw: String(out || "") };
}

function parseSessionMessagesJsonl(sessionFile: string): any[] {
  let content: string;
  try {
    content = fs.readFileSync(sessionFile, "utf8");
  } catch {
    return [];
  }
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
// PROJECT_UPDATER removed — project events now emitted from Python extraction.
const EVENTS_SCRIPT = path.join(PYTHON_PLUGIN_ROOT, "core/runtime/events.py");
// Re-entrancy guard for beforePromptBuildHandler.
// OC reuses the TUI session key for internal openresponses LLM calls spawned by
// callConfiguredLLM. This means session-key filtering cannot distinguish user messages
// from internal calls. Instead we track whether a recall is already in flight: if
// before_prompt_build fires while we are inside recallMemories, it must be an internal
// call and we skip to prevent a recursive recall→LLM→recall→... loop.
let _beforePromptBuildInFlight = false;

function _getGatewayCredential(providers: string[]): string | undefined {
  for (const provider of providers) {
    const normalized = String(provider || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "_");
    if (!normalized) continue;
    const directKey = String(process.env[`${normalized}_API_KEY`] || "").trim();
    if (directKey) return directKey;
    const directToken = String(process.env[`${normalized}_TOKEN`] || "").trim();
    if (directToken) return directToken;
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
    cache_read_tokens: data?.usage?.cache_read_input_tokens || 0,
    cache_creation_tokens: data?.usage?.cache_creation_input_tokens || 0,
    truncated: false,
  };
}

function _spawnWithTimeout(
  script: string, command: string, args: string[],
  label: string, env: Record<string, string | undefined>,
  timeoutMs: number = PYTHON_BRIDGE_TIMEOUT_MS
): Promise<string> {
  return spawnWithTimeout({
    cwd: WORKSPACE,
    env: buildPythonEnv(env) as NodeJS.ProcessEnv,
    timeoutMs,
    label,
    argv: ["python3", script, command, ...args],
  });
}

/**
 * Spawn a fire-and-forget Python notification script safely.
 * Writes code to a temp file to avoid shell injection via inline -c strings.
 * The script auto-deletes its temp file on completion.
 */
function spawnNotifyScript(scriptBody: string): boolean {
  const notifyLogFile = path.join(QUAID_LOGS_DIR, "notify-worker.log");
  const preamble = `import sys, os\nsys.path.insert(0, ${JSON.stringify(PYTHON_PLUGIN_ROOT)})\n`;
  return spawnDetachedScript({
    scriptDir: QUAID_NOTIFY_DIR,
    logFile: notifyLogFile,
    scriptPrefix: preamble,
    scriptBody,
    env: buildPythonEnv() as NodeJS.ProcessEnv,
    interpreter: "python3",
    filePrefix: "notify",
    fileExtension: ".py",
  });
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

function preprocessTranscriptText(text: string): string {
  return String(text || "")
    .replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "")
    .replace(/\n?\[message_id:\s*\d+\]/gi, "")
    .trim();
}

function shouldSkipTranscriptText(roleOrText: "user" | "assistant" | string, maybeText?: string): boolean {
  // Keep compatibility with both callback shapes:
  // - transcript formatter: shouldSkipText(role, text)
  // - session-timeout manager: shouldSkipText(text)
  const text = typeof maybeText === "string" ? maybeText : String(roleOrText || "");
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
  instanceRoot: _QUAID_INSTANCE ? path.join(WORKSPACE, _QUAID_INSTANCE) : undefined,
  pluginRoot: PYTHON_PLUGIN_ROOT,
  dbPath: resolveAdapterMemoryDbPath(WORKSPACE, _QUAID_INSTANCE, DB_PATH),
  eventSource: "openclaw_adapter",
  execPython: createPythonBridgeExecutor({
    scriptPath: PYTHON_SCRIPT,
    dbPath: resolveAdapterMemoryDbPath(WORKSPACE, _QUAID_INSTANCE, DB_PATH),
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
  // emitProjectEventBackground removed — project events now emitted from Python extraction.
  callLLM: callConfiguredLLM,
  getDefaultLLMProvider: getGatewayDefaultProvider,
  adapterName: "openclaw_adapter",
  defaultOwner: "quaid",
  isSystemSession: (sid: string) =>
    sid.startsWith("quaid-fast-") || sid.startsWith("quaid-deep-") || sid.includes("quaid-llm"),
  runtimeDir: QUAID_RUNTIME_DIR,
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
    // Keep runtime log transcripts as a last-resort fallback only.
    path.join(WORKSPACE, "logs", "quaid", "sessions"),
  ],
  readSessionMessagesFile: (sessionFile: string) => parseSessionMessagesJsonl(sessionFile),
  listCompactionSessions,
  requestSessionCompaction,
  initDatastore: () => {
    execFileSync("python3", [PYTHON_SCRIPT, "init"], {
      timeout: 20_000,
      env: buildPythonEnv(),
    });
  },
  getDatastoreStatsSync,
  getMemoryConfig,
  isSystemEnabled,
  isFailHardEnabled,
  trace: process.env.QUAID_TOOL_HINT_TRACE === "1" ? writeHookTrace : undefined,
  transcriptFormat: {
    preprocessText: preprocessTranscriptText,
    shouldSkipText: shouldSkipTranscriptText,
    speakerLabel: (role: "user" | "assistant") => role === "user" ? "User" : "Alfie",
  },
});

const getProjectNames = () => facade.getProjectNames();

// Shared recall abstraction — used by both memory_recall tool and auto-inject
type RecallOptions = FacadeRecallOptions & {
  waitForExtraction?: boolean;  // wait on facade extraction queue (tool=yes, inject=no)
  sourceTag?: "tool" | "auto_inject" | "unknown";
};


// ============================================================================
// Plugin Definition
// ============================================================================

const quaidPlugin = {
  id: "quaid",
  name: "Memory (Local Graph)",
  description: "Local graph-based memory with SQLite + Ollama embeddings",
  kind: "memory" as const,
  configSchema,

  register(api: OpenClawPluginApi<PluginConfig>) {
    console.log("[quaid] Registering local graph memory plugin");

    // Fail fast on model/provider/config mismatches so runtime doesn't degrade silently.
    runStartupSelfCheck();
    const strictContracts = facade.isPluginStrictMode();
    const contractDecl = loadAdapterContractDeclarations(strictContracts);
    if (contractDecl.enabled) {
      validateApiSurface(contractDecl.api, strictContracts, (m) => console.warn(m));
    }
    const registeredApi = new Set<string>(["openclaw_adapter_entry"]);
    const getMemoryConfig = () => facade.getConfig();
    const isSystemEnabled = (system: "memory" | "journal" | "projects" | "workspace") =>
      facade.isSystemEnabled(system);
    const isFailHardEnabled = () => facade.isFailHardEnabled();
    const readSessionMessagesFile = (sessionFile: string) => facade.readSessionMessagesFile(sessionFile);
    const wrapHookHandler = (registrationType: "on" | "registerHook", eventName: string, handler: any) => {
      return async (...args: any[]) => {
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
          has_ctx: Boolean(ctx),
        });
        console.log(
          `[quaid][debug][hook.invoke] registration=${registrationType} event=${eventName} session=${sessionId || "unknown"} messages=${messageCount}`
        );
        try {
          const out = await handler(...args);
          writeHookTrace("hook.debug.complete", {
            registration_type: registrationType,
            hook_event: eventName,
            session_id: sessionId,
          });
          return out;
        } catch (err: unknown) {
          writeHookTrace("hook.debug.error", {
            registration_type: registrationType,
            hook_event: eventName,
            session_id: sessionId,
            error: String((err as Error)?.message || err),
          });
          throw err;
        }
      };
    };
    const onChecked = (eventName: string, handler: any, options?: any) => {
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
        priority: Number(options?.priority || 0),
      });
      return api.on(eventName as any, wrapHookHandler("on", eventName, handler), options);
    };
    const registerInternalHookChecked = (eventName: string, handler: any, options?: any) => {
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
        priority: Number(options?.priority || 0),
      });
      return api.registerHook(eventName as any, wrapHookHandler("registerHook", eventName, handler), options);
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

    // Ensure database exists (facade owns the init policy; adapter supplies callback).
    try {
      const initialized = facade.initializeDatastoreIfMissing();
      if (initialized) {
        console.log("[quaid] Datastore initialization complete");
      }
    } catch (err: unknown) {
      console.error("[quaid] Datastore initialization failed:", (err as Error).message);
      if (isFailHardEnabled()) {
        throw err;
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

    let timeoutManager: SessionTimeoutManager | null = null;

    // Register lifecycle hooks.
    const beforeAgentStartHandler = async (event: any, ctx: any): Promise<{ prependContext?: string } | undefined> => {
      if (isInternalSessionContext(event, ctx)) {
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
      // api.on hooks can fire during plugin bootstrap before timeoutManager is constructed.
      if (timeoutManager) {
        timeoutManager.onAgentStart(resolveActiveUserSessionId(event, ctx));
      } else {
        writeHookTrace("hook.before_agent_start.skipped", {
          reason: "timeout_manager_uninitialized",
          hook_session_id: String(ctx?.sessionId || ""),
        });
      }

      const autoInjectEnabled = isAutoInjectEnabled(getMemoryConfig());

      if (!autoInjectEnabled) {
        // Explicitly disabled: skip all context injection for this instance.
        return { prependContext: event.prependContext };
      }

      return { prependContext: event.prependContext };
    };

    // --- Auto-injection via before_prompt_build ---
    //
    // !! CRITICAL — LATENCY AND TOKEN WARNING !!
    // before_prompt_build runs on EVERY SINGLE message the user sends.
    // Any code added here adds latency and token usage to every query.
    // Do NOT add anything here unless it is absolutely necessary and cannot
    // live in a session-start or lifecycle hook. This path is performance-critical.
    // Treat additions as a last resort.
    //
    // What lives here:
    //   1. Project docs injection (identity + TOOLS.md/AGENTS.md) — once per session,
    //      gated by a Set. Belongs in before_agent_start when OC adds support.
    //   2. Recall auto-injection — per-message, semantically relevant memories.
    const projectDocsInjectedSessions = new Set<string>();

    const beforePromptBuildHandler = async (event: any, ctx: any): Promise<{ prependContext?: string; prependSystemContext?: string; appendSystemContext?: string } | undefined> => {
      if (isInternalSessionContext(event, ctx)) return;

      // Inject project docs once per session on the first message.
      // - appendSystemContext: full TOOLS.md + AGENTS.md docs (appended after OC base prompt)
      // - prependSystemContext: short mandatory file-placement rules (prepended before OC base
      //   prompt so the model sees them at maximum priority)
      let appendSystemContext: string | undefined;
      let prependSystemContext: string | undefined;
      if (isSystemEnabled("projects")) {
        const sessionKeyDocs = String(event?.sessionId || ctx?.sessionId || ctx?.session?.id || "");
        writeHookTrace("hook.docs_gate_check", {
          session_id: sessionKeyDocs,
          in_set: projectDocsInjectedSessions.has(sessionKeyDocs),
          event_session_id: String(event?.sessionId || ""),
          ctx_session_id: String(ctx?.sessionId || ""),
        });
        if (sessionKeyDocs && !projectDocsInjectedSessions.has(sessionKeyDocs)) {
          projectDocsInjectedSessions.add(sessionKeyDocs);
          try {
            const projectDocs = facade.injectProjectContext(undefined);
            if (projectDocs) {
              appendSystemContext = projectDocs;
              writeHookTrace("hook.project_docs_injected", { session_id: sessionKeyDocs, len: projectDocs.length });
            }
          } catch (err: unknown) {
            console.warn(`[quaid] Project docs injection failed: ${(err as Error)?.message || String(err)}`);
          }
        }
        // Prepend mandatory file-placement rules on EVERY turn (not just first).
        // appendSystemContext (project docs) is gated once per session — expensive.
        // prependSystemContext (placement rules) is a cheap template — always inject.
        if (_QUAID_INSTANCE) {
          const miscPath = path.join(WORKSPACE, "projects", `misc--${_QUAID_INSTANCE}`);
          prependSystemContext = [
            `[Quaid — active knowledge layer | instance: ${_QUAID_INSTANCE}]`,
            `Quaid tracks files, projects, and knowledge across sessions. ALL files live inside tracked projects.`,
            ``,
            `[PROJECT CREATION — MANDATORY BEFORE ANY WORK BEGINS]`,
            `Before you write a single file, spawn a coding agent, run a build, or execute any multi-step task:`,
            `  STEP 1: Run quaid registry create-project <name> --source-roots <path>`,
            `  STEP 2: Then do the work inside that project.`,
            `DO NOT spawn a coding agent or subagent without completing Step 1 first.`,
            `DO NOT write any file without completing Step 1 first.`,
            `DO NOT create directories or PROJECT.md files manually — only the quaid CLI creates projects.`,
            `This applies to ALL work requests — even quick ones, even "just a script", even "just a test".`,
            ``,
            `[FILE PLACEMENT]`,
            `When the user says "temporary", "quick", "throwaway", or "somewhere temporary", use the misc project:`,
            `  Misc project path: ${miscPath}/`,
            `  The misc project directory already exists — write files there directly.`,
            `  If quaid commands say "project not found" for misc--${_QUAID_INSTANCE}, register it first:`,
            `    quaid registry create-project misc--${_QUAID_INSTANCE} --source-roots ${miscPath}/`,
            `  (If registration says "already exists", that is fine — proceed to write.)`,
            `For durable new work: run Step 1 above to create a named project first.`,
            `For work that belongs to an existing project: write there directly.`,
            ``,
            `Always tell the user which project received the file.`,
          ].join("\n");
          writeHookTrace("hook.file_placement_reminder_injected", { session_id: sessionKeyDocs });
        }
      }

      // Helper: carry any built docs context through all early returns.
      // The docs gate block above may have set prependSystemContext/appendSystemContext
      // and added the session to projectDocsInjectedSessions. Any early return after
      // that point MUST include those values — otherwise the session is marked as
      // "docs delivered" but the model never received them (e.g. rawPrompt < 5 during
      // a mass-reset fan-out triggers before_prompt_build before the user's message,
      // marks the session injected, and on the real message the docs are skipped).
      const withDocs = (base: { prependContext?: string }) => ({
        ...base,
        ...(prependSystemContext ? { prependSystemContext } : {}),
        ...(appendSystemContext ? { appendSystemContext } : {}),
      });

      const autoInjectEnabled = isAutoInjectEnabled(getMemoryConfig());
      if (!autoInjectEnabled) return withDocs({ prependContext: event.prependContext });

      try {
        let { query, source: querySource, rawPrompt } = selectAutoInjectQuery(
          event,
          lastUserMessageQuery,
        );
        const eventMessages: any[] = Array.isArray(event.messages) ? event.messages : [];

        writeHookTrace("hook.before_prompt_build.query_extracted", {
          query: query.slice(0, 80),
          source: querySource,
          msg_count: eventMessages.length,
          raw_prefix: rawPrompt.slice(0, 80),
        });

        if (query.length < 3) {
          writeHookTrace("hook.before_prompt_build.query_empty", {
            source: querySource,
            raw_prefix: rawPrompt.slice(0, 80),
            msg_count: eventMessages.length,
          });
          return withDocs({ prependContext: event.prependContext });
        }

        // Skip system/internal prompts, slash commands, and OC gateway error messages.
        // oc_prompt_json is the primary source (always current); this fallback handles
        // non-JSON-wrapped prompts where oc_prompt_json returned empty and event.messages
        // fell back to a stale slash command or startup message.
        const STARTUP_SKIP_RE = /^(A new session|Read HEARTBEAT|HEARTBEAT|You are being asked to|You are running as a subagent|You are a subagent|\/\w|Exec failed)/;
        if (STARTUP_SKIP_RE.test(query)) {
          // Try to recover from rawPrompt directly (handles "[Tue ...] <user msg>" format
          // where scrubQuery strips the leading timestamp bracket).
          const rawRecovered = scrubAutoInjectQuery(rawPrompt);
          const recoveredSource = "rawPrompt_recovered";
          if (rawRecovered.length >= 3 && !STARTUP_SKIP_RE.test(rawRecovered) &&
              !rawRecovered.startsWith("Extract memorable facts") &&
              !facade.isInternalMaintenancePrompt(rawRecovered)) {
            query = rawRecovered;
            querySource = recoveredSource;
            writeHookTrace("hook.before_prompt_build.staleness_recovered", { query: query.slice(0, 80), source: recoveredSource });
          } else {
            writeHookTrace("hook.before_prompt_build.startup_skip", { query: query.slice(0, 80), recovered_len: rawRecovered.length });
            return withDocs({ prependContext: event.prependContext });
          }
        }
        if (query.startsWith("Extract memorable facts and journal entries from this conversation:")) {
          return withDocs({ prependContext: event.prependContext });
        }
        // Skip janitor/reviewer internal prompts so maintenance flows never trigger auto-injection.
        if (facade.isInternalMaintenancePrompt(query)) {
          return withDocs({ prependContext: event.prependContext });
        }

        // Query quality gate — skip acknowledgments and short messages
        if (facade.isLowQualityQuery(query)) {
          return withDocs({ prependContext: event.prependContext });
        }

        // Auto-inject always bypasses the LLM router to keep latency low.
        // The router adds ~8s of LLM overhead which causes injection to arrive
        // after the agent has already responded. Direct vector+graph lookup is
        // sufficient for contextual injection.
        // Dynamic K: 2 * log2(nodeCount) — scales with graph size
        const autoInjectK = facade.computeDynamicK();
        const injectLimit = autoInjectK;
        const injectIntent: "general" = "general";
        // Use all-domain search for auto-inject: domain tagging may be incomplete
        // on fresh installs or for newly extracted facts. A strict { personal: true }
        // filter excludes untagged or differently-tagged facts. Retrieve all facts
        // and let semantic similarity + reranking surface the relevant ones.
        const injectDomain: DomainFilter = { all: true };

        // Re-entrancy guard: if before_prompt_build fires while we are already inside
        // recallMemories, this is an OC-internal LLM call (OC reuses the TUI session key
        // for callConfiguredLLM openresponses sessions). Skip to prevent the
        // recall→LLM→recall→... recursive loop.
        if (_beforePromptBuildInFlight) {
          writeHookTrace("hook.before_prompt_build.reentrant_skip", { query: query.slice(0, 80) });
          return withDocs({ prependContext: event.prependContext });
        }
        _beforePromptBuildInFlight = true;
        let allMemories: any[];
        let recallDiagnostics: Record<string, unknown> | null = null;
        try {
          let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
          const deadline = new Promise<[any[]]>(resolve => {
            deadlineTimer = setTimeout(() => {
              writeHookTrace("hook.before_prompt_build.deadline_hit", {});
              resolve([[]]);
            }, BEFORE_PROMPT_BUILD_DEADLINE_MS);
          });
          // planToolHint is intentionally omitted here: callFastRouter spawns OC
          // ephemeral sessions that each re-trigger before_prompt_build. Those
          // sessions fire after planToolHint completes (after _beforePromptBuildInFlight
          // is cleared), so the re-entrancy guard cannot block them. The result is
          // several concurrent recall calls stacking up and burning the deadline.
          // Recall-only injection (typically <22s) is within the budget.
          const recallStartMs = Date.now();
          writeHookTrace("hook.recall_start", { query: query.slice(0, 80), ts: recallStartMs });
          [allMemories] = await Promise.race([
            Promise.all([
              recallMemories({
                query,
                limit: injectLimit,
                expandGraph: false,
                // OC already injects project docs separately in before_prompt_build.
                // Keep auto-inject memory recall focused on memory stores so a slow
                // project search cannot consume the full hook budget and starve
                // otherwise-fast personal memory hits like Baxter.
                datastores: ["vector_basic"],
                routeStores: false,
                intent: injectIntent,
                domain: injectDomain,
                failOpen: true,
                waitForExtraction: false,
                fast: true,
                sourceTag: "auto_inject"
              }),
            ]),
            deadline,
          ]);
          // Cancel the deadline timer if recall completed first; prevents a ghost
          // deadline_hit trace from firing 10s after recall already succeeded.
          if (deadlineTimer !== undefined) clearTimeout(deadlineTimer);
          recallDiagnostics = summarizeRecallDiagnostics((allMemories as any)?.__quaidRecallDiagnostics || null);
          writeHookTrace("hook.recall_done", {
            count: allMemories.length,
            elapsed_ms: Date.now() - recallStartMs,
            diagnostics: recallDiagnostics,
            top_results: summarizeRecallResults(allMemories),
          });
        } finally {
          _beforePromptBuildInFlight = false;
        }

        if (!Array.isArray(allMemories) || allMemories.length === 0) {
          writeHookTrace("hook.before_prompt_build.recall_empty", {
            query: query.slice(0, 80),
            source: querySource,
            msg_count: eventMessages.length,
            diagnostics: recallDiagnostics,
          });
        }

        const injection = facade.prepareAutoInjectionContext({
          allMemories,
          eventMessages: event.messages || [],
          context: ctx,
          existingPrependContext: undefined,
          injectLimit,
          maxInjectionIdsPerSession: MAX_INJECTION_IDS_PER_SESSION,
        });
        if (!injection) {
          writeHookTrace("hook.before_prompt_build.injection_skipped", {
            query: query.slice(0, 80),
            source: querySource,
            recall_count: Array.isArray(allMemories) ? allMemories.length : 0,
            msg_count: eventMessages.length,
            diagnostics: recallDiagnostics,
            top_results: summarizeRecallResults(allMemories),
          });
          return withDocs({ prependContext: event.prependContext });
        }
        const { toInject, prependContext: memoriesBlock } = injection;
        writeHookTrace("hook.before_prompt_build.injection_ready", {
          query: query.slice(0, 80),
          source: querySource,
          recall_count: Array.isArray(allMemories) ? allMemories.length : 0,
          inject_count: toInject.length,
          inject_limit: injectLimit,
          diagnostics: recallDiagnostics,
          top_results: summarizeRecallResults(toInject),
        });
        // Inject memories into system context rather than the human turn.
        // System-level injection (appendSystemContext) is treated as authoritative by the
        // model. Human-turn injection (prependContext) is treated as user-provided context
        // and is frequently ignored for memory queries — confirmed by CC (system inject)
        // passing where OC (prependContext inject) fails on identical memory content.
        appendSystemContext = appendSystemContext
          ? `${appendSystemContext}\n\n${memoriesBlock}`
          : memoriesBlock;

        console.log(`[quaid] Auto-injected ${toInject.length} memories for "${query.slice(0, 50)}..."`);

        // Best-effort user notification for auto-injected recalls.
        try {
          if (facade.shouldNotifyFeature("retrieval", "summary")) {
            const payload = facade.buildRecallNotificationPayload(toInject, query, "auto_inject");
            const dataFile = path.join(QUAID_TMP_DIR, `auto-inject-recall-${Date.now()}.json`);
            fs.writeFileSync(dataFile, JSON.stringify(payload), { mode: 0o600 });
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

      } catch (error: unknown) {
        console.error("[quaid] Auto-injection error:", error);
      }

      return {
        prependContext: event.prependContext || undefined,
        ...(prependSystemContext ? { prependSystemContext } : {}),
        ...(appendSystemContext ? { appendSystemContext } : {}),
      };
    };

    // Register lifecycle hooks via registerHook (api.on is for event bus signals).
    console.log("[quaid] Registering before_agent_start hook for memory injection");
    onChecked("before_agent_start", beforeAgentStartHandler, {
      name: "memory-injection",
      priority: 10
    });

    // before_prompt_build fires per-message with the actual user prompt,
    // unlike before_agent_start which fires once per subagent session
    // (often without the prompt). This is where recall-based injection lives.
    onChecked("before_prompt_build", beforePromptBuildHandler, {
      name: "memory-injection-prompt-build",
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
    const transcriptLifecycleCursor = new Map<string, number>();
    let lastTranscriptSessionHint: { sessionId: string; seenAtMs: number } | null = null;
    let currentInteractiveSession: ActiveInteractiveSession | null = null;
    // Stores the most recent non-slash user message text captured from message_received,
    // which fires before before_prompt_build with the actual user query. Used as query
    // fallback when event.prompt is the OC metadata wrapper (empty after scrubbing).
    let lastUserMessageQuery: { text: string; seenAtMs: number } | null = null;
    const runtimeEvents = (api as any)?.runtime?.events;
    if (runtimeEvents && typeof runtimeEvents.onSessionTranscriptUpdate === "function") {
      runtimeEvents.onSessionTranscriptUpdate((update: any) => {
        try {
          const sessionFile = String(update?.sessionFile || "").trim();
          if (!sessionFile || !fs.existsSync(sessionFile)) return;
          // Track transcript path for daemon signal writing
          const trackSessionId = String(update?.sessionId || "").trim();
          if (trackSessionId) sessionTranscriptPaths.set(trackSessionId, sessionFile);
          const messages = readSessionMessagesFile(sessionFile);
          if (!Array.isArray(messages) || messages.length === 0) return;
          const sessionId =
            facade.parseSessionIdFromTranscriptPath(sessionFile) ||
            facade.resolveLifecycleHookSessionId(
              {
                sessionId: String(update?.sessionId || "").trim(),
                sessionKey: String(update?.sessionKey || update?.targetSessionKey || "").trim(),
              },
              undefined,
              [],
            ) ||
            String(update?.sessionId || "").trim();
          // Also register the path-derived sessionId so writeDaemonSignal can find
          // the transcript even when update.sessionId is missing (e.g. openresponses sessions).
          if (sessionId && sessionId !== trackSessionId) {
            sessionTranscriptPaths.set(sessionId, sessionFile);
          }
          const sessionKey = String(
            update?.sessionKey
            || update?.targetSessionKey
            || resolveSessionKeyForSessionId(sessionId)
            || ""
          ).trim();
          // Use sessionId from the transcript file path directly — it is authoritative
          // for which session is being updated. The previous rerouting to
          // currentInteractiveSession (mtime-based "most active" heuristic) was
          // unreliable: when a new session starts after /new or TUI restart, the
          // previous session's file has a higher mtime and the heuristic picks the
          // wrong session, causing onAgentEnd to fire with the wrong session ID.
          const timeoutActivitySessionId = sessionId;

          // Track resolved sessionId → transcript file for daemon signals
          if (sessionId) sessionTranscriptPaths.set(sessionId, sessionFile);

          // Seed an initial extraction cursor so the daemon's rolling poller can
          // discover this session before any compaction fires.  before_agent_start
          // only fires for internal per-turn agent sessions in OC — not for the
          // persistent TUI transcript session — so we seed here instead, where
          // both the session ID and the transcript file path are authoritative.
          if (
            sessionId
            && isSystemEnabled("memory")
            && !isInternalSessionContext({ sessionId, sessionKey }, { sessionId, sessionKey })
          ) {
            // Cursor must go in the instance silo, not WORKSPACE root.
            // Mirror the Python daemon: instance_root = QUAID_HOME / QUAID_INSTANCE.
            const instanceRoot = _QUAID_INSTANCE
              ? path.join(WORKSPACE, _QUAID_INSTANCE)
              : WORKSPACE;
            const cursorDir = path.join(instanceRoot, "data", "session-cursors");
            const cursorPath = path.join(cursorDir, `${sessionId}.json`);
            if (!fs.existsSync(cursorPath)) {
              try {
                fs.mkdirSync(cursorDir, { recursive: true });
                const nowIso = new Date().toISOString().replace(/\.\d+Z$/, "Z");
                fs.writeFileSync(cursorPath, JSON.stringify({
                  session_id: sessionId,
                  line_offset: 0,
                  transcript_path: sessionFile,
                  updated_at: nowIso,
                }, null, 2), "utf8");
                console.log(`[quaid][cursor] seeded rolling cursor for transcript session ${sessionId}`);
              } catch (e) {
                console.warn(`[quaid][cursor] cursor seed error: ${e}`);
              }
            }
          }

          // Keep timeout extraction on the real-time path by treating transcript updates
          // as activity boundaries; stale-sweep recovery should stay a fallback only.
          if (
            timeoutActivitySessionId
            && timeoutManager
            && !isInternalSessionContext(
              { sessionId: timeoutActivitySessionId, sessionKey },
              { sessionId: timeoutActivitySessionId, sessionKey },
            )
          ) {
            timeoutManager.onAgentEnd(messages, timeoutActivitySessionId, { source: "transcript_update" });
          } else if (sessionId) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: timeoutManager ? "internal_session" : "timeout_manager_uninitialized",
              parsed_session_id: sessionId,
              timeout_activity_session_id: timeoutActivitySessionId,
              parsed_session_key: sessionKey,
              session_file: sessionFile,
            });
          }
          const hasExtractionPrompt = messages.some((m: any) =>
            /^Extract memorable facts and journal entries from this conversation chunk:/i.test(
              String(facade.getMessageText(m) || "").trim()
            )
          );
          if (hasExtractionPrompt) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: "internal_extraction_transcript",
              session_file: sessionFile,
              message_count: messages.length,
            });
            return;
          }
          writeHookTrace("hook.transcript_update.received", {
            update_session_id: String(update?.sessionId || ""),
            session_file: sessionFile,
            message_count: messages.length,
          });
          const detail = facade.detectLifecycleSignal(messages);
          const conversationMessages = facade.filterConversationMessages(messages);
          const bootstrapOnlyConversation = facade.isResetBootstrapOnlyConversation(conversationMessages);
          const hasLifecycleUserCommand = facade.hasExplicitLifecycleUserCommand(conversationMessages);
          if (!detail) {
            const tail = messages.slice(-5).map((m: any) => ({
              role: String(m?.role || ""),
              text: String(facade.getMessageText(m) || "").slice(0, 200),
            }));
            writeHookTrace("hook.transcript_update.no_signal", {
              update_session_id: String(update?.sessionId || ""),
              session_file: sessionFile,
              message_count: messages.length,
              tail,
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
            tail: messages.slice(-5).map((m: any) => ({
              role: String(m?.role || ""),
              text: String(facade.getMessageText(m) || "").slice(0, 200),
            })),
          });
          if (!sessionId) {
            console.log(`[quaid][signal] transcript_update missing session id file=${sessionFile}`);
            return;
          }
          const detectedMessageIndex = Number.isFinite(detail.messageIndex)
            ? Number(detail.messageIndex)
            : (messages.length - 1);
          const replayCursorKey = `${sessionId}:${detail.label}:${detail.signature}`;
          const priorMessageIndex = transcriptLifecycleCursor.get(replayCursorKey);
          if (priorMessageIndex != null && detectedMessageIndex <= priorMessageIndex) {
            writeHookTrace("hook.transcript_update.skipped", {
              reason: "transcript_signal_replay",
              detected_label: String(detail.label || ""),
              detected_signature: String(detail.signature || ""),
              detected_message_index: detectedMessageIndex,
              prior_message_index: priorMessageIndex,
              session_file: sessionFile,
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
          if (
            conversationMessages.length > 0
            && !bootstrapOnlyConversation
            && !hasLifecycleUserCommand
          ) {
            lastTranscriptSessionHint = { sessionId, seenAtMs: Date.now() };
          }
          const daemonType = detail.label.toLowerCase().includes("reset") ? "reset" as const : "compaction" as const;
          writeDaemonSignal(sessionId, daemonType, { source: "transcript_update" });
          console.log(`[quaid][signal] daemon signal ${daemonType} session=${sessionId} source=transcript_update`);
        } catch (err: unknown) {
          console.error("[quaid] transcript_update fallback failed:", err);
        }
      });
      console.log("[quaid] Registered runtime.events.onSessionTranscriptUpdate lifecycle fallback");
    }

    const sessionIndexMessageCounts = new Map<string, number>();
    // Tracks the transcript byte-size at the time each prior session was last
    // fanout-signaled. A session is only re-signaled when its transcript has
    // grown past this size, meaning there is actually new content to extract.
    const sessionLastFanoutSizeMap = new Map<string, number>();
    const seenSessionIndexCommandKeys = new Set<string>();
    // Per-key last-seen sessionId. Transition detected when a key's value changes.
    // This replaces the single-"active"-session model with per-key tracking so that
    // multiple concurrent TUI/telegram sessions are all watched independently, and
    // /new in any session writes a reset for that specific session (not a tracked
    // "current" that may point to the wrong one).
    const sessionKeyLastSeen = new Map<string, string>();
    // Tracks the last time the watcher observed new messages in a session.
    // Used by the before_agent_start fallback to identify which session was
    // just active when OC does a visual-only /new (no sessions.json update).
    const sessionLastActivityMs = new Map<string, number>();
    const startSessionIndexWatcher = () => {
      if (sessionIndexWatcherStarted) {
        return;
      }
      sessionIndexWatcherStarted = true;
      // Hard floor: sessions whose transcripts were last modified before Quaid
      // was installed should never be signalled — they predate the install and
      // we have no cursors for them. Falls back to 0 (no floor) if the file
      // is missing, which is safe: the watcherStartMs check below still applies.
      const installedAtMs = readInstalledAtMs();
      // Soft floor: sessions not touched since this gateway started. Prevents
      // re-signalling sessions that were idle before the process restarted.
      const watcherStartMs = Date.now();
      // True once the first tick has completed and sessionKeyLastSeen is populated
      // with the initial snapshot from sessions.json. The new-key signal path is
      // suppressed on tick 1 to avoid treating ALL existing keys as new arrivals
      // (sessionKeyLastSeen starts empty so every key has prevSessionId=undefined).
      let initialSnapshotDone = false;
      // Tracks sessions where a key transition was detected but the .reset.* backup
      // may not have been created yet. Value is the time the watch was armed (ms).
      // Checked each tick; times out after 60s. Empty most of the time.
      const pendingOrphanChecks = new Map<string, number>();
      const ORPHAN_CHECK_DEADLINE_MS = 60_000;
      // Stale-sweep fallback: runs recoverStaleBuffers() every 30s to catch idle
      // sessions that were not caught by onSessionTranscriptUpdate (e.g. when the
      // event does not fire for a given OC version/configuration).
      const STALE_SWEEP_INTERVAL_MS = 30_000;
      let lastStaleRecoverMs = 0;
      const tickSessionIndex = () => {
        try {
          const data = readSessionsIndex();

          // Build the list of all recognized interactive key entries from sessions.json.
          const recognizedEntries: Array<{
            key: string;
            sessionId: string;
            sessionFile: string;
            updatedAt: number;
          }> = [];
          for (const [key, row] of Object.entries(data || {})) {
            if (
              !row
              || typeof row !== "object"
              || typeof (row as any)?.sessionId !== "string"
              || !key.startsWith("agent:")
            ) {
              continue;
            }
            const sessionId = String((row as any).sessionId || "").trim();
            if (!sessionId) continue;
            // Extract agentId from "agent:<agentId>:<channel>" and register for signal routing.
            // Extract raw agent label from "agent:<label>:<channel>" and map for signal routing.
            // getInstanceId(label) builds the full instance ID (e.g. "openclaw-main").
            const keyParts = key.split(":");
            const agentLabel = keyParts.length >= 3 ? (keyParts[1].trim() || "main") : "main";
            sessionIdToAgentId.set(sessionId, agentLabel);
            recognizedEntries.push({
              key,
              sessionId,
              sessionFile: getOpenClawSessionFile(sessionId),
              updatedAt: Number((row as any).updatedAt || 0),
            });
          }

          // For each recognized key: detect transitions (key moved to new sessionId)
          // and watch the current session's transcript for slash commands.
          for (const entry of recognizedEntries) {
            const { key, sessionId, sessionFile, updatedAt } = entry;
            const prevSessionId = sessionKeyLastSeen.get(key);

            if (prevSessionId && prevSessionId !== sessionId) {
              // This key just transitioned to a new session — the old session ended.
              writeHookTrace("session_index.key_transition", {
                key,
                from_session_id: prevSessionId,
                to_session_id: sessionId,
              });
              const prevFile = getOpenClawSessionFile(prevSessionId);
              preserveSessionTranscript(prevSessionId, prevFile, "session-key-transition");
              if (
                !isInternalSessionContext({ sessionKey: key }, { sessionId: prevSessionId })
                && isSystemEnabled("memory")
              ) {
                // Apply the same guards as new_key: skip pre-install / wiped transcripts.
                const mtimeFloorMs = installedAtMs > 0 ? installedAtMs : watcherStartMs;
                let prevSize = -1;
                let prevMtime = 0;
                try {
                  const prevSt = fs.statSync(getOpenClawSessionFile(prevSessionId));
                  prevSize = prevSt.size;
                  prevMtime = prevSt.mtimeMs;
                } catch {}
                if (prevSize <= 0) {
                  writeHookTrace("session_index.key_transition_skip", { reason: "empty", session_id: prevSessionId, key });
                } else if (prevMtime <= mtimeFloorMs) {
                  writeHookTrace("session_index.key_transition_skip", { reason: "mtime", session_id: prevSessionId, key, prev_mtime: prevMtime, installed_at_ms: installedAtMs });
                } else if (facade.shouldProcessLifecycleSignal(prevSessionId, {
                  label: "ResetSignal",
                  source: "session_index",
                  signature: `session_index:key_transition:${key}`,
                })) {
                  facade.markLifecycleSignalFromHook(prevSessionId, "ResetSignal");
                  writeDaemonSignal(prevSessionId, "reset", {
                    source: "session_index_key_transition",
                    session_key: key,
                    next_session_id: sessionId,
                  });
                  writeHookTrace("session_index.signal_queued", {
                    signal: "reset",
                    source: "key-transition",
                    session_id: prevSessionId,
                    session_key: key,
                  });
                }
              }
              // Arm a targeted orphan check: OC may not have created the .reset.*
              // backup yet. The pending check runs each tick and emits the signal
              // once the backup appears, or gives up after 60s.
              if (isSystemEnabled("memory") && !isInternalSessionContext({ sessionKey: key }, { sessionId: prevSessionId })) {
                pendingOrphanChecks.set(prevSessionId, Date.now());
              }
              // Clean up message count for the ended session.
              sessionIndexMessageCounts.delete(prevSessionId);
            } else if (!prevSessionId && initialSnapshotDone && isSystemEnabled("memory") && !isInternalSessionContext({ sessionKey: key }, { sessionId })) {
              // Brand new key in sessions.json — OC TUI /new adds a NEW key rather
              // than updating an existing key's session ID. Signal any sessions that
              // were recently active so their content is extracted before the user
              // moves on. Gated on initialSnapshotDone to avoid treating all
              // existing keys as new on the first tick (when sessionKeyLastSeen is
              // empty, every key has prevSessionId=undefined).
              writeHookTrace("session_index.new_key_detected", { key, session_id: sessionId, watcher_start_ms: watcherStartMs });
              // Only consider sessions currently present in sessions.json. Sessions
              // in sessionKeyLastSeen but absent from sessions.json are stale — they
              // belong to earlier test runs or gateway restarts within this process
              // lifetime. Without this guard the mtime check alone can't exclude them
              // (their transcripts were modified during this gateway lifetime).
              const currentSids = new Set(recognizedEntries.map((e) => e.sessionId));
              for (const [priorKey, priorSid] of sessionKeyLastSeen.entries()) {
                if (!currentSids.has(priorSid)) {
                  writeHookTrace("session_index.new_key_skip", { reason: "not_in_current_sessions", prior_sid: priorSid, prior_key: priorKey });
                  continue;
                }
                if (/^agent:[^:]+:hook:/.test(priorKey)) continue;
                if (priorSid === sessionId) continue;
                if (isInternalSessionContext({ sessionKey: priorKey }, { sessionId: priorSid })) continue;
                // Only signal sessions whose transcript was modified after Quaid
                // was installed. installedAtMs is the hard floor: pre-install
                // sessions must never be signalled (we have no cursors for them).
                // watcherStartMs is the fallback when installed-at.json is missing.
                const mtimeFloorMs = installedAtMs > 0 ? installedAtMs : watcherStartMs;
                let priorSize = -1;
                let priorMtime = 0;
                try {
                  const st = fs.statSync(getOpenClawSessionFile(priorSid));
                  priorSize = st.size;
                  priorMtime = st.mtimeMs;
                } catch {}
                if (priorSize <= 0) {
                  writeHookTrace("session_index.new_key_skip", { reason: "empty", prior_sid: priorSid, prior_key: priorKey, prior_size: priorSize });
                  continue;
                }
                if (priorMtime <= mtimeFloorMs) {
                  writeHookTrace("session_index.new_key_skip", { reason: "mtime", prior_sid: priorSid, prior_key: priorKey, prior_mtime: priorMtime, installed_at_ms: installedAtMs, watcher_start_ms: watcherStartMs });
                  continue;
                }
                // Skip sessions older than 2 hours — they were active before the current
                // work session and would already have been extracted by a prior OC process.
                // Without this cap, a restart re-signals all sessions since Quaid install,
                // flooding the daemon queue and delaying the active session by 10+ minutes.
                const FANOUT_MAX_AGE_MS = 2 * 60 * 60 * 1000; // 2 hours
                if (priorMtime < Date.now() - FANOUT_MAX_AGE_MS) {
                  writeHookTrace("session_index.new_key_skip", { reason: "too_old", prior_sid: priorSid, prior_key: priorKey, prior_mtime: priorMtime, age_ms: Date.now() - priorMtime });
                  continue;
                }
                // Content-based dedup: only signal if the transcript has grown since
                // the last time we signaled this session. This replaces the time-based
                // suppress window — no data loss risk from rapid /new usage.
                const lastFanoutSize = sessionLastFanoutSizeMap.get(priorSid) ?? -1;
                if (priorSize <= lastFanoutSize) {
                  writeHookTrace("session_index.new_key_skip", { reason: "no_new_content", prior_sid: priorSid, prior_key: priorKey, prior_size: priorSize, last_fanout_size: lastFanoutSize });
                  continue;
                }
                if (!facade.shouldProcessLifecycleSignal(priorSid, {
                  label: "ResetSignal",
                  source: "session_index",
                  signature: `session_index:new_key:${key}`,
                })) continue;
                facade.markLifecycleSignalFromHook(priorSid, "ResetSignal");
                sessionLastFanoutSizeMap.set(priorSid, priorSize);
                writeDaemonSignal(priorSid, "reset", {
                  source: "session_index_new_key",
                  new_key: key,
                  new_session_id: sessionId,
                });
                writeHookTrace("session_index.signal_queued", {
                  signal: "reset",
                  source: "new-key",
                  session_id: priorSid,
                  session_key: priorKey,
                  new_key: key,
                });
              }
            }

            sessionKeyLastSeen.set(key, sessionId);
            sessionTranscriptPaths.set(sessionId, sessionFile);

            // Watch this session's transcript for slash commands.
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
              // The before_prompt_build hook prepends injected context before the
              // user's actual message, and OC gateway prepends a [Day Date Time TZ]
              // timestamp to every user message. To detect slash commands robustly:
              // 1. Take the LAST non-empty line (user input is always last after injections).
              // 2. Strip the OC timestamp prefix from that line.
              const rawLines = rawText.split("\n").filter((l: string) => l.trim());
              const lastLine = (rawLines[rawLines.length - 1] || "").trim();
              const stripped = lastLine.replace(/^\[.*?\]\s*/, "").trim();
              const text = (stripped || rawText).toLowerCase();
              const commandKey = `${sessionId}:${priorCount + i}:${text}`;
              if (seenSessionIndexCommandKeys.has(commandKey)) {
                continue;
              }
              let daemonType: "reset" | "compaction" | null = null;
              let lifecycleSignal: "ResetSignal" | "CompactionSignal" | null = null;
              let commandName: "new" | "reset" | "compact" | null = null;
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
                text: text.slice(0, 120),
              });
              if (isInternalSessionContext({ sessionKey: key }, { sessionId }) || !isSystemEnabled("memory")) {
                continue;
              }
              preserveSessionTranscript(sessionId, sessionFile, `command-${commandName}`);
              if (!facade.shouldProcessLifecycleSignal(sessionId, {
                label: lifecycleSignal,
                source: "session_index",
                signature: `session_index:command_${commandName}`,
              })) {
                writeHookTrace("session_index.signal_suppressed", {
                  session_id: sessionId,
                  session_key: key,
                  command: commandName,
                  reason: "duplicate",
                });
                continue;
              }
              facade.markLifecycleSignalFromHook(sessionId, lifecycleSignal);
              writeDaemonSignal(sessionId, daemonType, {
                source: `session_index_command_${commandName}`,
                command: commandName,
                session_key: key,
              });
              writeHookTrace("session_index.signal_queued", {
                signal: daemonType,
                source: `command-${commandName}`,
                session_id: sessionId,
                session_key: key,
              });
            }
          }

          // Keep currentInteractiveSession updated for timeout tracking — pick the
          // most recently active recognized session (same logic as before).
          const active = pickActiveInteractiveSession(data);
          if (active) {
            currentInteractiveSession = active;
          }

          // Pending orphan checks: for each session where a key transition was
          // detected, look for the .reset.* backup. OC creates it shortly after
          // the gateway restarts, so this usually resolves on the first or second
          // tick. Gives up after 60s. O(pending sessions) not O(all files).
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
                if (!backup) continue; // not created yet — retry next tick
                let origSize = -1;
                try { origSize = fs.statSync(getOpenClawSessionFile(sid)).size; } catch {}
                if (origSize > 0) {
                  // Original still has content — key transition signal already handled it
                  pendingOrphanChecks.delete(sid);
                  continue;
                }
                if (!facade.shouldProcessLifecycleSignal(sid, {
                  label: "ResetSignal",
                  source: "watcher_scan",
                  signature: "hook:ResetSignal",
                })) {
                  pendingOrphanChecks.delete(sid); // already handled by key-transition signal
                  continue;
                }
                pendingOrphanChecks.delete(sid);
                // Skip orphan reset if daemon already holds the processing lock for this session.
                // Writing a signal while the lock is held causes unbounded signal pile-up.
                const _orphanLockPath = _QUAID_INSTANCE
                  ? path.join(WORKSPACE, _QUAID_INSTANCE, "data", "session-processing", `${sid}.lock`)
                  : path.join(WORKSPACE, "data", "session-processing", `${sid}.lock`);
                if (fs.existsSync(_orphanLockPath)) {
                  writeHookTrace("session_index.orphan_reset_skipped_locked", { session_id: sid });
                  console.log(`[quaid][signal] orphan reset skipped — session=${sid} already locked`);
                  continue;
                }
                facade.markLifecycleSignalFromHook(sid, "ResetSignal");
                writeDaemonSignal(sid, "reset", { source: "orphan_reset_check" });
                writeHookTrace("session_index.orphan_reset_detected", { session_id: sid });
                console.log(`[quaid][signal] orphan reset detected session=${sid}`);
              } catch {}
            }
          }
        } catch (err: unknown) {
          writeHookTrace("session_index.error", {
            error: String((err as Error)?.message || err),
          });
        }
        // Mark initial snapshot complete after the first tick so subsequent ticks
        // can distinguish genuinely new keys from the initial population.
        initialSnapshotDone = true;
        // Stale-sweep fallback: periodically call recoverStaleBuffers() so idle
        // sessions are caught even when onSessionTranscriptUpdate doesn't fire.
        if (timeoutManager && Date.now() - lastStaleRecoverMs >= STALE_SWEEP_INTERVAL_MS) {
          lastStaleRecoverMs = Date.now();
          void timeoutManager.recoverStaleBuffers();
        }
      };
      void tickSessionIndex();
      sessionIndexWatcherTimer = setInterval(tickSessionIndex, SESSION_INDEX_POLL_MS);
      writeHookTrace("session_index.watcher_started", {
        poll_ms: SESSION_INDEX_POLL_MS,
        sessions_path: getOpenClawSessionsPath(),
      });
      console.log(`[quaid] session index watcher started pollMs=${SESSION_INDEX_POLL_MS}`);
    };

    const resolveActiveUserSessionId = (event: any, ctx: any, messages: any[] = []): string => {
      const direct = facade.resolveLifecycleHookSessionId(event, ctx, messages);
      if (direct && !isInternalSessionContext(event, { ...(ctx || {}), sessionId: direct })) {
        return direct;
      }
      if (currentInteractiveSession?.sessionId) {
        return currentInteractiveSession.sessionId;
      }
      const hint = lastTranscriptSessionHint;
      if (hint?.sessionId) {
        const ageMs = Date.now() - Number(hint.seenAtMs || 0);
        if (ageMs >= 0 && ageMs <= (5 * 60_000)) {
          return hint.sessionId;
        }
      }
      return direct;
    };

    // Direct slash-command capture: command:new/reset/compact can arrive as message events
    // before transcript files settle; bind extraction to the active user session.
    const handleSlashLifecycleFromMessage = async (event: any, ctx: any, sourceEvent: "message:received" | "message:preprocessed") => {
      try {
        const rawText = String(
          facade.getMessageText(event?.message || event) ||
          event?.text ||
          event?.content ||
          ""
        ).trim();
        if (!rawText) return;
        // Strip OC gateway timestamp prefix [Day Date Time TZ] if present.
        // OC prepends "[Sat 2026-03-14 01:20 GMT+8] " to every user message
        // before calling hooks; without stripping, "/compact" → "[...] /compact"
        // which doesn't match the bare slash-command patterns.
        const text = rawText.replace(/^\[.*?\]\s*/, "").trim() || rawText;
        const normalized = text.toLowerCase();
        let commandAction: "new" | "reset" | "compact" | null = null;
        let lifecycleSignal: "ResetSignal" | "CompactionSignal" | null = null;
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
          hook_session_id: sessionId || "",
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled("memory")) {
          return;
        }
        const signature = `msg:${sourceEvent}:command_${commandAction}`;
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: lifecycleSignal,
          source: "hook",
          signature,
        })) {
          writeHookTrace("hook.message.signal_suppressed", {
            source_event: sourceEvent,
            command: commandAction,
            hook_session_id: sessionId,
            reason: "duplicate",
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, lifecycleSignal);
        const daemonSigType = lifecycleSignal.toLowerCase().includes("reset") ? "reset" as const : "compaction" as const;
        writeDaemonSignal(sessionId, daemonSigType, {
          source: sourceEvent,
          command: commandAction,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
        });
        console.log(`[quaid][signal] daemon signal ${daemonSigType} session=${sessionId} source=${sourceEvent} command=${commandAction}`);
        writeHookTrace("hook.message.signal_queued", {
          source_event: sourceEvent,
          command: commandAction,
          hook_session_id: sessionId,
        });
      } catch (err: unknown) {
        if (isFailHardEnabled()) throw err;
        console.error(`[quaid] ${sourceEvent} command detector failed:`, err);
        writeHookTrace("hook.message.error", {
          source_event: sourceEvent,
          error: String((err as Error)?.message || err),
        });
      }
    };
    onChecked("message_received", async (event: any, ctx: any) => {
      // Capture non-slash user messages for use as auto-inject query in before_prompt_build.
      // before_prompt_build fires after message_received but before the user text lands in
      // event.prompt or event.messages (OC appends those a few ms later via transcript_update).
      try {
        const rawText = String(
          facade.getMessageText(event?.message || event) ||
          event?.text ||
          event?.content ||
          ""
        ).replace(/^\[.*?\]\s*/, "").trim();
        if (rawText.length >= 3 && !rawText.startsWith("/")) {
          lastUserMessageQuery = { text: rawText, seenAtMs: Date.now() };
        }
      } catch { /* ignore */ }
      await handleSlashLifecycleFromMessage(event, ctx, "message:received");
    }, {
      name: "message-received-command-memory-extraction",
      priority: 10,
    });

    // Primary lifecycle command path on supported OpenClaw builds.
    // Keep this explicit hook path to avoid transcript/session drift.
    const resolveLifecycleCommandTargetSessionId = (
      action: "new" | "reset" | "compact",
      event: any,
      ctx: any,
    ): string => {
      // For /new and /reset the hook payload can include both the old session
      // (messages to extract) and the new session (empty/bootstrap). Prefer the
      // previous session entry when present so extraction targets real content.
      if (action === "new" || action === "reset") {
        const previousSessionId = String(
          // OC stores session data under event.context (nested), not top-level.
          // Read both paths: context.previousSessionEntry (preferred), context.sessionEntry
          // (fallback), context.sessionId (explicit field added by our OC patch), and
          // legacy top-level fields for older OC versions.
          event?.context?.previousSessionEntry?.sessionId
          || event?.context?.sessionEntry?.sessionId
          || event?.context?.sessionId
          || event?.previousSessionEntry?.sessionId
          || event?.previousSessionId
          || "",
        ).trim();
        if (previousSessionId) {
          return previousSessionId;
        }
        const hint = lastTranscriptSessionHint;
        if (hint?.sessionId) {
          const ageMs = Date.now() - Number(hint.seenAtMs || 0);
          if (ageMs >= 0 && ageMs <= (5 * 60_000)) {
            return hint.sessionId;
          }
        }
      }
      return facade.resolveLifecycleHookSessionId(event, ctx);
    };

    const handleLifecycleCommandHook = async (
      action: "new" | "reset",
      event: any,
      ctx: any
    ) => {
      try {
        const sessionId = resolveLifecycleCommandTargetSessionId(action, event, ctx);
        writeHookTrace("hook.command.received", {
          action,
          hook_session_id: sessionId || "",
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
          previous_session_entry_id: String(event?.previousSessionEntry?.sessionId || ""),
          previous_session_id: String(event?.previousSessionId || ""),
          transcript_hint_session_id: String(lastTranscriptSessionHint?.sessionId || ""),
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled("memory")) {
          return;
        }
        const signature = `hook:command_${action}`;
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "ResetSignal",
          source: "hook",
          signature,
        })) {
          writeHookTrace("hook.command.signal_suppressed", {
            action,
            hook_session_id: sessionId,
            reason: "duplicate",
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        writeDaemonSignal(sessionId, "reset", {
          source: `command:${action}`,
          command: action,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
        });
        console.log(`[quaid][signal] daemon signal reset session=${sessionId} source=command:${action}`);
        writeHookTrace("hook.command.signal_queued", {
          action,
          hook_session_id: sessionId,
        });
      } catch (err: unknown) {
        if (isFailHardEnabled()) throw err;
        console.error(`[quaid] command:${action} hook failed:`, err);
        writeHookTrace("hook.command.error", {
          action,
          error: String((err as Error)?.message || err),
        });
      }
    };
    registerInternalHookChecked("command", async (event: any, ctx: any) => {
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
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
        });
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled("memory")) {
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "CompactionSignal",
          source: "hook",
          signature: "hook:command_compact",
        })) {
          writeHookTrace("hook.command.signal_suppressed", {
            action,
            hook_session_id: sessionId,
            reason: "duplicate",
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "CompactionSignal");
        writeDaemonSignal(sessionId, "compaction", {
          source: "command:compact",
          command: action,
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
        });
        console.log(`[quaid][signal] daemon signal compaction session=${sessionId} source=command:${action}`);
        writeHookTrace("hook.command.signal_queued", {
          action,
          hook_session_id: sessionId,
        });
      } catch (err: unknown) {
        if (isFailHardEnabled()) throw err;
        console.error("[quaid] command:compact hook failed:", err);
        writeHookTrace("hook.command.error", {
          action,
          error: String((err as Error)?.message || err),
        });
      }
    }, {
      name: "command-memory-extraction",
      priority: 10,
    });
    registerInternalHookChecked("command:new", async (event: any, ctx: any) => {
      await handleLifecycleCommandHook("new", event, ctx);
    }, {
      name: "command-new-memory-extraction",
      priority: 10,
    });
    registerInternalHookChecked("command:reset", async (event: any, ctx: any) => {
      await handleLifecycleCommandHook("reset", event, ctx);
    }, {
      name: "command-reset-memory-extraction",
      priority: 10,
    });
    registerInternalHookChecked("session", async (event: any, ctx: any) => {
      try {
        const action = String(event?.action || "").trim().toLowerCase();
        if (action !== "compact:before") {
          return;
        }
        const sessionId = facade.resolveLifecycleHookSessionId(event, ctx);
        if (!sessionId || isInternalSessionContext(event, ctx) || !isSystemEnabled("memory")) {
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "CompactionSignal",
          source: "hook",
          signature: "hook:session_action_compact_before",
        })) {
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "CompactionSignal");
        writeDaemonSignal(sessionId, "compaction", {
          source: "session:compact:before",
          hook_session_id: sessionId,
          hook_session_key: String(event?.sessionKey || ctx?.sessionKey || ""),
        });
        console.log(`[quaid][signal] daemon signal compaction session=${sessionId} source=session action=compact:before`);
      } catch (err: unknown) {
        if (isFailHardEnabled()) throw err;
        console.error("[quaid] session hook failed:", err);
      }
    }, {
      name: "session-memory-extraction",
      priority: 10,
    });

    // Extraction promise gate is facade-owned so adapters remain swappable.
    timeoutManager = new SessionTimeoutManager({
      workspace: WORKSPACE,
      logDir: path.join(QUAID_LOGS_DIR, "quaid"),
      timeoutMinutes: () => facade.getCaptureTimeoutMinutes(),
      failHardEnabled: () => isFailHardEnabled(),
      isBootstrapOnly: (messages: any[]) => facade.isResetBootstrapOnlyConversation(messages),
      shouldSkipText: (text: string) => shouldSkipTranscriptText(text),
      readSessionMessages: (sessionId: string) => facade.readTimeoutSessionMessages(sessionId),
      listSessionActivity: () => facade.listTimeoutSessionActivity(),
      hasPendingSessionNotes: (sessionId: string) => facade.hasPendingMemoryNotes(sessionId),
      logger: (msg: string) => {
        const lowered = String(msg || "").toLowerCase();
        if (lowered.includes("fail") || lowered.includes("error")) {
          console.warn(msg);
          return;
        }
        console.log(msg);
      },
      extract: async (_msgs: any[], sid?: string, label?: string) => {
        // Extraction now delegated to the shared Python daemon.
        // The timeout manager calls this on idle-session timeout;
        // write a daemon signal so the daemon handles it.
        if (sid) {
          writeDaemonSignal(sid, "compaction", {
            source: "timeout_extract",
            label: label || "Timeout",
          });
          console.log(`[quaid][timeout] daemon signal for idle session=${sid} label=${label || "Timeout"}`);
        }
      },
    });
    // TS signal worker disabled — shared Python daemon processes extraction signals.
    // The daemon is started on boot and polls data/extraction-signals/ for work.
    // timeoutManager.startWorker() is no longer called.

    // Start the shared extraction daemon
    ensureDaemonAlive();
    console.log("[quaid][daemon] extraction daemon ensure_alive called at boot");
    startSessionIndexWatcher();

    // Session-transition fallback for OC TUI /new visual-only transitions:
    // When /new is typed in OC TUI, OC creates a new session internally but does NOT
    // update sessions.json or create the new session's JSONL on disk. The per-key
    // watcher therefore cannot detect the transition. However, before_agent_start
    // still fires for the new (post-/new) session ID.
    //
    // When we see a session ID we have never tracked (not in sessionKeyLastSeen),
    // find the session the watcher most recently observed activity in and write a
    // reset for it. "Most recently observed activity" means the watcher saw new
    // messages in that session in the current gateway lifetime — recorded in
    // sessionLastActivityMs. This avoids mtime-based guessing and false positives
    // from prior-run sessions that the watcher never actively watched this boot.
    onChecked("before_agent_start", async (event: any, ctx: any) => {
      if (isInternalSessionContext(event, ctx)) return;
      const newSessionId = String(ctx?.sessionId || event?.sessionId || "").trim();
      if (!newSessionId) return;
      writeHookTrace("hook.before_agent_start.session_seen", { session_id: newSessionId });

      // Only trigger a session transition when the NEW session is an interactive
      // user-facing session. OC embedded/internal operations (compact agent,
      // slug-gen, tool runners, etc.) also fire before_agent_start with new
      // session IDs. If we allow those to trigger a transition, we write a reset
      // signal for the prior interactive session while an embedded op is still
      // running — which causes OC to clear the command lane and abort the op
      // (CommandLaneClearedError on /compact).
      const newSessionKey = String(
        ctx?.sessionKey || event?.sessionKey || event?.targetSessionKey || resolveSessionKeyForSessionId(newSessionId)
      ).trim().toLowerCase();
      const isInteractiveKey = !newSessionKey
        || newSessionKey === "agent:main:main"
        || newSessionKey.startsWith("agent:main:tui-")
        || newSessionKey.startsWith("agent:main:telegram:");
      if (!isInteractiveKey) return;

      const isAlreadyTracked = Array.from(sessionKeyLastSeen.values()).includes(newSessionId);
      if (!isAlreadyTracked && isSystemEnabled("memory")) {
        // Find the prior session. Strategy:
        // 1. "Just reset" detection: look for a session whose JSONL is 0 bytes AND
        //    has a very recent .reset.* backup (< 120s old). This is the unambiguous
        //    signature of an OC /reset — only the session that was just reset shows
        //    this pattern. This is more reliable than mtime because the reset operation
        //    itself empties the JSONL (setting its mtime to "now"), which can make the
        //    reset session appear artificially recent OR other sessions can have
        //    higher mtimes for unrelated reasons (background OC activity).
        // 2. Fallback to JSONL mtime if no reset-signature session found.
        const RECENT_RESET_WINDOW_MS = 120_000;
        const nowMs = Date.now();
        let bestPriorSessionId: string | null = null;
        let detectionMethod = "mtime";

        // Pass 1: look for the definitive just-reset signature by scanning the
        // sessions base dir directly for .reset.* backup files modified within
        // the window. This is filesystem-only and survives gateway restarts
        // (e.g., OC restarts the gateway process on /reset, wiping sessionKeyLastSeen).
        try {
          const baseDir = getOpenClawSessionsBaseDir();
          const allFiles = fs.readdirSync(baseDir);
          let bestResetMtimeMs = 0;
          for (const fname of allFiles) {
            const dotIdx = fname.indexOf(".jsonl.reset.");
            if (dotIdx < 0) continue;
            const sid = fname.slice(0, dotIdx);
            if (!sid) continue;
            // Do NOT skip sid === newSessionId. OC TUI /reset keeps the same session
            // UUID — it empties the JSONL in place and creates a .reset.* backup, then
            // restarts the gateway. The new gateway fires before_agent_start for the
            // same UUID as if it were a fresh session. We need to detect that the
            // "new" session is actually itself a just-reset session and extract from
            // its own backup.
            try {
              const backupStat = fs.statSync(path.join(baseDir, fname));
              const age = nowMs - backupStat.mtimeMs;
              if (age >= 0 && age < RECENT_RESET_WINDOW_MS && backupStat.mtimeMs > bestResetMtimeMs) {
                bestResetMtimeMs = backupStat.mtimeMs;
                bestPriorSessionId = sid;
                detectionMethod = sid === newSessionId ? "self_reset" : "reset_signature";
              }
            } catch {}
          }
        } catch {}

        // Pass 2: fall back to JSONL mtime if no reset-signature session found.
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
            } catch {}
          }
        }

        if (bestPriorSessionId) {
          const priorKey = Array.from(sessionKeyLastSeen.entries())
            .find(([k, v]) => v === bestPriorSessionId && !/^agent:[^:]+:hook:/.test(k))?.[0]
            || "agent:main:tui-unknown";
          writeHookTrace("hook.before_agent_start.fallback_transition", {
            new_session_id: newSessionId,
            prior_session_id: bestPriorSessionId,
            prior_key: priorKey,
            detection_method: detectionMethod,
          });
          if (
            !isInternalSessionContext({ sessionKey: priorKey }, { sessionId: bestPriorSessionId })
            && facade.shouldProcessLifecycleSignal(bestPriorSessionId, {
              label: "ResetSignal",
              source: "hook",
              signature: `before_agent_start:fallback:${bestPriorSessionId}`,
            })
          ) {
            facade.markLifecycleSignalFromHook(bestPriorSessionId, "ResetSignal");
            writeDaemonSignal(bestPriorSessionId, "reset", {
              source: "before_agent_start_fallback",
              prior_session_id: bestPriorSessionId,
              new_session_id: newSessionId,
            });
            console.log(
              `[quaid][signal] daemon signal reset session=${bestPriorSessionId} source=before_agent_start_fallback`,
            );
          }
        }
        // Seed so repeated before_agent_start fires for the same new session don't re-trigger.
        sessionKeyLastSeen.set(`agent:main:hook:${newSessionId}`, newSessionId);
      }

    }, {
      name: "before-agent-start-session-transition",
      priority: 5,
    });

    // Shared recall abstraction — used by both memory_recall tool and auto-inject
    async function recallMemories(opts: RecallOptions): Promise<MemoryResult[]> {
      const {
        query, limit = 10, expandGraph = false,
        graphDepth = 1, datastores, routeStores = false, reasoning = "fast", intent = "general", ranking, domain = { all: true }, domainBoost, project, dateFrom, dateTo, docs, datastoreOptions, waitForExtraction = false, sourceTag = "unknown"
      } = opts;
      console.log(
        `[quaid][recall] source=${sourceTag} query="${String(query || "").slice(0, 120)}" limit=${limit} expandGraph=${expandGraph} graphDepth=${graphDepth} datastores=${Array.isArray(datastores) ? datastores.join(",") : "auto"} routed=${routeStores} reasoning=${reasoning} intent=${intent} domain=${JSON.stringify(domain)} domainBoost=${JSON.stringify(domainBoost || {})} project=${project || "any"} waitForExtraction=${waitForExtraction}`
      );

      // Wait for in-flight extraction if requested
      const queuedExtraction = facade.getQueuedExtractionPromise();
      if (waitForExtraction && queuedExtraction) {
        const waitStartedAt = Date.now();
        writeHookTrace("recall.wait_for_extraction.start", {
          source: sourceTag,
          query_preview: String(query || "").slice(0, 160),
        });
        let raceTimer: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            queuedExtraction,
            new Promise<void>((_, rej) => { raceTimer = setTimeout(() => rej(new Error("timeout")), 60_000); })
          ]);
          writeHookTrace("recall.wait_for_extraction.done", {
            source: sourceTag,
            wait_ms: Date.now() - waitStartedAt,
          });
        } catch (err: unknown) {
          writeHookTrace("recall.wait_for_extraction.error", {
            source: sourceTag,
            wait_ms: Date.now() - waitStartedAt,
            error: String((err as Error)?.message || err),
          });
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
        failOpen: opts.failOpen,
      };
      const recallResponse = (sourceTag !== "tool" && !(routeStores ?? false))
        ? await facade.recallWithDiagnostics(recallOpts)
        : {
            results: await (sourceTag === "tool"
              ? facade.recallWithToolRetry(recallOpts)
              : facade.recall(recallOpts)),
            diagnostics: null,
          };
      const results = Array.isArray(recallResponse.results) ? recallResponse.results : [];
      writeHookTrace("hook.recall_pipeline", {
        source: sourceTag,
        query_preview: String(query || "").slice(0, 160),
        datastores: Array.isArray(datastores) ? datastores : [],
        routed: Boolean(routeStores ?? false),
        reasoning,
        result_count: results.length,
        diagnostics: summarizeRecallDiagnostics(recallResponse.diagnostics),
        top_results: summarizeRecallResults(results),
      });
      if (recallResponse.diagnostics) {
        Object.defineProperty(results, "__quaidRecallDiagnostics", {
          value: recallResponse.diagnostics,
          enumerable: false,
          configurable: true,
        });
      }
      return results;
    }

    // Read messages from a session JSONL file
    // Shared memory extraction logic — used by both compaction and reset hooks
    const extractMemoriesFromMessages = async (messages: any[], label: string, sessionId?: string) => {
      console.log(`[quaid][extract] start label=${label} session=${sessionId || "unknown"} message_count=${messages.length}`);
      writeHookTrace("extract.start", {
        label,
        session_id: sessionId || "",
        message_count: messages.length,
      });
      if (!messages.length) {
        console.log(`[quaid] ${label}: no messages to analyze`);
        writeHookTrace("extract.skip_empty_messages", {
          label,
          session_id: sessionId || "",
        });
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

      const startNotify = facade.shouldNotifyExtractionStart({
        messages,
        label,
        sessionId,
        hasMeaningfulUserContent,
        bootTimeMs: ADAPTER_BOOT_TIME_MS,
        backlogNotifyStaleMs: BACKLOG_NOTIFY_STALE_MS,
        showProcessingStart: getMemoryConfig().notifications?.showProcessingStart !== false,
      });
      if (startNotify) {
        writeHookTrace("extract.notify_start", {
          label,
          session_id: sessionId || "",
          trigger: startNotify.triggerDesc,
        });
        spawnNotifyScript(`
from core.runtime.notify import notify_user
notify_user("🧠 Processing memories from ${startNotify.triggerDesc}...")
`);
      }

      let extractionResult: any = null;
      try {
        extractionResult = await facade.runExtractionPipeline(messages, label, sessionId);
      } catch (err: unknown) {
        const msg = String((err as Error)?.message || err);
        console.error(`[quaid] ${label} extraction failed: ${msg}`);
        writeHookTrace("extract.pipeline_error", {
          label,
          session_id: sessionId || "",
          error: msg,
        });
        // Extraction is best-effort at this adapter boundary. Lower layers still
        // enforce failHard semantics where configured, but we avoid crashing the
        // active gateway/session loop on background extraction failures.
        return;
      }

      if (!extractionResult) {
        console.log(`[quaid] ${label}: empty transcript after filtering`);
        writeHookTrace("extract.skip_empty_after_filter", {
          label,
          session_id: sessionId || "",
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
        `[quaid][extract] payload label=${label} session=${sessionId || "unknown"} ` +
        `facts_len=${factDetails.length} first_status=${firstFactStatus} ` +
        `stored=${stored} skipped=${skipped} edges=${edgesCreated}`,
      );
      writeHookTrace("extract.pipeline_done", {
        label,
        session_id: sessionId || "",
        fact_count: factDetails.length,
        stored,
        skipped,
        edges_created: edgesCreated,
        trigger_type: triggerFromExtraction,
      });
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
        writeHookTrace("extract.notify_compaction_batched", {
          session_id: dedupeSession,
          trigger_type: triggerType,
          stored,
          skipped,
          edges_created: edgesCreated,
        });
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
        writeHookTrace("extract.notify_completion", {
          session_id: dedupeSession,
          trigger_type: triggerType,
          stored,
          skipped,
          edges_created: edgesCreated,
          has_snippets: hasSnippets,
          has_journal_entries: hasJournalEntries,
          always_notify_completion: alwaysNotifyCompletion,
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
            alwaysNotifyCompletion,
          });
          const detailsPath = path.join(QUAID_TMP_DIR, `extraction-details-${Date.now()}.json`);
          fs.writeFileSync(detailsPath, JSON.stringify(payload), { mode: 0o600 });
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
          writeHookTrace("extract.notify_completion_error", {
            session_id: dedupeSession,
            trigger_type: triggerType,
            error: String((notifyErr as Error)?.message || notifyErr),
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
          always_notify_completion: alwaysNotifyCompletion,
        });
      }

      if (triggerType === "timeout") {
        await facade.maybeForceCompactionAfterTimeout(sessionId);
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
    onChecked("before_compaction", async (event: any, ctx: any) => {
      try {
        if (isInternalSessionContext(event, ctx)) {
          return;
        }
        const messages: any[] = event.messages || [];
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const fallbackInteractiveSessionId = currentInteractiveSession?.sessionId || "";
        // Always prefer the directly-passed sessionId (OC always provides ctx.sessionId).
        // Only fall back to currentInteractiveSession when sessionId is absent AND
        // the payload is empty (OC sometimes fires before_compaction with no messages).
        const extractionSessionId =
          sessionId
          || (conversationMessages.length === 0 ? fallbackInteractiveSessionId : "")
          || facade.extractSessionId(messages, ctx)
          || "";
        writeHookTrace("hook.before_compaction.received", {
          hook_session_id: sessionId || "",
          extraction_session_id: extractionSessionId || "",
          fallback_interactive_session_id: fallbackInteractiveSessionId,
          event_message_count: messages.length,
          conversation_message_count: conversationMessages.length,
        });
        if (conversationMessages.length === 0) {
          console.log(`[quaid] before_compaction: empty/internal hook payload; deferring to timeout source session=${extractionSessionId || "unknown"}`);
          writeHookTrace("hook.before_compaction.empty_payload", {
            extraction_session_id: extractionSessionId || "",
          });
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
                writeDaemonSignal(extractionSessionId, "compaction", {
                  source: "before_compaction",
                  hook_session_id: String(sessionId || ""),
                  extraction_session_id: String(extractionSessionId || ""),
                  event_message_count: messages.length,
                  conversation_message_count: conversationMessages.length,
                  has_system_compacted_notice: conversationMessages.some(
                    (m: any) => String(facade.getMessageText(m) || "").toLowerCase().includes("compacted (")
                  ),
                });
                console.log(`[quaid][signal] daemon signal compaction session=${extractionSessionId}`);
                writeHookTrace("hook.before_compaction.signal_queued", {
                  extraction_session_id: extractionSessionId || "",
                  source: "before_compaction",
                });
              } else {
                console.log(`[quaid][signal] suppressed duplicate CompactionSignal session=${extractionSessionId}`);
                writeHookTrace("hook.before_compaction.signal_suppressed", {
                  extraction_session_id: extractionSessionId || "",
                  reason: "duplicate",
                });
              }
            } else {
              // Empty hook payload — still write daemon signal; daemon reads transcript from disk
              const sigPath = writeDaemonSignal(extractionSessionId, "compaction", {
                source: "before_compaction_empty_payload",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || ""),
              });
              console.log(
                `[quaid][signal] daemon signal compaction (empty-payload) session=${extractionSessionId} wrote=${sigPath ? "yes" : "no"}`
              );
              writeHookTrace("hook.before_compaction.empty_payload_daemon_signal", {
                extraction_session_id: extractionSessionId || "",
                signal_written: Boolean(sigPath),
              });
            }
          } else {
            console.log("[quaid] Compaction: memory extraction skipped — memory system disabled");
            writeHookTrace("hook.before_compaction.skip_memory_disabled", {
              extraction_session_id: extractionSessionId || "",
            });
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

          // Project events are now emitted from extract_from_transcript() in Python.
          // The extraction call already identifies projects and produces summaries.

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
            writeHookTrace("hook.before_compaction.extraction_failed", {
              hook_session_id: sessionId || "",
              extraction_session_id: extractionSessionId || "",
              error: String((doErr as Error)?.message || doErr),
            });
            if (isFailHardEnabled()) {
              console.error(`[quaid] extraction failed (fail-hard): ${doErr}`);
            }
          });
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_compaction hook failed:", err);
        writeHookTrace("hook.before_compaction.error", {
          hook_session_id: String(ctx?.sessionId || ""),
          error: String((err as Error)?.message || err),
        });
      }
    }, {
      name: "compaction-memory-extraction",
      priority: 10
    });

    // command:new/reset hooks are inconsistent across runtime variants.
    // We use message event capture above for reset lifecycle detection.

    // Register reset hook — compatibility fallback for older runtimes.
    // Primary reset/new boundary path is session_end below.
    onChecked("before_reset", async (event: any, ctx: any) => {
      try {
        if (isInternalSessionContext(event, ctx)) {
          return;
        }
        const messages: any[] = event.messages || [];
        const reason = event.reason || "unknown";
        const sessionId = ctx?.sessionId;
        const conversationMessages = facade.filterConversationMessages(messages);
        const extractionSessionId = facade.resolveLifecycleHookSessionId(event, ctx, conversationMessages);
        writeHookTrace("hook.before_reset.received", {
          hook_session_id: sessionId || "",
          extraction_session_id: extractionSessionId || "",
          reason: String(reason || "unknown"),
          event_message_count: messages.length,
          conversation_message_count: conversationMessages.length,
        });
        if (!extractionSessionId) {
          console.log(`[quaid] before_reset: skip unresolved session id session=${sessionId || "unknown"}`);
          writeHookTrace("hook.before_reset.skipped", {
            hook_session_id: sessionId || "",
            reason: "unresolved_session_id",
          });
          return;
        }
        if (conversationMessages.length === 0) {
          console.log(
            `[quaid] before_reset: empty/internal transcript; queueing ResetSignal from source session session=${extractionSessionId}`
          );
        }
        console.log(`[quaid] before_reset hook triggered (reason: ${reason}), ${messages.length} messages, session=${sessionId || "unknown"}`);

        // Snapshot the transcript synchronously before OC tears it down.
        // writeDaemonSignal (called inside doExtraction) reads sessionTranscriptPaths,
        // which preserveSessionTranscript updates to point at the preserved copy.
        preserveSessionTranscript(extractionSessionId, null, "before_reset");

        const doExtraction = async () => {
          // before_reset can race with session teardown; queue signal for worker tick.
          if (isSystemEnabled("memory")) {
            if (facade.shouldProcessLifecycleSignal(extractionSessionId, {
              label: "ResetSignal",
              source: "hook",
              signature: "hook:before_reset",
            })) {
              facade.markLifecycleSignalFromHook(extractionSessionId, "ResetSignal");
              writeDaemonSignal(extractionSessionId, "reset", {
                source: "before_reset",
                hook_session_id: String(sessionId || ""),
                extraction_session_id: String(extractionSessionId || ""),
                reason: String(reason || "unknown"),
                event_message_count: messages.length,
                conversation_message_count: conversationMessages.length,
              });
              console.log(`[quaid][signal] daemon signal reset session=${extractionSessionId}`);
              writeHookTrace("hook.before_reset.signal_queued", {
                extraction_session_id: extractionSessionId,
                reason: String(reason || "unknown"),
              });
            } else {
              console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${extractionSessionId}`);
              writeHookTrace("hook.before_reset.signal_suppressed", {
                extraction_session_id: extractionSessionId,
                reason: "duplicate",
              });
            }
          } else {
            console.log("[quaid] Reset: memory extraction skipped — memory system disabled");
            writeHookTrace("hook.before_reset.skip_memory_disabled", {
              extraction_session_id: extractionSessionId,
            });
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

            // Project events are now emitted from extract_from_transcript() in Python.
          }
          console.log(`[quaid][reset] extraction_end session=${sessionId || "unknown"}`);
        };

        // Chain onto any in-flight extraction to avoid overwrite race
        const chainActive = facade.getQueuedExtractionPromise() ? "yes" : "no";
        console.log(`[quaid][reset] queue_extraction session=${sessionId || "unknown"} chain_active=${chainActive}`);
        facade.queueExtraction(doExtraction, "reset")
          .catch((doErr: unknown) => {
            console.error(`[quaid][reset] extraction_failed session=${sessionId || "unknown"} err=${String((doErr as Error)?.message || doErr)}`);
            writeHookTrace("hook.before_reset.extraction_failed", {
              hook_session_id: sessionId || "",
              extraction_session_id: extractionSessionId,
              error: String((doErr as Error)?.message || doErr),
            });
            if (isFailHardEnabled()) {
              console.error(`[quaid] extraction failed (fail-hard): ${doErr}`);
            }
          });
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] before_reset hook failed:", err);
        writeHookTrace("hook.before_reset.error", {
          hook_session_id: String(ctx?.sessionId || ""),
          error: String((err as Error)?.message || err),
        });
      }
    }, {
      name: "reset-memory-extraction",
      priority: 10
    });

    // Primary reset/new lifecycle capture path.
    // session_end is emitted when OpenClaw replaces/resets a session.
    onChecked("session_end", async (event: any, ctx: any) => {
      try {
        const sessionId = String(event?.sessionId || ctx?.sessionId || "").trim();
        const sessionKey = String(event?.sessionKey || ctx?.sessionKey || "").trim();
        const messageCount = Number(event?.messageCount || 0);
        writeHookTrace("hook.session_end.received", {
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0,
        });
        if (!sessionId || isInternalSessionContext(event, ctx)) {
          writeHookTrace("hook.session_end.skipped", {
            hook_session_id: sessionId,
            reason: "invalid_or_internal_session",
          });
          return;
        }
        if (!isSystemEnabled("memory")) {
          writeHookTrace("hook.session_end.skipped", {
            hook_session_id: sessionId,
            reason: "memory_disabled",
          });
          return;
        }
        if (!facade.shouldProcessLifecycleSignal(sessionId, {
          label: "ResetSignal",
          source: "hook",
          signature: "hook:session_end",
        })) {
          console.log(`[quaid][signal] suppressed duplicate ResetSignal session=${sessionId} source=session_end`);
          writeHookTrace("hook.session_end.signal_suppressed", {
            hook_session_id: sessionId,
            reason: "duplicate",
          });
          return;
        }
        facade.markLifecycleSignalFromHook(sessionId, "ResetSignal");
        writeDaemonSignal(sessionId, "session_end", {
          source: "session_end",
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
          message_count: Number.isFinite(messageCount) ? messageCount : 0,
        });
        console.log(
          `[quaid][signal] daemon signal session_end session=${sessionId} key=${sessionKey || "unknown"}`
        );
        writeHookTrace("hook.session_end.signal_queued", {
          hook_session_id: sessionId,
          hook_session_key: sessionKey,
        });
      } catch (err: unknown) {
        if (isFailHardEnabled()) {
          throw err;
        }
        console.error("[quaid] session_end hook failed:", err);
        writeHookTrace("hook.session_end.error", {
          hook_session_id: String(event?.sessionId || ctx?.sessionId || ""),
          error: String((err as Error)?.message || err),
        });
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

          const sessionLogPath = facade.getInjectionLogPath(sessionId);
          
          let logData: any = null;
          
          if (fs.existsSync(sessionLogPath)) {
            try {
              const content = fs.readFileSync(sessionLogPath, 'utf8');
              logData = JSON.parse(content);
            } catch (err: unknown) {
              console.error(`[quaid] Failed to read session log: ${String(err)}`);
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
  isAutoInjectEnabled,
  resolveAdapterMemoryDbPath,
  scrubAutoInjectQuery,
  summarizeRecallDiagnostics,
  summarizeRecallResults,
  selectAutoInjectQuery,
  isInternalSessionContext,
};
