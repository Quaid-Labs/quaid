import { spawn } from "node:child_process";
const PYTHON_BRIDGE_TIMEOUT_MS = 120000;
export function createPythonBridgeExecutor(config) {
  return async function execPython(command, args = []) {
    return new Promise((resolve, reject) => {
      const proc = spawn("python3", [config.scriptPath, command, ...args], {
        env: {
          ...process.env,
          MEMORY_DB_PATH: config.dbPath,
          QUAID_HOME: config.workspace,
          CLAWDBOT_WORKSPACE: config.workspace
        }
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
          reject(new Error(`Python error: ${stderr || stdout}`));
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
