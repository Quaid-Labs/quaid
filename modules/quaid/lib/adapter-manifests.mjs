import fs from "node:fs";
import path from "node:path";

export const ADAPTER_MANIFEST_SCHEMA = "quaid-adapter-install/v1";

function normalizeId(value) {
  return String(value || "").trim().toLowerCase();
}

export function adapterRegistryDir(workspace) {
  return path.join(String(workspace || "").trim(), ".quaid", "adaptors");
}

export function validateAdapterManifest(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    return { ok: false, error: "manifest must be an object" };
  }
  const schema = String(raw.schema || "").trim();
  if (!schema) return { ok: false, error: "manifest.schema is required" };
  if (schema !== ADAPTER_MANIFEST_SCHEMA) {
    return { ok: false, error: `unsupported schema: ${schema}` };
  }
  const id = normalizeId(raw.id);
  if (!id) return { ok: false, error: "manifest.id is required" };
  if (!/^[a-z0-9][a-z0-9._-]{1,63}$/.test(id)) {
    return { ok: false, error: `invalid adapter id: ${id}` };
  }
  const install = raw.install;
  if (!install || typeof install !== "object" || Array.isArray(install)) {
    return { ok: false, error: "manifest.install is required" };
  }
  const label = String(install.selectLabel || raw.name || "").trim();
  if (!label) return { ok: false, error: "manifest.install.selectLabel (or manifest.name) is required" };
  const runtime = raw.runtime;
  if (!runtime || typeof runtime !== "object" || Array.isArray(runtime)) {
    return { ok: false, error: "manifest.runtime is required" };
  }
  const runtimePy = runtime.python;
  if (!runtimePy || typeof runtimePy !== "object" || Array.isArray(runtimePy)) {
    return { ok: false, error: "manifest.runtime.python is required" };
  }
  const runtimeModule = String(runtimePy.module || "").trim();
  if (!runtimeModule) {
    return { ok: false, error: "manifest.runtime.python.module is required" };
  }
  const runtimeClass = String(runtimePy.class || "").trim();
  if (!runtimeClass) {
    return { ok: false, error: "manifest.runtime.python.class is required" };
  }
  return { ok: true };
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

export function syncBuiltinAdapterManifests({ workspace, installerDir }) {
  const root = adapterRegistryDir(workspace);
  const candidateDirs = [
    path.join(installerDir, "adaptors", "manifests"),
    path.join(installerDir, "modules", "quaid", "adaptors", "manifests"),
  ];
  const sourceDir = candidateDirs.find((d) => fs.existsSync(d));
  if (!sourceDir || !fs.existsSync(sourceDir)) return [];
  const installed = [];
  for (const ent of fs.readdirSync(sourceDir, { withFileTypes: true })) {
    if (!ent.isFile() || !ent.name.endsWith(".json")) continue;
    const srcPath = path.join(sourceDir, ent.name);
    const parsed = readJson(srcPath);
    const check = validateAdapterManifest(parsed);
    if (!check.ok) continue;
    const id = normalizeId(parsed.id);
    const destDir = path.join(root, id);
    const destPath = path.join(destDir, "adapter.json");
    fs.mkdirSync(destDir, { recursive: true });
    fs.copyFileSync(srcPath, destPath);
    const srcHooksDir = path.join(path.dirname(srcPath), "hooks");
    const destHooksDir = path.join(destDir, "hooks");
    if (fs.existsSync(srcHooksDir) && fs.statSync(srcHooksDir).isDirectory()) {
      fs.mkdirSync(destHooksDir, { recursive: true });
      fs.cpSync(srcHooksDir, destHooksDir, { recursive: true, force: true });
    }
    installed.push(destPath);
  }
  return installed;
}

export function loadAdapterManifests(workspace) {
  const root = adapterRegistryDir(workspace);
  if (!fs.existsSync(root)) return [];
  const manifests = [];
  for (const ent of fs.readdirSync(root, { withFileTypes: true })) {
    if (!ent.isDirectory()) continue;
    const manifestPath = path.join(root, ent.name, "adapter.json");
    if (!fs.existsSync(manifestPath)) continue;
    try {
      const parsed = readJson(manifestPath);
      const check = validateAdapterManifest(parsed);
      if (!check.ok) continue;
      manifests.push({
        ...parsed,
        id: normalizeId(parsed.id),
        __path: manifestPath,
      });
    } catch {
      // ignore malformed third-party manifest files
    }
  }
  manifests.sort((a, b) => {
    const sa = Number(a?.install?.sortOrder || 999);
    const sb = Number(b?.install?.sortOrder || 999);
    if (sa !== sb) return sa - sb;
    return String(a.id).localeCompare(String(b.id));
  });
  return manifests;
}

export function adapterSelectOptions(manifests) {
  const list = Array.isArray(manifests) ? manifests : [];
  return list
    .filter((m) => m && m.install && m.install.selectLabel)
    .map((m) => ({
      value: m.id,
      label: String(m.install.selectLabel || m.name || m.id),
      hint: String(m.install.selectHint || "").trim(),
    }));
}

export function resolveAdapterHookScript(manifest, hookName) {
  const scriptRel = String(manifest?.scripts?.[hookName] || "").trim();
  if (!scriptRel) return "";
  const base = path.dirname(String(manifest.__path || ""));
  if (!base) return "";
  const abs = path.resolve(base, scriptRel);
  if (!abs.startsWith(base)) return "";
  return abs;
}
