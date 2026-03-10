import * as fs from "node:fs";
import * as path from "node:path";

type SessionCursorPayload = {
  sessionId: string;
  clearedAt: string;
  lastMessageKey?: string;
  lastTimestampMs?: number;
};

type SessionActivityRecord = {
  sessionId: string;
  lastActivityMs: number;
};

type StaleRetryState = {
  sessionId: string;
  lastActivityMs: number;
  attemptCount: number;
  nextRecoveryAt?: string;
  lastError?: string;
};

type StaleSweepState = {
  installedAt?: string;
  lastSweepAt?: string;
  retries?: Record<string, StaleRetryState>;
};

type TimeoutExtractor = (messages: any[], sessionId?: string, label?: string) => Promise<void>;
type TimeoutLogger = (message: string) => void;

type SessionTimeoutManagerOptions = {
  workspace: string;
  logDir?: string;
  timeoutMinutes: number;
  extract: TimeoutExtractor;
  isBootstrapOnly: (messages: any[]) => boolean;
  failHardEnabled?: boolean | (() => boolean);
  logger?: TimeoutLogger;
  readSessionMessages?: (sessionId: string) => any[];
  listSessionActivity?: () => SessionActivityRecord[];
  hasPendingSessionNotes?: (sessionId: string) => boolean;
  shouldSkipText?: (text: string) => boolean;
};
type AgentEndMeta = {
  source?: string;
};

function safeLog(logger: TimeoutLogger | undefined, message: string): void {
  try {
    if (logger) {
      logger(message);
      return;
    }
    const looksLikeFailure = /\b(fail|error|warn|timeout|exception)\b/i.test(String(message || ""));
    if (looksLikeFailure) {
      console.warn(message);
    } else {
      console.log(message);
    }
  } catch {}
}

function messageText(msg: any): string {
  if (!msg) return "";
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) return msg.content.map((c: any) => c?.text || "").join(" ");
  return "";
}

function isInternalMaintenancePrompt(text: string): boolean {
  const t = String(text || "").trim().toLowerCase();
  if (!t) return false;
  const markers = [
    "extract memorable facts and journal entries from this conversation",
    "given a personal memory query and memory documents",
    "given a personal memory query, determine if this memory is relevant to the query",
    "rate each document",
    "generate focused memory-retrieval sub-queries for the user question",
    "rephrase this question as a declarative statement about someone's personal life",
    "review batch",
    "review the following",
    "you are reviewing",
    "you are checking",
    "respond with a json array",
    "json array only:",
    "fact a:",
    "fact b:",
    "candidate duplicate pairs",
    "dedup rejections",
    "journal entries to decide",
    "pending soul snippets",
  ];
  return markers.some((m) => t.includes(m));
}

function isExtractionJsonAssistantPayload(text: string): boolean {
  const compact = String(text || "").replace(/\s+/g, " ").trim();
  if (!/^\{\s*"facts"\s*:\s*\[/.test(compact)) return false;
  try {
    const parsed = JSON.parse(compact);
    if (!parsed || typeof parsed !== "object") return false;
    const keys = Object.keys(parsed);
    return keys.length > 0 && keys.every((k) => k === "facts" || k === "journal_entries" || k === "soul_snippets");
  } catch {
    return false;
  }
}

function isEligibleConversationMessage(msg: any, shouldSkipText?: (text: string) => boolean): boolean {
  if (!msg || (msg.role !== "user" && msg.role !== "assistant")) return false;
  const text = messageText(msg).trim();
  if (!text) return false;
  if (shouldSkipText?.(text)) return false;
  if (isInternalMaintenancePrompt(text)) return false;
  if (msg.role === "assistant" && isExtractionJsonAssistantPayload(text)) return false;
  return true;
}

function hasLifecycleSignalEvidence(messages: any[], label: string): boolean {
  if (!Array.isArray(messages) || messages.length === 0) return false;
  const normalizedLabel = String(label || "").trim().toLowerCase();
  for (const msg of messages) {
    if (!msg || typeof msg !== "object") continue;
    const role = String(msg.role || "").trim().toLowerCase();
    const text = messageText(msg).trim();
    if (!text) continue;
    const normalized = text
      .replace(/\[\[[^\]]+\]\]\s*/g, "")
      .replace(/^\[[^\]]+\]\s*/, "")
      .trim();
    const normalizedLc = normalized.toLowerCase();
    if (normalizedLabel === "resetsignal" || normalizedLabel === "reset") {
      if (role === "user" && /^\/(new|reset|restart)(?:\s|$)/i.test(normalized)) return true;
      if (
        normalizedLc.includes("new session was started via /new or /reset")
        || normalizedLc.includes("a new session was started via /new or /reset")
        || (normalizedLc.includes("new session was started") && normalizedLc.includes("/new"))
        || (normalizedLc.includes("session startup sequence") && normalizedLc.includes("/new"))
      ) {
        return true;
      }
      continue;
    }
    if (normalizedLabel === "compactionsignal" || normalizedLabel === "compaction") {
      if (role === "user" && /^\/compact(?:\s|$)/i.test(normalized)) return true;
      if (/(^|\s)\/compact(\s|$)/i.test(normalized)) return true;
      const hasCompacted = /\bcompacted\b/i.test(normalized);
      const hasDelta = /\(\s*[\d.]+k?\s*(?:->|→)\s*[\d.]+k?\s*\)/i.test(normalized);
      const hasContext = /\bcontext\b/i.test(normalized);
      if (hasCompacted && (hasDelta || hasContext)) return true;
    }
  }
  return false;
}

function filterEligibleMessages(messages: any[], shouldSkipText?: (text: string) => boolean): any[] {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg: any) => isEligibleConversationMessage(msg, shouldSkipText));
}

function messageDedupKey(msg: any): string {
  const id = typeof msg?.id === "string" ? msg.id : "";
  if (id) return `id:${id}`;
  const ts = typeof msg?.timestamp === "string" ? msg.timestamp : "";
  const role = typeof msg?.role === "string" ? msg.role : "";
  const text = messageText(msg).slice(0, 200);
  return `fallback:${ts}:${role}:${text}`;
}

function parseMessageTimestampMs(msg: any): number | null {
  const ts = msg?.timestamp;
  if (typeof ts === "number" && Number.isFinite(ts)) return ts;
  if (typeof ts === "string") {
    const asNum = Number(ts);
    if (Number.isFinite(asNum)) return asNum;
    const parsed = Date.parse(ts);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function mergeUniqueMessages(existing: any[], incoming: any[]): any[] {
  if (!incoming.length) return existing;
  const out = [...existing];
  const seen = new Set(existing.map((m) => messageDedupKey(m)));
  for (const msg of incoming) {
    const key = messageDedupKey(msg);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(msg);
  }
  return out;
}

export class SessionTimeoutManager {
  private timeoutMinutes: number;
  private extract: TimeoutExtractor;
  private isBootstrapOnly: (messages: any[]) => boolean;
  private logger?: TimeoutLogger;
  private readSessionMessagesSource: (sessionId: string) => any[];
  private listSessionActivitySource: () => SessionActivityRecord[];
  private shouldSkipText?: (text: string) => boolean;
  private hasPendingSessionNotesSource: (sessionId: string) => boolean;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pendingFallbackMessages: any[] | null = null;
  private pendingSessionId: string | undefined;
  private sessionCursorDir: string;
  private staleSweepStatePath: string;
  private installStatePath: string;
  private logDir: string;
  private sessionLogDir: string;
  private logFilePath: string;
  private eventFilePath: string;
  private chain: Promise<void> = Promise.resolve();
  private failHard: boolean;
  private extractTimeoutMs: number;
  private maxStaleRecoverPerTick: number;
  private readonly staleRecoveryInitialBackoffMs = 5000;
  private readonly staleRecoveryMaxBackoffMs = 5 * 60 * 1000;

  constructor(opts: SessionTimeoutManagerOptions) {
    this.timeoutMinutes = opts.timeoutMinutes;
    this.extract = opts.extract;
    this.isBootstrapOnly = opts.isBootstrapOnly;
    this.logger = opts.logger;
    this.shouldSkipText = opts.shouldSkipText;
    this.readSessionMessagesSource = (sessionId: string) => {
      try {
        return filterEligibleMessages(opts.readSessionMessages?.(sessionId) || [], opts.shouldSkipText);
      } catch (err: unknown) {
        safeLog(this.logger, `[memory][timeout] source readSessionMessages failed for ${sessionId}: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
        return [];
      }
    };
    this.listSessionActivitySource = () => {
      try {
        const rows = opts.listSessionActivity?.() || [];
        if (!Array.isArray(rows)) return [];
        return rows
          .map((r) => ({
            sessionId: String(r?.sessionId || "").trim(),
            lastActivityMs: Number(r?.lastActivityMs),
          }))
          .filter((r) => r.sessionId && Number.isFinite(r.lastActivityMs) && r.lastActivityMs > 0);
      } catch (err: unknown) {
        safeLog(this.logger, `[memory][timeout] source listSessionActivity failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
        return [];
      }
    };
    this.hasPendingSessionNotesSource = (sessionId: string) => {
      try {
        return Boolean(opts.hasPendingSessionNotes?.(sessionId));
      } catch (err: unknown) {
        safeLog(this.logger, `[memory][timeout] source hasPendingSessionNotes failed for ${sessionId}: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
        return false;
      }
    };

    this.logDir = path.resolve(String(opts.logDir || path.join(opts.workspace, "logs", "runtime")));
    this.sessionLogDir = path.join(this.logDir, "sessions");
    this.sessionCursorDir = path.join(opts.workspace, "data", "session-cursors");
    this.staleSweepStatePath = path.join(opts.workspace, "data", "stale-sweep-state.json");
    this.installStatePath = path.join(opts.workspace, "data", "installed-at.json");
    this.logFilePath = path.join(this.logDir, "session-timeout.log");
    this.eventFilePath = path.join(this.logDir, "session-timeout-events.jsonl");
    const failHardOpt = opts.failHardEnabled;
    if (typeof failHardOpt === "function") {
      try {
        this.failHard = Boolean(failHardOpt());
      } catch (err: unknown) {
        safeLog(this.logger, `[memory][timeout] failHard source threw; defaulting to true: ${String((err as Error)?.message || err)}`);
        this.failHard = true;
      }
    } else if (typeof failHardOpt === "boolean") {
      this.failHard = failHardOpt;
    } else {
      this.failHard = true;
    }
    const configuredTimeoutMs = Number(
      process.env.SESSION_EXTRACT_TIMEOUT_MS || process.env.QUAID_SESSION_EXTRACT_TIMEOUT_MS || "",
    );
    this.extractTimeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? Math.floor(configuredTimeoutMs)
      : 600_000;
    const configuredStaleRecoverPerTick = Number(
      process.env.STALE_RECOVERY_MAX_PER_TICK || process.env.QUAID_STALE_RECOVERY_MAX_PER_TICK || "",
    );
    this.maxStaleRecoverPerTick = Number.isFinite(configuredStaleRecoverPerTick) && configuredStaleRecoverPerTick > 0
      ? Math.floor(configuredStaleRecoverPerTick)
      : 3;

    try {
      fs.mkdirSync(this.logDir, { recursive: true });
      fs.mkdirSync(this.sessionLogDir, { recursive: true });
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      fs.mkdirSync(path.dirname(this.staleSweepStatePath), { recursive: true });
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err || "unknown directory initialization error");
      safeLog(this.logger, `[memory][timeout] failed to initialize runtime directories: ${msg}`);
      if (this.failHard) {
        throw err;
      }
    }
  }

  private async runExtractWithTimeout(messages: any[], sessionId?: string, label?: string): Promise<void> {
    const timeoutMs = Number(this.extractTimeoutMs);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      await this.extract(messages, sessionId, label);
      return;
    }
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      await Promise.race([
        this.extract(messages, sessionId, label),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error(`session-timeout extraction timed out after ${timeoutMs}ms`)),
            timeoutMs,
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  setTimeoutMinutes(minutes: number): void {
    this.timeoutMinutes = minutes;
  }

  onAgentStart(sessionId?: string): void {
    if (!this.timer) return;
    const sid = String(sessionId || "").trim();
    if (sid && this.pendingSessionId && sid !== this.pendingSessionId) {
      this.writeQuaidLog("timer_preserved", this.pendingSessionId, {
        reason: "agent_start_other_session",
        active_session_id: sid,
      });
      return;
    }
    clearTimeout(this.timer);
    this.timer = null;
    this.writeQuaidLog("timer_cleared", this.pendingSessionId || sid || undefined, {
      reason: "agent_start",
    });
  }

  onAgentEnd(messages: any[], sessionId: string, meta?: AgentEndMeta): void {
    if (!Array.isArray(messages) || messages.length === 0) return;
    if (!sessionId) return;
    const incoming = filterEligibleMessages(messages, this.shouldSkipText);
    if (incoming.length === 0) return;
    const gatedIncoming = this.filterReplayedMessages(sessionId, incoming);
    if (gatedIncoming.length === 0) {
      this.writeQuaidLog("skip_replayed_history", sessionId, { incoming: incoming.length });
      return;
    }
    const hasUserMessage = gatedIncoming.some((m: any) => m?.role === "user");
    if (!hasUserMessage) {
      this.writeQuaidLog("skip_assistant_only", sessionId, { incoming: gatedIncoming.length });
      return;
    }

    if (this.isBootstrapOnly(gatedIncoming)) {
      safeLog(this.logger, `[memory][timeout] skipping bootstrap-only transcript session=${sessionId} message_count=${gatedIncoming.length}; preserving prior timeout context`);
      this.writeQuaidLog("skip_bootstrap_only", sessionId, { message_count: gatedIncoming.length });
      return;
    }

    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }

    const source = String(meta?.source || "unknown");
    if (this.pendingSessionId === sessionId && this.pendingFallbackMessages) {
      this.pendingFallbackMessages = mergeUniqueMessages(this.pendingFallbackMessages, gatedIncoming);
      this.writeQuaidLog("buffer_write", sessionId, {
        source,
        mode: "merge",
        appended: gatedIncoming.length,
        total: this.pendingFallbackMessages.length,
      });
    } else {
      this.pendingFallbackMessages = gatedIncoming;
      this.pendingSessionId = sessionId;
      this.writeQuaidLog("buffer_write", sessionId, {
        source,
        mode: "set",
        appended: gatedIncoming.length,
        total: this.pendingFallbackMessages.length,
      });
    }

    this.writeQuaidLog("buffered", sessionId, {
      appended: gatedIncoming.length,
      timeout_minutes: this.timeoutMinutes,
      source: "event_messages",
    });

    if (this.timeoutMinutes <= 0) return;

    this.timer = setTimeout(() => {
      const sid = this.pendingSessionId;
      const fallback = this.pendingFallbackMessages || [];
      this.timer = null;
      this.pendingSessionId = undefined;
      this.pendingFallbackMessages = null;
      if (!sid) return;
      this.writeQuaidLog("timer_fired", sid, {
        timeout_minutes: this.timeoutMinutes,
      });
      this.queueExtractionFromSession(sid, fallback, this.timeoutMinutes);
    }, this.timeoutMinutes * 60 * 1000);
  }

  private async extractSessionFromSourceDirect(
    sessionId: string,
    label: string,
    fallbackMessages?: any[],
    signalMeta?: Record<string, any>,
  ): Promise<boolean> {
    if (!sessionId) return false;

    const sourceMessages = this.readSourceSessionMessages(sessionId);
    const sourceUnprocessed = this.filterReplayedMessages(sessionId, sourceMessages);

    const fallback = this.filterReplayedMessages(sessionId, filterEligibleMessages(fallbackMessages || [], this.shouldSkipText));
    const allowFallback = !this.failHard;
    const hasPendingNotes = this.hasPendingSessionNotesSource(sessionId);
    this.writeQuaidLog("extract_source_snapshot", sessionId, {
      label,
      source_messages: sourceMessages.length,
      source_unprocessed: sourceUnprocessed.length,
      fallback_messages: fallback.length,
      has_pending_notes: hasPendingNotes,
      fail_hard: this.failHard,
    });

    const source = sourceUnprocessed.length > 0
      ? "source_session_messages"
      : (allowFallback && fallback.length > 0
        ? "fallback_event_messages"
        : (hasPendingNotes ? "memory_notes_only" : "none"));

    const messages = sourceUnprocessed.length > 0 ? sourceUnprocessed : (allowFallback ? fallback : []);
    const signalSource = String(signalMeta?.source || "").trim().toLowerCase();
    if (
      source === "source_session_messages"
      && signalSource === "transcript_update"
      && (String(label || "").toLowerCase() === "resetsignal" || String(label || "").toLowerCase() === "compactionsignal")
      && !hasLifecycleSignalEvidence(messages, label)
    ) {
      this.writeQuaidLog("signal_skip_no_lifecycle_evidence", sessionId, {
        label,
        source: signalSource,
        message_count: messages.length,
      });
      return false;
    }
    if (!messages.length) {
      if (hasPendingNotes) {
        this.writeQuaidLog("extract_begin", sessionId, { label, message_count: 0, source, notes_only: true });
        await this.runExtractWithTimeout([], sessionId, label);
        this.writeQuaidLog("extract_done", sessionId, { label, message_count: 0, source, notes_only: true });
        this.clearSession(sessionId, []);
        return true;
      }
      if (this.failHard && fallback.length > 0 && sourceUnprocessed.length === 0) {
        const msg = "session-timeout fallback payload blocked by failHard; no source session messages available";
        this.writeQuaidLog("extract_fail_hard_blocked_fallback", sessionId, { label, fallback_count: fallback.length });
        throw new Error(msg);
      }
      this.writeQuaidLog("extract_skip_empty", sessionId, { label, source });
      return false;
    }

    this.writeQuaidLog("extract_begin", sessionId, { label, message_count: messages.length, source });
    await this.runExtractWithTimeout(messages, sessionId, label);
    this.writeQuaidLog("extract_done", sessionId, { label, message_count: messages.length, source });
    this.clearSession(sessionId, messages);
    return true;
  }

  async extractSessionFromLog(sessionId: string, label: string, fallbackMessages?: any[]): Promise<boolean> {
    let extracted = false;
    const work = this.chain
      .catch((err: unknown) => {
        safeLog(this.logger, `[memory][timeout] previous extraction chain error: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      })
      .then(async () => {
        extracted = await this.extractSessionFromSourceDirect(sessionId, label, fallbackMessages);
      })
      .catch((err: unknown) => {
        safeLog(this.logger, `[memory][timeout] extraction queue failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      });
    this.chain = work.then(() => undefined, () => undefined);
    await work;
    return extracted;
  }

  clearSession(sessionId?: string, cursorMessages?: any[]): void {
    if (!sessionId) return;
    const sourceMessages = cursorMessages || this.filterReplayedMessages(sessionId, this.readSourceSessionMessages(sessionId));
    this.writeSessionCursor(sessionId, sourceMessages);
    this.writeQuaidLog("session_cleared", sessionId);
    if (this.pendingSessionId === sessionId) {
      this.pendingSessionId = undefined;
      this.pendingFallbackMessages = null;
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
        this.writeQuaidLog("timer_cleared", sessionId, { reason: "session_cleared" });
      }
    }
  }

  async recoverStaleBuffers(): Promise<void> {
    if (this.timeoutMinutes <= 0) return;
    const timeoutMs = this.timeoutMinutes * 60 * 1000;
    const nowMs = Date.now();

    const state = this.readStaleSweepState();
    const installedAtMs = Date.parse(String(state.installedAt || this.readInstalledAt() || ""));
    const hasInstalledAt = Number.isFinite(installedAtMs);
    let lastSweepMs = Date.parse(String(state.lastSweepAt || ""));
    const isFirstSweep = !Number.isFinite(lastSweepMs);
    if (!Number.isFinite(lastSweepMs)) {
      lastSweepMs = nowMs - timeoutMs;
    }
    if (lastSweepMs > nowMs) {
      lastSweepMs = nowMs;
    }

    const currentCutoffMs = nowMs - timeoutMs;
    let previousCutoffMs = lastSweepMs - timeoutMs;
    if (isFirstSweep && hasInstalledAt) {
      // On first sweep, bound the historical scan at install time so we catch all
      // post-install stale sessions without reprocessing truly pre-install history.
      previousCutoffMs = Math.min(previousCutoffMs, installedAtMs - timeoutMs);
    }
    if (previousCutoffMs > currentCutoffMs) {
      previousCutoffMs = currentCutoffMs;
    }

    const activityRows = this.listSessionActivityRows();
    const latestActivityBySession = new Map<string, number>();
    for (const row of activityRows) {
      const prior = latestActivityBySession.get(row.sessionId);
      if (prior == null || row.lastActivityMs > prior) {
        latestActivityBySession.set(row.sessionId, row.lastActivityMs);
      }
    }

    const candidates = new Map<string, number>();
    for (const [sessionId, lastActivityMs] of latestActivityBySession.entries()) {
      if (lastActivityMs > currentCutoffMs) continue;
      if (lastActivityMs <= previousCutoffMs) continue;
      candidates.set(sessionId, lastActivityMs);
    }

    const retries = state.retries || {};
    for (const [sessionId, retry] of Object.entries(retries)) {
      const nextRecoveryAtMs = Date.parse(String(retry.nextRecoveryAt || ""));
      if (Number.isFinite(nextRecoveryAtMs) && nextRecoveryAtMs > nowMs) {
        continue;
      }
      const latestActivity = latestActivityBySession.get(sessionId);
      if (typeof latestActivity === "number" && latestActivity > Number(retry.lastActivityMs || 0)) {
        delete retries[sessionId];
        continue;
      }
      candidates.set(sessionId, Number(retry.lastActivityMs || latestActivity || 0));
    }

    this.writeQuaidLog("stale_sweep_window", undefined, {
      timeout_minutes: this.timeoutMinutes,
      previous_cutoff_ms: previousCutoffMs,
      current_cutoff_ms: currentCutoffMs,
      candidate_count: candidates.size,
    });

    let processed = 0;
    for (const [sessionId, lastActivityMs] of candidates.entries()) {
      if (processed >= this.maxStaleRecoverPerTick) {
        this.writeQuaidLog("stale_sweep_deferred", undefined, {
          deferred_count: Math.max(0, candidates.size - processed),
          max_per_tick: this.maxStaleRecoverPerTick,
        });
        break;
      }
      const messages = this.filterReplayedMessages(sessionId, this.readSourceSessionMessages(sessionId));
      if (!messages.length) {
        delete retries[sessionId];
        this.writeQuaidLog("recover_stale_buffer_skip_empty", sessionId, { last_activity_ms: lastActivityMs });
        processed += 1;
        continue;
      }

      this.writeQuaidLog("recover_stale_buffer", sessionId, {
        message_count: messages.length,
        source: "source_session_messages",
        last_activity_ms: lastActivityMs,
      });

      try {
        await this.runExtractWithTimeout(messages, sessionId, "Recovery");
        this.clearSession(sessionId, messages);
        delete retries[sessionId];
        processed += 1;
      } catch (err: unknown) {
        const error = String((err as Error)?.message || err);
        const priorAttempts = Math.max(0, Number(retries[sessionId]?.attemptCount || 0));
        const nextAttemptCount = priorAttempts + 1;
        const delayMs = this.staleRecoveryDelayMs(nextAttemptCount);
        retries[sessionId] = {
          sessionId,
          lastActivityMs,
          attemptCount: nextAttemptCount,
          nextRecoveryAt: new Date(nowMs + delayMs).toISOString(),
          lastError: error,
        };
        this.writeQuaidLog("recover_stale_buffer_backoff", sessionId, {
          attempt_count: nextAttemptCount,
          delay_ms: delayMs,
          error,
        });
        processed += 1;
      }
    }

    this.writeStaleSweepState({
      installedAt: state.installedAt || this.readInstalledAt(),
      lastSweepAt: new Date(nowMs).toISOString(),
      retries,
    });
  }

  private staleRecoveryDelayMs(attemptCount: number): number {
    const attempt = Math.max(1, Math.floor(attemptCount));
    const multiplier = 2 ** (attempt - 1);
    return Math.min(this.staleRecoveryInitialBackoffMs * multiplier, this.staleRecoveryMaxBackoffMs);
  }

  private queueExtractionFromSession(sessionId: string, fallbackMessages: any[], timeoutMinutes: number): void {
    this.chain = this.chain
      .catch((err: unknown) => {
        safeLog(this.logger, `[memory][timeout] previous extraction chain error: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      })
      .then(async () => {
        const extracted = await this.extractSessionFromSourceDirect(sessionId, "Timeout", fallbackMessages);
        if (!extracted) {
          this.writeQuaidLog("timeout_extract_skip_empty", sessionId, { timeout_minutes: timeoutMinutes });
          return;
        }
        this.writeQuaidLog("timeout_extract_done", sessionId, { timeout_minutes: timeoutMinutes });
      })
      .catch((err: unknown) => {
        safeLog(this.logger, `[memory][timeout] extraction queue failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      });
  }

  private cursorPath(sessionId: string): string {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.sessionCursorDir, `${safeSessionId}.json`);
  }

  private readSessionCursor(sessionId: string): SessionCursorPayload | null {
    try {
      const fp = this.cursorPath(sessionId);
      if (!fs.existsSync(fp)) return null;
      const payload = JSON.parse(fs.readFileSync(fp, "utf8")) as SessionCursorPayload;
      if (!payload || typeof payload !== "object") return null;
      return payload;
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed reading session cursor for ${sessionId}: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return null;
    }
  }

  private writeSessionCursor(sessionId: string, messages: any[]): void {
    try {
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      const last = Array.isArray(messages) && messages.length > 0 ? messages[messages.length - 1] : null;
      const payload: SessionCursorPayload = {
        sessionId,
        clearedAt: new Date().toISOString(),
      };
      if (last) {
        payload.lastMessageKey = messageDedupKey(last);
        const ts = parseMessageTimestampMs(last);
        if (ts !== null) payload.lastTimestampMs = ts;
      }
      fs.writeFileSync(this.cursorPath(sessionId), JSON.stringify(payload), { mode: 0o600 });
      this.writeQuaidLog("session_cursor_written", sessionId, {
        has_last_key: Boolean(payload.lastMessageKey),
        has_last_ts: typeof payload.lastTimestampMs === "number",
      });
    } catch (err: unknown) {
      this.writeQuaidLog("session_cursor_write_error", sessionId, { error: String((err as Error)?.message || err) });
      if (this.failHard) {
        throw err;
      }
    }
  }

  private filterReplayedMessages(sessionId: string, incoming: any[]): any[] {
    if (!incoming.length) return incoming;
    const cursor = this.readSessionCursor(sessionId);
    if (!cursor) return incoming;

    if (cursor.lastMessageKey) {
      for (let i = incoming.length - 1; i >= 0; i--) {
        const key = messageDedupKey(incoming[i]);
        if (key === cursor.lastMessageKey) {
          return incoming.slice(i + 1);
        }
      }
    }

    if (typeof cursor.lastTimestampMs === "number" && Number.isFinite(cursor.lastTimestampMs)) {
      return incoming.filter((msg) => {
        const ts = parseMessageTimestampMs(msg);
        return ts !== null && ts > cursor.lastTimestampMs!;
      });
    }

    return incoming;
  }

  private readSourceSessionMessages(sessionId: string): any[] {
    const rows = this.readSessionMessagesSource(sessionId);
    if (!Array.isArray(rows)) return [];
    return filterEligibleMessages(rows, this.shouldSkipText);
  }

  private listSessionActivityRows(): SessionActivityRecord[] {
    return this.listSessionActivitySource();
  }

  private hasUnprocessedSessionMessages(sessionId: string): boolean {
    if (this.pendingSessionId === sessionId && Array.isArray(this.pendingFallbackMessages)) {
      const pending = this.filterReplayedMessages(sessionId, filterEligibleMessages(this.pendingFallbackMessages, this.shouldSkipText));
      if (pending.length > 0) return true;
    }
    const messages = this.readSourceSessionMessages(sessionId);
    if (!messages.length) return false;
    const filtered = this.filterReplayedMessages(sessionId, messages);
    return filtered.length > 0;
  }

  private readStaleSweepState(): StaleSweepState {
    try {
      const installedAt = this.readInstalledAt();
      if (!fs.existsSync(this.staleSweepStatePath)) return { installedAt };
      const parsed = JSON.parse(fs.readFileSync(this.staleSweepStatePath, "utf8"));
      if (!parsed || typeof parsed !== "object") return { installedAt };
      const retriesRaw = parsed.retries && typeof parsed.retries === "object" ? parsed.retries : {};
      const retries: Record<string, StaleRetryState> = {};
      for (const [sid, value] of Object.entries(retriesRaw)) {
        const item = value as Partial<StaleRetryState>;
        const sessionId = String(item.sessionId || sid).trim();
        const lastActivityMs = Number(item.lastActivityMs);
        const attemptCount = Math.max(0, Number(item.attemptCount || 0));
        if (!sessionId || !Number.isFinite(lastActivityMs) || lastActivityMs <= 0) continue;
        retries[sessionId] = {
          sessionId,
          lastActivityMs,
          attemptCount,
          nextRecoveryAt: item.nextRecoveryAt,
          lastError: item.lastError,
        };
      }
      return {
        installedAt: typeof parsed.installedAt === "string" ? parsed.installedAt : installedAt,
        lastSweepAt: typeof parsed.lastSweepAt === "string" ? parsed.lastSweepAt : undefined,
        retries,
      };
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed reading stale sweep state: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return { installedAt: this.readInstalledAt() };
    }
  }

  private writeStaleSweepState(state: StaleSweepState): void {
    try {
      fs.mkdirSync(path.dirname(this.staleSweepStatePath), { recursive: true });
      const installedAt = state.installedAt || this.readInstalledAt();
      fs.writeFileSync(this.staleSweepStatePath, JSON.stringify({ ...state, installedAt }), { mode: 0o600 });
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed writing stale sweep state: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
  }

  private readInstalledAt(): string {
    try {
      if (fs.existsSync(this.installStatePath)) {
        const raw = JSON.parse(fs.readFileSync(this.installStatePath, "utf8"));
        const installedAt = String(raw?.installedAt || "").trim();
        if (installedAt) {
          return installedAt;
        }
      }
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed reading installed-at state: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
    const installedAt = new Date().toISOString();
    try {
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
      fs.writeFileSync(this.installStatePath, JSON.stringify({ installedAt }), { mode: 0o600 });
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed writing installed-at state: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
    return installedAt;
  }

  private writeQuaidLog(event: string, sessionId?: string, data?: Record<string, unknown>): void {
    const now = new Date().toISOString();
    const safeSessionId = sessionId ? String(sessionId) : "";
    const line = `${now} event=${event}${safeSessionId ? ` session=${safeSessionId}` : ""}${data ? ` data=${JSON.stringify(data)}` : ""}\n`;
    try {
      fs.mkdirSync(path.dirname(this.logFilePath), { recursive: true });
      fs.appendFileSync(this.logFilePath, line, "utf8");
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed writing timeout log file ${this.logFilePath}: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }

    const payload = { ts: now, event, session_id: safeSessionId || undefined, ...data };
    try {
      fs.mkdirSync(path.dirname(this.eventFilePath), { recursive: true });
      fs.appendFileSync(this.eventFilePath, `${JSON.stringify(payload)}\n`, "utf8");
    } catch (err: unknown) {
      safeLog(this.logger, `[memory][timeout] failed writing timeout event log ${this.eventFilePath}: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }

    if (safeSessionId) {
      const safeName = safeSessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
      const sessionPath = path.join(this.sessionLogDir, `${safeName}.jsonl`);
      try {
        fs.mkdirSync(this.sessionLogDir, { recursive: true });
        fs.appendFileSync(sessionPath, `${JSON.stringify(payload)}\n`, "utf8");
      } catch (err: unknown) {
        safeLog(this.logger, `[memory][timeout] failed writing timeout session log ${sessionPath}: ${String((err as Error)?.message || err)}`);
        if (this.failHard) {
          throw err;
        }
      }
    }
  }
}
