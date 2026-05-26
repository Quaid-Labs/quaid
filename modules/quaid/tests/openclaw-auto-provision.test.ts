import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { EventEmitter } from "node:events";

type AdapterPlugin = {
  register: (api: any) => void;
};
type AdapterTestApi = {
  shouldMirrorTranscriptUpdateToPreservedCopy: (sessionKey: string) => boolean;
  resolveAgentLabelFromModelName: (modelName: unknown) => string;
  resolveHookAgentLabel: (event: any, ctx: any) => string;
  rememberSessionTranscriptPath: (sessionId: string, transcriptPath: string, source?: string, opts?: Record<string, unknown>) => boolean;
  preserveSessionTranscript: (sessionId: string, preferredPath: string, reason: string) => string | null;
  writeDaemonSignal: (sessionId: string, signalType: "compaction" | "reset" | "session_end" | "timeout", meta?: Record<string, unknown>) => string | null;
};
type LoadedAdapter = {
  plugin: AdapterPlugin;
  testApi: AdapterTestApi;
};

const childProcessState = vi.hoisted(() => ({
  daemonStartCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  daemonStatusCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  daemonStatusByInstance: {} as Record<string, Array<boolean | "throw" | "bad-json">>,
  datastoreStatsSyncCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  datastoreStatsSpawnCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
}));

vi.mock("node:child_process", async () => {
  const actual = await vi.importActual<typeof import("node:child_process")>("node:child_process");
  const fsMod = await vi.importActual<typeof import("node:fs")>("node:fs");
  const pathMod = await vi.importActual<typeof import("node:path")>("node:path");
  return {
    ...actual,
    spawnSync: ((file: string, args?: readonly string[] | null, options?: any) => {
      const normalizedArgs = Array.isArray(args) ? args.map((arg) => String(arg)) : [];
      const script = normalizedArgs[1] || "";
      if (normalizedArgs[0] === "-c" && script.includes("sys.version_info")) {
        return { status: 0, stdout: "", stderr: "", error: undefined } as any;
      }
      if (normalizedArgs[0] === "-c" && script.includes("_auto_provision_from_env_if_needed")) {
        const env = (options?.env || {}) as Record<string, string | undefined>;
        const instance = String(env.QUAID_INSTANCE || "");
        const hiddenHome = String(env.QUAID_HOME || "");
        const visibleHome = String(env.QUAID_VISIBLE_HOME || "");
        if (instance && hiddenHome && visibleHome) {
          const hiddenRoot = pathMod.join(hiddenHome, "instances", instance);
          const visibleRoot = pathMod.join(visibleHome, "instances", instance);
          fsMod.mkdirSync(pathMod.join(hiddenRoot, "data"), { recursive: true });
          fsMod.mkdirSync(pathMod.join(hiddenRoot, "logs"), { recursive: true });
          fsMod.mkdirSync(pathMod.join(visibleRoot, "journal"), { recursive: true });
          fsMod.writeFileSync(
            pathMod.join(hiddenRoot, "config.json"),
            `${JSON.stringify({ instance: { id: instance }, adapter: { type: "openclaw" } }, null, 2)}\n`,
            "utf8",
          );
          for (const name of ["SOUL.md", "USER.md", "ENVIRONMENT.md"]) {
            fsMod.writeFileSync(pathMod.join(visibleRoot, name), `# ${name.replace(/\\.md$/, "")}\n`, "utf8");
          }
        }
        return { status: 0, stdout: "", stderr: "", error: undefined } as any;
      }
      if (normalizedArgs[0] === "-c" && script.includes("drain_deferred_notices")) {
        return {
          status: 0,
          stdout: JSON.stringify({ drained: 0, messages: [], kinds: [] }),
          stderr: "",
          error: undefined,
        } as any;
      }
      return actual.spawnSync(file, args as any, options);
    }) as typeof actual.spawnSync,
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
      if (normalizedArgs[1] === "stats") {
        childProcessState.datastoreStatsSyncCalls.push({
          file,
          args: normalizedArgs,
          env: (options?.env || {}) as Record<string, string | undefined>,
        });
      }
      return actual.execFileSync(file, args as any, options);
    }) as typeof actual.execFileSync,
    spawn: ((file: string, args?: readonly string[] | null, options?: any) => {
      const normalizedArgs = Array.isArray(args) ? args.map((arg) => String(arg)) : [];
      if (normalizedArgs[1] === "stats") {
        childProcessState.datastoreStatsSpawnCalls.push({
          file,
          args: normalizedArgs,
          env: (options?.env || {}) as Record<string, string | undefined>,
        });
        const proc = new EventEmitter() as EventEmitter & {
          stdout: EventEmitter;
          stderr: EventEmitter;
          kill: ReturnType<typeof vi.fn>;
        };
        proc.stdout = new EventEmitter();
        proc.stderr = new EventEmitter();
        proc.kill = vi.fn();
        queueMicrotask(() => {
          proc.stdout.emit("data", JSON.stringify({
            total_nodes: 10,
            edges: 0,
            active_nodes: 10,
            last_janitor_completed_at: "2020-01-01T00:00:00.000Z",
          }));
          proc.emit("close", 0);
        });
        return proc;
      }
      return actual.spawn(file, args as any, options);
    }) as typeof actual.spawn,
  };
});

function writeJson(filePath: string, value: unknown): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function readTraceEvents(hiddenHome: string, instanceId: string): any[] {
  const tracePath = path.join(hiddenHome, "instances", instanceId, "logs", "quaid-hook-trace.jsonl");
  if (!fs.existsSync(tracePath)) return [];
  return fs.readFileSync(tracePath, "utf8")
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => JSON.parse(line));
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
  childProcessState.datastoreStatsSyncCalls = [];
  childProcessState.datastoreStatsSpawnCalls = [];
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
      retrieval: { failHard: false, maxLimit: 20, autoInject: false },
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

  it("does not fail plugin register when boot daemon warmup misses under failHard", async () => {
    const home = makeTempDir("quaid-oc-boot-daemon-soft-home-");
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
      retrieval: { failHard: true, maxLimit: 20, autoInject: false },
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

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    childProcessState.daemonStatusByInstance["openclaw-main"] = [false, false];
    const { plugin } = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    expect(
      readTraceEvents(hiddenHome, "openclaw-main").some(
        (event) => event.event === "daemon.ensure_alive.boot_warmup_failed" &&
          event.fail_hard === true,
      ),
    ).toBe(true);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("routes daemon signals to the agent silo named by the OC transcript path", async () => {
    const home = makeTempDir("quaid-oc-agent-signal-route-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");
    const sessionId = "86d47463-3140-476d-89f5-13c566d123a1";
    const transcriptPath = path.join(openClawRoot, "agents", "m5iso004100", "sessions", `${sessionId}.jsonl`);

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");
    fs.mkdirSync(path.dirname(transcriptPath), { recursive: true });
    fs.writeFileSync(
      transcriptPath,
      `${JSON.stringify({ role: "user", content: "The reading chair has a brass desk lamp." })}\n`,
      "utf8",
    );
    writeJson(path.join(hiddenHome, "instances", "openclaw-main", "config.json"), {
      adapter: { type: "openclaw" },
      retrieval: { failHard: false },
      plugins: { strict: false },
    });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-main", "data"), { recursive: true });
    fs.mkdirSync(path.dirname(openClawConfigPath), { recursive: true });
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

    const { testApi } = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
    expect(testApi.rememberSessionTranscriptPath(
      sessionId,
      transcriptPath,
      "transcript-update-resolved-session-id",
      { trustedSessionMapping: true },
    )).toBe(true);
    const preservedPath = testApi.preserveSessionTranscript(sessionId, transcriptPath, "command-new");
    expect(preservedPath).toBeTruthy();

    const sigPath = testApi.writeDaemonSignal(sessionId, "session_end", { source: "session_end" });

    expect(sigPath).toBeTruthy();
    expect(String(sigPath)).toContain(`${path.sep}instances${path.sep}openclaw-m5iso004100${path.sep}`);
    expect(String(sigPath)).not.toContain(`${path.sep}instances${path.sep}openclaw-main${path.sep}`);
    const payload = JSON.parse(fs.readFileSync(String(sigPath), "utf8"));
    expect(payload.transcript_path).toBe(preservedPath);
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
      retrieval: { failHard: false, maxLimit: 20, autoInject: false },
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
    childProcessState.datastoreStatsSyncCalls = [];
    childProcessState.datastoreStatsSpawnCalls = [];
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
    expect(childProcessState.datastoreStatsSyncCalls).toHaveLength(0);
    await vi.waitFor(() => {
      expect(childProcessState.datastoreStatsSpawnCalls).toHaveLength(1);
    });
    expect(
      readTraceEvents(hiddenHome, "openclaw-main").some(
        (event) => event.event === "hook.before_agent_start.janitor_health_queued" &&
          event.reason === "async_stats" &&
          event.instance_id === "openclaw-m13test",
      ),
    ).toBe(true);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(beforePromptBuildCall?.[2]?.timeout).toBeGreaterThanOrEqual(60_000);
    const beforePromptBuildRegisterHookCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build-registerHook"
    );
    expect(beforePromptBuildRegisterHookCall).toBeTruthy();
    expect(beforePromptBuildRegisterHookCall?.[2]?.timeout).toBeGreaterThanOrEqual(60_000);
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
      readTraceEvents(hiddenHome, "openclaw-main").some(
        (event) => event.event === "daemon.ensure_alive.supervisor_miss" &&
          event.instance_id === "openclaw-m5run162" &&
          event.reason === "status_probe_failed" &&
          String(event.status_error || "").includes("invalid daemon status JSON"),
      ),
    ).toBe(true);

    childProcessState.daemonStatusByInstance["openclaw-m5fail"] = ["bad-json", false];
    await beforePromptBuildHandler(
      { prompt: "ok", messages: [], model: "openclaw/m5fail", prependContext: "" },
      {
        sessionId: "11f77f60-ded9-42c7-a0d2-61fc179db1bd",
        sessionKey: "agent:main:http-responses-fail",
      },
    );
    expect(
      readTraceEvents(hiddenHome, "openclaw-main").some(
        (event) => event.event === "daemon.ensure_alive.failed" &&
          event.instance_id === "openclaw-m5fail" &&
          event.reason === "status_probe_failed" &&
          String(event.status_error || "").includes("invalid daemon status JSON"),
      ),
    ).toBe(true);
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
