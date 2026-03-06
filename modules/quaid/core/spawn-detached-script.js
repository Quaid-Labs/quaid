import { spawn } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
export function spawnDetachedScript(opts) {
    const interpreter = String(opts.interpreter || process.execPath);
    const filePrefix = String(opts.filePrefix || "worker");
    const fileExtension = String(opts.fileExtension || ".tmp");
    const tmpFile = path.join(opts.scriptDir, `${filePrefix}-${Date.now()}-${Math.random().toString(36).slice(2)}${fileExtension.startsWith(".") ? fileExtension : `.${fileExtension}`}`);
    const appendLog = (msg) => {
        try {
            fs.appendFileSync(opts.logFile, `${new Date().toISOString()} ${msg}\n`);
        }
        catch {
        }
    };
    let launched = false;
    let logFd = null;
    const suffix = typeof opts.scriptSuffix === "string" ? opts.scriptSuffix : `\nos.unlink(${JSON.stringify(tmpFile)})\n`;
    fs.writeFileSync(tmpFile, opts.scriptPrefix + opts.scriptBody + suffix, { mode: 384 });
    try {
        logFd = fs.openSync(opts.logFile, "a");
        const proc = spawn(interpreter, [tmpFile], {
            detached: true,
            stdio: ["ignore", logFd, logFd],
            env: opts.env,
        });
        launched = true;
        proc.on("error", (err) => {
            appendLog(`[detached-worker-error] spawn failed: ${err.message}`);
            try {
                fs.unlinkSync(tmpFile);
            }
            catch {
            }
        });
        proc.unref();
    }
    catch (err) {
        appendLog(`[detached-worker-error] launch failed: ${String(err?.message || err)}`);
        if (!launched) {
            try {
                fs.unlinkSync(tmpFile);
            }
            catch {
            }
        }
    }
    finally {
        if (logFd !== null) {
            try {
                fs.closeSync(logFd);
            }
            catch {
            }
        }
    }
    return launched;
}
