function warnCatalog(message) {
  try {
    console.warn(message);
  } catch {
  }
}
function firstUsefulLine(content) {
  return String(content || "").split("\n").map((line) => line.trim()).find((line) => line && !line.startsWith("#") && !line.startsWith("|")) || "";
}
function getProjectDescriptionFromToolsMd(deps, homeDir) {
  try {
    if (!homeDir) return "";
    const toolsPath = deps.path.join(deps.workspace, homeDir, "TOOLS.md");
    if (!deps.fs.existsSync(toolsPath)) return "";
    const content = deps.fs.readFileSync(toolsPath, "utf8");
    const m = content.match(/^\s*(?:Project\s+Description|Description)\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err) {
    warnCatalog(`[quaid] project catalog: TOOLS.md description read failed: ${String(err?.message || err)}`);
    return "";
  }
}
function getProjectDescriptionFromProjectMd(deps, homeDir) {
  try {
    if (!homeDir) return "";
    const projectPath = deps.path.join(deps.workspace, homeDir, "PROJECT.md");
    if (!deps.fs.existsSync(projectPath)) return "";
    const content = deps.fs.readFileSync(projectPath, "utf8");
    const m = content.match(/^\s*Description\s*:\s*(.+)$/im);
    if (m && m[1]) return m[1].trim().slice(0, 180);
    return firstUsefulLine(content).slice(0, 180);
  } catch (err) {
    warnCatalog(`[quaid] project catalog: PROJECT.md description read failed: ${String(err?.message || err)}`);
    return "";
  }
}
function createProjectCatalogReader(deps) {
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
      throw new Error(`[quaid] project catalog: ${context}: ${detail}`, { cause });
    }
    warnCatalog(`[quaid] project catalog: ${context}: ${detail}`);
  }
  function getProjectNames() {
    try {
      const configPath = deps.path.join(deps.workspace, "config/memory.json");
      const configData = JSON.parse(deps.fs.readFileSync(configPath, "utf-8"));
      return Object.keys(configData?.projects?.definitions || {});
    } catch (err) {
      handleCatalogError("failed to load project names", err);
      return [];
    }
  }
  function getProjectCatalog() {
    try {
      const configPath = deps.path.join(deps.workspace, "config/memory.json");
      const configData = JSON.parse(deps.fs.readFileSync(configPath, "utf-8"));
      const defs = configData?.projects?.definitions || {};
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
