import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  deepMergeMissing,
  hydratePlatformInstanceConfigs,
  readJsonObject,
} from "../../../lib/install-config-hydration.mjs";

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

describe("install config hydration", () => {
  it("fills missing nested runtime sections without clobbering instance overrides", () => {
    const merged = deepMergeMissing(
      {
        models: {
          llmProvider: "openai-codex",
          deepReasoning: "gpt-5.4",
          fastReasoning: "gpt-5.4-mini",
        },
        capture: { chunk_tokens: 500, inactivityTimeoutMinutes: 60 },
        plugins: { strict: true, enabled: true },
        notifications: { level: "normal" },
      },
      {
        models: { fastReasoning: "custom-fast" },
        plugins: { strict: false },
      },
    );

    expect(merged.models.deepReasoning).toBe("gpt-5.4");
    expect(merged.models.fastReasoning).toBe("custom-fast");
    expect(merged.capture.chunk_tokens).toBe(500);
    expect(merged.plugins.strict).toBe(false);
    expect(merged.plugins.enabled).toBe(true);
    expect(merged.notifications.level).toBe("normal");
  });

  it("hydrates every OpenClaw instance config from installer defaults", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-install-hydrate-"));
    const instancesDir = path.join(root, "instances");
    const defaults = {
      adapter: { type: "openclaw" },
      models: {
        llmProvider: "openai-codex",
        deepReasoning: "gpt-5.4",
        fastReasoning: "gpt-5.4-mini",
      },
      capture: { chunk_tokens: 500 },
      plugins: { strict: true, enabled: true },
      notifications: { level: "normal" },
    };

    writeJson(path.join(instancesDir, "openclaw-main", "config.json"), {
      adapter: { type: "openclaw" },
      models: {},
      capture: {},
      plugins: { strict: false },
    });
    writeJson(path.join(instancesDir, "openclaw-m13test", "config.json"), {
      adapter: { type: "openclaw" },
    });
    writeJson(path.join(instancesDir, "codex-main", "config.json"), {
      adapter: { type: "codex" },
    });

    const hydrated = hydratePlatformInstanceConfigs({
      instancesDir,
      platformKey: "openclaw",
      defaults,
    });

    const main = readJsonObject(path.join(instancesDir, "openclaw-main", "config.json"))!;
    const m13 = readJsonObject(path.join(instancesDir, "openclaw-m13test", "config.json"))!;
    const codex = readJsonObject(path.join(instancesDir, "codex-main", "config.json"))!;
    expect(hydrated).toHaveLength(2);
    expect(main.models.deepReasoning).toBe("gpt-5.4");
    expect(main.capture.chunk_tokens).toBe(500);
    expect(main.plugins.strict).toBe(false);
    expect(main.notifications.level).toBe("normal");
    expect(m13.models.fastReasoning).toBe("gpt-5.4-mini");
    expect(m13.plugins.enabled).toBe(true);
    expect(codex.models).toBeUndefined();

    fs.rmSync(root, { recursive: true, force: true });
  });
});
