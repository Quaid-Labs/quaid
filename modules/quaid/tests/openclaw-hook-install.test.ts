import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

function makeTempDir(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("openclaw postinstall hook", () => {
  it("writes exec approvals allowlist for quaid paths", () => {
    const home = makeTempDir("quaid-openclaw-home-");
    const hiddenHome = path.join(home, ".quaid");
    fs.mkdirSync(path.join(home, ".openclaw"), { recursive: true });
    fs.mkdirSync(path.join(hiddenHome, "modules", "quaid"), { recursive: true });

    fs.writeFileSync(
      path.join(home, ".openclaw", "exec-approvals.json"),
      JSON.stringify({
        version: 1,
        agents: {
          "*": {
            allowlist: [{ pattern: "/keep/me" }],
          },
          main: {
            allowlist: [],
          },
        },
      }, null, 2) + "\n",
      "utf8",
    );

    const script = new URL("../adaptors/manifests/openclaw/hooks/postinstall.mjs", import.meta.url);
    const env = {
      ...process.env,
      HOME: home,
      QUAID_HOME: hiddenHome,
      // Keep postinstall deterministic in tests: skip discovering a real
      // host OpenClaw binary by narrowing PATH to core system bins only.
      PATH: "/usr/bin:/bin",
    };

    const res = spawnSync(process.execPath, [script.pathname], { env, encoding: "utf8" });
    expect(res.status).toBe(0);

    const cfg = JSON.parse(fs.readFileSync(path.join(home, ".openclaw", "exec-approvals.json"), "utf8"));
    const allowlist = (cfg.agents?.["*"]?.allowlist || []).map((entry: any) => String(entry.pattern || ""));
    const mainAllowlist = (cfg.agents?.main?.allowlist || []).map((entry: any) => String(entry.pattern || ""));

    expect(allowlist).toContain("/keep/me");
    expect(allowlist).toContain(path.join(home, ".openclaw", "extensions", "quaid", "quaid"));
    expect(allowlist).toContain(path.join(hiddenHome, "modules", "quaid", "quaid"));
    expect(allowlist).toContain(path.join(home, "bin", "quaid"));
    expect(allowlist).toContain(path.join(home, ".local", "bin", "quaid"));
    expect(allowlist).toContain("/usr/local/bin/quaid");
    expect(allowlist).toContain("/opt/homebrew/bin/quaid");
    expect(mainAllowlist).toContain(path.join(home, ".openclaw", "extensions", "quaid", "quaid"));
    expect(mainAllowlist).toContain(path.join(hiddenHome, "modules", "quaid", "quaid"));
  });
});
