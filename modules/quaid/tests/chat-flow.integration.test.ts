import { describe, it, expect } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { SessionTimeoutManager } from "../core/session-timeout.js";

function signalLabelForAction(actionRaw: unknown): "CompactionSignal" | "ResetSignal" | null {
  const action = String(actionRaw || "").trim().toLowerCase().replace(/^\//, "");
  if (action === "compact") return "CompactionSignal";
  if (action === "new") return "ResetSignal";
  if (action === "reset" || action === "restart") return "ResetSignal";
  return null;
}

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

describe("chat flow integration", () => {
  it("extracts new/reset/restart/compact sessions when command evidence is present in source messages", async () => {
    const workspace = makeWorkspace("quaid-chat-flow-");
    const calls: Array<{ sessionId?: string; label?: string; messages: any[] }> = [];
    const sourceMessagesBySession = new Map<string, any[]>();

    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      readSessionMessages: (sid: string) => sourceMessagesBySession.get(sid) || [],
      listSessionActivity: () => [],
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label });
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    });

    const events = [
      { action: "new", commandText: "/new", userText: "Fact one: my sister is Shannon." },
      { action: "reset", commandText: "/reset", userText: "Fact two: my cat is Ziggy." },
      { action: "restart", commandText: "/restart", userText: "Fact three: my uncle is Drew." },
      { action: "compact", commandText: "/compact", userText: "Fact four: my dog is Madu." },
      { action: "unknown", userText: "Ignored command payload." },
    ];

    for (const [idx, item] of events.entries()) {
      const sessionId = `session-chat-${idx + 1}`;
      const { action, userText } = item;
      sourceMessagesBySession.set(sessionId, [
        { role: "user", content: userText, timestamp: Date.now() + idx * 3 },
        { role: "assistant", content: `Acknowledged (${idx + 1}).`, timestamp: Date.now() + idx * 3 + 1 },
        ...(item.commandText
          ? [{ role: "user", content: item.commandText, timestamp: Date.now() + idx * 3 + 2 }]
          : []),
      ]);
      const label = signalLabelForAction(action);
      if (label) {
        const ok = await (manager as any).extractSessionFromSourceDirect(sessionId, label, [], {
          source: "transcript_update",
        });
        expect(ok).toBe(true);
      }
    }

    expect(calls).toHaveLength(4);
    expect(calls.map((c) => c.label)).toEqual([
      "ResetSignal",
      "ResetSignal",
      "ResetSignal",
      "CompactionSignal",
    ]);
    expect(calls.every((c) => c.messages.length >= 2)).toBe(true);
  });

  it("processes command signals when session has pending notes but no transcript delta", async () => {
    const workspace = makeWorkspace("quaid-chat-flow-notes-");
    const calls: Array<{ sessionId?: string; label?: string; messages: any[] }> = [];
    const pendingNotes = new Set<string>(["session-notes-only-1"]);
    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 10,
      readSessionMessages: () => [],
      listSessionActivity: () => [],
      hasPendingSessionNotes: (sid: string) => pendingNotes.has(sid),
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label });
      },
      isBootstrapOnly: () => false,
      logger: () => {},
    });

    const ok = await (manager as any).extractSessionFromSourceDirect("session-notes-only-1", "CompactionSignal", [], {
      source: "transcript_update",
    });

    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.sessionId).toBe("session-notes-only-1");
    expect(calls[0]?.label).toBe("CompactionSignal");
    expect(Array.isArray(calls[0]?.messages)).toBe(true);
    expect(calls[0]?.messages.length).toBe(0);
  });
});
