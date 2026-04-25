import fs from "node:fs";

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
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

function normalizeAllow(value) {
  return Array.isArray(value)
    ? value.map((entry) => String(entry || "").trim()).filter(Boolean)
    : [];
}

function ensureAllowId(plugins, pluginId, changedBits) {
  const allow = normalizeAllow(plugins.allow);
  if (allow.includes(pluginId)) return false;
  allow.push(pluginId);
  plugins.allow = allow;
  changedBits.push(`plugins.allow:${pluginId}`);
  return true;
}

export function captureOpenClawManagedState(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return null;

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  const plugins = isRecord(parsed.plugins) ? parsed.plugins : {};
  const entries = isRecord(plugins.entries) ? plugins.entries : {};
  const channels = isRecord(parsed.channels) ? parsed.channels : {};
  const allow = normalizeAllow(plugins.allow);

  const snapshot = {
    requiredAllow: [],
    entries: {},
    channels: {},
    agents: {},
  };

  if (allow.includes("quaid") || isRecord(entries.quaid)) {
    snapshot.requiredAllow.push("quaid");
  }
  if (allow.includes("matrix") || isRecord(entries.matrix) || isRecord(channels.matrix)) {
    snapshot.requiredAllow.push("matrix");
  }
  if (isRecord(entries.quaid)) {
    snapshot.entries.quaid = cloneJson(entries.quaid);
  }
  if (isRecord(entries.matrix)) {
    snapshot.entries.matrix = cloneJson(entries.matrix);
  }
  if (isRecord(channels.matrix)) {
    snapshot.channels.matrix = cloneJson(channels.matrix);
  }

  const defaultPrimary = String(parsed?.agents?.defaults?.model?.primary || "").trim();
  if (defaultPrimary) {
    snapshot.agents.defaultPrimary = defaultPrimary;
  }
  const listPrimaries = {};
  const agentList = Array.isArray(parsed?.agents?.list) ? parsed.agents.list : [];
  for (const agent of agentList) {
    if (!isRecord(agent)) continue;
    const agentId = String(agent.id || "").trim();
    const primary = String(agent?.model?.primary || "").trim();
    if (!agentId || !primary) continue;
    listPrimaries[agentId] = primary;
  }
  if (Object.keys(listPrimaries).length > 0) {
    snapshot.agents.listPrimaries = listPrimaries;
  }

  const hasManagedState = snapshot.requiredAllow.length > 0
    || Object.keys(snapshot.entries).length > 0
    || Object.keys(snapshot.channels).length > 0
    || Object.keys(snapshot.agents).length > 0;
  return hasManagedState ? snapshot : null;
}

export function reconcileOpenClawManagedStateObject(currentConfig, snapshot) {
  const parsed = cloneJson(currentConfig || {});
  const snap = isRecord(snapshot) ? snapshot : {};
  const changedBits = [];

  if (!isRecord(parsed.plugins)) parsed.plugins = {};
  const plugins = parsed.plugins;

  for (const pluginId of Array.isArray(snap.requiredAllow) ? snap.requiredAllow : []) {
    ensureAllowId(plugins, String(pluginId || "").trim(), changedBits);
  }

  if (isRecord(snap.entries)) {
    if (!isRecord(plugins.entries)) plugins.entries = {};
    for (const [pluginId, entry] of Object.entries(snap.entries)) {
      if (!isRecord(entry)) continue;
      const current = isRecord(plugins.entries[pluginId]) ? plugins.entries[pluginId] : null;
      const nextRaw = JSON.stringify(entry);
      const currentRaw = current ? JSON.stringify(current) : "";
      if (currentRaw !== nextRaw) {
        plugins.entries[pluginId] = cloneJson(entry);
        changedBits.push(`plugins.entries.${pluginId}`);
      }
    }
  }

  if (isRecord(snap.channels) && isRecord(snap.channels.matrix)) {
    if (!isRecord(parsed.channels)) parsed.channels = {};
    const current = isRecord(parsed.channels.matrix) ? parsed.channels.matrix : null;
    const nextRaw = JSON.stringify(snap.channels.matrix);
    const currentRaw = current ? JSON.stringify(current) : "";
    if (currentRaw !== nextRaw) {
      parsed.channels.matrix = cloneJson(snap.channels.matrix);
      changedBits.push("channels.matrix");
    }
  }

  if (isRecord(snap.agents)) {
    const defaultPrimary = String(snap.agents.defaultPrimary || "").trim();
    if (defaultPrimary) {
      if (!isRecord(parsed.agents)) parsed.agents = {};
      if (!isRecord(parsed.agents.defaults)) parsed.agents.defaults = {};
      if (!isRecord(parsed.agents.defaults.model)) parsed.agents.defaults.model = {};
      if (String(parsed.agents.defaults.model.primary || "").trim() !== defaultPrimary) {
        parsed.agents.defaults.model.primary = defaultPrimary;
        changedBits.push("agents.defaults.model.primary");
      }
    }

    const listPrimaries = isRecord(snap.agents.listPrimaries) ? snap.agents.listPrimaries : {};
    if (Object.keys(listPrimaries).length > 0) {
      if (!isRecord(parsed.agents)) parsed.agents = {};
      if (!Array.isArray(parsed.agents.list)) parsed.agents.list = [];
      for (const agent of parsed.agents.list) {
        if (!isRecord(agent)) continue;
        const agentId = String(agent.id || "").trim();
        if (!agentId) continue;
        const desired = String(listPrimaries[agentId] || "").trim();
        if (!desired) continue;
        if (!isRecord(agent.model)) agent.model = {};
        if (String(agent.model.primary || "").trim() === desired) continue;
        agent.model.primary = desired;
        changedBits.push(`agents.list.${agentId}.model.primary`);
      }
    }
  }

  return {
    changed: changedBits.length > 0,
    changedBits,
    config: parsed,
  };
}

export function restoreOpenClawManagedState(configPath, snapshot, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return { changed: false, reason: "missing-config-path", changedBits: [] };
  if (!snapshot || typeof snapshot !== "object") return { changed: false, reason: "missing-snapshot", changedBits: [] };

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  const result = reconcileOpenClawManagedStateObject(parsed, snapshot);
  if (!result.changed) {
    return { changed: false, reason: "already-restored", changedBits: [] };
  }

  writeJsonAtomically(fsImpl, cfgPath, result.config);
  return { changed: true, changedBits: result.changedBits };
}

export function readOpenClawManagedStateSnapshot(snapshotPath, fsImpl = fs) {
  const cfgPath = String(snapshotPath || "").trim();
  if (!cfgPath) return null;
  if (!fsImpl.existsSync(cfgPath)) return null;
  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  return JSON.parse(raw);
}

export function writeOpenClawManagedStateSnapshot(snapshotPath, snapshot, fsImpl = fs) {
  const cfgPath = String(snapshotPath || "").trim();
  if (!cfgPath) return false;
  if (!snapshot || typeof snapshot !== "object") return false;
  writeJsonAtomically(fsImpl, cfgPath, snapshot);
  return true;
}
