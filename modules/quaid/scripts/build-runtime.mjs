#!/usr/bin/env node

import { build } from "esbuild";
import { RUNTIME_PAIRS } from "./runtime-pairs.mjs";

async function main() {
  for (const [tsPath, jsPath] of RUNTIME_PAIRS) {
    await build({
      entryPoints: [tsPath],
      outfile: jsPath,
      platform: "node",
      target: "node18",
      format: "esm",
      bundle: false,
      sourcemap: false,
      logLevel: "silent",
      tsconfigRaw: {
        compilerOptions: {
          module: "esnext",
        },
      },
    });
  }
  console.log(`[runtime-build] built ${RUNTIME_PAIRS.length} runtime artifact(s)`);
}

main().catch((err) => {
  console.error(`[runtime-build] FAILED: ${err?.message || err}`);
  process.exit(1);
});
