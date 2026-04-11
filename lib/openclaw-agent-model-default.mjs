import fs from "node:fs";
import path from "node:path";

export const OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY = "openai-codex/gpt-5.4";

export function ensureOpenClawAgentModelDefault(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return { changed: false, reason: "missing-config-path" };

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  if (!parsed.agents || typeof parsed.agents !== "object" || Array.isArray(parsed.agents)) {
    parsed.agents = {};
  }
  if (!parsed.agents.defaults || typeof parsed.agents.defaults !== "object" || Array.isArray(parsed.agents.defaults)) {
    parsed.agents.defaults = {};
  }

  const defaults = parsed.agents.defaults;
  let changed = false;
  if (Object.prototype.hasOwnProperty.call(defaults, "modelPrimary")) {
    delete defaults.modelPrimary;
    changed = true;
  }

  if (!defaults.model || typeof defaults.model !== "object" || Array.isArray(defaults.model)) {
    defaults.model = {};
    changed = true;
  }
  const currentNestedPrimary = String(defaults.model.primary || "").trim();
  if (currentNestedPrimary !== OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY) {
    defaults.model.primary = OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY;
    changed = true;
  }

  if (!changed) return { changed: false, reason: "already-configured" };

  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    fsImpl.writeFileSync(tmpPath, `${JSON.stringify(parsed, null, 2)}\n`, "utf8");
    fsImpl.renameSync(tmpPath, cfgPath);
  } finally {
    try {
      if (fsImpl.existsSync(tmpPath)) fsImpl.unlinkSync(tmpPath);
    } catch {}
  }
  return { changed: true, primary: OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY };
}
