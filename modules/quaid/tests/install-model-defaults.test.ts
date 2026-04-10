import { describe, expect, it } from "vitest";

import {
  deriveInstallerLlmProviderSetting,
  installerDefaultProvider,
  installerFallbackModelDefaults,
  installerFallbackProviders,
} from "../../../lib/install-model-defaults.mjs";

describe("installer model defaults", () => {
  it("uses openai-codex as OpenClaw host-managed fallback", () => {
    expect(installerDefaultProvider("openclaw")).toBe("openai-codex");
    expect(installerFallbackProviders("openclaw")[0]).toBe("openai-codex");
    expect(installerFallbackModelDefaults("openclaw", "openai-codex")).toEqual({
      deep: "gpt-5.4",
      fast: "gpt-5.4-mini",
      deepEffort: "medium",
      fastEffort: "medium",
    });
  });

  it("derives openai-codex for unknown OpenClaw host-managed model hints", () => {
    expect(deriveInstallerLlmProviderSetting("openclaw", "", "", "", true)).toBe("openai-codex");
    expect(deriveInstallerLlmProviderSetting("openclaw", "default", "gpt-5.4", "gpt-5.4-mini", true)).toBe("openai-codex");
    expect(deriveInstallerLlmProviderSetting("openclaw", "default", "claude-sonnet-4-5", "claude-haiku-4-5", true)).toBe("anthropic");
  });
});
