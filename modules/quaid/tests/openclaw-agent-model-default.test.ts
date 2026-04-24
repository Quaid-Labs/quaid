import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ensureOpenClawAgentModelDefault,
} from "../../../lib/openclaw-agent-model-default.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("OpenClaw agent model defaults", () => {
  it("does not invent a default agent model for a fresh Quaid install", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-agent-model-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: { defaults: {} },
    });

    const result = ensureOpenClawAgentModelDefault(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(false);
    expect(cfg.agents.defaults.modelPrimary).toBeUndefined();
    expect(cfg.agents.defaults.model).toBeUndefined();
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("reports unchanged when a nested agent model is already written", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-agent-model-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: {
        defaults: {
          model: { primary: "anthropic/claude-haiku-4-5" },
        },
      },
    });

    const result = ensureOpenClawAgentModelDefault(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(false);
    expect(cfg.agents.defaults.modelPrimary).toBeUndefined();
    expect(cfg.agents.defaults.model.primary).toBe("anthropic/claude-haiku-4-5");
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("migrates the rejected flat modelPrimary key into the nested schema", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-agent-model-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: {
        defaults: {
          modelPrimary: "openai-codex/gpt-5.4-mini",
          model: { primary: "openai-codex/gpt-5.4-mini" },
        },
      },
    });

    const result = ensureOpenClawAgentModelDefault(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(cfg.agents.defaults.modelPrimary).toBeUndefined();
    expect(cfg.agents.defaults.model.primary).toBe("openai-codex/gpt-5.4-mini");
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("preserves an existing nested primary while removing the legacy flat key", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-agent-model-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: {
        defaults: {
          modelPrimary: "openai-codex/gpt-5.4-mini",
          model: { primary: "anthropic/claude-haiku-4-5" },
        },
      },
    });

    const result = ensureOpenClawAgentModelDefault(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(true);
    expect(cfg.agents.defaults.modelPrimary).toBeUndefined();
    expect(cfg.agents.defaults.model.primary).toBe("anthropic/claude-haiku-4-5");
    fs.rmSync(root, { recursive: true, force: true });
  });
});
