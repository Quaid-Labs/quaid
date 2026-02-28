import { afterEach, describe, expect, it, vi } from "vitest";
import { mkdirSync, writeFileSync } from "node:fs";
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

function makeWorkspace(caseName: string, strictContracts: boolean): string {
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
        api: { exports: ["openclaw_adapter_entry"] },
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
  // Startup preflight checks for these hard-coded plugin layout files.
  writeFile(join(workspace, "plugins", "quaid", "core", "lifecycle", "janitor.py"), "print('ok')\n");
  writeFile(join(workspace, "plugins", "quaid", "datastore", "memorydb", "memory_graph.py"), "print('ok')\n");
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
  process.env.CLAWDBOT_WORKSPACE = workspace;
  process.env.QUAID_HOME = workspace;
  vi.resetModules();
  const module = await import("../adaptors/openclaw/adapter.js");
  return module.default as AdapterPlugin;
}

afterEach(() => {
  delete process.env.CLAWDBOT_WORKSPACE;
  delete process.env.QUAID_HOME;
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
});
