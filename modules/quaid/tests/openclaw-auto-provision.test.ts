import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

type AdapterPlugin = {
  register: (api: any) => void;
};

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function makeFakeApi() {
  return {
    on: vi.fn(() => {}),
    registerHook: vi.fn(() => {}),
    registerHttpRoute: vi.fn(() => {}),
    registerTool: vi.fn(() => {}),
  };
}

async function loadAdapterWithHomes(hiddenHome: string, visibleHome: string, openClawConfigPath: string): Promise<AdapterPlugin> {
  vi.stubEnv("HOME", path.dirname(hiddenHome));
  vi.stubEnv("OPENCLAW_CONFIG_PATH", openClawConfigPath);
  vi.stubEnv("QUAID_HOME", hiddenHome);
  vi.stubEnv("QUAID_VISIBLE_HOME", visibleHome);
  vi.stubEnv("QUAID_INSTANCE", "openclaw-main");
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return module.default as AdapterPlugin;
}

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("openclaw auto-provision", () => {
  it("auto-provisions a non-default agent silo on first before_agent_start hook touch", async () => {
    vi.useFakeTimers();
    const home = makeTempDir("quaid-oc-autoprov-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");

    writeJson(path.join(hiddenHome, "instances", "openclaw-main", "config.json"), {
      adapter: { type: "openclaw" },
      retrieval: { failHard: false, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex",
      },
      plugins: { strict: false },
    });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-main", "data"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-main", "logs"), { recursive: true });
    fs.mkdirSync(path.join(visibleHome, "projects", "quaid"), { recursive: true });
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "SOUL.md"), "# SOUL\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "USER.md"), "# USER\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "ENVIRONMENT.md"), "# ENVIRONMENT\n", "utf8");
    writeJson(openClawConfigPath, {
      agents: {
        list: [
          { id: "main", default: true },
          { id: "m13test" },
        ],
      },
      env: {
        vars: {
          QUAID_INSTANCE: "openclaw-main",
        },
      },
    });
    fs.mkdirSync(path.join(openClawRoot, "agents", "m13test", "agent"), { recursive: true });

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
    const api = makeFakeApi();
    plugin.register(api as any);

    const targetConfigPath = path.join(hiddenHome, "instances", "openclaw-m13test", "config.json");
    const targetSoulPath = path.join(visibleHome, "instances", "openclaw-m13test", "SOUL.md");
    expect(fs.existsSync(targetConfigPath)).toBe(false);
    expect(fs.existsSync(targetSoulPath)).toBe(false);

    const beforeAgentStartCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_start" && call?.[2]?.name === "memory-injection"
    );
    expect(beforeAgentStartCall).toBeTruthy();

    const beforeAgentStartHandler = beforeAgentStartCall?.[1];
    await beforeAgentStartHandler(
      { prependContext: "" },
      {
        sessionId: "da6cb06a-08b0-4443-bad7-f709df233545",
        sessionKey: "agent:m13test:tui-da6cb06a",
      },
    );

    expect(fs.existsSync(targetConfigPath)).toBe(true);
    expect(fs.existsSync(targetSoulPath)).toBe(true);
    expect(fs.existsSync(path.join(visibleHome, "instances", "openclaw-m13test", "journal"))).toBe(true);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });
});
