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

  it("installer hydrates the default resolved instance config, not only explicit env instances", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("process.env.QUAID_INSTANCE || resolvedInstallerInstanceId(resolvedAdapterType)");
    expect(setupText).toContain("hydratePlatformInstanceConfigs");
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
    expect(setupText).toContain('if (INSTALL_ALL_PLATFORMS) {');
    expect(setupText).toContain('platform = "__install_all__";');
    expect(setupText).toContain('_beginChainedPlatformInstall(firstAdapter, queuedAdapters);');
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
  });

  it("OpenClaw install hard-stops when the gateway is unreachable", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("OpenClaw gateway must be running before installing Quaid.");
    expect(setupText).toContain('bail("OpenClaw gateway must be running before installing Quaid.");');
    expect(setupText).not.toContain("OpenClaw status/probe unavailable in agent mode; continuing with install.");
  });

  it("OpenClaw add-instance still writes gateway runtime env", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("const runtimeEnvChanged = _ensureOpenClawRuntimeInstanceEnv(_ocRuntimeInstance);");
    expect(setupText).toContain("responsesEndpointChanged || agentModelChanged || runtimeEnvChanged");
    expect(setupText).toContain("parsed.env.OPENCLAW_WORKSPACE = WORKSPACE;");
    expect(setupText).not.toContain("leaving fallback QUAID_INSTANCE unchanged in add-instance mode");
  });

  it("OpenClaw add-instance reconciles plugin registration and fails loudly if still missing", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("function _readOpenClawPluginState()");
    expect(setupText).toContain("function _ensureOpenClawPluginRegistered(pluginPath)");
    expect(setupText).toContain("const reg = _ensureOpenClawPluginRegistered(PLUGIN_DIR);");
    expect(setupText).toContain("OpenClaw add-instance install repaired a missing/stale plugin registration.");
    expect(setupText).toContain("OpenClaw Quaid plugin is not fully registered after install");
  });

  it("OpenClaw installer refreshes the extension dir from the canonical plugin tree", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("extensionDir: pluginPath");
    expect(setupText).toContain("fs.cpSync(pluginPath, extensionDir, {");
    expect(setupText).toContain("failed to copy canonical plugin into extension dir");
    expect(setupText).not.toContain("fs.symlinkSync(pluginPath, extensionDir, \"dir\")");
  });
});
