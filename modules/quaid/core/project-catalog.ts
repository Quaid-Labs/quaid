import type * as fsType from "node:fs";
import type * as pathType from "node:path";

type ProjectCatalogReaderDeps = {
  workspace: string;
  fs: typeof fsType;
  path: typeof pathType;
  getMemoryConfig: () => any;
  isFailHardEnabled?: () => boolean;
};

function warnCatalog(message: string): void {
  try { console.warn(message); } catch {}
}

function firstUsefulLine(content: string): string {
  return String(content || "")
    .split("\n")
    .map((line) => line.trim())
    .find((line) => line && !line.startsWith("#") && !line.startsWith("|")) || "";
}

function resolveVisibleHome(deps: ProjectCatalogReaderDeps): string {
  const explicit = String(process.env.QUAID_VISIBLE_HOME || "").trim();
  if (explicit) return explicit;
  const root = deps.path.resolve(deps.workspace);
  const base = deps.path.basename(root);
  if (base.startsWith(".") && base.length > 1) {
    return deps.path.join(deps.path.dirname(root), base.slice(1));
  }
  return root;
}

function resolveProjectHome(deps: ProjectCatalogReaderDeps, homeDir?: string): string {
  const raw = String(homeDir || "").trim();
  if (!raw) return "";
  if (deps.path.isAbsolute(raw)) return raw;
  if (raw === "projects" || raw.startsWith("projects/")) {
    return deps.path.join(resolveVisibleHome(deps), raw);
  }
  return deps.path.join(deps.workspace, raw);
}

function getProjectDescriptionFromToolsMd(
  deps: ProjectCatalogReaderDeps,
  homeDir?: string,
): string {
  try {
    if (!homeDir) return "";
    const toolsPath = deps.path.join(resolveProjectHome(deps, homeDir), "TOOLS.md");
    if (!deps.fs.existsSync(toolsPath)) return "";
    const content = deps.fs.readFileSync(toolsPath, "utf8");
    const m = content.match(/^\s*(?:Project\s+Description|Description)\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err: unknown) {
    warnCatalog(`[memory] project catalog: TOOLS.md description read failed: ${String((err as Error)?.message || err)}`);
    return "";
  }
}

function getProjectDescriptionFromProjectMd(
  deps: ProjectCatalogReaderDeps,
  homeDir?: string,
): string {
  try {
    if (!homeDir) return "";
    const projectPath = deps.path.join(resolveProjectHome(deps, homeDir), "PROJECT.md");
    if (!deps.fs.existsSync(projectPath)) return "";
    const content = deps.fs.readFileSync(projectPath, "utf8");
    const m = content.match(/^\s*Description\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err: unknown) {
    warnCatalog(`[memory] project catalog: PROJECT.md description read failed: ${String((err as Error)?.message || err)}`);
    return "";
  }
}

export function createProjectCatalogReader(deps: ProjectCatalogReaderDeps) {
  function getProjectDefinitions(): Record<string, any> {
    const config = deps.getMemoryConfig();
    const defs = config?.projects?.definitions;
    const out: Record<string, any> = defs && typeof defs === "object" && !Array.isArray(defs)
      ? { ...(defs as Record<string, any>) }
      : {};
    const projectsDir = deps.path.join(resolveVisibleHome(deps), "projects");
    try {
      if (deps.fs.existsSync(projectsDir)) {
        for (const name of deps.fs.readdirSync(projectsDir)) {
          const projectName = String(name || "").trim();
          if (!projectName || projectName.startsWith(".")) continue;
          if (out[projectName]) continue;
          const projectDir = deps.path.join(projectsDir, projectName);
          try {
            if (!deps.fs.statSync(projectDir).isDirectory()) continue;
            const hasProjectDoc = deps.fs.existsSync(deps.path.join(projectDir, "PROJECT.md"));
            const hasProjectLog = deps.fs.existsSync(deps.path.join(projectDir, "PROJECT.log"));
            if (!hasProjectDoc && !hasProjectLog) continue;
            out[projectName] = { homeDir: deps.path.join("projects", projectName) };
          } catch (err: unknown) {
            handleCatalogError(`failed to inspect visible project ${projectName}`, err);
          }
        }
      }
    } catch (err: unknown) {
      handleCatalogError("failed to scan visible project directory", err);
    }
    return out;
  }

  function shouldFailHard(): boolean {
    try {
      return deps.isFailHardEnabled?.() === true;
    } catch {
      return false;
    }
  }

  function handleCatalogError(context: string, err: unknown): void {
    const detail = String((err as Error)?.message || err);
    if (shouldFailHard()) {
      const cause = err instanceof Error ? err : new Error(detail);
      throw new Error(`[memory] project catalog: ${context}: ${detail}`, { cause });
    }
    warnCatalog(`[memory] project catalog: ${context}: ${detail}`);
  }

  function getProjectNames(): string[] {
    try {
      return Object.keys(getProjectDefinitions());
    } catch (err: unknown) {
      handleCatalogError("failed to load project names", err);
      return [];
    }
  }

  function getProjectCatalog(): Array<{ name: string; description: string }> {
    try {
      const defs = getProjectDefinitions();
      return Object.entries(defs).map(([name, def]: [string, any]) => {
        const description = String(def?.description || "").trim()
          || getProjectDescriptionFromToolsMd(deps, String(def?.homeDir || "").trim())
          || getProjectDescriptionFromProjectMd(deps, String(def?.homeDir || "").trim())
          || "No description";
        return { name, description };
      });
    } catch (err: unknown) {
      handleCatalogError("failed to load full catalog", err);
      return getProjectNames().map((name) => ({ name, description: "No description" }));
    }
  }

  return {
    getProjectNames,
    getProjectCatalog,
  };
}
