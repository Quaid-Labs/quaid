import { describe, expect, it } from "vitest";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { shouldStartExtractionDaemonAfterInstall } from "../../../lib/install-daemon-policy.mjs";

describe("install daemon policy", () => {
  it("starts the daemon after install for hook-driven hosts", () => {
    expect(shouldStartExtractionDaemonAfterInstall("claude-code")).toBe(true);
    expect(shouldStartExtractionDaemonAfterInstall("codex")).toBe(true);
    expect(shouldStartExtractionDaemonAfterInstall("openclaw")).toBe(true);
  });

  it("does not force daemon start for standalone installs", () => {
    expect(shouldStartExtractionDaemonAfterInstall("standalone")).toBe(false);
    expect(shouldStartExtractionDaemonAfterInstall("")).toBe(false);
  });

  it("step8 validation resolves the installed adapter before daemon policy", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).not.toContain("shouldStartExtractionDaemonAfterInstall(adapterType)");
    expect(setupText).toContain("const validationAdapterType = models?.adapterType || resolvedInstallerPlatform();");
    expect(setupText).toContain("shouldStartExtractionDaemonAfterInstall(validationAdapterType)");
  });

  it("installer writes shared platform config without inventing a default instance silo", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).not.toContain("return `${platform}-main`;");
    expect(setupText).toContain("Write runtime platform overrides to shared platform config only.");
    expect(setupText).not.toContain("Wrote instance config:");
    expect(setupText).not.toContain("hydratePlatformInstanceConfigs(");
  });

  it("installer keeps shared platform config thin while global config is exhaustive", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("const baseGlobalConfig = JSON.parse(JSON.stringify(config));");
    expect(setupText).toContain("delete baseGlobalConfig.adapter;");
    expect(setupText).toContain("delete baseGlobalConfig.plugins.slots.adapter;");
    expect(setupText).toContain("const mergedGlobalConfig = deepMergeMissing(baseGlobalConfig, existingGlobalConfig);");
    expect(setupText).toContain("const platformConfig = _deepDiffOverrides(sharedGlobalCfg, config) || {};");
    expect(setupText).toContain("platformConfig.adapter = adapterConfig;");
    expect(setupText).toContain("platformPlugins.slots.adapter = adapterPluginId;");
    expect(setupText).toContain("Wrote shared platform override config:");
    expect(setupText).not.toContain("fs.writeFileSync(sharedPlatformConfigPath, configJson);");
  });

  it("installer seeds janitor checkpoint time for fresh instance installs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _seedJanitorInstallCheckpoint(instanceId = \"\")");
    expect(setupText).toContain("_seedJanitorInstallCheckpoint(resolvedInstanceId);");
    expect(setupText).toContain("const installSeeded = Boolean(existing?.install_seeded);");
    expect(setupText).toContain("if (status && status !== \"completed\") return;");
    expect(setupText).toContain("last_completed_at: nowIso");
    expect(setupText).toContain("Seeded janitor health checkpoint:");
  });

  it("shared-only installs defer docs registry writes until a real instance exists", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('const resolvedInstanceId = String(process.env.QUAID_INSTANCE || "").trim();');
    expect(setupText).toContain("Skipping bundled project docs registration until the first real instance is created.");
    expect(setupText).toContain("Skipping existing project docs registration until the first real instance is created.");
  });

  it("installer keeps fail_hard enabled by default", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("fail_hard: true");
    expect(setupText).not.toContain("fail_hard: false");
  });

  it("interactive installer can chain into another installable platform", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("async function promptNextPlatformInstall(installedPlatform)");
    expect(setupText).toContain("let _chainedPlatformQueue = [];");
    expect(setupText).toContain("function _beginChainedPlatformInstall(adapterId, queuedAdapters = [])");
    expect(setupText).toContain('value: "__install_all__"');
    expect(setupText).toContain('label: "Install All Available"');
    expect(setupText).toContain("_remainingInstallableAdapterOptions(installed)");
    expect(setupText).toContain("if (_chainedPlatformQueue.length > 0)");
    expect(setupText).toContain("Other supported platforms were detected. Install another?");
    expect(setupText).toContain("!ALLOW_EXISTING_INSTALL && !_chainedPlatformInstall");
  });

  it("supports a CLI bulk-install flag that reuses the chained platform flow", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('if (arg === "--all-platforms")');
    expect(setupText).toContain('const INSTALL_ALL_PLATFORMS = !!INSTALL_ARGS.allPlatforms;');
    expect(setupText).toContain('--all-platforms     Install every currently installable platform by reusing');
    expect(setupText).toContain('--all-platforms cannot be combined with --adapter or --claude-code.');
    expect(setupText).toContain('if (INSTALL_ALL_PLATFORMS && !_platformOverride && _chainedPlatformQueue.length === 0) {');
    expect(setupText).toContain('const [firstAdapter, ...queuedAdapters] = installableAdapterOptions.map((opt) => opt.value);');
    expect(setupText).toContain('if (INSTALL_ALL_PLATFORMS) {');
    expect(setupText).toContain('platform = resolvedInstallerPlatform() ? "" : "__install_all__";');
    expect(setupText).toContain('_beginChainedPlatformInstall(firstAdapter, queuedAdapters);');
    expect(setupText).toContain('if (_chainedPlatformQueue.length > 0) {');
    const queueIdx = setupText.indexOf('if (_chainedPlatformQueue.length > 0) {');
    const gateIdx = setupText.indexOf('if (AGENT_MODE || DRY_RUN || SURVEY_ONLY || _testAnswers || FORCED_ADAPTER_TYPE) {');
    expect(queueIdx).toBeGreaterThanOrEqual(0);
    expect(gateIdx).toBeGreaterThanOrEqual(0);
    expect(queueIdx).toBeLessThan(gateIdx);
  });


  it("platform installs no longer prompt for platform-specific credentials", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).not.toContain('message: `OpenClaw ${providerLabel} token:`');
    expect(setupText).not.toContain('OpenClaw Auth Token Required — Action Needed');
    expect(setupText).not.toContain('Install incomplete: OpenClaw ${providerLabel} credential not found. Register it and re-run.');
    expect(setupText).not.toContain('message: `Codex ${providerLabel} credential:`');
    expect(setupText).not.toContain('Codex Auth Token Required — Action Needed');
    expect(setupText).not.toContain('Install incomplete: Codex credential not found. Register it and re-run.');
    expect(setupText).not.toContain('message: "Quaid OAuth token:"');
    expect(setupText).not.toContain('Auth Token Required — Action Needed');
    expect(setupText).not.toContain('Install incomplete: auth credential not found. Register it and re-run.');
  });

  it("first install owns a single shared auth gate", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('const sharedAuthTokenPath = sharedAuthRegistryPath(WORKSPACE);');
    expect(setupText).toContain('const sharedCredentialKinds = allSharedAuthKinds();');
    expect(setupText).toContain('if (!_existingInstallDetected) {');
    expect(setupText).toContain('message: "Quaid shared provider credential:"');
    expect(setupText).toContain('Shared Auth Credential Required — Action Needed');
    expect(setupText).toContain('Install incomplete: shared auth credential not found. Register it and re-run.');
    expect(setupText).toContain("async function _ensureCompatibleSharedCredentialForInstall(adapterType, provider, sharedAuthTokenPath)");
  });

  it("OpenClaw install hard-stops when the gateway is unreachable", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("OpenClaw gateway must be running before installing Quaid.");
    expect(setupText).toContain('bail("OpenClaw gateway must be running before installing Quaid.");');
    expect(setupText).not.toContain("OpenClaw status/probe unavailable in agent mode; continuing with install.");
  });

  it("OpenClaw hook symbol grep is diagnostic after the version gate", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("Gateway lifecycle support (version-gated)");
    expect(setupText).toContain("function gatewayHasHookSymbols(gwDir)");
    expect(setupText).not.toContain('bail("Gateway hooks required. Update OpenClaw and re-run.");');
    expect(setupText).not.toContain("Your gateway is missing the memory hooks Quaid needs.");
  });

  it("OpenClaw add-instance still writes gateway runtime env", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("const runtimeEnvChanged = _ocRuntimeInstance");
    expect(setupText).toContain("? _ensureOpenClawRuntimeInstanceEnv(_ocRuntimeInstance)");
    expect(setupText).toContain("responsesEndpointChanged || agentModelChanged || runtimeEnvChanged");
    expect(setupText).toContain("parsed.env.OPENCLAW_WORKSPACE = WORKSPACE;");
    expect(setupText).not.toContain("leaving fallback QUAID_INSTANCE unchanged in add-instance mode");
  });

  it("OpenClaw install reconciles runtime env to the created instance before finishing", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("const runtimeEnvReconciled = _ensureOpenClawRuntimeInstanceEnv(resolvedInstanceId);");
    expect(setupText).toContain("Reconciled OpenClaw runtime instance env to");
    expect(setupText).toContain('spawnSync("openclaw", ["gateway", "restart"]');
  });

  it("OpenClaw add-instance reconciles plugin registration and fails loudly if still missing", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _readOpenClawPluginState()");
    expect(setupText).toContain("function _ensureOpenClawPluginRegistered(pluginPath)");
    expect(setupText).toContain("const reg = _ensureOpenClawPluginRegistered(PLUGIN_DIR);");
    expect(setupText).toContain("OpenClaw add-instance install repaired a missing/stale plugin registration.");
    expect(setupText).toContain("OpenClaw Quaid plugin is not fully registered after install");
    expect(setupText).toContain("pluginListCheckOk");
    expect(setupText).toContain("pluginListed");
    expect(setupText).toContain("const listAttempts = 3;");
    expect(setupText).toContain("const listTimeoutMs = 60_000;");
    expect(setupText).toContain("if (listRes.status === 0 || discovered)");
    expect(setupText).toContain("_sleepMs(listRetryDelayMs)");
    expect(setupText).toContain("const preUninstallList = pluginListHasQuaid();");
    expect(setupText).toContain("if (preUninstallList.hasQuaid || !preUninstallList.ok)");
    expect(setupText).toContain('runCliWithTimeout(cli, ["plugins", "uninstall", "quaid", "--force"], 45_000)');
  });

  it("OpenClaw validation treats plugin-list visibility as diagnostic after direct registration passes", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _openClawPluginDirectRegistrationOk(state)");
    expect(setupText).toContain('log.warn(');
    expect(setupText).toContain("OpenClaw plugins list did not report quaid during");
    expect(setupText).toContain("continuing because direct registration checks passed");
    expect(setupText).toContain("_openClawPluginDirectRegistrationOk(state)");
    expect(setupText).toContain("&& state.pluginListCheckOk");
    expect(setupText).toContain("&& state.pluginListed");
    expect(setupText).toContain("const pluginDirectRegistrationOk = _openClawPluginDirectRegistrationOk(pluginState);");
    expect(setupText).toContain("if (!pluginDirectRegistrationOk)");
    const validationIdx = setupText.indexOf("const pluginDirectRegistrationOk = _openClawPluginDirectRegistrationOk(pluginState);");
    const validationSlice = setupText.slice(validationIdx, setupText.indexOf("_warnOpenClawPluginListDiagnostic(pluginState", validationIdx));
    expect(validationSlice).not.toContain("!pluginState.pluginListCheckOk");
    expect(validationSlice).not.toContain("!pluginState.pluginListed");
  });

  it("OpenClaw installer reconciles launchd env for the gateway service", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('function _ensureOpenClawGatewayLaunchAgentEnv(instanceId = "")');
    expect(setupText).toContain('function _resolveOpenClawGatewayEnvInstanceId(instanceId = "")');
    expect(setupText).toContain('path.join(os.homedir(), "Library", "LaunchAgents", "ai.openclaw.gateway.plist")');
    expect(setupText).toContain('QUAID_HOME: WORKSPACE');
    expect(setupText).toContain('QUAID_INSTANCE: resolvedInstance');
    expect(setupText).toContain('if (fromVars && fromVars !== "openclaw") return fromVars;');
    expect(setupText).toContain('if (selectedLabel) return `openclaw-${selectedLabel}`;');
    expect(setupText).toContain('return "openclaw-main";');
    expect(setupText).toContain('const gatewayLaunchAgentEnvReconciled = _ensureOpenClawGatewayLaunchAgentEnv(resolvedInstanceId);');
    expect(setupText).toContain('Reconciled ai.openclaw.gateway launch agent env for Quaid');
  });

  it("OpenClaw installer writes a real extension directory for discovery", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("Dirent.isDirectory() and does not follow");
    expect(setupText).toContain("fs.cpSync(stagedPluginPath, extensionDir, { recursive: true, dereference: true })");
    expect(setupText).toContain("failed to provision extension directory");
  });

  it("OpenClaw shared config seeds transcript mirror prefixes for lean instance layering", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("adapterCapabilities.preserve_transcript_mirror_session_prefixes");
    expect(setupText).toContain('["agent:main:matrix:channel:"]');
    expect(setupText).toContain("adapterConfig.capabilities = adapterCapabilities;");
  });

  it("first install seeds the shared quaid project docs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('const quaidProjDir = path.join(PROJECTS_DIR, "quaid");');
    expect(setupText).toContain('const quaidProjSrc = path.join(__dirname, "projects", "quaid");');
    expect(setupText).toContain("copyMissingDirSync(quaidProjSrc, quaidProjDir);");
    expect(setupText).toContain('ensureProjectSeedFileFromTemplate(quaidProjSrc, quaidProjDir, "AGENTS.md", MINIMAL_QUAID_PROJECT_AGENTS_MD);');
    expect(setupText).toContain("Before writing any file or delegating work to a sub-agent, pick the first matching rule:");
    expect(setupText).toContain("You MUST NOT write any file to");
    expect(setupText).toContain("reg.create_project(");
    expect(setupText).toContain("'quaid',");
    expect(setupText).toContain("home_dir='projects/quaid/'");
    expect(setupText).toContain("link_global_project('quaid', instance_id=");
    expect(setupText).toContain("if (regQuaidResult.status !== 0)");
  });

  it("installer only registers misc buckets when an explicit instance is being provisioned", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("Register instance misc project in projects/misc--{instanceId}/.");
    expect(setupText).toContain('if (resolvedInstanceId && resolvedInstanceId !== "standalone") {');
    expect(setupText).toContain("from core.project_registry import create_project as create_global_project, get_project as get_global_project, link_project as link_global_project");
    expect(setupText).toContain('create_global_project(');
    expect(setupText).toContain('link_global_project(${JSON.stringify(bucket.name)}, instance_id=');
    expect(setupText).toContain("if (result.status !== 0)");
    expect(setupText).toContain("Re-linked shared bucket:");
  });

  it("Claude Code install creates a plain-shell CLI shim", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function ensureClaudeCliShim()");
    expect(setupText).toContain("function buildClaudeCliWrapper(target)");
    expect(setupText).toContain("resolveHostBinary(\"claude\"");
    expect(setupText).toContain("if (path.resolve(candidateShimPath) === resolvedTargetPath)");
    expect(setupText).toContain("wrapperScript: buildClaudeCliWrapper(target)");
    expect(setupText).toContain("Updated Claude Code CLI shim:");
    expect(setupText).toContain("Could not update Claude Code CLI shim automatically.");
  });

  it("Codex install configures hooks via the managed postinstall path", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const postinstallText = fs.readFileSync(
      path.join(repoRoot, "modules", "quaid", "adaptors", "manifests", "codex", "hooks", "postinstall.mjs"),
      "utf8",
    );

    expect(setupText).toContain("function setupCodexHooks()");
    expect(setupText).toContain('s.start("Configuring Codex hooks...")');
    expect(setupText).toContain("setupCodexHooks();");
    expect(setupText).toContain('s.stop(C.green("Codex hooks configured"))');
    expect(postinstallText).toContain('const codexDir = path.join(os.homedir(), ".codex");');
    expect(postinstallText).toContain('const hooksPath = path.join(codexDir, "hooks.json");');
    expect(postinstallText).toContain('const configJsonPath = path.join(codexDir, "config.json");');
    expect(postinstallText).toContain("configJson.hooks = hooksConfig.hooks;");
    expect(postinstallText).toContain("configJson.features = {");
    expect(postinstallText).toContain('updatedToml = upsertTomlTopLevel(currentToml, "hooks", JSON.stringify(hooksPath));');
    expect(postinstallText).toContain('updatedToml = upsertTomlBool(updatedToml, "features", "codex_hooks", true);');
    expect(postinstallText).toContain('updatedToml = upsertTomlStringInTable(');
    expect(postinstallText).toContain('"trust_level"');
    expect(postinstallText).toContain('"trusted"');
  });

  it("Codex install only registers a persistent launchd daemon agent for explicit instance installs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function installCodexDaemonLaunchAgent(instanceId)");
    expect(setupText).toContain('const label = `com.quaid.daemon.${normalizedInstance}`;');
    expect(setupText).toContain('<string>daemon</string>');
    expect(setupText).toContain('<string>run</string>');
    expect(setupText).toContain('if (resolvedInstanceId) {');
    expect(setupText).toContain('s.start("Installing Codex daemon launch agent...")');
    expect(setupText).toContain("installCodexDaemonLaunchAgent(resolvedInstanceId)");
    expect(setupText).toContain("Skipping Codex daemon launch agent install until the first real instance is created by hook use.");
  });

  it("install ensures all visible identity stubs exist for the resolved instance", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function ensureVisibleIdentityStubs(visibleRoot)");
    expect(setupText).toContain('for (const f of ["SOUL.md", "USER.md", "ENVIRONMENT.md"])');
    expect(setupText).toContain("for (const f of ensureVisibleIdentityStubs(visibleRoot))");
  });
});
