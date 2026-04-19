import { spawn, spawnSync } from "child_process";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

function _normalizeWorkspacePath(rawPath: string): string {
  const trimmed = String(rawPath || "").trim();
  if (!trimmed) {
    return path.resolve(process.cwd());
  }
  const expanded = trimmed.startsWith("~")
    ? path.join(os.homedir(), trimmed.slice(1))
    : trimmed;
  return path.resolve(expanded);
}

function _resolveTimeoutMs(name: string, fallbackMs: number): number {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}

function _resolveVisibleHome(root: string): string {
  const explicit = String(process.env.QUAID_VISIBLE_HOME || "").trim();
  if (explicit) {
    return _normalizeWorkspacePath(explicit);
  }
  const resolved = _normalizeWorkspacePath(root);
  const base = path.basename(resolved);
  if (base.startsWith(".") && base.length > 1) {
    return path.join(path.dirname(resolved), base.slice(1));
  }
  return resolved;
}

export const PYTHON_BRIDGE_TIMEOUT_MS = _resolveTimeoutMs("QUAID_PYTHON_BRIDGE_TIMEOUT_MS", 120_000); // 2 minutes
const RECALL_TIMEOUT_GRACE_MS = _resolveTimeoutMs("QUAID_PYTHON_BRIDGE_RECALL_TIMEOUT_GRACE_MS", 1_500);
const PYTHON_BRIDGE_KILL_GRACE_MS = _resolveTimeoutMs("QUAID_PYTHON_BRIDGE_KILL_GRACE_MS", 2_000);

function _extractPositiveInt(value: unknown): number | null {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return Math.floor(parsed);
}

function _extractRecallTimeoutMs(command: string, args: string[]): number | null {
  if (command === "recall") {
    for (const arg of args) {
      const text = String(arg || "").trim();
      if (!text.startsWith("{")) continue;
      try {
        const parsed = JSON.parse(text);
        const timeout = _extractPositiveInt(parsed?.timeout_ms ?? parsed?.timeoutMs);
        if (timeout !== null) return timeout;
      } catch {
        // Non-config JSON is not expected here; ignore and keep the default bridge timeout.
      }
    }
  }
  if (command === "recall-fast") {
    const idx = args.findIndex((arg) => String(arg || "") === "--timeout-ms");
    if (idx >= 0) {
      return _extractPositiveInt(args[idx + 1]);
    }
  }
  return null;
}

export function resolvePythonBridgeCommandTimeoutMs(
  command: string,
  args: string[] = [],
  defaultTimeoutMs: number = PYTHON_BRIDGE_TIMEOUT_MS,
): number {
  const defaultTimeout = Math.max(1, Math.floor(Number(defaultTimeoutMs) || PYTHON_BRIDGE_TIMEOUT_MS));
  const recallTimeout = _extractRecallTimeoutMs(command, args);
  if (recallTimeout === null) return defaultTimeout;
  return Math.max(1, Math.min(defaultTimeout, recallTimeout + RECALL_TIMEOUT_GRACE_MS));
}

function _pythonVersionOk(bin: string): boolean {
  const candidate = String(bin || "").trim();
  if (!candidate) {
    return false;
  }
  try {
    const result = spawnSync(
      candidate,
      ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"],
      { stdio: "ignore" },
    );
    return !result.error && result.status === 0;
  } catch {
    return false;
  }
}

function _resolvePythonBin(): string {
  const explicit = String(process.env.QUAID_PYTHON_BIN || "").trim();
  const candidates = [
    explicit,
    "/opt/homebrew/bin/python3",
    "/opt/homebrew/bin/python3.13",
    "/opt/homebrew/bin/python3.12",
    "/opt/homebrew/bin/python3.11",
    "/opt/homebrew/bin/python3.10",
    "/usr/local/bin/python3",
    "/usr/local/bin/python3.13",
    "/usr/local/bin/python3.12",
    "/usr/local/bin/python3.11",
    "/usr/local/bin/python3.10",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3.10",
    "python3",
  ];
  for (const candidate of candidates) {
    if (_pythonVersionOk(candidate)) {
      process.env.QUAID_PYTHON_BIN = candidate;
      return candidate;
    }
  }
  return explicit || "python3";
}

const PYTHON_BIN = _resolvePythonBin();

function _formatBridgeErrorDetail(stderrText: string, stdoutText: string): string {
  const timeoutHint =
    /timed out|timeout|Gateway LLM proxy transient error|URLError|TimeoutError/i.test(stderrText)
      ? "[hint] upstream llm timeout/connection issue detected"
      : "";
  return [
    timeoutHint,
    stderrText ? `stderr: ${stderrText}` : "",
    stdoutText ? `stdout: ${stdoutText}` : "",
  ]
    .filter(Boolean)
    .join(" | ");
}

type PythonBridgeConfig = {
  scriptPath: string;
  dbPath: string;
  workspace: string;
  pluginRoot?: string;
};

export function createPythonBridgeExecutor(config: PythonBridgeConfig) {
  const explicitRoot = String(config.pluginRoot || "").trim();
  const modernRoot = path.join(config.workspace, "modules", "quaid");
  const legacyRoot = path.join(config.workspace, "plugins", "quaid");
  const pluginRoot = explicitRoot || (fs.existsSync(modernRoot) ? modernRoot : legacyRoot);
  const sep = process.platform === "win32" ? ";" : ":";
  const existingPyPath = String(process.env.PYTHONPATH || "").trim();
  const pythonPath = existingPyPath ? `${pluginRoot}${sep}${existingPyPath}` : pluginRoot;

  return async function execPython(command: string, args: string[] = []): Promise<string> {
    return new Promise((resolve, reject) => {
      const commandTimeoutMs = resolvePythonBridgeCommandTimeoutMs(command, args);
      const proc = spawn(PYTHON_BIN, [config.scriptPath, command, ...args], {
        cwd: config.workspace,
        env: {
          ...process.env,
          MEMORY_DB_PATH: config.dbPath,
          QUAID_HOME: config.workspace,
          QUAID_VISIBLE_HOME: _resolveVisibleHome(config.workspace),
          QUAID_WORKSPACE: config.workspace,
          OPENCLAW_WORKSPACE: config.workspace,
          PYTHONPATH: pythonPath,
        },
      });

      let stdout = "";
      let stderr = "";
      let settled = false;
      let killTimer: ReturnType<typeof setTimeout> | undefined;

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          proc.kill("SIGTERM");
          killTimer = setTimeout(() => {
            proc.kill("SIGKILL");
          }, PYTHON_BRIDGE_KILL_GRACE_MS);
          reject(new Error(`Python bridge timeout after ${commandTimeoutMs}ms: ${command} ${args.join(" ")}`));
        }
      }, commandTimeoutMs);

      proc.stdout.on("data", (data) => {
        stdout += data;
      });
      proc.stderr.on("data", (data) => {
        stderr += data;
      });

      proc.on("close", (code) => {
        if (killTimer !== undefined) {
          clearTimeout(killTimer);
        }
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timer);
        if (code === 0) {
          resolve(stdout.trim());
        } else {
          const stderrText = stderr.trim();
          const stdoutText = stdout.trim();
          const detail = _formatBridgeErrorDetail(stderrText, stdoutText);
          reject(new Error(`Python error (exit=${String(code)}): ${detail}`));
        }
      });

      proc.on("error", (err) => {
        if (killTimer !== undefined) {
          clearTimeout(killTimer);
        }
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timer);
        reject(err);
      });
    });
  };
}
