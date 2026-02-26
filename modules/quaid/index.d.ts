/**
 * quaid - Local graph-based memory plugin for Clawdbot
 *
 * Uses SQLite + Ollama embeddings for fully local memory storage.
 * Replaces memory-lancedb with no external API dependencies.
 */
import type { ClawdbotPluginApi } from "openclaw/plugin-sdk";
type PluginConfig = {
    autoCapture?: boolean;
    autoRecall?: boolean;
};
declare const quaidPlugin: {
    id: string;
    name: string;
    description: string;
    kind: "memory";
    configSchema: import("@sinclair/typebox").TObject<{
        autoCapture: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
        autoRecall: import("@sinclair/typebox").TOptional<import("@sinclair/typebox").TBoolean>;
    }>;
    register(api: ClawdbotPluginApi<PluginConfig>): void;
};
export default quaidPlugin;
