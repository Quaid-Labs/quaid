#!/usr/bin/env node

import { existsSync } from "node:fs";
import { execSync } from "node:child_process";
import { RUNTIME_PAIRS } from "./runtime-pairs.mjs";

const STRICT = process.argv.includes("--strict");

function main() {
  const errors = [];
  const jsPaths = RUNTIME_PAIRS.map(([, jsPath]) => jsPath);
  const jsArg = jsPaths.join(" ");
  const parsePorcelainPaths = (raw) =>
    raw
      .split("\n")
      .filter(Boolean)
      .map((line) => line.replace(/^.. /, "").trim())
      .filter(Boolean);

  for (const [tsPath, jsPath] of RUNTIME_PAIRS) {
    if (!existsSync(tsPath) || !existsSync(jsPath)) {
      errors.push(`Missing runtime pair file(s): ${tsPath} / ${jsPath}`);
    }
  }

  if (errors.length > 0) {
    console.error("[runtime-pairs] FAILED");
    for (const err of errors) {
      console.error(`  - ${err}`);
    }
    process.exit(1);
  }

  const before = new Set(
    parsePorcelainPaths(execSync(`git status --porcelain -- ${jsArg}`, { encoding: "utf8" }))
  );

  try {
    execSync("node scripts/build-runtime.mjs", { stdio: "inherit" });
  } catch {
    process.exit(1);
  }

  const after = new Set(
    parsePorcelainPaths(execSync(`git status --porcelain -- ${jsArg}`, { encoding: "utf8" }))
  );
  const changedGenerated = [...after].filter((file) => !before.has(file));

  if (changedGenerated.length > 0) {
    console.error("[runtime-pairs] FAILED");
    console.error("[runtime-pairs] Generated JS artifacts are out of date:");
    for (const file of changedGenerated) {
      console.error(`  - ${file}`);
    }
    console.error("[runtime-pairs] Run `npm run build:runtime` and commit generated runtime JS.");
    process.exit(1);
  }

  if (STRICT) {
    console.log("[runtime-pairs] PASS (strict)");
  } else {
    console.log("[runtime-pairs] PASS");
  }
}

main();
