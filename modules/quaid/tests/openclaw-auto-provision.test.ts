import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

type AdapterPlugin = {
  register: (api: any) => void;
};
type AdapterTestApi = {
  shouldMirrorTranscriptUpdateToPreservedCopy: (sessionKey: string) => boolean;
  resolveAgentLabelFromModelName: (modelName: unknown) => string;
  resolveHookAgentLabel: (event: any, ctx: any) => string;
};
type LoadedAdapter = {
  plugin: AdapterPlugin;
  testApi: AdapterTestApi;
};

const childProcessState = vi.hoisted(() => ({
  daemonStartCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  daemonStatusCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  daemonStatusByInstance: {} as Record<string, Array<boolean | "throw" | "bad-json">>,
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
      if (normalizedArgs[0] === "daemon" && normalizedArgs[1] === "status") {
        const env = (options?.env || {}) as Record<string, string | undefined>;
        const instance = String(env.QUAID_INSTANCE || "");
        childProcessState.daemonStatusCalls.push({
          file,
          args: normalizedArgs,
          env,
        });
        const queued = childProcessState.daemonStatusByInstance[instance];
        const next = Array.isArray(queued) && queued.length > 0 ? queued.shift() : true;
        if (next === "throw") {
          throw new Error("status transport failed");
        }
        if (next === "bad-json") {
          return "not json";
        }
        const running = Boolean(next);
        return JSON.stringify({
          running,
          pid: running ? 12345 : null,
          instance,
        });
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

async function loadAdapterWithHomes(hiddenHome: string, visibleHome: string, openClawConfigPath: string): Promise<LoadedAdapter> {
  vi.stubEnv("HOME", path.dirname(hiddenHome));
  vi.stubEnv("OPENCLAW_CONFIG_PATH", openClawConfigPath);
  vi.stubEnv("QUAID_HOME", hiddenHome);
  vi.stubEnv("QUAID_VISIBLE_HOME", visibleHome);
  vi.stubEnv("QUAID_INSTANCE", "openclaw-main");
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return {
    plugin: module.default as AdapterPlugin,
    testApi: (module as any).__test as AdapterTestApi,
  };
}

afterEach(() => {
  childProcessState.daemonStartCalls = [];
  childProcessState.daemonStatusCalls = [];
  childProcessState.daemonStatusByInstance = {};
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("openclaw auto-provision", () => {
  it("unrefs the session index watcher interval on register", async () => {
    const home = makeTempDir("quaid-oc-watcher-unref-home-");
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
        list: [{ id: "main", default: true }],
      },
      env: {
        vars: {
          QUAID_INSTANCE: "openclaw-main",
        },
      },
    });

    const unref = vi.fn();
    const fakeTimer = { unref } as any;
    const originalSetInterval = global.setInterval;
    const setIntervalSpy = vi.spyOn(global, "setInterval").mockImplementation(((fn: any, ms?: any, ...args: any[]) => {
      const scheduled = originalSetInterval(fn, ms, ...args);
      clearInterval(scheduled);
      return fakeTimer;
    }) as typeof global.setInterval);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const { plugin } = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
    const api = makeFakeApi();
    plugin.register(api as any);

    expect(setIntervalSpy).toHaveBeenCalled();
    expect(unref).toHaveBeenCalledOnce();

    setIntervalSpy.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

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
      adapter: {
        type: "openclaw",
        capabilities: { preserve_transcript_mirror_session_prefixes: ["agent:main:matrix:channel:"] },
      },
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

    const { plugin, testApi } = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
    let transcriptUpdateHook: ((update: any) => void) | undefined;
    const api = {
      ...makeFakeApi(),
      runtime: {
        events: {
          onSessionTranscriptUpdate: vi.fn((hook: (update: any) => void) => {
            transcriptUpdateHook = hook;
          }),
        },
      },
    };
    plugin.register(api as any);

    const targetConfigPath = path.join(hiddenHome, "instances", "openclaw-m13test", "config.json");
    const targetSoulPath = path.join(visibleHome, "instances", "openclaw-m13test", "SOUL.md");
    expect(fs.existsSync(targetConfigPath)).toBe(false);
    expect(fs.existsSync(targetSoulPath)).toBe(false);
    expect(testApi.resolveAgentLabelFromModelName("openclaw/m5run162")).toBe("m5run162");
    expect(
      testApi.resolveHookAgentLabel(
        { model: "openclaw/m5run162" },
        { sessionKey: "agent:main:http-responses" },
      ),
    ).toBe("m5run162");

    const beforeAgentStartCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_start" && call?.[2]?.name === "memory-injection"
    );
    expect(beforeAgentStartCall).toBeTruthy();
    const beforeAgentStartRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_start" && call?.[2]?.name === "memory-injection-registerHook"
    );
    expect(beforeAgentStartRegisterHookCall).toBeTruthy();

    const beforeAgentStartHandler = beforeAgentStartCall?.[1];
    childProcessState.daemonStatusByInstance["openclaw-m13test"] = ["throw", true];
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
    expect(targetConfig.instance?.id).toBe("openclaw-m13test");
    expect(targetConfig.adapter).toEqual({ type: "openclaw" });
    expect(targetConfig.models).toBeUndefined();
    expect(targetConfig.capture).toBeUndefined();
    expect(targetConfig.plugins).toBeUndefined();
    expect(targetConfig.notifications).toBeUndefined();
    expect(
      childProcessState.daemonStartCalls.some(
        (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m13test",
      ),
    ).toBe(true);
    const m13DaemonStarts = childProcessState.daemonStartCalls.filter(
      (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m13test",
    );
    expect(m13DaemonStarts).toHaveLength(2);
    expect(String(m13DaemonStarts[1]?.env?.QUAID_SUPERVISOR_DISABLE || "")).toBe("1");
    expect(
      childProcessState.daemonStatusCalls.filter(
        (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m13test",
      ),
    ).toHaveLength(2);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    const beforePromptBuildRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build-registerHook"
    );
    expect(beforePromptBuildRegisterHookCall).toBeTruthy();
    const beforeResetRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_reset" && call?.[2]?.name === "reset-memory-extraction-registerHook"
    );
    expect(beforeResetRegisterHookCall).toBeTruthy();
    const sessionEndRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "session_end" && call?.[2]?.name === "session-end-memory-extraction-registerHook"
    );
    expect(sessionEndRegisterHookCall).toBeTruthy();
    const beforeCompactionRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_compaction" && call?.[2]?.name === "compaction-memory-extraction-registerHook"
    );
    expect(beforeCompactionRegisterHookCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const modelTargetConfigPath = path.join(hiddenHome, "instances", "openclaw-m5run162", "config.json");
    expect(fs.existsSync(modelTargetConfigPath)).toBe(false);
    childProcessState.daemonStatusByInstance["openclaw-m5run162"] = ["bad-json", true];
    await beforePromptBuildHandler(
      { prompt: "ok", messages: [], model: "openclaw/m5run162", prependContext: "" },
      {
        sessionId: "9650d6bc-a71c-4b59-a08a-7fe9f5d41162",
        sessionKey: "agent:main:http-responses",
      },
    );
    expect(fs.existsSync(modelTargetConfigPath)).toBe(true);
    expect(
      childProcessState.daemonStartCalls.some(
        (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m5run162",
      ),
    ).toBe(true);
    const m5DaemonStarts = childProcessState.daemonStartCalls.filter(
      (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m5run162",
    );
    expect(m5DaemonStarts.length).toBeGreaterThanOrEqual(2);
    expect(m5DaemonStarts.some((call) => String(call.env?.QUAID_SUPERVISOR_DISABLE || "") === "1")).toBe(true);
    expect(
      childProcessState.daemonStatusCalls.filter(
        (call) => String(call.env?.QUAID_INSTANCE || "") === "openclaw-m5run162",
      ).length,
    ).toBeGreaterThanOrEqual(2);
    expect(
      testApi.resolveHookAgentLabel(
        { sessionId: "9650d6bc-a71c-4b59-a08a-7fe9f5d41162" },
        { sessionKey: "agent:main:http-responses" },
      ),
    ).toBe("m5run162");

    const modelTranscriptPath = path.join(openClawRoot, "agents", "main", "sessions", "9650d6bc-a71c-4b59-a08a-7fe9f5d41162.jsonl");
    fs.mkdirSync(path.dirname(modelTranscriptPath), { recursive: true });
    fs.writeFileSync(
      modelTranscriptPath,
      `${JSON.stringify({ role: "user", content: "My tamarind reading chair has a brass desk lamp." })}\n`,
      "utf8",
    );
    expect(transcriptUpdateHook).toBeTruthy();
    transcriptUpdateHook?.({
      sessionId: "9650d6bc-a71c-4b59-a08a-7fe9f5d41162",
      sessionKey: "agent:main:http-responses",
      model: "openclaw/m5run162",
      sessionFile: modelTranscriptPath,
    });
    expect(
      fs.existsSync(path.join(
        hiddenHome,
        "instances",
        "openclaw-m5run162",
        "data",
        "session-cursors",
        "9650d6bc-a71c-4b59-a08a-7fe9f5d41162.json",
      )),
    ).toBe(true);
    expect(
      fs.existsSync(path.join(
        hiddenHome,
        "instances",
        "openclaw-main",
        "data",
        "session-cursors",
        "9650d6bc-a71c-4b59-a08a-7fe9f5d41162.json",
      )),
    ).toBe(false);

    const promptResult = await beforePromptBuildHandler(
      { prompt: "ok", messages: [], prependContext: "" },
      {
        sessionId: "da6cb06a-08b0-4443-bad7-f709df233545",
        sessionKey: "agent:m13test:tui-da6cb06a",
      },
    );

    expect(String(promptResult?.prependSystemContext || "")).toContain("instance: openclaw-m13test");
    expect(String(promptResult?.prependSystemContext || "")).toContain("misc--openclaw-m13test");
    expect(String(promptResult?.prependSystemContext || "")).toContain(
      "quaid registry register <absolute-file-path> --project misc--openclaw-m13test",
    );
    expect(String(promptResult?.prependSystemContext || "")).toContain(
      "LINK ONLY FOR DURABLE ENGAGEMENT",
    );
    expect(String(promptResult?.prependSystemContext || "")).toContain(
      "For a read-only lookup, one-fact question",
    );
    expect(String(promptResult?.prependSystemContext || "")).toContain(
      "run quaid project link <project-name> first",
    );
    expect(String(promptResult?.prependSystemContext || "")).not.toContain("openclaw-main");
    expect(testApi.shouldMirrorTranscriptUpdateToPreservedCopy("agent:main:matrix:channel:!room:localhost")).toBe(true);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });
});
