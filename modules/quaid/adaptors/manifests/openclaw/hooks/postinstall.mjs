#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function resolveCli() {
  const ok = spawnSync("sh", ["-lc", "command -v openclaw >/dev/null 2>&1"], { stdio: "ignore" });
  if (ok.status === 0) return "openclaw";
  return "";
}

function ensureExecApprovalsAllowQuaid() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "exec-approvals.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  const targetAgent = "*";
  const quaidHome = String(process.env.QUAID_HOME || "").trim() || path.join(os.homedir(), ".quaid");
  const patterns = Array.from(new Set([
    path.join(os.homedir(), ".openclaw", "extensions", "quaid", "quaid"),
    path.join(quaidHome, "modules", "quaid", "quaid"),
    path.join(os.homedir(), "bin", "quaid"),
    path.join(os.homedir(), ".local", "bin", "quaid"),
    "/usr/local/bin/quaid",
    "/opt/homebrew/bin/quaid",
  ]));

  const normalizeEntry = (entry) => {
    if (typeof entry === "string") {
      const pattern = entry.trim();
      return pattern ? { pattern } : null;
    }
    if (entry && typeof entry === "object") {
      const pattern = String(entry.pattern || "").trim();
      if (!pattern) return null;
      return {
        ...entry,
        pattern,
      };
    }
    return null;
  };

  try {
    const raw = fs.existsSync(cfgPath) ? fs.readFileSync(cfgPath, "utf8") : '{"version":1,"agents":{}}';
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("invalid exec approvals payload");
    }
    if (parsed.version !== 1) {
      parsed.version = 1;
    }
    if (!parsed.agents || typeof parsed.agents !== "object" || Array.isArray(parsed.agents)) {
      parsed.agents = {};
    }
    const agent = parsed.agents[targetAgent] && typeof parsed.agents[targetAgent] === "object"
      ? parsed.agents[targetAgent]
      : {};
    const existingAllowlist = Array.isArray(agent.allowlist) ? agent.allowlist : [];
    const normalizedAllowlist = [];
    const seen = new Set();
    for (const rawEntry of existingAllowlist) {
      const entry = normalizeEntry(rawEntry);
      if (!entry) continue;
      if (seen.has(entry.pattern)) continue;
      seen.add(entry.pattern);
      normalizedAllowlist.push(entry);
    }

    let changed = normalizedAllowlist.length !== existingAllowlist.length;
    for (const pattern of patterns) {
      if (seen.has(pattern)) continue;
      normalizedAllowlist.push({
        id: `quaid-${normalizedAllowlist.length}`,
        pattern,
        lastUsedAt: Date.now(),
      });
      seen.add(pattern);
      changed = true;
    }

    if (!changed) return false;
    parsed.agents[targetAgent] = {
      ...agent,
      allowlist: normalizedAllowlist,
    };
    fs.mkdirSync(path.dirname(cfgPath), { recursive: true });
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch (err) {
    console.warn(
      `[quaid][adapter:openclaw][postinstall] exec approvals update failed: ${String(err)}`,
    );
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

const cli = resolveCli();
if (ensureExecApprovalsAllowQuaid()) {
  console.log("[quaid][adapter:openclaw][postinstall] ensured exec approvals allowlist includes quaid");
}
if (!cli) {
  console.warn("[quaid][adapter:openclaw][postinstall] OpenClaw CLI missing; skipped hook enable.");
  process.exit(0);
}

const requiredHooks = ["bootstrap-extra-files"];
for (const hookName of requiredHooks) {
  const res = spawnSync(cli, ["hooks", "enable", hookName], {
    stdio: "pipe",
    encoding: "utf8",
    timeout: 45000,
  });
  if (res.status === 0) {
    continue;
  }
  const stderr = String(res.stderr || "").trim();
  const stdout = String(res.stdout || "").trim();
  const text = `${stdout}\n${stderr}`.toLowerCase();
  if (text.includes("already enabled") || text.includes("already registered")) {
    continue;
  }
  console.warn(
    `[quaid][adapter:openclaw][postinstall] hooks enable ${hookName} returned ${res.status}; continuing.`,
  );
}
