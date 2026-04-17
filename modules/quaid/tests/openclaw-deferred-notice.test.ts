import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

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

function combinedSystemContext(result: any): string {
  return `${String(result?.prependSystemContext || "")}\n${String(result?.appendSystemContext || "")}`;
}

async function loadAdapterWithHomes(
  hiddenHome: string,
  visibleHome: string,
  openClawConfigPath: string,
  quaidInstance?: string,
): Promise<AdapterPlugin> {
  vi.stubEnv("HOME", path.dirname(hiddenHome));
  vi.stubEnv("OPENCLAW_CONFIG_PATH", openClawConfigPath);
  vi.stubEnv("QUAID_HOME", hiddenHome);
  vi.stubEnv("QUAID_VISIBLE_HOME", visibleHome);
  if (typeof quaidInstance === "string") {
    vi.stubEnv("QUAID_INSTANCE", quaidInstance);
  }
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return module.default as AdapterPlugin;
}

function seedDeferredNoticeFixture(prefix: string, instanceId: string, message: string) {
  const home = makeTempDir(prefix);
  const hiddenHome = path.join(home, ".quaid");
  const visibleHome = path.join(home, "quaid");
  const openClawRoot = path.join(home, ".openclaw");
  const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
  const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");

  fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
  fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");
  writeJson(path.join(hiddenHome, "instances", instanceId, "config.json"), {
    adapter: { type: "openclaw" },
    retrieval: { failHard: false, autoInject: false, maxLimit: 20 },
    models: {
      llmProvider: "openai-codex",
      deepReasoningProvider: "openai-codex",
      fastReasoningProvider: "openai-codex",
      deepReasoning: "gpt-5.1-codex",
      fastReasoning: "gpt-5.1-codex",
    },
    plugins: { strict: false },
  });
  fs.mkdirSync(path.join(hiddenHome, "instances", instanceId, "data"), { recursive: true });
  fs.mkdirSync(path.join(hiddenHome, "instances", instanceId, "logs"), { recursive: true });
  fs.mkdirSync(path.join(visibleHome, "projects", "quaid"), { recursive: true });
  fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "SOUL.md"), "# SOUL\n", "utf8");
  fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "USER.md"), "# USER\n", "utf8");
  fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "ENVIRONMENT.md"), "# ENVIRONMENT\n", "utf8");
  writeJson(openClawConfigPath, {
    agents: { list: [{ id: "main", default: true }] },
    env: { vars: { QUAID_INSTANCE: instanceId } },
  });
  const noticeFile = path.join(
    hiddenHome,
    "instances",
    instanceId,
    ".runtime",
    "notes",
    "delayed-llm-requests.json",
  );
  writeJson(noticeFile, {
    version: 1,
    requests: [{
      id: "notice-test",
      created_at: "2026-04-10T12:00:00Z",
      source: "pytest",
      kind: "janitor_summary",
      priority: "normal",
      status: "pending",
      message,
    }],
  });
  return { home, hiddenHome, visibleHome, openClawConfigPath, noticeFile };
}

afterEach(() => {
  childProcessState.daemonStartCalls = [];
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("openclaw deferred notices", () => {
  it("injects deferred notices into prompt context without claiming the inbound reply", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-visible-reply-home-",
      "openclaw-main",
      "[Quaid] Synthetic notice: silver lantern is ready.",
    );

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const api = makeFakeApi();
    plugin.register(api as any);

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-visible-relay"
    );
    expect(deferredReplyCall).toBeFalsy();

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-visible", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-visible", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("silver lantern");
    expect(systemContext).toContain("[Quaid Notice Relay Required]");
    expect(systemContext).toContain("<quaid_system_message>");

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(fixture.home, { recursive: true, force: true });
  });

  it("drains deferred notices into system context during prompt-build", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-prompt-build-home-",
      "openclaw-main",
      "[Quaid] Deferred drain prompt-build path.",
    );

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hello there", sessionId: "session-main-deferred", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-deferred", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("Deferred drain prompt-build path");
    expect(systemContext).toContain("[Quaid Notice Relay Required]");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(fixture.home, { recursive: true, force: true });
  });

  it("drains deferred notices during prompt-build even when auto-inject is disabled", async () => {
    vi.useFakeTimers();
    const home = makeTempDir("quaid-oc-deferred-home-");
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
      retrieval: { failHard: false, autoInject: false, maxLimit: 20 },
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

    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-main",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );
    writeJson(noticeFile, {
      version: 1,
      requests: [
        {
          id: "janitor-cmVwaWFy",
          created_at: "2026-04-10T12:00:00Z",
          source: "janitor",
          kind: "janitor_summary",
          priority: "normal",
          status: "pending",
          message: "[Quaid] Janitor summary: 3 memories reviewed.",
        },
      ],
    });

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-main");
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const result = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "Hey, what is up?",
        sessionId: "session-main-1",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-main-1",
        sessionKey: "agent:main:tui-main",
      },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("Janitor summary");
    expect(systemContext).toContain("[Quaid Notice Relay Required]");

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-visible-relay"
    );
    expect(deferredReplyCall).toBeFalsy();

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("uses the install-bound main instance for deferred notice drain paths", async () => {
    vi.useFakeTimers();
    const home = makeTempDir("quaid-oc-deferred-bound-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");

    writeJson(path.join(hiddenHome, "instances", "openclaw-livetest", "config.json"), {
      adapter: { type: "openclaw" },
      retrieval: { failHard: false, autoInject: false, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex",
      },
      plugins: { strict: false },
    });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "data"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "logs"), { recursive: true });
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
          QUAID_INSTANCE: "openclaw-livetest",
        },
      },
    });

    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-livetest",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );
    writeJson(noticeFile, {
      version: 1,
      requests: [
        {
          id: "janitor-livetest",
          created_at: "2026-04-10T12:00:00Z",
          source: "janitor",
          kind: "janitor_summary",
          priority: "normal",
          status: "pending",
          message: "[Quaid] Janitor summary: livetest main queue.",
        },
      ],
    });

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-livetest");
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-bound", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-bound", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    expect(combinedSystemContext(result)).toContain("livetest main queue");

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);
    expect(fs.existsSync(path.join(hiddenHome, "instances", "openclaw-main", ".runtime", "notes", "delayed-llm-requests.json"))).toBe(false);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("recovers a stale delayed-requests lock before draining", async () => {
    vi.useFakeTimers();
    const home = makeTempDir("quaid-oc-deferred-stale-lock-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");

    writeJson(path.join(hiddenHome, "instances", "openclaw-livetest", "config.json"), {
      adapter: { type: "openclaw" },
      retrieval: { failHard: false, autoInject: false, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex",
      },
      plugins: { strict: false },
    });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "data"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "logs"), { recursive: true });
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
          QUAID_INSTANCE: "openclaw-livetest",
        },
      },
    });

    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-livetest",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );
    writeJson(noticeFile, {
      version: 1,
      requests: [
        {
          id: "janitor-stale-lock",
          created_at: "2026-04-10T12:00:00Z",
          source: "janitor",
          kind: "janitor_summary",
          priority: "normal",
          status: "pending",
          message: "[Quaid] Janitor summary: stale lock recovery.",
        },
      ],
    });
    const lockPath = `${noticeFile}.lock`;
    fs.mkdirSync(path.dirname(lockPath), { recursive: true });
    fs.writeFileSync(lockPath, "stale", "utf8");
    const staleAt = new Date(Date.now() - 60_000);
    fs.utimesSync(lockPath, staleAt, staleAt);

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-livetest");
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    expect(combinedSystemContext(result)).toContain("stale lock recovery");
    expect(fs.existsSync(lockPath)).toBe(false);

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("surfaces changed invalid model config as same-turn provider context", async () => {
    const home = makeTempDir("quaid-oc-provider-drift-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");
    const configPath = path.join(hiddenHome, "instances", "openclaw-livetest", "config.json");

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "data"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "logs"), { recursive: true });
    fs.mkdirSync(path.join(visibleHome, "projects", "quaid"), { recursive: true });
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "SOUL.md"), "# SOUL\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "USER.md"), "# USER\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "ENVIRONMENT.md"), "# ENVIRONMENT\n", "utf8");

    const validConfig = {
      adapter: { type: "openclaw" },
      systems: { memory: true, projects: false },
      retrieval: { failHard: false, autoInject: true, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex",
      },
      plugins: { strict: false },
    };
    writeJson(configPath, validConfig);
    writeJson(openClawConfigPath, {
      agents: {
        list: [{ id: "main", default: true }],
      },
      env: {
        vars: {
          QUAID_INSTANCE: "openclaw-livetest",
        },
      },
    });

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url: any, init: any) => {
      expect(String(init?.headers?.["x-openclaw-model"] || "")).toContain("invalid-model-xyzzy");
      return {
        ok: false,
        status: 400,
        statusText: "Bad Request",
        text: async () => JSON.stringify({ error: { message: "model not found" } }),
      } as any;
    });

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-livetest");
    const api = makeFakeApi();
    plugin.register(api as any);

    writeJson(configPath, {
      ...validConfig,
      models: {
        ...validConfig.models,
        deepReasoning: "invalid-model-xyzzy",
        fastReasoning: "invalid-model-xyzzy",
      },
    });
    const changedAt = new Date(Date.now() + 10_000);
    fs.utimesSync(configPath, changedAt, changedAt);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const result = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "What do you remember about my family?",
        messages: [{ role: "user", content: "What do you remember about my family?" }],
        sessionId: "session-provider-drift",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-drift",
        sessionKey: "agent:main:tui-main",
      },
    );

    expect(fetchMock).toHaveBeenCalled();
    expect(String(result?.prependContext || "")).toContain("[Quaid error] [provider]");
    expect(String(result?.appendSystemContext || "")).toContain("[Quaid error] [provider]");

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(home, { recursive: true, force: true });
  });

  it("re-arms project context injection after before_compaction under default strategy", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-compaction-refresh-home-",
      "openclaw-main",
      "[Quaid] compaction refresh fixture",
    );
    fs.writeFileSync(
      path.join(fixture.visibleHome, "projects", "quaid", "TOOLS.md"),
      "# TOOLS\nCompaction refresh canary: amber-skyline\n",
      "utf8",
    );

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    const beforeCompactionCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_compaction" && call?.[2]?.name === "compaction-memory-extraction"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(beforeCompactionCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const beforeCompactionHandler = beforeCompactionCall?.[1];
    const promptCtx = {
      sessionId: "session-compaction-refresh",
      sessionKey: "agent:main:tui-main",
      agentId: "main",
      trigger: "user",
    };

    const first = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "first",
        messages: [{ role: "user", content: "first" }],
        sessionId: promptCtx.sessionId,
        sessionKey: promptCtx.sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(first)).toContain("Compaction refresh canary: amber-skyline");

    const second = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "second",
        messages: [{ role: "user", content: "second" }],
        sessionId: promptCtx.sessionId,
        sessionKey: promptCtx.sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(second)).not.toContain("Compaction refresh canary: amber-skyline");

    await beforeCompactionHandler(
      {
        messages: [],
        sessionId: promptCtx.sessionId,
        sessionKey: promptCtx.sessionKey,
      },
      {
        sessionId: promptCtx.sessionId,
        sessionKey: promptCtx.sessionKey,
        agentId: "main",
        trigger: "system",
      },
    );

    const third = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "third",
        messages: [{ role: "user", content: "third" }],
        sessionId: promptCtx.sessionId,
        sessionKey: promptCtx.sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(third)).toContain("Compaction refresh canary: amber-skyline");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    fs.rmSync(fixture.home, { recursive: true, force: true });
  });
});
