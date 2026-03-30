#!/usr/bin/env node

import { spawnSync } from "node:child_process";

function canRun(cmd) {
  const res = spawnSync("sh", ["-lc", `command -v ${cmd} >/dev/null 2>&1`], { stdio: "ignore" });
  return res.status === 0;
}

if (!canRun("openclaw") && !canRun("clawdbot")) {
  console.warn("[quaid][adapter:openclaw][preinstall] OpenClaw CLI not found yet; continuing.");
}

