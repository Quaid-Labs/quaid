#!/usr/bin/env node

import { existsSync } from "node:fs";
import { execSync } from "node:child_process";

const PAIRS = [
  ["adapters/openclaw/index.ts", "adapters/openclaw/index.js"],
  ["adapters/openclaw/command-signals.ts", "adapters/openclaw/command-signals.js"],
  ["core/session-timeout.ts", "core/session-timeout.js"],
];

function changedFiles() {
  try {
    const out = execSync("git diff --name-only HEAD", { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    return new Set(out.split("\n").map((s) => s.trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

function main() {
  const changed = changedFiles();
  if (changed.size === 0) {
    console.log("[runtime-pairs] no local changes detected; skip pair check");
    process.exit(0);
  }

  const errors = [];
  for (const [tsPath, jsPath] of PAIRS) {
    if (!existsSync(tsPath) || !existsSync(jsPath)) {
      errors.push(`Missing runtime pair file(s): ${tsPath} / ${jsPath}`);
      continue;
    }

    const tsChanged = changed.has(tsPath);
    const jsChanged = changed.has(jsPath);
    if (tsChanged !== jsChanged) {
      errors.push(
        `Unsynced runtime pair: ${tsPath} changed=${tsChanged}, ${jsPath} changed=${jsChanged}`
      );
    }
  }

  if (errors.length > 0) {
    console.error("[runtime-pairs] FAILED");
    for (const err of errors) {
      console.error(`  - ${err}`);
    }
    console.error(
      "[runtime-pairs] Edit TS+JS pairs together until gateway/runtime behavior is fully unified."
    );
    process.exit(1);
  }

  console.log("[runtime-pairs] PASS");
}

main();

