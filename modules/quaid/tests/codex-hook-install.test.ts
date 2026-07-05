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
    fs.writeFileSync(path.join(codexDir, "config.toml"), 'model = "gpt-5.2"\n\n[features]\ncodex_hooks = true\n', "utf8");

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
    const configJson = JSON.parse(fs.readFileSync(path.join(codexDir, "config.json"), "utf8"));
    const configToml = fs.readFileSync(path.join(codexDir, "config.toml"), "utf8");

    const flattenCommands = (eventName: string) =>
      (hooks.hooks[eventName] || []).flatMap((group: any) => (group.hooks || []).map((hook: any) => String(hook.command || "")));

    const sessionStartCommands = flattenCommands("SessionStart");
    const promptCommands = flattenCommands("UserPromptSubmit");
    const stopCommands = flattenCommands("Stop");
    const allManaged = [
      ...sessionStartCommands,
      ...promptCommands,
      ...stopCommands,
    ].filter((cmd) =>
      cmd.includes("hook-session-init")
      || cmd.includes("hook-inject")
      || cmd.includes("hook-codex-stop"),
    );

    expect(promptCommands).toContain("echo keep-me");
    expect(sessionStartCommands.some((cmd) => cmd.includes("hook-session-init"))).toBe(true);
    expect(promptCommands.some((cmd) => cmd.includes("hook-inject"))).toBe(true);
    expect(stopCommands.some((cmd) => cmd.includes("hook-codex-stop"))).toBe(true);
    expect(allManaged).toHaveLength(3);
    expect(allManaged.some((cmd) => cmd.includes("QUAID_INSTANCE"))).toBe(false);
    expect(allManaged.some((cmd) => cmd.includes("QUAID_ADAPTER_TYPE"))).toBe(false);
    expect(allManaged.every((cmd) => cmd.includes('CODEX_PROJECT_DIR="${CODEX_PROJECT_DIR:-$PWD}"'))).toBe(true);
    expect(configJson.features.hooks).toBe(true);
    expect(configJson.features.codex_hooks).toBeUndefined();
    expect(configJson.hooks.SessionStart).toBeTruthy();
    expect(configJson.hooks.UserPromptSubmit).toBeTruthy();
    expect(configJson.hooks.Stop).toBeTruthy();
    expect(
      Object.values(configJson.projects || {}).some((entry: any) => entry?.trust_level === "trusted"),
    ).toBe(true);
    expect(configToml).toContain('model = "gpt-5.2"');
    expect(configToml).toContain("[features]");
    expect(configToml).toContain("hooks = true");
    expect(configToml).not.toContain("codex_hooks");
    expect(configToml).toContain(`[hooks.state."${path.join(codexDir, "hooks.json")}:session_start:0:0"]`);
    expect(configToml).toContain(`[hooks.state."${path.join(codexDir, "hooks.json")}:user_prompt_submit:1:0"]`);
    expect(configToml).toContain(`[hooks.state."${path.join(codexDir, "hooks.json")}:stop:0:0"]`);
    expect(configToml).toMatch(/trusted_hash = "sha256:[a-f0-9]{64}"/);
    expect(configToml).toContain("enabled = true");
    expect(configToml.slice(0, configToml.indexOf("[features]"))).not.toMatch(/^hooks\s*=/m);
    expect(configToml).not.toContain("[[hooks.SessionStart]]");
    expect(configToml).not.toContain("[[hooks.SessionStart.hooks]]");
    expect(configToml).not.toContain("[[hooks.UserPromptSubmit]]");
    expect(configToml).not.toContain("[[hooks.UserPromptSubmit.hooks]]");
    expect(configToml).not.toContain("[[hooks.Stop]]");
    expect(configToml).not.toContain("[[hooks.Stop.hooks]]");
    expect(configToml).not.toContain("hook-session-init");
    expect(configToml).not.toContain("hook-inject");
    expect(configToml).not.toContain("hook-codex-stop");
  });
});
