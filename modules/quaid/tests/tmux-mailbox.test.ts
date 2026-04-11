import { describe, expect, it } from "vitest";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const mailboxScript = path.join(testDir, "livetest", "scripts", "tmux-mailbox.sh");

function runMailbox(args: string[], env: NodeJS.ProcessEnv) {
  return spawnSync("bash", [mailboxScript, ...args], { env, encoding: "utf8" });
}

function readMessages(root: string): any[] {
  const messagesPath = path.join(root, "messages.jsonl");
  return fs
    .readFileSync(messagesPath, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

describe("tmux mailbox", () => {
  it("returns the next queued message after ack and reply", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-mailbox-"));
    const mailboxRoot = path.join(root, "mailbox");
    const tmuxLog = path.join(root, "tmux.log");
    const tmuxMsgScript = path.join(root, "tmux-msg.sh");
    fs.writeFileSync(
      tmuxMsgScript,
      [
        "#!/bin/bash",
        "echo \"$*\" >> \"$TMUX_MAILBOX_TEST_LOG\"",
      ].join("\n") + "\n",
      { encoding: "utf8", mode: 0o755 },
    );

    const env = {
      ...process.env,
      TMUX_MAILBOX_ROOT: mailboxRoot,
      TMUX_MAILBOX_TMUX_MSG_SCRIPT: tmuxMsgScript,
      TMUX_MAILBOX_TEST_LOG: tmuxLog,
      TMUX_MSG_SENDER: "tester",
      TMUX_MSG_SOURCE: "main:3.0",
    };

    const firstPost = runMailbox(["post", "main:4.0", "first mailbox item"], env);
    expect(firstPost.status, firstPost.stderr).toBe(0);
    const secondPost = runMailbox(["post", "main:4.0", "second mailbox item"], env);
    expect(secondPost.status, secondPost.stderr).toBe(0);

    const messages = readMessages(mailboxRoot);
    expect(messages).toHaveLength(2);
    expect(fs.readFileSync(tmuxLog, "utf8").trim().split("\n")).toHaveLength(1);

    const ack = runMailbox(["ack", "main:4.0", messages[0].id], env);
    expect(ack.status, ack.stderr).toBe(0);
    expect(ack.stdout).toContain(`Acknowledged ${messages[0].id}`);
    expect(ack.stdout).toContain("second mailbox item");

    const reply = runMailbox(["reply", "main:4.0", messages[1].id, "handled second"], env);
    expect(reply.status, reply.stderr).toBe(0);
    expect(reply.stdout).toContain(`Acknowledged ${messages[1].id}`);

    const count = runMailbox(["count", "main:4.0"], env);
    expect(count.status, count.stderr).toBe(0);
    expect(count.stdout.trim()).toBe("0");

    fs.rmSync(root, { recursive: true, force: true });
  });
});
