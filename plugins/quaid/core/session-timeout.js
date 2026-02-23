import * as fs from "node:fs";
import * as path from "node:path";
function safeLog(logger, message) {
  try {
    (logger || console.log)(message);
  } catch {
  }
}
function messageText(msg) {
  if (!msg) return "";
  if (typeof msg.content === "string") return msg.content;
  if (Array.isArray(msg.content)) return msg.content.map((c) => c?.text || "").join(" ");
  return "";
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
  timer = null;
  pendingMessages = null;
  pendingSessionId;
  buffers = /* @__PURE__ */ new Map();
  bufferDir;
  logDir;
  sessionLogDir;
  sessionMessageLogDir;
  sessionCursorDir;
  pendingSignalDir;
  workerLockPath;
  workerLockToken;
  ownsWorkerLock = false;
  logFilePath;
  eventFilePath;
  workerTimer = null;
  chain = Promise.resolve();
  constructor(opts) {
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
    try {
      fs.mkdirSync(this.logDir, { recursive: true });
      fs.mkdirSync(this.sessionLogDir, { recursive: true });
      fs.mkdirSync(this.sessionMessageLogDir, { recursive: true });
      fs.mkdirSync(this.sessionCursorDir, { recursive: true });
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
    } catch {
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
  onAgentEnd(messages, sessionId) {
    if (!Array.isArray(messages) || messages.length === 0) return;
    if (!sessionId) return;
    const incoming = messages.filter((m) => m && (m.role === "user" || m.role === "assistant"));
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
    this.writeBuffer(sessionId, merged);
    this.appendSessionMessages(sessionId, added);
    this.pendingMessages = merged;
    this.pendingSessionId = sessionId;
    safeLog(this.logger, `[quaid][timeout] buffered session=${sessionId} appended=${Math.max(0, merged.length - existing.length)} total=${merged.length}`);
    this.writeQuaidLog("buffered", sessionId, {
      appended: added.length,
      total: merged.length,
      timeout_minutes: this.timeoutMinutes
    });
    if (this.timeoutMinutes <= 0) return;
    this.timer = setTimeout(() => {
      const msgs = this.pendingMessages;
      const sid = this.pendingSessionId;
      this.timer = null;
      this.pendingMessages = null;
      this.pendingSessionId = void 0;
      if (!msgs || !sid || msgs.length === 0) return;
      const loggedMessages = this.readSessionMessages(sid);
      const extractionMessages = loggedMessages.length > 0 ? loggedMessages : msgs;
      this.writeQuaidLog("timer_fired", sid, {
        message_count: extractionMessages.length,
        timeout_minutes: this.timeoutMinutes,
        source: loggedMessages.length > 0 ? "session_message_log" : "pending_buffer"
      });
      this.queueExtraction(extractionMessages, sid, this.timeoutMinutes);
    }, this.timeoutMinutes * 60 * 1e3);
  }
  async extractSessionFromLog(sessionId, label, fallbackMessages) {
    if (!sessionId) return false;
    const loggedMessages = this.readSessionMessages(sessionId);
    const source = loggedMessages.length > 0 ? "session_message_log" : Array.isArray(fallbackMessages) && fallbackMessages.length > 0 ? "fallback_event_messages" : "none";
    const messages = loggedMessages.length > 0 ? loggedMessages : fallbackMessages || [];
    if (!messages.length) {
      this.writeQuaidLog("extract_skip_empty", sessionId, { label, source });
      return false;
    }
    this.writeQuaidLog("extract_begin", sessionId, { label, message_count: messages.length, source });
    await this.extract(messages, sessionId, label);
    this.writeQuaidLog("extract_done", sessionId, { label, message_count: messages.length, source });
    this.clearSession(sessionId);
    return true;
  }
  clearSession(sessionId) {
    if (!sessionId) return;
    const loggedMessages = this.readSessionMessages(sessionId);
    const bufferedMessages = this.buffers.get(sessionId) || this.readBuffer(sessionId);
    const cursorMessages = loggedMessages.length > 0 ? loggedMessages : bufferedMessages;
    this.writeSessionCursor(sessionId, cursorMessages);
    this.buffers.delete(sessionId);
    this.clearBuffer(sessionId);
    this.clearSessionMessageLog(sessionId);
    this.writeQuaidLog("session_cleared", sessionId);
    if (this.pendingSessionId === sessionId) {
      this.pendingMessages = null;
      this.pendingSessionId = void 0;
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
        this.writeQuaidLog("timer_cleared", sessionId, { reason: "session_cleared" });
      }
    }
  }
  async recoverStaleBuffers() {
    if (this.timeoutMinutes <= 0) return;
    const now = Date.now();
    const staleMs = this.timeoutMinutes * 60 * 1e3;
    for (const filePath of this.listBufferFiles()) {
      const lockedPath = this.claimBufferFile(filePath);
      if (!lockedPath) {
        continue;
      }
      try {
        const payload = JSON.parse(fs.readFileSync(lockedPath, "utf8"));
        const sid = String(payload?.sessionId || path.basename(filePath, ".json")).trim();
        const updatedAtMs = Date.parse(String(payload?.updatedAt || ""));
        const msgs = Array.isArray(payload?.messages) ? payload.messages : [];
        if (!sid || msgs.length === 0) {
          try {
            fs.unlinkSync(lockedPath);
          } catch {
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
          source: loggedMessages.length > 0 ? "session_message_log" : "pending_buffer"
        });
        await this.extract(extractionMessages, sid);
        this.clearSession(sid);
        try {
          fs.unlinkSync(lockedPath);
        } catch {
        }
      } catch (err) {
        safeLog(this.logger, `[quaid][timeout] stale buffer recovery failed for ${filePath}: ${String(err?.message || err)}`);
        this.releaseBufferFile(lockedPath, filePath);
      }
    }
  }
  queueExtractionSignal(sessionId, label) {
    if (!sessionId) return;
    const signal = {
      sessionId,
      label: String(label || "Signal"),
      queuedAt: (/* @__PURE__ */ new Date()).toISOString()
    };
    try {
      fs.mkdirSync(this.pendingSignalDir, { recursive: true });
      const signalPath = this.signalPath(sessionId);
      if (fs.existsSync(signalPath)) {
        this.writeQuaidLog("signal_queue_coalesced", sessionId, { label: signal.label, reason: "already_pending" });
        this.triggerWorkerTick();
        return;
      }
      fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 384 });
      this.writeQuaidLog("signal_queued", sessionId, { label: signal.label });
      this.triggerWorkerTick();
    } catch (err) {
      this.writeQuaidLog("signal_queue_error", sessionId, { label: signal.label, error: String(err?.message || err) });
    }
  }
  async processPendingExtractionSignals() {
    for (const filePath of this.listSignalFiles()) {
      const lockedPath = this.claimSignalFile(filePath);
      if (!lockedPath) {
        continue;
      }
      let signal = null;
      try {
        signal = JSON.parse(fs.readFileSync(lockedPath, "utf8"));
      } catch {
        try {
          fs.unlinkSync(lockedPath);
        } catch {
        }
        continue;
      }
      const sessionId = String(signal?.sessionId || path.basename(filePath, ".json")).trim();
      const label = String(signal?.label || "Signal");
      if (!sessionId) {
        try {
          fs.unlinkSync(lockedPath);
        } catch {
        }
        continue;
      }
      try {
        this.writeQuaidLog("signal_process_begin", sessionId, { label });
        await this.extractSessionFromLog(sessionId, label);
        this.writeQuaidLog("signal_process_done", sessionId, { label });
      } catch (err) {
        this.writeQuaidLog("signal_process_error", sessionId, { label, error: String(err?.message || err) });
      } finally {
        try {
          fs.unlinkSync(lockedPath);
        } catch {
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
    } catch {
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
      try {
        fs.unlinkSync(this.workerLockPath);
      } catch {
      }
      fs.writeFileSync(this.workerLockPath, JSON.stringify(payload), { mode: 384, flag: "wx" });
      this.ownsWorkerLock = true;
      this.writeQuaidLog("worker_lock_stale_recovered", void 0, { stale_pid: existingPid || void 0 });
      return true;
    } catch {
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
    } catch {
    }
    try {
      fs.unlinkSync(this.workerLockPath);
    } catch {
    }
    this.ownsWorkerLock = false;
    return true;
  }
  queueExtraction(messages, sessionId, timeoutMinutes) {
    this.chain = this.chain.catch(() => {
    }).then(async () => {
      safeLog(this.logger, `[quaid] Inactivity timeout (${timeoutMinutes}m) \u2014 extracting ${messages.length} messages`);
      this.writeQuaidLog("extract_begin", sessionId, { message_count: messages.length, timeout_minutes: timeoutMinutes });
      await this.extract(messages, sessionId, "Timeout");
      this.writeQuaidLog("extract_done", sessionId, { message_count: messages.length });
      this.clearSession(sessionId);
    });
  }
  triggerWorkerTick() {
    this.chain = this.chain.catch(() => {
    }).then(async () => {
      await this.processPendingExtractionSignals();
      await this.recoverStaleBuffers();
    }).catch((err) => {
      safeLog(this.logger, `[quaid][timeout] worker tick failed: ${String(err?.message || err)}`);
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
    } catch {
      return [];
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
  bufferPath(sessionId) {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.bufferDir, `${safeSessionId}.json`);
  }
  readBuffer(sessionId) {
    try {
      const payload = JSON.parse(fs.readFileSync(this.bufferPath(sessionId), "utf8"));
      if (Array.isArray(payload?.messages)) return payload.messages;
    } catch {
    }
    return [];
  }
  writeBuffer(sessionId, messages) {
    try {
      fs.mkdirSync(this.bufferDir, { recursive: true });
      const payload = {
        sessionId,
        updatedAt: (/* @__PURE__ */ new Date()).toISOString(),
        messages
      };
      fs.writeFileSync(this.bufferPath(sessionId), JSON.stringify(payload), { mode: 384 });
    } catch (err) {
      safeLog(this.logger, `[quaid][timeout] failed to persist buffer session=${sessionId}: ${String(err?.message || err)}`);
      this.writeQuaidLog("buffer_persist_error", sessionId, { error: String(err?.message || err) });
    }
  }
  clearBuffer(sessionId) {
    try {
      fs.unlinkSync(this.bufferPath(sessionId));
    } catch {
    }
  }
  listBufferFiles() {
    try {
      if (!fs.existsSync(this.bufferDir)) return [];
      return fs.readdirSync(this.bufferDir).filter((f) => f.endsWith(".json")).map((f) => path.join(this.bufferDir, f));
    } catch {
      return [];
    }
  }
  claimBufferFile(filePath) {
    const lockedPath = `${filePath}.processing.${process.pid}`;
    try {
      fs.renameSync(filePath, lockedPath);
      return lockedPath;
    } catch {
      return null;
    }
  }
  releaseBufferFile(lockedPath, originalPath) {
    try {
      if (!fs.existsSync(lockedPath)) return;
      if (fs.existsSync(originalPath)) {
        try {
          fs.unlinkSync(lockedPath);
        } catch {
        }
        return;
      }
      fs.renameSync(lockedPath, originalPath);
    } catch {
    }
  }
  sessionMessagePath(sessionId) {
    const safeSessionId = String(sessionId || "unknown").replace(/[^a-zA-Z0-9_-]/g, "_");
    return path.join(this.sessionMessageLogDir, `${safeSessionId}.jsonl`);
  }
  appendSessionMessages(sessionId, messages) {
    if (!messages.length) {
      return;
    }
    try {
      fs.mkdirSync(this.sessionMessageLogDir, { recursive: true });
      const fp = this.sessionMessagePath(sessionId);
      const lines = messages.map((m) => JSON.stringify(m)).join("\n");
      fs.appendFileSync(fp, `${lines}
`, "utf8");
      this.writeQuaidLog("session_messages_appended", sessionId, { appended: messages.length });
    } catch (err) {
      this.writeQuaidLog("session_message_append_error", sessionId, { error: String(err?.message || err) });
    }
  }
  readSessionMessages(sessionId) {
    try {
      const fp = this.sessionMessagePath(sessionId);
      if (!fs.existsSync(fp)) {
        return [];
      }
      const content = fs.readFileSync(fp, "utf8");
      if (!content.trim()) {
        return [];
      }
      const lines = content.trim().split("\n");
      const out = [];
      for (const line of lines) {
        try {
          const parsed = JSON.parse(line);
          if (parsed && typeof parsed === "object") {
            out.push(parsed);
          }
        } catch {
        }
      }
      return out;
    } catch {
      return [];
    }
  }
  clearSessionMessageLog(sessionId) {
    try {
      fs.unlinkSync(this.sessionMessagePath(sessionId));
    } catch {
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
    } catch {
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
  writeQuaidLog(event, sessionId, data) {
    const now = (/* @__PURE__ */ new Date()).toISOString();
    const safeSessionId = sessionId ? String(sessionId) : "";
    const line = `${now} event=${event}${safeSessionId ? ` session=${safeSessionId}` : ""}${data ? ` data=${JSON.stringify(data)}` : ""}
`;
    try {
      fs.appendFileSync(this.logFilePath, line, "utf8");
    } catch {
    }
    const payload = { ts: now, event, session_id: safeSessionId || void 0, ...data };
    try {
      fs.appendFileSync(this.eventFilePath, `${JSON.stringify(payload)}
`, "utf8");
    } catch {
    }
    if (safeSessionId) {
      const safeName = safeSessionId.replace(/[^a-zA-Z0-9_-]/g, "_");
      const sessionPath = path.join(this.sessionLogDir, `${safeName}.jsonl`);
      try {
        fs.appendFileSync(sessionPath, `${JSON.stringify(payload)}
`, "utf8");
      } catch {
      }
    }
  }
}
export {
  SessionTimeoutManager
};
