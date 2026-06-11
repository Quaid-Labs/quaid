import fs from "node:fs";

const MESSAGE_TOOL_ID = "message";

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function normalizeStringList(value) {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => String(entry || "").trim()).filter(Boolean);
}

function listsEqual(left, right) {
  if (left.length !== right.length) return false;
  return left.every((entry, index) => entry === right[index]);
}

function ensureMessageInPolicy(policy) {
  const hasAllow = Array.isArray(policy.allow) && policy.allow.length > 0;
  const key = hasAllow ? "allow" : "alsoAllow";
  const before = normalizeStringList(policy[key]);
  const after = before.includes(MESSAGE_TOOL_ID) ? before : [...before, MESSAGE_TOOL_ID];
  if (Array.isArray(policy[key]) && listsEqual(policy[key], after)) {
    return { changed: false, key };
  }
  policy[key] = after;
  return { changed: true, key };
}

export function ensureOpenClawMessageToolAllowed(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return { changed: false, reason: "missing-config-path" };

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  if (!isRecord(parsed.tools)) {
    parsed.tools = {};
  }

  // OC 2026.6.5 requires the core message tool for Matrix channel delivery;
  // the coding profile otherwise strips it before the agent can reply.
  const result = ensureMessageInPolicy(parsed.tools);
  if (!result.changed) return { changed: false, reason: "already-allowed", key: result.key };

  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    fsImpl.writeFileSync(tmpPath, `${JSON.stringify(parsed, null, 2)}\n`, "utf8");
    fsImpl.renameSync(tmpPath, cfgPath);
  } finally {
    try {
      if (fsImpl.existsSync(tmpPath)) fsImpl.unlinkSync(tmpPath);
    } catch {}
  }
  return { changed: true, key: result.key };
}
