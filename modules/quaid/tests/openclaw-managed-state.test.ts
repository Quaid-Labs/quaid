import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  captureOpenClawManagedState,
  readOpenClawManagedStateSnapshot,
  restoreOpenClawManagedState,
  writeOpenClawManagedStateSnapshot,
} from "../../../lib/openclaw-managed-state.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("OpenClaw managed state", () => {
  it("captures and restores the managed plugin, matrix, and model subset", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-managed-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: {
        defaults: {
          model: { primary: "anthropic/claude-haiku-4-5" },
        },
        list: [
          { id: "main", default: true, model: { primary: "anthropic/claude-haiku-4-5" } },
        ],
      },
      plugins: {
        allow: ["quaid", "matrix", "openai"],
        entries: {
          quaid: { enabled: true },
          matrix: { enabled: true },
          openai: { enabled: true },
        },
        slots: { memory: "quaid" },
      },
      channels: {
        matrix: {
          enabled: true,
          homeserver: "http://127.0.0.1:8008",
          network: { dangerouslyAllowPrivateNetwork: true },
        },
      },
    });

    const snapshot = captureOpenClawManagedState(cfgPath);
    writeJson(cfgPath, {
      agents: {
        defaults: {
          model: { primary: "openai/gpt-5.4" },
        },
        list: [
          { id: "main", default: true, model: { primary: "openai/gpt-5.4" } },
        ],
      },
      plugins: {
        allow: ["openai", "memory-core"],
        entries: {
          openai: { enabled: true },
          matrix: { enabled: false },
        },
        slots: { memory: "memory-core" },
      },
      channels: {
        matrix: { enabled: false },
      },
    });

    const result = restoreOpenClawManagedState(cfgPath, snapshot);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(result.changedBits).toContain("plugins.allow:quaid");
    expect(result.changedBits).toContain("plugins.allow:matrix");
    expect(result.changedBits).toContain("plugins.entries.quaid");
    expect(result.changedBits).toContain("plugins.entries.matrix");
    expect(result.changedBits).toContain("plugins.slots.memory");
    expect(result.changedBits).toContain("channels.matrix");
    expect(result.changedBits).toContain("agents.defaults.model.primary");
    expect(result.changedBits).toContain("agents.list.main.model.primary");
    expect(cfg.plugins.allow).toEqual(["openai", "memory-core", "quaid", "matrix"]);
    expect(cfg.plugins.entries.quaid.enabled).toBe(true);
    expect(cfg.plugins.entries.matrix.enabled).toBe(true);
    expect(cfg.plugins.slots.memory).toBe("quaid");
    expect(cfg.channels.matrix.enabled).toBe(true);
    expect(cfg.agents.defaults.model.primary).toBe("anthropic/claude-haiku-4-5");
    expect(cfg.agents.list[0].model.primary).toBe("anthropic/claude-haiku-4-5");
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("returns null when there is no managed state to preserve", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-managed-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: {
        allow: ["openai"],
        entries: { openai: { enabled: true } },
      },
    });

    const snapshot = captureOpenClawManagedState(cfgPath);
    const result = restoreOpenClawManagedState(cfgPath, snapshot);

    expect(snapshot).toBeNull();
    expect(result.changed).toBe(false);
    expect(result.reason).toBe("missing-snapshot");
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("round-trips a persisted snapshot file", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-managed-"));
    const snapshotPath = path.join(root, "managed-openclaw.json");
    const snapshot = {
      requiredAllow: ["quaid", "matrix"],
      entries: {
        quaid: { enabled: true },
      },
      channels: {
        matrix: { enabled: true },
      },
      plugins: {
        slotsMemory: "quaid",
      },
      agents: {
        defaultPrimary: "anthropic/claude-haiku-4-5",
      },
    };

    expect(writeOpenClawManagedStateSnapshot(snapshotPath, snapshot)).toBe(true);
    expect(readOpenClawManagedStateSnapshot(snapshotPath)).toEqual(snapshot);
    fs.rmSync(root, { recursive: true, force: true });
  });
});
