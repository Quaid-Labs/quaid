import fs from "node:fs";

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function normalizeMatrixRoomMap(value) {
  if (!isRecord(value)) return value;
  const next = cloneJson(value);
  for (const room of Object.values(next)) {
    if (!isRecord(room) || !Object.prototype.hasOwnProperty.call(room, "allow")) continue;
    if (!Object.prototype.hasOwnProperty.call(room, "enabled")) {
      room.enabled = !!room.allow;
    }
    delete room.allow;
  }
  return next;
}

function normalizeMatrixChannelConfig(value) {
  if (!isRecord(value)) return null;
  const next = cloneJson(value);
  if (Object.prototype.hasOwnProperty.call(next, "allowPrivateNetwork")) {
    if (!isRecord(next.network)) next.network = {};
    if (next.network.dangerouslyAllowPrivateNetwork === undefined) {
      next.network.dangerouslyAllowPrivateNetwork = !!next.allowPrivateNetwork;
    }
    delete next.allowPrivateNetwork;
  }
  next.groups = normalizeMatrixRoomMap(next.groups);
  next.rooms = normalizeMatrixRoomMap(next.rooms);
  if (isRecord(next.accounts)) {
    for (const account of Object.values(next.accounts)) {
      if (!isRecord(account)) continue;
      if (Object.prototype.hasOwnProperty.call(account, "allowPrivateNetwork")) {
        if (!isRecord(account.network)) account.network = {};
        if (account.network.dangerouslyAllowPrivateNetwork === undefined) {
          account.network.dangerouslyAllowPrivateNetwork = !!account.allowPrivateNetwork;
        }
        delete account.allowPrivateNetwork;
      }
      account.groups = normalizeMatrixRoomMap(account.groups);
      account.rooms = normalizeMatrixRoomMap(account.rooms);
    }
  }
  return next;
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

export function captureOpenClawMatrixConfig(configPath, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return null;

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  const plugins = isRecord(parsed.plugins) ? parsed.plugins : {};
  const channels = isRecord(parsed.channels) ? parsed.channels : {};
  const entries = isRecord(plugins.entries) ? plugins.entries : {};
  const allow = Array.isArray(plugins.allow)
    ? plugins.allow.map((entry) => String(entry || "").trim()).filter(Boolean)
    : [];

  const matrixEntry = isRecord(entries.matrix) ? cloneJson(entries.matrix) : null;
  const matrixChannel = normalizeMatrixChannelConfig(channels.matrix);
  const allowMatrix = allow.includes("matrix");

  if (!allowMatrix && !matrixEntry && !matrixChannel) {
    return null;
  }

  return {
    allowMatrix,
    matrixEntry,
    matrixChannel,
  };
}

export function restoreOpenClawMatrixConfig(configPath, snapshot, fsImpl = fs) {
  const cfgPath = String(configPath || "").trim();
  if (!cfgPath) return { changed: false, reason: "missing-config-path" };
  if (!snapshot || typeof snapshot !== "object") return { changed: false, reason: "missing-snapshot" };

  const raw = fsImpl.readFileSync(cfgPath, "utf8");
  const parsed = JSON.parse(raw);
  if (!isRecord(parsed.plugins)) parsed.plugins = {};

  const plugins = parsed.plugins;
  let changed = false;
  let restoredAllow = false;
  let restoredEntry = false;
  let restoredChannel = false;

  if (snapshot.allowMatrix) {
    const allow = Array.isArray(plugins.allow)
      ? plugins.allow.map((entry) => String(entry || "").trim()).filter(Boolean)
      : [];
    if (!allow.includes("matrix")) {
      allow.push("matrix");
      plugins.allow = allow;
      restoredAllow = true;
      changed = true;
    }
  }

  if (snapshot.matrixEntry) {
    if (!isRecord(plugins.entries)) plugins.entries = {};
    const current = isRecord(plugins.entries.matrix) ? plugins.entries.matrix : null;
    const nextRaw = JSON.stringify(snapshot.matrixEntry);
    const currentRaw = current ? JSON.stringify(current) : "";
    if (currentRaw !== nextRaw) {
      plugins.entries.matrix = cloneJson(snapshot.matrixEntry);
      restoredEntry = true;
      changed = true;
    }
  }

  if (snapshot.matrixChannel) {
    if (!isRecord(parsed.channels)) parsed.channels = {};
    const current = isRecord(parsed.channels.matrix) ? parsed.channels.matrix : null;
    const nextRaw = JSON.stringify(snapshot.matrixChannel);
    const currentRaw = current ? JSON.stringify(current) : "";
    if (currentRaw !== nextRaw) {
      parsed.channels.matrix = cloneJson(snapshot.matrixChannel);
      restoredChannel = true;
      changed = true;
    }
  }

  if (!changed) {
    return { changed: false, reason: "already-restored", restoredAllow, restoredEntry, restoredChannel };
  }

  writeJsonAtomically(fsImpl, cfgPath, parsed);
  return { changed: true, restoredAllow, restoredEntry, restoredChannel };
}
