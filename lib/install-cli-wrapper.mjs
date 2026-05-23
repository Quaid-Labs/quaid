import fs from "node:fs";
import path from "node:path";

export function validQuaidCliFile(filePath) {
  try {
    const stat = fs.statSync(filePath);
    return stat.isFile() && stat.size > 0;
  } catch {
    return false;
  }
}

export function ensureInstalledQuaidCli(sourcePluginDir, installedPluginDir, options = {}) {
  const sourceCli = path.join(sourcePluginDir, "quaid");
  const installedCli = path.join(installedPluginDir, "quaid");
  if (!validQuaidCliFile(sourceCli)) {
    throw new Error(`quaid CLI wrapper missing or empty in plugin source: ${sourceCli}`);
  }

  if (!validQuaidCliFile(installedCli)) {
    fs.copyFileSync(sourceCli, installedCli);
    const message = `Restored missing or empty installed quaid CLI wrapper: ${installedCli}`;
    if (options.log?.warn) {
      options.log.warn(message);
    } else {
      options.log?.info?.(message);
    }
  }

  fs.chmodSync(installedCli, 0o755);

  if (!validQuaidCliFile(installedCli)) {
    throw new Error(`quaid CLI wrapper missing or empty after install: ${installedCli}`);
  }
}
