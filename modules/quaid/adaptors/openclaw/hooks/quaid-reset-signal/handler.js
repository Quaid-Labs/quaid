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

function signalPriority(label) {
  const normalized = String(label || "").trim().toLowerCase();
  if (normalized === "resetsignal" || normalized === "reset") return 3;
  if (normalized === "compactionsignal" || normalized === "compaction") return 2;
  return 1;
}

function resolveAction(event) {
  const direct = String(event?.action || "").trim().toLowerCase();
  if (direct) return direct;
  const type = String(event?.type || "").trim().toLowerCase();
  if (type.startsWith("command:")) return type.slice("command:".length);
  return "";
}

function resolveSignalLabel(action) {
  if (action === "new" || action === "reset" || action === "restart") {
    return "ResetSignal";
  }
  if (action === "compact") {
    return "CompactionSignal";
  }
  return "";
}

function resolveTargetSessionId(event, context) {
  const previous = context?.previousSessionEntry || {};
  const current = context?.sessionEntry || {};
  const candidates = [
    previous?.sessionId,
    previous?.id,
    context?.previousSessionId,
    current?.sessionId,
    current?.id,
    context?.sessionId,
    event?.sessionId,
    event?.session_id,
  ];
  for (const candidate of candidates) {
    const id = String(candidate || "").trim();
    if (id) return id;
  }
  return "";
}

export default async function quaidResetSignalHook(event) {
  if (!event) return;

  const context = event.context || {};
  const action = resolveAction(event);
  const label = resolveSignalLabel(action);
  if (!label) return;

  const sessionId = resolveTargetSessionId(event, context);
  if (!sessionId) return;

  const workspace = resolveWorkspaceFromContext(context);
  const signalDir = path.join(workspace, "data", "pending-extraction-signals");
  const signalPath = path.join(signalDir, `${sessionId}.json`);
  const signal = {
    sessionId,
    label,
    queuedAt: new Date().toISOString(),
  };

  fs.mkdirSync(signalDir, { recursive: true });
  if (fs.existsSync(signalPath)) {
    try {
      const existing = JSON.parse(fs.readFileSync(signalPath, "utf8"));
      const existingLabel = String(existing?.label || "").trim();
      if (signalPriority(existingLabel) >= signalPriority(label)) {
        return;
      }
    } catch {
      // Fall through and overwrite malformed pending signal.
    }
  }
  fs.writeFileSync(signalPath, JSON.stringify(signal), { mode: 0o600 });
}
