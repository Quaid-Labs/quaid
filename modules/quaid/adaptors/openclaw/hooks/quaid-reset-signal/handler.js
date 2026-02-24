import fs from "node:fs";
import os from "node:os";
import path from "node:path";

function resolveWorkspaceFromContext(context) {
  const cfg = context?.cfg || {};
  const defaultsWorkspace = cfg?.agents?.defaults?.workspace;
  if (typeof defaultsWorkspace === "string" && defaultsWorkspace.trim()) {
    return defaultsWorkspace;
  }
  const list = Array.isArray(cfg?.agents?.list) ? cfg.agents.list : [];
  const defaultAgent = list.find((a) => a && a.default) || list[0];
  if (defaultAgent && typeof defaultAgent.workspace === "string" && defaultAgent.workspace.trim()) {
    return defaultAgent.workspace;
  }
  return path.join(resolveStateDir(), "workspace");
}

function resolveStateDir() {
  const envDir = process.env.CLAWDBOT_STATE_DIR || process.env.OPENCLAW_STATE_DIR;
  if (typeof envDir === "string" && envDir.trim()) {
    return envDir;
  }
  return path.join(os.homedir(), ".openclaw");
}

function resolveTargetSessionId(context) {
  const previous = context?.previousSessionEntry;
  const current = context?.sessionEntry;
  const previousId = String(previous?.sessionId || "").trim();
  if (previousId) {
    return previousId;
  }
  const currentId = String(current?.sessionId || "").trim();
  if (currentId) {
    return currentId;
  }
  return "";
}

export default async function quaidResetSignalHook(event) {
  if (!event || event.type !== "command") return;
  if (event.action !== "new" && event.action !== "reset") return;

  const context = event.context || {};
  const sessionId = resolveTargetSessionId(context);
  if (!sessionId) return;

  const workspace = resolveWorkspaceFromContext(context);
  const signalDir = path.join(workspace, "data", "pending-extraction-signals");
  const signalPath = path.join(signalDir, `${sessionId}.json`);
  const signal = {
    sessionId,
    label: "ResetSignal",
    queuedAt: new Date().toISOString(),
  };

  fs.mkdirSync(signalDir, { recursive: true });
  fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
}
