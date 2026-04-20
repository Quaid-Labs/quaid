import { describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { ensureOpenClawExtensionDependencies } from "../../../lib/openclaw-extension-deps.mjs";

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

function writeSentinel(rootDir: string): void {
  const sentinel = path.join(rootDir, "node_modules", "@sinclair", "typebox");
  fs.mkdirSync(sentinel, { recursive: true });
  fs.writeFileSync(path.join(sentinel, "package.json"), JSON.stringify({ name: "@sinclair/typebox" }), "utf8");
}

describe("openclaw extension dependency provisioning", () => {
  it("copies only runtime deps from the plugin source when available", () => {
    const extensionDir = makeTempDir("quaid-oc-ext-");
    const pluginDir = makeTempDir("quaid-oc-plugin-");
    fs.writeFileSync(
      path.join(extensionDir, "package.json"),
      JSON.stringify({ name: "quaid", dependencies: { "@sinclair/typebox": "^0.32.0" } }),
      "utf8",
    );
    writeSentinel(pluginDir);
    const devOnly = path.join(pluginDir, "node_modules", "vitest");
    fs.mkdirSync(devOnly, { recursive: true });
    fs.writeFileSync(path.join(devOnly, "package.json"), JSON.stringify({ name: "vitest" }), "utf8");

    const spawn = vi.fn(() => ({ status: 0, stdout: "", stderr: "" }));
    const result = ensureOpenClawExtensionDependencies({ extensionDir, pluginDir, spawn });

    expect(result).toEqual({ ok: true, source: "copied" });
    expect(fs.existsSync(path.join(extensionDir, "node_modules", "@sinclair", "typebox", "package.json"))).toBe(true);
    expect(fs.existsSync(path.join(extensionDir, "node_modules", "vitest", "package.json"))).toBe(false);
    expect(spawn).not.toHaveBeenCalled();
  });

  it("falls back to npm install when staged deps are absent", () => {
    const extensionDir = makeTempDir("quaid-oc-ext-");
    const pluginDir = makeTempDir("quaid-oc-plugin-");
    fs.writeFileSync(
      path.join(extensionDir, "package.json"),
      JSON.stringify({ name: "quaid", dependencies: { "@sinclair/typebox": "^0.32.0" } }),
      "utf8",
    );

    const spawn = vi.fn((_cmd: string, _args: string[], opts: any) => {
      writeSentinel(opts.cwd);
      return { status: 0, stdout: "", stderr: "" };
    });
    const result = ensureOpenClawExtensionDependencies({ extensionDir, pluginDir, spawn });

    expect(result).toEqual({ ok: true, source: "installed" });
    expect(fs.existsSync(path.join(extensionDir, "node_modules", "@sinclair", "typebox", "package.json"))).toBe(true);
    expect(spawn).toHaveBeenCalledOnce();
  });
});
