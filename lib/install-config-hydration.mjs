import fs from "node:fs";
import path from "node:path";

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function deepMergeMissing(defaults, existing) {
  const base = isPlainObject(defaults) ? defaults : {};
  const current = isPlainObject(existing) ? existing : {};
  const result = { ...base };
  for (const [key, value] of Object.entries(current)) {
    if (isPlainObject(result[key]) && isPlainObject(value)) {
      result[key] = deepMergeMissing(result[key], value);
    } else {
      result[key] = value;
    }
  }
  return result;
}

export function readJsonObject(filePath) {
  try {
    if (!fs.existsSync(filePath)) return null;
    const parsed = JSON.parse(fs.readFileSync(filePath, "utf8"));
    return isPlainObject(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function writeJsonObject(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

export function hydrateConfigWithDefaults(existing, defaults) {
  return deepMergeMissing(defaults, existing);
}

export function hydrateInstanceConfigFile(configPath, defaults) {
  const existing = readJsonObject(configPath) || {};
  const merged = hydrateConfigWithDefaults(existing, defaults);
  writeJsonObject(configPath, merged);
  return merged;
}

export function hydratePlatformInstanceConfigs({ instancesDir, platformKey, defaults }) {
  const normalizedPlatform = String(platformKey || "").trim().toLowerCase();
  if (!normalizedPlatform || !fs.existsSync(instancesDir)) return [];
  const prefix = `${normalizedPlatform}-`;
  const hydrated = [];
  for (const entry of fs.readdirSync(instancesDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || !entry.name.startsWith(prefix)) continue;
    const configPath = path.join(instancesDir, entry.name, "config.json");
    hydrateInstanceConfigFile(configPath, defaults);
    hydrated.push(configPath);
  }
  return hydrated;
}
