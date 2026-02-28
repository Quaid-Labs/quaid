import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { tmpdir } from "node:os";

type AdapterPlugin = {
  register: (api: any) => void;
};

function writeFile(path: string, content: string): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content, "utf8");
}

function writeJson(path: string, value: unknown): void {
  writeFile(path, `${JSON.stringify(value, null, 2)}\n`);
}

function makeWorkspace(caseName: string, strictContracts: unknown): string {
  const workspace = join(tmpdir(), `quaid-contract-gate-${caseName}-${Date.now()}`);
  const memoryConfig = {
    models: {
      llmProvider: "openai",
      deepReasoningProvider: "openai",
      fastReasoningProvider: "openai",
      deepReasoning: "gpt-5.1-codex",
      fastReasoning: "gpt-5.1-codex",
    },
    retrieval: {
      failHard: false,
      maxLimit: 20,
    },
    plugins: {
      strict: strictContracts,
    },
  };
  const adapterManifest = {
    capabilities: {
      contract: {
        api: { exports: ["openclaw_adapter_entry", "/plugins/quaid/llm", "/memory/injected"] },
        // Deliberately incomplete so contract checks are exercised.
        events: { exports: ["agent_end"] },
        tools: { exports: ["memory_recall"] },
      },
    },
  };

  writeJson(join(workspace, "config", "memory.json"), memoryConfig);
  writeJson(join(workspace, "data", "memory.db"), {});
  writeJson(join(workspace, "modules", "quaid", "adaptors", "openclaw", "plugin.json"), adapterManifest);
  writeFile(
    join(workspace, "modules", "quaid", "datastore", "memorydb", "memory_graph.py"),
    [
      "#!/usr/bin/env python3",
      "import json, sys",
      "if len(sys.argv) > 1 and sys.argv[1] == 'stats':",
      "    print(json.dumps({'by_status': {'active': 1}, 'total_nodes': 1, 'edges': 0}))",
      "else:",
      "    print('{}')",
      "",
    ].join("\n"),
  );
  // Startup preflight checks for runtime plugin layout files.
  writeFile(join(workspace, "modules", "quaid", "core", "lifecycle", "janitor.py"), "print('ok')\n");
  return workspace;
}

function makeFakeApi() {
  return {
    on: vi.fn(() => {}),
    registerHook: vi.fn(() => {}),
    registerHttpRoute: vi.fn(() => {}),
    registerTool: vi.fn((factory: () => any) => factory()),
  };
}

async function loadAdapterWithWorkspace(workspace: string): Promise<AdapterPlugin> {
  vi.stubEnv("CLAWDBOT_WORKSPACE", workspace);
  vi.stubEnv("QUAID_HOME", workspace);
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return module.default as AdapterPlugin;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("adapter contract gate integration", () => {
  it("fails registration in strict mode when manifest misses exported events/tools", async () => {
    const workspace = makeWorkspace("strict", true);
    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).toThrow(/undeclared (events|tools) registration/);
  });

  it("warns and continues registration in non-strict mode", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const workspace = makeWorkspace("warn", false);
    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringMatching(/undeclared (events|tools) registration/));
    warn.mockRestore();
  });

  it("uses fallback ~/.quaid/memory-config.json for plugins.strict", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const workspace = makeWorkspace("fallback-strict", true);
    rmSync(join(workspace, "config", "memory.json"));

    const fakeHome = join(workspace, "fake-home");
    writeJson(join(fakeHome, ".quaid", "memory-config.json"), {
      retrieval: { failHard: false, maxLimit: 20 },
      plugins: { strict: false },
      models: {
        llmProvider: "openai",
        deepReasoningProvider: "openai",
        fastReasoningProvider: "openai",
        deepReasoning: "gpt-5.1-codex",
        fastReasoning: "gpt-5.1-codex",
      },
    });
    vi.stubEnv("HOME", fakeHome);

    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringMatching(/undeclared (events|tools) registration/));
    warn.mockRestore();
  });

  it("disables declaration checks when manifest is missing in non-strict mode", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const workspace = makeWorkspace("missing-manifest", false);
    rmSync(join(workspace, "modules", "quaid", "adaptors", "openclaw", "plugin.json"));

    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    const warnings = warn.mock.calls.map(([msg]) => String(msg));
    expect(warnings.some((msg) => msg.includes("failed reading adapter manifest"))).toBe(true);
    expect(warnings.some((msg) => msg.includes("undeclared tools registration"))).toBe(false);
    expect(warnings.some((msg) => msg.includes("undeclared events registration"))).toBe(false);
    warn.mockRestore();
  });

  it("treats numeric zero strict flag as non-strict", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const workspace = makeWorkspace("strict-zero", 0);
    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringMatching(/undeclared (events|tools) registration/));
    warn.mockRestore();
  });

  it("treats null strict flag as non-strict", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const workspace = makeWorkspace("strict-null", null);
    const plugin = await loadAdapterWithWorkspace(workspace);
    const api = makeFakeApi();
    expect(() => plugin.register(api as any)).not.toThrow();
    expect(warn).toHaveBeenCalledWith(expect.stringMatching(/undeclared (events|tools) registration/));
    warn.mockRestore();
  });
});
