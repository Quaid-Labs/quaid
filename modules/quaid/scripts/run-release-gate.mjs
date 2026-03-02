#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), "..");

function run(name, cmd, args) {
  console.log(`\n[release-gate] ${name}`);
  const res = spawnSync(cmd, args, {
    cwd: ROOT,
    env: process.env,
    stdio: "inherit",
  });
  if (res.status !== 0) {
    process.exit(res.status || 1);
  }
}

const fixture = path.join("tests", "fixtures", "openclaw-auto-compaction.jsonl");

run("Runtime build", "npm", ["run", "build:runtime"]);
run(
  "Targeted regression tests",
  "npm",
  [
    "run",
    "test:run",
    "--",
    "tests/lifecycle-signal.test.ts",
    "tests/chat-flow.integration.test.ts",
    "tests/session-timeout-manager.test.ts",
    "tests/reset-burst-notify.integration.test.ts",
  ],
);
run("Compaction replay fixture", "node", ["scripts/replay-compaction-signal.mjs", "--session-file", fixture]);

console.log("\n[release-gate] PASS");
