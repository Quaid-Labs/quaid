import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, readFileSync, readdirSync, rmSync, statSync, utimesSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

type AdapterPlugin = {
  register: (api: any) => void;
};

type Harness = {
  root: string;
  quaidHome: string;
  openClawConfigPath: string;
  sessionsDir: string;
  signalDir: string;
};

function writeFile(filePath: string, content: string): void {
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, content, "utf8");
}

function writeJson(filePath: string, value: unknown): void {
  writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function writeTranscript(filePath: string, messages: string[]): void {
  const lines = messages.map((text) => JSON.stringify({ role: "user", content: text }));
  writeFile(filePath, `${lines.join("\n")}\n`);
}

function writeAssistantTranscript(filePath: string, messages: string[]): void {
  const lines = messages.map((text) => JSON.stringify({ role: "assistant", content: text }));
  writeFile(filePath, `${lines.join("\n")}\n`);
}

function makeHarness(caseName: string): Harness {
  const root = join(tmpdir(), `quaid-oc-session-index-${caseName}-${Date.now()}`);
  const quaidHome = join(root, ".quaid");
  const openClawRoot = join(root, ".openclaw");
  const openClawConfigPath = join(openClawRoot, "openclaw.json");
  const sessionsDir = join(openClawRoot, "agents", "main", "sessions");
  const signalDir = join(quaidHome, "instances", "openclaw-main", "data", "extraction-signals");
  const installedAt = new Date(Date.now() - 5 * 60_000).toISOString();

  writeJson(join(quaidHome, "config", "config.json"), {
    models: {
      llmProvider: "openai-codex",
      deepReasoningProvider: "openai-codex",
      fastReasoningProvider: "openai-codex",
      deepReasoning: "gpt-5.1-codex",
      fastReasoning: "gpt-5.1-codex",
    },
    retrieval: {
      failHard: false,
      maxLimit: 20,
    },
    plugins: {
      strict: false,
    },
  });
  writeJson(join(quaidHome, "instances", "openclaw-main", "data", "installed-at.json"), {
    installedAt,
  });
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
  writeJson(join(quaidHome, "modules", "quaid", "adaptors", "openclaw", "plugin.json"), {
    capabilities: {
      contract: {
        api: { exports: ["openclaw_adapter_entry", "/plugins/quaid/llm", "/memory/injected"] },
        events: { exports: ["agent_end"] },
        tools: { exports: ["memory_recall"] },
      },
    },
  });
  writeFile(join(quaidHome, "modules", "quaid", "datastore", "memorydb", "memory_graph.py"), "print('{}')\n");
  writeFile(join(quaidHome, "modules", "quaid", "core", "lifecycle", "janitor.py"), "print('ok')\n");
  return {
    root,
    quaidHome,
    openClawConfigPath,
    sessionsDir,
    signalDir,
  };
}

function makeFakeApi() {
  return {
    on: vi.fn(() => {}),
    registerHook: vi.fn(() => {}),
    registerHttpRoute: vi.fn(() => {}),
    registerTool: vi.fn(() => {}),
  };
}

async function loadPlugin(harness: Harness): Promise<AdapterPlugin> {
  vi.stubEnv("OPENCLAW_CONFIG_PATH", harness.openClawConfigPath);
  vi.stubEnv("QUAID_HOME", harness.quaidHome);
  vi.stubEnv("QUAID_VISIBLE_HOME", harness.quaidHome.replace("/.quaid", "/quaid"));
  vi.stubEnv("QUAID_INSTANCE", "openclaw-main");
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return module.default as AdapterPlugin;
}

function readSignalPayloads(signalDir: string): any[] {
  try {
    return readdirSync(signalDir)
      .filter((name) => name.endsWith(".json"))
      .map((name) => JSON.parse(readFileSync(join(signalDir, name), "utf8")));
  } catch {
    return [];
  }
}

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("openclaw session_index watcher", () => {
  it("routes command:new from a named empty TUI lane to the generated content transcript", async () => {
    vi.useFakeTimers();
    const harness = makeHarness("command-new-generated-content");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const contentSessionId = "1e50152d-1111-4111-8111-111111111111";
    const emptyNamedSessionId = "acb1723d-2222-4222-8222-222222222222";
    const workerSessionId = "ad2d0f29-3333-4333-8333-333333333333";
    const contentTranscript = join(harness.sessionsDir, `${contentSessionId}.jsonl`);
    const emptyNamedTranscript = join(harness.sessionsDir, `${emptyNamedSessionId}.jsonl`);
    const workerTranscript = join(harness.sessionsDir, `${workerSessionId}.jsonl`);
    const now = Date.now();

    writeTranscript(contentTranscript, [
      "Just making conversation, do not store this manually - my brother David works at Google. David is married to Lisa, and they have a son named Oliver.",
    ]);
    writeAssistantTranscript(emptyNamedTranscript, [
      "Quaid had 1 deferred notice waiting and drained it before this turn.",
    ]);
    writeAssistantTranscript(workerTranscript, [
      "OpenResponses worker completed background routing.",
    ]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      "agent:main:tui-generated": { sessionId: contentSessionId, updatedAt: now - 1_000 },
      "agent:main:m7-verify": { sessionId: emptyNamedSessionId, updatedAt: now },
    });
    utimesSync(contentTranscript, new Date(now - 1_000), new Date(now - 1_000));
    utimesSync(emptyNamedTranscript, new Date(now), new Date(now));
    utimesSync(workerTranscript, new Date(now + 500), new Date(now + 500));

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
    const plugin = await loadPlugin(harness);
    plugin.register(api as any);
    expect(typeof transcriptUpdateHook).toBe("function");

    // OC can report the TUI conversation as update.sessionId while the file being
    // updated is an openresponses worker transcript. That must not poison the
    // target session -> transcript path map used by command:new extraction.
    transcriptUpdateHook?.({
      sessionId: contentSessionId,
      sessionKey: "agent:main:openresponses:worker",
      sessionFile: workerTranscript,
    });

    const commandNewHook = api.registerHook.mock.calls.find((call: any[]) =>
      call[0] === "command:new" && call[2]?.name === "command-new-memory-extraction"
    )?.[1];
    expect(typeof commandNewHook).toBe("function");

    await commandNewHook(
      {
        action: "new",
        sessionId: emptyNamedSessionId,
        sessionKey: "agent:main:m7-verify",
        context: {
          sessionEntry: {
            sessionId: emptyNamedSessionId,
            sessionFile: emptyNamedTranscript,
          },
        },
      },
      {
        sessionId: emptyNamedSessionId,
        sessionKey: "agent:main:m7-verify",
      },
    );

    const payloads = readSignalPayloads(harness.signalDir);
    expect(payloads).toEqual([
      expect.objectContaining({
        session_id: contentSessionId,
        type: "reset",
        meta: expect.objectContaining({
          source: "command:new",
          command: "new",
          hook_session_id: contentSessionId,
          hook_session_key: "agent:main:m7-verify",
        }),
      }),
    ]);
    expect(payloads[0]?.transcript_path).not.toBe(workerTranscript);
    expect(readFileSync(String(payloads[0]?.transcript_path || ""), "utf8")).toContain("David works at Google");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    rmSync(harness.root, { recursive: true, force: true });
  });

  it("routes visual /new fallback to the last user-active session instead of a notice-only lane", async () => {
    vi.useFakeTimers();
    const harness = makeHarness("visual-new-active-session");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const seedSessionId = "049e8f87-1111-4111-8111-111111111111";
    const noticeSessionId = "1e50152d-2222-4222-8222-222222222222";
    const newSessionId = "77777777-7777-4777-8777-777777777777";
    const seedTranscript = join(harness.sessionsDir, `${seedSessionId}.jsonl`);
    const noticeTranscript = join(harness.sessionsDir, `${noticeSessionId}.jsonl`);
    const now = Date.now();

    writeTranscript(seedTranscript, ["David works at Google, is married to Lisa, and has a son Oliver."]);
    writeAssistantTranscript(noticeTranscript, ["Quaid has 1 deferred maintenance notice waiting provider=1."]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      "agent:main:tui-seed": { sessionId: seedSessionId, updatedAt: now },
      "agent:main:tui-notice": { sessionId: noticeSessionId, updatedAt: now + 1_000 },
    });
    utimesSync(seedTranscript, new Date(now - 2_000), new Date(now - 2_000));
    utimesSync(noticeTranscript, new Date(now - 1_000), new Date(now - 1_000));

    const api = makeFakeApi();
    const plugin = await loadPlugin(harness);
    plugin.register(api as any);

    const beforeAgentStart = api.on.mock.calls.find((call: any[]) =>
      call[0] === "before_agent_start" && call[2]?.name === "before-agent-start-session-transition"
    )?.[1];
    expect(typeof beforeAgentStart).toBe("function");

    await beforeAgentStart(
      { sessionId: newSessionId, sessionKey: "agent:main:tui-new" },
      { sessionId: newSessionId, sessionKey: "agent:main:tui-new" },
    );

    const payloads = readSignalPayloads(harness.signalDir);
    expect(payloads).toEqual([
      expect.objectContaining({
        session_id: seedSessionId,
        type: "reset",
        meta: expect.objectContaining({
          source: "before_agent_start_fallback",
          prior_session_id: seedSessionId,
          new_session_id: newSessionId,
        }),
      }),
    ]);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    rmSync(harness.root, { recursive: true, force: true });
  });

  it("uses the last transcript hint when a named --message key rebinds before watcher sees the seed", async () => {
    vi.useFakeTimers();
    const harness = makeHarness("new-key-rebind-transcript-hint");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const staleSessionId = "afa62a87-1111-4111-8111-111111111111";
    const contentSessionId = "244f403b-2222-4222-8222-222222222222";
    const newSessionId = "7ffe6f8d-3333-4333-8333-333333333333";
    const now = Date.now();
    const staleTranscript = join(harness.sessionsDir, `${staleSessionId}.jsonl`);
    const contentTranscript = join(harness.sessionsDir, `${contentSessionId}.jsonl`);
    const newTranscript = join(harness.sessionsDir, `${newSessionId}.jsonl`);

    writeTranscript(staleTranscript, ["Old telegram content that should not receive the reset."]);
    writeTranscript(contentTranscript, ["My cousin Jake lives in Portland and owns Riverview Books."]);
    writeAssistantTranscript(newTranscript, ["New named session bootstrap."]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      "agent:main:telegram:group:-5221680718": { sessionId: staleSessionId, updatedAt: now },
    });
    utimesSync(staleTranscript, new Date(now), new Date(now));
    utimesSync(contentTranscript, new Date(now + 1_000), new Date(now + 1_000));
    utimesSync(newTranscript, new Date(now + 2_000), new Date(now + 2_000));

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
    const plugin = await loadPlugin(harness);
    plugin.register(api as any);
    expect(typeof transcriptUpdateHook).toBe("function");

    transcriptUpdateHook?.({
      sessionId: contentSessionId,
      sessionKey: "agent:main:m7-final",
      sessionFile: contentTranscript,
    });

    writeJson(join(harness.sessionsDir, "sessions.json"), {
      "agent:main:telegram:group:-5221680718": { sessionId: staleSessionId, updatedAt: now + 2_000 },
      "agent:main:m7-final": { sessionId: newSessionId, updatedAt: now + 3_000 },
    });

    vi.advanceTimersByTime(1_000);
    expect(readSignalPayloads(harness.signalDir)).toHaveLength(0);

    vi.advanceTimersByTime(2_000);

    const payloads = readSignalPayloads(harness.signalDir);
    expect(payloads).toEqual([
      expect.objectContaining({
        session_id: contentSessionId,
        type: "reset",
        meta: expect.objectContaining({
          source: "session_index_new_key",
          new_key: "agent:main:m7-final",
          new_session_id: newSessionId,
        }),
      }),
    ]);
    expect(readFileSync(String(payloads[0]?.transcript_path || ""), "utf8")).toContain("Riverview Books");

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    rmSync(harness.root, { recursive: true, force: true });
  });

  it("keeps an armed new-key fallback when a later key transition cannot queue directly", async () => {
    vi.useFakeTimers();
    const harness = makeHarness("rapid-new");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});

    const oldSessionId = "11111111-1111-4111-8111-111111111111";
    const newSessionId = "22222222-2222-4222-8222-222222222222";
    const replacementSessionId = "33333333-3333-4333-8333-333333333333";
    const laneKey = "agent:main:tui-lane-a";
    const newKey = "agent:main:tui-lane-b";
    const oldTranscript = join(harness.sessionsDir, `${oldSessionId}.jsonl`);
    const newTranscript = join(harness.sessionsDir, `${newSessionId}.jsonl`);
    const replacementTranscript = join(harness.sessionsDir, `${replacementSessionId}.jsonl`);

    writeTranscript(oldTranscript, ["first session remembers kiln trips"]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      [laneKey]: { sessionId: oldSessionId, updatedAt: Date.now() },
    });

    const plugin = await loadPlugin(harness);
    plugin.register(makeFakeApi() as any);

    writeTranscript(newTranscript, ["second session is now active"]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      [laneKey]: { sessionId: oldSessionId, updatedAt: Date.now() },
      [newKey]: { sessionId: newSessionId, updatedAt: Date.now() + 1 },
    });
    vi.advanceTimersByTime(1000);

    expect(statSync(oldTranscript).size).toBeGreaterThan(0);

    writeFile(oldTranscript, "");
    utimesSync(oldTranscript, new Date(), new Date());
    writeTranscript(replacementTranscript, ["replacement lane keeps running"]);
    writeJson(join(harness.sessionsDir, "sessions.json"), {
      [laneKey]: { sessionId: replacementSessionId, updatedAt: Date.now() + 2 },
      [newKey]: { sessionId: newSessionId, updatedAt: Date.now() + 1 },
    });
    vi.advanceTimersByTime(1000);

    expect(readSignalPayloads(harness.signalDir)).toHaveLength(0);

    vi.advanceTimersByTime(1000);

    const payloads = readSignalPayloads(harness.signalDir).filter((payload) => payload?.session_id === oldSessionId);
    expect(payloads).toEqual([
      expect.objectContaining({
        session_id: oldSessionId,
        type: "reset",
        meta: expect.objectContaining({
          source: "session_index_new_key",
        }),
      }),
    ]);

    warn.mockRestore();
    log.mockRestore();
    error.mockRestore();
    rmSync(harness.root, { recursive: true, force: true });
  });
});
