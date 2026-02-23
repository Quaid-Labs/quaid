import * as fs from "node:fs";
function readJson(path) {
  try {
    if (!fs.existsSync(path)) return null;
    return JSON.parse(fs.readFileSync(path, "utf8"));
  } catch {
    return null;
  }
}
function writeJson(path, payload) {
  fs.writeFileSync(path, JSON.stringify(payload, null, 2), { mode: 384 });
}
function makeRequestId(kind, message) {
  return `${kind}-${Buffer.from(message).toString("base64").slice(0, 16)}`;
}
function queueDelayedRequest(requestsPath, message, kind = "janitor", priority = "normal", source = "quaid_adapter") {
  try {
    const normalizedMessage = String(message || "").trim();
    if (!normalizedMessage) return false;
    const payload = readJson(requestsPath) || { version: 1, requests: [] };
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
  } catch {
    return false;
  }
}
function resolveDelayedRequests(requestsPath, ids, resolutionNote = "resolved by agent") {
  if (!Array.isArray(ids) || !ids.length) return 0;
  const idSet = new Set(ids.map((x) => String(x)));
  const payload = readJson(requestsPath) || { version: 1, requests: [] };
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
}
function clearResolvedRequests(requestsPath) {
  const payload = readJson(requestsPath) || { version: 1, requests: [] };
  const requests = Array.isArray(payload.requests) ? payload.requests : [];
  const before = requests.length;
  const kept = requests.filter((r) => r && r.status !== "resolved");
  payload.requests = kept;
  writeJson(requestsPath, payload);
  return Math.max(0, before - kept.length);
}
export {
  clearResolvedRequests,
  queueDelayedRequest,
  resolveDelayedRequests
};
