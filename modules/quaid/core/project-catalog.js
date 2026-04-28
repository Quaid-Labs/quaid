function warnCatalog(message) {
  try {
    console.warn(message);
  } catch {
  }
}
function firstUsefulLine(content) {
  return String(content || "").split("\n").map((line) => line.trim()).find((line) => line && !line.startsWith("#") && !line.startsWith("|")) || "";
}
function resolveVisibleHome(deps) {
  const explicit = String(process.env.QUAID_VISIBLE_HOME || "").trim();
  if (explicit) return explicit;
  const root = deps.path.resolve(deps.workspace);
  const base = deps.path.basename(root);
  if (base.startsWith(".") && base.length > 1) {
    return deps.path.join(deps.path.dirname(root), base.slice(1));
  }
  return root;
}
function resolveProjectHome(deps, homeDir) {
  const raw = String(homeDir || "").trim();
  if (!raw) return "";
  if (deps.path.isAbsolute(raw)) return raw;
  if (raw === "projects" || raw.startsWith("projects/")) {
    return deps.path.join(resolveVisibleHome(deps), raw);
  }
  return deps.path.join(deps.workspace, raw);
}
function getProjectDescriptionFromToolsMd(deps, homeDir) {
  try {
    if (!homeDir) return "";
    const toolsPath = deps.path.join(resolveProjectHome(deps, homeDir), "TOOLS.md");
    if (!deps.fs.existsSync(toolsPath)) return "";
    const content = deps.fs.readFileSync(toolsPath, "utf8");
    const m = content.match(/^\s*(?:Project\s+Description|Description)\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err) {
    warnCatalog(`[memory] project catalog: TOOLS.md description read failed: ${String(err?.message || err)}`);
    return "";
  }
}
function getProjectDescriptionFromProjectMd(deps, homeDir) {
  try {
    if (!homeDir) return "";
    const projectPath = deps.path.join(resolveProjectHome(deps, homeDir), "PROJECT.md");
    if (!deps.fs.existsSync(projectPath)) return "";
    const content = deps.fs.readFileSync(projectPath, "utf8");
    const m = content.match(/^\s*Description\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err) {
    warnCatalog(`[memory] project catalog: PROJECT.md description read failed: ${String(err?.message || err)}`);
    return "";
  }
}
function createProjectCatalogReader(deps) {
  function getProjectDefinitions() {
    const config = deps.getMemoryConfig();
    const defs = config?.projects?.definitions;
    const out = defs && typeof defs === "object" && !Array.isArray(defs) ? { ...defs } : {};
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
          } catch (err) {
            handleCatalogError(`failed to inspect visible project ${projectName}`, err);
          }
        }
      }
    } catch (err) {
      handleCatalogError("failed to scan visible project directory", err);
    }
    return out;
  }
  function shouldFailHard() {
    try {
      return deps.isFailHardEnabled?.() === true;
    } catch {
      return false;
    }
  }
  function handleCatalogError(context, err) {
    const detail = String(err?.message || err);
    if (shouldFailHard()) {
      const cause = err instanceof Error ? err : new Error(detail);
      throw new Error(`[memory] project catalog: ${context}: ${detail}`, { cause });
    }
    warnCatalog(`[memory] project catalog: ${context}: ${detail}`);
  }
  function getProjectNames() {
    try {
      return Object.keys(getProjectDefinitions());
    } catch (err) {
      handleCatalogError("failed to load project names", err);
      return [];
    }
  }
  function getProjectCatalog() {
    try {
      const defs = getProjectDefinitions();
      return Object.entries(defs).map(([name, def]) => {
        const description = String(def?.description || "").trim() || getProjectDescriptionFromToolsMd(deps, String(def?.homeDir || "").trim()) || getProjectDescriptionFromProjectMd(deps, String(def?.homeDir || "").trim()) || "No description";
        return { name, description };
      });
    } catch (err) {
      handleCatalogError("failed to load full catalog", err);
      return getProjectNames().map((name) => ({ name, description: "No description" }));
    }
  }
  return {
    getProjectNames,
    getProjectCatalog
  };
}
export {
  createProjectCatalogReader
};
