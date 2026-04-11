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
    expect(setupText).toContain("_remainingInstallableAdapterOptions(installed)");
    expect(setupText).toContain("Other supported platforms were detected. Install another?");
    expect(setupText).toContain("!ALLOW_EXISTING_INSTALL && !_chainedPlatformInstall");
  });

  it("OpenClaw install hard-stops when the gateway is unreachable", () => {
    const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
    const setupText = fs.readFileSync(path.join(repoRoot, "setup-quaid.mjs"), "utf8");

    expect(setupText).toContain("OpenClaw gateway must be running before installing Quaid.");
    expect(setupText).toContain('bail("OpenClaw gateway must be running before installing Quaid.");');
    expect(setupText).not.toContain("OpenClaw status/probe unavailable in agent mode; continuing with install.");
  });
});
