import fs from "node:fs";

export const OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS = Object.freeze([
  "active-memory",
  "memory-core",
  "memory-wiki",
]);

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function writeJsonAtomically(fsImpl, cfgPath, value) {
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    fsImpl.writeFileSync(tmpPath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    fsImpl.renameSync(tmpPath, cfgPath);
  } finally {
    try {
      if (fsImpl.existsSync(tmpPath)) fsImpl.unlinkSync(tmpPath);
    } catch {}
  }
}

export function sanitizeOpenClawNativeMemoryPlugins(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) {
    return { changed: false, reason: "missing-config-path" };
  }

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  if (!isRecord(parsed.plugins)) {
    parsed.plugins = {};
  }

  const plugins = parsed.plugins;
  let changed = false;
  let reboundMemorySlot = false;
  const removedAllow = [];
  const disabledEntries = [];

  if (Array.isArray(plugins.allow)) {
    const allow = plugins.allow
      .map((entry) => String(entry || "").trim())
      .filter(Boolean);
    const nextAllow = allow.filter((entry) => {
      if (!OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS.includes(entry)) return true;
      removedAllow.push(entry);
      return false;
    });
    if (allow.length !== nextAllow.length) {
      plugins.allow = nextAllow;
      changed = true;
    }
  }

  if (!isRecord(plugins.entries)) {
    plugins.entries = {};
  }
  for (const pluginId of OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS) {
    if (!Object.prototype.hasOwnProperty.call(plugins.entries, pluginId)) continue;
    const current = plugins.entries[pluginId];
    if (!isRecord(current)) {
      plugins.entries[pluginId] = { disabled: true };
      disabledEntries.push(pluginId);
      changed = true;
      continue;
    }
    if (current.disabled !== true || Object.prototype.hasOwnProperty.call(current, "enabled")) {
      delete current.enabled;
      current.disabled = true;
      disabledEntries.push(pluginId);
      changed = true;
    }
  }

  if (!isRecord(plugins.slots)) {
    plugins.slots = {};
  }
  if (String(plugins.slots.memory || "").trim() !== "quaid") {
    plugins.slots.memory = "quaid";
    reboundMemorySlot = true;
    changed = true;
  }

  if (!changed) {
    return {
      changed: false,
      reason: "already-sanitized",
      removedAllow,
      disabledEntries,
      reboundMemorySlot,
    };
  }

  writeJsonAtomically(fsImpl, cfgPath, parsed);
  return {
    changed: true,
    removedAllow,
    disabledEntries,
    reboundMemorySlot,
  };
}
