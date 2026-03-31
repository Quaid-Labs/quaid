import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("codex postinstall hook registration", () => {
  it("writes Codex hooks, preserves unrelated hooks, and enables feature flag", () => {
    const home = makeTempDir("quaid-codex-home-");
    const workspace = makeTempDir("quaid-codex-workspace-");
    const codexDir = path.join(home, ".codex");
    fs.mkdirSync(codexDir, { recursive: true });
    fs.mkdirSync(path.join(workspace, "modules", "quaid"), { recursive: true });
    fs.writeFileSync(path.join(workspace, "modules", "quaid", "quaid"), "#!/bin/sh\nexit 0\n", { mode: 0o755 });

    fs.writeFileSync(
      path.join(codexDir, "hooks.json"),
      JSON.stringify(
        {
          hooks: {
            UserPromptSubmit: [
              { hooks: [{ type: "command", command: "echo keep-me" }] },
              { hooks: [{ type: "command", command: "QUAID_INSTANCE=\"${QUAID_INSTANCE:-old}\" '/old/quaid' hook-inject" }] },
            ],
          },
        },
        null,
        2,
      ) + "\n",
      "utf8",
    );
    fs.writeFileSync(path.join(codexDir, "config.toml"), 'model = "gpt-5.2"\n', "utf8");

    const script = new URL("../adaptors/manifests/codex/hooks/postinstall.mjs", import.meta.url);
    const env = {
      ...process.env,
      HOME: home,
      QUAID_HOME: workspace,
      QUAID_INSTANCE: "codex-livetest",
    };

    const first = spawnSync("node", [script.pathname], { env, encoding: "utf8" });
    expect(first.status).toBe(0);

    const second = spawnSync("node", [script.pathname], { env, encoding: "utf8" });
    expect(second.status).toBe(0);

    const hooks = JSON.parse(fs.readFileSync(path.join(codexDir, "hooks.json"), "utf8"));
    const configToml = fs.readFileSync(path.join(codexDir, "config.toml"), "utf8");

    const flattenCommands = (eventName: string) =>
      (hooks.hooks[eventName] || []).flatMap((group: any) => (group.hooks || []).map((hook: any) => String(hook.command || "")));

    const sessionStartCommands = flattenCommands("SessionStart");
    const promptCommands = flattenCommands("UserPromptSubmit");
    const stopCommands = flattenCommands("Stop");
    const allManaged = [...sessionStartCommands, ...promptCommands, ...stopCommands].filter((cmd) =>
      cmd.includes("hook-session-init") || cmd.includes("hook-inject") || cmd.includes("hook-codex-stop"),
    );

    expect(promptCommands).toContain("echo keep-me");
    expect(sessionStartCommands.some((cmd) => cmd.includes("hook-session-init"))).toBe(true);
    expect(promptCommands.some((cmd) => cmd.includes("hook-inject"))).toBe(true);
    expect(stopCommands.some((cmd) => cmd.includes("hook-codex-stop"))).toBe(true);
    expect(allManaged).toHaveLength(3);
    expect(allManaged.every((cmd) => cmd.includes('codex-livetest'))).toBe(true);
    expect(configToml).toContain('model = "gpt-5.2"');
    expect(configToml).toContain("[features]");
    expect(configToml).toContain("codex_hooks = true");
  });
});
