import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ensureOpenClawAgentModelDefault,
  OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY,
} from "../../../lib/openclaw-agent-model-default.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("OpenClaw agent model defaults", () => {
  it("upgrades missing and old mini agent defaults to the full test agent model", () => {
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
    expect(cfg.agents.defaults.modelPrimary).toBe(OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY);
    expect(cfg.agents.defaults.model.primary).toBe(OPENCLAW_DEFAULT_AGENT_MODEL_PRIMARY);
    fs.rmSync(root, { recursive: true, force: true });
  });

  it("does not overwrite a custom agent model", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-oc-agent-model-"));
    const cfgPath = path.join(root, "openclaw.json");
    writeJson(cfgPath, {
      agents: {
        defaults: {
          modelPrimary: "anthropic/claude-opus-4-6",
        },
      },
    });

    const result = ensureOpenClawAgentModelDefault(cfgPath);
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

    expect(result.changed).toBe(false);
    expect(cfg.agents.defaults.modelPrimary).toBe("anthropic/claude-opus-4-6");
    expect(cfg.agents.defaults.model).toBeUndefined();
    fs.rmSync(root, { recursive: true, force: true });
  });
});
