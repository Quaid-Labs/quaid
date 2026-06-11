import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ensureOpenClawMessageToolAllowed,
} from "../../../lib/openclaw-message-tool-allow.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function readJson(filePath: string): any {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

describe("OpenClaw message tool allowlist", () => {
  it("adds message as an alsoAllow override for the coding profile", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-message-tool-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      tools: { profile: "coding" },
    });

    const result = ensureOpenClawMessageToolAllowed(cfgPath);
    const cfg = readJson(cfgPath);

    expect(result.changed).toBe(true);
    expect(result.key).toBe("alsoAllow");
    expect(cfg.tools.profile).toBe("coding");
    expect(cfg.tools.alsoAllow).toContain("message");
    expect(cfg.tools.allow).toBeUndefined();
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("merges message into an explicit allowlist instead of creating alsoAllow", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-message-tool-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      tools: { allow: ["read"] },
    });

    const result = ensureOpenClawMessageToolAllowed(cfgPath);
    const cfg = readJson(cfgPath);

    expect(result.changed).toBe(true);
    expect(result.key).toBe("allow");
    expect(cfg.tools.allow).toEqual(["read", "message"]);
    expect(cfg.tools.alsoAllow).toBeUndefined();
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("is idempotent when message is already allowed", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-message-tool-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      tools: { profile: "coding", alsoAllow: ["message"] },
    });

    const result = ensureOpenClawMessageToolAllowed(cfgPath);
    const cfg = readJson(cfgPath);

    expect(result.changed).toBe(false);
    expect(result.reason).toBe("already-allowed");
    expect(cfg.tools.alsoAllow).toEqual(["message"]);
    fs.rmSync(root, { recursive: true, force: true });
  });
});
