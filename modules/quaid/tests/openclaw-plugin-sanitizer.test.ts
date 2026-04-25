import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS,
  sanitizeOpenClawNativeMemoryPlugins,
} from "../../../lib/openclaw-plugin-sanitizer.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("OpenClaw native memory plugin sanitizer", () => {
  it("removes native memory plugins from allow, disables their entries, and rebinds memory slot to quaid", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-sanitize-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: {
        allow: ["quaid", "matrix", "active-memory", "memory-core", "memory-wiki", "openai"],
        entries: {
          quaid: { enabled: true },
          matrix: { enabled: true },
          "active-memory": { enabled: true },
          "memory-core": { enabled: true },
          "memory-wiki": { enabled: true },
        },
        slots: {
          memory: "memory-core",
        },
      },
    });

    const result = sanitizeOpenClawNativeMemoryPlugins(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(result.removedAllow.sort()).toEqual([...OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS].sort());
    expect(result.disabledEntries.sort()).toEqual([...OPENCLAW_NATIVE_MEMORY_PLUGIN_IDS].sort());
    expect(result.reboundMemorySlot).toBe(true);
    expect(cfg.plugins.allow).toEqual(["quaid", "matrix", "openai"]);
    expect(cfg.plugins.entries["active-memory"].disabled).toBe(true);
    expect(cfg.plugins.entries["memory-core"].disabled).toBe(true);
    expect(cfg.plugins.entries["memory-wiki"].disabled).toBe(true);
    expect(cfg.plugins.entries.quaid.enabled).toBe(true);
    expect(cfg.plugins.entries.matrix.enabled).toBe(true);
    expect(cfg.plugins.slots.memory).toBe("quaid");
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("reports unchanged when memory plugin state is already sanitized", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-sanitize-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: {
        allow: ["quaid", "matrix"],
        entries: {
          quaid: { enabled: true },
          matrix: { enabled: true },
          "memory-core": { disabled: true },
        },
        slots: {
          memory: "quaid",
        },
      },
    });

    const before = fs.readFileSync(cfgPath, "utf8");
    const result = sanitizeOpenClawNativeMemoryPlugins(cfgPath);
    const after = fs.readFileSync(cfgPath, "utf8");

    expect(result.changed).toBe(false);
    expect(result.reason).toBe("already-sanitized");
    expect(after).toBe(before);
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("sanitizes malformed native memory entries without touching unrelated plugins", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-sanitize-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      plugins: {
        allow: ["quaid", "matrix", "active-memory"],
        entries: {
          quaid: { enabled: true },
          matrix: { enabled: true },
          "active-memory": "unexpected",
        },
        slots: {
          memory: "quaid",
        },
      },
    });

    const result = sanitizeOpenClawNativeMemoryPlugins(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(cfg.plugins.allow).toEqual(["quaid", "matrix"]);
    expect(cfg.plugins.entries["active-memory"].disabled).toBe(true);
    expect(cfg.plugins.entries.matrix.enabled).toBe(true);
    expect(cfg.plugins.slots.memory).toBe("quaid");
    fs.rmSync(root, { recursive: true, force: true });
  });
});
