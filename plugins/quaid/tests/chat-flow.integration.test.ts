import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { SessionTimeoutManager } from "../core/session-timeout.js";
import { extractCommandName, signalLabelForCommand } from "../adapters/openclaw/command-signals.js";

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("chat flow integration", () => {
  it("routes new/reset/restart/compact commands into extraction signals", async () => {
    const workspace = makeWorkspace("quaid-chat-flow-");
    const calls: Array<{ sessionId?: string; label?: string; messages: any[] }> = [];

    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label });
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    });

    const sessionId = "session-chat-1";
    manager.onAgentEnd(
      [
        { role: "user", content: "My sibling is Shannon." },
        { role: "assistant", content: "Got it." },
      ],
      sessionId,
    );

    const events = [
      { event: { command: "new" }, userText: "Fact one: my sister is Shannon." },
      { event: { name: "/reset" }, userText: "Fact two: my cat is Ziggy." },
      { event: { text: "/restart now" }, userText: "Fact three: my uncle is Drew." },
      { event: { input: "/compact" }, userText: "Fact four: my dog is Madu." },
      { event: { text: "/unknown" }, userText: "Ignored command payload." },
    ];

    for (const [idx, item] of events.entries()) {
      const { event, userText } = item;
      manager.onAgentEnd(
        [
          { role: "user", content: userText },
          { role: "assistant", content: `Acknowledged (${idx + 1}).` },
        ],
        sessionId,
      );
      const cmd = extractCommandName(event);
      const label = signalLabelForCommand(cmd);
      if (label) {
        manager.queueExtractionSignal(sessionId, label);
        await manager.processPendingExtractionSignals();
      }
    }

    expect(calls).toHaveLength(4);
    expect(calls.map((c) => c.label)).toEqual([
      "NewSignal",
      "ResetSignal",
      "ResetSignal",
      "CompactionSignal",
    ]);
    expect(calls.every((c) => c.sessionId === sessionId)).toBe(true);
    expect(calls.every((c) => c.messages.length >= 2)).toBe(true);
  });
});
