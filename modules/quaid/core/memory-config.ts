import * as fs from "node:fs";
import * as path from "node:path";

type MemoryConfigResolverDeps = {
  workspace: string;
  isMissingFileError: (err: unknown) => boolean;
  isFailHardEnabled: () => boolean;
  getMemoryConfigCandidates?: () => string[];
  logger?: {
    warn: (message: string) => void;
    error: (message: string) => void;
  };
};

type MemoryConfigResolver = {
  getMemoryConfig: () => any;
  resolveMemoryConfigPath: () => string;
};

function buildFallbackMemoryConfig(): any {
  return {
    models: {
      llmProvider: "default",
      deepReasoning: "default",
      fastReasoning: "default",
      deepReasoningModelClasses: {
        anthropic: "claude-haiku-4-5",
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

export function createMemoryConfigResolver(deps: MemoryConfigResolverDeps): MemoryConfigResolver {
  let memoryConfigErrorLogged = false;
  let memoryConfigMtimeMs = -1;
  let memoryConfigPath = "";
  let memoryConfig: any = null;

  const warn = (message: string) => {
    if (deps.logger?.warn) {
      deps.logger.warn(message);
      return;
    }
    console.warn(message);
  };
  const error = (message: string) => {
    if (deps.logger?.error) {
      deps.logger.error(message);
      return;
    }
    console.error(message);
  };

  function memoryConfigCandidates(): string[] {
    const provided = deps.getMemoryConfigCandidates?.() || [];
    const normalized = provided.map((p) => String(p || "").trim()).filter(Boolean);
    if (normalized.length > 0) {
      return normalized;
    }
    return [
      path.join(deps.workspace, "memory-config.json"),
      path.join(process.cwd(), "memory-config.json"),
    ];
  }

  function resolveMemoryConfigPath(): string {
    for (const candidate of memoryConfigCandidates()) {
      try {
        if (fs.existsSync(candidate)) {
          return candidate;
        }
      } catch {
        // Ignore probe errors and continue.
      }
    }
    return memoryConfigCandidates()[0];
  }

  function getMemoryConfig(): any {
    const configPath = resolveMemoryConfigPath();
    if (configPath !== memoryConfigPath) {
      memoryConfigMtimeMs = -1;
      memoryConfigPath = configPath;
    }
    let mtimeMs = -1;
    try {
      mtimeMs = fs.statSync(configPath).mtimeMs;
    } catch (err: unknown) {
      const msg = String((err as Error)?.message || err || "");
      if (!msg.includes("ENOENT")) {
        warn(`[memory] memory config stat failed: ${msg}`);
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
    } catch (err: unknown) {
      if (!memoryConfigErrorLogged) {
        memoryConfigErrorLogged = true;
        error(`[memory] failed to load memory config (${configPath}): ${(err as Error)?.message || String(err)}`);
      }
      if (deps.isMissingFileError(err)) {
        memoryConfig = buildFallbackMemoryConfig();
        memoryConfigMtimeMs = -1;
        return memoryConfig;
      }
      // Prevent mutual recursion with isFailHardEnabled() while preserving old behavior.
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
