import { spawn } from "child_process";
import * as fs from "node:fs";
import * as path from "node:path";

function _resolveTimeoutMs(name: string, fallbackMs: number): number {
  const raw = Number(process.env[name] || "");
  if (!Number.isFinite(raw) || raw <= 0) {
    return fallbackMs;
  }
  return Math.floor(raw);
}

export const PYTHON_BRIDGE_TIMEOUT_MS = _resolveTimeoutMs("QUAID_PYTHON_BRIDGE_TIMEOUT_MS", 120_000); // 2 minutes

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
      const proc = spawn("python3", [config.scriptPath, command, ...args], {
        cwd: config.workspace,
        env: {
          ...process.env,
          MEMORY_DB_PATH: config.dbPath,
          QUAID_HOME: config.workspace,
          CLAWDBOT_WORKSPACE: config.workspace,
          PYTHONPATH: pythonPath,
        },
      });

      let stdout = "";
      let stderr = "";
      let settled = false;

      const timer = setTimeout(() => {
        if (!settled) {
          settled = true;
          proc.kill("SIGTERM");
          reject(new Error(`Python bridge timeout after ${PYTHON_BRIDGE_TIMEOUT_MS}ms: ${command} ${args.join(" ")}`));
        }
      }, PYTHON_BRIDGE_TIMEOUT_MS);

      proc.stdout.on("data", (data) => {
        stdout += data;
      });
      proc.stderr.on("data", (data) => {
        stderr += data;
      });

      proc.on("close", (code) => {
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
          const detail = [stderrText ? `stderr: ${stderrText}` : "", stdoutText ? `stdout: ${stdoutText}` : ""]
            .filter(Boolean)
            .join(" | ")
            .slice(0, 1000);
          reject(new Error(`Python error (exit=${String(code)}): ${detail}`));
        }
      });

      proc.on("error", (err) => {
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
