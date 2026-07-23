import { describe, expect, it } from "vitest";
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { shouldStartExtractionDaemonAfterInstall } from "../../../lib/install-daemon-policy.mjs";
import { ensureInstalledQuaidCli } from "../../../lib/install-cli-wrapper.mjs";

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function buildClaudeWrapperForTest(setupText: string, target: string): string {
  return loadClaudeShimHelpersForTest(setupText).buildClaudeCliWrapper(target);
}

function loadClaudeShimHelpersForTest(setupText: string): {
  buildClaudeCliWrapper: (targetPath: string) => string;
  ensureCliShim: (targetPath: string, shimName: string, options?: { wrapperScript?: string }) => string;
} {
  const start = setupText.indexOf("function _shellQuote");
  const end = setupText.indexOf("function ensureQuaidCliShim");
  expect(start).toBeGreaterThanOrEqual(0);
  expect(end).toBeGreaterThan(start);
  const snippet = setupText.slice(start, end);
  return new Function(
    "fs",
    "path",
    "process",
    "os",
    `${snippet}\nreturn { buildClaudeCliWrapper, ensureCliShim };`,
  )(fs, path, process, os) as {
    buildClaudeCliWrapper: (targetPath: string) => string;
    ensureCliShim: (targetPath: string, shimName: string, options?: { wrapperScript?: string }) => string;
  };
}

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

  it("installer clears stale supervisors before starting hook-driven daemons", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    const stopIdx = setupText.indexOf('s.start("Stopping stale Quaid supervisor...")');
    const startIdx = setupText.indexOf('s.start("Starting extraction daemon...")');
    expect(stopIdx).toBeGreaterThan(-1);
    expect(startIdx).toBeGreaterThan(stopIdx);
    expect(setupText).toContain("from core import project_docs");
    expect(setupText).toContain("project_docs.stop_supervisor()");
    expect(setupText).toContain("failed to stop stale Quaid supervisor before daemon restart");
  });

  it("installer repairs and validates the installed quaid CLI before shimming it", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const helperText = fs.readFileSync(path.join(repoRoot, "lib", "install-cli-wrapper.mjs"), "utf8");
    const tempRoot = makeTempDir("quaid-install-cli-");
    const sourcePlugin = path.join(tempRoot, "source");
    const installedPlugin = path.join(tempRoot, "installed");
    fs.mkdirSync(sourcePlugin, { recursive: true });
    fs.mkdirSync(installedPlugin, { recursive: true });
    const sourceCli = path.join(sourcePlugin, "quaid");
    const installedCli = path.join(installedPlugin, "quaid");
    fs.writeFileSync(sourceCli, "#!/usr/bin/env bash\necho quaid\n", { encoding: "utf8", mode: 0o755 });
    fs.writeFileSync(installedCli, "", "utf8");

    ensureInstalledQuaidCli(sourcePlugin, installedPlugin);

    expect(fs.statSync(installedCli).size).toBe(fs.statSync(sourceCli).size);
    expect(fs.readFileSync(installedCli, "utf8")).toContain("echo quaid");
    expect(fs.statSync(installedCli).mode & 0o111).toBeGreaterThan(0);
    expect(helperText).toContain("function ensureInstalledQuaidCli(sourcePluginDir, installedPluginDir");
    expect(helperText).toContain("quaid CLI wrapper missing or empty in plugin source");
    expect(helperText).toContain("quaid CLI wrapper missing or empty after install");

    const repairIdx = setupText.indexOf("ensureInstalledQuaidCli(pluginSrc, PLUGIN_DIR, { log });");
    const shimIdx = setupText.indexOf("const shimPath = ensureQuaidCliShim(PLUGIN_DIR);");
    expect(repairIdx).toBeGreaterThan(-1);
    expect(shimIdx).toBeGreaterThan(repairIdx);
  });

  it("installer fails loudly when the source quaid CLI wrapper is empty", () => {
    const tempRoot = makeTempDir("quaid-install-cli-empty-");
    const sourcePlugin = path.join(tempRoot, "source");
    const installedPlugin = path.join(tempRoot, "installed");
    fs.mkdirSync(sourcePlugin, { recursive: true });
    fs.mkdirSync(installedPlugin, { recursive: true });
    fs.writeFileSync(path.join(sourcePlugin, "quaid"), "", "utf8");

    expect(() => ensureInstalledQuaidCli(sourcePlugin, installedPlugin)).toThrow(
      "quaid CLI wrapper missing or empty in plugin source",
    );
  });

  it("installer preserves a valid installed quaid CLI wrapper", () => {
    const tempRoot = makeTempDir("quaid-install-cli-valid-");
    const sourcePlugin = path.join(tempRoot, "source");
    const installedPlugin = path.join(tempRoot, "installed");
    fs.mkdirSync(sourcePlugin, { recursive: true });
    fs.mkdirSync(installedPlugin, { recursive: true });
    fs.writeFileSync(path.join(sourcePlugin, "quaid"), "#!/usr/bin/env bash\necho source\n", "utf8");
    const installedCli = path.join(installedPlugin, "quaid");
    fs.writeFileSync(installedCli, "#!/usr/bin/env bash\necho installed\n", "utf8");

    ensureInstalledQuaidCli(sourcePlugin, installedPlugin);

    expect(fs.readFileSync(installedCli, "utf8")).toContain("echo installed");
    expect(fs.readFileSync(installedCli, "utf8")).not.toContain("echo source");
    expect(fs.statSync(installedCli).mode & 0o111).toBeGreaterThan(0);
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

  it("installer threads real instances into TOOLS domain sync instead of silently swallowing failures", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const setupShellText = fs.readFileSync(path.join(repoRoot, "setup-quaid.sh"), "utf8");
    const syncIdx = setupText.indexOf("const syncToolsScript = path.join(pluginSrc");
    const shellSyncIdx = setupShellText.indexOf("sync-tools-domain-block.py");
    expect(syncIdx).toBeGreaterThanOrEqual(0);
    expect(shellSyncIdx).toBeGreaterThanOrEqual(0);
    const syncBlock = setupText.slice(syncIdx, syncIdx + 900);
    const shellSyncBlock = setupShellText.slice(shellSyncIdx - 250, shellSyncIdx + 550);

    expect(syncBlock).toContain("const syncToolsScript = path.join(pluginSrc, \"scripts\", \"sync-tools-domain-block.py\");");
    expect(syncBlock).toContain(
      'const syncToolsResult = python3Spawn([syncToolsScript, "--workspace", WORKSPACE, "--instance", resolvedInstanceId], {',
    );
    expect(syncBlock).toContain("if (syncToolsResult.status !== 0)");
    expect(syncBlock).toContain("syncToolsResult.error?.message");
    expect(syncBlock).toContain("TOOLS.md domain block sync skipped");
    expect(syncBlock).toContain("Skipping TOOLS.md domain block sync until a real instance ID is known.");

    expect(shellSyncBlock).toContain('--instance "${QUAID_INSTANCE}"');
    expect(shellSyncBlock).toContain("[domains] TOOLS.md domain block sync skipped: QUAID_INSTANCE is not set");
    expect(shellSyncBlock).not.toContain("2>/dev/null");
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
    expect(setupText).toContain('if (instanceId.startsWith("openclaw-")) return "openclaw";');
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

  it("generates installer python env dynamically for chained platform installs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function pythonInstallerEnvSetup(adapterType = \"\")");
    expect(setupText).toContain("os.environ['QUAID_ADAPTER_TYPE']");
    expect(setupText).toContain("os.environ.pop('QUAID_INSTANCE', None)");
    expect(setupText).not.toContain("const PY_ENV_SETUP =");
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
    const preflightStart = setupText.indexOf('s.message("Checking OpenClaw gateway status...");');
    const preflightEnd = setupText.indexOf("    // --- Onboarding / agents list ---");
    expect(preflightStart).toBeGreaterThan(-1);
    expect(preflightEnd).toBeGreaterThan(preflightStart);
    const preflightBlock = setupText.slice(preflightStart, preflightEnd);
    expect(preflightBlock).toContain('let gatewayHealthCode = _gatewayHttpCode("/health", "GET", null);');
    expect(preflightBlock).toContain("await waitForGatewayWarmup(60_000)");
    expect(preflightBlock).toContain("_sanitizeOpenClawGatewayBlockingStaleQuaidRegistration()");
    expect(preflightBlock).not.toContain('["status"]');
    expect(preflightBlock).not.toContain('["gateway", "probe"]');
    expect(preflightBlock).not.toContain("runCliWithTimeout(bin, args, 8_000)");
  });

  it("OpenClaw gateway health checks resolve the port from config before falling back to defaults", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _resolveOpenClawGatewayPort()");
    expect(setupText).toContain('const envPort = String(process.env.OPENCLAW_GATEWAY_PORT || "").trim();');
    expect(setupText).toContain('const cfgPort = String(parsed?.gateway?.port || "").trim();');
    expect(setupText).toContain('const rawPort = _resolveOpenClawGatewayPort();');
  });

  it("OpenClaw installer smoke can recover a foreground gateway when explicitly enabled", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const workflowText = fs.readFileSync(
      path.join(repoRoot, ".github", "workflows", "installer-openclaw-smoke.yml"),
      "utf8",
    );

    expect(workflowText).toContain("QUAID_INSTALLER_OPENCLAW_FOREGROUND_GATEWAY_RECOVERY=1");
    expect(setupText).toContain("function _foregroundGatewayRecoveryEnabled()");
    expect(setupText).toContain("function _startForegroundOpenClawGateway(cli, context)");
    expect(setupText).toContain('text.includes("runtime: stopped")');
    expect(setupText).toContain('["gateway", "run", "--allow-unconfigured", "--force", "--port", port]');
    expect(setupText).toContain("Gateway service is unavailable during ${context}; attempting env-gated foreground recovery.");
  });

  it("OpenClaw hook symbol grep is diagnostic after the version gate", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("Gateway lifecycle support (version-gated)");
    expect(setupText).toContain("function gatewayHasHookSymbols(gwDir)");
    expect(setupText).not.toContain('bail("Gateway hooks required. Update OpenClaw and re-run.");');
    expect(setupText).not.toContain("Your gateway is missing the memory hooks Quaid needs.");
  });

  it("OpenClaw installer blocks Matrix reply-session conflict versions", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('const OPENCLAW_REPLY_SESSION_INIT_BUG_MIN_VERSION = "2026.6.11";');
    expect(setupText).toContain('const OPENCLAW_REPLY_SESSION_INIT_FIXED_VERSION = "2026.6.33";');
    expect(setupText).toContain("function isOpenClawReplySessionInitBugVersion(actualRaw)");
    expect(setupText).toContain("isOpenClawReplySessionInitBugVersion(gwVersion)");
    expect(setupText).toContain("can drop the second turn after /new");
    expect(setupText).toContain("openclaw update --tag extended-stable --yes");
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
    expect(setupText).toContain("const preservedOpenClawMatrixConfig = _isPlatform(\"openclaw\") ? _captureOpenClawMatrixConfig() : null;");
    expect(setupText).toContain('await _reassertOpenClawPostRestartState("runtime env reconcile", preservedOpenClawMatrixConfig);');
  });

  it("OpenClaw add-instance reconciles plugin registration and fails loudly if still missing", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _readOpenClawPluginState(options = {})");
    expect(setupText).toContain("function _ensureOpenClawPluginRegistered(pluginPath)");
    expect(setupText).toContain("const reg = _ensureOpenClawPluginRegistered(PLUGIN_DIR);");
    expect(setupText).toContain("OpenClaw add-instance install repaired a missing/stale plugin registration.");
    expect(setupText).toContain("OpenClaw Quaid plugin is not fully registered after install");
    expect(setupText).toContain("pluginListCheckOk");
    expect(setupText).toContain("pluginListed");
    expect(setupText).toContain("_readOpenClawPluginState({ skipPluginList: true })");
    expect(setupText).toContain("const listAttempts = 3;");
    expect(setupText).toContain("const listTimeoutMs = 60_000;");
    expect(setupText).toContain("if (listRes.status === 0 || discovered)");
    expect(setupText).toContain('const installsPath = path.join(os.homedir(), ".openclaw", "plugins", "installs.json");');
    expect(setupText).toContain("plugins?.installRecords?.quaid?.installPath");
    expect(setupText).toContain("plugins?.installs?.quaid?.installPath");
    expect(setupText).toContain("parsed?.installRecords?.quaid?.installPath");
    expect(setupText).toContain("installPathExists: !!(installPath ? fs.existsSync(installPath) : fs.existsSync(extensionDir))");
    expect(setupText).toContain("Avoid OpenClaw plugin");
    expect(setupText).not.toContain("const preUninstallList = pluginListHasQuaid();");
    expect(setupText).not.toContain('runCliWithTimeout(cli, ["plugins", "uninstall", "quaid", "--force"], 45_000)');
  });

  it("OpenClaw plugin registration stages runtime source without generated caches", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("OPENCLAW_PLUGIN_STAGE_EXCLUDE_NAMES");
    expect(setupText).toContain('"node_modules"');
    expect(setupText).toContain('"tests"');
    expect(setupText).toContain('"logs"');
    expect(setupText).toContain("_copyOpenClawPluginSource(pluginPath, stagedPluginPath);");
    expect(setupText).toContain("_copyOpenClawPluginSource(stagedPluginPath, extensionDir);");
    expect(setupText).toContain("pluginDir: pluginPath");
    expect(setupText).not.toContain("fs.cpSync(stagedPluginPath, extensionDir, { recursive: true, dereference: true })");
  });

  it("OpenClaw install no longer strips managed state before the install succeeds", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    const preflightStart = setupText.indexOf('s.message("Checking OpenClaw agent configuration...");');
    const preflightEnd = setupText.indexOf("    _ensureOpenClawCompactionModeDefault();");
    expect(preflightStart).toBeGreaterThan(-1);
    expect(preflightEnd).toBeGreaterThan(preflightStart);
    const preflightBlock = setupText.slice(preflightStart, preflightEnd);
    expect(preflightBlock).not.toContain("_sanitizeOpenClawMemorySlot();");
    expect(preflightBlock).not.toContain("_sanitizeOpenClawQuaidPluginEntry();");
    expect(preflightBlock).not.toContain("_removeOpenClawPluginsAllowQuaid();");

    const precleanStart = setupText.indexOf("// Pre-clean stale extension/config before direct repair.");
    const precleanEnd = setupText.indexOf("// OpenClaw plugin discovery reads Dirent.isDirectory()");
    expect(precleanStart).toBeGreaterThan(-1);
    expect(precleanEnd).toBeGreaterThan(precleanStart);
    const precleanBlock = setupText.slice(precleanStart, precleanEnd);
    expect(precleanBlock).toContain("_sanitizeOpenClawPluginInstallSources();");
    expect(precleanBlock).not.toContain("_sanitizeOpenClawMemorySlot();");
    expect(precleanBlock).not.toContain("_sanitizeOpenClawQuaidPluginEntry();");
    expect(precleanBlock).not.toContain("_removeOpenClawPluginsAllowQuaid();");
    expect(setupText).not.toContain("function _sanitizeOpenClawMemorySlot()");
    expect(setupText).not.toContain("function _removeOpenClawPluginsAllowQuaid()");
  });

  it("OpenClaw preflight only clears stale Quaid plugin records when install path is missing", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _sanitizeOpenClawGatewayBlockingStaleQuaidRegistration()");
    expect(setupText).toContain("if (installPath && fs.existsSync(installPath)) return false;");
    expect(setupText).toContain("if (!installPath && fs.existsSync(defaultExtensionDir)) return false;");
    expect(setupText).toContain("delete plugins.entries.quaid;");
    expect(setupText).toContain("delete plugins.slots.memory;");
    expect(setupText).toContain("delete plugins.installs.quaid;");
    expect(setupText).toContain("Cleared stale Quaid plugin registration; restarting OpenClaw gateway...");
    expect(setupText).toContain("Starting foreground OpenClaw gateway recovery...");
    expect(setupText).toContain('_startForegroundOpenClawGateway(cfgCli, "stale Quaid plugin preflight recovery")');
  });

  it("OpenClaw validation treats plugin-list visibility as diagnostic after direct registration passes", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _openClawPluginDirectRegistrationOk(state)");
    expect(setupText).toContain('log.warn(');
    expect(setupText).toContain("OpenClaw plugins list did not report quaid during");
    expect(setupText).toContain("continuing because direct registration checks passed");
    expect(setupText).toContain("_openClawPluginDirectRegistrationOk(state)");
    expect(setupText).toContain("listState.pluginListCheckOk && listState.pluginListed");
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
    const fnStart = setupText.indexOf('function _ensureOpenClawGatewayLaunchAgentEnv(instanceId = "")');
    const fnEnd = setupText.indexOf("function _sanitizeOpenClawQuaidPluginEntry()", fnStart);
    const fnSlice = setupText.slice(fnStart, fnEnd);

    expect(setupText).toContain('function _ensureOpenClawGatewayLaunchAgentEnv(instanceId = "")');
    expect(setupText).toContain('function _resolveOpenClawGatewayEnvInstanceId(instanceId = "")');
    expect(setupText).toContain('path.join(os.homedir(), "Library", "LaunchAgents", "ai.openclaw.gateway.plist")');
    expect(fnSlice).toContain('QUAID_HOME: WORKSPACE');
    expect(fnSlice).toContain('QUAID_VISIBLE_HOME: VISIBLE_HOME');
    expect(fnSlice).toContain('QUAID_INSTANCE: resolvedInstance');
    expect(fnSlice).toContain('const obsoleteEnvKeys = ["OPENCLAW_WORKSPACE"];');
    expect(fnSlice).toContain('Delete :EnvironmentVariables:${key}');
    expect(fnSlice).not.toContain('OPENCLAW_WORKSPACE: WORKSPACE');
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
    expect(setupText).toContain("_copyOpenClawPluginSource(stagedPluginPath, extensionDir);");
    expect(setupText).toContain("failed to provision extension directory");
  });

  it("OpenClaw hotswap expands remote home paths instead of creating literal tilde dirs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const hotswapText = fs.readFileSync(
      path.join(repoRoot, "modules", "quaid", "scripts", "hotswap-openclaw-adapter.sh"),
      "utf8",
    );

    expect(hotswapText).toContain("printf '$HOME'");
    expect(hotswapText).toContain('local rest="${value:2}"');
    expect(hotswapText).toContain('printf "\\$HOME/\'%s\'"');
    expect(hotswapText).not.toContain('${value#~/}');
    expect(hotswapText).not.toContain('printf "~/\'%s\'"');
    expect(hotswapText).toContain("copy_and_verify");
    expect(hotswapText).toContain("Remote copy verification failed");
    expect(hotswapText).toContain("Verified remote copy:");
  });

  it("OpenClaw installer re-sanitizes native memory plugins after gateway reloads", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('import { sanitizeOpenClawNativeMemoryPlugins } from "./lib/openclaw-plugin-sanitizer.mjs";');
    expect(setupText).toContain("function _sanitizeOpenClawNativeMemoryPlugins()");
    expect(setupText).toContain("async function _reassertOpenClawPostRestartState(context = \"gateway restart\", matrixSnapshot = null)");
    expect(setupText).toContain('await _reassertOpenClawPostRestartState("plugin registration", preservedOpenClawMatrixConfig);');
    expect(setupText).toContain('await _reassertOpenClawPostRestartState("hook configuration", preservedOpenClawMatrixConfig);');
    expect(setupText).toContain("native-memory-plugins");
    expect(setupText).not.toContain('plugins.entries[pluginId] = { disabled: true }');
    expect(setupText).not.toContain("current.disabled !== true");
    expect(setupText).toContain("Restarting gateway to apply changes.");
  });

  it("OpenClaw installer preserves matrix channel config across its own restart points", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain('import {');
    expect(setupText).toContain('captureOpenClawMatrixConfig,');
    expect(setupText).toContain('restoreOpenClawMatrixConfig,');
    expect(setupText).toContain("function _captureOpenClawMatrixConfig()");
    expect(setupText).toContain("function _restoreOpenClawMatrixConfig(snapshot)");
    expect(setupText).toContain("const matrixRestore = _restoreOpenClawMatrixConfig(matrixSnapshot);");
    expect(setupText).toContain("changedBits.push(\"matrix-channel\")");
    expect(setupText).toContain('await _reassertOpenClawPostRestartState("plugin registration", preservedOpenClawMatrixConfig);');
    expect(setupText).toContain('await _reassertOpenClawPostRestartState("hook configuration", preservedOpenClawMatrixConfig);');
  });

  it("livetest preflight seeds OpenClaw Matrix with current non-legacy schema", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const preflightText = fs.readFileSync(
      path.join(repoRoot, "modules/quaid/tests/livetest/scripts/livetest-preflight.sh"),
      "utf8",
    );

    expect(preflightText).toContain('network_cfg["dangerouslyAllowPrivateNetwork"] = True');
    expect(preflightText).toContain('matrix_cfg.pop("allowPrivateNetwork", None)');
    expect(preflightText).toContain('room_entry.pop("allow", None)');
    expect(preflightText).not.toContain('matrix_cfg["allowPrivateNetwork"] = True');
    expect(preflightText).not.toContain('room_entry["allow"] = True');
  });

  it("presnapshot preflight bakes the OpenClaw Matrix plugin into the base image", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const presnapshotText = fs.readFileSync(
      path.join(repoRoot, "modules/quaid/tests/livetest/scripts/livetest-presnapshot-preflight.sh"),
      "utf8",
    );

    expect(presnapshotText).toContain("run_presnapshot_matrix_plugin_install() {");
    expect(presnapshotText).toContain('min_openclaw_version="${OPENCLAW_MATRIX_MIN_OPENCLAW_VERSION:-2026.6.33}"');
    expect(presnapshotText).toContain('matrix_plugin_version="${OPENCLAW_MATRIX_PLUGIN_VERSION:-2026.6.1}"');
    expect(presnapshotText).toContain('matrix_plugin_spec="${OPENCLAW_MATRIX_PLUGIN_SPEC:-@openclaw/matrix@${matrix_plugin_version}}"');
    expect(presnapshotText).toContain("openclaw plugins list");
    expect(presnapshotText).toContain("openclaw plugins install --force --pin '${matrix_plugin_spec}'");
    expect(presnapshotText).toContain("OpenClaw ${oc_version:-unknown} is too old for Matrix plugin bake");
    expect(presnapshotText).toContain("OpenClaw Matrix plugin install failed");
    expect(presnapshotText).toContain("livetest-openclaw-gateway-restart.sh");
    expect(presnapshotText).toContain("PRESNAPSHOT_CLEANUP_CHANGED=1");
  });

  it("OpenClaw install acquires a host-level config lock before preflight writes", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _ensureOpenClawHostConfigLock()");
    expect(setupText).toContain('const lockFile = path.join(openClawRoot, ".quaid-installer.lock");');
    expect(setupText).toContain("Another installer is already mutating the host OpenClaw config.");
    expect(setupText).toContain("_ensureOpenClawHostConfigLock();");

    const lockCall = setupText.indexOf("_ensureOpenClawHostConfigLock();");
    const preflightHook = setupText.indexOf('runAdapterInstallHook(resolvedInstallerPlatform(), "preinstall");');
    expect(lockCall).toBeGreaterThan(-1);
    expect(preflightHook).toBeGreaterThan(-1);
    expect(lockCall).toBeLessThan(preflightHook);
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
    expect(setupText).toContain("function isQuaidClaudeCliWrapper(candidate)");
    expect(setupText).toContain("return target && !isQuaidClaudeCliWrapper(target) ? target : \"\"");
    expect(setupText).toContain("resolveHostBinary(\"claude\"");
    expect(setupText).toContain("if (path.resolve(candidateShimPath) === resolvedTargetPath)");
    expect(setupText).toContain("wrapperScript: buildClaudeCliWrapper(target)");
    expect(setupText).toContain("path.basename(realTarget) === \"cli.js\"");
    expect(setupText).toContain("const wrapperScript = typeof options.wrapperScript === \"string\"");
    expect(setupText).toContain(".credentials.json.quaid-run.");
    expect(setupText).toContain("_quaid_cc_restore() {");
    expect(setupText).toContain("trap '_quaid_cc_restore' EXIT");
    expect(setupText).toContain("$_quaid_cc_backup");
    expect(setupText).toContain("restored valid Claude credentials after CLI cleared the access token");
    expect(setupText).toContain("_quaid_cc_status=$?");
    expect(setupText).not.toContain("exec ${_shellQuote(process.execPath)} ${_shellQuote(realTarget)}");
    expect(setupText).not.toContain("path.basename(realTarget) !== \"cli.js\"");
    expect(setupText).not.toContain("selfTargetCollision && typeof options.wrapperScript");
    expect(setupText).toContain("Updated Claude Code CLI shim:");
    expect(setupText).toContain("Could not update Claude Code CLI shim automatically.");
  });

  it("Claude Code shim restores a valid credential cleared by the CLI", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const tmp = makeTempDir("quaid-claude-shim-");
    try {
      const realCli = path.join(tmp, "cli.js");
      fs.writeFileSync(
        realCli,
        [
          "const fs = require('fs');",
          "const path = require('path');",
          "const creds = path.join(process.env.HOME, '.claude', '.credentials.json');",
          "fs.writeFileSync(creds, JSON.stringify({ claudeAiOauth: { accessToken: '', refreshToken: 'placeholder-not-used-accesstoken-valid-48h', expiresAt: 0 } }) + '\\n');",
          "process.exit(7);",
          "",
        ].join("\n"),
        "utf8",
      );
      const wrapperPath = path.join(tmp, "claude");
      fs.writeFileSync(wrapperPath, buildClaudeWrapperForTest(setupText, realCli), {
        encoding: "utf8",
        mode: 0o755,
      });
      fs.chmodSync(wrapperPath, 0o755);

      const home = path.join(tmp, "home");
      const claudeDir = path.join(home, ".claude");
      fs.mkdirSync(claudeDir, { recursive: true });
      const credsPath = path.join(claudeDir, ".credentials.json");
      const expiresAt = Date.now() + 48 * 60 * 60 * 1000;
      fs.writeFileSync(
        credsPath,
        JSON.stringify({
          claudeAiOauth: {
            accessToken: "valid-access-token",
            refreshToken: "placeholder-not-used-accesstoken-valid-48h",
            expiresAt,
          },
        }) + "\n",
        "utf8",
      );

      const result = spawnSync(wrapperPath, ["--fake"], {
        env: { ...process.env, HOME: home },
        encoding: "utf8",
      });

      expect(result.status).toBe(7);
      expect(result.stderr).toContain("restored valid Claude credentials");
      const restored = JSON.parse(fs.readFileSync(credsPath, "utf8"));
      expect(restored.claudeAiOauth.accessToken).toBe("valid-access-token");
      expect(restored.claudeAiOauth.expiresAt).toBe(expiresAt);
      expect(fs.readdirSync(claudeDir).some((name) => name.includes(".quaid-run."))).toBe(false);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("Claude Code shim restores credentials for a non-JS Claude binary target", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const tmp = makeTempDir("quaid-claude-native-shim-");
    try {
      const realCli = path.join(tmp, "claude.exe");
      fs.writeFileSync(
        realCli,
        [
          "#!/bin/sh",
          "cat > \"$HOME/.claude/.credentials.json\" <<'JSON'",
          "{\"claudeAiOauth\":{\"accessToken\":\"\",\"refreshToken\":\"placeholder-not-used-accesstoken-valid-48h\",\"expiresAt\":0}}",
          "JSON",
          "exit 9",
          "",
        ].join("\n"),
        { encoding: "utf8", mode: 0o755 },
      );
      fs.chmodSync(realCli, 0o755);
      const wrapperPath = path.join(tmp, "claude");
      fs.writeFileSync(wrapperPath, buildClaudeWrapperForTest(setupText, realCli), {
        encoding: "utf8",
        mode: 0o755,
      });
      fs.chmodSync(wrapperPath, 0o755);

      const home = path.join(tmp, "home");
      const claudeDir = path.join(home, ".claude");
      fs.mkdirSync(claudeDir, { recursive: true });
      const credsPath = path.join(claudeDir, ".credentials.json");
      const expiresAt = Date.now() + 48 * 60 * 60 * 1000;
      fs.writeFileSync(
        credsPath,
        JSON.stringify({
          claudeAiOauth: {
            accessToken: "valid-access-token",
            refreshToken: "placeholder-not-used-accesstoken-valid-48h",
            expiresAt,
          },
        }) + "\n",
        "utf8",
      );

      const result = spawnSync(wrapperPath, ["--fake"], {
        env: { ...process.env, HOME: home },
        encoding: "utf8",
      });

      expect(result.status).toBe(9);
      expect(result.stderr).toContain("restored valid Claude credentials");
      const restored = JSON.parse(fs.readFileSync(credsPath, "utf8"));
      expect(restored.claudeAiOauth.accessToken).toBe("valid-access-token");
      expect(restored.claudeAiOauth.expiresAt).toBe(expiresAt);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });

  it("Claude Code install writes credential wrapper for non-collision shim topology", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");
    const helpers = loadClaudeShimHelpersForTest(setupText);
    const tmp = makeTempDir("quaid-claude-wrapper-install-");
    const oldHome = process.env.HOME;
    const oldPath = process.env.PATH;
    try {
      const home = path.join(tmp, "home");
      const shimDir = path.join(home, "bin");
      const targetDir = path.join(tmp, "system-bin");
      fs.mkdirSync(shimDir, { recursive: true });
      fs.mkdirSync(targetDir, { recursive: true });
      const realCli = path.join(targetDir, "claude.exe");
      fs.writeFileSync(realCli, "#!/bin/sh\nexit 0\n", { encoding: "utf8", mode: 0o755 });
      fs.chmodSync(realCli, 0o755);
      process.env.HOME = home;
      process.env.PATH = shimDir;

      const wrapperScript = helpers.buildClaudeCliWrapper(realCli);
      const shimPath = helpers.ensureCliShim(realCli, "claude", { wrapperScript });

      expect(shimPath).toBe(path.join(shimDir, "claude"));
      expect(fs.lstatSync(shimPath).isSymbolicLink()).toBe(false);
      const shimText = fs.readFileSync(shimPath, "utf8");
      expect(shimText).toContain("_quaid_cc_restore() {");
      expect(shimText).toContain("claude.exe");
    } finally {
      if (oldHome === undefined) {
        delete process.env.HOME;
      } else {
        process.env.HOME = oldHome;
      }
      if (oldPath === undefined) {
        delete process.env.PATH;
      } else {
        process.env.PATH = oldPath;
      }
      fs.rmSync(tmp, { recursive: true, force: true });
    }
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
    expect(postinstallText).toContain('updatedToml = removeTomlTopLevelKey(currentToml, "hooks");');
    expect(postinstallText).toContain('updatedToml = stripManagedHookTomlBlocks(updatedToml, managedCommands);');
    expect(postinstallText).not.toContain("managedHookTomlBlocks(desiredHooks)");
    expect(postinstallText).toContain("delete configJson.features.codex_hooks;");
    expect(postinstallText).toContain('updatedToml = removeTomlBool(updatedToml, "features", "codex_hooks");');
    expect(postinstallText).toContain('updatedToml = upsertTomlBool(updatedToml, "features", "hooks", true);');
    expect(postinstallText).toContain('updatedToml = upsertTomlStringInTable(');
    expect(postinstallText).toContain("upsertCodexHookTrustState(");
    expect(postinstallText).toContain("normalizedCodexCommandHookHash(");
    expect(postinstallText).toContain("trusted_hash");
    expect(postinstallText).toContain('"trust_level"');
    expect(postinstallText).toContain('"trusted"');
  });

  it("Codex install removes legacy launchd daemon agents for explicit instance installs", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function removeCodexDaemonLaunchAgent(instanceId)");
    expect(setupText).toContain('const label = `com.quaid.daemon.${normalizedInstance}`;');
    expect(setupText).toContain('spawnSync("launchctl", ["bootout", `gui/${process.getuid()}`, plistPath]');
    expect(setupText).toContain('spawnSync("launchctl", ["remove", label]');
    expect(setupText).toContain('if (resolvedInstanceId) {');
    expect(setupText).toContain('s.start("Removing legacy Codex daemon launch agent...")');
    expect(setupText).toContain("removeCodexDaemonLaunchAgent(resolvedInstanceId)");
    expect(setupText).toContain("Skipping legacy Codex daemon launch agent cleanup until a real instance ID is known.");
  });

  it("install ensures all visible identity stubs exist for the resolved instance", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function ensureVisibleIdentityStubs(visibleRoot)");
    expect(setupText).toContain('for (const f of ["SOUL.md", "USER.md", "ENVIRONMENT.md"])');
    expect(setupText).toContain("for (const f of ensureVisibleIdentityStubs(visibleRoot))");
  });

  it("Claude Code install defers instance silos to project-derived hook runtime", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    const platformBranch = setupText.indexOf('if (platform === "claude-code") return "";');
    const overrideBranch = setupText.indexOf("if (_instanceIdOverride) return _instanceIdOverride;");

    expect(platformBranch).toBeGreaterThan(-1);
    expect(overrideBranch).toBeGreaterThan(platformBranch);
    expect(setupText).toContain(
      "Claude Code instances are derived from the active project path on first hook use."
    );
  });
});
