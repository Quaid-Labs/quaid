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
