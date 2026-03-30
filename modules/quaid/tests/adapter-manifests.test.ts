import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  ADAPTER_MANIFEST_SCHEMA,
  adapterRegistryDir,
  adapterSelectOptions,
  loadAdapterManifests,
  resolveAdapterHookScript,
  syncBuiltinAdapterManifests,
  validateAdapterManifest,
} from "../lib/adapter-manifests.mjs";

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("adapter manifest registry", () => {
  it("validates a minimal v1 manifest", () => {
    const check = validateAdapterManifest({
      schema: ADAPTER_MANIFEST_SCHEMA,
      id: "openclaw",
      name: "OpenClaw",
      install: { selectLabel: "OpenClaw" },
      runtime: { python: { module: "adaptors.openclaw.adapter", class: "OpenClawAdapter" } },
    });
    expect(check.ok).toBe(true);
  });

  it("rejects unsupported schema", () => {
    const check = validateAdapterManifest({
      schema: "quaid-adapter-install/v2",
      id: "openclaw",
      install: { selectLabel: "OpenClaw" },
    });
    expect(check.ok).toBe(false);
  });

  it("syncs built-in manifests to workspace registry", () => {
    const workspace = makeTempDir("quaid-adapter-registry-");
    const installerDir = makeTempDir("quaid-installer-src-");
    const builtinsDir = path.join(installerDir, "adaptors", "manifests");
    fs.mkdirSync(builtinsDir, { recursive: true });
    fs.writeFileSync(
      path.join(builtinsDir, "agentfoo.json"),
      JSON.stringify({
        schema: ADAPTER_MANIFEST_SCHEMA,
        id: "agentfoo",
        name: "AgentFoo",
        install: { selectLabel: "AgentFoo", selectHint: "external adapter" },
        runtime: { python: { module: "agentfoo.adapter", class: "AgentFooAdapter" } },
        scripts: { preinstall: "./hooks/preinstall.sh" },
      }, null, 2),
      "utf8",
    );
    fs.mkdirSync(path.join(builtinsDir, "hooks"), { recursive: true });
    fs.writeFileSync(path.join(builtinsDir, "hooks", "preinstall.sh"), "#!/bin/sh\necho ok\n", "utf8");

    const copied = syncBuiltinAdapterManifests({ workspace, installerDir });
    expect(copied.length).toBe(1);
    const manifestPath = path.join(adapterRegistryDir(workspace), "agentfoo", "adapter.json");
    expect(fs.existsSync(manifestPath)).toBe(true);
    const hookPath = path.join(adapterRegistryDir(workspace), "agentfoo", "hooks", "preinstall.sh");
    expect(fs.existsSync(hookPath)).toBe(true);
  });

  it("loads manifests and derives select options", () => {
    const workspace = makeTempDir("quaid-adapter-load-");
    const regDir = path.join(adapterRegistryDir(workspace), "agentfoo");
    fs.mkdirSync(regDir, { recursive: true });
    fs.writeFileSync(
      path.join(regDir, "adapter.json"),
      JSON.stringify({
        schema: ADAPTER_MANIFEST_SCHEMA,
        id: "agentfoo",
        name: "AgentFoo",
        install: { selectLabel: "AgentFoo", selectHint: "third-party host" },
        runtime: { python: { module: "agentfoo.adapter", class: "AgentFooAdapter" } },
      }, null, 2),
      "utf8",
    );

    const manifests = loadAdapterManifests(workspace);
    expect(manifests.length).toBe(1);
    expect(manifests[0].id).toBe("agentfoo");
    const options = adapterSelectOptions(manifests);
    expect(options[0].value).toBe("agentfoo");
  });

  it("resolves hook script path within manifest dir", () => {
    const workspace = makeTempDir("quaid-adapter-hook-");
    const manifestDir = path.join(adapterRegistryDir(workspace), "agentfoo");
    fs.mkdirSync(path.join(manifestDir, "hooks"), { recursive: true });
    const manifest = {
      id: "agentfoo",
      __path: path.join(manifestDir, "adapter.json"),
      scripts: { preinstall: "./hooks/preinstall.sh" },
    };
    const resolved = resolveAdapterHookScript(manifest, "preinstall");
    expect(resolved).toBe(path.join(manifestDir, "hooks", "preinstall.sh"));
  });
});
