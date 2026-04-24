import fs from "node:fs";

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function ensureOpenClawAgentModelDefault(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return { changed: false, reason: "missing-config-path" };

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  const agents = isRecord(parsed.agents) ? parsed.agents : null;
  const defaults = agents && isRecord(agents.defaults) ? agents.defaults : null;
  const legacyPrimary = String(defaults?.modelPrimary || "").trim();
  const nestedPrimary = String(defaults?.model?.primary || "").trim();

  if (!legacyPrimary) {
    return { changed: false, reason: nestedPrimary ? "already-configured" : "no-legacy-model-primary" };
  }

  if (!isRecord(parsed.agents)) {
    parsed.agents = {};
  }
  if (!isRecord(parsed.agents.defaults)) {
    parsed.agents.defaults = {};
  }
  if (!isRecord(parsed.agents.defaults.model)) {
    parsed.agents.defaults.model = {};
  }
  delete parsed.agents.defaults.modelPrimary;
  if (!nestedPrimary) {
    parsed.agents.defaults.model.primary = legacyPrimary;
  }

  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    fsImpl.writeFileSync(tmpPath, `${JSON.stringify(parsed, null, 2)}\n`, "utf8");
    fsImpl.renameSync(tmpPath, cfgPath);
  } finally {
    try {
      if (fsImpl.existsSync(tmpPath)) fsImpl.unlinkSync(tmpPath);
    } catch {}
  }
  return { changed: true, primary: String(parsed.agents.defaults.model.primary || "").trim(), migratedLegacy: true };
}
