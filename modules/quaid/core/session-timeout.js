import * as fs from "node:fs";
import * as path from "node:path";
function signalPriority(label) {
  const normalized = String(label || "").trim().toLowerCase();
  if (normalized === "resetsignal" || normalized === "reset") return 3;
  if (normalized === "compactionsignal" || normalized === "compaction") return 2;
  return 1;
}
function safeLog(logger, message) {
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
  } catch {
  }
}
function messageText(msg) {
  if (!msg) return "";
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) return msg.content.map((c) => c?.text || "").join(" ");
  return "";
}
function isInternalMaintenancePrompt(text) {
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
    "pending soul snippets"
  ];
  return markers.some((m) => t.includes(m));
}
function isExtractionJsonAssistantPayload(text) {
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
function isEligibleConversationMessage(msg, shouldSkipText) {
  if (!msg || msg.role !== "user" && msg.role !== "assistant") return false;
  const text = messageText(msg).trim();
  if (!text) return false;
  if (shouldSkipText?.(text)) return false;
  if (isInternalMaintenancePrompt(text)) return false;
  if (msg.role === "assistant" && isExtractionJsonAssistantPayload(text)) return false;
  return true;
}
function filterEligibleMessages(messages, shouldSkipText) {
  if (!Array.isArray(messages) || messages.length === 0) return [];
  return messages.filter((msg) => isEligibleConversationMessage(msg, shouldSkipText));
}
function messageDedupKey(msg) {
  const id = typeof msg?.id === "string" ? msg.id : "";
  if (id) return `id:${id}`;
  const ts = typeof msg?.timestamp === "string" ? msg.timestamp : "";
  const role = typeof msg?.role === "string" ? msg.role : "";
  const text = messageText(msg).slice(0, 200);
  return `fallback:${ts}:${role}:${text}`;
}
function parseMessageTimestampMs(msg) {
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
function mergeUniqueMessages(existing, incoming) {
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
class SessionTimeoutManager {
  timeoutMinutes;
  extract;
  isBootstrapOnly;
  logger;
  readSessionMessagesSource;
  listSessionActivitySource;
  shouldSkipText;
  timer = null;
  pendingFallbackMessages = null;
  pendingSessionId;
  sessionCursorDir;
  pendingSignalDir;
  workerLockPath;
  workerLockToken;
  staleSweepStatePath;
  installStatePath;
  ownsWorkerLock = false;
  logDir;
  sessionLogDir;
  logFilePath;
  eventFilePath;
  workerTimer = null;
  chain = Promise.resolve();
  failHard;
  extractTimeoutMs;
  maxSignalRetries;
  staleRecoveryInitialBackoffMs = 5e3;
  staleRecoveryMaxBackoffMs = 5 * 60 * 1e3;
  constructor(opts) {
    this.timeoutMinutes = opts.timeoutMinutes;
    this.extract = opts.extract;
    this.isBootstrapOnly = opts.isBootstrapOnly;
    this.logger = opts.logger;
    this.shouldSkipText = opts.shouldSkipText;
    this.readSessionMessagesSource = (sessionId) => {
      try {
        return filterEligibleMessages(opts.readSessionMessages?.(sessionId) || [], opts.shouldSkipText);
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] source readSessionMessages failed for ${sessionId}: ${String(err?.message || err)}`);
        if (this.failHard) throw err;
        return [];
      }
    };
    this.listSessionActivitySource = () => {
      try {
        const rows = opts.listSessionActivity?.() || [];
        if (!Array.isArray(rows)) return [];
        return rows.map((r) => ({
          sessionId: String(r?.sessionId || "").trim(),
          lastActivityMs: Number(r?.lastActivityMs)
        })).filter((r) => r.sessionId && Number.isFinite(r.lastActivityMs) && r.lastActivityMs > 0);
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] source listSessionActivity failed: ${String(err?.message || err)}`);
        if (this.failHard) throw err;
        return [];
      }
    };
    this.logDir = path.resolve(String(opts.logDir || path.join(opts.workspace, "logs", "runtime")));
    this.sessionLogDir = path.join(this.logDir, "sessions");
    this.sessionCursorDir = path.join(opts.workspace, "data", "session-cursors");
    this.pendingSignalDir = path.join(opts.workspace, "data", "pending-extraction-signals");
    this.workerLockPath = path.join(opts.workspace, "data", "session-timeout-worker.lock");
    this.workerLockToken = `${process.pid}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
    this.staleSweepStatePath = path.join(opts.workspace, "data", "stale-sweep-state.json");
    this.installStatePath = path.join(opts.workspace, "data", "installed-at.json");
    this.logFilePath = path.join(this.logDir, "session-timeout.log");
    this.eventFilePath = path.join(this.logDir, "session-timeout-events.jsonl");
    const failHardOpt = opts.failHardEnabled;
    if (typeof failHardOpt === "function") {
      try {
        this.failHard = Boolean(failHardOpt());
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] failHard source threw; defaulting to true: ${String(err?.message || err)}`);
        this.failHard = true;
      }
    } else if (typeof failHardOpt === "boolean") {
      this.failHard = failHardOpt;
    } else {
      this.failHard = true;
    }
    const configuredTimeoutMs = Number(
      process.env.SESSION_EXTRACT_TIMEOUT_MS || process.env.QUAID_SESSION_EXTRACT_TIMEOUT_MS || ""
    );
    this.extractTimeoutMs = Number.isFinite(configuredTimeoutMs) && configuredTimeoutMs > 0 ? Math.floor(configuredTimeoutMs) : 6e5;
    const configuredSignalRetries = Number(
      process.env.SIGNAL_MAX_RETRIES || process.env.QUAID_SIGNAL_MAX_RETRIES || ""
    );
    this.maxSignalRetries = Number.isFinite(configuredSignalRetries) && configuredSignalRetries >= 0 ? Math.floor(configuredSignalRetries) : 3;
    try {
      fs.mkdirSync(this.logDir, { recursive: true });
      fs.mkdirSync(this.sessionLogDir, { recursive: true });
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      fs.mkdirSync(path.dirname(this.staleSweepStatePath), { recursive: true });
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
    } catch (err) {
      const msg = String(err?.message || err || "unknown directory initialization error");
      safeLog(this.logger, `[memory][timeout] failed to initialize runtime directories: ${msg}`);
      if (this.failHard) {
        throw err;
      }
    }
  }
  async runExtractWithTimeout(messages, sessionId, label) {
    const timeoutMs = Number(this.extractTimeoutMs);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
      await this.extract(messages, sessionId, label);
      return;
    }
    let timer = null;
    try {
      await Promise.race([
        this.extract(messages, sessionId, label),
        new Promise((_, reject) => {
          timer = setTimeout(
            () => reject(new Error(`session-timeout extraction timed out after ${timeoutMs}ms`)),
            timeoutMs
          );
        })
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  setTimeoutMinutes(minutes) {
    this.timeoutMinutes = minutes;
  }
  onAgentStart() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
      this.writeQuaidLog("timer_cleared", void 0, { reason: "agent_start" });
    }
  }
  onAgentEnd(messages, sessionId, meta) {
    if (!Array.isArray(messages) || messages.length === 0) return;
    if (!sessionId) return;
    const incoming = filterEligibleMessages(messages, this.shouldSkipText);
    if (incoming.length === 0) return;
    const gatedIncoming = this.filterReplayedMessages(sessionId, incoming);
    if (gatedIncoming.length === 0) {
      this.writeQuaidLog("skip_replayed_history", sessionId, { incoming: incoming.length });
      return;
    }
    const hasUserMessage = gatedIncoming.some((m) => m?.role === "user");
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
        total: this.pendingFallbackMessages.length
      });
    } else {
      this.pendingFallbackMessages = gatedIncoming;
      this.pendingSessionId = sessionId;
      this.writeQuaidLog("buffer_write", sessionId, {
        source,
        mode: "set",
        appended: gatedIncoming.length,
        total: this.pendingFallbackMessages.length
      });
    }
    this.writeQuaidLog("buffered", sessionId, {
      appended: gatedIncoming.length,
      timeout_minutes: this.timeoutMinutes,
      source: "event_messages"
    });
    if (this.timeoutMinutes <= 0) return;
    this.timer = setTimeout(() => {
      const sid = this.pendingSessionId;
      const fallback = this.pendingFallbackMessages || [];
      this.timer = null;
      this.pendingSessionId = void 0;
      this.pendingFallbackMessages = null;
      if (!sid) return;
      this.writeQuaidLog("timer_fired", sid, {
        timeout_minutes: this.timeoutMinutes
      });
      this.queueExtractionFromSession(sid, fallback, this.timeoutMinutes);
    }, this.timeoutMinutes * 60 * 1e3);
  }
  async extractSessionFromSourceDirect(sessionId, label, fallbackMessages) {
    if (!sessionId) return false;
    const sourceMessages = this.readSourceSessionMessages(sessionId);
    const sourceUnprocessed = this.filterReplayedMessages(sessionId, sourceMessages);
    const fallback = this.filterReplayedMessages(sessionId, filterEligibleMessages(fallbackMessages || [], this.shouldSkipText));
    const allowFallback = !this.failHard;
    const source = sourceUnprocessed.length > 0 ? "source_session_messages" : allowFallback && fallback.length > 0 ? "fallback_event_messages" : "none";
    const messages = sourceUnprocessed.length > 0 ? sourceUnprocessed : allowFallback ? fallback : [];
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
  async extractSessionFromLog(sessionId, label, fallbackMessages) {
    let extracted = false;
    const work = this.chain.catch((err) => {
      safeLog(this.logger, `[memory][timeout] previous extraction chain error: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    }).then(async () => {
      extracted = await this.extractSessionFromSourceDirect(sessionId, label, fallbackMessages);
    }).catch((err) => {
      safeLog(this.logger, `[memory][timeout] extraction queue failed: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    });
    this.chain = work.then(() => void 0, () => void 0);
    await work;
    return extracted;
  }
  clearSession(sessionId, cursorMessages) {
    if (!sessionId) return;
    const sourceMessages = cursorMessages || this.filterReplayedMessages(sessionId, this.readSourceSessionMessages(sessionId));
    this.writeSessionCursor(sessionId, sourceMessages);
    this.writeQuaidLog("session_cleared", sessionId);
    if (this.pendingSessionId === sessionId) {
      this.pendingSessionId = void 0;
      this.pendingFallbackMessages = null;
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
        this.writeQuaidLog("timer_cleared", sessionId, { reason: "session_cleared" });
      }
    }
  }
  async recoverStaleBuffers() {
    if (this.timeoutMinutes <= 0) return;
    const timeoutMs = this.timeoutMinutes * 60 * 1e3;
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
      previousCutoffMs = Math.min(previousCutoffMs, installedAtMs - timeoutMs);
    }
    if (previousCutoffMs > currentCutoffMs) {
      previousCutoffMs = currentCutoffMs;
    }
    const activityRows = this.listSessionActivityRows();
    const latestActivityBySession = /* @__PURE__ */ new Map();
    for (const row of activityRows) {
      const prior = latestActivityBySession.get(row.sessionId);
      if (prior == null || row.lastActivityMs > prior) {
        latestActivityBySession.set(row.sessionId, row.lastActivityMs);
      }
    }
    const candidates = /* @__PURE__ */ new Map();
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
    this.writeQuaidLog("stale_sweep_window", void 0, {
      timeout_minutes: this.timeoutMinutes,
      previous_cutoff_ms: previousCutoffMs,
      current_cutoff_ms: currentCutoffMs,
      candidate_count: candidates.size
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
        last_activity_ms: lastActivityMs
      });
      try {
        await this.runExtractWithTimeout(messages, sessionId, "Recovery");
        this.clearSession(sessionId, messages);
        delete retries[sessionId];
      } catch (err) {
        const error = String(err?.message || err);
        const priorAttempts = Math.max(0, Number(retries[sessionId]?.attemptCount || 0));
        const nextAttemptCount = priorAttempts + 1;
        const delayMs = this.staleRecoveryDelayMs(nextAttemptCount);
        retries[sessionId] = {
          sessionId,
          lastActivityMs,
          attemptCount: nextAttemptCount,
          nextRecoveryAt: new Date(nowMs + delayMs).toISOString(),
          lastError: error
        };
        this.writeQuaidLog("recover_stale_buffer_backoff", sessionId, {
          attempt_count: nextAttemptCount,
          delay_ms: delayMs,
          error
        });
      }
    }
    this.writeStaleSweepState({
      installedAt: state.installedAt || this.readInstalledAt(),
      lastSweepAt: new Date(nowMs).toISOString(),
      retries
    });
  }
  staleRecoveryDelayMs(attemptCount) {
    const attempt = Math.max(1, Math.floor(attemptCount));
    const multiplier = 2 ** (attempt - 1);
    return Math.min(this.staleRecoveryInitialBackoffMs * multiplier, this.staleRecoveryMaxBackoffMs);
  }
  buildOriginHintMeta(meta) {
    if (meta && typeof meta === "object") return meta;
    const stack = String(new Error().stack || "");
    const lines = stack.split("\n").map((s) => s.trim()).filter(Boolean);
    const caller = lines.find(
      (line) => !line.includes("buildOriginHintMeta") && !line.includes("queueExtractionSignal") && !line.includes("SessionTimeoutManager.")
    );
    if (!caller) return void 0;
    return { origin_hint: caller.slice(0, 240) };
  }
  queueExtractionSignal(sessionId, label, meta) {
    if (!sessionId) return;
    const signalMeta = this.buildOriginHintMeta(meta);
    if (!this.hasUnprocessedSessionMessages(sessionId)) {
      this.writeQuaidLog("signal_queue_skipped_already_cleared", sessionId, {
        label: String(label || "Signal"),
        ...signalMeta ? { meta: signalMeta } : {}
      });
      return;
    }
    const signal = {
      sessionId,
      label: String(label || "Signal"),
      queuedAt: (/* @__PURE__ */ new Date()).toISOString(),
      attemptCount: 0,
      ...signalMeta ? { meta: signalMeta } : {}
    };
    try {
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      const signalPath = this.signalPath(sessionId);
      if (fs.existsSync(signalPath)) {
        let existingLabel = "Signal";
        let existingAttemptCount = 0;
        try {
          const existing = JSON.parse(fs.readFileSync(signalPath, "utf8"));
          existingLabel = String(existing?.label || "Signal");
          existingAttemptCount = Math.max(0, Number(existing?.attemptCount || 0));
        } catch (err) {
          safeLog(this.logger, `[memory][timeout] failed to parse existing extraction signal ${signalPath}: ${String(err?.message || err)}`);
        }
        const incomingPriority = signalPriority(signal.label);
        const existingPriority = signalPriority(existingLabel);
        if (incomingPriority > existingPriority) {
          signal.attemptCount = 0;
          fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 384 });
          this.writeQuaidLog("signal_file_write", sessionId, {
            op: "promote_overwrite",
            path: signalPath,
            label: signal.label,
            has_meta: Boolean(signalMeta)
          });
          this.writeQuaidLog("signal_queue_promoted", sessionId, {
            from: existingLabel,
            to: signal.label,
            previous_attempt_count: existingAttemptCount,
            ...signalMeta ? { meta: signalMeta } : {}
          });
        } else {
          this.writeQuaidLog("signal_queue_coalesced", sessionId, {
            label: signal.label,
            existing_label: existingLabel,
            reason: "already_pending",
            ...signalMeta ? { meta: signalMeta } : {}
          });
        }
        this.triggerWorkerTick();
        return;
      }
      fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 384 });
      this.writeQuaidLog("signal_file_write", sessionId, {
        op: "create",
        path: signalPath,
        label: signal.label,
        has_meta: Boolean(signalMeta)
      });
      this.writeQuaidLog("signal_queued", sessionId, {
        label: signal.label,
        ...signalMeta ? { meta: signalMeta } : {}
      });
      this.triggerWorkerTick();
    } catch (err) {
      this.writeQuaidLog("signal_queue_error", sessionId, {
        label: signal.label,
        ...signalMeta ? { meta: signalMeta } : {},
        error: String(err?.message || err)
      });
    }
  }
  async processPendingExtractionSignals() {
    this.recoverOrphanedSignalClaims();
    for (const filePath of this.listSignalFiles()) {
      const lockedPath = this.claimSignalFile(filePath);
      if (!lockedPath) {
        continue;
      }
      this.writeQuaidLog("signal_file_claimed", path.basename(filePath, ".json"), {
        from: filePath,
        to: lockedPath
      });
      let signal = null;
      try {
        signal = JSON.parse(fs.readFileSync(lockedPath, "utf8"));
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] dropping malformed extraction signal ${lockedPath}: ${String(err?.message || err)}`);
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr) {
          safeLog(this.logger, `[memory][timeout] failed to delete malformed extraction signal ${lockedPath}: ${String(unlinkErr?.message || unlinkErr)}`);
        }
        continue;
      }
      const sessionId = String(signal?.sessionId || path.basename(filePath, ".json")).trim();
      const label = String(signal?.label || "Signal");
      const attemptCount = Math.max(0, Number(signal?.attemptCount || 0));
      const meta = signal?.meta && typeof signal.meta === "object" ? signal.meta : void 0;
      if (!sessionId) {
        try {
          fs.unlinkSync(lockedPath);
        } catch (unlinkErr) {
          safeLog(this.logger, `[memory][timeout] failed to delete signal without session id ${lockedPath}: ${String(unlinkErr?.message || unlinkErr)}`);
        }
        continue;
      }
      let restoredClaim = false;
      try {
        this.writeQuaidLog("signal_process_begin", sessionId, {
          label,
          ...meta ? { meta } : {}
        });
        await this.extractSessionFromSourceDirect(sessionId, label);
        this.writeQuaidLog("signal_process_done", sessionId, {
          label,
          ...meta ? { meta } : {}
        });
      } catch (err) {
        this.writeQuaidLog("signal_process_error", sessionId, {
          label,
          ...meta ? { meta } : {},
          error: String(err?.message || err)
        });
        const nextAttemptCount = attemptCount + 1;
        const canRetry = nextAttemptCount <= this.maxSignalRetries;
        try {
          const originalPath = filePath;
          if (canRetry && !fs.existsSync(originalPath) && fs.existsSync(lockedPath)) {
            const nextSignal = {
              sessionId,
              label,
              queuedAt: String(signal?.queuedAt || (/* @__PURE__ */ new Date()).toISOString()),
              attemptCount: nextAttemptCount,
              ...meta ? { meta } : {}
            };
            fs.writeFileSync(lockedPath, JSON.stringify(nextSignal), { mode: 384 });
            fs.renameSync(lockedPath, originalPath);
            restoredClaim = true;
            this.writeQuaidLog("signal_file_write", sessionId, {
              op: "retry_requeue",
              path: originalPath,
              label,
              has_meta: Boolean(meta)
            });
            this.writeQuaidLog("signal_process_requeued", sessionId, {
              label,
              attempt_count: nextAttemptCount,
              max_retries: this.maxSignalRetries,
              ...meta ? { meta } : {}
            });
          } else if (!canRetry) {
            this.writeQuaidLog("signal_process_dropped", sessionId, {
              label,
              attempt_count: nextAttemptCount,
              max_retries: this.maxSignalRetries,
              reason: "max_retries_exceeded",
              ...meta ? { meta } : {}
            });
          }
        } catch (restoreErr) {
          safeLog(this.logger, `[memory][timeout] failed restoring signal claim ${lockedPath}: ${String(restoreErr?.message || restoreErr)}`);
          if (restoreErr?.code !== "ENOENT") {
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
        } catch (unlinkErr) {
          safeLog(this.logger, `[memory][timeout] failed cleaning claimed signal ${lockedPath}: ${String(unlinkErr?.message || unlinkErr)}`);
          if (this.failHard && unlinkErr?.code !== "ENOENT") {
            throw unlinkErr;
          }
        }
      }
    }
  }
  startWorker(heartbeatSeconds = 30) {
    this.stopWorker();
    if (!this.tryAcquireWorkerLock()) {
      this.writeQuaidLog("worker_start_skipped", void 0, {
        reason: "lock_held",
        lock_path: this.workerLockPath
      });
      return false;
    }
    const sec = Number.isFinite(heartbeatSeconds) ? Math.max(5, Math.floor(heartbeatSeconds)) : 30;
    this.workerTimer = setInterval(() => this.triggerWorkerTick(), sec * 1e3);
    if (typeof this.workerTimer.unref === "function") {
      this.workerTimer.unref();
    }
    this.writeQuaidLog("worker_started", void 0, {
      heartbeat_seconds: sec,
      lock_path: this.workerLockPath,
      leader_pid: process.pid
    });
    this.triggerWorkerTick();
    return true;
  }
  stopWorker() {
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
  isPidAlive(pid) {
    if (!Number.isFinite(pid) || pid <= 0) return false;
    try {
      process.kill(pid, 0);
      return true;
    } catch {
      return false;
    }
  }
  tryAcquireWorkerLock() {
    if (this.ownsWorkerLock) return true;
    try {
      fs.mkdirSync(path.dirname(this.workerLockPath), { recursive: true });
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed to initialize worker lock directory: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
      return false;
    }
    const payload = {
      pid: process.pid,
      token: this.workerLockToken,
      started_at: (/* @__PURE__ */ new Date()).toISOString()
    };
    try {
      fs.writeFileSync(this.workerLockPath, JSON.stringify(payload), { mode: 384, flag: "wx" });
      this.ownsWorkerLock = true;
      return true;
    } catch (err) {
      const code = err?.code;
      if (code !== "EEXIST") return false;
    }
    try {
      const raw = fs.readFileSync(this.workerLockPath, "utf8");
      const existing = JSON.parse(raw);
      const existingPid = Number(existing?.pid || 0);
      if (this.isPidAlive(existingPid)) return false;
      const verifyRaw = fs.readFileSync(this.workerLockPath, "utf8");
      const verify = JSON.parse(verifyRaw);
      if (Number(verify?.pid || 0) !== existingPid || String(verify?.token || "") !== String(existing?.token || "")) {
        return false;
      }
      try {
        fs.unlinkSync(this.workerLockPath);
      } catch (unlinkErr) {
        safeLog(this.logger, `[memory][timeout] failed removing stale worker lock ${this.workerLockPath}: ${String(unlinkErr?.message || unlinkErr)}`);
        if (this.failHard && unlinkErr?.code !== "ENOENT") {
          throw unlinkErr;
        }
      }
      fs.writeFileSync(this.workerLockPath, JSON.stringify(payload), { mode: 384, flag: "wx" });
      this.ownsWorkerLock = true;
      this.writeQuaidLog("worker_lock_stale_recovered", void 0, { stale_pid: existingPid || void 0 });
      return true;
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed stale-worker lock recovery: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
      return false;
    }
  }
  releaseWorkerLock() {
    if (!this.ownsWorkerLock) return false;
    try {
      const raw = fs.readFileSync(this.workerLockPath, "utf8");
      const existing = JSON.parse(raw);
      if (existing?.token && existing.token !== this.workerLockToken) {
        this.ownsWorkerLock = false;
        return false;
      }
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed validating worker lock before release: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
    }
    try {
      fs.unlinkSync(this.workerLockPath);
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed releasing worker lock ${this.workerLockPath}: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
    }
    this.ownsWorkerLock = false;
    return true;
  }
  queueExtractionFromSession(sessionId, fallbackMessages, timeoutMinutes) {
    this.chain = this.chain.catch((err) => {
      safeLog(this.logger, `[memory][timeout] previous extraction chain error: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    }).then(async () => {
      const extracted = await this.extractSessionFromSourceDirect(sessionId, "Timeout", fallbackMessages);
      if (!extracted) {
        this.writeQuaidLog("timeout_extract_skip_empty", sessionId, { timeout_minutes: timeoutMinutes });
        return;
      }
      this.writeQuaidLog("timeout_extract_done", sessionId, { timeout_minutes: timeoutMinutes });
    }).catch((err) => {
      safeLog(this.logger, `[memory][timeout] extraction queue failed: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    });
  }
  triggerWorkerTick() {
    this.chain = this.chain.catch((err) => {
      safeLog(this.logger, `[memory][timeout] previous worker chain error: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    }).then(async () => {
      await this.processPendingExtractionSignals();
      await this.recoverStaleBuffers();
    }).catch((err) => {
      safeLog(this.logger, `[memory][timeout] worker tick failed: ${String(err?.message || err)}`);
      if (this.failHard) throw err;
    });
  }
  signalPath(sessionId) {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.pendingSignalDir, `${safeSessionId}.json`);
  }
  listSignalFiles() {
    try {
      if (!fs.existsSync(this.pendingSignalDir)) return [];
      return fs.readdirSync(this.pendingSignalDir).filter((f) => f.endsWith(".json")).map((f) => path.join(this.pendingSignalDir, f));
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed listing signal files: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }
  listSignalClaimFiles() {
    try {
      if (!fs.existsSync(this.pendingSignalDir)) return [];
      return fs.readdirSync(this.pendingSignalDir).filter((f) => /\.json\.processing\.\d+$/.test(f)).map((f) => path.join(this.pendingSignalDir, f));
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed listing signal claim files: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
      return [];
    }
  }
  recoverOrphanedSignalClaims() {
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
            orphan: lockedPath
          });
          continue;
        }
        try {
          const raw = JSON.parse(fs.readFileSync(lockedPath, "utf8"));
          if (raw && typeof raw === "object") {
            const priorLabel = String(raw.label || "Signal");
            const priorMeta = raw.meta && typeof raw.meta === "object" ? raw.meta : {};
            const patched = {
              sessionId: String(raw.sessionId || path.basename(originalPath, ".json")),
              label: priorLabel.toLowerCase().includes("recover") ? priorLabel : "RecoverySignal",
              queuedAt: String(raw.queuedAt || (/* @__PURE__ */ new Date()).toISOString()),
              attemptCount: Math.max(0, Number(raw.attemptCount || 0)),
              meta: {
                ...priorMeta,
                source: "orphan_recovery",
                recovered_orphan: true,
                original_label: priorLabel
              }
            };
            fs.writeFileSync(lockedPath, JSON.stringify(patched), { mode: 384 });
          }
        } catch {
        }
        fs.renameSync(lockedPath, originalPath);
        this.writeQuaidLog("signal_file_recovered", path.basename(originalPath, ".json"), {
          from: lockedPath,
          to: originalPath
        });
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] failed recovering orphaned signal claim ${lockedPath}: ${String(err?.message || err)}`);
        if (this.failHard && err?.code !== "ENOENT") {
          throw err;
        }
      }
    }
  }
  claimSignalFile(filePath) {
    const lockedPath = `${filePath}.processing.${process.pid}`;
    try {
      fs.renameSync(filePath, lockedPath);
      return lockedPath;
    } catch {
      return null;
    }
  }
  cursorPath(sessionId) {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.sessionCursorDir, `${safeSessionId}.json`);
  }
  readSessionCursor(sessionId) {
    try {
      const fp = this.cursorPath(sessionId);
      if (!fs.existsSync(fp)) return null;
      const payload = JSON.parse(fs.readFileSync(fp, "utf8"));
      if (!payload || typeof payload !== "object") return null;
      return payload;
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed reading session cursor for ${sessionId}: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
      return null;
    }
  }
  writeSessionCursor(sessionId, messages) {
    try {
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      const last = Array.isArray(messages) && messages.length > 0 ? messages[messages.length - 1] : null;
      const payload = {
        sessionId,
        clearedAt: (/* @__PURE__ */ new Date()).toISOString()
      };
      if (last) {
        payload.lastMessageKey = messageDedupKey(last);
        const ts = parseMessageTimestampMs(last);
        if (ts !== null) payload.lastTimestampMs = ts;
      }
      fs.writeFileSync(this.cursorPath(sessionId), JSON.stringify(payload), { mode: 384 });
      this.writeQuaidLog("session_cursor_written", sessionId, {
        has_last_key: Boolean(payload.lastMessageKey),
        has_last_ts: typeof payload.lastTimestampMs === "number"
      });
    } catch (err) {
      this.writeQuaidLog("session_cursor_write_error", sessionId, { error: String(err?.message || err) });
      if (this.failHard) {
        throw err;
      }
    }
  }
  filterReplayedMessages(sessionId, incoming) {
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
        return ts !== null && ts > cursor.lastTimestampMs;
      });
    }
    return incoming;
  }
  readSourceSessionMessages(sessionId) {
    const rows = this.readSessionMessagesSource(sessionId);
    if (!Array.isArray(rows)) return [];
    return filterEligibleMessages(rows, this.shouldSkipText);
  }
  listSessionActivityRows() {
    return this.listSessionActivitySource();
  }
  hasUnprocessedSessionMessages(sessionId) {
    if (this.pendingSessionId === sessionId && Array.isArray(this.pendingFallbackMessages)) {
      const pending = this.filterReplayedMessages(sessionId, filterEligibleMessages(this.pendingFallbackMessages, this.shouldSkipText));
      if (pending.length > 0) return true;
    }
    const messages = this.readSourceSessionMessages(sessionId);
    if (!messages.length) return false;
    const filtered = this.filterReplayedMessages(sessionId, messages);
    return filtered.length > 0;
  }
  readStaleSweepState() {
    try {
      const installedAt = this.readInstalledAt();
      if (!fs.existsSync(this.staleSweepStatePath)) return { installedAt };
      const parsed = JSON.parse(fs.readFileSync(this.staleSweepStatePath, "utf8"));
      if (!parsed || typeof parsed !== "object") return { installedAt };
      const retriesRaw = parsed.retries && typeof parsed.retries === "object" ? parsed.retries : {};
      const retries = {};
      for (const [sid, value] of Object.entries(retriesRaw)) {
        const item = value;
        const sessionId = String(item.sessionId || sid).trim();
        const lastActivityMs = Number(item.lastActivityMs);
        const attemptCount = Math.max(0, Number(item.attemptCount || 0));
        if (!sessionId || !Number.isFinite(lastActivityMs) || lastActivityMs <= 0) continue;
        retries[sessionId] = {
          sessionId,
          lastActivityMs,
          attemptCount,
          nextRecoveryAt: item.nextRecoveryAt,
          lastError: item.lastError
        };
      }
      return {
        installedAt: typeof parsed.installedAt === "string" ? parsed.installedAt : installedAt,
        lastSweepAt: typeof parsed.lastSweepAt === "string" ? parsed.lastSweepAt : void 0,
        retries
      };
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed reading stale sweep state: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
      return { installedAt: this.readInstalledAt() };
    }
  }
  writeStaleSweepState(state) {
    try {
      fs.mkdirSync(path.dirname(this.staleSweepStatePath), { recursive: true });
      const installedAt = state.installedAt || this.readInstalledAt();
      fs.writeFileSync(this.staleSweepStatePath, JSON.stringify({ ...state, installedAt }), { mode: 384 });
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed writing stale sweep state: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
  }
  readInstalledAt() {
    try {
      if (fs.existsSync(this.installStatePath)) {
        const raw = JSON.parse(fs.readFileSync(this.installStatePath, "utf8"));
        const installedAt2 = String(raw?.installedAt || "").trim();
        if (installedAt2) {
          return installedAt2;
        }
      }
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed reading installed-at state: ${String(err?.message || err)}`);
      if (this.failHard && err?.code !== "ENOENT") {
        throw err;
      }
    }
    const installedAt = (/* @__PURE__ */ new Date()).toISOString();
    try {
      fs.mkdirSync(path.dirname(this.installStatePath), { recursive: true });
      fs.writeFileSync(this.installStatePath, JSON.stringify({ installedAt }), { mode: 384 });
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed writing installed-at state: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
    return installedAt;
  }
  writeQuaidLog(event, sessionId, data) {
    const now = (/* @__PURE__ */ new Date()).toISOString();
    const safeSessionId = sessionId ? String(sessionId) : "";
    const line = `${now} event=${event}${safeSessionId ? ` session=${safeSessionId}` : ""}${data ? ` data=${JSON.stringify(data)}` : ""}
`;
    try {
      fs.mkdirSync(path.dirname(this.logFilePath), { recursive: true });
      fs.appendFileSync(this.logFilePath, line, "utf8");
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed writing timeout log file ${this.logFilePath}: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
    const payload = { ts: now, event, session_id: safeSessionId || void 0, ...data };
    try {
      fs.mkdirSync(path.dirname(this.eventFilePath), { recursive: true });
      fs.appendFileSync(this.eventFilePath, `${JSON.stringify(payload)}
`, "utf8");
    } catch (err) {
      safeLog(this.logger, `[memory][timeout] failed writing timeout event log ${this.eventFilePath}: ${String(err?.message || err)}`);
      if (this.failHard) {
        throw err;
      }
    }
    if (safeSessionId) {
      const safeName = safeSessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
      const sessionPath = path.join(this.sessionLogDir, `${safeName}.jsonl`);
      try {
        fs.mkdirSync(this.sessionLogDir, { recursive: true });
        fs.appendFileSync(sessionPath, `${JSON.stringify(payload)}
`, "utf8");
      } catch (err) {
        safeLog(this.logger, `[memory][timeout] failed writing timeout session log ${sessionPath}: ${String(err?.message || err)}`);
        if (this.failHard) {
          throw err;
        }
      }
    }
  }
}
export {
  SessionTimeoutManager
};
