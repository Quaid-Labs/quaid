import { afterEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const childProcessState = vi.hoisted(() => ({
  daemonStartCalls: [] as Array<{ file: string; args: readonly string[]; env: Record<string, string | undefined> }>,
  deferredRelayStdout: "" as string,
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
    spawnSync: ((file: string, args?: readonly string[] | null, options?: any) => {
      const normalizedArgs = Array.isArray(args) ? args.map((arg) => String(arg)) : [];
      const inlineScript = normalizedArgs[0] === "-c" ? String(normalizedArgs[1] || "") : "";
      if (
        childProcessState.deferredRelayStdout
        && inlineScript.includes("format_pending_notice_relay")
        && inlineScript.includes("drain_deferred_notices")
      ) {
        return {
          status: 0,
          signal: null,
          error: undefined,
          stdout: childProcessState.deferredRelayStdout,
          stderr: "",
          output: [null, childProcessState.deferredRelayStdout, ""],
          pid: 0,
        } as any;
      }
      return actual.spawnSync(file, args as any, options);
    }) as typeof actual.spawnSync,
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

function readHookTraceEvents(hiddenHome: string, instanceId: string): any[] {
  const tracePath = path.join(hiddenHome, "instances", instanceId, "logs", "quaid-hook-trace.jsonl");
  if (!fs.existsSync(tracePath)) return [];
  return fs.readFileSync(tracePath, "utf8")
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line));
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
  childProcessState.deferredRelayStdout = "";
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("openclaw deferred notices", () => {
  it("skips prompt-build injection for timestamped slug-generator sessions", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-slug-generator-internal-home-",
      "openclaw-main",
      "[Quaid] slug-generator fixture",
    );
    const configPath = path.join(fixture.hiddenHome, "instances", "openclaw-main", "config.json");
    const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
    writeJson(configPath, {
      ...config,
      systems: { memory: true, projects: false },
      retrieval: { ...config.retrieval, autoInject: true },
    });

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

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const event = {
      prependContext: "",
      prompt: "Generate a short title for this chat",
      messages: [{ role: "user", content: "What grinder do I use for my espresso setup?" }],
      sessionId: "slug-generator-1778267431707",
      sessionKey: "agent:main:slug-generator-1778267431707",
    };
    const result = await beforePromptBuildHandler(event, {
      sessionId: "slug-generator-1778267431707",
      sessionKey: "agent:main:slug-generator-1778267431707",
      agentId: "main",
      trigger: "user",
    });

    expect(result).toBeUndefined();
    expect(event.prependContext).toBe("");
    expect(log.mock.calls.some((call) => String(call.join(" ")).includes("Auto-injected"))).toBe(false);
    const traceEvents = readHookTraceEvents(fixture.hiddenHome, "openclaw-main").map((row) => String(row.event || ""));
    expect(traceEvents).toContain("hook.debug.invoke");
    expect(traceEvents).not.toContain("hook.before_prompt_build.query_extracted");
    expect(traceEvents).not.toContain("hook.before_prompt_build.injection_applied");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers cached-user auto-injection when OC prompt-build body is empty", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-cached-user-inject-home-",
      "openclaw-main",
      "[Quaid] cached-user injection fixture",
    );
    const configPath = path.join(fixture.hiddenHome, "instances", "openclaw-main", "config.json");
    const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
    writeJson(configPath, {
      ...config,
      systems: { memory: true, projects: false },
      retrieval: { ...config.retrieval, autoInject: true },
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

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const adapterModule = await import("../adaptors/openclaw/adapter.js");
    const testApi = (adapterModule as any).__test;
    testApi.clearAutoInjectTurnCaches();
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    const messageReceivedCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "message_received" && call?.[2]?.name === "message-received-command-memory-extraction"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(messageReceivedCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const messageReceivedHandler = messageReceivedCall?.[1];
    const sessionId = "session-empty-body-cached-user";
    const sessionKey = "agent:main:matrix:direct:@quaid-test-bot:localhost";
    const query = "What grinder do I use for my espresso setup?";
    await messageReceivedHandler(
      { text: query, sessionId, sessionKey, timestamp: 1778267431707 },
      { sessionId, sessionKey, agentId: "main", trigger: "user" },
    );

    const memory = {
      id: "mem-baratza",
      text: "Solomon owns a Baratza Encore grinder and a Flair 58 espresso setup.",
      similarity: 1,
      via: "vector",
      category: "fact",
    };
    const turnKey = testApi.autoInjectTurnKey("main", query, sessionKey);
    testApi.rememberCompletedAutoInjectTurn(turnKey, {
      allMemories: [memory],
      recallDiagnostics: { mode: "test" },
      injection: {
        toInject: [memory],
        prependContext: [
          "<injected_memories>",
          "- fact | Solomon owns a Baratza Encore grinder and a Flair 58 espresso setup.",
          "</injected_memories>",
        ].join("\n"),
      },
    }, Date.now());

    const event = {
      prependContext: "",
      prompt: "",
      body: "",
      cleanedBody: "",
      messages: [],
      sessionId,
      sessionKey,
    };
    const result = await beforePromptBuildHandler(event, {
      sessionId,
      sessionKey,
      agentId: "main",
      trigger: "user",
    });

    expect(fetchMock).toHaveBeenCalled();
    expect(String(result?.prependContext || "")).toContain("Baratza Encore");
    expect(String((event as any).prependContext || "")).toContain("Baratza Encore");
    expect(log.mock.calls.some((call) => String(call.join(" ")).includes("Auto-injected 1 memories"))).toBe(true);
    const preinjectPath = path.join(fixture.hiddenHome, "instances", "openclaw-main", "logs", "daemon", "preinject.jsonl");
    const preinjectRows = fs.readFileSync(preinjectPath, "utf8")
      .split(/\r?\n/)
      .filter((line) => line.trim())
      .map((line) => JSON.parse(line));
    expect(preinjectRows.at(-1)).toEqual(expect.objectContaining({
      sessionId,
      sessionKey,
      source: "message_received_cache",
      injectedCount: 1,
    }));

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers deferred notices through before_prompt_build relay context", async () => {
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

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const event = { prompt: "Hey, what is up?", sessionId: "session-main-visible", sessionKey: "agent:main:tui-main" };
    const result = await beforePromptBuildCall?.[1](
      event,
      { sessionId: "session-main-visible", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("MANDATORY: Quaid has active notices for the human user.");
    expect(systemContext).toContain("silver lantern is ready");
    expect(String(result?.prependContext || "")).toContain("silver lantern is ready");
    expect(String((event as any).prependContext || "")).toContain("silver lantern is ready");
    expect(String((event as any).appendSystemContext || "")).toContain("silver lantern is ready");

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

  it("reuses prompt-build deferred notice cache for visible reply without redraining", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-reply-cache-home-",
      "openclaw-main",
      "[Quaid] Cached prompt-build notice must become a visible reply.",
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
    const deferredReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(deferredReplyCall).toBeTruthy();

    const sessionId = "session-main-reply-cache";
    const sessionKey = "agent:main:tui-main";
    const promptResult = await beforePromptBuildCall?.[1](
      {
        prompt: "Hello",
        messages: [{ role: "user", content: "Hello" }],
        sessionId,
        sessionKey,
      },
      { sessionId, sessionKey, agentId: "main", trigger: "user" },
    );
    expect(String(promptResult?.prependContext || "")).toContain("Cached prompt-build notice");

    const afterPromptBuild = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    expect(
      (Array.isArray(afterPromptBuild?.requests) ? afterPromptBuild.requests : [])
        .filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending"),
    ).toHaveLength(0);

    const replyResult = await deferredReplyCall?.[1](
      { sessionId, sessionKey },
      { sessionId, sessionKey, agentId: "main", trigger: "user" },
    );
    expect(replyResult?.handled).toBe(true);
    expect(replyResult?.reason).toBe("deferred_notice_visible_relay");
    expect(String(replyResult?.reply?.text || "")).toContain("Quaid notice:");
    expect(String(replyResult?.reply?.text || "")).toContain("Cached prompt-build notice must become a visible reply");

    const trace = readHookTraceEvents(fixture.hiddenHome, "openclaw-main");
    const visibleReply = trace.find((row) => String(row.event || "") === "deferred_notice.reply_relay_visible_reply");
    expect(visibleReply).toBeTruthy();
    expect(visibleReply?.source).toBe("prompt_build_cache");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("relays deferred notices in before_prompt_build when the reset signal is too old", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-aged-reset-home-",
      "openclaw-main",
      "[Quaid] Aged reset notice should still reach the prompt.",
    );
    childProcessState.deferredRelayStdout = [
      "[quaid] runtime warning before JSON",
      JSON.stringify({
        drained: 1,
        relay: [
          "MANDATORY: Quaid has active notices for the human user.",
          "",
          "<quaid_system_message>",
          "• [Quaid] Aged reset notice should still reach the prompt.",
          "</quaid_system_message>",
        ].join("\n"),
        kinds: ["janitor_summary"],
      }),
      "",
    ].join("\n");

    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const adapterModule = await import("../adaptors/openclaw/adapter.js");
    const api = makeFakeApi();
    plugin.register(api as any);

    const resetMs = Date.parse("2026-04-26T21:32:03.000Z");
    const lateDecision = (adapterModule as any).__test.lateTranscriptUpdateSessionEndDecision(
      "session-aged-reset",
      [{ role: "user", content: "Please remember the aged-reset prompt notice canary." }],
      2048,
      {
        nowMs: resetMs + (7 * 60 * 1000),
        lastResetSignalMs: resetMs,
        alreadySignaled: () => false,
      },
    );
    expect(lateDecision.shouldQueue).toBe(false);
    expect(lateDecision.reason).toBe("reset_signal_too_old");

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      {
        prompt: "Do I have any pending notices?",
        messages: [{ role: "user", content: "Do I have any pending notices?" }],
        sessionId: "session-aged-reset",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-aged-reset",
        sessionKey: "agent:main:tui-main",
        agentId: "main",
        trigger: "user",
      },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("MANDATORY: Quaid has active notices for the human user.");
    expect(systemContext).toContain("Aged reset notice should still reach the prompt");
    expect(String(result?.prependContext || "")).toContain("Aged reset notice should still reach the prompt");
    expect(String((result as any)?.prependContext || "")).not.toContain("runtime warning before JSON");
    expect(
      readHookTraceEvents(fixture.hiddenHome, "openclaw-main")
        .map((row) => String(row.event || "")),
    ).toContain("deferred_notice.relay_context");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("replays deferred prompt-build notices across duplicate OC hook surfaces", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-duplicate-prompt-home-",
      "openclaw-main",
      "[Quaid] M6 test notice: scheduled review found 3 facts.",
    );
    writeJson(fixture.noticeFile, {
      version: 1,
      requests: [{
        id: "m6-partA-duplicate-surface",
        status: "pending",
        kind: "janitor_notice",
        message: "[Quaid] M6 test notice: scheduled review found 3 facts.",
      }],
    });

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
    const beforePromptBuildRegisterCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build-registerHook"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(beforePromptBuildRegisterCall).toBeTruthy();

    const sessionId = "session-main-duplicate-relay";
    const sessionKey = "agent:main:tui-main";
    const ctx = { sessionId, sessionKey, agentId: "main", trigger: "user" };
    const firstResult = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId, sessionKey },
      ctx,
    );
    expect(String(firstResult?.prependContext || "")).toContain("scheduled review found 3 facts");

    const afterFirst = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    expect(
      (Array.isArray(afterFirst?.requests) ? afterFirst.requests : [])
        .filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending"),
    ).toHaveLength(0);

    const secondResult = await beforePromptBuildRegisterCall?.[1](
      { prompt: "Hey, what is up?", sessionId, sessionKey },
      ctx,
    );
    expect(String(secondResult?.prependContext || "")).toContain("scheduled review found 3 facts");
    expect(combinedSystemContext(secondResult)).toContain("MANDATORY: Quaid has active notices for the human user.");

    const afterSecond = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    expect(
      (Array.isArray(afterSecond?.requests) ? afterSecond.requests : [])
        .filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered"),
    ).toHaveLength(1);
    expect(
      readHookTraceEvents(fixture.hiddenHome, "openclaw-main")
        .map((row) => String(row.event || "")),
    ).toContain("deferred_notice.relay_context_reused");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("delivers deferred notices from the install-bound instance during before_prompt_build", async () => {
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
    expect(systemContext).toContain("Deferred drain prompt-build path");
    expect(systemContext).toContain("MANDATORY: Quaid has active notices for the human user.");
    expect(String(result?.prependContext || "")).toContain("Deferred drain prompt-build path");

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

  it("delivers deferred notices even when the first prompt payload is empty", async () => {
    vi.useFakeTimers();
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-empty-prompt-home-",
      "openclaw-main",
      "[Quaid] Empty prompt relay still needs delivery.",
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
      { prompt: "", messages: [], sessionId: "session-main-empty", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-empty", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    const systemContext = combinedSystemContext(result);
    expect(systemContext).toContain("Empty prompt relay still needs delivery");
    expect(String(result?.prependContext || "")).toContain("Empty prompt relay still needs delivery");

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    const delivered = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered")
      : [];
    expect(pending).toHaveLength(0);
    expect(delivered).toHaveLength(1);

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
    expect(systemContext).toContain("Janitor summary: 3 memories reviewed");
    expect(systemContext).toContain("MANDATORY: Quaid has active notices for the human user.");
    expect(String(result?.prependContext || "")).toContain("Janitor summary: 3 memories reviewed");

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

  it("uses the install-bound main instance for deferred notice relay injection", async () => {
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

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-bound", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-bound", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );
    expect(String(result?.prependContext || "")).toContain("livetest main queue");

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

  it("delivers deferred notices in before_prompt_build even when a stale lock file is present", async () => {
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

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();

    const result = await beforePromptBuildCall?.[1](
      { prompt: "Hey, what is up?", sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-stale-lock", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );

    expect(String(result?.prependContext || "")).toContain("stale lock recovery");
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

  it("relays deferred notices as visible replies from before_agent_reply user turns", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-user-reply-home-",
      "openclaw-main",
      "[Quaid] Deferred notice on user reply trigger.",
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
      { sessionId: "session-main-user", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-main-user", sessionKey: "agent:main:tui-main", agentId: "main", trigger: "user" },
    );
    expect(relayResult?.handled).toBe(true);
    expect(relayResult?.reason).toBe("deferred_notice_visible_relay");
    expect(String(relayResult?.reply?.text || "")).toContain("Quaid notice:");
    expect(String(relayResult?.reply?.text || "")).toContain("Deferred notice on user reply trigger");

    const drained = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    const pending = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "pending")
      : [];
    const delivered = Array.isArray(drained?.requests)
      ? drained.requests.filter((item: any) => String(item?.status || "").trim().toLowerCase() === "delivered")
      : [];
    expect(pending).toHaveLength(0);
    expect(delivered).toHaveLength(1);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("parses deferred delivery JSON after notify stdout chatter", async () => {
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-deferred-json-parse-home-",
      "openclaw-main",
      "[Quaid] Deferred notice parser fixture.",
    );
    await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const adapterModule = await import("../adaptors/openclaw/adapter.js");

    const payload = (adapterModule as any).__test.parseJsonObjectFromProcessStdout(
      '[notify] Sent to matrix:!room\n{\n  "delivered": 1,\n  "items": []\n}\n',
    );

    expect(payload.delivered).toBe(1);
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
    expect(String(result?.prependContext || "")).toContain("Start your next response by relaying this exact Quaid error");
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

  it("surfaces invalid model config on short visible user turns", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const home = makeTempDir("quaid-oc-provider-short-turn-home-");
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
        deepReasoning: "invalid-model-short-turn",
        fastReasoning: "invalid-model-short-turn",
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
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url: any, init: any) => {
      expect(String(init?.headers?.["x-openclaw-model"] || "")).toContain("invalid-model-short-turn");
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
        prompt: "sounds good",
        messages: [{ role: "user", content: "sounds good" }],
        sessionId: "session-provider-short-turn",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-short-turn",
        sessionKey: "agent:main:tui-main",
      },
    );

    expect(fetchMock).toHaveBeenCalled();
    const combined = combinedSystemContext(result);
    expect(combined).toContain("[Quaid error] [provider]");
    expect(combined).toContain("janitor summary stays queued");

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(home);
  });

  it("revalidates cached provider notices instead of replaying stale recovery state", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const home = makeTempDir("quaid-oc-provider-stale-cache-home-");
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
        deepReasoning: "invalid-model-stale-cache",
        fastReasoning: "invalid-model-stale-cache",
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
    let fetchCalls = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_url: any, init: any) => {
      fetchCalls += 1;
      expect(String(init?.headers?.["x-openclaw-model"] || "")).toContain("invalid-model-stale-cache");
      if (fetchCalls === 1) {
        return {
          ok: false,
          status: 400,
          statusText: "Bad Request",
          text: async () => JSON.stringify({ error: { message: "model not found" } }),
        } as any;
      }
      return {
        ok: true,
        status: 200,
        statusText: "OK",
        text: async () => JSON.stringify({ output_text: "OK" }),
      } as any;
    });

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath, "openclaw-livetest");
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    const beforePromptBuildHandler = beforePromptBuildCall?.[1];

    const first = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "sounds good",
        messages: [{ role: "user", content: "sounds good" }],
        sessionId: "session-provider-stale-cache-1",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-stale-cache-1",
        sessionKey: "agent:main:tui-main",
      },
    );
    const firstCombined = `${String(first?.prependContext || "")}\n${combinedSystemContext(first)}`;
    expect(firstCombined).toContain("[Quaid error] [provider]");

    const second = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "sounds good",
        messages: [{ role: "user", content: "sounds good" }],
        sessionId: "session-provider-stale-cache-2",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-stale-cache-2",
        sessionKey: "agent:main:tui-main",
      },
    );
    const secondCombined = `${String(second?.prependContext || "")}\n${combinedSystemContext(second)}`;
    expect(fetchCalls).toBeGreaterThanOrEqual(2);
    expect(secondCombined).not.toContain("[Quaid error] [provider]");
    expect(secondCombined).not.toContain("active provider/configuration error");

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

    const result = await beforePromptBuildCall?.[1](
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
    const systemContext = combinedSystemContext(result);
    expect(systemContext).not.toContain("stale pending provider notice");
    expect(systemContext).not.toContain("stale delivered provider notice");
    expect(String(result?.prependContext || "")).not.toContain("stale pending provider notice");
    expect(String(result?.prependContext || "")).not.toContain("stale delivered provider notice");
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

  it("clears stale provider deferred notices before prompt relay when auto-inject is disabled", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-provider-recovery-autoinject-off-home-",
      "openclaw-main",
      "[Quaid] placeholder",
    );
    writeJson(fixture.noticeFile, {
      version: 1,
      requests: [
        {
          id: "provider-agent-notice",
          dedupe_key: "provider-agent-notice",
          created_at: "2026-05-09T08:00:00Z",
          source: "llm_config",
          kind: "agent_notice",
          priority: "high",
          status: "pending",
          message: "[Quaid error] [llm_config] stale provider notice from broken config",
        },
        {
          id: "janitor-pending",
          dedupe_key: "janitor-pending",
          created_at: "2026-05-09T08:01:00Z",
          source: "janitor",
          kind: "janitor_summary",
          priority: "low",
          status: "pending",
          message: "[Quaid] janitor summary remains visible",
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
      {
        prependContext: "",
        prompt: "Clean recovery turn after config restore",
        sessionId: "session-provider-recovery-autoinject-off",
        sessionKey: "agent:main:tui-main",
      },
      {
        sessionId: "session-provider-recovery-autoinject-off",
        sessionKey: "agent:main:tui-main",
      },
    );

    expect(fetchMock).toHaveBeenCalled();
    const systemContext = combinedSystemContext(result);
    expect(systemContext).not.toContain("stale provider notice from broken config");
    expect(String(result?.prependContext || "")).not.toContain("stale provider notice from broken config");
    expect(systemContext).toContain("janitor summary remains visible");

    const payload = JSON.parse(fs.readFileSync(fixture.noticeFile, "utf8"));
    expect(payload.requests).toHaveLength(1);
    expect(String(payload.requests[0]?.source || "")).toBe("janitor");

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("clears stale provider deferred notices before visible reply relay", async () => {
    vi.stubEnv("QUAID_DISABLE_NOTIFICATIONS", "1");
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-provider-recovery-reply-relay-home-",
      "openclaw-main",
      "[Quaid] placeholder",
    );
    writeJson(fixture.noticeFile, {
      version: 1,
      requests: [
        {
          id: "provider-agent-notice",
          dedupe_key: "provider-agent-notice",
          created_at: "2026-05-09T08:00:00Z",
          source: "provider",
          kind: "agent_notice",
          priority: "high",
          status: "pending",
          message: "[Quaid error] [provider] stale provider notice before reply",
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

    const plugin = await loadAdapterWithHomes(
      fixture.hiddenHome,
      fixture.visibleHome,
      fixture.openClawConfigPath,
      "openclaw-main",
    );
    const api = makeFakeApi();
    plugin.register(api as any);

    const beforeReplyCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_reply" && call?.[2]?.name === "deferred-notice-channel-relay"
    );
    expect(beforeReplyCall).toBeTruthy();

    const result = await beforeReplyCall?.[1](
      { sessionId: "session-provider-recovery-reply", sessionKey: "agent:main:tui-main" },
      { sessionId: "session-provider-recovery-reply", sessionKey: "agent:main:tui-main", trigger: "user" },
    );

    expect(fetchMock).toHaveBeenCalled();
    expect(result).toBeUndefined();
    expect(fs.existsSync(fixture.noticeFile)).toBe(false);

    fetchMock.mockRestore();
    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
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

  it("surfaces compact identity refresh on before_agent_start before prompt-build recall", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-compaction-start-refresh-home-",
      "openclaw-main",
      "[Quaid] compaction start refresh fixture",
    );
    const identityDir = path.join(fixture.visibleHome, "instances", "openclaw-main");
    fs.mkdirSync(identityDir, { recursive: true });
    fs.writeFileSync(
      path.join(identityDir, "USER.md"),
      "# USER\nThe office plant is named Bartholomew. It is a fiddle-leaf fig.\n",
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

    const beforeAgentStartCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_start" && call?.[2]?.name === "memory-injection"
    );
    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    const beforeCompactionCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_compaction" && call?.[2]?.name === "compaction-memory-extraction"
    );
    expect(beforeAgentStartCall).toBeTruthy();
    expect(beforePromptBuildCall).toBeTruthy();
    expect(beforeCompactionCall).toBeTruthy();

    const beforeAgentStartHandler = beforeAgentStartCall?.[1];
    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const beforeCompactionHandler = beforeCompactionCall?.[1];
    const ctx = {
      sessionId: "session-compaction-start-refresh",
      sessionKey: "agent:main:matrix:room-compaction-start-refresh",
      agentId: "main",
      trigger: "user",
    };

    await beforeCompactionHandler(
      {
        messages: [],
        sessionId: ctx.sessionId,
        sessionKey: ctx.sessionKey,
      },
      ctx,
    );

    const start = await beforeAgentStartHandler(
      {
        prependContext: "",
        sessionId: ctx.sessionId,
        sessionKey: ctx.sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(start)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(start)).toContain("Bartholomew");
    expect(String(start?.prependContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(start?.prependContext || "")).toContain("Bartholomew");

    const prompt = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "what is the office plant named?",
        messages: [{ role: "user", content: "what is the office plant named?" }],
        sessionId: ctx.sessionId,
        sessionKey: ctx.sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(prompt)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(prompt)).toContain("Bartholomew");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("returns newly armed identity refresh from before_agent_start session transition", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-start-transition-refresh-home-",
      "openclaw-main",
      "[Quaid] start transition refresh fixture",
    );
    const identityDir = path.join(fixture.visibleHome, "instances", "openclaw-main");
    fs.mkdirSync(identityDir, { recursive: true });
    fs.writeFileSync(
      path.join(identityDir, "USER.md"),
      "# USER\nThe office plant is named Bartholomew. It is a fiddle-leaf fig.\n",
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

    const beforeAgentStartTransitionCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_agent_start" && call?.[2]?.name === "before-agent-start-session-transition"
    );
    const beforePromptBuildCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "before_prompt_build" && call?.[2]?.name === "memory-injection-prompt-build"
    );
    expect(beforeAgentStartTransitionCall).toBeTruthy();
    expect(beforePromptBuildCall).toBeTruthy();

    const beforeAgentStartTransitionHandler = beforeAgentStartTransitionCall?.[1];
    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const ctx = {
      sessionId: "session-start-transition-refresh",
      sessionKey: "agent:main:matrix:room-start-transition-refresh",
      agentId: "main",
      trigger: "user",
    };

    const start = await beforeAgentStartTransitionHandler(
      {
        prependContext: "",
        sessionId: ctx.sessionId,
        sessionKey: ctx.sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(start)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(start)).toContain("Bartholomew");
    expect(String(start?.prependSystemContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(start?.prependSystemContext || "")).toContain("Bartholomew");

    const prompt = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "what is the office plant named?",
        messages: [{ role: "user", content: "what is the office plant named?" }],
        sessionId: ctx.sessionId,
        sessionKey: ctx.sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(prompt)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(prompt)).toContain("Bartholomew");

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

  it("re-arms project context injection when /compact is captured as a message event", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-message-compact-refresh-home-",
      "openclaw-main",
      "[Quaid] message compact refresh fixture",
    );
    const toolsPath = path.join(fixture.visibleHome, "projects", "quaid", "TOOLS.md");
    fs.writeFileSync(
      toolsPath,
      "# TOOLS\nMessage compact refresh canary: copper-orchid\n",
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
    const messageReceivedCall = api.on.mock.calls.find((call: any[]) =>
      call?.[0] === "message_received" && call?.[2]?.name === "message-received-command-memory-extraction"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(messageReceivedCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const messageReceivedHandler = messageReceivedCall?.[1];
    const sessionKey = "agent:main:matrix:room-compact-message";
    const ctx = {
      sessionId: "session-message-compact-refresh",
      sessionKey,
      agentId: "main",
      trigger: "user",
    };

    const first = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "first",
        messages: [{ role: "user", content: "first" }],
        sessionId: ctx.sessionId,
        sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(first)).toContain("copper-orchid");

    const second = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "second",
        messages: [{ role: "user", content: "second" }],
        sessionId: ctx.sessionId,
        sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(second)).not.toContain("copper-orchid");

    fs.writeFileSync(
      toolsPath,
      "# TOOLS\nMessage compact refresh canary: brass-fern\n",
      "utf8",
    );

    await messageReceivedHandler(
      {
        message: { role: "user", content: "/compact" },
        sessionId: ctx.sessionId,
        sessionKey,
      },
      ctx,
    );

    const third = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "third",
        messages: [{ role: "user", content: "third" }],
        sessionId: ctx.sessionId,
        sessionKey,
      },
      ctx,
    );
    expect(combinedSystemContext(third)).toContain("brass-fern");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("re-arms project context injection after /new on the same matrix session key", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-new-refresh-key-home-",
      "openclaw-main",
      "[Quaid] /new refresh fixture",
    );
    const identityDir = path.join(fixture.visibleHome, "instances", "openclaw-main");
    fs.mkdirSync(identityDir, { recursive: true });
    fs.writeFileSync(
      path.join(identityDir, "USER.md"),
      "# USER\nInitial OC M7 identity canary: basalt-harbor\n",
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
    const commandNewCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "command:new" && call?.[2]?.name === "command-new-memory-extraction"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(commandNewCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const commandNewHandler = commandNewCall?.[1];
    const sessionKey = "agent:main:matrix:channel:!m7-room:localhost";

    const first = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "first identity check",
        messages: [{ role: "user", content: "first identity check" }],
        sessionId: "session-m7-before-new",
        sessionKey,
      },
      {
        sessionId: "session-m7-before-new",
        sessionKey,
        agentId: "main",
        trigger: "user",
      },
    );
    expect(combinedSystemContext(first)).toContain("basalt-harbor");
    expect(combinedSystemContext(first)).not.toContain("Quaid Refreshed Identity Context");
    expect(String(first?.prependContext || "")).toContain("basalt-harbor");

    fs.writeFileSync(
      path.join(identityDir, "USER.md"),
      "# USER\nThe office plant is named Bartholomew. It is a fiddle-leaf fig.\n",
      "utf8",
    );

    const stillGated = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "second identity check",
        messages: [{ role: "user", content: "second identity check" }],
        sessionId: "session-m7-after-new",
        sessionKey,
      },
      {
        sessionId: "session-m7-after-new",
        sessionKey,
        agentId: "main",
        trigger: "user",
      },
    );
    expect(combinedSystemContext(stillGated)).toContain("Bartholomew");
    expect(combinedSystemContext(stillGated)).not.toContain("Quaid Refreshed Identity Context");
    expect(String(stillGated?.prependContext || "")).toContain("Bartholomew");

    await commandNewHandler(
      {
        action: "new",
        sessionId: "session-m7-after-new",
        sessionKey,
      },
      {
        sessionId: "session-m7-after-new",
        sessionKey,
        agentId: "main",
        trigger: "user",
      },
    );

    const refreshed = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "what is the office plant named?",
        messages: [{ role: "user", content: "what is the office plant named?" }],
        sessionId: "session-m7-after-new",
        sessionKey,
      },
      {
        sessionId: "session-m7-after-new",
        sessionKey,
        agentId: "main",
        trigger: "user",
      },
    );
    expect(combinedSystemContext(refreshed)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(refreshed)).toContain("Bartholomew");
    expect(combinedSystemContext(refreshed)).toContain("fiddle-leaf fig");
    expect(String(refreshed?.prependSystemContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(refreshed?.prependSystemContext || "")).toContain("Bartholomew");
    expect(String(refreshed?.prependSystemContext || "")).toContain("fiddle-leaf fig");
    expect(String(refreshed?.prependContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(refreshed?.prependContext || "")).toContain("Bartholomew");
    expect(String(refreshed?.prependContext || "")).toContain("fiddle-leaf fig");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });

  it("keeps identity context available after startup turns consume /new project docs", async () => {
    vi.useFakeTimers();
    const fixture = seedDeferredNoticeFixture(
      "quaid-oc-new-identity-every-turn-home-",
      "openclaw-main",
      "[Quaid] /new identity fixture",
    );
    const identityDir = path.join(fixture.visibleHome, "instances", "openclaw-main");
    fs.mkdirSync(identityDir, { recursive: true });
    fs.writeFileSync(
      path.join(identityDir, "USER.md"),
      "# USER\nThe office plant is named Bartholomew. It is a fiddle-leaf fig.\n",
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
    const commandNewCall = api.registerHook.mock.calls.find((call: any[]) =>
      call?.[0] === "command:new" && call?.[2]?.name === "command-new-memory-extraction"
    );
    expect(beforePromptBuildCall).toBeTruthy();
    expect(commandNewCall).toBeTruthy();

    const beforePromptBuildHandler = beforePromptBuildCall?.[1];
    const commandNewHandler = commandNewCall?.[1];
    const startupSessionKey = "agent:main:hook:startup-boundary";
    const startupSessionId = "session-m7-startup-boundary";
    const gradedSessionKey = "agent:main:matrix:direct:@quaid-test-bot:localhost";
    const gradedSessionId = "session-m7-graded-turn";

    await commandNewHandler(
      {
        action: "new",
        sessionId: startupSessionId,
        sessionKey: startupSessionKey,
      },
      {
        sessionId: startupSessionId,
        sessionKey: startupSessionKey,
        agentId: "main",
        trigger: "user",
      },
    );

    const startup = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "Hello",
        messages: [{ role: "user", content: "Hello" }],
        sessionId: startupSessionId,
        sessionKey: startupSessionKey,
      },
      {
        sessionId: startupSessionId,
        sessionKey: startupSessionKey,
        agentId: "main",
        trigger: "user",
      },
    );
    expect(combinedSystemContext(startup)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(startup)).toContain("Bartholomew");
    expect(String(startup?.prependContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(startup?.prependContext || "")).toContain("Bartholomew");

    const graded = await beforePromptBuildHandler(
      {
        prependContext: "",
        prompt: "What's the office plant named?",
        messages: [{ role: "user", content: "What's the office plant named?" }],
        sessionId: gradedSessionId,
        sessionKey: gradedSessionKey,
      },
      {
        sessionId: gradedSessionId,
        sessionKey: gradedSessionKey,
        agentId: "main",
        trigger: "user",
      },
    );
    expect(combinedSystemContext(graded)).toContain("Quaid Refreshed Identity Context");
    expect(combinedSystemContext(graded)).toContain("Bartholomew");
    expect(combinedSystemContext(graded)).toContain("fiddle-leaf fig");
    expect(String(graded?.prependSystemContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(graded?.prependSystemContext || "")).toContain("Bartholomew");
    expect(String(graded?.prependSystemContext || "")).toContain("fiddle-leaf fig");
    expect(String(graded?.prependContext || "")).toContain("Quaid Refreshed Identity Context");
    expect(String(graded?.prependContext || "")).toContain("Bartholomew");
    expect(String(graded?.prependContext || "")).toContain("fiddle-leaf fig");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    removeTempDir(fixture.home);
  });
});
