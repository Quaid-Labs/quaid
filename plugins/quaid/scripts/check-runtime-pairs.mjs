#!/usr/bin/env node

import { existsSync } from "node:fs";
import { execSync } from "node:child_process";
import { RUNTIME_PAIRS } from "./runtime-pairs.mjs";

const STRICT = process.argv.includes("--strict");

function gitNames(cmd) {
  try {
    const out = execSync(cmd, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] });
    return out.split("\n").map((s) => s.trim()).filter(Boolean);
  } catch {
    return [];
  }
}

function changedFiles() {
  const changed = new Set();
  // Uncommitted local changes.
  for (const name of gitNames("git diff --name-only HEAD")) changed.add(name);
  // Newly committed files in current HEAD (CI-friendly, clean working tree).
  for (const name of gitNames("git diff-tree --no-commit-id --name-only -r HEAD")) changed.add(name);
  return changed;
}

function main() {
  const changed = changedFiles();
  const errors = [];

  for (const [tsPath, jsPath] of RUNTIME_PAIRS) {
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

  if (!STRICT && changed.size === 0) {
    console.log("[runtime-pairs] no local/head changes detected; pair files exist");
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

  console.log(STRICT ? "[runtime-pairs] PASS (strict)" : "[runtime-pairs] PASS");
}

main();
