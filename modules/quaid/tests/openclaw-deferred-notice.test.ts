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

describe("openclaw deferred notices", () => {
  it("drains deferred notices into prompt-build relay context", async () => {
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

    const plugin = await loadAdapterWithHomes(hiddenHome, visibleHome, openClawConfigPath);
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

    expect(String(result?.appendSystemContext || "")).toContain("pending notifications for the user");
    expect(String(result?.appendSystemContext || "")).toContain("Janitor summary");

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
});
