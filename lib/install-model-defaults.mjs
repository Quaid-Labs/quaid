export function installerDefaultProvider(adapterType = "") {
  const platform = String(adapterType || "").trim().toLowerCase();
  if (platform === "openclaw") return "openai";
  if (platform === "codex") return "openai";
  return "anthropic";
}

export function installerFallbackProviders(adapterType = "") {
  const platform = String(adapterType || "").trim().toLowerCase();
  if (platform === "openclaw") {
    return ["anthropic", "openai"];
  }
  return ["anthropic", "openai", "openrouter", "together", "ollama"];
}

export function installerFallbackModelDefaults(adapterType = "", provider = "") {
  const platform = String(adapterType || "").trim().toLowerCase();
  const normalizedProvider = String(provider || "").trim().toLowerCase();
  if (platform === "openclaw" && normalizedProvider === "openai-codex") {
    return {
      deep: "gpt-5.4",
      fast: "gpt-5.4-mini",
      deepEffort: "medium",
      fastEffort: "medium",
    };
  }
  return null;
}

export function resolveInstallerProvider(
  adapterType = "",
  supportedProviders = [],
  {
    sharedOverrideProvider = "",
    forcedProvider = "",
  } = {},
) {
  const normalizedSupported = Array.isArray(supportedProviders)
    ? supportedProviders.map((value) => String(value || "").trim().toLowerCase()).filter(Boolean)
    : [];
  let provider = installerDefaultProvider(adapterType);
  const normalizedSharedOverride = String(sharedOverrideProvider || "").trim().toLowerCase();
  const normalizedForced = String(forcedProvider || "").trim().toLowerCase();

  if (normalizedSharedOverride && normalizedSupported.includes(normalizedSharedOverride)) {
    provider = normalizedSharedOverride;
  }
  if (normalizedForced && normalizedSupported.includes(normalizedForced)) {
    provider = normalizedForced;
  }
  if (!normalizedSupported.includes(provider)) {
    provider = normalizedSupported[0] || "anthropic";
  }
  return provider;
}

export function deriveInstallerLlmProviderSetting(
  adapterType,
  provider,
  deepModel,
  fastModel,
  hostManagedLlmDefault,
) {
  const normalizedAdapter = String(adapterType || "").trim().toLowerCase();
  const normalizedProvider = String(provider || "").trim().toLowerCase();
  if (!hostManagedLlmDefault) {
    if (normalizedProvider === "anthropic") return "anthropic";
    if (normalizedProvider === "openai") return "openai";
    return "openai-compatible";
  }
  if (normalizedProvider && normalizedProvider !== "default") {
    return normalizedProvider;
  }
  const modelHints = `${String(deepModel || "")} ${String(fastModel || "")}`.trim().toLowerCase();
  if (modelHints.includes("claude")) return "anthropic";
  if (modelHints.includes("gpt-")) return "openai-compatible";
  return "openai-compatible";
}
