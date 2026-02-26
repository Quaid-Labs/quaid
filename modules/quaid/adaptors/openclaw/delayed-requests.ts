import * as fs from "node:fs";

type RequestItem = {
  id: string;
  created_at: string;
  source: string;
  kind: string;
  priority: string;
  status: "pending" | "resolved";
  message: string;
  resolved_at?: string;
  resolution_note?: string;
};

type RequestsPayload = {
  version: number;
  requests: RequestItem[];
};

function warnDelayed(message: string): void {
  try { console.warn(message); } catch {}
}

function readJson(path: string): any {
  try {
    if (!fs.existsSync(path)) return null;
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } catch (err: unknown) {
    warnDelayed(`[quaid] delayed requests read failed path=${path}: ${String((err as Error)?.message || err)}`);
    return null;
  }
}

function writeJson(path: string, payload: any): void {
  const tmpPath = `${path}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2), { mode: 0o600 });
  fs.renameSync(tmpPath, path);
}

function _sleepMs(ms: number): void {
  const i32 = new Int32Array(new SharedArrayBuffer(4));
  Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
}

function withRequestsLock<T>(requestsPath: string, fn: () => T): T {
  const lockPath = `${requestsPath}.lock`;
  let fd: number | undefined;
  let lastErr: unknown;
  for (let attempt = 0; attempt < 50; attempt++) {
    try {
      fd = fs.openSync(lockPath, "wx", 0o600);
      break;
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException)?.code;
      if (code !== "EEXIST") throw err;
      lastErr = err;
      _sleepMs(10);
    }
  }
  if (fd === undefined) {
    throw new Error(`failed to acquire delayed-requests lock: ${String((lastErr as Error)?.message || lastErr)}`);
  }
  try {
    return fn();
  } finally {
    try { fs.closeSync(fd); } catch {}
    try { fs.unlinkSync(lockPath); } catch {}
  }
}

function makeRequestId(kind: string, message: string): string {
  return `${kind}-${Buffer.from(message).toString("base64").slice(0, 16)}`;
}

export function queueDelayedRequest(
  requestsPath: string,
  message: string,
  kind: string = "janitor",
  priority: string = "normal",
  source: string = "quaid_adapter"
): boolean {
  try {
    return withRequestsLock(requestsPath, () => {
      const normalizedMessage = String(message || "").trim();
      if (!normalizedMessage) return false;
      const loaded = readJson(requestsPath);
      const payload = (loaded && typeof loaded === "object" && !Array.isArray(loaded)
        ? loaded
        : { version: 1, requests: [] }) as RequestsPayload;
      const requests = Array.isArray(payload.requests) ? payload.requests : [];
      const id = makeRequestId(kind, normalizedMessage);
      if (requests.some((r: any) => r && String(r.id || "") === id && r.status === "pending")) {
        return false;
      }
      requests.push({
        id,
        created_at: new Date().toISOString(),
        source,
        kind,
        priority,
        status: "pending",
        message: normalizedMessage,
      });
      payload.version = 1;
      payload.requests = requests;
      writeJson(requestsPath, payload);
      return true;
    });
  } catch (err: unknown) {
    warnDelayed(`[quaid] delayed requests queue failed path=${requestsPath}: ${String((err as Error)?.message || err)}`);
    return false;
  }
}

export function resolveDelayedRequests(
  requestsPath: string,
  ids: string[],
  resolutionNote: string = "resolved by agent"
): number {
  if (!Array.isArray(ids) || !ids.length) return 0;
  return withRequestsLock(requestsPath, () => {
    const idSet = new Set(ids.map((x) => String(x)));
    const loaded = readJson(requestsPath);
    const payload = (loaded && typeof loaded === "object" && !Array.isArray(loaded)
      ? loaded
      : { version: 1, requests: [] }) as RequestsPayload;
    const requests = Array.isArray(payload.requests) ? payload.requests : [];
    let changed = 0;
    for (const req of requests) {
      if (!req || req.status !== "pending") continue;
      if (!idSet.has(String(req.id || ""))) continue;
      req.status = "resolved";
      req.resolved_at = new Date().toISOString();
      req.resolution_note = resolutionNote;
      changed += 1;
    }
    payload.requests = requests;
    writeJson(requestsPath, payload);
    return changed;
  });
}

export function clearResolvedRequests(requestsPath: string): number {
  return withRequestsLock(requestsPath, () => {
    const loaded = readJson(requestsPath);
    const payload = (loaded && typeof loaded === "object" && !Array.isArray(loaded)
      ? loaded
      : { version: 1, requests: [] }) as RequestsPayload;
    const requests = Array.isArray(payload.requests) ? payload.requests : [];
    const before = requests.length;
    const kept = requests.filter((r) => r && r.status !== "resolved");
    payload.requests = kept;
    writeJson(requestsPath, payload);
    return Math.max(0, before - kept.length);
  });
}
