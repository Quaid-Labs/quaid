import type { execSync as execSyncType } from "node:child_process";
import type * as fsType from "node:fs";
import type * as pathType from "node:path";

type ProjectCatalogReaderDeps = {
  workspace: string;
  docsRegistryScript: string;
  fs: typeof fsType;
  path: typeof pathType;
  execSync: typeof execSyncType;
};

function firstUsefulLine(content: string): string {
  return String(content || "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith("#") && !line.startsWith("|")) || "";
}

function getProjectDescriptionFromToolsMd(
  deps: ProjectCatalogReaderDeps,
  homeDir?: string,
): string {
  try {
    if (!homeDir) return "";
    const toolsPath = deps.path.join(deps.workspace, homeDir, "TOOLS.md");
    if (!deps.fs.existsSync(toolsPath)) return "";
    const content = deps.fs.readFileSync(toolsPath, "utf8");
    const m = content.match(/^\s*(?:Project\s+Description|Description)\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch {
    return "";
  }
}

function getProjectDescriptionFromProjectMd(
  deps: ProjectCatalogReaderDeps,
  homeDir?: string,
): string {
  try {
    if (!homeDir) return "";
    const projectPath = deps.path.join(deps.workspace, homeDir, "PROJECT.md");
    if (!deps.fs.existsSync(projectPath)) return "";
    const content = deps.fs.readFileSync(projectPath, "utf8");
    const m = content.match(/^\s*Description\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch {
    return "";
  }
}

export function createProjectCatalogReader(deps: ProjectCatalogReaderDeps) {
  function getProjectNames(): string[] {
    try {
      const output = deps.execSync(
        `python3 "${deps.docsRegistryScript}" list-projects --names-only`,
        {
          encoding: "utf-8",
          env: {
            ...process.env,
            QUAID_HOME: deps.workspace,
            CLAWDBOT_WORKSPACE: deps.workspace,
          },
          timeout: 10_000,
        },
      ).trim();
      return output.split("\n").filter(Boolean);
    } catch {
      try {
        const configPath = deps.path.join(deps.workspace, "config/memory.json");
        const configData = JSON.parse(deps.fs.readFileSync(configPath, "utf-8"));
        return Object.keys(configData?.projects?.definitions || {});
      } catch {
        return [];
      }
    }
  }

  function getProjectCatalog(): Array<{ name: string; description: string }> {
    try {
      const configPath = deps.path.join(deps.workspace, "config/memory.json");
      const configData = JSON.parse(deps.fs.readFileSync(configPath, "utf-8"));
      const defs = configData?.projects?.definitions || {};
      return Object.entries(defs).map(([name, def]: [string, any]) => {
        const description = String(def?.description || "").trim()
          || getProjectDescriptionFromToolsMd(deps, String(def?.homeDir || "").trim())
          || getProjectDescriptionFromProjectMd(deps, String(def?.homeDir || "").trim())
          || "No description";
        return { name, description };
      });
    } catch {
      return getProjectNames().map((name) => ({ name, description: "No description" }));
    }
  }

  return {
    getProjectNames,
    getProjectCatalog,
  };
}
