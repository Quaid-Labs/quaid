import * as fs from "node:fs";
const DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS = 50;
const DELAYED_REQUESTS_LOCK_SLEEP_MS = 10;
const DELAYED_REQUESTS_LOCK_STALE_MS = 5e3;
function warnDelayed(message) {
  try {
    console.warn(message);
  } catch {
  }
}
function readJson(path) {
  try {
    if (!fs.existsSync(path)) return null;
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } catch (err) {
    warnDelayed(`[quaid] delayed requests read failed path=${path}: ${String(err?.message || err)}`);
    return null;
  }
}
function writeJson(path, payload) {
  const tmpPath = `${path}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), { mode: 384 });
  fs.renameSync(tmpPath, path);
}
function _sleepMs(ms) {
  const i32 = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
}
function tryRecoverStaleRequestsLock(lockPath) {
  try {
    const stat = fs.statSync(lockPath);
    const ageMs = Date.now() - Number(stat.mtimeMs || 0);
    if (!(ageMs >= DELAYED_REQUESTS_LOCK_STALE_MS)) {
      return false;
    }
    fs.unlinkSync(lockPath);
    warnDelayed(`[quaid] delayed requests recovered stale lock path=${lockPath} age_ms=${Math.round(ageMs)}`);
    return true;
  } catch {
    return false;
  }
}
function withRequestsLock(requestsPath, fn) {
  const lockPath = `${requestsPath}.lock`;
  let fd;
  let lastErr;
  for (let attempt = 0; attempt < DELAYED_REQUESTS_LOCK_MAX_ATTEMPTS; attempt++) {
    try {
      fd = fs.openSync(lockPath, "wx", 384);
      break;
    } catch (err) {
      const code = err?.code;
      if (code !== "EEXIST") throw err;
      lastErr = err;
      if (tryRecoverStaleRequestsLock(lockPath)) {
        continue;
      }
      _sleepMs(DELAYED_REQUESTS_LOCK_SLEEP_MS);
    }
  }
  if (fd === void 0) {
    throw new Error(`failed to acquire delayed-requests lock: ${String(lastErr?.message || lastErr)}`);
  }
  try {
    return fn();
  } finally {
    try {
      fs.closeSync(fd);
    } catch {
    }
    try {
      fs.unlinkSync(lockPath);
    } catch {
    }
  }
}
function makeRequestId(kind, message) {
  return `${kind}-${Buffer.from(message).toString("base64").slice(0, 16)}`;
}
function priorityRank(priority) {
  const token = String(priority || "").trim().toLowerCase();
  if (token === "high") return 2;
  if (token === "low") return 0;
  return 1;
}
function sortPendingRequests(items) {
  return items.slice().sort((a, b) => {
    const rankDelta = priorityRank(String(b?.priority || "")) - priorityRank(String(a?.priority || ""));
    if (rankDelta !== 0) return rankDelta;
    return String(a?.created_at || "").localeCompare(String(b?.created_at || ""));
  });
}
function queueDelayedRequest(requestsPath, message, kind = "janitor", priority = "normal", source = "quaid_adapter", failHard = false) {
  try {
    return withRequestsLock(requestsPath, () => {
      const normalizedMessage = String(message || "").trim();
      if (!normalizedMessage) return false;
      const loaded = readJson(requestsPath);
      if (loaded === null && failHard && fs.existsSync(requestsPath)) {
        throw new Error(`delayed requests file is unreadable or malformed: ${requestsPath}`);
      }
      const payload = loaded && typeof loaded === "object" && !Array.isArray(loaded) ? loaded : { version: 1, requests: [] };
      const requests = Array.isArray(payload.requests) ? payload.requests : [];
      const id = makeRequestId(kind, normalizedMessage);
      if (requests.some((r) => r && String(r.id || "") === id && r.status === "pending")) {
        return false;
      }
      requests.push({
        id,
        created_at: (/* @__PURE__ */ new Date()).toISOString(),
        source,
        kind,
        priority,
        status: "pending",
        message: normalizedMessage
      });
      payload.version = 1;
      payload.requests = requests;
      writeJson(requestsPath, payload);
      return true;
    });
  } catch (err) {
    const detail = `[quaid] delayed requests queue failed path=${requestsPath}: ${String(err?.message || err)}`;
    if (failHard) {
      const cause = err instanceof Error ? err : new Error(String(err));
      throw new Error(detail, { cause });
    }
    warnDelayed(detail);
    return false;
  }
}
function resolveDelayedRequests(requestsPath, ids, resolutionNote = "resolved by agent") {
  if (!Array.isArray(ids) || !ids.length) return 0;
  return withRequestsLock(requestsPath, () => {
    const idSet = new Set(ids.map((x) => String(x)));
    const loaded = readJson(requestsPath);
    const payload = loaded && typeof loaded === "object" && !Array.isArray(loaded) ? loaded : { version: 1, requests: [] };
    const requests = Array.isArray(payload.requests) ? payload.requests : [];
    let changed = 0;
    for (const req of requests) {
      if (!req || req.status !== "pending") continue;
      if (!idSet.has(String(req.id || ""))) continue;
      req.status = "resolved";
      req.resolved_at = (/* @__PURE__ */ new Date()).toISOString();
      req.resolution_note = resolutionNote;
      changed += 1;
    }
    payload.requests = requests;
    writeJson(requestsPath, payload);
    return changed;
  });
}
function clearResolvedRequests(requestsPath) {
  return withRequestsLock(requestsPath, () => {
    const loaded = readJson(requestsPath);
    const payload = loaded && typeof loaded === "object" && !Array.isArray(loaded) ? loaded : { version: 1, requests: [] };
    const requests = Array.isArray(payload.requests) ? payload.requests : [];
    const before = requests.length;
    const kept = requests.filter((r) => r && r.status !== "resolved");
    payload.requests = kept;
    writeJson(requestsPath, payload);
    return Math.max(0, before - kept.length);
  });
}
function drainPendingRequests(requestsPath, limit = 50, resolutionNote = "drained by openclaw prompt hook") {
  const maxItems = Math.max(1, Math.min(Number(limit) || 50, 500));
  return withRequestsLock(requestsPath, () => {
    const loaded = readJson(requestsPath);
    const payload = loaded && typeof loaded === "object" && !Array.isArray(loaded) ? loaded : { version: 1, requests: [] };
    const requests = Array.isArray(payload.requests) ? payload.requests : [];
    const pending = sortPendingRequests(
      requests.filter(
        (item) => Boolean(item) && String(item.status || "pending").trim().toLowerCase() === "pending"
      )
    );
    if (!pending.length) {
      return [];
    }
    const selectedIds = new Set(
      pending.slice(0, maxItems).map((item) => String(item.id || "").trim()).filter(Boolean)
    );
    if (!selectedIds.size) {
      return [];
    }
    const drainedAt = (/* @__PURE__ */ new Date()).toISOString();
    const drained = [];
    for (const item of requests) {
      if (!item || String(item.status || "pending").trim().toLowerCase() !== "pending") {
        continue;
      }
      const itemId = String(item.id || "").trim();
      if (!selectedIds.has(itemId)) {
        continue;
      }
      item.status = "resolved";
      item.resolved_at = drainedAt;
      item.resolution_note = resolutionNote;
      drained.push({ ...item });
    }
    payload.version = 1;
    payload.requests = requests;
    writeJson(requestsPath, payload);
    return sortPendingRequests(drained);
  });
}
export {
  clearResolvedRequests,
  drainPendingRequests,
  queueDelayedRequest,
  resolveDelayedRequests
};
