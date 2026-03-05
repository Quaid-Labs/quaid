import * as fs from "node:fs";
import * as path from "node:path";

type SessionCursorPayload = {
  sessionId: string;
  clearedAt: string;
  lastMessageKey?: string;
  lastTimestampMs?: number;
};

type PendingExtractionSignal = {
  sessionId: string;
  label: string;
  queuedAt: string;
  attemptCount?: number;
  meta?: Record<string, any>;
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

function signalPriority(label: string): number {
  const normalized = String(label || "").trim().toLowerCase();
  if (normalized === "resetsignal" || normalized === "reset") return 3;
  if (normalized === "compactionsignal" || normalized === "compaction") return 2;
  return 1;
}

type TimeoutExtractor = (messages: any[], sessionId?: string, label?: string) => Promise<void>;
type TimeoutLogger = (message: string) => void;

type SessionTimeoutManagerOptions = {
  workspace: string;
  timeoutMinutes: number;
  extract: TimeoutExtractor;
  isBootstrapOnly: (messages: any[]) => boolean;
  logger?: TimeoutLogger;
  readSessionMessages?: (sessionId: string) => any[];
  listSessionActivity?: () => SessionActivityRecord[];
  shouldSkipText?: (text: string) => boolean;
};
type AgentEndMeta = {
  source?: string;
};

type FailHardCacheEntry = {
  value: boolean;
  mtimeMs: number;
  checkedAtMs: number;
};

const FAIL_HARD_CACHE_MS = 5000;
const failHardCache = new Map<string, FailHardCacheEntry>();

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

function isFailHardEnabled(workspace: string): boolean {
  const now = Date.now();
  const cached = failHardCache.get(workspace);
  if (cached && (now - cached.checkedAtMs) < FAIL_HARD_CACHE_MS) {
    return cached.value;
  }

  const configPath = path.join(workspace, "config", "memory.json");
  let mtimeMs = -1;
  try {
    mtimeMs = fs.statSync(configPath).mtimeMs;
  } catch {}

  if (cached && cached.mtimeMs === mtimeMs) {
    cached.checkedAtMs = now;
    failHardCache.set(workspace, cached);
    return cached.value;
  }

  let value = true;
  try {
    const raw = JSON.parse(fs.readFileSync(configPath, "utf8"));
    const retrieval = raw?.retrieval || {};
    if (typeof retrieval.fail_hard === "boolean") value = retrieval.fail_hard;
    if (typeof retrieval.failHard === "boolean") value = retrieval.failHard;
  } catch (err: unknown) {
    const msg = String((err as Error)?.message || err || "");
    if (!msg.includes("ENOENT")) {
      console.warn(`[quaid][timeout] failed to read failHard config; defaulting to true: ${msg}`);
    }
  }
  failHardCache.set(workspace, { value, mtimeMs, checkedAtMs: now });
  return value;
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
    "rate each document",
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
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pendingFallbackMessages: any[] | null = null;
  private pendingSessionId: string | undefined;
  private sessionCursorDir: string;
  private pendingSignalDir: string;
  private workerLockPath: string;
  private workerLockToken: string;
  private staleSweepStatePath: string;
  private installStatePath: string;
  private ownsWorkerLock: boolean = false;
  private logDir: string;
  private sessionLogDir: string;
  private logFilePath: string;
  private eventFilePath: string;
  private workerTimer: ReturnType<typeof setInterval> | null = null;
  private chain: Promise<void> = Promise.resolve();
  private failHard: boolean;
  private extractTimeoutMs: number;
  private maxSignalRetries: number;
  private readonly staleRecoveryInitialBackoffMs = 5000;
  private readonly staleRecoveryMaxBackoffMs = 5 * 60 * 1000;

  constructor(opts: SessionTimeoutManagerOptions) {
    this.timeoutMinutes = opts.timeoutMinutes;
    this.extract = opts.extract;
    this.isBootstrapOnly = opts.isBootstrapOnly;
    this.logger = opts.logger;
    this.readSessionMessagesSource = (sessionId: string) => {
      try {
        return filterEligibleMessages(opts.readSessionMessages?.(sessionId) || [], opts.shouldSkipText);
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] source readSessionMessages failed for ${sessionId}: ${String((err as Error)?.message || err)}`);
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
        safeLog(this.logger, `[quaid][timeout] source listSessionActivity failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
        return [];
      }
    };

    this.logDir = path.join(opts.workspace, "logs", "quaid");
    this.sessionLogDir = path.join(this.logDir, "sessions");
    this.sessionCursorDir = path.join(opts.workspace, "data", "session-cursors");
    this.pendingSignalDir = path.join(opts.workspace, "data", "pending-extraction-signals");
    this.workerLockPath = path.join(opts.workspace, "data", "session-timeout-worker.lock");
    this.workerLockToken = `${process.pid}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
    this.staleSweepStatePath = path.join(opts.workspace, "data", "stale-sweep-state.json");
    this.installStatePath = path.join(opts.workspace, "data", "installed-at.json");
    this.logFilePath = path.join(this.logDir, "session-timeout.log");
    this.eventFilePath = path.join(this.logDir, "session-timeout-events.jsonl");
    this.failHard = isFailHardEnabled(opts.workspace);
    const configuredTimeoutMs = Number(process.env.QUAID_SESSION_EXTRACT_TIMEOUT_MS || "");
    this.extractTimeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0
      ? Math.floor(configuredTimeoutMs)
      : 600_000;
    const configuredSignalRetries = Number(process.env.QUAID_SIGNAL_MAX_RETRIES || "");
    this.maxSignalRetries = Number.isFinite(configuredSignalRetries) && configuredSignalRetries >= 0
      ? Math.floor(configuredSignalRetries)
      : 3;

    try {
      fs.mkdirSync(this.logDir, { recursive: true });
      fs.mkdirSync(this.sessionLogDir, { recursive: true });
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      fs.mkdirSync(path.dirname(this.staleSweepStatePath), { recursive: true });
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err || "unknown directory initialization error");
      safeLog(this.logger, `[quaid][timeout] failed to initialize runtime directories: ${msg}`);
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

  onAgentStart(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
      this.writeQuaidLog("timer_cleared", undefined, { reason: "agent_start" });
    }
  }

  onAgentEnd(messages: any[], sessionId: string, meta?: AgentEndMeta): void {
    if (!Array.isArray(messages) || messages.length === 0) return;
    if (!sessionId) return;
    const incoming = filterEligibleMessages(messages);
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
      safeLog(this.logger, `[quaid][timeout] skipping bootstrap-only transcript session=${sessionId} message_count=${gatedIncoming.length}; preserving prior timeout context`);
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

  private async extractSessionFromSourceDirect(sessionId: string, label: string, fallbackMessages?: any[]): Promise<boolean> {
    if (!sessionId) return false;

    const sourceMessages = this.readSourceSessionMessages(sessionId);
    const sourceUnprocessed = this.filterReplayedMessages(sessionId, sourceMessages);

    const fallback = this.filterReplayedMessages(sessionId, filterEligibleMessages(fallbackMessages || []));
    const allowFallback = !this.failHard;

    const source = sourceUnprocessed.length > 0
      ? "source_session_messages"
      : (allowFallback && fallback.length > 0 ? "fallback_event_messages" : "none");

    const messages = sourceUnprocessed.length > 0 ? sourceUnprocessed : (allowFallback ? fallback : []);
    if (!messages.length) {
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
        safeLog(this.logger, `[quaid][timeout] previous extraction chain error: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      })
      .then(async () => {
        extracted = await this.extractSessionFromSourceDirect(sessionId, label, fallbackMessages);
      })
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] extraction queue failed: ${String((err as Error)?.message || err)}`);
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

    for (const [sessionId, lastActivityMs] of candidates.entries()) {
      const messages = this.filterReplayedMessages(sessionId, this.readSourceSessionMessages(sessionId));
      if (!messages.length) {
        delete retries[sessionId];
        this.writeQuaidLog("recover_stale_buffer_skip_empty", sessionId, { last_activity_ms: lastActivityMs });
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

  private buildOriginHintMeta(meta?: Record<string, any>): Record<string, any> | undefined {
    if (meta && typeof meta === "object") return meta;
    const stack = String(new Error().stack || "");
    const lines = stack.split("\n").map((s) => s.trim()).filter(Boolean);
    const caller = lines.find((line) =>
      !line.includes("buildOriginHintMeta")
      && !line.includes("queueExtractionSignal")
      && !line.includes("SessionTimeoutManager.")
    );
    if (!caller) return undefined;
    return { origin_hint: caller.slice(0, 240) };
  }

  queueExtractionSignal(sessionId: string, label: string, meta?: Record<string, any>): void {
    if (!sessionId) return;
    const signalMeta = this.buildOriginHintMeta(meta);
    if (!this.hasUnprocessedSessionMessages(sessionId)) {
      this.writeQuaidLog("signal_queue_skipped_already_cleared", sessionId, {
        label: String(label || "Signal"),
        ...(signalMeta ? { meta: signalMeta } : {}),
      });
      return;
    }
    const signal: PendingExtractionSignal = {
      sessionId,
      label: String(label || "Signal"),
      queuedAt: new Date().toISOString(),
      attemptCount: 0,
      ...(signalMeta ? { meta: signalMeta } : {}),
    };
    try {
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      const signalPath = this.signalPath(sessionId);
      if (fs.existsSync(signalPath)) {
        let existingLabel = "Signal";
        let existingAttemptCount = 0;
        try {
          const existing = JSON.parse(fs.readFileSync(signalPath, "utf8")) as PendingExtractionSignal;
          existingLabel = String(existing?.label || "Signal");
          existingAttemptCount = Math.max(0, Number(existing?.attemptCount || 0));
        } catch (err: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to parse existing extraction signal ${signalPath}: ${String((err as Error)?.message || err)}`);
        }
        const incomingPriority = signalPriority(signal.label);
        const existingPriority = signalPriority(existingLabel);
        if (incomingPriority > existingPriority) {
          signal.attemptCount = 0;
          fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
          this.writeQuaidLog("signal_file_write", sessionId, {
            op: "promote_overwrite",
            path: signalPath,
            label: signal.label,
            has_meta: Boolean(signalMeta),
          });
          this.writeQuaidLog("signal_queue_promoted", sessionId, {
            from: existingLabel,
            to: signal.label,
            previous_attempt_count: existingAttemptCount,
            ...(signalMeta ? { meta: signalMeta } : {}),
          });
        } else {
          this.writeQuaidLog("signal_queue_coalesced", sessionId, {
            label: signal.label,
            existing_label: existingLabel,
            reason: "already_pending",
            ...(signalMeta ? { meta: signalMeta } : {}),
          });
        }
        this.triggerWorkerTick();
        return;
      }
      fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
      this.writeQuaidLog("signal_file_write", sessionId, {
        op: "create",
        path: signalPath,
        label: signal.label,
        has_meta: Boolean(signalMeta),
      });
      this.writeQuaidLog("signal_queued", sessionId, {
        label: signal.label,
        ...(signalMeta ? { meta: signalMeta } : {}),
      });
      this.triggerWorkerTick();
    } catch (err: unknown) {
      this.writeQuaidLog("signal_queue_error", sessionId, {
        label: signal.label,
        ...(signalMeta ? { meta: signalMeta } : {}),
        error: String((err as Error)?.message || err),
      });
    }
  }

  async processPendingExtractionSignals(): Promise<void> {
    this.recoverOrphanedSignalClaims();
    for (const filePath of this.listSignalFiles()) {
      const lockedPath = this.claimSignalFile(filePath);
      if (!lockedPath) { continue; }
      this.writeQuaidLog("signal_file_claimed", path.basename(filePath, ".json"), {
        from: filePath,
        to: lockedPath,
      });
      let signal: PendingExtractionSignal | null = null;
      try {
        signal = JSON.parse(fs.readFileSync(lockedPath, "utf8")) as PendingExtractionSignal;
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] dropping malformed extraction signal ${lockedPath}: ${String((err as Error)?.message || err)}`);
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to delete malformed extraction signal ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
        }
        continue;
      }
      const sessionId = String(signal?.sessionId || path.basename(filePath, ".json")).trim();
      const label = String(signal?.label || "Signal");
      const attemptCount = Math.max(0, Number(signal?.attemptCount || 0));
      const meta = signal?.meta && typeof signal.meta === "object" ? signal.meta : undefined;
      if (!sessionId) {
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to delete signal without session id ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
        }
        continue;
      }
      let restoredClaim = false;
      try {
        this.writeQuaidLog("signal_process_begin", sessionId, {
          label,
          ...(meta ? { meta } : {}),
        });
        await this.extractSessionFromSourceDirect(sessionId, label);
        this.writeQuaidLog("signal_process_done", sessionId, {
          label,
          ...(meta ? { meta } : {}),
        });
      } catch (err: unknown) {
        this.writeQuaidLog("signal_process_error", sessionId, {
          label,
          ...(meta ? { meta } : {}),
          error: String((err as Error)?.message || err),
        });
        const nextAttemptCount = attemptCount + 1;
        const canRetry = nextAttemptCount <= this.maxSignalRetries;
        try {
          const originalPath = filePath;
          if (canRetry && !fs.existsSync(originalPath) && fs.existsSync(lockedPath)) {
            const nextSignal: PendingExtractionSignal = {
              sessionId,
              label,
              queuedAt: String(signal?.queuedAt || new Date().toISOString()),
              attemptCount: nextAttemptCount,
              ...(meta ? { meta } : {}),
            };
            fs.writeFileSync(lockedPath, JSON.stringify(nextSignal), { mode: 0o600 });
            fs.renameSync(lockedPath, originalPath);
            restoredClaim = true;
            this.writeQuaidLog("signal_file_write", sessionId, {
              op: "retry_requeue",
              path: originalPath,
              label,
              has_meta: Boolean(meta),
            });
            this.writeQuaidLog("signal_process_requeued", sessionId, {
              label,
              attempt_count: nextAttemptCount,
              max_retries: this.maxSignalRetries,
              ...(meta ? { meta } : {}),
            });
          } else if (!canRetry) {
            this.writeQuaidLog("signal_process_dropped", sessionId, {
              label,
              attempt_count: nextAttemptCount,
              max_retries: this.maxSignalRetries,
              reason: "max_retries_exceeded",
              ...(meta ? { meta } : {}),
            });
          }
        } catch (restoreErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed restoring signal claim ${lockedPath}: ${String((restoreErr as Error)?.message || restoreErr)}`);
          if ((restoreErr as NodeJS.ErrnoException)?.code !== "ENOENT") {
            throw restoreErr;
          }
        }
        if (this.failHard) {
          throw err;
        }
      } finally {
        try {
          if (!restoredClaim && fs.existsSync(lockedPath)) {
            fs.unlinkSync(lockedPath);
          }
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed cleaning claimed signal ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
          if (this.failHard && (unlinkErr as NodeJS.ErrnoException)?.code !== "ENOENT") {
            throw unlinkErr;
          }
        }
      }
    }
  }

  startWorker(heartbeatSeconds: number = 30): boolean {
    this.stopWorker();
    if (!this.tryAcquireWorkerLock()) {
      this.writeQuaidLog("worker_start_skipped", undefined, {
        reason: "lock_held",
        lock_path: this.workerLockPath,
      });
      return false;
    }
    const sec = Number.isFinite(heartbeatSeconds) ? Math.max(5, Math.floor(heartbeatSeconds)) : 30;
    this.workerTimer = setInterval(() => this.triggerWorkerTick(), sec * 1000);
    if (typeof (this.workerTimer as any).unref === "function") {
      (this.workerTimer as any).unref();
    }
    this.writeQuaidLog("worker_started", undefined, {
      heartbeat_seconds: sec,
      lock_path: this.workerLockPath,
      leader_pid: process.pid,
    });
    this.triggerWorkerTick();
    return true;
  }

  stopWorker(): void {
    const hadTimer = Boolean(this.workerTimer);
    if (this.workerTimer) {
      clearInterval(this.workerTimer);
      this.workerTimer = null;
    }
    const released = this.releaseWorkerLock();
    if (hadTimer || released) {
      this.writeQuaidLog("worker_stopped");
    }
  }

  private isPidAlive(pid: number): boolean {
    if (!Number.isFinite(pid) || pid <= 0) return false;
    try {
      process.kill(pid, 0);
      return true;
    } catch {
      return false;
    }
  }

  private tryAcquireWorkerLock(): boolean {
    if (this.ownsWorkerLock) return true;
    try {
      fs.mkdirSync(path.dirname(this.workerLockPath), { recursive: true });
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed to initialize worker lock directory: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
      return false;
    }

    const payload = {
      pid: process.pid,
      token: this.workerLockToken,
      started_at: new Date().toISOString(),
    };

    try {
      fs.writeFileSync(this.workerLockPath, JSON.stringify(payload), { mode: 0o600, flag: "wx" });
      this.ownsWorkerLock = true;
      return true;
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code !== "EEXIST") return false;
    }

    try {
      const raw = fs.readFileSync(this.workerLockPath, "utf8");
      const existing = JSON.parse(raw) as { pid?: number; token?: string; started_at?: string };
      const existingPid = Number(existing?.pid || 0);
      if (this.isPidAlive(existingPid)) return false;
      const verifyRaw = fs.readFileSync(this.workerLockPath, "utf8");
      const verify = JSON.parse(verifyRaw) as { pid?: number; token?: string };
      if (
        Number(verify?.pid || 0) !== existingPid
        || String(verify?.token || "") !== String(existing?.token || "")
      ) {
        return false;
      }
      try {
        fs.unlinkSync(this.workerLockPath);
      } catch (unlinkErr: unknown) {
        safeLog(this.logger, `[quaid][timeout] failed removing stale worker lock ${this.workerLockPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
        if (this.failHard && (unlinkErr as NodeJS.ErrnoException)?.code !== "ENOENT") {
          throw unlinkErr;
        }
      }
      fs.writeFileSync(this.workerLockPath, JSON.stringify(payload), { mode: 0o600, flag: "wx" });
      this.ownsWorkerLock = true;
      this.writeQuaidLog("worker_lock_stale_recovered", undefined, { stale_pid: existingPid || undefined });
      return true;
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed stale-worker lock recovery: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
      return false;
    }
  }

  private releaseWorkerLock(): boolean {
    if (!this.ownsWorkerLock) return false;
    try {
      const raw = fs.readFileSync(this.workerLockPath, "utf8");
      const existing = JSON.parse(raw) as { token?: string };
      if (existing?.token && existing.token !== this.workerLockToken) {
        this.ownsWorkerLock = false;
        return false;
      }
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed validating worker lock before release: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
    try {
      fs.unlinkSync(this.workerLockPath);
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed releasing worker lock ${this.workerLockPath}: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
    this.ownsWorkerLock = false;
    return true;
  }

  private queueExtractionFromSession(sessionId: string, fallbackMessages: any[], timeoutMinutes: number): void {
    this.chain = this.chain
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] previous extraction chain error: ${String((err as Error)?.message || err)}`);
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
        safeLog(this.logger, `[quaid][timeout] extraction queue failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      });
  }

  private triggerWorkerTick(): void {
    this.chain = this.chain
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] previous worker chain error: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      })
      .then(async () => {
        await this.processPendingExtractionSignals();
        await this.recoverStaleBuffers();
      })
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] worker tick failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      });
  }

  private signalPath(sessionId: string): string {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.pendingSignalDir, `${safeSessionId}.json`);
  }

  private listSignalFiles(): string[] {
    try {
      if (!fs.existsSync(this.pendingSignalDir)) return [];
      return fs.readdirSync(this.pendingSignalDir)
        .filter((f) => f.endsWith(".json"))
        .map((f) => path.join(this.pendingSignalDir, f));
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed listing signal files: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }

  private listSignalClaimFiles(): string[] {
    try {
      if (!fs.existsSync(this.pendingSignalDir)) return [];
      return fs.readdirSync(this.pendingSignalDir)
        .filter((f) => /\.json\.processing\.\d+$/.test(f))
        .map((f) => path.join(this.pendingSignalDir, f));
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed listing signal claim files: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }

  private recoverOrphanedSignalClaims(): void {
    for (const lockedPath of this.listSignalClaimFiles()) {
      const m = lockedPath.match(/^(.*\.json)\.processing\.(\d+)$/);
      if (!m) continue;
      const originalPath = m[1];
      const ownerPid = Number(m[2]);
      if (this.isPidAlive(ownerPid)) continue;
      try {
        if (fs.existsSync(originalPath)) {
          fs.unlinkSync(lockedPath);
          this.writeQuaidLog("signal_file_recover_skip", path.basename(originalPath, ".json"), {
            reason: "original_exists",
            orphan: lockedPath,
          });
          continue;
        }
        // Preserve the queued work, but mark it as orphan recovery so downstream
        // notification logic can suppress user-facing noise.
        try {
          const raw = JSON.parse(fs.readFileSync(lockedPath, "utf8")) as PendingExtractionSignal;
          if (raw && typeof raw === "object") {
            const priorLabel = String(raw.label || "Signal");
            const priorMeta = raw.meta && typeof raw.meta === "object" ? raw.meta : {};
            const patched: PendingExtractionSignal = {
              sessionId: String(raw.sessionId || path.basename(originalPath, ".json")),
              label: priorLabel.toLowerCase().includes("recover") ? priorLabel : "RecoverySignal",
              queuedAt: String(raw.queuedAt || new Date().toISOString()),
              attemptCount: Math.max(0, Number(raw.attemptCount || 0)),
              meta: {
                ...priorMeta,
                source: "orphan_recovery",
                recovered_orphan: true,
                original_label: priorLabel,
              },
            };
            fs.writeFileSync(lockedPath, JSON.stringify(patched), { mode: 0o600 });
          }
        } catch {}
        fs.renameSync(lockedPath, originalPath);
        this.writeQuaidLog("signal_file_recovered", path.basename(originalPath, ".json"), {
          from: lockedPath,
          to: originalPath,
        });
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] failed recovering orphaned signal claim ${lockedPath}: ${String((err as Error)?.message || err)}`);
        if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
          throw err;
        }
      }
    }
  }

  private claimSignalFile(filePath: string): string | null {
    const lockedPath = `${filePath}.processing.${process.pid}`;
    try {
      fs.renameSync(filePath, lockedPath);
      return lockedPath;
    } catch {
      return null;
    }
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
      safeLog(this.logger, `[quaid][timeout] failed reading session cursor for ${sessionId}: ${String((err as Error)?.message || err)}`);
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
    return filterEligibleMessages(rows);
  }

  private listSessionActivityRows(): SessionActivityRecord[] {
    return this.listSessionActivitySource();
  }

  private hasUnprocessedSessionMessages(sessionId: string): boolean {
    if (this.pendingSessionId === sessionId && Array.isArray(this.pendingFallbackMessages)) {
      const pending = this.filterReplayedMessages(sessionId, filterEligibleMessages(this.pendingFallbackMessages));
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
      safeLog(this.logger, `[quaid][timeout] failed reading stale sweep state: ${String((err as Error)?.message || err)}`);
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
      safeLog(this.logger, `[quaid][timeout] failed writing stale sweep state: ${String((err as Error)?.message || err)}`);
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
      safeLog(this.logger, `[quaid][timeout] failed reading installed-at state: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
    const installedAt = new Date().toISOString();
    try {
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
      fs.writeFileSync(this.installStatePath, JSON.stringify({ installedAt }), { mode: 0o600 });
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed writing installed-at state: ${String((err as Error)?.message || err)}`);
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
      safeLog(this.logger, `[quaid][timeout] failed writing timeout log file ${this.logFilePath}: ${String((err as Error)?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }

    const payload = { ts: now, event, session_id: safeSessionId || undefined, ...data };
    try {
      fs.mkdirSync(path.dirname(this.eventFilePath), { recursive: true });
      fs.appendFileSync(this.eventFilePath, `${JSON.stringify(payload)}\n`, "utf8");
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed writing timeout event log ${this.eventFilePath}: ${String((err as Error)?.message || err)}`);
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
        safeLog(this.logger, `[quaid][timeout] failed writing timeout session log ${sessionPath}: ${String((err as Error)?.message || err)}`);
        if (this.failHard) {
          throw err;
        }
      }
    }
  }
}
