import { spawn } from "node:child_process";

export type SpawnWithTimeoutOptions = {
  cwd: string;
  env: NodeJS.ProcessEnv;
  timeoutMs: number;
  label: string;
  argv: string[];
  killAfterMs?: number;
};

export function spawnWithTimeout(opts: SpawnWithTimeoutOptions): Promise<string> {
  return new Promise((resolve, reject) => {
    const proc = spawn(String(opts.argv[0] || ""), opts.argv.slice(1), {
      cwd: opts.cwd,
      env: opts.env,
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    let killTimer: ReturnType<typeof setTimeout> | null = null;
    const killAfterMs = Math.max(1, Number(opts.killAfterMs || 5_000));
    const timeoutMs = Math.max(1, Number(opts.timeoutMs || 1));
    const argsText = opts.argv.slice(1).join(" ");

    const timer = setTimeout(() => {
      if (settled) return;
      try {
        proc.kill("SIGTERM");
      } catch {
        // best-effort only
      }
      killTimer = setTimeout(() => {
        if (settled) return;
        try {
          proc.kill("SIGKILL");
        } catch {
          // best-effort only
        }
      }, killAfterMs);
      settled = true;
      reject(new Error(`${opts.label} timeout after ${timeoutMs}ms: ${argsText}`));
    }, timeoutMs);

    proc.stdout.on("data", (data: Buffer) => { stdout += data; });
    proc.stderr.on("data", (data: Buffer) => { stderr += data; });
    proc.on("close", (code: number | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (killTimer) {
        clearTimeout(killTimer);
        killTimer = null;
      }
      if (code === 0) {
        resolve(stdout.trim());
        return;
      }
      const stderrText = stderr.trim();
      const stdoutText = stdout.trim();
      const detail = [stderrText ? `stderr: ${stderrText}` : "", stdoutText ? `stdout: ${stdoutText}` : ""]
        .filter(Boolean)
        .join(" | ")
        .slice(0, 1000);
      reject(new Error(`${opts.label} error (exit=${String(code)}): ${detail}`));
    });
    proc.on("error", (err: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (killTimer) {
        clearTimeout(killTimer);
        killTimer = null;
      }
      reject(err);
    });
  });
}
