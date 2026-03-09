import { afterEach, describe, expect, it, vi } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { SessionTimeoutManager } from "../core/session-timeout";

function makeWorkspace(prefix: string): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

type SourceState = {
  messagesBySession: Map<string, any[]>;
  activityBySession: Map<string, number>;
};

function createSourceState(): SourceState {
  return {
    messagesBySession: new Map<string, any[]>(),
    activityBySession: new Map<string, number>(),
  };
}

function buildManager(params: {
  workspace: string;
  timeoutMinutes: number;
  source: SourceState;
  failHardEnabled?: boolean;
  extract?: (messages: any[], sessionId?: string, label?: string) => Promise<void>;
}) {
  return new SessionTimeoutManager({
    workspace: params.workspace,
    timeoutMinutes: params.timeoutMinutes,
    failHardEnabled: params.failHardEnabled,
    isBootstrapOnly: () => false,
    logger: () => {},
    readSessionMessages: (sessionId: string) => params.source.messagesBySession.get(sessionId) || [],
    listSessionActivity: () =>
      Array.from(params.source.activityBySession.entries()).map(([sessionId, lastActivityMs]) => ({
        sessionId,
        lastActivityMs,
      })),
    extract:
      params.extract ||
      (async () => {
        // no-op
      }),
  });
}

describe("SessionTimeoutManager (cursor + source)", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  it("clears an active timeout buffer when agent start fires", () => {
    vi.useFakeTimers();
    const workspace = makeWorkspace("quaid-timeout-agent-start-");
    const source = createSourceState();
    const manager = buildManager({ workspace, timeoutMinutes: 10, source });

    manager.onAgentEnd(
      [
        { role: "user", content: "remember this", timestamp: Date.now() },
        { role: "assistant", content: "ok", timestamp: Date.now() + 1 },
      ],
      "session-agent-start",
    );

    expect((manager as any).timer).toBeTruthy();
    manager.onAgentStart();
    expect((manager as any).timer).toBeNull();
  });

  it("requires explicit lifecycle evidence before transcript-update reset extraction", async () => {
    const workspace = makeWorkspace("quaid-timeout-signal-");
    const source = createSourceState();
    source.messagesBySession.set("session-1", [
      { id: "u1", role: "user", content: "remember this", timestamp: Date.now() },
      { id: "a1", role: "assistant", content: "ok", timestamp: Date.now() + 1 },
    ]);

    const manager = buildManager({ workspace, timeoutMinutes: 10, source });
    const ok = await (manager as any).extractSessionFromSourceDirect("session-1", "ResetSignal", [], {
      source: "transcript_update",
    });
    expect(ok).toBe(false);
  });

  it("extracts from source session messages and writes cursor", async () => {
    const workspace = makeWorkspace("quaid-timeout-source-extract-");
    const source = createSourceState();
    source.messagesBySession.set("session-2", [
      { id: "u1", role: "user", content: "fact", timestamp: new Date().toISOString() },
      { id: "a1", role: "assistant", content: "ack", timestamp: new Date().toISOString() },
    ]);

    const calls: Array<{ sessionId?: string; label?: string; messages: any[] }> = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 10,
      source,
      extract: async (messages, sessionId, label) => {
        calls.push({ messages, sessionId, label });
      },
    });

    const ok = await manager.extractSessionFromLog("session-2", "Reset");
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.sessionId).toBe("session-2");

    const second = await manager.extractSessionFromLog("session-2", "Reset");
    expect(second).toBe(false);
  });

  it("blocks fallback payload when failHard=true and source has no messages", async () => {
    const workspace = makeWorkspace("quaid-timeout-failhard-block-");
    const source = createSourceState();

    const manager = buildManager({ workspace, timeoutMinutes: 10, source, failHardEnabled: true });
    await expect(
      manager.extractSessionFromLog("session-failhard", "Reset", [
        { role: "user", content: "remember this", timestamp: Date.now() },
      ]),
    ).rejects.toThrow(/fallback payload blocked by failHard/i);
  });

  it("allows fallback payload when failHard=false", async () => {
    const workspace = makeWorkspace("quaid-timeout-soft-fallback-");
    const source = createSourceState();

    const calls: Array<any[]> = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 10,
      source,
      failHardEnabled: false,
      extract: async (messages) => {
        calls.push(messages);
      },
    });

    const ok = await manager.extractSessionFromLog("session-soft", "Reset", [
      { role: "user", content: "remember this", timestamp: Date.now() },
    ]);

    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toHaveLength(1);
  });

  it("recovers only sessions that became stale within the current sweep window", async () => {
    const workspace = makeWorkspace("quaid-timeout-stale-window-");
    const source = createSourceState();
    const now = Date.now();

    source.activityBySession.set("session-window-hit", now - 61 * 60 * 1000);
    source.activityBySession.set("session-window-miss", now - 70 * 60 * 1000);

    source.messagesBySession.set("session-window-hit", [
      { id: "u1", role: "user", content: "hit", timestamp: now - 61 * 60 * 1000 },
    ]);
    source.messagesBySession.set("session-window-miss", [
      { id: "u2", role: "user", content: "miss", timestamp: now - 70 * 60 * 1000 },
    ]);

    const calls: string[] = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async (_messages, sessionId) => {
        calls.push(String(sessionId));
      },
    });

    const staleStatePath = path.join(workspace, "data", "stale-sweep-state.json");
    fs.mkdirSync(path.dirname(staleStatePath), { recursive: true });
    fs.writeFileSync(
      staleStatePath,
      JSON.stringify({ lastSweepAt: new Date(now - 5 * 60 * 1000).toISOString(), retries: {} }),
      "utf8",
    );

    await manager.recoverStaleBuffers();

    expect(calls).toEqual(["session-window-hit"]);
  });

  it("uses installedAt lower bound on first stale sweep to include older post-install sessions", async () => {
    const workspace = makeWorkspace("quaid-timeout-stale-installed-bound-");
    const source = createSourceState();
    const now = Date.now();
    const installedAtMs = now - 6 * 60 * 60 * 1000;

    source.activityBySession.set("session-post-install", now - 5 * 60 * 60 * 1000);
    source.activityBySession.set("session-pre-install", installedAtMs - 2 * 60 * 60 * 1000);
    source.messagesBySession.set("session-post-install", [
      { id: "u1", role: "user", content: "include me", timestamp: now - 5 * 60 * 60 * 1000 },
    ]);
    source.messagesBySession.set("session-pre-install", [
      { id: "u2", role: "user", content: "skip me", timestamp: installedAtMs - 2 * 60 * 60 * 1000 },
    ]);

    const installStatePath = path.join(workspace, "data", "installed-at.json");
    fs.mkdirSync(path.dirname(installStatePath), { recursive: true });
    fs.writeFileSync(installStatePath, JSON.stringify({ installedAt: new Date(installedAtMs).toISOString() }), "utf8");

    const calls: string[] = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async (_messages, sessionId) => {
        calls.push(String(sessionId));
      },
    });

    await manager.recoverStaleBuffers();
    expect(calls).toEqual(["session-post-install"]);
  });

  it("records stale recovery backoff when extraction fails", async () => {
    const workspace = makeWorkspace("quaid-timeout-stale-backoff-");
    const source = createSourceState();
    const now = Date.now();

    source.activityBySession.set("session-stale", now - 61 * 60 * 1000);
    source.messagesBySession.set("session-stale", [
      { id: "u1", role: "user", content: "retry me", timestamp: now - 61 * 60 * 1000 },
    ]);

    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async () => {
        throw new Error("gateway timeout after 10000ms");
      },
    });
    ;(manager as any).failHard = false;

    await manager.recoverStaleBuffers();

    const staleStatePath = path.join(workspace, "data", "stale-sweep-state.json");
    const state = JSON.parse(fs.readFileSync(staleStatePath, "utf8"));
    const retry = state?.retries?.["session-stale"];
    expect(retry).toBeTruthy();
    expect(retry.attemptCount).toBe(1);
    expect(String(retry.lastError || "")).toContain("gateway timeout");
  });

  it("retries stale recovery when retry is due even if outside stale window", async () => {
    const workspace = makeWorkspace("quaid-timeout-stale-retry-due-");
    const source = createSourceState();
    const now = Date.now();

    source.activityBySession.set("session-old", now - 5 * 60 * 60 * 1000);
    source.messagesBySession.set("session-old", [
      { id: "u1", role: "user", content: "old but retry", timestamp: now - 5 * 60 * 60 * 1000 },
    ]);

    const calls: string[] = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async (_messages, sessionId) => {
        calls.push(String(sessionId));
      },
    });

    const staleStatePath = path.join(workspace, "data", "stale-sweep-state.json");
    fs.mkdirSync(path.dirname(staleStatePath), { recursive: true });
    fs.writeFileSync(
      staleStatePath,
      JSON.stringify({
        lastSweepAt: new Date(now).toISOString(),
        retries: {
          "session-old": {
            sessionId: "session-old",
            lastActivityMs: now - 5 * 60 * 60 * 1000,
            attemptCount: 2,
            nextRecoveryAt: new Date(now - 1_000).toISOString(),
          },
        },
      }),
      "utf8",
    );

    await manager.recoverStaleBuffers();

    expect(calls).toEqual(["session-old"]);
    const state = JSON.parse(fs.readFileSync(staleStatePath, "utf8"));
    expect(state?.retries?.["session-old"]).toBeUndefined();
  });

  it("drops retry entry when session has newer activity than failed stale snapshot", async () => {
    const workspace = makeWorkspace("quaid-timeout-stale-retry-drop-");
    const source = createSourceState();
    const now = Date.now();

    source.activityBySession.set("session-changed", now - 10 * 60 * 1000);
    source.messagesBySession.set("session-changed", [
      { id: "u1", role: "user", content: "newer", timestamp: now - 10 * 60 * 1000 },
    ]);

    const manager = buildManager({ workspace, timeoutMinutes: 60, source });

    const staleStatePath = path.join(workspace, "data", "stale-sweep-state.json");
    fs.mkdirSync(path.dirname(staleStatePath), { recursive: true });
    fs.writeFileSync(
      staleStatePath,
      JSON.stringify({
        lastSweepAt: new Date(now).toISOString(),
        retries: {
          "session-changed": {
            sessionId: "session-changed",
            lastActivityMs: now - 5 * 60 * 60 * 1000,
            attemptCount: 2,
            nextRecoveryAt: new Date(now - 1_000).toISOString(),
          },
        },
      }),
      "utf8",
    );

    await manager.recoverStaleBuffers();

    const state = JSON.parse(fs.readFileSync(staleStatePath, "utf8"));
    expect(state?.retries?.["session-changed"]).toBeUndefined();
  });

  it("runs timeout extraction by reading source on timer fire", async () => {
    vi.useFakeTimers();
    const workspace = makeWorkspace("quaid-timeout-timer-source-");
    const source = createSourceState();
    const now = Date.now();

    source.messagesBySession.set("session-timer", [
      { id: "u1", role: "user", content: "via timer", timestamp: now },
      { id: "a1", role: "assistant", content: "ok", timestamp: now + 1 },
    ]);

    const calls: Array<{ sid?: string; label?: string }> = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 1,
      source,
      extract: async (_messages, sid, label) => {
        calls.push({ sid, label });
      },
    });

    manager.onAgentEnd([
      { role: "user", content: "via timer", timestamp: now },
      { role: "assistant", content: "ok", timestamp: now + 1 },
    ], "session-timer");

    await vi.advanceTimersByTimeAsync(61_000);
    await (manager as any).chain;

    expect(calls.length).toBe(1);
    expect(calls[0]?.sid).toBe("session-timer");
    expect(calls[0]?.label).toBe("Timeout");
  });

  it("allows transcript-update lifecycle extraction when transcript contains command evidence", async () => {
    const workspace = makeWorkspace("quaid-timeout-lifecycle-evidence-");
    const source = createSourceState();
    source.messagesBySession.set("session-orphan", [
      { id: "u1", role: "user", content: "recover me", timestamp: Date.now() - 5000 },
      { id: "u2", role: "user", content: "/new", timestamp: Date.now() - 4000 },
    ]);

    const calls: Array<{ sid?: string; label?: string }> = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async (_messages, sid, label) => {
        calls.push({ sid, label });
      },
    });

    const ok = await (manager as any).extractSessionFromSourceDirect("session-orphan", "ResetSignal", [], {
      source: "transcript_update",
    });

    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.sid).toBe("session-orphan");
    expect(calls[0]?.label).toBe("ResetSignal");
  });

  it("extracts notes-only sessions without transcript messages", async () => {
    const workspace = makeWorkspace("quaid-timeout-notes-only-");
    const source = createSourceState();
    const pendingNotes = new Set<string>(["session-drop"]);
    const calls: Array<{ sid?: string; label?: string; messages: any[] }> = [];

    const manager = new SessionTimeoutManager({
      workspace,
      timeoutMinutes: 60,
      isBootstrapOnly: () => false,
      logger: () => {},
      readSessionMessages: () => [],
      listSessionActivity: () => [],
      hasPendingSessionNotes: (sid: string) => pendingNotes.has(sid),
      extract: async (messages, sid, label) => {
        calls.push({ sid, label, messages });
      },
    });

    const ok = await (manager as any).extractSessionFromSourceDirect("session-drop", "CompactionSignal", [], {
      source: "transcript_update",
    });
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.sid).toBe("session-drop");
    expect(calls[0]?.label).toBe("CompactionSignal");
    expect(calls[0]?.messages).toEqual([]);
  });

  it("filters internal system maintenance traffic from source extraction payload", async () => {
    const workspace = makeWorkspace("quaid-timeout-filter-system-");
    const source = createSourceState();
    source.messagesBySession.set("session-filter", [
      { id: "s1", role: "system", content: "Compacted (37k → 5k)", timestamp: Date.now() - 5 },
      {
        id: "u1",
        role: "user",
        content: "Extract memorable facts and journal entries from this conversation",
        timestamp: Date.now() - 4,
      },
      { id: "a1", role: "assistant", content: "{\"facts\":[],\"journal_entries\":[],\"soul_snippets\":[]}", timestamp: Date.now() - 3 },
      { id: "u2", role: "user", content: "real user context", timestamp: Date.now() - 2 },
      { id: "a2", role: "assistant", content: "real assistant context", timestamp: Date.now() - 1 },
    ]);

    const calls: any[][] = [];
    const manager = buildManager({
      workspace,
      timeoutMinutes: 60,
      source,
      extract: async (messages) => {
        calls.push(messages);
      },
    });

    const ok = await manager.extractSessionFromLog("session-filter", "Reset");
    expect(ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.map((m) => m.id)).toEqual(["u2", "a2"]);
  });

  it("persists installedAt marker for future bounded migration logic", async () => {
    const workspace = makeWorkspace("quaid-timeout-installed-at-");
    const source = createSourceState();
    const manager = buildManager({ workspace, timeoutMinutes: 60, source });

    await manager.recoverStaleBuffers();

    const installStatePath = path.join(workspace, "data", "installed-at.json");
    const staleStatePath = path.join(workspace, "data", "stale-sweep-state.json");

    expect(fs.existsSync(installStatePath)).toBe(true);
    expect(fs.existsSync(staleStatePath)).toBe(true);

    const installState = JSON.parse(fs.readFileSync(installStatePath, "utf8"));
    const staleState = JSON.parse(fs.readFileSync(staleStatePath, "utf8"));

    expect(typeof installState?.installedAt).toBe("string");
    expect(staleState?.installedAt).toBe(installState?.installedAt);
  });
});
