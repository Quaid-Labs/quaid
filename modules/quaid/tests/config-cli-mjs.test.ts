import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("config_cli.mjs", () => {
  it("starts edit mode with workspace janitor policy in the summary", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-config-cli-mjs-"));
    try {
      writeJson(path.join(root, "shared", "config", "global", "config.json"), {
        janitor: {
          approvalPolicies: {
            coreMarkdownWrites: "ask",
            projectDocsWrites: "auto",
            workspaceFileMovesDeletes: "ask",
            destructiveMemoryOps: "auto",
          },
        },
      });

      const result = spawnSync(process.execPath, ["config_cli.mjs", "edit", "--shared"], {
        cwd: process.cwd(),
        env: {
          ...process.env,
          QUAID_HOME: root,
          OPENCLAW_WORKSPACE: root,
          TERM: "dumb",
        },
        input: "",
        encoding: "utf8",
      });

      expect(result.status).toBe(0);
      const output = `${result.stdout}\n${result.stderr}`;
      expect(output).toContain("workspace=ask");
      expect(output).not.toContain("workspacePolicy is not defined");
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
