import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";

export type SpawnDetachedScriptOptions = {
  scriptDir: string;
  logFile: string;
  scriptPrefix: string;
  scriptBody: string;
  scriptSuffix?: string;
  env: NodeJS.ProcessEnv;
  interpreter?: string;
  filePrefix?: string;
};

export function spawnDetachedScript(opts: SpawnDetachedScriptOptions): boolean {
  const interpreter = String(opts.interpreter || "python3");
  const filePrefix = String(opts.filePrefix || "worker");
  const tmpFile = path.join(
    opts.scriptDir,
    `${filePrefix}-${Date.now()}-${Math.random().toString(36).slice(2)}.py`,
  );
  const appendLog = (msg: string) => {
    try {
      fs.appendFileSync(opts.logFile, `${new Date().toISOString()} ${msg}\n`);
    } catch {
      // best-effort only
    }
  };
  let launched = false;
  let logFd: number | null = null;
  const suffix = typeof opts.scriptSuffix === "string" ? opts.scriptSuffix : `\nos.unlink(${JSON.stringify(tmpFile)})\n`;
  fs.writeFileSync(tmpFile, opts.scriptPrefix + opts.scriptBody + suffix, { mode: 0o600 });
  try {
    logFd = fs.openSync(opts.logFile, "a");
    const proc = spawn(interpreter, [tmpFile], {
      detached: true,
      stdio: ["ignore", logFd, logFd],
      env: opts.env,
    });
    launched = true;
    proc.on("error", (err: Error) => {
      appendLog(`[detached-worker-error] spawn failed: ${err.message}`);
      try {
        fs.unlinkSync(tmpFile);
      } catch {
        // best-effort only
      }
    });
    proc.unref();
  } catch (err: unknown) {
    appendLog(`[detached-worker-error] launch failed: ${String((err as Error)?.message || err)}`);
    if (!launched) {
      try {
        fs.unlinkSync(tmpFile);
      } catch {
        // best-effort only
      }
    }
  } finally {
    if (logFd !== null) {
      try {
        fs.closeSync(logFd);
      } catch {
        // best-effort only
      }
    }
  }
  return launched;
}
