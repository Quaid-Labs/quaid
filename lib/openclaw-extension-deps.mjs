import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

function dependencyNames(rootDir) {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(rootDir, "package.json"), "utf8"));
    const deps = pkg && typeof pkg.dependencies === "object" && pkg.dependencies
      ? Object.keys(pkg.dependencies)
      : [];
    return deps.length > 0 ? deps : ["@sinclair/typebox"];
  } catch {
    return ["@sinclair/typebox"];
  }
}

function packagePath(rootDir, packageName) {
  return path.join(rootDir, "node_modules", ...String(packageName || "").split("/").filter(Boolean));
}

function hasRuntimeDeps(rootDir) {
  return dependencyNames(rootDir).every((name) => fs.existsSync(path.join(packagePath(rootDir, name), "package.json")));
}

function copyRuntimeDepsFromSource(sourceDir, extensionDir) {
  if (!sourceDir) return false;
  let copied = false;
  for (const name of dependencyNames(extensionDir)) {
    const src = packagePath(sourceDir, name);
    if (!fs.existsSync(path.join(src, "package.json"))) {
      continue;
    }
    const dest = packagePath(extensionDir, name);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.cpSync(src, dest, {
      recursive: true,
      force: true,
      dereference: true,
    });
    copied = true;
  }
  return copied;
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
  if (pluginNodeModules && fs.existsSync(pluginNodeModules)) {
    copyRuntimeDepsFromSource(pluginDir, extensionDir);
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
