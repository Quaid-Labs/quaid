import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
function buildFallbackMemoryConfig() {
    return {
        models: {
            llmProvider: "default",
            deepReasoning: "default",
            fastReasoning: "default",
            deepReasoningModelClasses: {
                anthropic: "claude-opus-4-6",
                openai: "gpt-5",
                "openai-compatible": "gpt-4.1",
            },
            fastReasoningModelClasses: {
                anthropic: "claude-haiku-4-5",
                openai: "gpt-5-mini",
                "openai-compatible": "gpt-4.1-mini",
            },
        },
        retrieval: {
            maxLimit: 8,
        },
    };
}
export function createMemoryConfigResolver(deps) {
    let memoryConfigErrorLogged = false;
    let memoryConfigMtimeMs = -1;
    let memoryConfigPath = "";
    let memoryConfig = null;
    const warn = (message) => {
        if (deps.logger?.warn) {
            deps.logger.warn(message);
            return;
        }
        console.warn(message);
    };
    const error = (message) => {
        if (deps.logger?.error) {
            deps.logger.error(message);
            return;
        }
        console.error(message);
    };
    function memoryConfigCandidates() {
        const provided = deps.getMemoryConfigCandidates?.() || [];
        const normalized = provided.map((p) => String(p || "").trim()).filter(Boolean);
        if (normalized.length > 0) {
            return normalized;
        }
        return [
            path.join(deps.workspace, "memory-config.json"),
            path.join(os.homedir(), ".quaid", "memory-config.json"),
            path.join(process.cwd(), "memory-config.json"),
        ];
    }
    function resolveMemoryConfigPath() {
        for (const candidate of memoryConfigCandidates()) {
            try {
                if (fs.existsSync(candidate)) {
                    return candidate;
                }
            }
            catch {
                // Ignore probe errors and continue.
            }
        }
        return memoryConfigCandidates()[0];
    }
    function getMemoryConfig() {
        const configPath = resolveMemoryConfigPath();
        if (configPath !== memoryConfigPath) {
            memoryConfigMtimeMs = -1;
            memoryConfigPath = configPath;
        }
        let mtimeMs = -1;
        try {
            mtimeMs = fs.statSync(configPath).mtimeMs;
        }
        catch (err) {
            const msg = String(err?.message || err || "");
            if (!msg.includes("ENOENT")) {
                warn(`[quaid] memory config stat failed: ${msg}`);
            }
        }
        if (memoryConfig && mtimeMs >= 0 && memoryConfigMtimeMs === mtimeMs) {
            return memoryConfig;
        }
        if (memoryConfig && mtimeMs < 0) {
            return memoryConfig;
        }
        try {
            memoryConfig = JSON.parse(fs.readFileSync(configPath, "utf8"));
            memoryConfigMtimeMs = mtimeMs;
        }
        catch (err) {
      if (!memoryConfigErrorLogged) {
        memoryConfigErrorLogged = true;
        error(`[quaid] failed to load memory config (${configPath}): ${err?.message || String(err)}`);
      }
            if (deps.isMissingFileError(err)) {
                memoryConfig = buildFallbackMemoryConfig();
                memoryConfigMtimeMs = -1;
                return memoryConfig;
            }
            memoryConfig = buildFallbackMemoryConfig();
            memoryConfigMtimeMs = mtimeMs;
            if (deps.isFailHardEnabled()) {
                throw err;
            }
        }
        return memoryConfig;
    }
    return {
        getMemoryConfig,
        resolveMemoryConfigPath,
    };
}
