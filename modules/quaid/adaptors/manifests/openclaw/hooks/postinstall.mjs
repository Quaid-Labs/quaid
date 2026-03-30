#!/usr/bin/env node

import { spawnSync } from "node:child_process";

function resolveCli() {
  for (const cli of ["openclaw", "clawdbot"]) {
    const ok = spawnSync("sh", ["-lc", `command -v ${cli} >/dev/null 2>&1`], { stdio: "ignore" });
    if (ok.status === 0) return cli;
  }
  return "";
}

const cli = resolveCli();
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

