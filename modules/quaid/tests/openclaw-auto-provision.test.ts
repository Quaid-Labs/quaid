import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

type AdapterPlugin = {
  register: (api: any) => void;
};

const childProcessState = vi.hoisted(() => ({
  daemonStartCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
}));

vi.mock("node:child_process", async () => {
  const actual = await vi.importActual<typeof import("node:child_process")>("node:child_process");
  return {
    ...actual,
    execFileSync: ((file: string, args?: readonly string[] | null, options?: any) => {
      const normalizedArgs = Array.isArray(args) ? args.map((arg) => String(arg)) : [];
      if (normalizedArgs[0] === "daemon" && normalizedArgs[1] === "start") {
        childProcessState.daemonStartCalls.push({
          file,
          args: normalizedArgs,
          env: (options?.env || {}) as Record<string, string | undefined>,
        });
        return "";
      }
      return actual.execFileSync(file, args as any, options);
    }) as typeof actual.execFileSync,
  };
});

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
  childProcessState.daemonStartCalls = [];
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
    writeJson(path.join(hiddenHome, "shared", "config", "openclaw", "config.json"), {
      adapter: { type: "openclaw" },
      retrieval: { failHard: false, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex-mini",
      },
      capture: { chunk_tokens: 500 },
      plugins: { strict: false, enabled: true },
      notifications: { level: "normal" },
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
    const targetConfig = JSON.parse(fs.readFileSync(targetConfigPath, "utf8"));
    expect(targetConfig.models.deepReasoning).toBe("gpt-5.1-codex");
    expect(targetConfig.models.fastReasoning).toBe("gpt-5.1-codex-mini");
    expect(targetConfig.capture.chunk_tokens).toBe(500);
    expect(targetConfig.plugins.enabled).toBe(true);
    expect(targetConfig.notifications.level).toBe("normal");
    expect(
      childProcessState.daemonStartCalls.some(
        (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m13test",
      ),
    ).toBe(true);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const promptResult = await beforePromptBuildHandler(
      { prompt: "ok", messages: [], prependContext: "" },
      {
        sessionId: "da6cb06a-08b0-4443-bad7-f709df233545",
        sessionKey: "agent:m13test:tui-da6cb06a",
      },
    );

    expect(String(promptResult?.prependSystemContext || "")).toContain("instance: openclaw-m13test");
    expect(String(promptResult?.prependSystemContext || "")).toContain("misc--openclaw-m13test");
    expect(String(promptResult?.prependSystemContext || "")).not.toContain("openclaw-main");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });
});
