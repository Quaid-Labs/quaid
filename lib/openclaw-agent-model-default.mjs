import fs from "node:fs";
import path from "node:path";

export const OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY = "openai-codex/gpt-5.4";

const OLD_DEFAULT_AGENT_MODELS = new Set([
  "",
  "openai-codex/gpt-5.4-mini",
]);

function shouldReplaceDefault(value) {
  return OLD_DEFAULT_AGENT_MODELS.has(String(value || "").trim());
}

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

  const currentModelPrimary = String(defaults.modelPrimary || "").trim();
  if (shouldReplaceDefault(currentModelPrimary)) {
    defaults.modelPrimary = OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY;
    changed = true;
  }

  const modelObj = defaults.model;
  if (modelObj && typeof modelObj === "object" && !Array.isArray(modelObj)) {
    const currentNestedPrimary = String(modelObj.primary || "").trim();
    if (shouldReplaceDefault(currentNestedPrimary)) {
      modelObj.primary = OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY;
      changed = true;
    }
  } else if (shouldReplaceDefault(currentModelPrimary)) {
    defaults.model = { primary: OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY };
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
  return { changed: true, modelPrimary: OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY };
}
