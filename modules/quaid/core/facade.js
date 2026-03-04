import { createDatastoreBridge } from "./datastore-bridge.js";
import { createProjectCatalogReader } from "./project-catalog.js";
import { createKnowledgeEngine } from "./knowledge-engine.js";
import {
  normalizeKnowledgeDatastores,
  renderKnowledgeDatastoreGuidanceForAgents
} from "./knowledge-stores.js";
import { execFileSync } from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
const FAST_ROUTER_TIMEOUT_MS = 45e3;
const DEEP_ROUTER_TIMEOUT_MS = 6e4;
const NODE_COUNT_CACHE_MS = 12e4;
const DATASTORE_STATS_TIMEOUT_MS = 3e4;
const MAX_MEMORY_NOTES_PER_SESSION = 400;
const MAX_MEMORY_NOTE_SESSIONS = 200;
const RECALL_RETRY_STOPWORDS = /* @__PURE__ */ new Set([
  "a",
  "an",
  "and",
  "are",
  "as",
  "at",
  "be",
  "by",
  "do",
  "for",
  "from",
  "how",
  "i",
  "in",
  "is",
  "it",
  "me",
  "my",
  "of",
  "on",
  "or",
  "our",
  "that",
  "the",
  "their",
  "they",
  "this",
  "to",
  "was",
  "we",
  "what",
  "when",
  "where",
  "which",
  "who",
  "why",
  "with",
  "you",
  "your"
]);
function createQuaidFacade(deps) {
  const datastoreBridge = createDatastoreBridge(deps.execPython);
  const projectCatalogReader = createProjectCatalogReader({
    workspace: deps.workspace,
    fs,
    path,
    isFailHardEnabled: deps.isFailHardEnabled
  });
  const knowledgeEngine = createKnowledgeEngine({
    workspace: deps.workspace,
    getMemoryConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    callFastRouter: async (systemPrompt, userPrompt) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "fast", 120, FAST_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    callDeepRouter: async (systemPrompt, userPrompt) => {
      const llm = await deps.callLLM(systemPrompt, userPrompt, "deep", 160, DEEP_ROUTER_TIMEOUT_MS);
      return String(llm?.text || "");
    },
    recallVector: async (query, limit, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(
        query,
        limit,
        false,
        1,
        scope,
        domainBoost,
        project,
        dateFrom,
        dateTo
      );
      return results.map((r) => ({ ...r, via: "vector" }));
    },
    recallGraph: async (query, limit, depth, scope, domainBoost, project, dateFrom, dateTo) => {
      const results = await recallFromBridge(
        query,
        limit,
        true,
        depth,
        scope,
        domainBoost,
        project,
        dateFrom,
        dateTo
      );
      return results.filter((r) => (r.via || "") === "graph" || r.category === "graph").map((r) => ({ ...r, via: "graph" }));
    },
    recallJournalStore: async (query, limit) => {
      const journalConfig = deps.getMemoryConfig().docs?.journal || {};
      const journalDir = path.join(deps.workspace, journalConfig.journalDir || "journal");
      return recallFromJournal(query, limit, journalDir);
    },
    recallProjectStore: async (query, limit, project, docs) => {
      const args = [query, "--limit", String(limit)];
      if (project) args.push("--project", project);
      if (Array.isArray(docs) && docs.length > 0) args.push("--docs", docs.join(","));
      const out = await deps.execDocsRag("search", args);
      return parseProjectStoreResults(out, query, limit, project);
    }
  });
  let _cachedDatastoreStats = null;
  let _datastoreStatsTimestamp = 0;
  let _cachedNodeCount = null;
  let _nodeCountTimestamp = 0;
  const lifecycleSignalHistory = /* @__PURE__ */ new Map();
  function getDatastoreStatsSync(maxAgeMs = NODE_COUNT_CACHE_MS) {
    const now = Date.now();
    if (now - _datastoreStatsTimestamp < maxAgeMs) {
      return _cachedDatastoreStats;
    }
    try {
      const pyScript = path.join(deps.pluginRoot, "datastore/memorydb/memory_graph.py");
      const output = execFileSync("python3", [pyScript, "stats"], {
        encoding: "utf-8",
        timeout: DATASTORE_STATS_TIMEOUT_MS,
        env: {
          ...process.env,
          MEMORY_DB_PATH: deps.dbPath,
          QUAID_HOME: deps.workspace,
          CLAWDBOT_WORKSPACE: deps.workspace,
          PYTHONPATH: deps.pluginRoot
        }
      });
      const parsed = JSON.parse(output);
      if (!parsed || typeof parsed !== "object") {
        _cachedDatastoreStats = null;
        _datastoreStatsTimestamp = now;
        return null;
      }
      _cachedDatastoreStats = parsed;
      _datastoreStatsTimestamp = now;
      return parsed;
    } catch (err) {
      const msg = `[quaid][facade] datastore stats read failed: ${err?.message || String(err)}`;
      if (deps.isFailHardEnabled()) {
        throw new Error(msg, { cause: err instanceof Error ? err : new Error(String(err)) });
      }
      _cachedDatastoreStats = null;
      _datastoreStatsTimestamp = now;
      console.warn(msg);
      return null;
    }
  }
  function getActiveNodeCount() {
    const now = Date.now();
    if (_cachedNodeCount !== null && now - _nodeCountTimestamp < NODE_COUNT_CACHE_MS) {
      return _cachedNodeCount;
    }
    const stats = getDatastoreStatsSync(NODE_COUNT_CACHE_MS);
    const active = Number(stats?.active_nodes ?? stats?.by_status?.active ?? 0);
    if (Number.isFinite(active) && active > 0) {
      _cachedNodeCount = active;
      _nodeCountTimestamp = now;
      return _cachedNodeCount;
    }
    if (_cachedNodeCount === null && deps.isFailHardEnabled()) {
      throw new Error("[quaid][facade] unable to derive active node count under failHard");
    }
    return _cachedNodeCount ?? 100;
  }
  function parseDatastoreStats(raw) {
    let parsed = null;
    try {
      parsed = JSON.parse(raw || "{}");
    } catch {
      return null;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return null;
    }
    const totalNodes = Number(parsed.total_nodes);
    const edges = Number(parsed.edges);
    const activeNodes = Number(parsed.active_nodes);
    if (!Number.isFinite(totalNodes) || totalNodes < 0) {
      return null;
    }
    if (!Number.isFinite(edges) || edges < 0) {
      return null;
    }
    return {
      total_nodes: totalNodes,
      edges,
      ...Number.isFinite(activeNodes) && activeNodes >= 0 ? { active_nodes: activeNodes } : {}
    };
  }
  async function getStatsParsed() {
    try {
      const output = await datastoreBridge.stats();
      return parseDatastoreStats(output);
    } catch (err) {
      console.error("[quaid][facade] stats error:", err.message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
      return null;
    }
  }
  function getMessageText(msg) {
    if (!msg || typeof msg !== "object") return "";
    const candidate = msg.content ?? msg.text ?? msg.message;
    if (typeof candidate === "string") return candidate;
    if (Array.isArray(candidate)) {
      return candidate.map((part) => {
        if (!part || typeof part !== "object") return "";
        const text = part.text;
        return typeof text === "string" ? text : "";
      }).join(" ");
    }
    return "";
  }
  function buildTranscript(messages) {
    const transcript = [];
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg;
      const role = String(rec.role || "");
      if (role !== "user" && role !== "assistant") continue;
      let text = getMessageText(msg);
      if (!text) continue;
      if (typeof deps.transcriptFormat?.preprocessText === "function") {
        text = deps.transcriptFormat.preprocessText(text);
      }
      const shouldSkip = deps.transcriptFormat?.shouldSkipText;
      if (typeof shouldSkip === "function" && shouldSkip(role, text)) continue;
      if (!text) continue;
      const speakerLabel = deps.transcriptFormat?.speakerLabel;
      const speaker = typeof speakerLabel === "function" ? speakerLabel(role) : role === "user" ? "User" : "Assistant";
      transcript.push(`${speaker}: ${text}`);
    }
    return transcript.join("\n\n");
  }
  function extractFilePaths(messages) {
    const paths = /* @__PURE__ */ new Set();
    for (const msg of messages) {
      const text = getMessageText(msg);
      if (!text) continue;
      const matches = text.match(/(?:^|\s)((?:\/[\w.-]+)+|(?:[\w.-]+\/)+[\w.-]+)/gm);
      if (!matches) continue;
      for (const match of matches) {
        const candidate = match.trim();
        if (candidate.includes("/") && !candidate.startsWith("http") && candidate.length < 200) {
          paths.add(candidate);
        }
      }
    }
    return Array.from(paths);
  }
  async function summarizeProjectSession(messages, timeoutMs = FAST_ROUTER_TIMEOUT_MS) {
    const transcript = buildTranscript(messages);
    if (!transcript || transcript.length < 20) {
      return { project_name: null, text: "" };
    }
    try {
      const llm = await deps.callLLM(
        `You summarize coding sessions. Given a conversation, identify: 1) What project was being worked on (use one of the available project names, or null if unclear), 2) Brief summary of what changed/was discussed. Available projects: ${projectCatalogReader.getProjectNames().join(", ")}. Use these EXACT names. Respond with JSON only: {"project_name": "name-or-null", "text": "brief summary"}`,
        `Summarize this session:

${transcript.slice(0, 4e3)}`,
        "fast",
        300,
        timeoutMs
      );
      const output = String(llm?.text || "").trim();
      const jsonMatch = output.match(/\{[\s\S]*\}/);
      if (jsonMatch) {
        try {
          const parsed = JSON.parse(jsonMatch[0]);
          return {
            project_name: typeof parsed.project_name === "string" ? parsed.project_name : null,
            text: typeof parsed.text === "string" ? parsed.text : ""
          };
        } catch {
        }
      }
    } catch (err) {
      console.error("[quaid][facade] Quick project summary failed:", err.message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
    }
    return { project_name: null, text: transcript.slice(0, 500) };
  }
  function isResetBootstrapOnlyConversation(messages, bootstrapPrompt = "A new session was started via /new or /reset.") {
    const userTexts = messages.filter((msg) => msg && typeof msg === "object" && String(msg.role || "") === "user").map((msg) => getMessageText(msg).trim()).filter(Boolean);
    if (userTexts.length === 0) return false;
    const nonBootstrapUserTexts = userTexts.filter((text) => !text.startsWith(bootstrapPrompt));
    return nonBootstrapUserTexts.length === 0;
  }
  async function updateDocsFromTranscript(messages, label, sessionId, tempDir = path.join(deps.workspace, ".quaid", "tmp")) {
    if (!deps.isSystemEnabled("workspace")) {
      return;
    }
    const memConfig = deps.getMemoryConfig();
    if (!memConfig.docs?.autoUpdateOnCompact) {
      return;
    }
    const fullTranscript = buildTranscript(messages);
    if (!fullTranscript.trim()) {
      console.log(`[quaid][facade] ${label}: no transcript for doc update`);
      return;
    }
    const tmpPath = path.join(
      tempDir,
      `docs-ingest-${Date.now()}-${Math.random().toString(36).slice(2)}.txt`
    );
    fs.writeFileSync(tmpPath, fullTranscript, { mode: 384 });
    try {
      console.log(`[quaid][facade] ${label}: dispatching docs ingest event...`);
      const startTime = Date.now();
      const out = await deps.execEvents("emit", [
        "--name",
        "docs.ingest_transcript",
        "--payload",
        JSON.stringify({
          transcript_path: tmpPath,
          label,
          session_id: sessionId || null
        }),
        "--source",
        String(deps.eventSource || "adapter"),
        "--dispatch",
        "immediate"
      ]);
      let parsed = {};
      try {
        const candidate = JSON.parse(out || "{}");
        if (candidate && typeof candidate === "object" && !Array.isArray(candidate)) {
          parsed = candidate;
        }
      } catch {
      }
      const processed = parsed.processed;
      const details = Array.isArray(processed?.details) ? processed?.details : [];
      const first = details[0] && typeof details[0] === "object" ? details[0] : {};
      const resultObj = first.result && typeof first.result === "object" ? first.result : {};
      const nested = resultObj.result && typeof resultObj.result === "object" ? resultObj.result : resultObj;
      const status = String(nested.status || "").trim();
      const elapsed = ((Date.now() - startTime) / 1e3).toFixed(1);
      if (status === "up_to_date") {
        console.log(`[quaid][facade] ${label}: all docs up-to-date (${elapsed}s)`);
        return;
      }
      if (status === "updated") {
        const updatedDocs = Number(nested.updatedDocs || 0);
        const staleDocs = Number(nested.staleDocs || 0);
        console.log(`[quaid][facade] ${label}: docs updated (${updatedDocs}/${staleDocs}) (${elapsed}s)`);
        return;
      }
      if (status === "disabled" || status === "skipped") {
        console.log(`[quaid][facade] ${label}: docs ingest skipped (${String(nested.message || "disabled")})`);
        return;
      }
      console.log(`[quaid][facade] ${label}: docs ingest finished (${elapsed}s)`);
    } catch (err) {
      console.error(`[quaid][facade] ${label} doc update failed:`, err.message);
      if (deps.isFailHardEnabled()) {
        throw err;
      }
    } finally {
      try {
        fs.unlinkSync(tmpPath);
      } catch {
      }
    }
  }
  async function stageProjectEvent(messages, trigger, sessionId, stagingDirOverride, summaryTimeoutMs = FAST_ROUTER_TIMEOUT_MS) {
    if (!deps.isSystemEnabled("projects")) {
      return null;
    }
    const memConfig = deps.getMemoryConfig();
    if (!memConfig.projects?.enabled) {
      return null;
    }
    const summary = await summarizeProjectSession(messages, summaryTimeoutMs);
    const event = {
      project_hint: summary.project_name || null,
      files_touched: extractFilePaths(messages),
      summary: summary.text,
      trigger,
      session_id: sessionId,
      timestamp: (/* @__PURE__ */ new Date()).toISOString()
    };
    const stagingDir = stagingDirOverride ? path.resolve(stagingDirOverride) : path.join(deps.workspace, memConfig.projects.stagingDir || "projects/staging/");
    if (!fs.existsSync(stagingDir)) {
      fs.mkdirSync(stagingDir, { recursive: true });
    }
    const eventPath = path.join(stagingDir, `${Date.now()}-${trigger}.json`);
    fs.writeFileSync(eventPath, JSON.stringify(event, null, 2));
    return { eventPath, projectHint: summary.project_name || null };
  }
  function detectExplicitLifecycleUserCommand(text) {
    if (!text) return null;
    const lines = String(text).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    if (lines.length !== 1) return null;
    const normalized = lines[0].replace(/\[\[[^\]]+\]\]\s*/g, "").trim();
    const m = normalized.match(/^(?:\[[^\]]+\]\s*)?\/(new|reset|restart|compact)(?=\s|$)/i);
    if (!m) return null;
    return `/${m[1].toLowerCase()}`;
  }
  function latestMessageTimestampMs(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    let latest = null;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      const rec = msg;
      const raw = rec.timestamp ?? rec.createdAt ?? rec.time ?? null;
      if (raw == null) continue;
      let ts = null;
      if (typeof raw === "number" && Number.isFinite(raw)) ts = raw;
      else {
        const parsed = Date.parse(String(raw));
        if (Number.isFinite(parsed)) ts = parsed;
      }
      if (ts == null) continue;
      latest = latest == null ? ts : Math.max(latest, ts);
    }
    return latest;
  }
  function hasExplicitLifecycleUserCommand(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return false;
    for (const msg of messages) {
      if (!msg || typeof msg !== "object") continue;
      if (String(msg.role || "") !== "user") continue;
      const text = getMessageText(msg);
      if (!text) continue;
      if (detectExplicitLifecycleUserCommand(text)) return true;
    }
    return false;
  }
  function isBacklogLifecycleReplay(messages, trigger, nowMs, bootTimeMs, staleMs) {
    if (trigger !== "reset" && trigger !== "new" && trigger !== "recovery") return false;
    const latestTs = latestMessageTimestampMs(messages);
    if (latestTs == null) {
      return !hasExplicitLifecycleUserCommand(messages);
    }
    return latestTs < Math.min(nowMs, bootTimeMs) - staleMs;
  }
  function detectLifecycleSignal(messages) {
    if (!Array.isArray(messages) || messages.length === 0) return null;
    const tail = messages.slice(-8);
    for (let i = tail.length - 1; i >= 0; i--) {
      const msg = tail[i];
      if (!msg || typeof msg !== "object") continue;
      const role = String(msg.role || "").trim();
      const text = getMessageText(msg).trim();
      if (!text) continue;
      const normalized = text.replace(/\[\[[^\]]+\]\]\s*/g, "").replace(/^\[[^\]]+\]\s*/, "").trim();
      if (role === "user") {
        const command = detectExplicitLifecycleUserCommand(text);
        if (command === "/new" || command === "/reset" || command === "/restart") {
          return { label: "ResetSignal", source: "user_command", signature: `cmd:${command}` };
        }
        if (command === "/compact") {
          return { label: "CompactionSignal", source: "user_command", signature: `cmd:${command}` };
        }
      }
      if (role === "system") {
        const hasCompacted = /\bcompacted\b/i.test(normalized);
        const hasDelta = /\(\s*[\d.]+k?\s*(?:->|→)\s*[\d.]+k?\s*\)/i.test(normalized);
        const hasContext = /\bcontext\b/i.test(normalized);
        if (hasCompacted && (hasDelta || hasContext)) {
          return {
            label: "CompactionSignal",
            source: "system_notice",
            signature: `system:${normalized.toLowerCase()}`
          };
        }
      }
    }
    return null;
  }
  function lifecycleSignalKey(sessionId, label) {
    return `${sessionId}:${label}`;
  }
  function shouldProcessLifecycleSignal(sessionId, signal, suppressMs = 15e3, retentionMs = 10 * 6e4) {
    const now = Date.now();
    for (const [key2, value] of lifecycleSignalHistory.entries()) {
      if (now - value.seenAt > retentionMs) {
        lifecycleSignalHistory.delete(key2);
      }
    }
    const key = lifecycleSignalKey(sessionId, signal.label);
    const prior = lifecycleSignalHistory.get(key);
    lifecycleSignalHistory.set(key, { source: signal.source, signature: signal.signature, seenAt: now });
    if (!prior) return true;
    const ageMs = now - prior.seenAt;
    if (prior.signature === signal.signature && ageMs < suppressMs) return false;
    if (ageMs < suppressMs && prior.source === "hook" && signal.source === "system_notice") {
      return false;
    }
    return true;
  }
  function markLifecycleSignalFromHook(sessionId, label) {
    lifecycleSignalHistory.set(lifecycleSignalKey(sessionId, label), {
      source: "hook",
      signature: `hook:${label}`,
      seenAt: Date.now()
    });
  }
  function computeDynamicK() {
    const nodeCount = getActiveNodeCount();
    if (nodeCount < 10) return 5;
    const k = Math.round(11.5 * Math.log(nodeCount) - 61.7);
    return Math.max(5, Math.min(k, 40));
  }
  async function recallFromBridge(query, limit, expandGraph, graphDepth, domain, domainBoost, project, dateFrom, dateTo) {
    try {
      const args = [
        query,
        "--limit",
        String(limit),
        "--json"
      ];
      if (domain && typeof domain === "object") {
        args.push("--domain", JSON.stringify(domain));
      }
      if (domainBoost) {
        args.push("--domain-boost", JSON.stringify(domainBoost));
      }
      if (project) {
        args.push("--project", project);
      }
      if (dateFrom) {
        args.push("--date-from", dateFrom);
      }
      if (dateTo) {
        args.push("--date-to", dateTo);
      }
      let output;
      if (expandGraph) {
        args.push("--depth", String(graphDepth));
        output = await datastoreBridge.searchGraphAware(args);
      } else {
        output = await datastoreBridge.search(args);
      }
      return parseMemoryResults(output, expandGraph);
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.error("[quaid][facade] recall error:", err.message);
      return [];
    }
  }
  function parseMemoryResults(output, expandGraph) {
    const results = [];
    if (!output || !output.trim()) return results;
    try {
      const parsed = JSON.parse(output);
      const items = Array.isArray(parsed) ? parsed : parsed?.results || parsed?.items || [];
      for (const item of items) {
        if (!item || typeof item !== "object") continue;
        const text = String(item.text || "").trim();
        if (!text) continue;
        const domains = (() => {
          if (Array.isArray(item.domains)) return item.domains.map((d) => String(d || "").trim()).filter(Boolean);
          if (typeof item.domains === "string") {
            try {
              const p = JSON.parse(item.domains);
              if (Array.isArray(p)) return p;
            } catch {
            }
          }
          return void 0;
        })();
        results.push({
          text,
          category: String(item.category || "fact"),
          similarity: Number(item.similarity) || 0.5,
          id: item.id ? String(item.id) : void 0,
          domains,
          sourceType: item.source_type || item.sourceType || void 0,
          extractionConfidence: typeof item.extraction_confidence === "number" ? item.extraction_confidence : void 0,
          createdAt: item.created_at || item.createdAt || void 0,
          validFrom: item.valid_from || item.validFrom || void 0,
          validUntil: item.valid_until || item.validUntil || void 0,
          privacy: item.privacy || void 0,
          ownerId: item.owner_id || item.ownerId || void 0,
          via: expandGraph ? void 0 : "vector"
        });
      }
      if (expandGraph) {
        const rels = parsed?.relationships || parsed?.graph_results || [];
        for (const r of rels) {
          if (!r || typeof r !== "object") continue;
          const id = typeof r.id === "string" ? r.id : typeof r.id === "number" ? String(r.id) : "";
          const name = typeof r.name === "string" ? r.name : "";
          const relation = typeof r.relation === "string" ? r.relation : "";
          const direction = typeof r.direction === "string" ? r.direction : "out";
          const sourceName = typeof r.source_name === "string" ? r.source_name : "";
          if (!id || !name || !relation || !sourceName) continue;
          const text = direction === "in" ? `${name} --${relation}--> ${sourceName}` : `${sourceName} --${relation}--> ${name}`;
          results.push({
            text,
            category: "graph",
            similarity: 0.75,
            id,
            relation,
            direction,
            sourceName,
            via: "graph"
          });
        }
      }
    } catch {
      for (const line of output.split("\n")) {
        if (line.startsWith("[direct]")) {
          const match = line.match(/\[direct\]\s+\[(\d+\.\d+)\]\s+\[(\w+)\]\s+(.+)/);
          if (match) {
            results.push({
              text: match[3].trim(),
              category: match[2],
              similarity: parseFloat(match[1]),
              via: "vector"
            });
          }
        } else if (line.startsWith("[graph]")) {
          results.push({
            text: line.substring(7).trim(),
            category: "graph",
            similarity: 0.75,
            via: "graph"
          });
        }
      }
    }
    return results;
  }
  function recallFromJournal(query, limit, journalDir) {
    const stop = /* @__PURE__ */ new Set([
      "the",
      "and",
      "for",
      "with",
      "that",
      "this",
      "from",
      "have",
      "has",
      "was",
      "were",
      "what",
      "when",
      "where",
      "which",
      "who",
      "how",
      "why",
      "about",
      "tell",
      "me",
      "your",
      "my",
      "our",
      "their",
      "his",
      "her",
      "its",
      "into",
      "onto",
      "than",
      "then"
    ]);
    const tokens = Array.from(new Set(
      String(query || "").toLowerCase().replace(/[^a-z0-9\s]/g, " ").split(/\s+/).map((t) => t.trim()).filter((t) => t.length >= 3 && !stop.has(t))
    )).slice(0, 16);
    if (!tokens.length) return [];
    let files = [];
    try {
      files = fs.readdirSync(journalDir).filter((f) => f.endsWith(".journal.md"));
    } catch (err) {
      if (deps.isFailHardEnabled()) {
        throw new Error("[quaid][facade] Journal recall listing failed under failHard", { cause: err });
      }
      console.warn(`[quaid][facade] Journal recall listing failed: ${String(err?.message || err)}`);
      return [];
    }
    const scored = [];
    for (const file of files) {
      try {
        const fullPath = path.join(journalDir, file);
        const content = fs.readFileSync(fullPath, "utf8");
        const lc = content.toLowerCase();
        let hits = 0;
        for (const t of tokens) {
          if (lc.includes(t)) hits += 1;
        }
        if (hits === 0) continue;
        const excerpt = content.replace(/\s+/g, " ").trim().slice(0, 220);
        const similarity = Math.min(0.95, 0.45 + hits / Math.max(tokens.length, 1) * 0.5);
        scored.push({
          text: `${file}: ${excerpt}${content.length > 220 ? "..." : ""}`,
          category: "journal",
          similarity,
          via: "journal"
        });
      } catch (err) {
        if (deps.isFailHardEnabled()) {
          throw new Error(`[quaid][facade] Journal recall read failed for ${file} under failHard`, { cause: err });
        }
        console.warn(`[quaid][facade] Journal recall read failed for ${file}: ${String(err?.message || err)}`);
      }
    }
    scored.sort((a, b) => (b.similarity || 0) - (a.similarity || 0));
    return scored.slice(0, limit);
  }
  function parseProjectStoreResults(out, _query, _limit, _project) {
    if (!out || !out.trim()) return [];
    const results = [];
    const lines = out.split("\n");
    for (const line of lines) {
      const m = line.match(/^\d+\.\s+~?\/?([^\s>]+)\s+>\s+(.+?)\s+\(similarity:\s+([\d.]+)\)/);
      if (!m) continue;
      const sourcePath = m[1];
      const section = m[2].trim();
      const sim = Number.parseFloat(m[3]) || 0.6;
      results.push({
        text: `${sourcePath} > ${section}`,
        category: "project",
        similarity: sim,
        via: "project"
      });
    }
    return results;
  }
  const _memoryNotes = /* @__PURE__ */ new Map();
  const _memoryNotesTouchedAt = /* @__PURE__ */ new Map();
  const NOTES_DIR = path.join(deps.workspace, ".quaid", "runtime", "notes");
  function getNotesPath(sessionId) {
    return path.join(NOTES_DIR, `memory-notes-${sessionId}.json`);
  }
  function _sleepMs(ms) {
    const i32 = new Int32Array(new SharedArrayBuffer(4));
    Atomics.wait(i32, 0, 0, Math.max(1, Math.floor(ms)));
  }
  function withNotesLock(sessionId, fn) {
    const lockPath = `${getNotesPath(sessionId)}.lock`;
    let fd;
    let lastErr;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      try {
        fd = fs.openSync(lockPath, "wx", 384);
        break;
      } catch (err) {
        const code = err?.code;
        if (code !== "EEXIST") throw err;
        lastErr = err;
        _sleepMs(10);
      }
    }
    if (fd === void 0) {
      throw new Error(`failed to acquire memory-notes lock for session=${sessionId}: ${String(lastErr?.message || lastErr)}`);
    }
    try {
      return fn();
    } finally {
      try {
        fs.closeSync(fd);
      } catch {
      }
      try {
        fs.unlinkSync(lockPath);
      } catch {
      }
    }
  }
  function addMemoryNote(sessionId, text, category) {
    _memoryNotesTouchedAt.set(sessionId, Date.now());
    if (_memoryNotes.size >= MAX_MEMORY_NOTE_SESSIONS && !_memoryNotes.has(sessionId)) {
      const oldest = Array.from(_memoryNotesTouchedAt.entries()).sort((a, b) => a[1] - b[1])[0]?.[0];
      if (oldest) {
        _memoryNotes.delete(oldest);
        _memoryNotesTouchedAt.delete(oldest);
      }
    }
    if (!_memoryNotes.has(sessionId)) {
      _memoryNotes.set(sessionId, []);
    }
    const noteList = _memoryNotes.get(sessionId);
    noteList.push(`[${category}] ${text}`);
    if (noteList.length > MAX_MEMORY_NOTES_PER_SESSION) {
      noteList.splice(0, noteList.length - MAX_MEMORY_NOTES_PER_SESSION);
    }
    try {
      withNotesLock(sessionId, () => {
        const notesPath = getNotesPath(sessionId);
        let existing = [];
        try {
          existing = JSON.parse(fs.readFileSync(notesPath, "utf8"));
        } catch (err) {
          const msg = String(err?.message || err);
          if (!msg.includes("ENOENT") && deps.isFailHardEnabled()) throw err;
        }
        existing.push(`[${category}] ${text}`);
        fs.writeFileSync(notesPath, JSON.stringify(existing), { mode: 384 });
      });
    } catch (err) {
      if (deps.isFailHardEnabled()) throw err;
      console.warn(`[quaid][facade] memory note write failed for session ${sessionId}: ${String(err?.message || err)}`);
    }
  }
  function getAndClearMemoryNotes(sessionId) {
    return withNotesLock(sessionId, () => {
      const inMemory = _memoryNotes.get(sessionId) || [];
      let onDisk = [];
      const notesPath = getNotesPath(sessionId);
      try {
        onDisk = JSON.parse(fs.readFileSync(notesPath, "utf8"));
      } catch {
      }
      const merged = [.../* @__PURE__ */ new Set([...onDisk, ...inMemory])];
      _memoryNotes.delete(sessionId);
      _memoryNotesTouchedAt.delete(sessionId);
      try {
        fs.unlinkSync(notesPath);
      } catch {
      }
      return merged;
    });
  }
  async function recall(opts) {
    const {
      query,
      limit = 10,
      expandGraph = true,
      graphDepth = 1,
      datastores,
      routeStores,
      reasoning = "fast",
      intent = "general",
      ranking,
      domain = { all: true },
      domainBoost,
      project,
      dateFrom,
      dateTo,
      docs,
      datastoreOptions,
      failOpen
    } = opts;
    const selectedStores = normalizeKnowledgeDatastores(datastores, expandGraph);
    const shouldRouteStores = routeStores ?? !Array.isArray(datastores);
    const runRecall = (q) => {
      const recallOpts = {
        datastores: selectedStores,
        expandGraph,
        graphDepth,
        intent,
        ranking,
        domain,
        domainBoost,
        project,
        dateFrom,
        dateTo,
        docs,
        datastoreOptions
      };
      if (shouldRouteStores) {
        return knowledgeEngine.total_recall(q, limit, {
          ...recallOpts,
          reasoning,
          failOpen
        });
      }
      return knowledgeEngine.totalRecall(q, limit, recallOpts);
    };
    return runRecall(query);
  }
  function isLowInformationEntityNode(result) {
    if ((result.via || "vector") === "graph" || result.category === "graph") return false;
    const category = String(result.category || "").toLowerCase();
    if (!["person", "concept", "event", "entity"].includes(category)) return false;
    const text = String(result.text || "").trim();
    if (!text) return true;
    const words = text.split(/\s+/).filter(Boolean);
    if (words.length <= 2 && /^[A-Za-z][A-Za-z0-9'_-]*(?:\s+[A-Za-z][A-Za-z0-9'_-]*)?$/.test(text)) return true;
    return false;
  }
  function getConfiguredDomainIds() {
    try {
      const defs = deps.getMemoryConfig()?.retrieval?.domains;
      if (defs && typeof defs === "object" && !Array.isArray(defs)) {
        return Object.keys(defs).map((k) => String(k).trim()).filter(Boolean).sort();
      }
    } catch {
    }
    return [];
  }
  function normalizeToken(raw) {
    return String(raw || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  }
  function stemToken(token) {
    if (token.length > 6 && token.endsWith("ing")) return token.slice(0, -3);
    if (token.length > 5 && token.endsWith("ed")) return token.slice(0, -2);
    if (token.length > 4 && token.endsWith("s")) return token.slice(0, -1);
    return token;
  }
  function tokenizeQuery(query) {
    return String(query || "").split(/\s+/).map((part) => normalizeToken(part)).map((token) => stemToken(token)).filter((token) => token.length >= 3 && !RECALL_RETRY_STOPWORDS.has(token));
  }
  function temporalCuePresent(query) {
    const lowered = String(query || "").toLowerCase();
    const cues = ["latest", "current", "currently", "still", "now", "as of", "when", "last", "updated"];
    return cues.some((cue) => lowered.includes(cue));
  }
  function attributionCuePresent(query) {
    const lowered = String(query || "").toLowerCase();
    const cues = ["who", "whose", "did", "does", "said", "asked", "told", "mentioned", "attributed"];
    return cues.some((cue) => lowered.includes(cue));
  }
  function isVectorRecallResult(result) {
    const via = String(result.via || "").toLowerCase();
    return via === "vector" || via === "vector_basic" || via === "vector_technical";
  }
  function computeEntityCoverage(query, results) {
    const resultBlob = results.map((r) => `${String(r.text || "").toLowerCase()} ${String(r.sourceName || "").toLowerCase()}`).join(" ");
    const tokens = tokenizeQuery(query);
    if (!tokens.length) return 1;
    const matched = tokens.filter((token) => resultBlob.includes(token)).length;
    return matched / tokens.length;
  }
  function buildExpandedRecallQuery(query) {
    const tokens = tokenizeQuery(query);
    const expanded = new Set(tokens);
    for (const token of tokens) {
      expanded.add(stemToken(token));
    }
    if (temporalCuePresent(query)) {
      ["latest", "current", "timeline", "asof", "status"].forEach((t) => expanded.add(t));
    }
    if (attributionCuePresent(query)) {
      ["person", "speaker", "attribution"].forEach((t) => expanded.add(t));
    }
    const expansionTail = Array.from(expanded).slice(0, 16).join(" ");
    if (!expansionTail) return query;
    return `${query} ${expansionTail}`;
  }
  function shouldRetryRecall(query, results) {
    const reasons = [];
    if (!results.length) {
      reasons.push("no_results");
      return { retry: true, reasons };
    }
    const vectorResults = results.filter((r) => isVectorRecallResult(r));
    if (!vectorResults.length) {
      reasons.push("no_vector_hits");
      return { retry: true, reasons };
    }
    const avgSimilarity = vectorResults.reduce((sum, r) => sum + Number(r.similarity || 0), 0) / vectorResults.length;
    const maxSimilarity = Math.max(...vectorResults.map((r) => Number(r.similarity || 0)));
    if (avgSimilarity < 0.48 && maxSimilarity < 0.62) {
      reasons.push("low_similarity");
    }
    const coverage = computeEntityCoverage(query, results);
    if (coverage < 0.35) {
      reasons.push("low_entity_coverage");
    }
    if (temporalCuePresent(query)) {
      const hasTemporalFields = results.some((r) => Boolean(r.createdAt || r.validFrom || r.validUntil));
      if (!hasTemporalFields) reasons.push("missing_temporal_context");
    }
    return { retry: reasons.length > 0, reasons };
  }
  function mergeRecallResults(primary, secondary, limit) {
    const merged = /* @__PURE__ */ new Map();
    const upsert = (row) => {
      const key = String(row.id || `${row.category}:${row.text}`).trim();
      const current = merged.get(key);
      if (!current) {
        merged.set(key, row);
        return;
      }
      if (Number(row.similarity || 0) > Number(current.similarity || 0)) {
        merged.set(key, row);
      }
    };
    primary.forEach(upsert);
    secondary.forEach(upsert);
    return Array.from(merged.values()).sort((a, b) => Number(b.similarity || 0) - Number(a.similarity || 0)).slice(0, Math.max(1, limit));
  }
  async function recallWithToolRetry(opts) {
    const primary = await recall(opts);
    const query = String(opts.query || "");
    const limit = Math.max(1, Number(opts.limit || 10));
    const retryDecision = shouldRetryRecall(query, primary);
    if (!retryDecision.retry) return primary;
    const expanded = buildExpandedRecallQuery(query);
    if (expanded === query) return primary;
    console.log(
      `[quaid][facade][recall] retry reasons=${retryDecision.reasons.join(",")} expanded="${expanded.slice(0, 160)}"`
    );
    const secondary = await recall({ ...opts, query: expanded });
    return mergeRecallResults(primary, secondary, limit);
  }
  function formatMemoriesForInjection(memories) {
    if (!memories.length) return "";
    const sorted = [...memories].sort((a, b) => {
      if (!a.createdAt && !b.createdAt) return 0;
      if (!a.createdAt) return -1;
      if (!b.createdAt) return 1;
      return a.createdAt.localeCompare(b.createdAt);
    });
    const graphNodeHits = sorted.filter((m) => isLowInformationEntityNode(m));
    const regularMemories = sorted.filter((m) => !isLowInformationEntityNode(m));
    const lines = regularMemories.map((m) => {
      const conf = m.extractionConfidence ?? 0.5;
      const timestamp = m.createdAt ? ` (${m.createdAt.split("T")[0]})` : "";
      const domainLabel = Array.isArray(m.domains) && m.domains.length ? ` [domains:${m.domains.join(",")}]` : "";
      if (conf < 0.4) {
        return `- [${m.category}]${timestamp}${domainLabel} (uncertain) ${m.text}`;
      }
      return `- [${m.category}]${timestamp}${domainLabel} ${m.text}`;
    });
    if (graphNodeHits.length > 0) {
      const packed = graphNodeHits.slice(0, 8).map((m) => `${m.text} (${Math.round((m.similarity || 0) * 100)}%)`).join(", ");
      lines.push(`- [graph-node-hits] Entity node references (not standalone facts): ${packed}`);
    }
    const configuredDomains = getConfiguredDomainIds();
    const domainGuidance = configuredDomains.length ? `
DOMAIN RECALL RULE: Use memory_recall options.filters.domain (map of domain->bool). Example: {"technical": true}. Use domain filters only.
AVAILABLE_DOMAINS: ${configuredDomains.join(", ")}` : "";
    return `<injected_memories>
AUTOMATED MEMORY SYSTEM: The following memories were automatically retrieved from past conversations. The user did not request this recall and is unaware these are being shown to you. Use them as background context only. Items marked (uncertain) have lower extraction confidence. Dates shown are when the fact was recorded.
INJECTOR CONFIDENCE RULE: Treat injected memories as hints, not final truth. If the answer depends on personal details and the match is not exact/high-confidence, run memory_recall before answering.${domainGuidance}
${lines.join("\n")}
</injected_memories>`;
  }
  function notImplemented(name) {
    throw new Error(`[quaid][facade] ${name} is not yet implemented \u2014 scheduled for a future PR`);
  }
  return {
    // Pass-through
    getConfig: deps.getMemoryConfig,
    isSystemEnabled: deps.isSystemEnabled,
    isFailHardEnabled: deps.isFailHardEnabled,
    // Datastore
    stats: () => datastoreBridge.stats(),
    getStatsParsed,
    store: (args) => datastoreBridge.store(args),
    forget: (args) => datastoreBridge.forget(args),
    searchBySession: (sessionId, limit = 20) => datastoreBridge.search([
      "*",
      "--session-id",
      sessionId,
      "--owner",
      deps.resolveOwner(),
      "--limit",
      String(limit)
    ]),
    // Events
    emitEvent: async (name, payload, dispatch = "auto") => {
      const args = [
        "--name",
        name,
        "--payload",
        JSON.stringify(payload || {}),
        "--source",
        String(deps.eventSource || "adapter"),
        "--dispatch",
        dispatch
      ];
      const out = await deps.execEvents("emit", args);
      let parsed = null;
      try {
        parsed = JSON.parse(out || "{}");
      } catch (err) {
        const msg = String(err?.message || err);
        throw new Error(`[quaid][facade] events emit returned invalid JSON: ${msg}`);
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("[quaid][facade] events emit returned non-object payload");
      }
      return parsed;
    },
    // Recall
    recall,
    recallWithToolRetry,
    formatMemoriesForInjection,
    // Dynamic K
    computeDynamicK,
    getActiveNodeCount,
    // Docs
    docsSearch: (query, args) => deps.execDocsRag("search", [query, ...args]),
    docsRead: (identifier) => deps.execDocsRegistry("read", [identifier]),
    docsList: (args) => deps.execDocsRegistry("list", args),
    docsRegister: (args) => deps.execDocsRegistry("register", args),
    docsCreateProject: (args) => deps.execDocsRegistry("create-project", args),
    docsListProjects: (args = ["--json"]) => deps.execDocsRegistry("list-projects", args),
    docsCheckStaleness: () => deps.execDocsUpdater("check", ["--json"]),
    // Memory notes
    addMemoryNote,
    getAndClearMemoryNotes,
    // Project catalog
    getProjectCatalog: () => projectCatalogReader.getProjectCatalog(),
    getProjectNames: () => projectCatalogReader.getProjectNames(),
    // Guidance
    renderDatastoreGuidance: renderKnowledgeDatastoreGuidanceForAgents,
    getMessageText,
    buildTranscript,
    extractFilePaths,
    summarizeProjectSession,
    isResetBootstrapOnlyConversation,
    isVectorRecallResult,
    updateDocsFromTranscript,
    stageProjectEvent,
    // Stubs
    detectLifecycleSignal,
    latestMessageTimestampMs,
    hasExplicitLifecycleUserCommand,
    isBacklogLifecycleReplay,
    shouldProcessLifecycleSignal,
    markLifecycleSignalFromHook,
    clearLifecycleSignalHistory: () => lifecycleSignalHistory.clear(),
    processLifecycleEvent: () => notImplemented("processLifecycleEvent"),
    maybeRunMaintenance: () => notImplemented("maybeRunMaintenance"),
    getJanitorHealthIssue: () => {
      const stats = getDatastoreStatsSync(60 * 1e3);
      const completedAt = String(stats?.last_janitor_completed_at || "").trim();
      if (!completedAt) {
        return "[Quaid] Janitor has never run. Please run janitor and ensure schedule is active.";
      }
      const ts = Date.parse(completedAt);
      if (Number.isNaN(ts)) return null;
      const hours = (Date.now() - ts) / (1e3 * 60 * 60);
      if (hours > 72) {
        return `[Quaid] Janitor appears unhealthy (last successful run ${Math.floor(hours)}h ago). Diagnose scheduler/run path and run janitor.`;
      }
      if (hours > 48) {
        return `[Quaid] Janitor may be delayed (last successful run ${Math.floor(hours)}h ago). Verify schedule and run status.`;
      }
      return null;
    },
    queueDelayedRequest: () => notImplemented("queueDelayedRequest")
  };
}
export {
  createQuaidFacade
};
