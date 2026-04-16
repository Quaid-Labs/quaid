import { describe, expect, it } from "vitest";

import {
  deriveInstallerLlmProviderSetting,
  installerDefaultProvider,
  installerFallbackModelDefaults,
  installerFallbackProviders,
  resolveInstallerProvider,
} from "../../../lib/install-model-defaults.mjs";

describe("installer model defaults", () => {
  it("uses explicit-token providers for OpenClaw fallback", () => {
    expect(installerDefaultProvider("openclaw")).toBe("anthropic");
    expect(installerFallbackProviders("openclaw")).toEqual(["anthropic", "openai"]);
    expect(installerFallbackModelDefaults("openclaw", "openai-codex")).toEqual({
      deep: "gpt-5.4",
      fast: "gpt-5.4-mini",
      deepEffort: "medium",
      fastEffort: "medium",
    });
  });

  it("defaults Codex installs to Anthropic lanes", () => {
    expect(installerDefaultProvider("codex")).toBe("anthropic");
  });

  it("keeps installer provider on Anthropic unless explicitly overridden", () => {
    expect(resolveInstallerProvider("openclaw", ["anthropic", "openai"])).toBe("anthropic");
    expect(
      resolveInstallerProvider("openclaw", ["anthropic", "openai"], {
        sharedOverrideProvider: "openai",
      }),
    ).toBe("openai");
    expect(
      resolveInstallerProvider("openclaw", ["anthropic", "openai"], {
        forcedProvider: "openai",
      }),
    ).toBe("openai");
  });

  it("derives direct-provider settings for OpenClaw model hints", () => {
    expect(deriveInstallerLlmProviderSetting("openclaw", "", "", "", true)).toBe("openai-compatible");
    expect(deriveInstallerLlmProviderSetting("openclaw", "default", "gpt-5.4", "gpt-5.4-mini", true)).toBe("openai-compatible");
    expect(deriveInstallerLlmProviderSetting("openclaw", "default", "claude-sonnet-4-5", "claude-haiku-4-5", true)).toBe("anthropic");
  });
});
