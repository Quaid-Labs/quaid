#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { renderQuaidBanner } from "./lib/quaid_banner.mjs";

function tryImportClackPath() {
  const candidates = [];

  try {
    const which = spawnSync("sh", ["-c", "command -v clawdbot || command -v openclaw"], {
      stdio: ["ignore", "pipe", "ignore"],
      encoding: "utf8",
    });
    if (which.status === 0) {
      const cliBin = which.stdout.trim();
      if (cliBin) {
        try {
          const resolved = fs.realpathSync(cliBin);
          const pkgRoot = path.join(path.dirname(resolved), "..");
          candidates.push(path.join(pkgRoot, "node_modules", "@clack", "prompts", "dist", "index.mjs"));
        } catch {}
      }
    }
  } catch {}

  candidates.push(path.join(process.cwd(), "node_modules", "@clack", "prompts", "dist", "index.mjs"));

  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return "@clack/prompts";
}

const clack = await import(tryImportClackPath());
const { select, text, confirm, isCancel, cancel, note } = clack;

const C = {
  mag: (s) => `\x1b[38;5;170m${s}\x1b[0m`,
  cyan: (s) => `\x1b[36m${s}\x1b[0m`,
  bmag: (s) => `\x1b[1;38;5;200m${s}\x1b[0m`,
  bcyan: (s) => `\x1b[1;36m${s}\x1b[0m`,
  dim: (s) => `\x1b[2m${s}\x1b[0m`,
  bold: (s) => `\x1b[1m${s}\x1b[0m`,
};

function clearScreen() {
  if (process.stdout.isTTY) process.stdout.write("\x1B[2J\x1B[H");
}

function showBanner() {
  const lines = renderQuaidBanner(C, {
    subtitle: "config control surface",
    title: " INTERACTIVE CONFIG EDITOR ",
    topRightTail: "                                      ",
  });
  console.log(lines.join("\n"));
}

function bail(msg = "Cancelled") {
  cancel(msg);
  process.exit(1);
}

function handleCancel(v, msg = "Cancelled") {
  if (isCancel(v)) bail(msg);
  return v;
}

function workspaceRoot() {
  const envRoot = String(process.env.QUAID_HOME || process.env.CLAWDBOT_WORKSPACE || "").trim();
  if (envRoot) return envRoot;
  return process.cwd();
}

function configPath() {
  return path.join(workspaceRoot(), "config", "memory.json");
}

function loadConfig() {
  const p = configPath();
  if (!fs.existsSync(p)) throw new Error(`Config not found: ${p}`);
  return { path: p, data: JSON.parse(fs.readFileSync(p, "utf8")) };
}

function saveConfig(p, data) {
  const tmp = `${p}.tmp`;
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(tmp, `${JSON.stringify(data, null, 2)}\n`, "utf8");
  fs.renameSync(tmp, p);
}

function getPath(obj, dotted, fallback = undefined) {
  let cur = obj;
  for (const seg of dotted.split(".")) {
    if (!cur || typeof cur !== "object" || !(seg in cur)) return fallback;
    cur = cur[seg];
  }
  return cur;
}

function setPath(obj, dotted, value) {
  const parts = dotted.split(".");
  let cur = obj;
  for (const seg of parts.slice(0, -1)) {
    if (!cur[seg] || typeof cur[seg] !== "object") cur[seg] = {};
    cur = cur[seg];
  }
  cur[parts.at(-1)] = value;
}

function parseValue(raw) {
  const v = String(raw).trim();
  if (v === "true") return true;
  if (v === "false") return false;
  if (/^-?\d+$/.test(v)) return parseInt(v, 10);
  if (/^-?\d+\.\d+$/.test(v)) return parseFloat(v);
  if ((v.startsWith("[") && v.endsWith("]")) || (v.startsWith("{") && v.endsWith("}"))) {
    try { return JSON.parse(v); } catch {}
  }
  return v;
}

function normalizeProvider(provider) {
  return String(provider || "").trim().toLowerCase();
}

function gatewayProviderDefault() {
  try {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    if (!fs.existsSync(cfgPath)) return "";
    const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    const primary = String(cfg?.agents?.main?.modelPrimary || cfg?.agents?.defaults?.modelPrimary || "").trim();
    if (!primary.includes("/")) return "";
    return normalizeProvider(primary.split("/", 1)[0]);
  } catch {
    return "";
  }
}

function resolveEffectiveProvider(cfg) {
  const configured = normalizeProvider(getPath(cfg, "models.llmProvider", "default"));
  if (configured && configured !== "default") return configured;
  return gatewayProviderDefault() || "openai-codex";
}

function tierProviderKey(tier) {
  return tier === "deep" ? "models.deepReasoningProvider" : "models.fastReasoningProvider";
}

function resolveEffectiveTierProvider(cfg, tier) {
  const explicit = normalizeProvider(getPath(cfg, tierProviderKey(tier), "default"));
  if (explicit && explicit !== "default") return explicit;
  return resolveEffectiveProvider(cfg);
}

function providerDisplayName(provider) {
  const p = normalizeProvider(provider);
  if (p === "openai-codex") return "openai-codex (OpenAI Codex OAuth)";
  if (p === "openai") return "openai (OpenAI API)";
  if (p === "anthropic") return "anthropic (Anthropic API / Claude Code OAuth)";
  if (!p || p === "default") return "default";
  return p;
}

function resolveEffectiveEmbeddingsProvider(cfg) {
  const configured = normalizeProvider(getPath(cfg, "models.embeddingsProvider", "ollama"));
  if (configured && configured !== "default") return configured;
  const adapterType = String(getPath(cfg, "adapter.type", "openclaw")).trim().toLowerCase();
  if (adapterType === "openclaw") return "ollama";
  return "ollama";
}

function effectiveEmbeddingModel(cfg, provider) {
  if (provider === "ollama") return String(getPath(cfg, "ollama.embeddingModel", "qwen3-embedding:8b"));
  if (provider === "openai") return "provider-default";
  return "provider-default";
}

function defaultMappedModel(cfg, tier, provider) {
  const key = tier === "deep" ? "models.deepReasoningModelClasses" : "models.fastReasoningModelClasses";
  const map = getPath(cfg, key, {});
  return map?.[provider] || map?.[provider.replace(/-code$/, "")] || "(unmapped)";
}

function compactSummary(cfgPath, cfg) {
  const p = resolveEffectiveProvider(cfg);
  const deepProvider = resolveEffectiveTierProvider(cfg, "deep");
  const fastProvider = resolveEffectiveTierProvider(cfg, "fast");
  const ep = resolveEffectiveEmbeddingsProvider(cfg);
  const epModel = effectiveEmbeddingModel(cfg, ep);
  const configuredProvider = String(getPath(cfg, "models.llmProvider", "default"));
  const configuredEmbProvider = String(getPath(cfg, "models.embeddingsProvider", "ollama"));
  const deep = getPath(cfg, "models.deepReasoning", "default");
  const fast = getPath(cfg, "models.fastReasoning", "default");
  const deepDesc = deep === "default"
    ? `${deepProvider}:${defaultMappedModel(cfg, "deep", deepProvider)}`
    : `${deepProvider}:${deep}`;
  const fastDesc = fast === "default"
    ? `${fastProvider}:${defaultMappedModel(cfg, "fast", fastProvider)}`
    : `${fastProvider}:${fast}`;
  const notifyLevel = String(getPath(cfg, "notifications.level", "normal"));
  const janitorVerb = String(getPath(cfg, "notifications.janitor.verbosity", "inherit"));
  const extractionVerb = String(getPath(cfg, "notifications.extraction.verbosity", "inherit"));
  const retrievalVerb = String(getPath(cfg, "notifications.retrieval.verbosity", "inherit"));
  const janitorApplyMode = String(getPath(cfg, "janitor.applyMode", "auto"));
  const corePolicy = String(getPath(cfg, "janitor.approvalPolicies.coreMarkdownWrites", "ask"));
  const projectPolicy = String(getPath(cfg, "janitor.approvalPolicies.projectDocsWrites", "ask"));
  const workspacePolicy = String(getPath(cfg, "janitor.approvalPolicies.workspaceFileMovesDeletes", "ask"));
  const destructivePolicy = String(getPath(cfg, "janitor.approvalPolicies.destructiveMemoryOps", "auto"));
  const routerFailOpen = !!getPath(cfg, "retrieval.routerFailOpen", true);
  const autoCompactionOnTimeout = !!getPath(cfg, "capture.autoCompactionOnTimeout", true);

  const lines = [
    `${C.bold("Config")}: ${cfgPath}`,
    `${C.bold("Agent System")}: ${getPath(cfg, "adapter.type", "openclaw")} ${C.dim("(host integration layer)")}`,
    `${C.bold("Provider")}: ${configuredProvider} ${C.dim(`(default -> ${providerDisplayName(p)})`)}`,
    `${C.bold("Deep Provider")}: ${getPath(cfg, "models.deepReasoningProvider", "default")} ${C.dim(`(effective -> ${providerDisplayName(deepProvider)})`)}`,
    `${C.bold("Fast Provider")}: ${getPath(cfg, "models.fastReasoningProvider", "default")} ${C.dim(`(effective -> ${providerDisplayName(fastProvider)})`)}`,
    `${C.bold("Deep")}: ${deep} ${C.dim(`(${deepDesc})`)}`,
    `${C.bold("Fast")}: ${fast} ${C.dim(`(${fastDesc})`)}`,
    `${C.bold("Embeddings")}: ${configuredEmbProvider} ${C.dim(`(default -> ${ep} / ${epModel})`)}`,
    `${C.bold("Notifications")}: ${notifyLevel} ${C.dim(`(janitor:${janitorVerb} extraction:${extractionVerb} retrieval:${retrievalVerb})`)}`,
    `${C.bold("Janitor Apply")}: ${janitorApplyMode} ${C.dim("(legacy master policy)")}`,
    `${C.bold("Janitor Policies")}: core=${corePolicy} project=${projectPolicy} workspace=${workspacePolicy} destructive=${destructivePolicy}`,
    `${C.bold("Timeout")}: ${getPath(cfg, "capture.inactivityTimeoutMinutes", 10)}m`,
    `${C.bold("Auto-compact Timeout")}: ${autoCompactionOnTimeout ? "on" : "off"} ${C.dim("(trigger compaction after timeout extraction)")}`,
    `${C.bold("Pre-injection Pass")}: ${getPath(cfg, "retrieval.preInjectionPass", true) ? "on" : "off"} ${C.dim("(auto-inject total_recall planner)")}`,
    `${C.bold("Router Fail-Open")}: ${routerFailOpen ? "on" : "off"} ${C.dim("(on: noisy fallback to default recall plan if prepass fails)")}`,
  ];
  note(lines.join("\n"), "Current");
}

async function chooseWithCustom(message, options, current) {
  const normalized = options.filter(Boolean);
  const unique = [...new Map(normalized.map((o) => [o.value, o])).values()];
  unique.push({ value: "__custom__", label: "Custom…", hint: "Type your own value" });

  const picked = handleCancel(await select({
    message,
    options: unique,
    initialValue: current,
  }));
  if (picked !== "__custom__") return picked;

  const custom = handleCancel(await text({
    message: `${message} (custom)` ,
    placeholder: String(current || ""),
    validate: (v) => String(v || "").trim() ? undefined : "Value required",
  }));
  return String(custom).trim();
}

function providerOptions(cfg) {
  const gwProvider = providerDisplayName(gatewayProviderDefault() || "openai-codex");
  return [
    {
      value: "default",
      label: "default",
      hint: `uses provider set in Agent system: OpenClaw (${gwProvider})`,
    },
    { value: "openai", label: "openai", hint: "OpenAI API" },
    { value: "openai-codex", label: "openai-codex", hint: "OpenAI Codex OAuth" },
    { value: "anthropic", label: "anthropic", hint: "Anthropic API / Claude Code OAuth" },
  ];
}

function modelOptions(cfg, tier) {
  const effective = resolveEffectiveTierProvider(cfg, tier);
  const mapped = defaultMappedModel(cfg, tier, effective);
  const value = tier === "deep" ? "models.deepReasoning" : "models.fastReasoning";
  const isAnthropic = effective.startsWith("anthropic");
  const fallback = isAnthropic
    ? (tier === "deep" ? ["claude-opus-4-6", "claude-opus-4-5"] : ["claude-haiku-4-5", "claude-sonnet-4-20250514"])
    : (tier === "deep" ? ["gpt-5.3-codex", "gpt-5.2-codex", "gpt-4o"] : ["gpt-5.1-codex-mini", "gpt-4o-mini"]);
  const mapKey = tier === "deep" ? "models.deepReasoningModelClasses" : "models.fastReasoningModelClasses";
  const mappedForProvider = String(getPath(cfg, `${mapKey}.${effective}`, "") || "").trim();
  const mapValues = mappedForProvider ? [mappedForProvider] : [];
  const opts = [
    {
      value: "default",
      label: "default",
      hint: `provider ${effective}: ${mapped}`,
    },
    ...[...new Set([...fallback, ...mapValues])].map((m) => ({ value: m, label: m })),
  ];
  return { key: value, options: opts };
}

function tierProviderOptions(cfg, tier) {
  const effectiveBase = resolveEffectiveProvider(cfg);
  const current = getPath(cfg, tierProviderKey(tier), "default");
  return {
    key: tierProviderKey(tier),
    current,
    options: [
      {
        value: "default",
        label: "default",
        hint: `inherits LLM provider (${providerDisplayName(effectiveBase)})`,
      },
      { value: "openai", label: "openai", hint: "OpenAI API" },
      { value: "openai-codex", label: "openai-codex", hint: "OpenAI Codex OAuth" },
      { value: "anthropic", label: "anthropic", hint: "Anthropic API / Claude Code OAuth" },
    ],
  };
}

function notificationOptions() {
  return [
    { value: "quiet", label: "quiet", hint: "errors only" },
    { value: "normal", label: "normal", hint: "janitor summary + extraction summary; retrieval off" },
    { value: "verbose", label: "verbose", hint: "janitor full + extraction/retrieval summary" },
    { value: "debug", label: "debug", hint: "full detail + diagnostics" },
  ];
}

function notificationVerbosityOptions() {
  return [
    { value: "off", label: "off", hint: "disable this notification type" },
    { value: "summary", label: "summary", hint: "short operational messages" },
    { value: "full", label: "full", hint: "full detail (debug-heavy)" },
  ];
}

function janitorApplyOptions() {
  return [
    { value: "auto", label: "auto", hint: "janitor applies changes when run with --apply" },
    { value: "ask", label: "ask", hint: "requires explicit --approve for apply runs" },
    { value: "dry_run", label: "dry_run", hint: "always dry-run, never mutates files/db" },
  ];
}

function janitorScopePolicyOptions() {
  return [
    { value: "ask", label: "ask", hint: "queue request, ask user before applying" },
    { value: "auto", label: "auto", hint: "apply immediately when janitor runs" },
  ];
}

function systemRows(cfg) {
  return [
    { key: "memory", desc: "fact extraction + recall tools" },
    { key: "journal", desc: "snippets + journal distillation" },
    { key: "projects", desc: "project docs indexing/updates" },
    { key: "workspace", desc: "workspace health + housekeeping" },
  ].map((s) => ({ ...s, on: !!getPath(cfg, `systems.${s.key}`, true) }));
}

async function editSystems(cfg, cfgPath) {
  while (true) {
    clearScreen();
    showBanner();
    compactSummary(cfgPath, cfg);
    const rows = systemRows(cfg);
    const action = handleCancel(await select({
      message: "Systems (toggle)",
      options: [
        ...rows.map((r) => ({
          value: r.key,
          label: `${r.key.padEnd(10, " ")} ${r.on ? "on" : "off"}`,
          hint: r.desc,
        })),
        { value: "back", label: "Back" },
      ],
    }));

    if (action === "back") return;
    setPath(cfg, `systems.${action}`, !getPath(cfg, `systems.${action}`, true));
  }
}

async function runEdit() {
  const { path: cfgPath, data: cfg } = loadConfig();

  while (true) {
    clearScreen();
    showBanner();
    compactSummary(cfgPath, cfg);

    const menu = handleCancel(await select({
      message: "Config menu",
      options: [
        { value: "agent", label: "Agent system", hint: "Host integration layer (currently OpenClaw-first)" },
        { value: "provider", label: "LLM provider", hint: "Routing class for deep/fast reasoning" },
        { value: "deep_provider", label: "Deep reasoning provider", hint: "optional override for deep tier" },
        { value: "deep", label: "Deep reasoning model", hint: "deep extraction/review" },
        { value: "fast_provider", label: "Fast reasoning provider", hint: "optional override for fast tier" },
        { value: "fast", label: "Fast reasoning model", hint: "cheap utility/rerank calls" },
        { value: "emb_provider", label: "Embeddings provider", hint: "vector model host" },
        { value: "emb_model", label: "Embeddings model", hint: "semantic retrieval vectors" },
        { value: "notify", label: "Notification level", hint: "master level; feature verbosities may override" },
        { value: "notify_recommended", label: "Notifications: apply recommended", hint: "janitor=summary extraction=summary retrieval=off" },
        { value: "notify_janitor", label: "Notifications: janitor", hint: "off/summary/full" },
        { value: "notify_extraction", label: "Notifications: extraction", hint: "off/summary/full" },
        { value: "notify_retrieval", label: "Notifications: retrieval", hint: "off/summary/full" },
        { value: "pre_injection_pass", label: "Recall: pre-injection pass", hint: "auto-inject uses total_recall planner (recommended on)" },
        { value: "router_fail_open", label: "Recall: router fail-open", hint: "on = fallback to default recall plan if prepass fails (recommended)" },
        { value: "janitor_policy_core", label: "Janitor: core markdown policy", hint: "root markdown writes (SOUL/USER/MEMORY/TOOLS)" },
        { value: "janitor_policy_project", label: "Janitor: project docs policy", hint: "project docs outside projects/quaid" },
        { value: "janitor_policy_workspace", label: "Janitor: workspace move/delete policy", hint: "workspace audit file moves/deletes" },
        { value: "janitor_policy_destructive", label: "Janitor: destructive memory policy", hint: "merges/supersedes/deletes in memory DB" },
        { value: "janitor_apply", label: "Janitor apply mode (legacy)", hint: "master fallback: auto/ask/dry-run-only" },
        { value: "timeout", label: "Inactivity timeout", hint: "minutes before timeout extraction" },
        { value: "timeout_auto_compact", label: "Timeout auto-compaction", hint: "on/off compaction after timeout extraction (recommended on)" },
        { value: "systems", label: "Systems on/off", hint: "feature gates" },
        { value: "save", label: "Save and exit" },
        { value: "discard", label: "Exit without saving" },
      ],
    }));

    if (menu === "agent") {
      const next = handleCancel(await select({
        message: "adapter.type",
        initialValue: getPath(cfg, "adapter.type", "openclaw"),
        options: [
          { value: "openclaw", label: "openclaw", hint: "recommended; gateway-integrated runtime" },
          { value: "standalone", label: "standalone", hint: "local-only mode (advanced)" },
        ],
      }));
      setPath(cfg, "adapter.type", next);
    } else if (menu === "provider") {
      const next = await chooseWithCustom("models.llmProvider", providerOptions(cfg), getPath(cfg, "models.llmProvider", "default"));
      setPath(cfg, "models.llmProvider", next);
    } else if (menu === "deep_provider") {
      const def = tierProviderOptions(cfg, "deep");
      const next = await chooseWithCustom(def.key, def.options, def.current);
      setPath(cfg, def.key, next);
    } else if (menu === "deep") {
      const def = modelOptions(cfg, "deep");
      const next = await chooseWithCustom("models.deepReasoning", def.options, getPath(cfg, def.key, "default"));
      setPath(cfg, def.key, next);
    } else if (menu === "fast_provider") {
      const def = tierProviderOptions(cfg, "fast");
      const next = await chooseWithCustom(def.key, def.options, def.current);
      setPath(cfg, def.key, next);
    } else if (menu === "fast") {
      const def = modelOptions(cfg, "fast");
      const next = await chooseWithCustom("models.fastReasoning", def.options, getPath(cfg, def.key, "default"));
      setPath(cfg, def.key, next);
    } else if (menu === "emb_provider") {
      const inferred = resolveEffectiveEmbeddingsProvider(cfg);
      const next = await chooseWithCustom(
        "models.embeddingsProvider",
        [
          { value: "ollama", label: "ollama", hint: "local embeddings runtime" },
          { value: "openai", label: "openai", hint: "OpenAI API embeddings" },
          { value: "default", label: "default", hint: `adapter-selected default (${inferred})` },
        ],
        getPath(cfg, "models.embeddingsProvider", "ollama"),
      );
      setPath(cfg, "models.embeddingsProvider", next);
    } else if (menu === "emb_model") {
      const next = await chooseWithCustom(
        "ollama.embeddingModel",
        [
          { value: "qwen3-embedding:8b", label: "qwen3-embedding:8b", hint: "high-quality local embedding" },
          { value: "nomic-embed-text", label: "nomic-embed-text", hint: "fast baseline" },
          { value: "bge-large", label: "bge-large" },
          { value: "mxbai-embed-large", label: "mxbai-embed-large" },
          { value: "all-minilm", label: "all-minilm" },
        ],
        getPath(cfg, "ollama.embeddingModel", "qwen3-embedding:8b"),
      );
      setPath(cfg, "ollama.embeddingModel", next);
    } else if (menu === "notify") {
      const next = handleCancel(await select({
        message: "notifications.level",
        initialValue: getPath(cfg, "notifications.level", "normal"),
        options: notificationOptions(),
      }));
      setPath(cfg, "notifications.level", next);
    } else if (menu === "notify_recommended") {
      setPath(cfg, "notifications.level", "normal");
      setPath(cfg, "notifications.janitor.verbosity", "summary");
      setPath(cfg, "notifications.extraction.verbosity", "summary");
      setPath(cfg, "notifications.retrieval.verbosity", "off");
    } else if (menu === "notify_janitor") {
      const next = handleCancel(await select({
        message: "notifications.janitor.verbosity",
        initialValue: getPath(cfg, "notifications.janitor.verbosity", "summary"),
        options: notificationVerbosityOptions(),
      }));
      setPath(cfg, "notifications.janitor.verbosity", next);
    } else if (menu === "notify_extraction") {
      const next = handleCancel(await select({
        message: "notifications.extraction.verbosity",
        initialValue: getPath(cfg, "notifications.extraction.verbosity", "summary"),
        options: notificationVerbosityOptions(),
      }));
      setPath(cfg, "notifications.extraction.verbosity", next);
    } else if (menu === "notify_retrieval") {
      const next = handleCancel(await select({
        message: "notifications.retrieval.verbosity",
        initialValue: getPath(cfg, "notifications.retrieval.verbosity", "off"),
        options: notificationVerbosityOptions(),
      }));
      setPath(cfg, "notifications.retrieval.verbosity", next);
    } else if (menu === "pre_injection_pass") {
      const current = !!getPath(cfg, "retrieval.preInjectionPass", true);
      const next = handleCancel(await select({
        message: "retrieval.preInjectionPass",
        initialValue: current ? "on" : "off",
        options: [
          { value: "on", label: "on", hint: "auto-inject uses total_recall (fast planner + routed stores)" },
          { value: "off", label: "off", hint: "auto-inject uses plain recall on vector_basic + graph" },
        ],
      }));
      setPath(cfg, "retrieval.preInjectionPass", next === "on");
    } else if (menu === "router_fail_open") {
      const current = !!getPath(cfg, "retrieval.routerFailOpen", true);
      const next = handleCancel(await select({
        message: "retrieval.routerFailOpen",
        initialValue: current ? "on" : "off",
        options: [
          { value: "on", label: "on", hint: "fail-open: log error and continue with deterministic default recall plan (recommended)" },
          { value: "off", label: "off", hint: "strict mode: router failure raises error" },
        ],
      }));
      setPath(cfg, "retrieval.routerFailOpen", next === "on");
    } else if (menu === "janitor_policy_core") {
      const next = handleCancel(await select({
        message: "janitor.approvalPolicies.coreMarkdownWrites",
        initialValue: getPath(cfg, "janitor.approvalPolicies.coreMarkdownWrites", "ask"),
        options: janitorScopePolicyOptions(),
      }));
      setPath(cfg, "janitor.approvalPolicies.coreMarkdownWrites", next);
    } else if (menu === "janitor_policy_project") {
      const next = handleCancel(await select({
        message: "janitor.approvalPolicies.projectDocsWrites",
        initialValue: getPath(cfg, "janitor.approvalPolicies.projectDocsWrites", "ask"),
        options: janitorScopePolicyOptions(),
      }));
      setPath(cfg, "janitor.approvalPolicies.projectDocsWrites", next);
    } else if (menu === "janitor_policy_workspace") {
      const next = handleCancel(await select({
        message: "janitor.approvalPolicies.workspaceFileMovesDeletes",
        initialValue: getPath(cfg, "janitor.approvalPolicies.workspaceFileMovesDeletes", "ask"),
        options: janitorScopePolicyOptions(),
      }));
      setPath(cfg, "janitor.approvalPolicies.workspaceFileMovesDeletes", next);
    } else if (menu === "janitor_policy_destructive") {
      const next = handleCancel(await select({
        message: "janitor.approvalPolicies.destructiveMemoryOps",
        initialValue: getPath(cfg, "janitor.approvalPolicies.destructiveMemoryOps", "auto"),
        options: janitorScopePolicyOptions(),
      }));
      setPath(cfg, "janitor.approvalPolicies.destructiveMemoryOps", next);
    } else if (menu === "janitor_apply") {
      const next = handleCancel(await select({
        message: "janitor.applyMode",
        initialValue: getPath(cfg, "janitor.applyMode", "auto"),
        options: janitorApplyOptions(),
      }));
      setPath(cfg, "janitor.applyMode", next);
    } else if (menu === "timeout") {
      const next = handleCancel(await text({
        message: "capture.inactivityTimeoutMinutes",
        placeholder: String(getPath(cfg, "capture.inactivityTimeoutMinutes", 10)),
        validate: (v) => /^\d+$/.test(String(v || "").trim()) ? undefined : "Enter a whole number",
      }));
      setPath(cfg, "capture.inactivityTimeoutMinutes", parseInt(String(next).trim(), 10));
    } else if (menu === "timeout_auto_compact") {
      const next = handleCancel(await confirm({
        message: "capture.autoCompactionOnTimeout",
        initialValue: !!getPath(cfg, "capture.autoCompactionOnTimeout", true),
      }));
      setPath(cfg, "capture.autoCompactionOnTimeout", !!next);
    } else if (menu === "systems") {
      await editSystems(cfg, cfgPath);
    } else if (menu === "save") {
      saveConfig(cfgPath, cfg);
      console.log(`Saved: ${cfgPath}`);
      return;
    } else if (menu === "discard") {
      const ok = handleCancel(await confirm({ message: "Discard unsaved changes?", initialValue: false }));
      if (ok) {
        console.log("No changes saved");
        return;
      }
    }
  }
}

function showConfig() {
  const { path: cfgPath, data: cfg } = loadConfig();
  const effective = resolveEffectiveProvider(cfg);
  const effectiveDeepProvider = resolveEffectiveTierProvider(cfg, "deep");
  const effectiveFastProvider = resolveEffectiveTierProvider(cfg, "fast");
  const effectiveEmb = resolveEffectiveEmbeddingsProvider(cfg);
  const effectiveEmbModel = effectiveEmbeddingModel(cfg, effectiveEmb);
  const deep = getPath(cfg, "models.deepReasoning", "default");
  const fast = getPath(cfg, "models.fastReasoning", "default");

  console.log("Quaid Configuration");
  console.log(cfgPath);
  console.log("");
  console.log(`agent system:     ${getPath(cfg, "adapter.type", "openclaw")}`);
  console.log(`provider:         ${getPath(cfg, "models.llmProvider", "default")} (default -> ${providerDisplayName(effective)})`);
  console.log(`deep provider:    ${getPath(cfg, "models.deepReasoningProvider", "default")} (effective -> ${providerDisplayName(effectiveDeepProvider)})`);
  console.log(`deep reasoning:   ${deep}`);
  console.log(`fast provider:    ${getPath(cfg, "models.fastReasoningProvider", "default")} (effective -> ${providerDisplayName(effectiveFastProvider)})`);
  console.log(`fast reasoning:   ${fast}`);
  console.log(`embeddings:       ${getPath(cfg, "models.embeddingsProvider", "ollama")} (default -> ${effectiveEmb})`);
  console.log(`embeddings model: ${effectiveEmbModel}`);
  console.log(`notify level:     ${getPath(cfg, "notifications.level", "normal")} (janitor:${getPath(cfg, "notifications.janitor.verbosity", "inherit")} extraction:${getPath(cfg, "notifications.extraction.verbosity", "inherit")} retrieval:${getPath(cfg, "notifications.retrieval.verbosity", "inherit")})`);
  console.log(`janitor apply:    ${getPath(cfg, "janitor.applyMode", "auto")}`);
  console.log(`janitor policies: core=${getPath(cfg, "janitor.approvalPolicies.coreMarkdownWrites", "ask")} project=${getPath(cfg, "janitor.approvalPolicies.projectDocsWrites", "ask")} workspace=${getPath(cfg, "janitor.approvalPolicies.workspaceFileMovesDeletes", "ask")} destructive=${getPath(cfg, "janitor.approvalPolicies.destructiveMemoryOps", "auto")}`);
  console.log(`idle timeout:     ${getPath(cfg, "capture.inactivityTimeoutMinutes", 10)}m`);
  console.log(`timeout compact:  ${getPath(cfg, "capture.autoCompactionOnTimeout", true) ? "on" : "off"}`);
  console.log("\nsystems:");
  for (const row of systemRows(cfg)) {
    console.log(`  ${row.key.padEnd(10, " ")} ${row.on ? "on" : "off"}  # ${row.desc}`);
  }
}

function setConfig(dotted, raw) {
  const { path: cfgPath, data: cfg } = loadConfig();
  setPath(cfg, dotted, parseValue(raw));
  saveConfig(cfgPath, cfg);
  console.log(`Set ${dotted} in ${cfgPath}`);
}

function usage() {
  console.log("Usage: quaid config [show|edit|path|set <dotted.key> <value>]");
}

async function main() {
  const cmd = process.argv[2] || "show";
  if (cmd === "path") {
    console.log(configPath());
    return;
  }
  if (cmd === "show") {
    showConfig();
    return;
  }
  if (cmd === "edit") {
    await runEdit();
    return;
  }
  if (cmd === "set") {
    const key = process.argv[3];
    const value = process.argv.slice(4).join(" ");
    if (!key || !value) {
      usage();
      process.exit(1);
    }
    setConfig(key, value);
    return;
  }

  usage();
  process.exit(1);
}

main().catch((err) => {
  console.error(err?.message || String(err));
  process.exit(1);
});
