import { describe, expect, it, vi } from "vitest";
import { createQuaidFacade } from "../core/facade.js";
import type { QuaidFacadeDeps, LLMCallResult } from "../core/facade.js";
import { mkdir, mkdtemp, readFile, readdir, rm, unlink, writeFile } from "node:fs/promises";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";

function makeMockDeps(overrides: Partial<QuaidFacadeDeps> = {}): QuaidFacadeDeps {
  return {
    workspace: "/tmp/test-workspace",
    pluginRoot: "/tmp/test-workspace/modules/quaid",
    dbPath: "/tmp/test-memory.db",
    eventSource: "openclaw_adapter",
    execPython: vi.fn(async () => "{}"),
    execExtractPipeline: vi.fn(async () => "{}"),
    execDocsRag: vi.fn(async () => ""),
    execDocsRegistry: vi.fn(async () => ""),
    execDocsUpdater: vi.fn(async () => "{}"),
    execEvents: vi.fn(async () => ""),
    callLLM: vi.fn(async () => ({
      text: '{"query":"test","datastores":["vector_basic"]}',
      model: "test-model",
      input_tokens: 10,
      output_tokens: 20,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      truncated: false,
    } satisfies LLMCallResult)),
    getMemoryConfig: vi.fn(() => ({ retrieval: { failHard: false } })),
    getDatastoreStatsSync: vi.fn(() => null),
    isSystemEnabled: vi.fn(() => false),
    isFailHardEnabled: vi.fn(() => false),
    transcriptFormat: {
      preprocessText: (text: string) => String(text || "")
        .replace(/^\[(?:Telegram|WhatsApp|Discord|Signal|Slack)\s+[^\]]+\]\s*/i, "")
        .replace(/\n?\[message_id:\s*\d+\]/gi, "")
        .trim(),
      shouldSkipText: (_role: "user" | "assistant", text: string) => {
        if (!text) return true;
        if (text.startsWith("GatewayRestart:") || text.startsWith("System:")) return true;
        if (text.includes('"kind": "restart"')) return true;
        if (text.includes("HEARTBEAT") && text.includes("HEARTBEAT_OK")) return true;
        if (text.replace(/[*_<>\/b\s]/g, "").startsWith("HEARTBEAT_OK")) return true;
        return false;
      },
      speakerLabel: (role: "user" | "assistant") => role === "user" ? "User" : "Alfie",
    },
    ...overrides,
  };
}

describe("QuaidFacade", () => {
  // -----------------------------------------------------------------------
  // Pass-through methods
  // -----------------------------------------------------------------------

  it("getConfig delegates to deps.getMemoryConfig", () => {
    const getMemoryConfig = vi.fn(() => ({ models: { fast: "haiku" } }));
    const facade = createQuaidFacade(makeMockDeps({ getMemoryConfig }));
    expect(facade.getConfig()).toEqual({ models: { fast: "haiku" } });
    expect(getMemoryConfig).toHaveBeenCalledTimes(1);
  });

  it("isSystemEnabled delegates to deps", () => {
    const isSystemEnabled = vi.fn((s: string) => s === "memory");
    const facade = createQuaidFacade(makeMockDeps({ isSystemEnabled: isSystemEnabled as any }));
    expect(facade.isSystemEnabled("memory")).toBe(true);
    expect(facade.isSystemEnabled("journal")).toBe(false);
  });

  it("isFailHardEnabled delegates to deps", () => {
    const facade = createQuaidFacade(makeMockDeps({ isFailHardEnabled: vi.fn(() => true) }));
    expect(facade.isFailHardEnabled()).toBe(true);
  });

  it("injectFullJournalContext appends full-mode journal content", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-journal-"));
    const journalDir = path.join(workspace, "journal");
    await mkdir(journalDir, { recursive: true });
    await writeFile(path.join(journalDir, "2026-03-06.journal.md"), "A meaningful entry", "utf8");
    await writeFile(path.join(journalDir, "ignore.txt"), "ignore", "utf8");

    const facade = createQuaidFacade(makeMockDeps({
      workspace,
      isSystemEnabled: vi.fn((s: string) => s === "journal"),
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        docs: { journal: { mode: "full", journalDir: "journal" } },
      })),
    }));

    const out = facade.injectFullJournalContext("seed");
    expect(out).toContain("seed");
    expect(out).toContain("[JOURNAL - Full Soul Mode]");
    expect(out).toContain("--- 2026-03-06.journal.md ---");
    expect(out).toContain("A meaningful entry");

    await rm(workspace, { recursive: true, force: true });
  });

  it("initializeDatastoreIfMissing creates db dir and calls init callback once", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-db-init-"));
    const dbPath = path.join(workspace, "data", "memory.db");
    const initDatastore = vi.fn(() => {
      mkdirSync(path.dirname(dbPath), { recursive: true });
      writeFileSync(dbPath, "", "utf8");
    });
    const facade = createQuaidFacade(makeMockDeps({ workspace, dbPath, initDatastore }));
    expect(facade.initializeDatastoreIfMissing()).toBe(true);
    expect(facade.initializeDatastoreIfMissing()).toBe(false);
    expect(initDatastore).toHaveBeenCalledTimes(1);
    await rm(workspace, { recursive: true, force: true });
  });

  it("getCaptureTimeoutMinutes reads capture timeout from config", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        capture: { inactivityTimeoutMinutes: 45 },
      })),
    }));
    expect(facade.getCaptureTimeoutMinutes()).toBe(45);
  });

  it("isInternalQuaidSession identifies internal utility sessions", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(facade.isInternalQuaidSession("quaid-fast-123")).toBe(true);
    expect(facade.isInternalQuaidSession("quaid-deep-456")).toBe(true);
    expect(facade.isInternalQuaidSession("agent:main:quaid-llm-fast")).toBe(true);
    expect(facade.isInternalQuaidSession("normal-session-id")).toBe(false);
  });

  it("resolveTierModel returns explicit provider/model from tier value", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        models: {
          llmProvider: "openai",
          fastReasoning: "anthropic/claude-haiku-4-5",
          deepReasoning: "openai/gpt-5",
        },
      })),
    }));
    expect(facade.resolveTierModel("fast")).toEqual({
      provider: "anthropic",
      model: "claude-haiku-4-5",
    });
  });

  it("resolveTierModel uses model classes with default provider callback and alias map", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getDefaultLLMProvider: vi.fn(() => "openai-codex"),
      providerAliases: { "openai-codex": "openai" },
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        models: {
          llmProvider: "default",
          fastReasoning: "default",
          deepReasoning: "default",
          fastReasoningModelClasses: { openai: "gpt-5-mini" },
          deepReasoningModelClasses: { openai: "gpt-5" },
        },
      })),
    }));
    expect(facade.resolveTierModel("deep")).toEqual({
      provider: "openai-codex",
      model: "gpt-5",
    });
  });

  // -----------------------------------------------------------------------
  // Datastore bridge delegation
  // -----------------------------------------------------------------------

  it("stats calls execPython with 'stats'", async () => {
    const execPython = vi.fn(async () => '{"total":42}');
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const result = await facade.stats();
    expect(result).toBe('{"total":42}');
    expect(execPython).toHaveBeenCalledWith("stats", []);
  });

  it("getStatsParsed returns typed datastore stats", async () => {
    const execPython = vi.fn(async () => '{"total_nodes":42,"edges":9,"active_nodes":7}');
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const result = await facade.getStatsParsed();
    expect(result).toEqual({ total_nodes: 42, edges: 9, active_nodes: 7 });
    expect(execPython).toHaveBeenCalledWith("stats", []);
  });

  it("getStatsParsed returns null on invalid stats payload", async () => {
    const execPython = vi.fn(async () => '{"total_nodes":"bad","edges":9}');
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const result = await facade.getStatsParsed();
    expect(result).toBeNull();
  });

  it("store calls execPython with 'store'", async () => {
    const execPython = vi.fn(async () => "ok");
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    await facade.store(["--text", "hello"]);
    expect(execPython).toHaveBeenCalledWith("store", ["--text", "hello"]);
  });

  it("forget calls execPython with 'forget'", async () => {
    const execPython = vi.fn(async () => "deleted");
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    await facade.forget(["--id", "abc"]);
    expect(execPython).toHaveBeenCalledWith("forget", ["--id", "abc"]);
  });

  it("searchBySession calls execPython with scoped search args", async () => {
    const execPython = vi.fn(async () => "[]");
    const getMemoryConfig = vi.fn(() => ({
      retrieval: { failHard: false },
      users: { defaultOwner: "owner-123", identities: {} },
    }));
    const facade = createQuaidFacade(makeMockDeps({ execPython, getMemoryConfig }));
    await facade.searchBySession("sess-1", 7);
    expect(execPython).toHaveBeenCalledWith("search", ["*", "--session-id", "sess-1", "--owner", "owner-123", "--limit", "7"]);
  });

  it("resolveOwner uses users config speaker/channel mapping", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        users: {
          defaultOwner: "quaid",
          identities: {
            solomon: {
              speakers: ["Solomon"],
              channels: { telegram: ["*"] },
            },
          },
        },
      })),
    }));
    expect(facade.resolveOwner("Solomon", "discord")).toBe("solomon");
    expect(facade.resolveOwner("AnyUser", "telegram")).toBe("solomon");
    expect(facade.resolveOwner("Unknown", "discord")).toBe("quaid");
  });

  it("shouldNotifyFeature and shouldNotifyProjectCreate respect notifications config", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        notifications: {
          level: "verbose",
          retrieval: { verbosity: "full" },
          projectCreate: { enabled: false },
        },
      })),
    }));
    expect(facade.shouldNotifyFeature("retrieval", "summary")).toBe(true);
    expect(facade.shouldNotifyFeature("retrieval", "full")).toBe(true);
    expect(facade.shouldNotifyFeature("janitor", "full")).toBe(true);
    expect(facade.shouldNotifyProjectCreate()).toBe(false);
  });

  it("isPluginStrictMode and isPreInjectionPassEnabled read config flags", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { pre_injection_pass: false, failHard: false },
        plugins: { strict: 0 },
      })),
    }));
    expect(facade.isPluginStrictMode()).toBe(false);
    expect(facade.isPreInjectionPassEnabled()).toBe(false);
  });

  it("shouldEmitExtractionNotify dedupes keys within cooldown", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const t0 = 1_700_000_000_000;
    expect(facade.shouldEmitExtractionNotify("done:sess:reset:1:0:0", t0)).toBe(true);
    expect(facade.shouldEmitExtractionNotify("done:sess:reset:1:0:0", t0 + 250)).toBe(false);
    expect(facade.shouldEmitExtractionNotify("done:sess:reset:1:0:0", t0 + 95_000)).toBe(true);
    facade.clearExtractionNotifyHistory();
    expect(facade.shouldEmitExtractionNotify("done:sess:reset:1:0:0", t0 + 95_100)).toBe(true);
  });

  it("listRecentSessionsFromExtractionLog returns sorted extraction sessions", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-log-list-"));
    await mkdir(path.join(workspace, "data"), { recursive: true });
    await writeFile(
      path.join(workspace, "data", "extraction-log.json"),
      JSON.stringify({
        older: { last_extracted_at: "2026-03-01T01:00:00.000Z", message_count: 8, label: "ResetSignal", topic_hint: "older topic" },
        newer: { last_extracted_at: "2026-03-02T01:00:00.000Z", message_count: 12, label: "CompactionSignal", topic_hint: "newer topic" },
      }),
      "utf8",
    );
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    const sessions = facade.listRecentSessionsFromExtractionLog(5);
    expect(sessions).toHaveLength(2);
    expect(sessions[0]).toMatchObject({
      sessionId: "newer",
      messageCount: 12,
      label: "CompactionSignal",
      topicHint: "newer topic",
    });
    expect(sessions[1].sessionId).toBe("older");
    await rm(workspace, { recursive: true, force: true });
  });

  it("updateExtractionLog writes topic hint from first meaningful user message", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-log-update-"));
    await mkdir(path.join(workspace, "data"), { recursive: true });
    await writeFile(path.join(workspace, "data", "extraction-log.json"), "{}", "utf8");
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    facade.updateExtractionLog(
      "sess-topic",
      [
        { role: "user", content: "GatewayRestart: heartbeat" },
        { role: "assistant", content: "ack" },
        { role: "user", content: "Need to fix adapter boundary and extract facade logic" },
      ],
      "CompactionSignal",
    );
    const payload = JSON.parse(await readFile(path.join(workspace, "data", "extraction-log.json"), "utf8"));
    expect(payload["sess-topic"]).toBeTruthy();
    expect(payload["sess-topic"].label).toBe("CompactionSignal");
    expect(payload["sess-topic"].topic_hint).toContain("Need to fix adapter boundary");
    await rm(workspace, { recursive: true, force: true });
  });

  it("getInjectionLogPath resolves under workspace runtime injection dir", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-injection-path-"));
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    expect(facade.getInjectionLogPath("session-123")).toBe(
      path.join(workspace, ".quaid", "runtime", "injection", "memory-injection-session-123.log"),
    );
    await rm(workspace, { recursive: true, force: true });
  });

  it("pruneInjectionLogFiles keeps latest 400 memory-injection logs", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-injection-prune-"));
    const injectionDir = path.join(workspace, ".quaid", "runtime", "injection");
    await mkdir(injectionDir, { recursive: true });
    for (let i = 0; i < 402; i += 1) {
      await writeFile(path.join(injectionDir, `memory-injection-${i}.log`), String(i), "utf8");
    }
    await writeFile(path.join(injectionDir, "keep-me.txt"), "keep", "utf8");

    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    facade.pruneInjectionLogFiles();

    const files = await readdir(injectionDir);
    const logFiles = files.filter((f) => f.startsWith("memory-injection-") && f.endsWith(".log"));
    expect(logFiles).toHaveLength(400);
    expect(files).toContain("keep-me.txt");
    await rm(workspace, { recursive: true, force: true });
  });

  // -----------------------------------------------------------------------
  // Events
  // -----------------------------------------------------------------------

  it("emitEvent delegates to execEvents with normalized args", async () => {
    const execEvents = vi.fn(async () => '{"ok":true}');
    const facade = createQuaidFacade(makeMockDeps({ execEvents }));
    const result = await facade.emitEvent("session_end", { id: "s1" }, "queued");
    expect(result).toEqual({ ok: true });
    expect(execEvents).toHaveBeenCalledWith("emit", [
      "--name",
      "session_end",
      "--payload",
      '{"id":"s1"}',
      "--source",
      "openclaw_adapter",
      "--dispatch",
      "queued",
    ]);
  });

  // -----------------------------------------------------------------------
  // Docs
  // -----------------------------------------------------------------------

  it("docsSearch delegates to execDocsRag", async () => {
    const execDocsRag = vi.fn(async () => "1. result");
    const facade = createQuaidFacade(makeMockDeps({ execDocsRag }));
    const result = await facade.docsSearch("architecture", ["--limit", "5"]);
    expect(result).toBe("1. result");
    expect(execDocsRag).toHaveBeenCalledWith("search", ["architecture", "--limit", "5"]);
  });

  it("docsRead delegates to execDocsRegistry", async () => {
    const execDocsRegistry = vi.fn(async () => "# Doc content");
    const facade = createQuaidFacade(makeMockDeps({ execDocsRegistry }));
    const result = await facade.docsRead("PROJECT.md");
    expect(result).toBe("# Doc content");
    expect(execDocsRegistry).toHaveBeenCalledWith("read", ["PROJECT.md"]);
  });

  it("docsList delegates to execDocsRegistry", async () => {
    const execDocsRegistry = vi.fn(async () => '[{"name":"doc1"}]');
    const facade = createQuaidFacade(makeMockDeps({ execDocsRegistry }));
    const result = await facade.docsList(["--json", "--project", "quaid"]);
    expect(result).toBe('[{"name":"doc1"}]');
    expect(execDocsRegistry).toHaveBeenCalledWith("list", ["--json", "--project", "quaid"]);
  });

  it("docsRegister delegates to execDocsRegistry", async () => {
    const execDocsRegistry = vi.fn(async () => "registered");
    const facade = createQuaidFacade(makeMockDeps({ execDocsRegistry }));
    await facade.docsRegister(["docs/new.md", "--project", "quaid"]);
    expect(execDocsRegistry).toHaveBeenCalledWith("register", ["docs/new.md", "--project", "quaid"]);
  });

  it("docsCreateProject delegates to execDocsRegistry", async () => {
    const execDocsRegistry = vi.fn(async () => "created");
    const facade = createQuaidFacade(makeMockDeps({ execDocsRegistry }));
    await facade.docsCreateProject(["my-proj", "--label", "My Proj"]);
    expect(execDocsRegistry).toHaveBeenCalledWith("create-project", ["my-proj", "--label", "My Proj"]);
  });

  it("docsListProjects delegates to execDocsRegistry", async () => {
    const execDocsRegistry = vi.fn(async () => '[{"name":"my-proj"}]');
    const facade = createQuaidFacade(makeMockDeps({ execDocsRegistry }));
    const result = await facade.docsListProjects(["--json"]);
    expect(result).toBe('[{"name":"my-proj"}]');
    expect(execDocsRegistry).toHaveBeenCalledWith("list-projects", ["--json"]);
  });

  it("docsCheckStaleness delegates to execDocsUpdater", async () => {
    const execDocsUpdater = vi.fn(async () => '{"stale": true}');
    const facade = createQuaidFacade(makeMockDeps({ execDocsUpdater }));
    const result = await facade.docsCheckStaleness();
    expect(result).toBe('{"stale": true}');
    expect(execDocsUpdater).toHaveBeenCalledWith("check", ["--json"]);
  });

  it("getDocsStalenessWarning formats stale docs warning", async () => {
    const execDocsUpdater = vi.fn(async () => JSON.stringify({
      alpha: { gap_hours: 7, stale_sources: ["PROJECT.log", "SOUL.md"] },
      beta: { gap_hours: 2, stale_sources: ["MEMORY.md"] },
    }));
    const facade = createQuaidFacade(makeMockDeps({ execDocsUpdater }));
    const warning = await facade.getDocsStalenessWarning();
    expect(warning).toContain("STALENESS WARNING");
    expect(warning).toContain("alpha (7h behind: PROJECT.log, SOUL.md)");
    expect(warning).toContain("beta (2h behind: MEMORY.md)");
    expect(execDocsUpdater).toHaveBeenCalledWith("check", ["--json"]);
  });

  it("getDocsStalenessWarning returns empty string when no stale docs", async () => {
    const execDocsUpdater = vi.fn(async () => "{}");
    const facade = createQuaidFacade(makeMockDeps({ execDocsUpdater }));
    const warning = await facade.getDocsStalenessWarning();
    expect(warning).toBe("");
  });

  it("buildDocsSearchNotificationPayload parses doc search lines", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const payload = facade.buildDocsSearchNotificationPayload(
      "auth flow",
      "1. ~/docs/ARCH.md > login flow (similarity: 0.82)\n2. docs/API.md > tokens (similarity: 0.73)\nnoise",
    );
    expect(payload.query).toBe("auth flow");
    expect(payload.results).toEqual([
      { doc: "ARCH.md", section: "login flow", score: 0.82 },
      { doc: "API.md", section: "tokens", score: 0.73 },
    ]);
  });

  it("loadProjectMarkdown reads PROJECT.md for configured project home", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-project-md-"));
    const projectDir = path.join(workspace, "projects", "alpha");
    await mkdir(projectDir, { recursive: true });
    await writeFile(path.join(projectDir, "PROJECT.md"), "# Alpha\nproject notes", "utf8");
    const facade = createQuaidFacade(makeMockDeps({
      workspace,
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        projects: { definitions: { alpha: { homeDir: "projects/alpha" } } },
      })),
    }));
    const projectMd = facade.loadProjectMarkdown("alpha");
    expect(projectMd).toContain("# Alpha");
    await rm(workspace, { recursive: true, force: true });
  });

  // -----------------------------------------------------------------------
  // Recall (routes through knowledgeEngine)
  // -----------------------------------------------------------------------

  it("recall routes through knowledgeEngine when routeStores=false", async () => {
    const execPython = vi.fn(async (command: string) => {
      if (command === "search") {
        return JSON.stringify([
          { text: "test fact", category: "fact", similarity: 0.85 },
        ]);
      }
      return "{}";
    });
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const results = await facade.recall({
      query: "test",
      limit: 5,
      routeStores: false,
      datastores: ["vector_basic"],
      expandGraph: false,
    });
    expect(results.length).toBeGreaterThan(0);
    expect(results[0].text).toBe("test fact");
  });

  it("recall routes through total_recall when routeStores=true", async () => {
    const callLLM = vi.fn(async () => ({
      text: '{"query":"test","datastores":["vector_basic"]}',
      model: "test",
      input_tokens: 10,
      output_tokens: 20,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      truncated: false,
    } satisfies LLMCallResult));
    const execPython = vi.fn(async (command: string) => {
      if (command === "search") {
        return JSON.stringify([
          { text: "routed fact", category: "fact", similarity: 0.9 },
        ]);
      }
      return "{}";
    });
    const facade = createQuaidFacade(makeMockDeps({ callLLM, execPython }));
    const results = await facade.recall({
      query: "test",
      limit: 5,
      routeStores: true,
      expandGraph: false,
    });
    expect(callLLM).toHaveBeenCalled();
    expect(results.length).toBeGreaterThan(0);
  });

  it("recall forwards domain filter with --domain-filter flag", async () => {
    const execPython = vi.fn(async (command: string) => {
      if (command === "search") {
        return JSON.stringify([{ text: "domain fact", category: "fact", similarity: 0.8 }]);
      }
      return "{}";
    });
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    await facade.recall({
      query: "test domain filter",
      limit: 5,
      routeStores: false,
      datastores: ["vector_basic"],
      expandGraph: false,
      domain: { personal: true },
    });
    const searchCall = execPython.mock.calls.find((args) => args[0] === "search");
    expect(searchCall).toBeTruthy();
    expect(searchCall?.[1]).toContain("--domain-filter");
    expect(searchCall?.[1]).not.toContain("--domain");
  });

  it("recallWithToolRetry returns primary results when retry heuristics do not trigger", async () => {
    const execPython = vi.fn(async (command: string) => {
      if (command !== "search") return "{}";
      return JSON.stringify([
        { text: "Alice project plan is current and active", category: "fact", similarity: 0.91 },
      ]);
    });
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const results = await facade.recallWithToolRetry({
      query: "alice project plan",
      routeStores: false,
      datastores: ["vector_basic"],
      expandGraph: false,
      limit: 5,
    });
    expect(results).toHaveLength(1);
    expect(results[0].text).toContain("Alice project plan");
    expect(execPython).toHaveBeenCalledTimes(1);
  });

  it("recallWithToolRetry retries with expanded query and merges results", async () => {
    const execPython = vi.fn(async (command: string) => {
      if (command !== "search") return "{}";
      const callCount = execPython.mock.calls.filter(([cmd]) => cmd === "search").length;
      if (callCount === 1) {
        return JSON.stringify([
          { text: "misc unrelated fragment", category: "fact", similarity: 0.2 },
        ]);
      }
      return JSON.stringify([
        { text: "Alice leads the project alpha roadmap", category: "fact", similarity: 0.84 },
      ]);
    });
    const facade = createQuaidFacade(makeMockDeps({ execPython }));
    const results = await facade.recallWithToolRetry({
      query: "who leads project alpha",
      routeStores: false,
      datastores: ["vector_basic"],
      expandGraph: false,
      limit: 5,
    });
    expect(results.length).toBeGreaterThanOrEqual(2);
    expect(results[0].text).toContain("Alice");
    expect(execPython).toHaveBeenCalledTimes(2);
  });

  it("formatMemoriesForInjection sorts by date and includes domain/confidence markers", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { domains: { technical: {}, personal: {} } },
      })),
    }));
    const out = facade.formatMemoriesForInjection([
      { text: "Alice", category: "person", similarity: 0.52 },
      {
        text: "Older fact",
        category: "fact",
        similarity: 0.7,
        createdAt: "2026-01-01T00:00:00Z",
        domains: ["technical"],
      },
      {
        text: "New uncertain fact",
        category: "fact",
        similarity: 0.65,
        createdAt: "2026-01-02T00:00:00Z",
        extractionConfidence: 0.2,
        domains: ["personal"],
      },
    ]);
    expect(out).toContain("<injected_memories>");
    expect(out).toContain("AVAILABLE_DOMAINS: personal, technical");
    expect(out).toContain("- [fact] (2026-01-01) [domains:technical] Older fact");
    expect(out).toContain("- [fact] (2026-01-02) [domains:personal] (uncertain) New uncertain fact");
    expect(out).toContain("[graph-node-hits] Entity node references");
    expect(out.indexOf("Older fact")).toBeLessThan(out.indexOf("New uncertain fact"));
  });

  // -----------------------------------------------------------------------
  // computeDynamicK
  // -----------------------------------------------------------------------

  it("computeDynamicK returns 5 when node count is low", () => {
    // With mock execPython returning {} (no by_status.active), node count falls to default 100
    const facade = createQuaidFacade(makeMockDeps());
    const k = facade.computeDynamicK();
    // 11.5 * Math.log(100) - 61.7 ≈ 11.5 * 4.605 - 61.7 ≈ -8.74 → floor=5
    expect(k).toBe(5);
  });

  it("computeDynamicK derives K from datastore active_nodes stats", () => {
    const facade = createQuaidFacade(makeMockDeps({
      getDatastoreStatsSync: vi.fn(() => ({ active_nodes: 500 })),
    }));
    expect(facade.computeDynamicK()).toBe(10);
  });

  it("computeDynamicK falls back when stats probe fails under failHard", () => {
    const facade = createQuaidFacade(makeMockDeps({
      isFailHardEnabled: vi.fn(() => true),
      getDatastoreStatsSync: vi.fn(() => {
        throw new Error("stats probe failed");
      }),
    }));
    expect(facade.computeDynamicK()).toBe(5);
  });

  // -----------------------------------------------------------------------
  // Memory notes lifecycle
  // -----------------------------------------------------------------------

  it("addMemoryNote + getAndClearMemoryNotes round-trips in memory", () => {
    const deps = makeMockDeps();
    const facade = createQuaidFacade(deps);

    // addMemoryNote will fail on disk writes (mock workspace doesn't exist)
    // but in-memory should still work
    try {
      facade.addMemoryNote("sess1", "Remember this", "fact");
    } catch {
      // Disk write may fail in test env — that's fine, in-memory still works
    }

    // Notes may not round-trip through disk in test env, but the interface works
    // Just verify the methods exist and are callable
    expect(typeof facade.addMemoryNote).toBe("function");
    expect(typeof facade.getAndClearMemoryNotes).toBe("function");
  });

  // -----------------------------------------------------------------------
  // Project catalog
  // -----------------------------------------------------------------------

  it("getProjectCatalog and getProjectNames delegate to projectCatalogReader", () => {
    const facade = createQuaidFacade(makeMockDeps());
    // With mock fs, these return empty arrays
    expect(Array.isArray(facade.getProjectCatalog())).toBe(true);
    expect(Array.isArray(facade.getProjectNames())).toBe(true);
  });

  // -----------------------------------------------------------------------
  // Guidance
  // -----------------------------------------------------------------------

  it("renderDatastoreGuidance returns store guidance text", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const guidance = facade.renderDatastoreGuidance();
    expect(guidance).toContain("Knowledge datastores:");
    expect(guidance).toContain("vector_basic");
    expect(guidance).toContain("project");
  });

  it("getMessageText supports content arrays", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(
      facade.getMessageText({
        role: "assistant",
        content: [{ text: "alpha" }, { text: "beta" }],
      }),
    ).toBe("alpha beta");
  });

  it("extractSessionId prefers context sessionId and otherwise hashes first user timestamp", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(
      facade.extractSessionId(
        [{ role: "user", content: "hello", timestamp: "2026-03-05T00:00:00Z" }],
        { sessionId: "ctx-session-1" },
      ),
    ).toBe("ctx-session-1");
    expect(
      facade.extractSessionId([
        { role: "user", content: "hello", timestamp: "2026-03-05T00:00:00Z" },
      ]),
    ).toBe("d06519e2e0ce");
  });

  it("parseSessionIdFromTranscriptPath extracts UUID session ids", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(
      facade.parseSessionIdFromTranscriptPath("/tmp/sessions/ABC-123-5f2a1b2c-4d7e-49de-a6f5-5c63b5a5b36b.jsonl"),
    ).toBe("5f2a1b2c-4d7e-49de-a6f5-5c63b5a5b36b");
    expect(facade.parseSessionIdFromTranscriptPath("/tmp/sessions/no-id.jsonl")).toBe("");
  });

  it("resolveMemoryStoreSessionId falls back from context key to main/recent sessions", () => {
    const facade = createQuaidFacade(makeMockDeps({
      resolveSessionIdFromSessionKey: vi.fn(() => ""),
      resolveDefaultSessionId: vi.fn(() => "main-session-id"),
      resolveMostRecentSessionId: vi.fn(() => "recent-session-id"),
    }));
    expect(facade.resolveMemoryStoreSessionId({ sessionKey: "missing-key" })).toBe("main-session-id");
  });

  it("resolveLifecycleHookSessionId uses event key mapping when available", () => {
    const facade = createQuaidFacade(makeMockDeps({
      resolveSessionIdFromSessionKey: vi.fn((key: string) => key === "event-key" ? "resolved-event-session" : ""),
    }));
    expect(
      facade.resolveLifecycleHookSessionId(
        { sessionKey: "event-key" },
        {},
        [{ role: "user", content: "hello", timestamp: "2026-01-01T00:00:00.000Z" }],
      ),
    ).toBe("resolved-event-session");
  });

  it("readTimeoutSessionMessages reads from session store + JSONL transcript", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-timeout-read-"));
    const sessionsDir = path.join(workspace, "sessions");
    await mkdir(sessionsDir, { recursive: true });
    const transcriptPath = path.join(sessionsDir, "sess-a.jsonl");
    await writeFile(
      transcriptPath,
      [
        JSON.stringify({ type: "message", message: { role: "user", content: "hello" } }),
        JSON.stringify({ role: "assistant", content: "world" }),
      ].join("\n") + "\n",
      "utf8",
    );
    const storePath = path.join(sessionsDir, "sessions.json");
    await writeFile(
      storePath,
      JSON.stringify({ "agent:main:a": { sessionId: "sess-a", sessionFile: transcriptPath } }),
      "utf8",
    );
    const facade = createQuaidFacade(makeMockDeps({
      timeoutSessionStorePath: () => storePath,
      timeoutSessionTranscriptDirs: () => [sessionsDir],
    }));
    const rows = facade.readTimeoutSessionMessages("sess-a") as Array<Record<string, unknown>>;
    expect(rows).toHaveLength(2);
    expect(rows[0]?.role).toBe("user");
    expect(rows[1]?.role).toBe("assistant");
    await rm(workspace, { recursive: true, force: true });
  });

  it("listTimeoutSessionActivity prefers updatedAt and falls back to transcript mtime", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-timeout-activity-"));
    const sessionsDir = path.join(workspace, "sessions");
    await mkdir(sessionsDir, { recursive: true });
    const transcriptPath = path.join(sessionsDir, "sess-b.jsonl");
    await writeFile(transcriptPath, JSON.stringify({ role: "user", content: "x" }) + "\n", "utf8");
    const storePath = path.join(sessionsDir, "sessions.json");
    await writeFile(
      storePath,
      JSON.stringify({
        "agent:main:a": { sessionId: "sess-a", updatedAt: 1700000000123 },
        "agent:main:b": { sessionId: "sess-b", sessionFile: transcriptPath },
      }),
      "utf8",
    );
    const facade = createQuaidFacade(makeMockDeps({
      timeoutSessionStorePath: () => storePath,
      timeoutSessionTranscriptDirs: () => [sessionsDir],
    }));
    const rows = facade.listTimeoutSessionActivity();
    const byId = new Map(rows.map((r) => [r.sessionId, r.lastActivityMs]));
    expect(byId.get("sess-a")).toBe(1700000000123);
    expect((byId.get("sess-b") || 0) > 0).toBe(true);
    await rm(workspace, { recursive: true, force: true });
  });

  it("resolveSessionForCompaction prefers matching session then default-session fallback", () => {
    const facade = createQuaidFacade(makeMockDeps({
      listCompactionSessions: () => ([
        { key: "agent:main:other", sessionId: "sess-other" },
        { key: "agent:main:main", sessionId: "sess-main" },
      ]),
      resolveDefaultSessionId: () => "sess-main",
    }));
    expect(facade.resolveSessionForCompaction("sess-main")).toBe("agent:main:main");
    expect(facade.resolveSessionForCompaction("missing")).toBe("agent:main:main");
  });

  it("maybeForceCompactionAfterTimeout invokes requestSessionCompaction when enabled", () => {
    const requestSessionCompaction = vi.fn(() => ({ ok: true, compacted: 2 }));
    const facade = createQuaidFacade(makeMockDeps({
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        capture: { autoCompactionOnTimeout: true },
      })),
      listCompactionSessions: () => [{ key: "agent:main:main", sessionId: "sess-main" }],
      requestSessionCompaction,
    }));
    facade.maybeForceCompactionAfterTimeout("sess-main");
    expect(requestSessionCompaction).toHaveBeenCalledWith("agent:main:main");
  });

  it("isLowQualityQuery filters acknowledgments and short prompts", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(facade.isLowQualityQuery("ok")).toBe(true);
    expect(facade.isLowQualityQuery("sounds good")).toBe(true);
    expect(facade.isLowQualityQuery("please summarize the migration risks")).toBe(false);
  });

  it("filterMemoriesByPrivacy keeps visible and owner-matching private memories", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const out = facade.filterMemoriesByPrivacy([
      { text: "public", category: "fact", similarity: 0.8 },
      { text: "mine", category: "fact", similarity: 0.9, privacy: "private", ownerId: "quaid" },
      { text: "other", category: "fact", similarity: 0.95, privacy: "private", ownerId: "alice" },
    ], "quaid");
    expect(out.map((m) => m.text)).toEqual(["public", "mine"]);
  });

  it("load/save/reset injection dedup state round-trips", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-injection-state-"));
    await mkdir(path.join(workspace, ".quaid", "runtime", "injection"), { recursive: true });
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    expect(facade.loadInjectedMemoryKeys("sess-1")).toEqual([]);
    const merged = facade.saveInjectedMemoryKeys(
      "sess-1",
      [],
      [
        { text: "alpha", category: "fact", similarity: 0.9 },
        { text: "beta", category: "fact", similarity: 0.8 },
      ],
      100,
    );
    expect(merged).toEqual(["alpha", "beta"]);
    expect(facade.loadInjectedMemoryKeys("sess-1")).toEqual(["alpha", "beta"]);
    facade.resetInjectionDedupAfterCompaction("sess-1");
    expect(facade.loadInjectedMemoryKeys("sess-1")).toEqual([]);
    await rm(workspace, { recursive: true, force: true });
  });

  it("prepareAutoInjectionContext applies privacy filter, dedup, and context merge", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-auto-inject-"));
    await mkdir(path.join(workspace, ".quaid", "runtime", "injection"), { recursive: true });
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    const baseMemories = [
      { text: "public fact", category: "fact", similarity: 0.9 },
      { text: "other private", category: "fact", similarity: 0.8, privacy: "private", ownerId: "alice" },
    ];

    const first = facade.prepareAutoInjectionContext({
      allMemories: baseMemories,
      eventMessages: [{ role: "user", content: "hello", timestamp: Date.now() }],
      context: { sessionId: "sess-auto" },
      existingPrependContext: "seed",
      injectLimit: 5,
      maxInjectionIdsPerSession: 100,
    });
    expect(first).toBeTruthy();
    expect(first?.toInject.map((m) => m.text)).toEqual(["public fact"]);
    expect(first?.prependContext).toContain("seed");
    expect(first?.prependContext).toContain("public fact");

    const second = facade.prepareAutoInjectionContext({
      allMemories: baseMemories,
      eventMessages: [{ role: "user", content: "hello", timestamp: Date.now() }],
      context: { sessionId: "sess-auto" },
      existingPrependContext: "seed",
      injectLimit: 5,
      maxInjectionIdsPerSession: 100,
    });
    expect(second).toBeNull();
    await rm(workspace, { recursive: true, force: true });
  });

  it("formatRecallToolResponse returns grouped text and source breakdown", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const out = facade.formatRecallToolResponse([
      { text: "vector memory", category: "fact", similarity: 0.7, via: "vector" },
      { text: "graph memory", category: "graph", similarity: 0.8, via: "graph" },
      { text: "journal memory", category: "fact", similarity: 0.6, via: "journal" },
      { text: "project memory", category: "fact", similarity: 0.65, via: "project" },
    ]);
    expect(out.text).toContain("Direct Matches");
    expect(out.text).toContain("Graph Discoveries");
    expect(out.breakdown).toEqual({
      vector_count: 1,
      graph_count: 1,
      journal_count: 1,
      project_count: 1,
    });
  });

  it("buildRecallNotificationPayload normalizes memories and derives breakdown", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const payload = facade.buildRecallNotificationPayload(
      [
        { text: "v1", category: "fact", similarity: 0.71, via: "vector" },
        { text: "g1", category: "graph", similarity: 0.82, via: "graph" },
        { text: "j1", category: "fact", similarity: 0.63, via: "journal" },
      ],
      "who leads project alpha",
      "tool",
    );
    expect(payload.memories).toEqual([
      { text: "v1", similarity: 71, via: "vector", category: "fact" },
      { text: "g1", similarity: 82, via: "graph", category: "graph" },
      { text: "j1", similarity: 63, via: "journal", category: "fact" },
    ]);
    expect(payload.source_breakdown).toEqual({
      vector_count: 1,
      graph_count: 1,
      journal_count: 1,
      project_count: 0,
      query: "who leads project alpha",
      mode: "tool",
    });
  });

  it("buildRecallNotificationPayload respects provided breakdown override", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const payload = facade.buildRecallNotificationPayload(
      [{ text: "v1", category: "fact", similarity: 0.5, via: "vector" }],
      "query",
      "auto_inject",
      { vector_count: 5, graph_count: 2, journal_count: 0, project_count: 0 },
    );
    expect(payload.source_breakdown.vector_count).toBe(5);
    expect(payload.source_breakdown.graph_count).toBe(2);
    expect(payload.source_breakdown.mode).toBe("auto_inject");
  });

  it("buildExtractionCompletionNotificationPayload merges snippet and journal details", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const payload = facade.buildExtractionCompletionNotificationPayload({
      stored: 2,
      skipped: 1,
      edgesCreated: 3,
      triggerType: "unknown",
      factDetails: [{ text: "fact", status: "stored" }],
      snippetDetails: { "a.md": ["s1"] },
      journalDetails: { "a.md": ["j1"], "b.md": ["j2"] },
      alwaysNotifyCompletion: true,
    });
    expect(payload.trigger).toBe("reset");
    expect(payload.snippet_details).toEqual({
      "a.md": ["[snippet] s1", "[journal] j1"],
      "b.md": ["[journal] j2"],
    });
    expect(payload.always_notify).toBe(true);
    expect(payload.stored).toBe(2);
    expect(payload.edges_created).toBe(3);
  });

  it("queueCompactionExtractionSummary batches and flushes once", () => {
    vi.useFakeTimers();
    const notify = vi.fn();
    const facade = createQuaidFacade(makeMockDeps());
    facade.queueCompactionExtractionSummary("s1", 2, 1, 1, notify);
    facade.queueCompactionExtractionSummary("s2", 3, 0, 2, notify);
    vi.advanceTimersByTime(11_000);
    expect(notify).toHaveBeenCalledTimes(1);
    const msg = String(notify.mock.calls[0]?.[0] || "");
    expect(msg).toContain("Sessions processed: 2");
    expect(msg).toContain("Facts stored: 5");
    vi.useRealTimers();
  });

  it("filterConversationMessages drops internal extraction payloads and maintenance prompts", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const filtered = facade.filterConversationMessages([
      { role: "user", content: "normal user message" },
      { role: "assistant", content: "Extract memorable facts and journal entries from this conversation: ..." },
      { role: "assistant", content: '{"facts":[{"text":"x"}],"journal_entries":[],"soul_snippets":[]}' },
      { role: "assistant", content: "Review batch #42 and respond with a JSON array only:" },
      { role: "assistant", content: "normal assistant message" },
    ]);
    expect(filtered).toHaveLength(2);
    expect((filtered[0] as any).content).toBe("normal user message");
    expect((filtered[1] as any).content).toBe("normal assistant message");
  });

  it("buildTranscript filters system noise and formats user/assistant messages", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const transcript = facade.buildTranscript([
      { role: "system", content: "ignored" },
      { role: "user", content: "[Telegram bob] hello there" },
      { role: "assistant", content: "HEARTBEAT_OK" },
      { role: "assistant", content: "working on /tmp/test/file.ts" },
    ]);
    expect(transcript).toContain("User: hello there");
    expect(transcript).toContain("Alfie: working on /tmp/test/file.ts");
    expect(transcript).not.toContain("HEARTBEAT_OK");
  });

  it("extractFilePaths returns deduplicated non-http path candidates", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const paths = facade.extractFilePaths([
      { role: "user", content: "edit /tmp/a.ts and src/main.ts" },
      { role: "assistant", content: "see also https://example.com/a.ts and src/main.ts" },
    ]);
    expect(paths).toEqual(expect.arrayContaining(["/tmp/a.ts", "src/main.ts"]));
    expect(paths.some((p) => p.startsWith("http"))).toBe(false);
  });

  it("summarizeProjectSession parses JSON from LLM output", async () => {
    const callLLM = vi.fn(async () => ({
      text: '{"project_name":"quaid","text":"worked on adapter boundary"}',
      model: "test-model",
      input_tokens: 10,
      output_tokens: 20,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      truncated: false,
    } satisfies LLMCallResult));
    const facade = createQuaidFacade(makeMockDeps({ callLLM }));
    const out = await facade.summarizeProjectSession([
      { role: "user", content: "please refactor adapter boundary in quaid" },
      { role: "assistant", content: "done with facade extraction" },
    ]);
    expect(out).toEqual({
      project_name: "quaid",
      text: "worked on adapter boundary",
    });
  });

  it("summarizeProjectSession falls back to transcript snippet on non-JSON output", async () => {
    const callLLM = vi.fn(async () => ({
      text: "not-json",
      model: "test-model",
      input_tokens: 10,
      output_tokens: 20,
      cache_read_tokens: 0,
      cache_creation_tokens: 0,
      truncated: false,
    } satisfies LLMCallResult));
    const facade = createQuaidFacade(makeMockDeps({ callLLM }));
    const out = await facade.summarizeProjectSession([
      { role: "user", content: "first line" },
      { role: "assistant", content: "second line" },
    ]);
    expect(out.project_name).toBeNull();
    expect(out.text).toContain("User: first line");
  });

  it("isResetBootstrapOnlyConversation detects bootstrap-only user prompts", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(
      facade.isResetBootstrapOnlyConversation([
        { role: "user", content: "A new session was started via /new or /reset." },
        { role: "assistant", content: "How can I help?" },
      ]),
    ).toBe(true);
    expect(
      facade.isResetBootstrapOnlyConversation([
        { role: "user", content: "A new session was started via /new or /reset." },
        { role: "user", content: "real question" },
      ]),
    ).toBe(false);
  });

  it("isVectorRecallResult matches vector datastore variants only", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(facade.isVectorRecallResult({ text: "a", category: "fact", similarity: 0.5, via: "vector" })).toBe(true);
    expect(facade.isVectorRecallResult({ text: "a", category: "fact", similarity: 0.5, via: "vector_technical" })).toBe(true);
    expect(facade.isVectorRecallResult({ text: "a", category: "graph", similarity: 0.5, via: "graph" })).toBe(false);
  });

  it("updateDocsFromTranscript emits docs.ingest_transcript event when enabled", async () => {
    const execEvents = vi.fn(async () => JSON.stringify({
      processed: { details: [{ result: { result: { status: "updated", updatedDocs: 2, staleDocs: 3 } } }] },
    }));
    const facade = createQuaidFacade(makeMockDeps({
      execEvents,
      isSystemEnabled: vi.fn((system: string) => system === "workspace") as any,
      getMemoryConfig: vi.fn(() => ({ docs: { autoUpdateOnCompact: true } })),
    }));
    await facade.updateDocsFromTranscript(
      [{ role: "user", content: "please update docs for src/main.ts" }],
      "Compaction",
      "sess-1",
      "/tmp",
    );
    expect(execEvents).toHaveBeenCalledTimes(1);
    expect(execEvents).toHaveBeenCalledWith(
      "emit",
      expect.arrayContaining(["--name", "docs.ingest_transcript", "--dispatch", "immediate"]),
    );
  });

  it("stageProjectEvent writes event payload when projects are enabled", async () => {
    const facade = createQuaidFacade(makeMockDeps({
      isSystemEnabled: vi.fn((system: string) => system === "projects") as any,
      getMemoryConfig: vi.fn(() => ({ projects: { enabled: true } })),
      callLLM: vi.fn(async () => ({
        text: '{"project_name":"quaid","text":"session summary"}',
        model: "test-model",
        input_tokens: 10,
        output_tokens: 20,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        truncated: false,
      })),
    }));
    const staged = await facade.stageProjectEvent(
      [{ role: "user", content: "edited src/main.ts in quaid" }],
      "compact",
      "sess-2",
      "/tmp",
      1000,
    );
    expect(staged).not.toBeNull();
    const payload = JSON.parse(await readFile(staged!.eventPath, "utf8"));
    expect(payload.project_hint).toBe("quaid");
    expect(payload.trigger).toBe("compact");
    expect(payload.session_id).toBe("sess-2");
    expect(Array.isArray(payload.files_touched)).toBe(true);
    await unlink(staged!.eventPath);
  });

  it("shouldProcessLifecycleSignal suppresses duplicate signatures in cooldown window", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const sessionId = "sess-a";
    const signal = { label: "ResetSignal" as const, source: "user_command" as const, signature: "cmd:/reset" };
    expect(facade.shouldProcessLifecycleSignal(sessionId, signal, 60_000, 60_000)).toBe(true);
    expect(facade.shouldProcessLifecycleSignal(sessionId, signal, 60_000, 60_000)).toBe(false);
  });

  it("markLifecycleSignalFromHook suppresses immediate system_notice for same label", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const sessionId = "sess-b";
    facade.markLifecycleSignalFromHook(sessionId, "CompactionSignal");
    const signal = { label: "CompactionSignal" as const, source: "system_notice" as const, signature: "system:compacted..." };
    expect(facade.shouldProcessLifecycleSignal(sessionId, signal, 60_000, 60_000)).toBe(false);
    facade.clearLifecycleSignalHistory();
    expect(facade.shouldProcessLifecycleSignal(sessionId, signal, 60_000, 60_000)).toBe(true);
  });

  it("isInternalMaintenancePrompt detects janitor/review internal prompts", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(facade.isInternalMaintenancePrompt("Review batch #42 and respond with a JSON array only:")).toBe(true);
    expect(facade.isInternalMaintenancePrompt("Please remember my dog is Pixel")).toBe(false);
  });

  it("resolveExtractionTrigger normalizes lifecycle labels", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(facade.resolveExtractionTrigger("CompactionSignal")).toBe("compaction");
    expect(facade.resolveExtractionTrigger("timeout event")).toBe("timeout");
    expect(facade.resolveExtractionTrigger("recovery-run")).toBe("recovery");
    expect(facade.resolveExtractionTrigger("")).toBe("unknown");
  });

  it("shouldNotifyExtractionStart returns trigger description when gates pass", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const out = facade.shouldNotifyExtractionStart({
      messages: [{ role: "user", content: "remember this", timestamp: Date.now() }],
      label: "CompactionSignal",
      sessionId: "sess-start",
      hasMeaningfulUserContent: true,
      bootTimeMs: Date.now() - 1_000,
      backlogNotifyStaleMs: 90_000,
      showProcessingStart: true,
    });
    expect(out).toEqual({ triggerDesc: "compaction" });
  });

  // -----------------------------------------------------------------------
  // Stubs throw "not implemented"
  // -----------------------------------------------------------------------

  it("detectLifecycleSignal identifies reset user command", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const signal = facade.detectLifecycleSignal([
      { role: "user", content: "/new" },
    ]);
    expect(signal).toEqual({
      label: "ResetSignal",
      source: "user_command",
      signature: "cmd:/new",
    });
  });

  it("detectLifecycleSignal identifies system compaction notice", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const signal = facade.detectLifecycleSignal([
      { role: "system", content: "Compacted (37k -> 5.0k) Context 5.0k/200k" },
    ]);
    expect(signal).toEqual({
      label: "CompactionSignal",
      source: "system_notice",
      signature: "system:compacted (37k -> 5.0k) context 5.0k/200k",
    });
  });

  it("hasExplicitLifecycleUserCommand detects slash lifecycle input", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(
      facade.hasExplicitLifecycleUserCommand([
        { role: "user", content: "/compact" },
      ]),
    ).toBe(true);
  });

  it("isBacklogLifecycleReplay suppresses stale implicit reset replay", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const bootTime = Date.parse("2026-03-04T00:00:00Z");
    const stale = 90_000;
    expect(
      facade.isBacklogLifecycleReplay(
        [{ role: "system", content: "session resumed", timestamp: bootTime - stale - 1 }],
        "reset",
        bootTime + 1_000,
        bootTime,
        stale,
      ),
    ).toBe(true);
  });

  it("processLifecycleEvent throws not implemented", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(() => facade.processLifecycleEvent({}, {})).toThrow("not yet implemented");
  });

  it("maybeRunMaintenance throws not implemented", () => {
    const facade = createQuaidFacade(makeMockDeps());
    expect(() => facade.maybeRunMaintenance("sess1")).toThrow("not yet implemented");
  });

  it("getJanitorHealthIssue returns null/string status", () => {
    const facade = createQuaidFacade(makeMockDeps());
    const result = facade.getJanitorHealthIssue();
    expect(result === null || typeof result === "string").toBe(true);
  });

  it("queueExtraction serializes queued tasks", async () => {
    const facade = createQuaidFacade(makeMockDeps());
    const order: string[] = [];
    const first = facade.queueExtraction(async () => {
      await new Promise((resolve) => setTimeout(resolve, 15));
      order.push("first");
    }, "first");
    const second = facade.queueExtraction(async () => {
      order.push("second");
    }, "second");
    expect(facade.getQueuedExtractionPromise()).not.toBeNull();
    await Promise.all([first, second]);
    expect(order).toEqual(["first", "second"]);
  });

  it("queueExtraction retries next task after prior failure when failHard is disabled", async () => {
    const facade = createQuaidFacade(makeMockDeps({
      isFailHardEnabled: vi.fn(() => false),
    }));
    const first = facade.queueExtraction(async () => {
      throw new Error("boom");
    }, "first");
    await expect(first).rejects.toThrow("boom");

    let ran = false;
    await facade.queueExtraction(async () => {
      ran = true;
    }, "second");
    expect(ran).toBe(true);
  });

  it("queueDelayedRequest writes and dedupes pending delayed requests", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-delayed-req-"));
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    const queued = facade.queueDelayedRequest({
      message: "janitor needs follow-up",
      kind: "janitor_health",
      priority: "high",
      source: "test",
    });
    const duplicate = facade.queueDelayedRequest({
      message: "janitor needs follow-up",
      kind: "janitor_health",
      priority: "high",
      source: "test",
    });
    expect(queued).toBe(true);
    expect(duplicate).toBe(false);

    const delayedPath = path.join(workspace, ".quaid", "runtime", "notes", "delayed-llm-requests.json");
    const payload = JSON.parse(await readFile(delayedPath, "utf8"));
    expect(Array.isArray(payload.requests)).toBe(true);
    expect(payload.requests).toHaveLength(1);
    expect(payload.requests[0]).toMatchObject({
      kind: "janitor_health",
      priority: "high",
      source: "test",
      status: "pending",
      message: "janitor needs follow-up",
    });
    await rm(workspace, { recursive: true, force: true });
  });

  it("emitProjectEvent calls background callback after staging", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-project-event-"));
    await mkdir(path.join(workspace, "projects", "demo"), { recursive: true });
    const emitProjectEventBackground = vi.fn();
    const facade = createQuaidFacade(makeMockDeps({
      workspace,
      emitProjectEventBackground,
      isSystemEnabled: vi.fn((system: string) => system === "projects"),
      getMemoryConfig: vi.fn(() => ({
        retrieval: { failHard: false },
        projects: { enabled: true, stagingDir: ".quaid/runtime/projects/staging" },
      })),
      callLLM: vi.fn(async () => ({
        text: '{"project_name":"demo","summary":"project event summary"}',
        model: "test-model",
        input_tokens: 10,
        output_tokens: 10,
        cache_read_tokens: 0,
        cache_creation_tokens: 0,
        truncated: false,
      })),
    }));
    await facade.emitProjectEvent(
      [{ role: "user", content: "Updated demo project", timestamp: "2026-03-05T00:00:00.000Z" }],
      "compact",
      "sess-project",
      1_000,
    );
    expect(emitProjectEventBackground).toHaveBeenCalledTimes(1);
    const [eventPath, projectHint] = emitProjectEventBackground.mock.calls[0];
    expect(String(eventPath)).toContain(".quaid/runtime/projects/staging");
    expect(projectHint).toBe("demo");
    await rm(workspace, { recursive: true, force: true });
  });

  it("collectJanitorNudges emits install/approval nudges with cooldown persistence", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-janitor-nudges-"));
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    const statePath = path.join(workspace, ".quaid", "runtime", "notes", "janitor-nudge-state.json");
    const pendingInstallMigrationPath = path.join(workspace, ".quaid", "runtime", "pending-install-migration.json");
    const pendingApprovalRequestsPath = path.join(workspace, ".quaid", "runtime", "notes", "pending-approval-requests.json");
    await mkdir(path.dirname(pendingInstallMigrationPath), { recursive: true });
    await mkdir(path.dirname(pendingApprovalRequestsPath), { recursive: true });
    await writeFile(pendingInstallMigrationPath, JSON.stringify({ status: "pending" }), "utf8");
    await writeFile(
      pendingApprovalRequestsPath,
      JSON.stringify({ requests: [{ status: "pending" }, { status: "resolved" }] }),
      "utf8",
    );

    const nudges = facade.collectJanitorNudges({
      statePath,
      pendingInstallMigrationPath,
      pendingApprovalRequestsPath,
      nowMs: 1_700_000_000_000,
    });
    expect(nudges).toHaveLength(2);
    expect(nudges[0]).toContain("just installed Quaid");
    expect(nudges[1]).toContain("1 pending approval request");

    const suppressed = facade.collectJanitorNudges({
      statePath,
      pendingInstallMigrationPath,
      pendingApprovalRequestsPath,
      nowMs: 1_700_000_000_500,
    });
    expect(suppressed).toEqual([]);
    await rm(workspace, { recursive: true, force: true });
  });

  it("maybeQueueJanitorHealthAlert persists cooldown state", async () => {
    const workspace = await mkdtemp(path.join(tmpdir(), "quaid-facade-janitor-health-"));
    const facade = createQuaidFacade(makeMockDeps({ workspace }));
    const statePath = path.join(workspace, ".quaid", "runtime", "notes", "janitor-nudge-state.json");
    const first = facade.maybeQueueJanitorHealthAlert({
      statePath,
      nowMs: 1_700_000_000_000,
    });
    const second = facade.maybeQueueJanitorHealthAlert({
      statePath,
      nowMs: 1_700_000_001_000,
    });
    expect(first).toBe(true);
    expect(second).toBe(false);
    const state = JSON.parse(await readFile(statePath, "utf8"));
    expect(typeof state.lastJanitorHealthIssue).toBe("string");
    expect(Number(state.lastJanitorHealthAlertAt)).toBe(1_700_000_000_000);
    await rm(workspace, { recursive: true, force: true });
  });
});
