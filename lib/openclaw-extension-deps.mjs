import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

const TYPEBOX_SENTINEL = path.join("@sinclair", "typebox", "package.json");

function hasRuntimeDeps(rootDir) {
  return fs.existsSync(path.join(rootDir, "node_modules", TYPEBOX_SENTINEL));
}

export function ensureOpenClawExtensionDependencies({
  extensionDir,
  pluginDir,
  spawn = spawnSync,
} = {}) {
  if (!extensionDir || !fs.existsSync(path.join(extensionDir, "package.json"))) {
    return { ok: false, reason: "extension package.json missing" };
  }
  if (hasRuntimeDeps(extensionDir)) {
    return { ok: true, source: "existing" };
  }

  const pluginNodeModules = pluginDir ? path.join(pluginDir, "node_modules") : "";
  if (pluginNodeModules && hasRuntimeDeps(pluginDir)) {
    fs.cpSync(pluginNodeModules, path.join(extensionDir, "node_modules"), {
      recursive: true,
      force: true,
      dereference: true,
    });
    if (hasRuntimeDeps(extensionDir)) {
      return { ok: true, source: "copied" };
    }
  }

  const npmResult = spawn("npm", ["install", "--omit=dev", "--omit=peer", "--no-audit", "--no-fund"], {
    cwd: extensionDir,
    stdio: "pipe",
    timeout: 120000,
    encoding: "utf8",
  });
  if (npmResult.status !== 0) {
    const detail = String(npmResult.stderr || npmResult.stdout || "").trim();
    return { ok: false, reason: detail || "npm install failed" };
  }
  if (!hasRuntimeDeps(extensionDir)) {
    return { ok: false, reason: "runtime deps still missing after npm install" };
  }
  return { ok: true, source: "installed" };
}
