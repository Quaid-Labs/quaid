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

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

function removeTempDir(dir: string): void {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
      return;
    } catch (error: any) {
      const code = String(error?.code || "");
      if (!["ENOTEMPTY", "EBUSY", "EPERM"].includes(code) || attempt === 4) {
        throw error;
      }
      sleepSync(25 * (attempt + 1));
    }
  }
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
  it("delivers deferred notices from before_agent_reply without mutating prompt context", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-visible", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-visible", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).not.toContain("silver lantern");
    expect(systemContext).not.toContain("[Quaid Notice Relay Required]");
    expect(String(result?.prependContext || "")).not.toContain("silver lantern");
    expect(String(result?.prependContext || "")).not.toContain("[Quaid Notice Relay Required]");

    const relayResult = await deferredReplyCall?.[1](
      { sessionId: "session-main-visible", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-visible", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );
    expect(relayResult).toBeUndefined();

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);
    const delivered = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered")
      : [];
    expect(delivered).toHaveLength(1);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers deferred notices from the install-bound instance on before_agent_reply", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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
    expect(systemContext).not.toContain("Deferred drain prompt-build path");
    expect(systemContext).not.toContain("[Quaid Notice Relay Required]");
    expect(String(result?.prependContext || "")).not.toContain("Deferred drain prompt-build path");
    expect(String(result?.prependContext || "")).not.toContain("[Quaid Notice Relay Required]");

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();
    await deferredReplyCall?.[1](
      { sessionId: "session-main-deferred", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-deferred", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers deferred notices even when auto-inject is disabled", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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
    expect(systemContext).not.toContain("Janitor summary");
    expect(systemContext).not.toContain("[Quaid Notice Relay Required]");

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();
    await deferredReplyCall?.[1](
      { sessionId: "session-main-1", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-1", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("uses the install-bound main instance for deferred notice channel delivery", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();

    const result = await deferredReplyCall?.[1](
      { sessionId: "session-main-bound", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-bound", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );
    expect(result).toBeUndefined();

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);
    expect(fs.existsSync(path.join(hiddenHome, "instances", "openclaw-main", ".runtime", "notes", "delayed-llm-requests.json"))).toBe(false);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("delivers deferred notices even when a stale lock file is present", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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

    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();

    const result = await deferredReplyCall?.[1](
      { sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    expect(result).toBeUndefined();
    expect(fs.existsSync(lockPath)).toBe(true);

    const drained = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("delivers deferred notices from before_agent_reply even when trigger is non-user", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-non-user-trigger-home-",
      "openclaw-main",
      "[Quaid] Deferred notice on non-user reply trigger.",
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
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(deferredReplyCall).toBeTruthy();

    const relayResult = await deferredReplyCall?.[1](
      { sessionId: "session-main-non-user", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-non-user", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "assistant" },
    );
    expect(relayResult).toBeUndefined();

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    expect(pending).toHaveLength(0);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers changed invalid model config through deferred channel notices", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
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

    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-livetest",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );
    const payload = fs.existsSync(noticeFile)
      ? JSON.parse(fs.readFileSync(noticeFile, "utf8"))
      : { requests: [] };
    const pending = Array.isArray(payload?.requests)
      ? payload.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    const delivered = Array.isArray(payload?.requests)
      ? payload.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered")
      : [];
    expect(pending).toHaveLength(0);
    expect(delivered).toHaveLength(0);

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("delivers startup-invalid model config on the first user turn", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const home = makeTempDir("quaid-oc-provider-startup-invalid-home-");
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

    writeJson(configPath, {
      adapter: { type: "openclaw" },
      systems: { memory: true, projects: false },
      retrieval: { failHard: false, autoInject: true, maxLimit: 20 },
      models: {
        llmProvider: "openai-codex",
        deepReasoningProvider: "openai-codex",
        fastReasoningProvider: "openai-codex",
        deepReasoning: "invalid-model-startup-xyzzy",
        fastReasoning: "invalid-model-startup-xyzzy",
      },
      plugins: { strict: false },
    });
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
      expect(String(init?.headers?.["x-openclaw-model"] || "")).toContain("invalid-model-startup-xyzzy");
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

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      {
        prependContext: "",
        prompt: "What do you remember about my family?",
        messages: [{ role: "user", content: "What do you remember about my family?" }],
        sessionId: "session-provider-startup-invalid",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-startup-invalid",
        sessionKey: "agent:main:tui-main",
      },
    );

    expect(fetchMock).toHaveBeenCalled();
    expect(String(result?.prependContext || "")).toContain("[Quaid error] [provider]");
    expect(String(result?.appendSystemContext || "")).toContain("[Quaid error] [provider]");

    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-livetest",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );
    const payload = fs.existsSync(noticeFile)
      ? JSON.parse(fs.readFileSync(noticeFile, "utf8"))
      : { requests: [] };
    const pending = Array.isArray(payload?.requests)
      ? payload.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    const delivered = Array.isArray(payload?.requests)
      ? payload.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered")
      : [];
    expect(pending).toHaveLength(0);
    expect(delivered).toHaveLength(0);

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("clears stale provider deferred notices after config recovery", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const home = makeTempDir("quaid-oc-provider-recovery-home-");
    const hiddenHome = path.join(home, ".quaid");
    const visibleHome = path.join(home, "quaid");
    const openClawRoot = path.join(home, ".openclaw");
    const openClawConfigPath = path.join(openClawRoot, "openclaw.json");
    const repoModulesRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
    const linkedModulesRoot = path.join(hiddenHome, "modules", "quaid");
    const configPath = path.join(hiddenHome, "instances", "openclaw-livetest", "config.json");
    const noticeFile = path.join(
      hiddenHome,
      "instances",
      "openclaw-livetest",
      ".runtime",
      "notes",
      "delayed-llm-requests.json",
    );

    fs.mkdirSync(path.dirname(linkedModulesRoot), { recursive: true });
    fs.symlinkSync(repoModulesRoot, linkedModulesRoot, "dir");
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "data"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "instances", "openclaw-livetest", "logs"), { recursive: true });
    fs.mkdirSync(path.join(visibleHome, "projects", "quaid"), { recursive: true });
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "SOUL.md"), "# SOUL\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "USER.md"), "# USER\n", "utf8");
    fs.writeFileSync(path.join(visibleHome, "projects", "quaid", "ENVIRONMENT.md"), "# ENVIRONMENT\n", "utf8");

    writeJson(configPath, {
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
    });
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
    writeJson(noticeFile, {
      version: 1,
      requests: [
        {
          id: "provider-pending",
          dedupe_key: "provider-pending",
          created_at: "2026-04-25T00:00:00Z",
          source: "provider",
          kind: "provider",
          priority: "high",
          status: "pending",
          message: "[Quaid error] [provider] stale pending provider notice",
        },
        {
          id: "provider-delivered",
          dedupe_key: "provider-delivered",
          created_at: "2026-04-25T00:01:00Z",
          source: "provider",
          kind: "provider",
          priority: "high",
          status: "delivered",
          delivered_at: "2026-04-25T00:02:00Z",
          message: "[Quaid error] [provider] stale delivered provider notice",
        },
        {
          id: "janitor-pending",
          dedupe_key: "janitor-pending",
          created_at: "2026-04-25T00:03:00Z",
          source: "janitor",
          kind: "janitor_summary",
          priority: "low",
          status: "pending",
          message: "[Quaid] janitor summary stays queued",
        },
      ],
    });

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      text: async () => "OK",
    } as any));

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-livetest");
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    await beforePromptBuildCall?.[1](
      {
        prependContext: "",
        prompt: "What do you remember about my family?",
        messages: [{ role: "user", content: "What do you remember about my family?" }],
        sessionId: "session-provider-recovery",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-recovery",
        sessionKey: "agent:main:tui-main",
      },
    );

    expect(fetchMock).toHaveBeenCalled();
    const payload = JSON.parse(fs.readFileSync(noticeFile, "utf8"));
    expect(Array.isArray(payload?.requests)).toBe(true);
    expect(payload.requests).toHaveLength(1);
    expect(String(payload.requests[0]?.source || "")).toBe("janitor");
    expect(String(payload.requests[0]?.message || "")).toContain("janitor summary");

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
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
    removeTempDir(fixture.home);
  });

  it("re-arms project context injection when before_compaction uses a different session id on the same session key", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-compaction-refresh-key-home-",
      "openclaw-main",
      "[Quaid] compaction refresh session-key fixture",
    );
    fs.writeFileSync(
      path.join(fixture.visibleHome, "projects", "quaid", "TOOLS.md"),
      "# TOOLS\nCompaction refresh canary: mortimer-fern\n",
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
    const sessionKey = "agent:main:matrix:room-mortimer";
    const promptCtx = {
      sessionId: "session-visible-room",
      sessionKey,
      agentId: "main",
      trigger: "user",
    };

    const first = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "first",
        messages: [{ role: "user", content: "first" }],
        sessionId: promptCtx.sessionId,
        sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(first)).toContain("Compaction refresh canary: mortimer-fern");

    const second = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "second",
        messages: [{ role: "user", content: "second" }],
        sessionId: promptCtx.sessionId,
        sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(second)).not.toContain("Compaction refresh canary: mortimer-fern");

    await beforeCompactionHandler(
      {
        messages: [],
        sessionId: "session-compaction-worker",
        sessionKey,
      },
      {
        sessionId: "session-compaction-worker",
        sessionKey,
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
        sessionKey,
      },
      promptCtx,
    );
    expect(combinedSystemContext(third)).toContain("Compaction refresh canary: mortimer-fern");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });
});
