import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function installFakeClack(root: string): string {
  const binDir = path.join(root, "fake-openclaw");
  const openclawBin = path.join(binDir, "openclaw");
  const clackPath = path.join(binDir, "node_modules", "@clack", "prompts", "dist", "index.mjs");
  fs.mkdirSync(path.dirname(clackPath), { recursive: true });
  fs.writeFileSync(openclawBin, "#!/bin/sh\nexit 0\n", "utf8");
  fs.chmodSync(openclawBin, 0o755);
  fs.writeFileSync(
    clackPath,
    `let configMenuCalls = 0;
export async function select(opts) {
  const message = String(opts?.message || "");
  if (message === "Config menu") return configMenuCalls++ === 0 ? "zone_retrieval" : "save";
  if (message === "Retrieval") return "fail_hard";
  if (message === "retrieval.fail_hard") return "off";
  throw new Error("unexpected select prompt: " + message);
}
export async function text(opts) { throw new Error("unexpected text prompt: " + String(opts?.message || "")); }
export async function confirm() { return true; }
export function isCancel() { return false; }
export function cancel(msg) { throw new Error(String(msg || "cancelled")); }
export function note() {}
`,
    "utf8",
  );
  return binDir;
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

  it("saves fail hard edits to the canonical snake_case key only", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-config-cli-mjs-save-"));
    try {
      const configPath = path.join(root, "shared", "config", "global", "config.json");
      writeJson(configPath, {
        retrieval: {
          failHard: true,
        },
      });
      fs.mkdirSync(`${configPath}.tmp`);
      const fakeBinDir = installFakeClack(root);

      const result = spawnSync(process.execPath, ["config_cli.mjs", "edit", "--shared"], {
        cwd: process.cwd(),
        env: {
          ...process.env,
          PATH: `${fakeBinDir}${path.delimiter}${process.env.PATH || ""}`,
          QUAID_HOME: root,
          OPENCLAW_WORKSPACE: root,
          QUAID_QUIET: "1",
          TERM: "dumb",
        },
        encoding: "utf8",
      });

      expect(result.status).toBe(0);
      const saved = JSON.parse(fs.readFileSync(configPath, "utf8"));
      expect(saved.retrieval.fail_hard).toBe(false);
      expect(saved.retrieval.failHard).toBeUndefined();
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });
});
