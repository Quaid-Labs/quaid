import { afterEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import * as os from "node:os";
import * as path from "node:path";

const spawnMock = vi.fn();
const spawnSyncMock = vi.fn(() => ({ status: 0, error: undefined }));

vi.mock("child_process", () => ({
  spawn: spawnMock,
  spawnSync: spawnSyncMock,
}));

function makeProc() {
  const proc = new EventEmitter() as EventEmitter & {
    stdout: EventEmitter;
    stderr: EventEmitter;
    kill: ReturnType<typeof vi.fn>;
  };
  proc.stdout = new EventEmitter();
  proc.stderr = new EventEmitter();
  proc.kill = vi.fn();
  queueMicrotask(() => proc.emit("close", 0));
  return proc;
}

afterEach(() => {
  vi.resetModules();
  vi.clearAllMocks();
  delete process.env.QUAID_VISIBLE_HOME;
});

describe("python-bridge visible home resolution", () => {
  it("uses recall timeout_ms plus grace as subprocess timeout", async () => {
    const { resolvePythonBridgeCommandTimeoutMs } = await import("../adaptors/openclaw/python-bridge.js");

    expect(resolvePythonBridgeCommandTimeoutMs("recall", [
      "What do you know about Baxter?",
      JSON.stringify({ stores: ["vector"], timeout_ms: 32000 }),
      "--json",
    ], 120_000)).toBe(33_500);
    expect(resolvePythonBridgeCommandTimeoutMs("stats", [], 120_000)).toBe(120_000);
  });

  it("normalizes an explicit QUAID_VISIBLE_HOME with tilde expansion", async () => {
    process.env.QUAID_VISIBLE_HOME = "~/quaid-visible";
    spawnMock.mockImplementation(() => makeProc());

    const { createPythonBridgeExecutor } = await import("../adaptors/openclaw/python-bridge.js");
    const execPython = createPythonBridgeExecutor({
      scriptPath: "/tmp/test-script.py",
      dbPath: "/tmp/test.db",
      workspace: "/tmp/.quaid",
      pluginRoot: "/tmp/plugin-root",
    });

    await execPython("stats", []);

    const env = spawnMock.mock.calls[0]?.[2]?.env;
    expect(env?.QUAID_VISIBLE_HOME).toBe(path.join(os.homedir(), "quaid-visible"));
  });
});
