import * as fs from "node:fs";
import * as path from "node:path";

type TimeoutBufferPayload = {
  sessionId: string;
  updatedAt: string;
  messages: any[];
};

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

function isEligibleConversationMessage(msg: any): boolean {
  if (!msg || (msg.role !== "user" && msg.role !== "assistant")) return false;
  const text = messageText(msg).trim();
  if (!text) return false;
  const lower = text.toLowerCase();
  if (lower.startsWith("gatewayrestart:") || lower.startsWith("system:")) return false;
  if (isInternalMaintenancePrompt(text)) return false;
  if (msg.role === "assistant" && isExtractionJsonAssistantPayload(text)) return false;
  return true;
}

function filterEligibleMessages(messages: any[]): any[] {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg: any) => isEligibleConversationMessage(msg));
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
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pendingMessages: any[] | null = null;
  private pendingSessionId: string | undefined;
  private buffers = new Map<string, any[]>();
  private bufferTouchedAt = new Map<string, number>();
  private bufferDir: string;
  private logDir: string;
  private sessionLogDir: string;
  private sessionMessageLogDir: string;
  private sessionCursorDir: string;
  private pendingSignalDir: string;
  private workerLockPath: string;
  private workerLockToken: string;
  private ownsWorkerLock: boolean = false;
  private logFilePath: string;
  private eventFilePath: string;
  private workerTimer: ReturnType<typeof setInterval> | null = null;
  private chain: Promise<void> = Promise.resolve();
  private failHard: boolean;
  private readonly maxInMemoryBuffers = 200;

  constructor(opts: SessionTimeoutManagerOptions) {
    this.timeoutMinutes = opts.timeoutMinutes;
    this.extract = opts.extract;
    this.isBootstrapOnly = opts.isBootstrapOnly;
    this.logger = opts.logger;
    this.bufferDir = path.join(opts.workspace, "data", "timeout-buffers");
    this.logDir = path.join(opts.workspace, "logs", "quaid");
    this.sessionLogDir = path.join(this.logDir, "sessions");
    this.sessionMessageLogDir = path.join(this.logDir, "session-messages");
    this.sessionCursorDir = path.join(opts.workspace, "data", "session-cursors");
    this.pendingSignalDir = path.join(opts.workspace, "data", "pending-extraction-signals");
    this.workerLockPath = path.join(opts.workspace, "data", "session-timeout-worker.lock");
    this.workerLockToken = `${process.pid}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
    this.logFilePath = path.join(this.logDir, "session-timeout.log");
    this.eventFilePath = path.join(this.logDir, "session-timeout-events.jsonl");
    this.failHard = isFailHardEnabled(opts.workspace);
    try {
      fs.mkdirSync(this.logDir, { recursive: true });
      fs.mkdirSync(this.sessionLogDir, { recursive: true });
      fs.mkdirSync(this.sessionMessageLogDir, { recursive: true });
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err || "unknown directory initialization error");
      safeLog(this.logger, `[quaid][timeout] failed to initialize runtime directories: ${msg}`);
      if (this.failHard) {
        throw err;
      }
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

  onAgentEnd(messages: any[], sessionId: string): void {
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

    const inMemory = this.buffers.get(sessionId) || [];
    const onDisk = this.readBuffer(sessionId);
    const existing = mergeUniqueMessages(onDisk, inMemory);
    const merged = mergeUniqueMessages(existing, gatedIncoming);
    const added = merged.slice(existing.length);
    this.buffers.set(sessionId, merged);
    this.bufferTouchedAt.set(sessionId, Date.now());
    this.evictInMemoryBuffersIfNeeded(sessionId);
    this.writeBuffer(sessionId, merged);
    this.appendSessionMessages(sessionId, added);

    this.pendingMessages = merged;
    this.pendingSessionId = sessionId;
    safeLog(this.logger, `[quaid][timeout] buffered session=${sessionId} appended=${Math.max(0, merged.length - existing.length)} total=${merged.length}`);
    this.writeQuaidLog("buffered", sessionId, {
      appended: added.length,
      total: merged.length,
      timeout_minutes: this.timeoutMinutes,
    });

    if (this.timeoutMinutes <= 0) return;

    this.timer = setTimeout(() => {
      const msgs = this.pendingMessages;
      const sid = this.pendingSessionId;
      this.timer = null;
      this.pendingMessages = null;
      this.pendingSessionId = undefined;
      if (!msgs || !sid || msgs.length === 0) return;
      const loggedMessages = this.readSessionMessages(sid);
      const extractionMessages = loggedMessages.length > 0 ? loggedMessages : msgs;
      this.writeQuaidLog("timer_fired", sid, {
        message_count: extractionMessages.length,
        timeout_minutes: this.timeoutMinutes,
        source: loggedMessages.length > 0 ? "session_message_log" : "pending_buffer",
      });
      this.queueExtraction(extractionMessages, sid, this.timeoutMinutes);
    }, this.timeoutMinutes * 60 * 1000);
  }

  private async extractSessionFromLogDirect(sessionId: string, label: string, fallbackMessages?: any[]): Promise<boolean> {
    if (!sessionId) return false;
    const loggedMessages = this.readSessionMessages(sessionId);
    const fallback = filterEligibleMessages(fallbackMessages || []);
    const allowFallback = !this.failHard;
    const source = loggedMessages.length > 0
      ? "session_message_log"
      : (allowFallback && fallback.length > 0 ? "fallback_event_messages" : "none");
    const messages = loggedMessages.length > 0 ? loggedMessages : (allowFallback ? fallback : []);
    if (!messages.length) {
      if (this.failHard && fallback.length > 0) {
        const msg = "session-timeout fallback payload blocked by failHard; no session_message_log available";
        this.writeQuaidLog("extract_fail_hard_blocked_fallback", sessionId, { label, fallback_count: fallback.length });
        throw new Error(msg);
      }
      this.writeQuaidLog("extract_skip_empty", sessionId, { label, source });
      return false;
    }

    this.writeQuaidLog("extract_begin", sessionId, { label, message_count: messages.length, source });
    await this.extract(messages, sessionId, label);
    this.writeQuaidLog("extract_done", sessionId, { label, message_count: messages.length, source });
    this.clearSession(sessionId);
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
        extracted = await this.extractSessionFromLogDirect(sessionId, label, fallbackMessages);
      })
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] extraction queue failed: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      });
    this.chain = work.then(() => undefined, () => undefined);
    await work;
    return extracted;
  }

  clearSession(sessionId?: string): void {
    if (!sessionId) return;
    const loggedMessages = this.readSessionMessages(sessionId);
    const bufferedMessages = this.buffers.get(sessionId) || this.readBuffer(sessionId);
    const cursorMessages = loggedMessages.length > 0 ? loggedMessages : bufferedMessages;
    this.writeSessionCursor(sessionId, cursorMessages);
    this.buffers.delete(sessionId);
    this.bufferTouchedAt.delete(sessionId);
    this.clearBuffer(sessionId);
    this.clearSessionMessageLog(sessionId);
    this.writeQuaidLog("session_cleared", sessionId);
    if (this.pendingSessionId === sessionId) {
      this.pendingMessages = null;
      this.pendingSessionId = undefined;
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
        this.writeQuaidLog("timer_cleared", sessionId, { reason: "session_cleared" });
      }
    }
  }

  private evictInMemoryBuffersIfNeeded(currentSessionId: string): void {
    while (this.buffers.size > this.maxInMemoryBuffers) {
      const oldestSession = Array.from(this.bufferTouchedAt.entries())
        .sort((a, b) => a[1] - b[1])
        .find(([sid]) => sid !== currentSessionId && sid !== this.pendingSessionId)?.[0];
      if (!oldestSession) {
        break;
      }
      this.buffers.delete(oldestSession);
      this.bufferTouchedAt.delete(oldestSession);
      this.writeQuaidLog("buffer_evicted", oldestSession, {
        reason: "in_memory_buffer_limit",
        limit: this.maxInMemoryBuffers,
      });
    }
  }

  async recoverStaleBuffers(): Promise<void> {
    if (this.timeoutMinutes <= 0) return;
    this.recoverOrphanedBufferClaims();
    const now = Date.now();
    const staleMs = this.timeoutMinutes * 60 * 1000;
    for (const filePath of this.listBufferFiles()) {
      const lockedPath = this.claimBufferFile(filePath);
      if (!lockedPath) { continue; }
      try {
        const payload = JSON.parse(fs.readFileSync(lockedPath, "utf8")) as TimeoutBufferPayload;
        const sid = String(payload?.sessionId || path.basename(filePath, ".json")).trim();
        const updatedAtMs = Date.parse(String(payload?.updatedAt || ""));
        const msgs = filterEligibleMessages(Array.isArray(payload?.messages) ? payload.messages : []);
        if (!sid || msgs.length === 0) {
          try {
            fs.unlinkSync(lockedPath);
          } catch (unlinkErr: unknown) {
            safeLog(this.logger, `[quaid][timeout] failed to delete invalid stale buffer claim ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
          }
          continue;
        }
        if (Number.isFinite(updatedAtMs) && now - updatedAtMs < staleMs) {
          this.releaseBufferFile(lockedPath, filePath);
          continue;
        }

        const loggedMessages = this.readSessionMessages(sid);
        const extractionMessages = loggedMessages.length > 0 ? loggedMessages : msgs;
        safeLog(this.logger, `[quaid][timeout] recovering stale buffer session=${sid} message_count=${extractionMessages.length}`);
        this.writeQuaidLog("recover_stale_buffer", sid, {
          message_count: extractionMessages.length,
          source: loggedMessages.length > 0 ? "session_message_log" : "pending_buffer",
        });
        await this.extract(extractionMessages, sid);
        this.clearSession(sid);
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to delete processed stale buffer claim ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
        }
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] stale buffer recovery failed for ${filePath}: ${String((err as Error)?.message || err)}`);
        this.releaseBufferFile(lockedPath, filePath);
      }
    }
  }

  queueExtractionSignal(sessionId: string, label: string): void {
    if (!sessionId) return;
    const signal: PendingExtractionSignal = {
      sessionId,
      label: String(label || "Signal"),
      queuedAt: new Date().toISOString(),
    };
    try {
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      const signalPath = this.signalPath(sessionId);
      if (fs.existsSync(signalPath)) {
        let existingLabel = "Signal";
        try {
          const existing = JSON.parse(fs.readFileSync(signalPath, "utf8")) as PendingExtractionSignal;
          existingLabel = String(existing?.label || "Signal");
        } catch (err: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to parse existing extraction signal ${signalPath}: ${String((err as Error)?.message || err)}`);
        }
        const incomingPriority = signalPriority(signal.label);
        const existingPriority = signalPriority(existingLabel);
        if (incomingPriority > existingPriority) {
          fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
          this.writeQuaidLog("signal_queue_promoted", sessionId, {
            from: existingLabel,
            to: signal.label,
          });
        } else {
          this.writeQuaidLog("signal_queue_coalesced", sessionId, {
            label: signal.label,
            existing_label: existingLabel,
            reason: "already_pending",
          });
        }
        this.triggerWorkerTick();
        return;
      }
      fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
      this.writeQuaidLog("signal_queued", sessionId, { label: signal.label });
      this.triggerWorkerTick();
    } catch (err: unknown) {
      this.writeQuaidLog("signal_queue_error", sessionId, { label: signal.label, error: String((err as Error)?.message || err) });
    }
  }

  async processPendingExtractionSignals(): Promise<void> {
    this.recoverOrphanedSignalClaims();
    for (const filePath of this.listSignalFiles()) {
      const lockedPath = this.claimSignalFile(filePath);
      if (!lockedPath) { continue; }
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
      if (!sessionId) {
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed to delete signal without session id ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
        }
        continue;
      }
      try {
        this.writeQuaidLog("signal_process_begin", sessionId, { label });
        await this.extractSessionFromLogDirect(sessionId, label);
        this.writeQuaidLog("signal_process_done", sessionId, { label });
      } catch (err: unknown) {
        this.writeQuaidLog("signal_process_error", sessionId, { label, error: String((err as Error)?.message || err) });
        if (this.failHard) {
          try {
            const originalPath = filePath;
            if (!fs.existsSync(originalPath) && fs.existsSync(lockedPath)) {
              fs.renameSync(lockedPath, originalPath);
            }
          } catch (restoreErr: unknown) {
            safeLog(this.logger, `[quaid][timeout] failed restoring signal claim ${lockedPath}: ${String((restoreErr as Error)?.message || restoreErr)}`);
            if ((restoreErr as NodeJS.ErrnoException)?.code !== "ENOENT") {
              throw restoreErr;
            }
          }
          throw err;
        }
      } finally {
        try {
          if (fs.existsSync(lockedPath)) {
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
      try { fs.unlinkSync(this.workerLockPath); } catch {}
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

  private queueExtraction(messages: any[], sessionId: string, timeoutMinutes: number): void {
    this.chain = this.chain
      .catch((err: unknown) => {
        safeLog(this.logger, `[quaid][timeout] previous extraction chain error: ${String((err as Error)?.message || err)}`);
        if (this.failHard) throw err;
      })
      .then(async () => {
        safeLog(this.logger, `[quaid] Inactivity timeout (${timeoutMinutes}m) — extracting ${messages.length} messages`);
        this.writeQuaidLog("extract_begin", sessionId, { message_count: messages.length, timeout_minutes: timeoutMinutes });
        await this.extract(messages, sessionId, "Timeout");
        this.writeQuaidLog("extract_done", sessionId, { message_count: messages.length });
        this.clearSession(sessionId);
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
          continue;
        }
        fs.renameSync(lockedPath, originalPath);
      } catch {}
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

  private bufferPath(sessionId: string): string {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.bufferDir, `${safeSessionId}.json`);
  }

  private readBuffer(sessionId: string): any[] {
    try {
      const payload = JSON.parse(fs.readFileSync(this.bufferPath(sessionId), "utf8")) as TimeoutBufferPayload;
      if (Array.isArray(payload?.messages)) return payload.messages;
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed reading buffer session=${sessionId}: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
    return [];
  }

  private writeBuffer(sessionId: string, messages: any[]): void {
    try {
      fs.mkdirSync(this.bufferDir, { recursive: true });
      const payload: TimeoutBufferPayload = {
        sessionId,
        updatedAt: new Date().toISOString(),
        messages,
      };
      fs.writeFileSync(this.bufferPath(sessionId), JSON.stringify(payload), { mode: 0o600 });
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed to persist buffer session=${sessionId}: ${String((err as Error)?.message || err)}`);
      this.writeQuaidLog("buffer_persist_error", sessionId, { error: String((err as Error)?.message || err) });
      if (this.failHard) {
        throw err;
      }
    }
  }

  private clearBuffer(sessionId: string): void {
    try { fs.unlinkSync(this.bufferPath(sessionId)); } catch {}
  }

  private listBufferFiles(): string[] {
    try {
      if (!fs.existsSync(this.bufferDir)) return [];
      return fs.readdirSync(this.bufferDir)
        .filter((f) => f.endsWith(".json"))
        .map((f) => path.join(this.bufferDir, f));
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed listing buffer files: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }

  private listBufferClaimFiles(): string[] {
    try {
      if (!fs.existsSync(this.bufferDir)) return [];
      return fs.readdirSync(this.bufferDir)
        .filter((f) => /\.json\.processing\.\d+$/.test(f))
        .map((f) => path.join(this.bufferDir, f));
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed listing buffer claim files: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }

  private recoverOrphanedBufferClaims(): void {
    for (const lockedPath of this.listBufferClaimFiles()) {
      const m = lockedPath.match(/^(.*\.json)\.processing\.(\d+)$/);
      if (!m) continue;
      const originalPath = m[1];
      const ownerPid = Number(m[2]);
      if (this.isPidAlive(ownerPid)) continue;
      try {
        if (fs.existsSync(originalPath)) {
          fs.unlinkSync(lockedPath);
          continue;
        }
        fs.renameSync(lockedPath, originalPath);
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] failed recovering orphaned buffer claim ${lockedPath}: ${String((err as Error)?.message || err)}`);
        if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
          throw err;
        }
      }
    }
  }

  private claimBufferFile(filePath: string): string | null {
    const lockedPath = `${filePath}.processing.${process.pid}`;
    try {
      fs.renameSync(filePath, lockedPath);
      return lockedPath;
    } catch {
      return null;
    }
  }

  private releaseBufferFile(lockedPath: string, originalPath: string): void {
    try {
      if (!fs.existsSync(lockedPath)) return;
      if (fs.existsSync(originalPath)) {
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr: unknown) {
          safeLog(this.logger, `[quaid][timeout] failed removing buffer claim ${lockedPath}: ${String((unlinkErr as Error)?.message || unlinkErr)}`);
          if (this.failHard && (unlinkErr as NodeJS.ErrnoException)?.code !== "ENOENT") {
            throw unlinkErr;
          }
        }
        return;
      }
      fs.renameSync(lockedPath, originalPath);
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed releasing buffer claim ${lockedPath}: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
    }
  }

  private sessionMessagePath(sessionId: string): string {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.sessionMessageLogDir, `${safeSessionId}.jsonl`);
  }

  private appendSessionMessages(sessionId: string, messages: any[]): void {
    const sanitized = filterEligibleMessages(messages);
    if (!sanitized.length) { return; }
    try {
      fs.mkdirSync(this.sessionMessageLogDir, { recursive: true });
      const fp = this.sessionMessagePath(sessionId);
      const lines = sanitized.map((m) => JSON.stringify(m)).join("\n");
      fs.appendFileSync(fp, `${lines}\n`, "utf8");
      this.writeQuaidLog("session_messages_appended", sessionId, { appended: sanitized.length });
    } catch (err: unknown) {
      this.writeQuaidLog("session_message_append_error", sessionId, { error: String((err as Error)?.message || err) });
    }
  }

  private readSessionMessages(sessionId: string): any[] {
    try {
      const fp = this.sessionMessagePath(sessionId);
      if (!fs.existsSync(fp)) { return []; }
      const content = fs.readFileSync(fp, "utf8");
      if (!content.trim()) { return []; }
      const lines = content.trim().split("\n");
      const out: any[] = [];
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          if (parsed && typeof parsed === "object") { out.push(parsed); }
        } catch (err: unknown) {
          safeLog(this.logger, `[quaid][timeout] skipped malformed session-message line for ${sessionId}: ${String((err as Error)?.message || err)}`);
        }
      }
      return filterEligibleMessages(out);
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed reading session message log for ${sessionId}: ${String((err as Error)?.message || err)}`);
      if (this.failHard && (err as NodeJS.ErrnoException)?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }

  private clearSessionMessageLog(sessionId: string): void {
    try {
      fs.unlinkSync(this.sessionMessagePath(sessionId));
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed clearing session message log for ${sessionId}: ${String((err as Error)?.message || err)}`);
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

  private writeQuaidLog(event: string, sessionId?: string, data?: Record<string, unknown>): void {
    const now = new Date().toISOString();
    const safeSessionId = sessionId ? String(sessionId) : "";
    const line = `${now} event=${event}${safeSessionId ? ` session=${safeSessionId}` : ""}${data ? ` data=${JSON.stringify(data)}` : ""}\n`;
    try {
      fs.appendFileSync(this.logFilePath, line, "utf8");
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed writing timeout log file ${this.logFilePath}: ${String((err as Error)?.message || err)}`);
    }

    const payload = { ts: now, event, session_id: safeSessionId || undefined, ...data };
    try {
      fs.appendFileSync(this.eventFilePath, `${JSON.stringify(payload)}\n`, "utf8");
    } catch (err: unknown) {
      safeLog(this.logger, `[quaid][timeout] failed writing timeout event log ${this.eventFilePath}: ${String((err as Error)?.message || err)}`);
    }

    if (safeSessionId) {
      const safeName = safeSessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
      const sessionPath = path.join(this.sessionLogDir, `${safeName}.jsonl`);
      try {
        fs.appendFileSync(sessionPath, `${JSON.stringify(payload)}\n`, "utf8");
      } catch (err: unknown) {
        safeLog(this.logger, `[quaid][timeout] failed writing timeout session log ${sessionPath}: ${String((err as Error)?.message || err)}`);
      }
    }
  }
}
