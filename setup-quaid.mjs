#!/usr/bin/env node
// =============================================================================
// Quaid Knowledge Layer Plugin — Guided Installer
// =============================================================================
// Interactive installer using @clack/prompts (resolved from OpenClaw).
// Supports two modes:
//   - Standalone (default): Uses QUAID_HOME env or ~/quaid/ as home directory
//   - OpenClaw: detected via CLAWDBOT_WORKSPACE env or clawdbot/openclaw on PATH
//
// Author: Steadman Labs (https://github.com/quaid-labs)
// License: MIT
// =============================================================================

import { execSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { renderQuaidBanner } from "./lib/quaid_banner.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function parseInstallArgs(argv) {
  const opts = {
    workspace: "",
    ownerName: "",
    source: "",
    ref: "",
    githubRepo: "",
    artifact: "",
    agent: false,
    claudeCode: false,
    help: false,
    errors: [],
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--workspace") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--workspace requires a path value");
      } else {
        opts.workspace = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--workspace=")) {
      const value = arg.slice("--workspace=".length);
      if (!value) {
        opts.errors.push("--workspace requires a non-empty path");
      } else {
        opts.workspace = value;
      }
      continue;
    }
    if (arg === "--agent") {
      opts.agent = true;
      continue;
    }
    if (arg === "--source") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--source requires a value (local|github|artifact)");
      } else {
        opts.source = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--source=")) {
      const value = arg.slice("--source=".length);
      if (!value) {
        opts.errors.push("--source requires a non-empty value");
      } else {
        opts.source = value;
      }
      continue;
    }
    if (arg === "--ref") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--ref requires a value");
      } else {
        opts.ref = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--ref=")) {
      const value = arg.slice("--ref=".length);
      if (!value) {
        opts.errors.push("--ref requires a non-empty value");
      } else {
        opts.ref = value;
      }
      continue;
    }
    if (arg === "--github-repo") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--github-repo requires a value (owner/repo)");
      } else {
        opts.githubRepo = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--github-repo=")) {
      const value = arg.slice("--github-repo=".length);
      if (!value) {
        opts.errors.push("--github-repo requires a non-empty value");
      } else {
        opts.githubRepo = value;
      }
      continue;
    }
    if (arg === "--artifact") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--artifact requires a URL or local file path");
      } else {
        opts.artifact = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--artifact=")) {
      const value = arg.slice("--artifact=".length);
      if (!value) {
        opts.errors.push("--artifact requires a non-empty value");
      } else {
        opts.artifact = value;
      }
      continue;
    }
    if (arg === "--owner-name") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--owner-name requires a value");
      } else {
        opts.ownerName = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--owner-name=")) {
      const value = arg.slice("--owner-name=".length);
      if (!value) {
        opts.errors.push("--owner-name requires a non-empty value");
      } else {
        opts.ownerName = value;
      }
      continue;
    }
    if (arg === "--claude-code") {
      opts.claudeCode = true;
      continue;
    }
    if (arg === "-h" || arg === "--help") {
      opts.help = true;
      continue;
    }
    opts.errors.push(`Unknown option: ${arg}`);
  }
  return opts;
}

function printUsageAndExit() {
  console.log(`Usage: node setup-quaid.mjs [options]

Options:
  --workspace <path>  Override workspace/home path (highest priority)
  --owner-name <name> Person name used to tag memories (recommended for --agent)
  --source <kind>     Plugin source: local (default), github, artifact
  --ref <git-ref>     Git ref/commit to install when --source github
  --github-repo <r>   GitHub repo for github source (default: quaid-labs/quaid)
  --artifact <path>   Local file path or URL to .tar.gz when --source artifact
  --agent             Non-interactive agent mode (accepts sane defaults)
  --claude-code       Install for Claude Code (hooks + OAuth provider)
  -h, --help          Show this help
`);
  process.exit(0);
}

const INSTALL_ARGS = parseInstallArgs(process.argv.slice(2));
if (INSTALL_ARGS.help) printUsageAndExit();
if (INSTALL_ARGS.errors.length) {
  console.error("[x] Invalid installer arguments:");
  for (const err of INSTALL_ARGS.errors) console.error(`    - ${err}`);
  console.error("    Use --help for usage.");
  process.exit(2);
}
const INSTALL_SOURCE = String(INSTALL_ARGS.source || process.env.QUAID_INSTALL_SOURCE || "local").trim().toLowerCase();
const INSTALL_REF = String(INSTALL_ARGS.ref || process.env.QUAID_INSTALL_REF || "main").trim();
const INSTALL_GITHUB_REPO = String(INSTALL_ARGS.githubRepo || process.env.QUAID_INSTALL_GITHUB_REPO || "quaid-labs/quaid").trim();
const INSTALL_ARTIFACT = String(INSTALL_ARGS.artifact || process.env.QUAID_INSTALL_ARTIFACT || "").trim();
if (!["local", "github", "artifact"].includes(INSTALL_SOURCE)) {
  console.error(`[x] Invalid --source: ${INSTALL_SOURCE}`);
  console.error("    Expected one of: local, github, artifact");
  process.exit(2);
}
if (INSTALL_SOURCE === "github" && !INSTALL_REF) {
  console.error("[x] --source github requires --ref (or QUAID_INSTALL_REF).");
  process.exit(2);
}
if (INSTALL_SOURCE === "artifact" && !INSTALL_ARTIFACT) {
  console.error("[x] --source artifact requires --artifact (or QUAID_INSTALL_ARTIFACT).");
  process.exit(2);
}

// --- Constants ---
const VERSION = "0.2.15-alpha";
const HOOKS_PR_URL = "https://github.com/openclaw/openclaw/releases/tag/v2026.3.7";
const MIN_GATEWAY_VERSION = "2026.3.7";
const PROJECT_URL = "https://github.com/quaid-labs/quaid";
const TOTAL_INSTALL_STEPS = 7;

// AI agents: this is the canonical pre-install survey contract.
// Only include fields listed here when presenting a survey to a human.
// Everything not listed here is out of scope for the survey.
const AGENT_SURVEY_CONTRACT = {
  sourceOfTruth: "setup-quaid.mjs",
  rule: "Only the fields listed here belong in the pre-install survey.",
  fields: [
    {
      id: "owner_name",
      label: "Owner name",
      source: "step2_owner()",
      required: true,
      notes: [
        "Use the human's real name, not the system username.",
      ],
    },
    {
      id: "workspace_path",
      label: "Workspace path",
      source: "WORKSPACE resolution + installer args",
      required: true,
    },
    {
      id: "adapter_type",
      label: "Adapter type",
      source: "adapter detection in step3_models()",
      required: true,
    },
    {
      id: "llm_models",
      label: "LLM provider + deep/fast models",
      source: "step3_models()",
      required: true,
      notes: [
        "For supported Anthropic/OpenAI lanes, include installer defaults unless the user overrides them.",
        "For unsupported/custom lanes, collect explicit deep and fast model IDs from the user.",
      ],
    },
    {
      id: "embeddings",
      label: "Embeddings provider/model",
      source: "step4_embeddings()",
      required: true,
      notes: [
        "Include the RAM snapshot used for recommendation.",
        "Include whether Ollama is installed/running.",
        "Include whether the installer will attempt Ollama install/start.",
        "If proceeding without Ollama, require explicit user approval because recall degrades.",
      ],
    },
    {
      id: "notifications",
      label: "Notification level + per-feature verbosity",
      source: "step3_models() notification prompts",
      required: true,
      notes: [
        "If a non-default level requires Advanced Setup, state that explicitly in the survey.",
      ],
    },
    {
      id: "notification_channel",
      label: "Notification routing channel",
      source: "resolvePinnedNotificationRoute() + installer env overrides",
      required: true,
      notes: [
        "For OpenClaw installs, survey the explicit runtime notification channel.",
        "Do not rely on implicit last_used during install.",
      ],
    },
    {
      id: "janitor",
      label: "Janitor apply mode/policies",
      source: "step3_models() + step6_schedule()",
      required: true,
    },
    {
      id: "janitor_schedule",
      label: "Janitor schedule choice",
      source: "step6_schedule()",
      required: true,
    },
  ],
  notes: [
    "Do not add survey sections for internal installer steps with no user choice.",
    "Do not use test-only controls like QUAID_TEST_ANSWERS in normal AI install guidance unless explicitly running a test harness.",
    "Workspace file import is not a standalone survey field unless the installer actually prompts for it.",
  ],
};
// Detect mode: OpenClaw (has gateway+agent infra) vs Standalone (just Quaid)
function which(cmd) {
  return spawnSync("sh", ["-c", `command -v '${cmd.replace(/'/g, "'\\''")}'`], { stdio: "pipe" }).status === 0;
}
function readWorkspaceFromOpenClawConfig() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const ws = parsed?.workspace || parsed?.agents?.defaults?.workspace || "";
    return typeof ws === "string" ? ws.trim() : "";
  } catch {
    return "";
  }
}
function detectWorkspaceFromCli() {
  return (
    shell("clawdbot config get workspace 2>/dev/null </dev/null") ||
    shell("openclaw config get workspace 2>/dev/null </dev/null") ||
    readWorkspaceFromOpenClawConfig()
  );
}
const IS_CLAUDE_CODE = INSTALL_ARGS.claudeCode || process.env.QUAID_INSTALL_CLAUDE_CODE === "1";
const IS_OPENCLAW = !IS_CLAUDE_CODE && !!(process.env.CLAWDBOT_WORKSPACE || which("clawdbot") || which("openclaw"));
const WORKSPACE =
  INSTALL_ARGS.workspace ||
  process.env.QUAID_WORKSPACE ||
  process.env.QUAID_HOME ||
  process.env.CLAWDBOT_WORKSPACE ||
  detectWorkspaceFromCli() ||
  path.join(os.homedir(), "quaid");
const AGENT_MODE = INSTALL_ARGS.agent || process.env.QUAID_INSTALL_AGENT === "1" || !process.stdin.isTTY;
const MODULES_PLUGIN_DIR = path.join(WORKSPACE, "modules", "quaid");
const LEGACY_PLUGIN_DIR = path.join(WORKSPACE, "plugins", "quaid");
const PLUGIN_DIR = fs.existsSync(path.join(MODULES_PLUGIN_DIR, "package.json"))
  ? MODULES_PLUGIN_DIR
  : LEGACY_PLUGIN_DIR;
const CONFIG_DIR = path.join(WORKSPACE, "config");
const DATA_DIR = path.join(WORKSPACE, "data");
const JOURNAL_DIR = path.join(WORKSPACE, "journal");
const LOGS_DIR = path.join(WORKSPACE, "logs");
const PROJECTS_DIR = path.join(WORKSPACE, "projects");
const TEMP_DIR = path.join(WORKSPACE, "temp");
const SCRATCH_DIR = path.join(WORKSPACE, "scratch");
const OLLAMA_BASE_URL = (process.env.OLLAMA_URL || "http://localhost:11434")
  .replace(/\/v1\/?$/, "")
  .replace(/\/+$/, "");
const OLLAMA_TAGS_URL = `${OLLAMA_BASE_URL}/api/tags`;
const OLLAMA_PS_URL = `${OLLAMA_BASE_URL}/api/ps`;

// Mutable platform override — set by interactive platform selection prompt.
// Allows the prompt to override IS_OPENCLAW / IS_CLAUDE_CODE after they're set.
let _platformOverride = "";

/**
 * Check if current install platform matches the given name.
 * Respects both CLI flags and interactive selection.
 */
function _isPlatform(name) {
  if (_platformOverride) return _platformOverride === name;
  if (name === "claude-code") return IS_CLAUDE_CODE;
  if (name === "openclaw") return IS_OPENCLAW;
  if (name === "standalone") return !IS_OPENCLAW && !IS_CLAUDE_CODE;
  return false;
}

// Python env setup — always set canonical Quaid root, plus workspace hint.
const PY_ENV_SETUP =
  `os.environ['QUAID_HOME'] = '${WORKSPACE}'\n` +
  `os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'`;

// Step-specific quotes — each tied to the step's theme
const STEP_QUOTES = {
  preflight:  "Get ready for a surprise!",
  identity:   "If I am not me, then who the hell am I?",
  models:     "What is it that you want, Mr. Quaid?",
  embeddings: "Ever heard of Rekall? They sell fake memories.",
  janitor:    "No wonder you have nightmares, you're always here.",
  install:    "See you at the party, Richter!",
  validate:   "Baby, you make me wish I had three hands.",
  outro:      "You think this is the real Quaid? It is.",
};

// --- ANSI styling ---
// Uses bold variants + 256-color where needed for light/dark terminal compat.
// Avoid: dim (invisible on light bg), pure blue (invisible on dark bg),
//        dark magenta (hard on dark bg). Safe everywhere: bold+cyan, bold+green,
//        bold+yellow, bold+white, 256-color bright magenta (200), bright cyan (80).
const C = {
  mag:    (s) => `\x1b[38;5;170m${s}\x1b[0m`,      // muted pink-magenta (both themes)
  cyan:   (s) => `\x1b[36m${s}\x1b[0m`,
  bold:   (s) => `\x1b[1m${s}\x1b[0m`,
  dim:    (s) => `\x1b[2m${s}\x1b[0m`,
  yellow: (s) => `\x1b[33m${s}\x1b[0m`,
  green:  (s) => `\x1b[32m${s}\x1b[0m`,
  red:    (s) => `\x1b[31m${s}\x1b[0m`,
  bmag:   (s) => `\x1b[1;38;5;200m${s}\x1b[0m`,     // bright magenta (visible both themes)
  bcyan:  (s) => `\x1b[1;36m${s}\x1b[0m`,
};

// Known embedding models: model name → { dim, ramGB, quality }
const EMBED_MODELS = {
  "qwen3-embedding:8b":  { dim: 4096, ramGB: 6,   quality: "Best", rank: 1 },
  "nomic-embed-text":    { dim: 768,  ramGB: 1.5, quality: "Good", rank: 2 },
  "bge-large":           { dim: 1024, ramGB: 1.2, quality: "Good", rank: 3 },
  "mxbai-embed-large":   { dim: 1024, ramGB: 1.2, quality: "Good", rank: 4 },
  "all-minilm":          { dim: 384,  ramGB: 0.5, quality: "Basic", rank: 5 },
};

function readPkgName(pkgDir) {
  try {
    const raw = fs.readFileSync(path.join(pkgDir, "package.json"), "utf8");
    const parsed = JSON.parse(raw);
    const name = String(parsed?.name || "").trim();
    return name;
  } catch {
    return "";
  }
}

function findPackageRootFrom(startPath, allowedNames = new Set(["openclaw", "clawdbot"])) {
  let dir = startPath;
  try {
    const st = fs.statSync(startPath);
    if (!st.isDirectory()) {
      dir = path.dirname(startPath);
    }
  } catch {
    dir = path.dirname(startPath);
  }

  while (true) {
    const pkgJson = path.join(dir, "package.json");
    if (fs.existsSync(pkgJson)) {
      const pkgName = readPkgName(dir);
      if (allowedNames.has(pkgName)) {
        return dir;
      }
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

function discoverOpenClawRoots() {
  const roots = new Set();
  const allowed = new Set(["openclaw", "clawdbot"]);

  for (const cli of ["clawdbot", "openclaw"]) {
    const cliBin = shell(`command -v ${cli} 2>/dev/null`) || "";
    if (!cliBin) continue;
    for (const candidate of [cliBin, fs.existsSync(cliBin) ? fs.realpathSync(cliBin) : ""]) {
      if (!candidate) continue;
      const root = findPackageRootFrom(candidate, allowed);
      if (root) roots.add(root);
    }
  }

  const npmRoot = shell("npm root -g 2>/dev/null") || "";
  if (npmRoot && fs.existsSync(npmRoot)) {
    for (const entry of fs.readdirSync(npmRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (!entry.name.startsWith("openclaw") && !entry.name.startsWith("clawdbot")) continue;
      const dir = path.join(npmRoot, entry.name);
      const root = findPackageRootFrom(dir, allowed) || (fs.existsSync(path.join(dir, "package.json")) ? dir : null);
      if (root) roots.add(root);
    }
  }

  for (const dir of [
    path.join(os.homedir(), "openclaw"),
    path.join(os.homedir(), "openclaw-source"),
    "/opt/homebrew/lib/node_modules/openclaw",
    "/opt/homebrew/lib/node_modules/clawdbot",
    "/usr/local/lib/node_modules/openclaw",
    "/usr/local/lib/node_modules/clawdbot",
    "/usr/lib/node_modules/openclaw",
    "/usr/lib/node_modules/clawdbot",
  ]) {
    if (!fs.existsSync(path.join(dir, "package.json"))) continue;
    const root = findPackageRootFrom(dir, allowed) || dir;
    if (root) roots.add(root);
  }

  return [...roots];
}

// --- Resolve @clack/prompts ---
// Try OpenClaw installation first, then well-known paths, then local/global npm
let clack;
if (!clack) {
  for (const base of [
    ...discoverOpenClawRoots(),
    // Standalone: try @clack/prompts installed globally or alongside this script
    path.join(__dirname, "node_modules", "@clack", "prompts"),
    ...(process.env.npm_config_prefix ? [path.join(process.env.npm_config_prefix, "lib", "node_modules", "@clack", "prompts")] : []),
  ]) {
    // For OpenClaw paths, @clack is nested under node_modules
    const p = base.endsWith("prompts")
      ? path.join(base, "dist", "index.mjs")
      : path.join(base, "node_modules", "@clack", "prompts", "dist", "index.mjs");
    if (fs.existsSync(p)) {
      try { clack = await import(p); break; } catch { /* next */ }
    }
  }
}

// Last resort: try Node's built-in resolution (works if @clack/prompts is in any node_modules ancestor)
if (!clack) {
  try { clack = await import("@clack/prompts"); } catch { /* fall through */ }
}

if (!clack) {
  console.error(C.red("[x] Could not find @clack/prompts."));
  if (IS_OPENCLAW) {
    console.error("    Make sure OpenClaw is installed: npm install -g openclaw");
  } else {
    console.error("    Install it: npm install @clack/prompts");
    console.error("    Or install OpenClaw (includes it): npm install -g openclaw");
  }
  process.exit(1);
}

// --- Test mode: read canned answers from JSON instead of interactive prompts ---
const TEST_ANSWERS_PATH = process.env.QUAID_TEST_ANSWERS;
let _testAnswers = null;
let _testIdx = 0;
if (TEST_ANSWERS_PATH) {
  _testAnswers = JSON.parse(fs.readFileSync(TEST_ANSWERS_PATH, "utf8"));
  _testIdx = 0;
}

function _nextAnswer(type, message) {
  if (!_testAnswers) return undefined;
  const answer = _testAnswers.answers[_testIdx];
  if (answer === undefined) throw new Error(`Test mode: ran out of answers at index ${_testIdx} (${type}: ${message})`);
  _testIdx++;
  return answer;
}

const _clack = clack;
const { intro: _intro, outro: _outro, note: _note, cancel: _cancel, isCancel: _isCancel, log: _log, spinner: _spinner } = _clack;

const intro = _intro;
const outro = _outro;
const note = _note;
const cancel = _cancel;
const isCancel = _isCancel;
const log = _log;

const select = _testAnswers
  ? async (opts) => { const a = _nextAnswer("select", opts.message); log.info(C.dim(`[test] select "${opts.message}" → ${a}`)); return a; }
  : AGENT_MODE
    ? async (opts) => {
        const initial = opts?.initialValue;
        if (initial !== undefined && initial !== null) {
          log.info(C.dim(`[agent] select "${opts.message}" → ${initial}`));
          return initial;
        }
        const first = Array.isArray(opts?.options) ? opts.options[0] : undefined;
        const picked = first?.value ?? first;
        log.info(C.dim(`[agent] select "${opts.message}" → ${picked}`));
        return picked;
      }
    : _clack.select;
const confirm = _testAnswers
  ? async (opts) => { const a = _nextAnswer("confirm", opts.message); log.info(C.dim(`[test] confirm "${opts.message}" → ${a}`)); return a; }
  : AGENT_MODE
    ? async (opts) => {
        const v = opts?.initialValue;
        const picked = v === undefined ? true : !!v;
        log.info(C.dim(`[agent] confirm "${opts.message}" → ${picked}`));
        return picked;
      }
    : _clack.confirm;
const text = _testAnswers
  ? async (opts) => { const a = _nextAnswer("text", opts.message); log.info(C.dim(`[test] text "${opts.message}" → ${a}`)); return a; }
  : AGENT_MODE
    ? async (opts) => {
        const picked = String(opts?.initialValue ?? opts?.placeholder ?? "");
        if (typeof opts?.validate === "function") {
          const validation = await opts.validate(picked);
          if (typeof validation === "string" && validation.trim()) {
            throw new Error(`Agent-mode invalid default for "${opts?.message || "text"}": ${validation}`);
          }
        }
        log.info(C.dim(`[agent] text "${opts.message}" → ${picked}`));
        return picked;
      }
    : _clack.text;
const spinner = _testAnswers
  ? () => ({ start: (m) => log.info(C.dim(`[test] spinner: ${m}`)), stop: (m) => log.info(C.dim(`[test] done: ${m}`)) })
  : _clack.spinner;

// --- Helpers ---
function shell(cmd, trim = true) {
  try {
    const out = execSync(cmd, {
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      cwd: os.homedir(),
      timeout: 15_000,
    });
    return trim ? out.trim() : out;
  } catch { return ""; }
}

function runCliWithTimeout(bin, args, timeoutMs = 30_000) {
  return spawnSync(bin, args, {
    encoding: "utf8",
    stdio: "pipe",
    timeout: timeoutMs,
  });
}

function renderCliFailure(res, timeoutMs = null) {
  const sig = String(res?.signal || "");
  if (sig === "SIGTERM" || sig === "SIGKILL") {
    return timeoutMs && Number.isFinite(timeoutMs)
      ? `timed out after ${Number(timeoutMs)}ms`
      : "timed out";
  }
  return String(res?.stderr || res?.stdout || "").trim() || "unknown error";
}

function _safeTrim(value) {
  return String(value || "").trim();
}

function _gatewayStatusSnapshot(cli) {
  const statusRes = runCliWithTimeout(cli, ["gateway", "status"], 20_000);
  const probeRes = runCliWithTimeout(cli, ["gateway", "probe"], 10_000);
  const statusText = [_safeTrim(statusRes.stdout), _safeTrim(statusRes.stderr)].filter(Boolean).join("\n");
  const probeText = [_safeTrim(probeRes.stdout), _safeTrim(probeRes.stderr)].filter(Boolean).join("\n");
  const health = _gatewayHttpCode("/health", "GET", null);
  const responses = _gatewayHttpCode("/v1/responses", "POST", "{}");
  const pluginLlm = _gatewayHttpCode("/plugins/quaid/llm", "POST", "{}");
  return {
    statusRes,
    probeRes,
    statusText,
    probeText,
    health,
    responses,
    pluginLlm,
  };
}

function _formatGatewaySnapshot(snapshot) {
  const parts = [
    `health=${snapshot.health}`,
    `responses=${snapshot.responses}`,
    `plugin=${snapshot.pluginLlm}`,
  ];
  if (snapshot.statusText) parts.push(`status=${snapshot.statusText.replace(/\s+/g, " ").trim()}`);
  if (snapshot.probeText) parts.push(`probe=${snapshot.probeText.replace(/\s+/g, " ").trim()}`);
  return parts.join(" | ");
}

function _gatewayServiceLooksMissing(snapshot) {
  const text = `${snapshot.statusText}\n${snapshot.probeText}`.toLowerCase();
  return text.includes("service not installed")
    || text.includes("service unit not found")
    || text.includes("could not find service");
}

function _gatewayServiceLooksStopped(snapshot) {
  const text = `${snapshot.statusText}\n${snapshot.probeText}`.toLowerCase();
  return text.includes("not loaded")
    || text.includes("reachable: no")
    || text.includes("econnrefused")
    || text.includes("connect failed");
}

async function ensureGatewayReadyOrThrow(cli, context, timeoutMs = 12_000) {
  if (!IS_OPENCLAW || !cli) return;
  if (await waitForGatewayWarmup(timeoutMs)) return;

  let snapshot = _gatewayStatusSnapshot(cli);
  log.warn(`Gateway warmup failed during ${context}: ${_formatGatewaySnapshot(snapshot)}`);

  if (_gatewayServiceLooksMissing(snapshot)) {
    log.warn("Gateway service appears missing after restart; attempting service install recovery.");
    const installRes = runCliWithTimeout(cli, ["gateway", "install"], 30_000);
    if (installRes.status !== 0) {
      const msg = renderCliFailure(installRes, 30_000);
      throw new Error(`gateway service missing after ${context}; auto-recovery failed during install: ${msg || "unknown error"}`);
    }
    const restartRes = runCliWithTimeout(cli, ["gateway", "restart"], 30_000);
    if (restartRes.status !== 0) {
      const msg = renderCliFailure(restartRes, 30_000);
      throw new Error(`gateway service recovered but restart failed during ${context}: ${msg || "unknown error"}`);
    }
    if (await waitForGatewayWarmup(30_000)) return;
    snapshot = _gatewayStatusSnapshot(cli);
  } else if (_gatewayServiceLooksStopped(snapshot)) {
    log.warn("Gateway service appears installed but not healthy; attempting restart recovery.");
    const restartRes = runCliWithTimeout(cli, ["gateway", "restart"], 30_000);
    if (restartRes.status !== 0) {
      const msg = renderCliFailure(restartRes, 30_000);
      throw new Error(`gateway restart recovery failed during ${context}: ${msg || "unknown error"}`);
    }
    if (await waitForGatewayWarmup(30_000)) return;
    snapshot = _gatewayStatusSnapshot(cli);
  }

  const detail = _formatGatewaySnapshot(snapshot);
  const message =
    `Gateway failed to become healthy during ${context}. ${detail}. `
    + "Run `openclaw gateway status` and `openclaw gateway install` on this host before retrying install.";
  log.error(message);
  sendInstallerNotification(`❌ Quaid install stopped: ${message}`);
  throw new Error(message);
}

function canRun(cmd) {
  return spawnSync("sh", ["-c", `command -v '${cmd.replace(/'/g, "'\\''")}'`], { stdio: "pipe" }).status === 0;
}

function looksLikeUrl(value) {
  return /^https?:\/\//i.test(String(value || "").trim());
}

async function downloadRemoteFile(url, destPath) {
  const res = await fetch(url, {
    headers: {
      "User-Agent": "quaid-installer",
      Accept: "application/octet-stream",
    },
  });
  if (!res.ok) {
    throw new Error(`download failed (${res.status} ${res.statusText}) for ${url}`);
  }
  const ab = await res.arrayBuffer();
  fs.writeFileSync(destPath, Buffer.from(ab));
}

function extractTarGz(archivePath, extractDir) {
  const tarRes = spawnSync("tar", ["-xzf", archivePath, "-C", extractDir], { stdio: "pipe", encoding: "utf8" });
  if (tarRes.status !== 0) {
    const detail = `${tarRes.stderr || ""}\n${tarRes.stdout || ""}`.trim();
    throw new Error(`failed to extract archive (${archivePath}): ${detail || "tar exited non-zero"}`);
  }
}

function findPluginDirInExtracted(rootDir) {
  const candidates = [
    path.join(rootDir, "modules", "quaid"),
    path.join(rootDir, "quaid"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, "package.json"))) {
      return candidate;
    }
  }
  try {
    for (const entry of fs.readdirSync(rootDir, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      const nested = path.join(rootDir, entry.name, "modules", "quaid");
      if (fs.existsSync(path.join(nested, "package.json"))) return nested;
      const pluginOnly = path.join(rootDir, entry.name, "quaid");
      if (fs.existsSync(path.join(pluginOnly, "package.json"))) return pluginOnly;
    }
  } catch {}
  return "";
}

async function resolvePluginSource() {
  if (INSTALL_SOURCE === "local") {
    const pluginSrc = [
      path.join(__dirname, "modules", "quaid"),
      PLUGIN_DIR,
    ].find((p) => {
      try {
        return fs.existsSync(p) && fs.statSync(p).isDirectory() && fs.readdirSync(p).length > 0;
      } catch {
        return false;
      }
    });
    if (!pluginSrc) {
      throw new Error(`expected local plugin source at ${path.join(__dirname, "modules", "quaid")} or ${PLUGIN_DIR}`);
    }
    return pluginSrc;
  }

  const tmpBase = fs.mkdtempSync(path.join(os.tmpdir(), "quaid-installer-src-"));
  const archivePath = path.join(tmpBase, "source.tar.gz");
  const extractDir = path.join(tmpBase, "extract");
  fs.mkdirSync(extractDir, { recursive: true });

  if (INSTALL_SOURCE === "github") {
    const refSafe = encodeURIComponent(INSTALL_REF);
    const repoSafe = INSTALL_GITHUB_REPO.replace(/^https?:\/\/github\.com\//i, "").replace(/\.git$/i, "");
    const url = `https://codeload.github.com/${repoSafe}/tar.gz/${refSafe}`;
    await downloadRemoteFile(url, archivePath);
  } else {
    if (looksLikeUrl(INSTALL_ARTIFACT)) {
      await downloadRemoteFile(INSTALL_ARTIFACT, archivePath);
    } else {
      const localPath = path.resolve(INSTALL_ARTIFACT);
      if (!fs.existsSync(localPath)) {
        throw new Error(`artifact file not found: ${localPath}`);
      }
      fs.copyFileSync(localPath, archivePath);
    }
  }

  extractTarGz(archivePath, extractDir);
  const pluginSrc = findPluginDirInExtracted(extractDir);
  if (!pluginSrc) {
    throw new Error(`could not find modules/quaid in extracted source (${extractDir})`);
  }
  return pluginSrc;
}

function ownerIdFromDisplayName(displayName) {
  const normalized = String(displayName || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return normalized || "default";
}

function _hasSqliteVec() {
  return spawnSync("python3", ["-c", "import sqlite_vec"], { stdio: "pipe" }).status === 0;
}

function _installSqliteVec() {
  const attempts = [
    ["python3", ["-m", "pip", "install", "sqlite-vec"]],
    ["python3", ["-m", "pip", "install", "--user", "sqlite-vec"]],
    ["pip3", ["install", "sqlite-vec"]],
    ["pip", ["install", "sqlite-vec"]],
  ];
  for (const [cmd, args] of attempts) {
    const res = spawnSync(cmd, args, { stdio: "pipe" });
    if (res.status === 0) return true;
  }
  return false;
}

function _readAgentsList(cli) {
  const out = shell(`${cli} config get agents.list 2>/dev/null </dev/null`, false);
  if (!out) return [];
  try {
    const parsed = JSON.parse(out);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function _ensureAgentsList(cli, workspacePath) {
  const existing = _readAgentsList(cli);
  if (existing.some((a) => a && typeof a === "object" && a.id)) return true;
  if (Array.isArray(existing) && existing.length > 0) {
    log.warn("agents.list exists but entries are non-standard (missing id); refusing to overwrite");
    return false;
  }
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const ws =
      workspacePath ||
      parsed?.workspace ||
      parsed?.agents?.defaults?.workspace ||
      path.join(os.homedir(), "quaid");
    if (!parsed.agents || typeof parsed.agents !== "object") parsed.agents = {};
    parsed.agents.list = [
      {
        id: "main",
        default: true,
        name: "Default",
        workspace: ws,
      },
    ];
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return _readAgentsList(cli).some((a) => a && typeof a === "object" && a.id);
  } catch (err) {
    log.warn(`Could not auto-heal agents.list: ${String(err)}`);
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _ensureOpenClawResponsesEndpoint() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed.gateway || typeof parsed.gateway !== "object") parsed.gateway = {};
    if (!parsed.gateway.http || typeof parsed.gateway.http !== "object") parsed.gateway.http = {};
    if (!parsed.gateway.http.endpoints || typeof parsed.gateway.http.endpoints !== "object") {
      parsed.gateway.http.endpoints = {};
    }
    if (!parsed.gateway.http.endpoints.responses || typeof parsed.gateway.http.endpoints.responses !== "object") {
      parsed.gateway.http.endpoints.responses = {};
    }
    const alreadyEnabled = !!parsed.gateway.http.endpoints.responses.enabled;
    if (alreadyEnabled) return false;
    parsed.gateway.http.endpoints.responses.enabled = true;
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _sanitizeOpenClawQuaidPluginEntry() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const entries = parsed?.plugins?.entries;
    const quaid = entries?.quaid;
    if (!quaid || typeof quaid !== "object" || !Object.prototype.hasOwnProperty.call(quaid, "workspace")) {
      return false;
    }
    delete quaid.workspace;
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _sanitizeOpenClawMemorySlot() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const plugins = parsed?.plugins;
    if (!plugins || typeof plugins !== "object") return false;
    const slots = plugins.slots;
    if (!slots || typeof slots !== "object") return false;
    if (String(slots.memory || "").trim() !== "quaid") return false;

    const installs = plugins.installs;
    const installPath = installs?.quaid?.installPath;
    const quaidPresent = typeof installPath === "string" && installPath.trim() && fs.existsSync(installPath);
    if (quaidPresent) return false;

    slots.memory = "memory-core";
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _sanitizeOpenClawPluginInstallSources() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const plugins = parsed?.plugins;
    if (!plugins || typeof plugins !== "object") return false;
    const installs = plugins.installs;
    if (!installs || typeof installs !== "object") return false;

    let changed = false;
    for (const [pluginId, installRec] of Object.entries(installs)) {
      if (!installRec || typeof installRec !== "object") continue;
      const source = String(installRec.source || "").trim().toLowerCase();
      if (!source) continue;
      if (source === "npm" || source === "archive" || source === "path") continue;

      // OpenClaw beta validates installs.<id>.source as enum(npm|archive|path).
      // Older installs wrote "local"; normalize that forward-compatible value.
      if (source === "local") {
        installRec.source = "path";
        changed = true;
        continue;
      }

      // Unknown/legacy source values can hard-fail all plugin CLI commands.
      // Drop the invalid install record and let plugin install repopulate it.
      delete installs[pluginId];
      changed = true;
    }

    if (!changed) return false;
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _ensureOpenClawPluginsAllowQuaid() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const plugins = parsed.plugins;
    if (!plugins || typeof plugins !== "object") return false;
    const allow = Array.isArray(plugins.allow) ? plugins.allow : [];
    const nextAllow = Array.from(
      new Set(allow.map((entry) => String(entry || "").trim()).filter(Boolean).concat(["quaid"])),
    );
    if (allow.length === nextAllow.length && allow.every((entry, idx) => String(entry || "").trim() === nextAllow[idx])) {
      return false;
    }
    plugins.allow = nextAllow;
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _removeOpenClawPluginsAllowQuaid() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const plugins = parsed.plugins;
    if (!plugins || typeof plugins !== "object") return false;
    const allow = Array.isArray(plugins.allow) ? plugins.allow : [];
    const nextAllow = allow
      .map((entry) => String(entry || "").trim())
      .filter((entry) => entry && entry !== "quaid");
    if (allow.length === nextAllow.length) return false;
    plugins.allow = nextAllow;
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _ensureOpenClawCompactionModeDefault() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed.agents || typeof parsed.agents !== "object") parsed.agents = {};
    if (!parsed.agents.defaults || typeof parsed.agents.defaults !== "object") {
      parsed.agents.defaults = {};
    }
    if (!parsed.agents.defaults.compaction || typeof parsed.agents.defaults.compaction !== "object") {
      parsed.agents.defaults.compaction = {};
    }
    const current = String(parsed.agents.defaults.compaction.mode || "").trim().toLowerCase();
    if (current === "default") return false;
    parsed.agents.defaults.compaction.mode = "default";
    fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
    fs.renameSync(tmpPath, cfgPath);
    return true;
  } catch {
    return false;
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function _registerOpenClawQuaidPlugin(pluginPath) {
  const cli = canRun("openclaw") ? "openclaw" : (canRun("clawdbot") ? "clawdbot" : "");
  if (!cli) return { ok: false, reason: "OpenClaw CLI not found" };
  const normalize = (s) => String(s || "").toLowerCase();
  const extensionDir = path.join(os.homedir(), ".openclaw", "extensions", "quaid");
  const stagedPluginPath = path.join(
    os.tmpdir(),
    `quaid-plugin-stage-${process.pid}-${Date.now()}`,
  );
  const removeStaleExtensionDir = () => {
    try {
      if (!fs.existsSync(extensionDir)) return false;
      fs.rmSync(extensionDir, { recursive: true, force: true });
      return true;
    } catch {
      return false;
    }
  };

  // Force-refresh plugin install to avoid stale extension code lingering at ~/.openclaw/extensions/quaid.
  // Some OpenClaw builds report "already installed" and keep old files instead of replacing contents.
  try {
    // Preserve symlinks while staging. Dereferencing can fail when optional
    // dependency links point outside the plugin tree and are absent locally.
    fs.cpSync(pluginPath, stagedPluginPath, { recursive: true, dereference: false });
  } catch (err) {
    return { ok: false, reason: `failed to stage plugin source: ${String(err)}` };
  }
  try {
    // OpenClaw beta rejects symlinked manifests, even when links stay inside plugin root.
    // Normalize the staged manifest to a regular file before install.
    const stagedManifestPath = path.join(stagedPluginPath, "openclaw.plugin.json");
    const manifestStat = fs.lstatSync(stagedManifestPath);
    if (manifestStat.isSymbolicLink()) {
      const resolvedManifestPath = fs.realpathSync(stagedManifestPath);
      const manifestRaw = fs.readFileSync(resolvedManifestPath, "utf8");
      fs.unlinkSync(stagedManifestPath);
      fs.writeFileSync(stagedManifestPath, manifestRaw, "utf8");
    }
  } catch (err) {
    return { ok: false, reason: `failed to normalize staged plugin manifest: ${String(err)}` };
  }

  // Pre-clean stale extension/config so plugin CLI can run even when previous state is invalid.
  removeStaleExtensionDir();
  _sanitizeOpenClawPluginInstallSources();
  _sanitizeOpenClawQuaidPluginEntry();
  _removeOpenClawPluginsAllowQuaid();
  _sanitizeOpenClawMemorySlot();

  const uninstallRes = runCliWithTimeout(cli, ["plugins", "uninstall", "quaid", "--force"], 45_000);
  if (uninstallRes.status !== 0) {
    const msg = renderCliFailure(uninstallRes, 45_000);
    const norm = normalize(msg);
    const unmanaged = norm.includes("not managed by plugins config/install records");
    if (!norm.includes("not installed") && !norm.includes("not found") && !norm.includes("missing") && !unmanaged) {
      return { ok: false, reason: `plugins uninstall failed: ${msg.trim() || "unknown error"}` };
    }
    if (unmanaged) removeStaleExtensionDir();
  }

  const installRes = runCliWithTimeout(cli, ["plugins", "install", stagedPluginPath], 60_000);
  if (installRes.status !== 0) {
    const msg = renderCliFailure(installRes, 60_000);
    const norm = normalize(msg);
    if ((norm.includes("already installed") || norm.includes("already exists")) && removeStaleExtensionDir()) {
      const retry = runCliWithTimeout(cli, ["plugins", "install", stagedPluginPath], 60_000);
      if (retry.status === 0) {
        // continue
      } else {
        const retryMsg = renderCliFailure(retry, 60_000);
        return { ok: false, reason: `plugins install failed after stale-dir cleanup: ${retryMsg.trim() || "unknown error"}` };
      }
    } else {
      return { ok: false, reason: `plugins install failed: ${msg.trim() || "unknown error"}` };
    }
  }

  const enableRes = runCliWithTimeout(cli, ["plugins", "enable", "quaid"], 45_000);
  if (enableRes.status !== 0) {
    const msg = renderCliFailure(enableRes, 45_000);
    const norm = normalize(msg);
    if (!norm.includes("already enabled")) {
      return { ok: false, reason: `plugins enable failed: ${msg.trim() || "unknown error"}` };
    }
  }

  // Verify the plugin registry can actually resolve/load Quaid on this OpenClaw build.
  // Some builds accept install/enable config writes but still do not expose the plugin at runtime.
  const listRes = runCliWithTimeout(cli, ["plugins", "list", "--json"], 20_000);
  if (listRes.status !== 0) {
    const msg = renderCliFailure(listRes, 20_000);
    return { ok: false, reason: `plugins list failed after enable: ${msg || "unknown error"}` };
  }
  const listRaw = String(listRes.stdout || "");
  const hasQuaid = /"id"\s*:\s*"quaid"/.test(listRaw);
  if (!hasQuaid) {
    return {
      ok: false,
      reason:
        "OpenClaw did not resolve plugin 'quaid' after install/enable (plugins list missing id=quaid). " +
        "This OpenClaw build may be incompatible with Quaid's plugin registration path.",
    };
  }

  const restartRes = runCliWithTimeout(cli, ["gateway", "restart"], 30_000);
  if (restartRes.status !== 0) {
    const msg = renderCliFailure(restartRes, 30_000);
    return { ok: false, reason: `gateway restart failed: ${msg || "unknown error"}` };
  }

  try {
    fs.rmSync(stagedPluginPath, { recursive: true, force: true });
  } catch {}

  return { ok: true, reason: "" };
}

function bail(msg) {
  cancel(msg);
  process.exit(1);
}

function handleCancel(value, msg = "Setup cancelled.") {
  if (isCancel(value)) bail(msg);
  return value;
}

function getSystemRAM() {
  const total = Math.round(os.totalmem() / 1024 / 1024 / 1024);
  let free;

  if (process.platform === "darwin") {
    // macOS: os.freemem() only counts truly free pages, ignoring reclaimable cache.
    // vm_stat gives free + inactive + speculative + purgeable for realistic availability.
    try {
      const { stdout } = spawnSync("vm_stat", { encoding: "utf8", stdio: "pipe" });
      const pageSizeMatch = stdout.match(/page size of (\d+) bytes/);
      const pageSize = pageSizeMatch ? parseInt(pageSizeMatch[1]) : 16384;
      const parse = (label) => {
        const m = stdout.match(new RegExp(`${label}:\\s+(\\d+)`));
        return m ? parseInt(m[1]) : 0;
      };
      const available = (parse("Pages free") + parse("Pages inactive") +
        parse("Pages speculative") + parse("Pages purgeable")) * pageSize;
      free = Math.round(available / 1024 / 1024 / 1024);
    } catch {
      free = Math.round(os.freemem() / 1024 / 1024 / 1024);
    }
  } else {
    // Linux: /proc/meminfo MemAvailable is the kernel's own availability estimate
    try {
      const meminfo = fs.readFileSync("/proc/meminfo", "utf8");
      const m = meminfo.match(/MemAvailable:\s+(\d+)\s+kB/);
      free = m ? Math.round(parseInt(m[1]) / 1024 / 1024) : Math.round(os.freemem() / 1024 / 1024 / 1024);
    } catch {
      free = Math.round(os.freemem() / 1024 / 1024 / 1024);
    }
  }

  return { total, free };
}

async function waitForKey(msg = "Press any key to continue...") {
  if (_testAnswers || AGENT_MODE) return; // skip in test + agent mode
  log.message(C.dim(msg));
  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
    process.stdin.resume();
    await new Promise((resolve) => process.stdin.once("data", resolve));
    process.stdin.setRawMode(false);
    process.stdin.pause();
  }
}

function clearScreen() {
  if (_testAnswers) return; // skip in test mode
  process.stdout.write("\x1B[2J\x1B[H");
}

function getOllamaModels() {
  // Returns all pulled (installed) models
  try {
    const raw = execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { encoding: "utf8", cwd: os.homedir() });
    const data = JSON.parse(raw);
    return (data.models || []).map(m => m.name || "");
  } catch { return []; }
}

function getLoadedOllamaModels() {
  // Returns models currently loaded in VRAM (/api/ps)
  try {
    const raw = execSync(`curl -sf ${JSON.stringify(OLLAMA_PS_URL)}`, { encoding: "utf8", cwd: os.homedir() });
    const data = JSON.parse(raw);
    return (data.models || []).map(m => ({
      name: m.name || "",
      sizeGB: ((m.size || 0) / 1e9).toFixed(1),
      vramGB: ((m.size_vram || 0) / 1e9).toFixed(1),
    }));
  } catch { return []; }
}

function showBanner() {
  // Persistent header — shown at the top of every step
  const lines = renderQuaidBanner(C, {
    subtitle: "by Douglas Quaid",
    title: " LONG-TERM MEMORY SYSTEM ",
    topRightTail: "                                      ",
    footerLines: [
      " ".repeat(48) + C.dim(`v${VERSION}`),
    ],
  });
  console.log(lines.join("\n"));
}

function stepHeader(num, total, title, quote) {
  clearScreen();
  showBanner();
  const w = 60;
  const label = `  STEP ${num}/${total}  ▸  ${title}  `;
  const pad = Math.max(0, w - label.length);
  log.message(C.mag("  ┌" + "─".repeat(w) + "┐"));
  log.message(C.mag("  │") + C.bcyan(label) + " ".repeat(pad) + C.mag("│"));
  if (quote) {
    const qtext = `  "${quote}"  `;
    const qpad = Math.max(0, w - qtext.length);
    log.message(C.mag("  │") + C.cyan(qtext) + " ".repeat(qpad) + C.mag("│"));
  }
  log.message(C.mag("  └" + "─".repeat(w) + "┘"));
  log.message("");
}

// =============================================================================
// Step 1: Pre-flight Checks
// =============================================================================
async function step1_preflight() {
  stepHeader(1, TOTAL_INSTALL_STEPS, "PREFLIGHT", STEP_QUOTES.preflight);
  intro(C.dim("Checking your system..."));

  log.info(C.dim(`Workspace: ${WORKSPACE}`));

  // Snapshot existing files BEFORE any clawdbot commands — those commands load
  // the quaid plugin which creates data/memory.db, giving a false "dirty" signal.
  const _existingFiles = [
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "TOOLS.md",
    "AGENTS.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
    "TODO.md",
  ]
    .filter(f => fs.existsSync(path.join(WORKSPACE, f)));
  const _hasConfig = fs.existsSync(path.join(CONFIG_DIR, "memory.json"));
  const _hasDb = fs.existsSync(path.join(DATA_DIR, "memory.db"));

  const s = spinner();

  // --- Platform selection ---
  // If not explicitly set via CLI flag, ask in interactive mode.
  if (!IS_OPENCLAW && !IS_CLAUDE_CODE && !AGENT_MODE && !_testAnswers) {
    const platform = handleCancel(await select({
      message: "Which platform are you installing Quaid for?",
      options: [
        { value: "standalone", label: "Standalone", hint: "local-only runtime (default)" },
        { value: "claude-code", label: "Claude Code", hint: "hooks + OAuth for Claude Code CLI" },
        { value: "openclaw", label: "OpenClaw", hint: "gateway-integrated runtime" },
      ],
    }));
    if (platform === "claude-code") {
      // Promote to IS_CLAUDE_CODE for the rest of the install
      // We can't reassign const, so use a module-level mutable flag.
      _platformOverride = "claude-code";
    } else if (platform === "openclaw") {
      _platformOverride = "openclaw";
    }
  }

  if (_isPlatform("claude-code")) {
    // --- Claude Code mode ---
    s.start("Checking Claude Code...");
    const hasClaude = canRun("claude");
    if (!hasClaude) {
      s.stop(C.yellow("Claude Code CLI not found"), 2);
      log.warn("Install Claude Code: https://docs.anthropic.com/en/docs/claude-code");
      log.warn("Continuing anyway — CLI is needed at runtime, not install time.");
    }
    // Check OAuth credentials
    const credsPath = path.join(os.homedir(), ".claude", ".credentials.json");
    if (fs.existsSync(credsPath)) {
      try {
        const creds = JSON.parse(fs.readFileSync(credsPath, "utf8"));
        if (creds?.claudeAiOauth?.accessToken) {
          s.stop(C.green("Claude Code") + C.dim(" — OAuth credentials found"));
        } else {
          s.stop(C.yellow("Claude Code") + C.dim(" — no OAuth token, run 'claude login'"));
        }
      } catch {
        s.stop(C.yellow("Claude Code") + C.dim(" — credentials unreadable"));
      }
    } else {
      s.stop(C.yellow("Claude Code") + C.dim(" — no credentials, run 'claude login' first"));
    }
    fs.mkdirSync(WORKSPACE, { recursive: true });
  } else if (_isPlatform("openclaw")) {
    // --- OpenClaw installed ---
    s.start("Scanning for OpenClaw...");
    if (!canRun("clawdbot") && !canRun("openclaw")) {
      s.stop(C.red("OpenClaw not found"), 2);
      note(
        "Quaid is a plugin for OpenClaw and requires it to run.\n\n" +
        "Install OpenClaw first:\n" +
        "  npm install -g openclaw\n" +
        "  openclaw setup\n\n" +
        "Then re-run this installer.",
        "Missing dependency"
      );
      bail("OpenClaw is not installed.");
    }

    // --- Gateway running ---
    const statusOut = shell("clawdbot status 2>/dev/null </dev/null") ||
                      shell("openclaw status 2>/dev/null </dev/null") ||
                      shell("clawdbot gateway probe 2>/dev/null </dev/null") ||
                      shell("openclaw gateway probe 2>/dev/null </dev/null");
    if (!statusOut) {
      s.stop(C.red("Gateway offline"), 2);
      note(
        "Quaid needs the gateway running to read your config\n" +
        "and hook into conversation events.\n\n" +
        "Start it with:\n" +
        "  clawdbot gateway start\n\n" +
        "Then re-run this installer.",
        "Gateway offline"
      );
      if (!AGENT_MODE) {
        bail("OpenClaw gateway is not running.");
      }
      log.warn("OpenClaw status/probe unavailable in agent mode; continuing with install.");
    }

    // --- Onboarding / agents list ---
    const cfgCli = canRun("clawdbot") ? "clawdbot" : "openclaw";
    let hasAgent = _readAgentsList(cfgCli).some((a) => a && typeof a === "object" && a.id);
    if (!hasAgent) {
      log.warn("No agents.list detected; attempting auto-heal in ~/.openclaw/openclaw.json");
      hasAgent = _ensureAgentsList(cfgCli, WORKSPACE);
    }
    if (!hasAgent) {
      log.warn(
        "OpenClaw agents.list is still missing. Install continues, but run `openclaw setup` if agent sessions fail.",
      );
    }
    if (_sanitizeOpenClawMemorySlot()) {
      log.info("Healed stale plugins.slots.memory=quaid to memory-core (quaid not installed)");
    }
    if (_sanitizeOpenClawQuaidPluginEntry()) {
      log.info("Removed invalid plugins.entries.quaid.workspace from ~/.openclaw/openclaw.json");
    }
    if (_removeOpenClawPluginsAllowQuaid()) {
      log.info("Removed stale plugins.allow entry for quaid before plugin registration");
    }
    const responsesEndpointChanged = _ensureOpenClawResponsesEndpoint();
    if (responsesEndpointChanged) {
      log.info("Enabled gateway.http.endpoints.responses.enabled=true in ~/.openclaw/openclaw.json");
      const restart = spawnSync(cfgCli, ["gateway", "restart"], { encoding: "utf8", stdio: "pipe" });
      if (restart.status === 0) {
        log.info("Restarted OpenClaw gateway to apply endpoint config");
      } else {
        log.warn("Could not auto-restart OpenClaw gateway. Restart it manually to apply endpoint config.");
      }
    }
    if (_ensureOpenClawCompactionModeDefault()) {
      log.info("Set agents.defaults.compaction.mode=default in ~/.openclaw/openclaw.json");
    }
    s.stop(C.green("OpenClaw") + " gateway running");
  } else {
    // --- Standalone mode: ensure workspace directory exists ---
    s.start("Checking workspace directory...");
    fs.mkdirSync(WORKSPACE, { recursive: true });
    s.stop(C.green("Standalone mode") + C.dim(` — workspace: ${WORKSPACE}`));
  }

  // --- Python ---
  s.start("Checking Python 3.10+...");
  if (!canRun("python3")) {
    s.stop(C.red("Python 3 not found"), 2);
    const installed = await tryBrewInstall("python@3.12", "Python 3.12");
    if (!installed) bail("Python 3.10+ is required.");
    s.start("Rechecking Python...");
  }
  const pyVer = shell('python3 -c "import sys; print(f\'{sys.version_info.major}.{sys.version_info.minor}\')"');
  const pyOk = spawnSync("python3", ["-c", "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"], { stdio: "pipe" }).status === 0;
  if (!pyOk) {
    s.stop(C.red(`Python ${pyVer} — too old`), 2);
    const installed = await tryBrewInstall("python@3.12", "Python 3.12");
    if (!installed) bail("Python 3.10+ is required.");
    s.start("Rechecking Python...");
    const newVer = shell('python3 -c "import sys; print(f\'{sys.version_info.major}.{sys.version_info.minor}\')"');
    const newOk = spawnSync("python3", ["-c", "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"], { stdio: "pipe" }).status === 0;
    if (!newOk) bail("Python 3.10+ required. Update your PATH to use Homebrew Python.");
    s.stop(C.green(`Python ${newVer}`));
  } else {
    s.stop(C.green(`Python ${pyVer}`));
  }

  // --- SQLite ---
  s.start("Checking SQLite 3.35+...");
  const sqliteVer = shell('python3 -c "import sqlite3; print(sqlite3.sqlite_version)"');
  const sqliteOk = spawnSync("python3", ["-c", "import sqlite3; parts=[int(x) for x in sqlite3.sqlite_version.split('.')]; exit(0 if (parts[0],parts[1])>=(3,35) else 1)"], { stdio: "pipe" }).status === 0;
  if (!sqliteOk) {
    s.stop(C.red(`SQLite ${sqliteVer} — too old`), 2);
    log.warn("Python's sqlite3 module uses the system SQLite. Installing Python via Homebrew links it to a modern SQLite.");
    const installed = await tryBrewInstall("python@3.12", "Python 3.12 (with modern SQLite)");
    if (!installed) bail("SQLite 3.35+ required for FTS5 + JSON support.");
    s.start("Rechecking SQLite...");
    const newVer = shell('python3 -c "import sqlite3; print(sqlite3.sqlite_version)"');
    s.stop(C.green(`SQLite ${newVer}`));
  } else {
    s.stop(C.green(`SQLite ${sqliteVer}`));
  }

  // --- FTS5 ---
  s.start("Checking FTS5 support...");
  const fts5Ok = spawnSync("python3", ["-c", "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(content)'); c.close()"], { stdio: "pipe" }).status === 0;
  if (!fts5Ok) {
    s.stop(C.red("FTS5 not available"), 2);
    log.warn("FTS5 is included in Homebrew's SQLite.");
    const installed = await tryBrewInstall("sqlite", "SQLite (with FTS5)");
    if (installed) {
      shell("brew reinstall python@3.12 2>/dev/null || brew reinstall python 2>/dev/null || true");
    }
    if (!installed) bail("SQLite FTS5 support is required.");
    s.start("Rechecking FTS5...");
    const ok = spawnSync("python3", ["-c", "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(content)'); c.close()"], { stdio: "pipe" }).status === 0;
    if (!ok) bail("FTS5 still not available. Try: brew install sqlite && brew reinstall python@3.12");
    s.stop(C.green("FTS5 support"));
  } else {
    s.stop(C.green("FTS5 support"));
  }

  // --- Git ---
  s.start("Checking git...");
  if (!canRun("git")) {
    s.stop(C.red("Git not found"), 2);
    const installed = await tryBrewInstall("git", "Git");
    if (!installed) bail("Git is required for doc staleness tracking and project management.");
    s.start("Rechecking git...");
    if (!canRun("git")) bail("Git still not found. Install it and re-run.");
  }
  const gitVer = shell("git --version").replace("git version ", "").trim();
  s.stop(C.green(`Git ${gitVer}`));

  // --- sqlite-vec (required) ---
  s.start("Checking sqlite-vec (required)...");
  if (!_hasSqliteVec()) {
    const installed = _installSqliteVec();
    if (!installed || !_hasSqliteVec()) {
      s.stop(C.red("sqlite-vec unavailable"), 2);
      bail("sqlite-vec is required for vector retrieval. Install with: python3 -m pip install --user sqlite-vec");
    }
  }
  s.stop(C.green("sqlite-vec"));

  // --- Gateway hooks (OpenClaw only) ---
  if (_isPlatform("openclaw")) {
    s.start("Checking gateway memory hooks...");
    const gwDir = findGateway();
    if (!gwDir) {
      s.stop(C.red("Gateway not found"), 2);
      bail("Could not locate the OpenClaw gateway installation.");
    }
    const gwVersion = readGatewayVersion(gwDir);
    if (!isVersionAtLeast(gwVersion, MIN_GATEWAY_VERSION)) {
      s.stop(C.red("Gateway version unsupported"), 2);
      note(
        `Your OpenClaw version is below Quaid's required minimum.\n\n` +
        `Installed: ${gwVersion || "unknown"}\n` +
        `Required: ${MIN_GATEWAY_VERSION}+\n\n` +
        `Update with:\n` +
        `  npm install -g openclaw\n`,
        "Gateway update required"
      );
      bail("Unsupported OpenClaw version. Update OpenClaw and re-run.");
    }
    const hasHooks = gatewayHasHooks(gwDir);
    if (!hasHooks) {
      s.stop(C.red("Memory hooks missing"), 2);
      note(
        `Your gateway is missing the memory hooks Quaid needs.\n` +
        `Quaid now requires OpenClaw ${MIN_GATEWAY_VERSION}+ lifecycle hook support.\n\n` +
        `Update your gateway to the latest version:\n` +
        `  npm install -g openclaw\n\n` +
        `Or check: ${HOOKS_PR_URL}`,
        "Gateway update required"
      );
      bail("Gateway hooks required. Update OpenClaw and re-run.");
    }
    s.stop(C.green("Gateway hooks present"));
  }

  // --- Plugin source ---
  s.start("Resolving plugin source...");
  let pluginSrc = "";
  try {
    pluginSrc = await resolvePluginSource();
  } catch (err) {
    s.stop(C.red("Plugin source not found"), 2);
    bail(String((err && err.message) ? err.message : err));
  }
  const srcInfo =
    INSTALL_SOURCE === "github"
      ? `${INSTALL_GITHUB_REPO}@${INSTALL_REF}`
      : (INSTALL_SOURCE === "artifact" ? INSTALL_ARTIFACT : "local workspace");
  s.stop(C.green(`Plugin source ready (${INSTALL_SOURCE}: ${srcInfo})`));

  log.success("All checks passed. Ready to install.");
  log.message("");

  await waitForKey("Press any key to begin installation...");

  // --- Backup (only if existing files) ---
  // Uses snapshots from before clawdbot commands (which create data/memory.db)
  if (_existingFiles.length > 0 || _hasConfig || _hasDb) {
    log.warn("Quaid's nightly janitor modifies your workspace markdown files");
    log.warn("(SOUL.md, USER.md, etc.) to keep them current. Back up first.");

    const doBackup = handleCancel(await confirm({ message: "Create a backup now?" }));
    if (doBackup) {
      const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      const backupDir = path.join(WORKSPACE, `.quaid-backup-${ts}`);
      fs.mkdirSync(backupDir, { recursive: true });
      let count = 0;
      for (const f of ["SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md", "AGENTS.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md"]) {
        const src = path.join(WORKSPACE, f);
        if (fs.existsSync(src)) { fs.copyFileSync(src, path.join(backupDir, f)); count++; }
      }
      if (_hasConfig) { fs.copyFileSync(path.join(CONFIG_DIR, "memory.json"), path.join(backupDir, "memory.json")); count++; }
      if (_hasDb) { fs.copyFileSync(path.join(DATA_DIR, "memory.db"), path.join(backupDir, "memory.db")); count++; }

      note(
        `${C.green(count + " files")} backed up to:\n${C.bcyan(backupDir)}\n\n` +
        `To uninstall Quaid and restore this backup later:\n` +
        `  ${C.bcyan("quaid uninstall")}\n\n` +
        `Backups are stored alongside your workspace.`,
        C.bmag("BACKUP COMPLETE")
      );
      await waitForKey();
    }
  }

  return pluginSrc;
}

// =============================================================================
// Step 2: Detect Owner
// =============================================================================
async function step2_owner() {
  stepHeader(2, TOTAL_INSTALL_STEPS, "IDENTITY", STEP_QUOTES.identity);

  log.info(C.bold("Every memory is stored against an owner name."));
  log.info(C.bold("This is how Quaid keeps memories namespaced — one owner per person."));
  log.info(C.dim("Tell us your real name so memory tags stay human-readable."));
  log.message("");

  const seedName =
    String(INSTALL_ARGS.ownerName || process.env.QUAID_OWNER_NAME || shell("git config user.name 2>/dev/null") || "").trim();
  if (seedName) {
    log.info(`Suggested: ${C.bcyan(seedName)}`);
  } else if (AGENT_MODE) {
    throw new Error("Agent mode requires --owner-name or QUAID_OWNER_NAME so memories are tagged to the person.");
  }

  const display = handleCancel(await text({
    message: "What is your name so we can tag your memories?",
    initialValue: seedName || undefined,
    placeholder: "Solomon",
    validate: (v) => String(v || "").trim().length === 0 ? "Name is required" : undefined,
  }));
  const id = ownerIdFromDisplayName(display);
  log.success(`Owner: ${C.bcyan(display)} ${C.dim(`(${id})`)}`);
  return { display, id };
}

// =============================================================================
// Step 3: Models + Notifications
// =============================================================================
async function step3_models() {
  stepHeader(3, TOTAL_INSTALL_STEPS, "MODELS", STEP_QUOTES.models);

  log.info(C.dim("Quaid uses two LLM tiers: deep reasoning (extraction, review)"));
  log.info(C.dim("and fast reasoning (reranking, classification)."));

  const janitorAskFirst = handleCancel(await confirm({
    message: "Sometimes Quaid changes files and memory records to organize them. Should it ask first before applying those changes?",
    initialValue: true,
  }));

  const autoCompactionOnTimeout = handleCancel(await confirm({
    message: "Do you want to trade off a little less quality for a LOT of savings by auto-compacting after timeout memory extraction?",
    initialValue: true,
  }));

  const advancedSetup = handleCancel(await confirm({
    message: "Advanced setup? (recommended keeps sane defaults)",
    initialValue: false,
  }));

  const forcedProvider = String(process.env.QUAID_INSTALL_PROVIDER || "").trim().toLowerCase();
  let provider = "anthropic";
  let adapterType = _isPlatform("claude-code") ? "claude-code" : _isPlatform("openclaw") ? "openclaw" : "standalone";
  if (advancedSetup) {
    adapterType = handleCancel(await select({
      message: "Agent system adapter",
      initialValue: adapterType,
      options: [
        { value: "standalone", label: "standalone", hint: "local-only runtime" },
        { value: "claude-code", label: "claude-code", hint: "Claude Code hooks + OAuth" },
        { value: "openclaw", label: "openclaw", hint: "gateway-integrated runtime" },
      ],
    }));
    provider = handleCancel(await select({
      message: "LLM provider",
      options: [
        { value: "anthropic",  label: "Anthropic (Claude)", hint: "Recommended" },
        { value: "openai",     label: "OpenAI",             hint: "Experimental" },
        { value: "openrouter", label: "OpenRouter",         hint: "Experimental — multi-provider gateway" },
        { value: "together",   label: "Together AI",        hint: "Experimental" },
        { value: "ollama",     label: "Ollama (local)",     hint: "Experimental — quality depends on model size" },
      ],
    }));
  }
  if (!advancedSetup && ["anthropic", "openai", "openrouter", "together", "ollama"].includes(forcedProvider)) {
    provider = forcedProvider;
    log.info(`Provider override: ${C.bcyan(provider)} ${C.dim("(QUAID_INSTALL_PROVIDER)")}`);
  }

  if (provider !== "anthropic") {
    log.warn(C.bold("Non-Anthropic providers are experimental. Prompts are tuned for Claude."));
    log.warn(C.bold("Extraction quality may vary. You can switch providers later in config."));
    log.message("");
    await waitForKey();
  }

  let highModel, lowModel;
  if (provider === "anthropic") {
    highModel = "claude-opus-4-6";
    lowModel = "claude-haiku-4-5";
  } else if (provider === "ollama") {
    highModel = "llama3.1:70b";
    lowModel = "llama3.1:8b";
  } else {
    highModel = "gpt-4o";
    lowModel = lowModelFor(highModel);
  }

  if (advancedSetup) {
    highModel = handleCancel(await text({
      message: "Deep reasoning model:",
      placeholder: highModel,
      initialValue: highModel,
    }));
    const defaultLow = lowModelFor(highModel);
    lowModel = handleCancel(await text({
      message: "Fast reasoning model:",
      placeholder: defaultLow,
      initialValue: defaultLow,
    }));
  } else {
    log.info(`Deep reasoning: ${C.bcyan(highModel)}  |  Fast reasoning: ${C.bcyan(lowModel)}`);
  }

  // API key — the bot passes its key to Quaid at runtime.
  // No need to check env here.
  const keyEnv = keyEnvFor(provider);

  // Notifications
  let notifLevel = "normal";
  if (advancedSetup) {
    notifLevel = handleCancel(await select({
      message: "Notification verbosity",
      initialValue: "normal",
      options: [
        { value: "quiet",   label: "Quiet",   hint: "Errors only" },
        { value: "normal",  label: "Normal",  hint: "Recommended: janitor/extraction summaries, retrieval off" },
        { value: "verbose", label: "Verbose", hint: "Janitor full + extraction/retrieval summaries" },
        { value: "debug",   label: "Debug",   hint: "Full details on everything" },
      ],
    }));
  } else {
    log.info(C.dim("Notifications: normal (recommended)"));
  }
  const pinnedNotifyRoute = IS_OPENCLAW ? resolvePinnedNotificationRoute() : null;
  const notifChannel = pinnedNotifyRoute?.channel || "last_used";
  if (IS_OPENCLAW && pinnedNotifyRoute?.channel) {
    log.info(C.dim(`Notifications will be pinned to the OpenClaw channel '${pinnedNotifyRoute.channel}' during install.`));
  } else if (IS_OPENCLAW) {
    log.warn("No active OpenClaw notification route detected; falling back to last_used until a channel is established.");
  }
  log.info(C.dim("You can ask your agent to change notification routing or level anytime."));

  const preset = (() => {
    if (notifLevel === "quiet") return { janitor: "off", extraction: "off", retrieval: "off" };
    if (notifLevel === "verbose") return { janitor: "full", extraction: "summary", retrieval: "summary" };
    if (notifLevel === "debug") return { janitor: "full", extraction: "full", retrieval: "full" };
    return { janitor: "summary", extraction: "summary", retrieval: "off" };
  })();

  const advancedNotif = advancedSetup && handleCancel(await confirm({
    message: "Advanced notification config?",
    initialValue: false,
  }));

  let notifConfig = { ...preset };
  if (advancedNotif) {
    const pickVerb = async (message, initialValue) => handleCancel(await select({
      message,
      initialValue,
      options: [
        { value: "off", label: "off", hint: "disable this notification type" },
        { value: "summary", label: "summary", hint: "short operational messages" },
        { value: "full", label: "full", hint: "full detail (debug-heavy)" },
      ],
    }));
    notifConfig = {
      janitor: await pickVerb("Janitor notifications", preset.janitor),
      extraction: await pickVerb("Extraction notifications", preset.extraction),
      retrieval: await pickVerb("Retrieval notifications", preset.retrieval),
    };
  }

  return {
    provider,
    highModel,
    lowModel,
    apiFormat: provider === "anthropic" ? "anthropic" : "openai-compatible",
    apiKeyEnv: keyEnv,
    baseUrl: baseUrlFor(provider),
    notifLevel,
    notifConfig,
    notifChannel,
    advancedSetup,
    adapterType,
    janitorAskFirst,
    autoCompactionOnTimeout,
  };
}

// =============================================================================
// Step 4: Embeddings
// =============================================================================
async function step4_embeddings() {
  stepHeader(4, TOTAL_INSTALL_STEPS, "EMBEDDINGS", STEP_QUOTES.embeddings);

  log.info(C.dim("Embeddings power semantic search — turning text into vectors"));
  log.info(C.dim("so Quaid can find relevant memories by meaning, not just keywords."));
  const { total: totalRam, free: freeRam } = getSystemRAM();
  log.info(C.dim(`System RAM: ${totalRam}GB total, ~${freeRam}GB available`));
  const recommendedByRam =
    (freeRam >= 8 || totalRam >= 24) ? "qwen3-embedding:8b" :
    (freeRam >= 4 || totalRam >= 12) ? "nomic-embed-text" :
    "all-minilm";
  log.info(C.dim(`Recommended embedding by RAM: ${recommendedByRam}`));

  // Check Ollama
  let ollamaRunning = false;
  if (process.env.QUAID_TEST_NO_OLLAMA) {
    // Test mode: simulate Ollama not installed/running
    ollamaRunning = false;
  } else {
    try { execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { stdio: "pipe" }); ollamaRunning = true; } catch {}
  }

  if (!ollamaRunning && !process.env.QUAID_TEST_NO_OLLAMA && canRun("ollama")) {
    log.warn("Ollama is installed but not running.");
    const start = handleCancel(await confirm({ message: "Start Ollama now?" }));
    if (start) {
      const s = spinner();
      s.start("Starting Ollama...");
      try {
        execSync("brew services start ollama 2>/dev/null || (ollama serve >/dev/null 2>&1 &)", { stdio: "pipe" });
        await sleep(3000);
        execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { stdio: "pipe" });
        ollamaRunning = true;
        s.stop(C.green("Ollama started"));
      } catch (err) {
        s.stop("Could not start Ollama");
        const detail = String(err?.message || err || "").trim();
        if (detail) {
          log.warn(`Start failure detail: ${detail}`);
        }
        log.warn("You can start it manually later: ollama serve");
      }
    }
  }

  if (!ollamaRunning && (process.env.QUAID_TEST_NO_OLLAMA || !canRun("ollama"))) {
    log.warn("Ollama not found.");
    log.info(C.dim("Ollama runs embedding models locally — free, fast, and private."));
    log.info(C.dim("Without it, semantic recall degrades substantially."));
    const install = handleCancel(await confirm({
      message: "Install Ollama now? (recommended)",
      initialValue: true,
    }));
    if (install) {
      const s = spinner();
      s.start("Installing Ollama...");
      try {
        if (canRun("brew")) {
          execSync("brew install ollama", { stdio: "pipe" });
          execSync("brew services start ollama 2>/dev/null || true", { stdio: "pipe" });
        } else {
          execSync("curl -fsSL https://ollama.ai/install.sh | sh", { stdio: "inherit" });
          execSync("ollama serve >/dev/null 2>&1 &", { stdio: "pipe" });
        }
        await sleep(3000);
        execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { stdio: "pipe" });
        ollamaRunning = true;
        s.stop(C.green("Ollama installed and running"));
      } catch (err) {
        s.stop("Ollama install had issues");
        const detail = String(err?.message || err || "").trim();
        if (detail) {
          log.warn(`Install failure detail: ${detail}`);
        }
        log.warn("You may need to start it manually: ollama serve");
      }
    } else {
      const proceedDegraded = handleCancel(await confirm({
        message: "Continue without Ollama and accept degraded recall quality?",
        initialValue: false,
      }));
      if (!proceedDegraded) {
        bail("Install cancelled. Re-run after installing Ollama.");
      }
    }
  }

  let embedModel, embedDim;

  if (ollamaRunning) {
    const { total, free } = getSystemRAM();
    const pulledModels = getOllamaModels();
    const loadedModels = getLoadedOllamaModels();

    // Find which known embedding models are already pulled
    const installedEmbedModels = Object.keys(EMBED_MODELS).filter(
      m => pulledModels.some(p => p.startsWith(m.split(":")[0]))
    );

    log.info(`System RAM: ${C.bcyan(total + "GB")} total, ~${C.bcyan(free + "GB")} available`);
    if (installedEmbedModels.length > 0) {
      log.info(`Pulled (on disk): ${C.green(installedEmbedModels.join(", "))}`);
    }
    if (loadedModels.length > 0) {
      log.info(`Loaded (in VRAM): ${C.bcyan(loadedModels.map(m => `${m.name} (${m.vramGB}GB)`).join(", "))}`);
    }
    log.warn("Embedding models use persistent RAM while Ollama is active.");

    // Build options — installed models first, sorted by quality
    const loadedNames = loadedModels.map(m => m.name);
    const options = [];
    for (const [model, info] of Object.entries(EMBED_MODELS)) {
      const installed = installedEmbedModels.includes(model);
      const loaded = loadedNames.some(n => n.startsWith(model.split(":")[0]));
      const fitsRAM = free >= info.ramGB || total >= (info.ramGB * 3);
      let hint = `${info.dim} dim, ~${info.ramGB}GB RAM`;
      if (loaded) {
        hint += " — " + C.green("Loaded in VRAM");
      } else if (installed) {
        hint += " — " + C.green("Pulled");
      } else if (!fitsRAM) {
        hint += " — " + C.yellow("Low RAM");
      }
      if (info.rank === 1) hint += (installed || loaded) ? "" : ` — ${info.quality}`;
      options.push({ value: model, label: model, hint });
    }

    // Cloud API embeddings — backend not yet implemented (see ROADMAP.md)
    // options.push({
    //   value: "text-embedding-3-small",
    //   label: "OpenAI API (cloud)",
    //   hint: "1536 dim, no GPU needed — requires OPENAI_API_KEY, ~$0.02/M tokens",
    // });

    // No embeddings at all
    options.push({
      value: "none",
      label: "None (keyword search only)",
      hint: "No vectors — FTS5 keyword search only, no semantic recall",
    });

    // Default: best installed model, or best that fits RAM, or smallest
    let defaultModel;
    if (installedEmbedModels.length > 0) {
      // Pick the best quality model that's already installed
      defaultModel = installedEmbedModels.sort(
        (a, b) => EMBED_MODELS[a].rank - EMBED_MODELS[b].rank
      )[0];
    } else {
      const canFit = Object.entries(EMBED_MODELS)
        .filter(([, info]) => free >= info.ramGB || total >= (info.ramGB * 3))
        .sort(([, a], [, b]) => a.rank - b.rank);
      defaultModel = canFit.length > 0 ? canFit[0][0] : "all-minilm";
    }

    const choice = handleCancel(await select({
      message: "Embedding model",
      initialValue: defaultModel,
      options,
    }));

    embedModel = choice;

    if (choice === "none") {
      embedDim = 0;
      log.warn("No embedding model selected — semantic search disabled.");
      log.info(C.dim("Quaid will use FTS5 keyword search only. You can add embeddings later."));
      log.success("Keyword-only mode");
    } else {
      embedDim = EMBED_MODELS[choice]?.dim || 384;

      // Check if model is pulled, if not pull it
      const hasPulled = installedEmbedModels.includes(choice);
      if (hasPulled) {
        log.success(`${embedModel} already available`);
      } else {
        const s = spinner();
        s.start(`Downloading ${embedModel}... (this may take a few minutes)`);
        try {
          execSync(`ollama pull ${embedModel}`, { stdio: "pipe", timeout: 600000 });
          s.stop(C.green(`${embedModel} ready`));
        } catch {
          s.stop("Download failed");
          log.warn(`Run 'ollama pull ${embedModel}' manually before using memory.`);
        }
      }
    }
  } else {
    const proceedDegraded = handleCancel(await confirm({
      message: "Ollama is still unavailable. Continue with keyword-only mode (degraded)?",
      initialValue: false,
    }));
    if (!proceedDegraded) {
      bail("Install cancelled. Re-run after starting or installing Ollama.");
    }
    log.warn(C.bold("Proceeding without Ollama — semantic search disabled."));
    log.info(C.bold("Install Ollama later for vector search: https://ollama.ai"));
    embedModel = "none";
    embedDim = 0;
    log.success("Keyword-only mode (FTS5 full-text search)");
  }

  log.warn(C.bold("Changing embedding models later requires re-embedding all stored facts."));
  log.message("");
  await waitForKey();
  return { embedModel, embedDim };
}

// =============================================================================
// Step 5: Janitor Schedule
// =============================================================================
async function step6_schedule(embeddings = {}, advancedSetup = false, janitorAskFirst = true) {
  stepHeader(5, TOTAL_INSTALL_STEPS, "JANITOR", STEP_QUOTES.janitor);

  log.info(C.dim("The janitor runs nightly: reviewing new facts, deduplication,"));
  log.info(C.dim("contradiction detection, memory decay, and doc updates."));
  log.info(C.dim("Takes 5-30 minutes. Budget capped at 60 minutes."));
  log.info("");
  log.info(C.dim("Scheduled via your bot's heartbeat by default (recommended)."));
  log.info(C.dim("The bot passes its API key securely at runtime."));

  // Show existing scheduled tasks for reference
  const existingTasks = getExistingScheduledTasks();
  if (existingTasks.length > 0) {
    log.info("");
    log.info(C.bcyan("Existing scheduled tasks:"));
    for (const task of existingTasks) {
      log.info(C.dim(`  ${task}`));
    }
  }

  const hour = handleCancel(await select({
    message: "When should the janitor run?",
    initialValue: "4",
    options: [
      { value: "2",    label: "2:00 AM",  hint: "Early morning" },
      { value: "3",    label: "3:00 AM",  hint: "Before early morning" },
      { value: "4",    label: "4:00 AM",  hint: "Recommended" },
      { value: "5",    label: "5:00 AM",  hint: "Before most wake up" },
      { value: "custom", label: "Custom",  hint: "Pick your own hour" },
      { value: "skip", label: "Skip",     hint: "I'll configure it myself" },
    ],
  }));

  if (hour === "skip") {
    log.warn("No schedule set.");
    log.warn("You MUST add a janitor entry to your HEARTBEAT.md manually.");
    log.warn(C.dim("See: https://github.com/quaid-labs/quaid#janitor-scheduling"));
    return { hour: null, scheduled: false };
  }

  let scheduleHour;
  if (hour === "custom") {
    const customHour = handleCancel(await text({
      message: "Hour to run (0-23):",
      placeholder: "3",
      validate: v => {
        const n = parseInt(v);
        if (isNaN(n) || n < 0 || n > 23) return "Enter a number 0-23";
        return undefined;
      },
    }));
    scheduleHour = parseInt(customHour);
  } else {
    scheduleHour = parseInt(hour);
  }

  const ampm = scheduleHour === 0 ? "12:00 AM"
             : scheduleHour < 12 ? `${scheduleHour}:00 AM`
             : scheduleHour === 12 ? "12:00 PM"
             : `${scheduleHour - 12}:00 PM`;

  let scheduled = false;

  if (_isPlatform("claude-code")) {
    // Claude Code: schedule via launchd plist (macOS) since there is no
    // gateway heartbeat. The janitor uses the OAuth token from
    // ~/.claude/.credentials.json — no API key env var needed.
    if (process.platform === "darwin") {
      scheduled = installLaunchdSchedule(scheduleHour);
      if (scheduled) {
        log.success(`Janitor scheduled for ${C.bcyan(ampm)} daily via launchd`);
      } else {
        log.warn("Could not install launchd schedule.");
        log.warn(`Run manually: quaid janitor --apply --task all`);
      }

      const scheduleLines = [
        C.yellow("The janitor is scheduled via macOS launchd."),
        C.yellow("It uses your Claude Code OAuth token — no API key needed."),
        "",
        C.bold("The launchd agent runs daily and applies janitor maintenance."),
        C.bold("It will review pending facts, deduplicate, and maintain your"),
        C.bold("knowledge base automatically."),
        "",
        C.dim("To check status: launchctl list | grep quaid"),
        C.dim("To unload: launchctl unload ~/Library/LaunchAgents/com.quaid.janitor.plist"),
      ];
      note(scheduleLines.join("\n"), C.bmag("JANITOR SCHEDULING"));

      handleCancel(await confirm({
        message: "I understand — the janitor runs nightly via launchd.",
        initialValue: true,
      }));
    } else {
      // Linux/other: install crontab entry
      scheduled = installCrontabSchedule(scheduleHour);
      if (scheduled) {
        log.success(`Janitor scheduled for ${C.bcyan(ampm)} daily via crontab`);
      } else {
        log.warn("Could not install crontab schedule.");
        log.warn(`Run manually: quaid janitor --apply --task all`);
      }

      const scheduleLines = [
        C.yellow("The janitor is scheduled via crontab."),
        C.yellow("It uses your Claude Code OAuth token — no API key needed."),
        "",
        C.bold("The cron job runs daily and applies janitor maintenance."),
        "",
        C.dim("To check: crontab -l | grep quaid"),
        C.dim("To remove: crontab -l | grep -v quaid | crontab -"),
      ];
      note(scheduleLines.join("\n"), C.bmag("JANITOR SCHEDULING"));

      handleCancel(await confirm({
        message: "I understand — the janitor runs nightly via crontab.",
        initialValue: true,
      }));
    }
  } else {
    // OpenClaw / Standalone: schedule via HEARTBEAT.md (bot reads on wake)
    scheduled = installHeartbeatSchedule(scheduleHour);

    if (scheduled) {
      log.success(`Janitor scheduled for ${C.bcyan(ampm)} daily via HEARTBEAT.md`);
    } else {
      log.warn("Could not update HEARTBEAT.md automatically.");
      log.warn("Add this to your HEARTBEAT.md manually:");
      log.warn(C.dim(`  Janitor check: if ${scheduleHour}:00, run quaid janitor --apply --task all`));
    }

    // Persistent warning — DO NOT REMOVE. The user must understand this.
    const scheduleLines = [
      C.yellow("The janitor runs via your bot's HEARTBEAT system."),
      C.yellow("This keeps your API key secure — never stored in cron or launchd."),
      "",
      C.bold("Your bot reads HEARTBEAT.md on each wake, checks the time,"),
      C.bold("and runs the janitor with secure key injection."),
    ];

    scheduleLines.push(
      "",
      C.bold("If Quaid warns the janitor hasn't run recently, check that"),
      C.bold("your bot's heartbeat is active and the entry is in HEARTBEAT.md."),
      C.bold("Ask your agent to fix it."),
      "",
      C.dim("Want cron instead? Set your API key in .env and ask your"),
      C.dim("agent to configure it. (Not recommended — less secure.)"),
    );
    note(scheduleLines.join("\n"), C.bmag("JANITOR SCHEDULING"));

    handleCancel(await confirm({
      message: "I understand — the janitor runs from the bot's heartbeat.",
      initialValue: true,
    }));
  }

  const approvalPolicies = janitorAskFirst
    ? {
        coreMarkdownWrites: "ask",
        projectDocsWrites: "ask",
        workspaceFileMovesDeletes: "ask",
        destructiveMemoryOps: "auto",
      }
    : {
        coreMarkdownWrites: "auto",
        projectDocsWrites: "auto",
        workspaceFileMovesDeletes: "auto",
        destructiveMemoryOps: "auto",
      };

  if (advancedSetup) {
    log.message("");
    log.info(C.bold("Janitor Approval Policies"));
    log.info(C.dim("Choose where janitor should ask before applying changes."));

    for (const row of [
      ["coreMarkdownWrites", "Root core markdown writes (SOUL/USER/MEMORY/TOOLS)"],
      ["projectDocsWrites", "Project docs writes outside projects/quaid"],
      ["workspaceFileMovesDeletes", "Workspace file moves/deletes"],
      ["destructiveMemoryOps", "Destructive memory DB ops (merges/supersedes/deletes)"],
    ]) {
      const [key, label] = row;
      const mode = handleCancel(await select({
        message: `${label}:`,
        initialValue: approvalPolicies[key],
        options: [
          { value: "ask", label: "ask", hint: "queue for approval, notify user" },
          { value: "auto", label: "auto", hint: "apply automatically during janitor run" },
        ],
      }));
      approvalPolicies[key] = mode;
    }
  }

  return { hour: scheduleHour, scheduled, approvalPolicies };
}

function getExistingScheduledTasks() {
  const tasks = [];

  // Check crontab
  const crontab = shell("crontab -l 2>/dev/null");
  if (crontab) {
    for (const line of crontab.split("\n")) {
      const trimmed = line.trim();
      if (trimmed && !trimmed.startsWith("#")) {
        tasks.push(`cron: ${trimmed}`);
      }
    }
  }

  // Check launchd agents (macOS) — only show scheduled ones with a time
  if (process.platform === "darwin") {
    const agentDir = path.join(os.homedir(), "Library", "LaunchAgents");
    if (fs.existsSync(agentDir)) {
      try {
        for (const file of fs.readdirSync(agentDir)) {
          if (!file.endsWith(".plist")) continue;
          try {
            const content = fs.readFileSync(path.join(agentDir, file), "utf8");
            // Only show agents that have a scheduled time (StartCalendarInterval)
            const hourMatch = content.match(/<key>Hour<\/key>\s*<integer>(\d+)<\/integer>/);
            if (hourMatch) {
              tasks.push(`launchd: ${file.replace(".plist", "")} (${hourMatch[1]}:00)`);
            }
          } catch { /* skip unreadable */ }
        }
      } catch { /* ignore */ }
    }
  }

  return tasks;
}

function installHeartbeatSchedule(hour) {
  // The janitor MUST run through the bot's heartbeat system because:
  // 1. The bot has the API key in its environment
  // 2. The bot can send notifications to the user
  // 3. A standalone cron/launchd job would not have API credentials
  //
  // We add an entry to HEARTBEAT.md that the bot checks on each wake.
  const heartbeatPath = path.join(WORKSPACE, "HEARTBEAT.md");
  const minEnd = hour === 23 ? 0 : hour + 1;
  const padH = (h) => String(h).padStart(2, "0");
  const scheduleWindowEnd = hour === 23 ? "24:00" : `${padH(minEnd)}:00`;

  const janitorBlock = [
    "",
    `## Quaid Janitor (${padH(hour)}:00 daily)`,
    "",
    `**Schedule:** Check if current time is between ${padH(hour)}:00-${scheduleWindowEnd} and janitor hasn't run today.`,
    "",
    "**IMPORTANT:** The janitor requires your LLM API key. It must be run through",
    "the bot's heartbeat — NOT from a standalone cron job or launchd agent.",
    "The bot passes its API key to the janitor subprocess at runtime.",
    "",
    "**To run:** `quaid janitor --apply --task all`",
    "",
    "**Logic:**",
    `- If time is between ${padH(hour)}:00 and ${scheduleWindowEnd} AND janitor hasn't run today:`,
    "  - Run: `./quaid janitor --apply --task all`",
    "  - Log completion status",
    "- Otherwise: skip (already ran or not time yet)",
    "",
    "**Timeout:** 60 minutes max. Typical: 5-30 minutes.",
    "",
    "## Post-Janitor Review",
    "",
    "If `logs/janitor/pending-project-review.json` exists, the janitor detected",
    "project-specific content in TOOLS.md or AGENTS.md. Read the file and walk",
    "the user through each finding using `projects/quaid/project_onboarding.md`.",
    "Only clear the file after the user has reviewed everything.",
    "",
    "## Quaid Delayed Requests",
    "",
    "If `.quaid/runtime/notes/delayed-llm-requests.json` exists:",
    "- Read all `pending` items when conversation timing is appropriate.",
    "- Surface the important ones to the user, resolve them together, and take action.",
    "- After resolution, mark those items `done` (or remove them).",
    "- Keep unresolved items as `pending` for later follow-up.",
    "",
  ].join("\n");

  try {
    let content = "";
    if (fs.existsSync(heartbeatPath)) {
      content = fs.readFileSync(heartbeatPath, "utf8");
      // Remove any existing Quaid Janitor + Post-Janitor Review sections
      content = content.replace(/\n## Quaid Janitor[^\n]*[\s\S]*?(?=\n## (?!Post-Janitor)|\s*$)/g, "");
      content = content.replace(/\n## Post-Janitor Review[\s\S]*?(?=\n## |\s*$)/g, "");
      content = content.replace(/\n## Quaid Delayed Requests[\s\S]*?(?=\n## |\s*$)/g, "");
    } else {
      content = "# HEARTBEAT.md\n\n# Periodic checks — the bot reads this on each heartbeat wake\n";
    }

    content = content.trimEnd() + "\n" + janitorBlock;
    fs.writeFileSync(heartbeatPath, content);
    return true;
  } catch {
    return false;
  }
}

function installLaunchdSchedule(hour) {
  // macOS launchd plist for nightly janitor.
  // Uses Claude Code's OAuth token (via ~/.claude/.credentials.json) —
  // no API key env var needed. The quaid CLI resolves QUAID_HOME and
  // adapter type from embedded env vars.
  if (process.platform !== "darwin") {
    log.warn("launchd is macOS-only. Install a cron job manually for this platform.");
    return false;
  }

  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const label = "com.quaid.janitor";
  const plistPath = path.join(os.homedir(), "Library", "LaunchAgents", `${label}.plist`);
  const logPath = path.join(LOGS_DIR, "janitor", "launchd.log");
  const errPath = path.join(LOGS_DIR, "janitor", "launchd-err.log");

  // Ensure log directory exists
  fs.mkdirSync(path.join(LOGS_DIR, "janitor"), { recursive: true });

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${quaidCmd}</string>
    <string>janitor</string>
    <string>--task</string>
    <string>all</string>
    <string>--apply</string>
    <string>--time-budget</string>
    <string>3600</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QUAID_HOME</key>
    <string>${WORKSPACE}</string>
    <key>QUAID_ADAPTER</key>
    <string>claude-code</string>
    <key>PYTHONPATH</key>
    <string>${PLUGIN_DIR}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>${hour}</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${logPath}</string>
  <key>StandardErrorPath</key>
  <string>${errPath}</string>
  <key>Nice</key>
  <integer>10</integer>
</dict>
</plist>
`;

  try {
    // Unload existing if present
    if (fs.existsSync(plistPath)) {
      spawnSync("launchctl", ["unload", plistPath], { stdio: "pipe" });
    }

    fs.mkdirSync(path.dirname(plistPath), { recursive: true });
    fs.writeFileSync(plistPath, plist);

    // Load the new schedule
    const loadResult = spawnSync("launchctl", ["load", plistPath], { stdio: "pipe" });
    if (loadResult.status !== 0) {
      log.warn("launchctl load failed — you may need to load manually:");
      log.warn(C.dim(`  launchctl load ${plistPath}`));
      return false;
    }

    return true;
  } catch {
    return false;
  }
}

function installCrontabSchedule(hour) {
  // Linux/other: crontab entry for nightly janitor.
  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const logPath = path.join(LOGS_DIR, "janitor", "cron.log");

  fs.mkdirSync(path.join(LOGS_DIR, "janitor"), { recursive: true });

  const envVars = `QUAID_HOME='${WORKSPACE}' QUAID_ADAPTER=claude-code PYTHONPATH='${PLUGIN_DIR}'`;
  const cronLine = `30 ${hour} * * * ${envVars} ${quaidCmd} janitor --task all --apply --time-budget 3600 >> ${logPath} 2>&1`;
  const marker = "# quaid-janitor";

  try {
    const existing = shell("crontab -l 2>/dev/null") || "";

    // Already installed?
    if (existing.includes(marker)) {
      // Replace existing entry
      const lines = existing.split("\n").filter(l => !l.includes(marker) && l.trim() !== "");
      lines.push(`${cronLine} ${marker}`);
      const { status } = spawnSync("crontab", ["-"], {
        input: lines.join("\n") + "\n",
        stdio: ["pipe", "pipe", "pipe"],
      });
      return status === 0;
    }

    // Add new entry
    const newCrontab = existing.trimEnd() + "\n" + `${cronLine} ${marker}` + "\n";
    const { status } = spawnSync("crontab", ["-"], {
      input: newCrontab,
      stdio: ["pipe", "pipe", "pipe"],
    });
    return status === 0;
  } catch {
    return false;
  }
}

// =============================================================================
// Step 7: Install & Migrate
// =============================================================================
async function step7_install(pluginSrc, owner, models, embeddings, systems, janitorPolicies = null) {
  stepHeader(6, TOTAL_INSTALL_STEPS, "INSTALL", STEP_QUOTES.install);

  const s = spinner();
  let postInstallStateStabilized = false;

  // Create directories
  s.start("Creating directories...");
  for (const dir of [CONFIG_DIR, DATA_DIR, JOURNAL_DIR, LOGS_DIR, path.join(JOURNAL_DIR, "archive")]) {
    fs.mkdirSync(dir, { recursive: true });
  }
  s.stop(C.green("Directories created"));

  // Copy/sync plugin source
  const pluginDirEmpty = !fs.existsSync(PLUGIN_DIR) || fs.readdirSync(PLUGIN_DIR).length === 0;
  let samePluginTree = false;
  try {
    samePluginTree = fs.realpathSync(pluginSrc) === fs.realpathSync(PLUGIN_DIR);
  } catch {
    samePluginTree = false;
  }
  if (samePluginTree) {
    log.info("Plugin source already in place");
  } else {
    s.start(pluginDirEmpty ? "Installing plugin source..." : "Syncing plugin source...");
    fs.mkdirSync(PLUGIN_DIR, { recursive: true });
    copyDirSync(pluginSrc, PLUGIN_DIR);
    s.stop(C.green(pluginDirEmpty ? "Plugin installed" : "Plugin synced"));
  }
  const skipBinShim = String(process.env.QUAID_INSTALL_SKIP_BIN_SHIM || "").trim() === "1";
  if (skipBinShim) {
    log.info("Skipping ~/bin/quaid shim update (QUAID_INSTALL_SKIP_BIN_SHIM=1).");
  } else if (ensureQuaidCliShim(PLUGIN_DIR)) {
    log.info(`Updated CLI shim: ${path.join(os.homedir(), "bin", "quaid")} -> ${path.join(PLUGIN_DIR, "quaid")}`);
  } else {
    log.warn("Could not update ~/bin/quaid shim automatically.");
  }

  // Install Node dependencies (typebox etc.)
  const pluginPkg = path.join(PLUGIN_DIR, "package.json");
  const pluginNodeMods = path.join(PLUGIN_DIR, "node_modules");
  if (fs.existsSync(pluginPkg) && !fs.existsSync(pluginNodeMods)) {
    s.start("Installing plugin dependencies...");
    const npmResult = spawnSync("npm", ["install", "--omit=dev", "--omit=peer", "--no-audit", "--no-fund"], {
      cwd: PLUGIN_DIR, stdio: "pipe", timeout: 60000,
    });
    if (npmResult.status === 0) {
      s.stop(C.green("Dependencies installed"));
    } else {
      s.stop(C.yellow("npm install failed — plugin may not load"));
      log.warn("Try running manually: cd " + PLUGIN_DIR + " && npm install --omit=dev --omit=peer");
    }
  }

  // Legacy hook is deprecated; reset/compaction is now handled by lifecycle contracts.
  log.info("Legacy hook quaid-reset-signal is deprecated and no longer needed (no action required).");
  if (IS_OPENCLAW) {
    s.start("Registering Quaid plugin in OpenClaw...");
    const reg = _registerOpenClawQuaidPlugin(PLUGIN_DIR);
    if (!reg.ok) {
      s.stop(C.red("OpenClaw plugin registration failed"));
      throw new Error(reg.reason || "openclaw plugins install/enable failed");
    }
    s.stop(C.green("OpenClaw plugin registered"));
    if (_ensureOpenClawPluginsAllowQuaid()) {
      log.info("Ensured plugins.allow includes: quaid");
    }
    await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "plugin registration", 8_000);
    enableRequiredOpenClawHooks();
  }
  if (_isPlatform("claude-code")) {
    // Create per-instance identity directory for SOUL.md, USER.md, MEMORY.md
    const identityDir = path.join(WORKSPACE, "claude-code", "identity");
    if (!fs.existsSync(identityDir)) {
      fs.mkdirSync(identityDir, { recursive: true });
      log.info(`Created identity directory: ${identityDir}`);
    }
    setupClaudeCodeHooks();
  }

  // sqlite-vec is required and already checked in preflight; re-verify here.
  if (!_hasSqliteVec()) {
    throw new Error("sqlite-vec is required for vector retrieval");
  }

  // Initialize database
  s.start("Initializing database...");
  const dbPath = path.join(DATA_DIR, "memory.db");
  const schemaPath = path.join(PLUGIN_DIR, "datastore/memorydb/schema.sql");
  if (!fs.existsSync(schemaPath)) {
    s.stop(C.red("Database initialization failed"));
    throw new Error(`schema.sql not found: ${schemaPath}`);
  }
  const initScript = `
import sqlite3
conn = sqlite3.connect(${JSON.stringify(dbPath)})
with open(${JSON.stringify(schemaPath)}) as f:
    conn.executescript(f.read())
conn.close()
`;
  const initResult = spawnSync("python3", ["-c", initScript], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
  if (initResult.status !== 0) {
    s.stop(C.red("Database initialization failed"));
    const detail = (initResult.stderr || initResult.stdout || "").trim();
    throw new Error(detail || "python schema initialization failed");
  }
  const verifyScript = `
import sqlite3
conn = sqlite3.connect(${JSON.stringify(dbPath)})
row = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='nodes'").fetchone()
conn.close()
print(int(row[0] if row else 0))
`;
  const verifyResult = spawnSync("python3", ["-c", verifyScript], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
  const nodesTableCount = Number((verifyResult.stdout || "").trim());
  if (verifyResult.status !== 0 || !Number.isFinite(nodesTableCount) || nodesTableCount < 1) {
    s.stop(C.red("Database initialization failed"));
    const detail = (verifyResult.stderr || verifyResult.stdout || "").trim();
    throw new Error(detail || "nodes table missing after schema initialization");
  }
  try { fs.chmodSync(dbPath, 0o600); } catch {}
  s.stop(C.green("Database initialized"));

  // Write config
  s.start("Writing configuration...");
  writeConfig(owner, models, embeddings, systems, janitorPolicies);
  s.stop(C.green("Config written"));

  // Installer-owned contract bootstrap: load config once so datastore init/config
  // hooks run exactly once (for all enabled datastores).
  const domainInitScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from config import get_config
_cfg = get_config()
print('[+] Datastore init hooks complete')
`;
  const domainInitResult = spawnSync("python3", ["-c", domainInitScript], {
    cwd: PLUGIN_DIR,
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
  });
  if (domainInitResult.status !== 0) {
    const detail = String(domainInitResult.stderr || domainInitResult.stdout || "").trim();
    log.warn(`Datastore init hook bootstrap failed during install; continuing. ${detail || ""}`.trim());
  }

  // Contract-owned project workspace dirs should exist after datastore init hooks.
  // Some runtime profiles trim plugin slots during bootstrap; guard here so
  // install always yields expected workspace shape.
  const contractOwnedDirs = [PROJECTS_DIR, TEMP_DIR, SCRATCH_DIR];
  const missingContractOwnedDirs = contractOwnedDirs.filter((dir) => !fs.existsSync(dir));
  if (missingContractOwnedDirs.length > 0) {
    for (const dir of missingContractOwnedDirs) {
      fs.mkdirSync(dir, { recursive: true });
    }
    log.warn(
      `Datastore init did not materialize ${missingContractOwnedDirs.length} contract-owned workspace dir(s); `
      + "installer created them as fallback."
    );
  }

  // Scratch is intentionally workspace-visible and can hold ad-hoc drafts.
  // The directory is contract-owned (docsdb init); installer only bootstraps
  // local history after contract init has run.
  if (fs.existsSync(SCRATCH_DIR) && !fs.existsSync(path.join(SCRATCH_DIR, ".git"))) {
    const scratchGitInit = spawnSync("git", ["init"], {
      cwd: SCRATCH_DIR,
      stdio: "pipe",
      encoding: "utf8",
    });
    if (scratchGitInit.status === 0) {
      log.info("Initialized scratch/ local git history");
    } else {
      const detail = String(scratchGitInit.stderr || scratchGitInit.stdout || "").trim();
      log.warn(`Could not initialize scratch/ git history${detail ? `: ${detail}` : ""}`);
    }
  } else if (!fs.existsSync(SCRATCH_DIR)) {
    log.warn("scratch/ directory missing after datastore init hooks; skipping scratch history bootstrap.");
  }

  // Create workspace files
  for (const f of ["SOUL.md", "USER.md", "MEMORY.md"]) {
    const fp = path.join(WORKSPACE, f);
    if (!fs.existsSync(fp)) {
      fs.writeFileSync(fp, `# ${f.replace(".md", "")}\n`);
      log.info(`Created ${f}`);
    }
  }

  // Create journal files
  if (systems.journal) {
    for (const f of ["SOUL", "USER", "MEMORY"]) {
      const jf = path.join(JOURNAL_DIR, `${f}.journal.md`);
      if (!fs.existsSync(jf)) {
        fs.writeFileSync(jf, `# ${f} Journal\n`);
      }
    }
    log.info("Journal files created");
  }

  // Initialize git repo for workspace (required for doc staleness tracking)
  const gitDir = path.join(WORKSPACE, ".git");
  if (!fs.existsSync(gitDir)) {
    s.start("Initializing git repository...");
    spawnSync("git", ["init"], { cwd: WORKSPACE, stdio: "pipe" });
    // Create .gitignore for runtime artifacts
    const gitignore = [
      "# Runtime data",
      "data/*.db",
      "data/*.db-*",
      "logs/",
      "temp/",
      "scratch/",
      ".env",
      ".env.*",
      "",
      "# Python",
      "__pycache__/",
      "*.pyc",
      ".pytest_cache/",
      "",
      "# OS",
      ".DS_Store",
      "Thumbs.db",
      "",
      "# Build",
      "node_modules/",
      "build/",
      "",
    ].join("\n");
    const ignorePath = path.join(WORKSPACE, ".gitignore");
    if (!fs.existsSync(ignorePath)) {
      fs.writeFileSync(ignorePath, gitignore);
    }
    // Initial commit so git diff/log have a baseline
    spawnSync("git", ["add", "-A"], { cwd: WORKSPACE, stdio: "pipe" });
    const initCommit = spawnSync("git", ["commit", "-m", "Initial Quaid workspace"], { cwd: WORKSPACE, stdio: "pipe" });
    if (initCommit.status !== 0) {
      const fallbackCommit = spawnSync(
        "git",
        ["-c", "user.name=Quaid Installer", "-c", "user.email=installer@local", "commit", "-m", "Initial Quaid workspace"],
        { cwd: WORKSPACE, stdio: "pipe" },
      );
      if (fallbackCommit.status !== 0) {
        s.stop(C.yellow("Git initialized (baseline commit skipped: identity not configured)"));
      } else {
        s.stop(C.green("Git repository initialized"));
      }
    } else {
      s.stop(C.green("Git repository initialized"));
    }
  } else {
    log.info("Git repository already exists");
  }

  // Create owner Person node
  s.start("Creating owner node...");
  const safeDisplay = owner.display.replace(/'/g, "\\'");
  const safeId = owner.id.replace(/'/g, "\\'");
  const storeScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store
try:
    store('${safeDisplay}', owner_id='${safeId}', category='person', source='installer')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
`;
  spawnSync("python3", ["-c", storeScript], { cwd: PLUGIN_DIR, stdio: "pipe" });
  s.stop(C.green(`Owner node: ${owner.display}`));

  // Migration
  let migrationCompleted = false;
  const mdFiles = ["SOUL.md", "USER.md", "TOOLS.md", "MEMORY.md", "AGENTS.md"]
    .filter(f => {
      const fp = path.join(WORKSPACE, f);
      if (!fs.existsSync(fp)) return false;
      const lines = fs.readFileSync(fp, "utf8").split("\n").length;
      return lines > 5;
    });

  if (mdFiles.length > 0) {
    log.info(`Found existing workspace files: ${C.bcyan(mdFiles.join(", "))}`);
    const doMigrate = handleCancel(await confirm({
      message: "Import facts from existing files into memory? (uses LLM processing)",
      initialValue: true,
    }));
    if (doMigrate) {
      if (IS_OPENCLAW) {
        log.info("Waiting for OpenClaw gateway/plugin route to finish warming up...");
        await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "workspace migration");
      }
      s.start("Extracting facts from workspace files...");
      const useMock = process.env.QUAID_TEST_MOCK_MIGRATION === "1";
      const migrateScript = useMock ? `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store
files = ${JSON.stringify(mdFiles)}
total = 0
for fname in files:
    fpath = os.path.join(${JSON.stringify(WORKSPACE)}, fname)
    with open(fpath) as f:
        lines = f.read().strip().split('\\n')
    for line in lines:
        line = line.strip().lstrip('- ')
        if line and not line.startswith('#') and len(line) > 15:
            cat = 'preference' if any(w in line.lower() for w in ['prefer', 'like', 'enjoy', 'favorite']) else 'fact'
            store(line, owner_id='${safeId}', category=cat, source='migration')
            total += 1
print(total)
` : `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store
from core.llm.clients import call_deep_reasoning, parse_json_response
files = ${JSON.stringify(mdFiles)}
total = 0
for fname in files:
    fpath = os.path.join(${JSON.stringify(WORKSPACE)}, fname)
    with open(fpath) as f:
        content = f.read().strip()
    if len(content) < 50:
        continue
    prompt = f"""Extract factual information from this document. Return a JSON array of objects:
[{{"fact": "...", "category": "fact|preference|belief|experience"}}]
Only extract clear, specific facts. Skip meta-information and formatting.
Document ({fname}):\\n{content}"""
    try:
        response, _ = call_deep_reasoning(prompt, max_tokens=4000)
    except Exception as e:
        print(f'warn: migration llm call failed for {fname}: {e}', file=sys.stderr)
        continue
    if response:
        try:
            parsed = parse_json_response(response)
        except Exception as e:
            print(f'warn: migration response parse failed for {fname}: {e}', file=sys.stderr)
            parsed = None
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and 'fact' in item:
                    store(item['fact'], owner_id='${safeId}', category=item.get('category', 'fact'), source='migration')
                    total += 1
print(total)
`;
      const result = spawnSync("python3", ["-c", migrateScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      const factCount = (result.stdout || "").trim();
      if (result.status === 0) {
        migrationCompleted = true;
        s.stop(C.green(`Migration complete: ${factCount} facts extracted`));
        log.info(C.bold("These facts will be reviewed and deduplicated by the nightly janitor."));
      } else {
        s.stop(C.yellow("Migration failed; prompting again after install"));
        const detail = (result.stderr || result.stdout || "").trim();
        if (detail) {
          log.warn(detail);
        }
      }
      log.message("");
      await waitForKey();
    }
  }

  if (!postInstallStateStabilized) {
    const postInstall = _stabilizePostInstallExtractionState();
    postInstallStateStabilized = true;
    log.info(
      `Marked prior sessions as extracted: seeded ${postInstall.cursorsSeeded} cursor(s), `
      + `cleared ${postInstall.pendingSignalsCleared} pending signal(s), `
      + `${postInstall.timeoutBuffersCleared} stale timeout buffer(s).`
    );
  }

  // Projects system — always create a default project with PROJECT.md
  if (systems.projects) {
    const existingDirs = [];
    // Scan projects/ for existing project directories
    try {
      for (const entry of fs.readdirSync(PROJECTS_DIR, { withFileTypes: true })) {
        if (entry.isDirectory() && !entry.name.startsWith(".") && entry.name !== "staging") {
          existingDirs.push(entry.name);
        }
      }
    } catch { /* no projects dir yet */ }

    // Register any existing project directories (e.g. migrating from clawdbot)
    if (existingDirs.length > 0) {
      log.info(`Found ${C.bcyan(existingDirs.length)} existing project dir(s): ${C.bcyan(existingDirs.join(", "))}`);
      s.start("Registering existing projects...");
      const projNames = JSON.stringify(existingDirs);
      const registerScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.docsdb.registry import DocsRegistry
reg = DocsRegistry()
names = ${projNames}
total_docs = 0
for name in names:
    proj_dir = os.path.join(${JSON.stringify(PROJECTS_DIR)}, name)
    project_md = os.path.join(proj_dir, 'PROJECT.md')
    try:
        if not os.path.exists(project_md):
            reg.create_project(name, home_dir=f'projects/{name}/')
        else:
            # Already has PROJECT.md — just register+discover
            from config import ProjectDefinition, reload_config
            defn = ProjectDefinition(
                label=name.replace('-',' ').title(),
                home_dir=f'projects/{name}/',
                source_roots=[], auto_index=True,
                patterns=['*.md'], exclude=['*.db','*.log','*.pyc','__pycache__/'],
                description=f'{name.replace("-"," ").title()} project.',
            )
            reg.save_project_definition(name, defn)
            reload_config()
            reg._config = None
        found = reg.auto_discover(name)
        total_docs += len(found)
    except Exception as e:
        print(f'warn: {name}: {e}', file=sys.stderr)
print(total_docs)
`;
      const regResult = spawnSync("python3", ["-c", registerScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      const regCount = (regResult.stdout || "").trim();
      s.stop(C.green(`Registered ${existingDirs.length} project(s), ${regCount} doc(s) indexed`));
    }

    log.info(C.dim("Your agent can discover more projects — ask it to \"set up projects\""));

    // Install Quaid project reference docs and constitutional guidance
    const quaidProjDir = path.join(PROJECTS_DIR, "quaid");
    fs.mkdirSync(quaidProjDir, { recursive: true });
    const quaidProjSrc = path.join(__dirname, "projects", "quaid");
    for (const f of ["TOOLS.md", "AGENTS.md", "USER.md", "SOUL.md", "MEMORY.md", "ARCHITECTURE.md", "project_onboarding.md"]) {
      const src = path.join(quaidProjSrc, f);
      const dst = path.join(quaidProjDir, f);
      if (fs.existsSync(src) && !fs.existsSync(dst)) {
        fs.copyFileSync(src, dst);
      }
    }
    // Create PROJECT.md for Quaid itself
    if (!fs.existsSync(path.join(quaidProjDir, "PROJECT.md"))) {
      fs.writeFileSync(path.join(quaidProjDir, "PROJECT.md"), [
        "# Quaid Knowledge Layer",
        "",
        "Persistent long-term knowledge layer. Stores facts, relationships, and preferences",
        "in a local SQLite graph database. Retrieved automatically via hybrid search.",
        "",
        "## Key Files",
        "- `TOOLS.md` — How to use project tools and recall paths effectively",
        "- `AGENTS.md` — Project behavior rules and operating guidance",
        "- `USER.md` — Journaling guidance for user-understanding entries",
        "- `SOUL.md` — Journaling guidance for agent self-reflection entries",
        "- `MEMORY.md` — Journaling guidance for shared-moment entries",
        "- `ARCHITECTURE.md` — Full system architecture and design",
        "- `project_onboarding.md` — Guide for discovering and registering projects",
        "",
        "## Systems",
        "- **Knowledge** — Fact extraction, graph storage, hybrid recall",
        "- **Journal** — Slow-path learning, personality evolution",
        "- **Projects** — Documentation tracking, staleness detection, RAG search",
        "- **Workspace** — Core markdown monitoring, nightly maintenance",
        "",
      ].join("\n"));
    }
    // Register Quaid as a project unless it was already covered by existing project scan.
    const quaidAlreadyRegisteredViaExisting = existingDirs.includes("quaid");
    const regQuaidScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.docsdb.registry import DocsRegistry
reg = DocsRegistry()
try:
    reg.create_project('quaid', label='Quaid Knowledge Layer', description='Knowledge layer reference docs and agent instructions.')
except ValueError:
    pass  # already exists
found = reg.auto_discover('quaid')
print(len(found))
`;
    if (quaidAlreadyRegisteredViaExisting) {
      log.info("Quaid project docs were already registered in the existing-project scan; skipping duplicate registration pass.");
    } else {
      const regQuaidResult = spawnSync("python3", ["-c", regQuaidScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      const quaidDocCount = (regQuaidResult.stdout || "").trim();
      log.info(`Quaid project installed (${quaidDocCount} new docs discovered)`);
    }

    // Keep projects/quaid/TOOLS.md domain block aligned after install.
    spawnSync("python3", ["scripts/sync-tools-domain-block.py", "--workspace", WORKSPACE], {
      cwd: PLUGIN_DIR,
      stdio: "pipe",
      env: { ...process.env, QUAID_HOME: WORKSPACE, CLAWDBOT_WORKSPACE: WORKSPACE },
    });
  }

  if (!postInstallStateStabilized) {
    const postInstall = _stabilizePostInstallExtractionState();
    postInstallStateStabilized = true;
    log.info(
      `Marked prior sessions as extracted: seeded ${postInstall.cursorsSeeded} cursor(s), `
      + `cleared ${postInstall.pendingSignalsCleared} pending signal(s), `
      + `${postInstall.timeoutBuffersCleared} stale timeout buffer(s).`
    );
  }
  log.success("Installation complete!");
  log.message("");
  try {
    const markerDir = path.join(LOGS_DIR, "janitor");
    const markerPath = path.join(markerDir, "pending-install-migration.json");
    fs.mkdirSync(markerDir, { recursive: true });
    if (migrationCompleted || mdFiles.length === 0) {
      try { fs.rmSync(markerPath, { force: true }); } catch {}
    } else {
      fs.writeFileSync(
        markerPath,
        JSON.stringify({
          createdAt: new Date().toISOString(),
          status: "pending",
          prompt: "Hey, I see you just installed Quaid. Want me to help migrate important context into managed memory now?"
        }, null, 2) + "\n",
        "utf8"
      );
    }
  } catch {}
  await waitForKey("Press any key to run validation...");
}

// =============================================================================
// Step 8: Validation
// =============================================================================
async function step8_validate(owner, models, embeddings, systems) {
  stepHeader(7, TOTAL_INSTALL_STEPS, "VALIDATION", STEP_QUOTES.validate);

  const s = spinner();
  s.start("Running health checks...");

  const checks = [];

  // Database
  const dbPath = path.join(DATA_DIR, "memory.db");
  if (fs.existsSync(dbPath)) {
    const tableProbe = `
import sqlite3
c = sqlite3.connect(${JSON.stringify(dbPath)})
print(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
c.close()
`;
    const tableResult = spawnSync("python3", ["-c", tableProbe], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    const tables = tableResult.status === 0 ? (tableResult.stdout || "").trim() : "unknown";
    checks.push(`${C.green("■")} Database     ${C.dim("—")} ${tables} tables`);
  } else {
    checks.push(`${C.red("■")} Database     ${C.dim("—")} MISSING`);
  }

  // Embeddings
  let ollamaOk = false;
  try { execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { stdio: "pipe" }); ollamaOk = true; } catch {}
  if (ollamaOk) {
    checks.push(`${C.green("■")} Embeddings   ${C.dim("—")} ${embeddings.embedModel} (${embeddings.embedDim} dim)`);
  } else if (embeddings.embedModel === "text-embedding-3-small") {
    checks.push(`${C.yellow("■")} Embeddings   ${C.dim("—")} Cloud (${embeddings.embedModel})`);
  } else {
    checks.push(`${C.red("■")} Embeddings   ${C.dim("—")} Ollama not running`);
  }

  checks.push(`${C.green("■")} LLM (high)   ${C.dim("—")} ${models.highModel}`);
  checks.push(`${C.green("■")} LLM (fast)   ${C.dim("—")} ${models.lowModel}`);

  if (fs.existsSync(path.join(CONFIG_DIR, "memory.json"))) {
    checks.push(`${C.green("■")} Config       ${C.dim("—")} OK`);
  } else {
    checks.push(`${C.red("■")} Config       ${C.dim("—")} MISSING`);
  }

  checks.push(`${C.green("■")} Owner        ${C.dim("—")} ${owner.display} (${owner.id})`);

  const enabledSystems = Object.entries(systems).filter(([,v]) => v).map(([k]) => k).join(", ");
  checks.push(`${C.green("■")} Systems      ${C.dim("—")} ${enabledSystems}`);

  s.stop(C.green("Health checks complete"));
  note(checks.join("\n"), C.bmag("STATUS"));

  // Smoke test
  if (IS_OPENCLAW) {
    log.info("Waiting for OpenClaw gateway/plugin route to finish warming up...");
    await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "validation smoke test");
  }
  s.start("Smoke test (store + recall)...");
  const smokeSafeId = owner.id.replace(/'/g, "\\'");
  const smokeScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store, recall
try:
    store('Quaid installer smoke test fact', owner_id='${smokeSafeId}', category='fact', source='installer-test')
    results = recall('installer smoke test', owner_id='${smokeSafeId}', limit=1)
    if results:
        print('OK')
    else:
        print('PARTIAL')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
    print('PARTIAL')
`;
  const smoke = spawnSync("python3", ["-c", smokeScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
  const smokeResult = (smoke.stdout || "").trim();
  if (smoke.status !== 0) {
    s.stop(C.red("Smoke test failed — Python execution error"));
    const detail = (smoke.stderr || smoke.stdout || "").trim();
    if (detail) {
      log.warn(detail);
    }
  } else if (smokeResult === "OK") {
    s.stop(C.green("Smoke test passed — store and recall working"));
  } else {
    s.stop(C.yellow("Smoke test partial — store OK, recall needs embeddings"));
  }

  const nextSteps = [
    `${C.bcyan("→")} Facts are extracted automatically on context compaction and new sessions`,
    `${C.bcyan("→")} The nightly janitor reviews, deduplicates, and maintains memories`,
    `${C.bcyan("→")} Run the janitor now to discover and organize your projects:`,
    `   ${C.bcyan("cd modules/quaid && python3 core/lifecycle/janitor.py --task workspace --apply")}`,
    `   The janitor will scan TOOLS.md and AGENTS.md for project specs`,
    `   and flag them for review. Your agent will walk you through the`,
    `   findings on your next conversation and help organize them.`,
    `${C.bcyan("→")} Run ${C.bcyan("quaid doctor")} anytime to check system health`,
    `${C.bcyan("→")} Run ${C.bcyan("quaid stats")} to see your memory database grow`,
    `${C.bcyan("→")} Run ${C.bcyan("quaid config edit")} to customize advanced settings anytime`,
    "",
    C.dim(`Docs: ${PROJECT_URL}`),
    C.dim(`Uninstall: quaid uninstall`),
  ].join("\n");
  note(nextSteps, C.bmag("NEXT STEPS"));

  outro(C.bcyan(`  "${STEP_QUOTES.outro}"  `));
}

// =============================================================================
// Helpers
// =============================================================================

function lowModelFor(high) {
  const map = {
    "claude-opus-4-6": "claude-haiku-4-5",
    "claude-sonnet-4-5": "claude-haiku-4-5",
    "gpt-4o": "gpt-4o-mini",
    "gpt-5.2": "gpt-5-mini",
    "gemini-2.5-pro": "gemini-2.0-flash",
    "gemini-3-pro": "gemini-3-flash",
  };
  return map[high] || high;
}

function keyEnvFor(provider) {
  const map = {
    anthropic: "ANTHROPIC_API_KEY",
    openai: "OPENAI_API_KEY",
    openrouter: "OPENROUTER_API_KEY",
    together: "TOGETHER_API_KEY",
    ollama: "",
  };
  return map[provider] || "ANTHROPIC_API_KEY";
}

function baseUrlFor(provider) {
  const ollamaResolved = (process.env.OLLAMA_URL || "http://localhost:11434").replace(/\/+$/, "");
  const map = {
    openrouter: "https://openrouter.ai/api/v1",
    together: "https://api.together.xyz/v1",
    ollama: ollamaResolved.endsWith("/v1") ? ollamaResolved : `${ollamaResolved}/v1`,
  };
  return map[provider] || null;
}

function findGateway() {
  const rawCandidates = discoverOpenClawRoots();
  const candidates = rawCandidates.filter((candidate) => !/\.npm-backup/i.test(path.basename(candidate)));
  const usable = candidates.length > 0 ? candidates : rawCandidates;

  // Prefer the package root backing the currently active CLI binary.
  // This avoids picking stale npm-backup trees during e2e bootstrap.
  for (const cli of ["openclaw", "clawdbot"]) {
    const cliBin = shell(`command -v ${cli} 2>/dev/null`) || "";
    if (!cliBin) continue;
    const real = fs.existsSync(cliBin) ? fs.realpathSync(cliBin) : cliBin;
    const cliRoot = findPackageRootFrom(real);
    if (cliRoot && usable.includes(cliRoot)) {
      return cliRoot;
    }
  }

  for (const candidate of usable) {
    if (fs.existsSync(path.join(candidate, "package.json"))) {
      return candidate;
    }
  }
  return null;
}

function gatewayHasHooks(gwDir) {
  for (const sub of ["dist", "src"]) {
    const dir = path.join(gwDir, sub);
    if (!fs.existsSync(dir)) continue;
    const out = shell(`grep -rl "runBeforeCompaction\\|before_compaction" "${dir}" 2>/dev/null | head -1`);
    if (out) return true;  // before_compaction is the critical hook
  }
  return false;
}

function parseVersionTriplet(raw) {
  const m = String(raw || "").trim().match(/(\d+)\.(\d+)\.(\d+)/);
  if (!m) return null;
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

function compareVersionTriplets(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] > b[i]) return 1;
    if (a[i] < b[i]) return -1;
  }
  return 0;
}

function isVersionAtLeast(actualRaw, minimumRaw) {
  const actual = parseVersionTriplet(actualRaw);
  const minimum = parseVersionTriplet(minimumRaw);
  if (!actual || !minimum) return false;
  return compareVersionTriplets(actual, minimum) >= 0;
}

function readGatewayVersion(gwDir) {
  try {
    const pkgPath = path.join(gwDir, "package.json");
    const raw = fs.readFileSync(pkgPath, "utf8");
    const parsed = JSON.parse(raw);
    return String(parsed?.version || "").trim();
  } catch {
    return "";
  }
}

function enableRequiredOpenClawHooks() {
  const cli = canRun("openclaw") ? "openclaw" : (canRun("clawdbot") ? "clawdbot" : "");
  if (!cli) {
    throw new Error("OpenClaw CLI not found; cannot enable required hooks.");
  }

  // Keep installer behavior strict and production-faithful: no alias fallback and no
  // direct openclaw.json edits. If required hooks cannot be enabled via CLI, install fails.
  const requiredHooks = ["bootstrap-extra-files"];
  log.info("Explicitly enabling required OpenClaw hooks: bootstrap-extra-files");
  for (const hookName of requiredHooks) {
    const res = runCliWithTimeout(cli, ["hooks", "enable", hookName], 25_000);
    const out = `${String(res.stdout || "")}\n${String(res.stderr || "")}`;
    if (res.status === 0 || /already enabled/i.test(out)) {
      log.info(`Hook enabled: ${hookName}`);
      continue;
    }
    throw new Error(`Could not enable required hook '${hookName}': ${renderCliFailure(res, 25_000)}`);
  }
}

function ensureQuaidCliShim(pluginDirPath) {
  try {
    const target = path.join(pluginDirPath, "quaid");
    if (!fs.existsSync(target)) {
      return false;
    }
    const binDir = path.join(os.homedir(), "bin");
    const shimPath = path.join(binDir, "quaid");
    fs.mkdirSync(binDir, { recursive: true });
    fs.rmSync(shimPath, { force: true });
    fs.symlinkSync(target, shimPath);
    return true;
  } catch {
    return false;
  }
}

function setupClaudeCodeHooks() {
  const settingsPath = path.join(os.homedir(), ".claude", "settings.json");
  let settings = {};
  if (fs.existsSync(settingsPath)) {
    try {
      settings = JSON.parse(fs.readFileSync(settingsPath, "utf8"));
    } catch {
      settings = {};
    }
  }

  if (!settings.hooks) settings.hooks = {};

  // Resolve the quaid binary path. Use absolute paths so multiple installs
  // can coexist — each instance's hooks point to its own quaid script.
  // QUAID_HOME sets the data directory; QUAID_ADAPTER forces claude-code
  // adapter even if the config says standalone/openclaw (enables shared DB).
  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const envPrefix = `QUAID_HOME='${WORKSPACE}' QUAID_ADAPTER=claude-code`;

  const desiredHooks = {
    SessionStart: [
      {
        matcher: "",
        hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-session-init` }],
      },
    ],
    UserPromptSubmit: [{
      matcher: "",
      hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-inject` }],
    }],
    PreCompact: [{
      matcher: "",
      hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-extract --precompact` }],
    }],
  };

  let changed = false;
  for (const [event, hookList] of Object.entries(desiredHooks)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = hookList;
      changed = true;
    } else {
      // Check if quaid hooks already exist for this event
      const existingCmds = new Set();
      for (const entry of settings.hooks[event]) {
        for (const h of (entry.hooks || [])) {
          existingCmds.add(h.command || "");
        }
      }
      for (const entry of hookList) {
        for (const h of (entry.hooks || [])) {
          if (!existingCmds.has(h.command)) {
            settings.hooks[event].push(entry);
            changed = true;
          }
        }
      }
    }
  }

  if (changed) {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
    log.info(`Claude Code hooks configured in ${settingsPath}`);
  } else {
    log.info("Claude Code hooks already configured");
  }
}

async function tryBrewInstall(pkg, label) {
  if (AGENT_MODE) {
    log.warn(`Agent mode: skipping auto-install for ${label}. Install manually: brew install ${pkg}`);
    return false;
  }
  if (!canRun("brew")) {
    const installBrew = handleCancel(await confirm({
      message: "Homebrew is not installed. Install it now?",
    }));
    if (installBrew) {
      const s = spinner();
      s.start("Installing Homebrew...");
      try {
        execSync('/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"', { stdio: "inherit" });
        if (fs.existsSync("/opt/homebrew/bin/brew")) {
          process.env.PATH = `/opt/homebrew/bin:${process.env.PATH}`;
        }
        s.stop(C.green("Homebrew installed"));
      } catch {
        s.stop("Homebrew install failed");
        return false;
      }
    } else {
      log.warn(`Install manually: brew install ${pkg}`);
      return false;
    }
  }

  const doInstall = handleCancel(await confirm({
    message: `Install ${label} via Homebrew? (brew install ${pkg})`,
  }));
  if (!doInstall) {
    log.warn(`Install manually: brew install ${pkg}`);
    return false;
  }

  const s = spinner();
  s.start(`Installing ${label}...`);
  try {
    execSync(`brew install ${pkg}`, { stdio: "pipe", timeout: 300000 });
    s.stop(C.green(`${label} installed`));
    return true;
  } catch {
    s.stop(`brew install ${pkg} failed`);
    return false;
  }
}

function writeConfig(owner, models, embeddings, systems, janitorPolicies = null) {
  const resolvedAdapterType = models.adapterType || (_isPlatform("claude-code") ? "claude-code" : _isPlatform("openclaw") ? "openclaw" : "standalone");
  const policies = janitorPolicies || {
    coreMarkdownWrites: "ask",
    projectDocsWrites: "ask",
    workspaceFileMovesDeletes: "ask",
    destructiveMemoryOps: "auto",
  };
  const config = {
    adapter: { type: resolvedAdapterType },
    plugins: {
      enabled: true,
      strict: true,
      apiVersion: 1,
      // Include module path explicitly; pathlib rglob does not reliably recurse
      // into symlinked plugin dirs across environments.
      paths: ["modules/quaid", "plugins"],
      allowList: ["memorydb.core", "docsdb.core", "core.extract", "openclaw.adapter", "claude_code.adapter"],
      slots: {
        adapter: resolvedAdapterType === "openclaw" ? "openclaw.adapter" : resolvedAdapterType === "claude-code" ? "claude_code.adapter" : "",
        ingest: ["core.extract"],
        dataStores: ["memorydb.core", "docsdb.core"],
      },
      config: {
        "memorydb.core": {},
        "docsdb.core": {},
        "core.extract": {},
      },
    },
    systems,
    models: {
      llmProvider: models.apiFormat,
      apiKeyEnv: models.apiKeyEnv,
      baseUrl: models.baseUrl,
      fastReasoning: models.lowModel,
      deepReasoning: models.highModel,
      fastReasoningContext: 200000,
      deepReasoningContext: 200000,
      fastReasoningMaxOutput: 8192,
      deepReasoningMaxOutput: 16384,
      batchBudgetPercent: 0.5,
    },
    capture: {
      enabled: true,
      strictness: "high",
      inactivityTimeoutMinutes: 60,
      autoCompactionOnTimeout: models.autoCompactionOnTimeout ?? true,
      skipPatterns: ["^(thanks|ok|sure|yes|no)$", "^(hi|hello|hey)\\b"],
    },
    decay: {
      enabled: true,
      thresholdDays: 30,
      ratePercent: 10,
      minimumConfidence: 0.1,
      protectVerified: true,
      protectPinned: true,
      reviewQueueEnabled: true,
      mode: "exponential",
      baseHalfLifeDays: 60,
      accessBonusFactor: 0.15,
    },
    janitor: {
      enabled: true,
      dryRun: false,
      applyMode: models.janitorAskFirst ? "ask" : "auto",
      approvalPolicies: policies,
      taskTimeoutMinutes: 60,
      opusReview: { enabled: true, batchSize: 50, maxTokens: 4000 },
      dedup: {
        similarityThreshold: 0.85,
        highSimilarityThreshold: 0.95,
        autoRejectThreshold: 0.98,
        grayZoneLow: 0.88,
        llmVerifyEnabled: true,
      },
      contradiction: { enabled: true, timeoutMinutes: 60, minSimilarity: 0.6, maxSimilarity: 0.85 },
    },
    retrieval: {
      defaultLimit: 5,
      maxLimit: 8,
      minSimilarity: 0.6,
      notifyMinSimilarity: 0.85,
      boostRecent: true,
      boostFrequent: true,
      maxTokens: 2000,
      reranker: { enabled: true, topK: 20 },
      rrfK: 60,
      rerankerBlend: 0.5,
      compositeRelevanceWeight: 0.60,
      compositeRecencyWeight: 0.20,
      compositeFrequencyWeight: 0.15,
      multiPassGate: 0.70,
      mmrLambda: 0.7,
      coSessionDecay: 0.6,
      recencyDecayDays: 90,
      useHyde: true,
      traversal: { useBeam: true, beamWidth: 5, maxDepth: 2, hopDecay: 0.7 },
    },
    logging: {
      enabled: true,
      level: "info",
      retentionDays: 30,
      components: ["memory", "janitor"],
    },
    notifications: {
      level: models.notifLevel,
      janitor: { verbosity: models.notifConfig?.janitor ?? "summary", channel: models.notifChannel || "last_used" },
      extraction: { verbosity: models.notifConfig?.extraction ?? "summary", channel: models.notifChannel || "last_used" },
      retrieval: { verbosity: models.notifConfig?.retrieval ?? "off", channel: models.notifChannel || "last_used" },
      projectCreate: { enabled: true },
      fullText: false,
      showProcessingStart: false,
    },
    docs: {
      autoUpdateOnCompact: true,
      maxDocsPerUpdate: 3,
      stalenessCheckEnabled: true,
      updateTimeoutSeconds: 120,
      coreMarkdown: {
        enabled: true,
        monitorForBloat: true,
        monitorForOutdated: true,
        files: {
          "SOUL.md": { purpose: "Personality and interaction style", maxLines: 80 },
          "USER.md": { purpose: "About the user", maxLines: 150 },
          "MEMORY.md": { purpose: "Core memories loaded every session", maxLines: 100 },
        },
      },
      journal: {
        enabled: true,
        snippetsEnabled: true,
        mode: "distilled",
        journalDir: "journal",
        targetFiles: ["SOUL.md", "USER.md", "MEMORY.md"],
        maxEntriesPerFile: 50,
        maxTokens: 8192,
        distillationIntervalDays: 7,
        archiveAfterDistillation: true,
      },
      sourceMapping: {},
      docPurposes: {},
    },
    projects: {
      enabled: true,
      projectsDir: "projects/",
      stagingDir: "projects/staging/",
      definitions: {},
      defaultProject: "quaid",
    },
    users: {
      defaultOwner: owner.id,
      identities: {
        [owner.id]: {
          channels: { cli: ["*"] },
          speakers: [owner.display, owner.id, "The user"],
          personNodeName: owner.display,
        },
      },
    },
    database: {
      path: "data/memory.db",
      archivePath: "data/memory_archive.db",
      walMode: true,
    },
    ollama: {
      url: (process.env.OLLAMA_URL || "http://localhost:11434").replace(/\/v1\/?$/, "").replace(/\/+$/, ""),
      embeddingModel: embeddings.embedModel,
      embeddingDim: embeddings.embedDim,
    },
    rag: {
      docsDir: "docs",
      chunkMaxTokens: 800,
      chunkOverlapTokens: 100,
      maxResults: 5,
      searchLimit: 5,
      minSimilarity: 0.3,
    },
  };

  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  fs.writeFileSync(path.join(CONFIG_DIR, "memory.json"), JSON.stringify(config, null, 2) + "\n");
}

function copyDirSync(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "node_modules") continue;
    if (entry.name === ".git") continue;
    if (entry.name === "__pycache__") continue;
    if (entry.name.endsWith(".pyc")) continue;
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDirSync(srcPath, destPath);
    } else {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function _messageDedupKey(msg) {
  const id = typeof msg?.id === "string" ? msg.id : "";
  if (id) return `id:${id}`;
  const ts = typeof msg?.timestamp === "string" ? msg.timestamp : "";
  const role = typeof msg?.role === "string" ? msg.role : "";
  const text = (typeof msg?.content === "string" ? msg.content : "").slice(0, 200);
  return `fallback:${ts}:${role}:${text}`;
}

function _parseMessageTimestampMs(msg) {
  const ts = msg?.timestamp;
  if (typeof ts === "number" && Number.isFinite(ts)) return ts;
  if (typeof ts === "string") {
    const asNum = Number(ts);
    if (Number.isFinite(asNum)) return asNum;
    const parsed = Date.parse(ts);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function _stabilizePostInstallExtractionState() {
  const dataDir = path.join(WORKSPACE, "data");
  const sessionMessagesDir = path.join(LOGS_DIR, "session-messages");
  const cursorDir = path.join(dataDir, "session-cursors");
  const pendingSignalsDir = path.join(dataDir, "pending-extraction-signals");
  const timeoutBuffersDir = path.join(dataDir, "timeout-buffers");
  const summary = { cursorsSeeded: 0, pendingSignalsCleared: 0, timeoutBuffersCleared: 0 };

  try {
    fs.mkdirSync(cursorDir, { recursive: true });
    if (fs.existsSync(sessionMessagesDir)) {
      for (const name of fs.readdirSync(sessionMessagesDir)) {
        if (!name.endsWith(".jsonl")) continue;
        const sessionId = name.replace(/\.jsonl$/, "");
        const fp = path.join(sessionMessagesDir, name);
        const lines = fs.readFileSync(fp, "utf8").split("\n").filter(Boolean);
        let last = null;
        for (const line of lines) {
          try {
            const parsed = JSON.parse(line);
            if (parsed && typeof parsed === "object") last = parsed;
          } catch {}
        }
        if (!last) continue;
        const payload = {
          sessionId,
          clearedAt: new Date().toISOString(),
          lastMessageKey: _messageDedupKey(last),
        };
        const ts = _parseMessageTimestampMs(last);
        if (typeof ts === "number") payload.lastTimestampMs = ts;
        fs.writeFileSync(path.join(cursorDir, `${sessionId}.json`), JSON.stringify(payload), { mode: 0o600 });
        summary.cursorsSeeded += 1;
      }
    }
  } catch (err) {
    log.warn(`Post-install cursor seeding failed: ${String(err?.message || err)}`);
  }

  for (const dir of [pendingSignalsDir, timeoutBuffersDir]) {
    try {
      if (!fs.existsSync(dir)) continue;
      for (const name of fs.readdirSync(dir)) {
        if (!name.endsWith(".json") && !name.includes(".processing.")) continue;
        try {
          fs.unlinkSync(path.join(dir, name));
          if (dir === pendingSignalsDir) summary.pendingSignalsCleared += 1;
          if (dir === timeoutBuffersDir) summary.timeoutBuffersCleared += 1;
        } catch {}
      }
    } catch (err) {
      log.warn(`Post-install cleanup failed for ${dir}: ${String(err?.message || err)}`);
    }
  }

  return summary;
}

function _gatewayHttpCode(pathname, method = "GET", body = null) {
  if (!canRun("curl")) return 0;
  const rawPort = String(process.env.OPENCLAW_GATEWAY_PORT || "18789").trim();
  const port = /^[0-9]+$/.test(rawPort) ? rawPort : "18789";
  const url = `http://127.0.0.1:${port}${pathname}`;
  const args = ["-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "2", "-X", method, url];
  if (body !== null) {
    args.push("-H", "Content-Type: application/json", "--data", body);
  }
  const res = spawnSync("curl", args, { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
  if (res.status !== 0) return 0;
  const code = Number.parseInt(String(res.stdout || "").trim(), 10);
  return Number.isFinite(code) ? code : 0;
}

async function waitForGatewayWarmup(timeoutMs = 12000) {
  if (!IS_OPENCLAW || !canRun("curl")) return true;
  const startedAt = Date.now();
  let nextHeartbeatAt = startedAt + 30_000;
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  while (Date.now() < deadline) {
    const now = Date.now();
    const health = _gatewayHttpCode("/health", "GET", null);
    const responses = _gatewayHttpCode("/v1/responses", "POST", "{}");
    const pluginLlm = _gatewayHttpCode("/plugins/quaid/llm", "POST", "{}");
    if (health === 200 && ((responses >= 100 && responses <= 599) || (pluginLlm >= 100 && pluginLlm <= 599))) {
      return true;
    }
    if (now >= nextHeartbeatAt) {
      const elapsedSec = Math.floor((now - startedAt) / 1000);
      const remainingSec = Math.max(0, Math.ceil((deadline - now) / 1000));
      log.info(
        `Still waiting for gateway warmup (${elapsedSec}s elapsed, ~${remainingSec}s remaining)` +
        ` [health=${health} responses=${responses} plugin=${pluginLlm}]`
      );
      nextHeartbeatAt += 30_000;
    }
    await sleep(500);
  }
  return false;
}

let _installNotifyUnavailableLogged = false;

function _resolveInstallerMessageCli() {
  return shell("command -v openclaw 2>/dev/null") || shell("command -v clawdbot 2>/dev/null") || "";
}

function _resolveLastChannelFromSessions() {
  const candidates = [];
  const home = os.homedir();
  const root = path.join(home, ".openclaw", "agents");
  try {
    if (fs.existsSync(root)) {
      for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        candidates.push(path.join(root, entry.name, "sessions", "sessions.json"));
      }
    }
  } catch {}
  candidates.push(path.join(home, ".openclaw", "agents", "main", "sessions", "sessions.json"));
  candidates.push(path.join(home, ".openclaw", "sessions", "sessions.json"));

  const scoreTs = (session, fallbackMs) => {
    const raw =
      session?.lastActivityAt
      || session?.lastMessageAt
      || session?.updatedAt
      || session?.lastSeenAt
      || session?.createdAt
      || "";
    const ts = Date.parse(String(raw || ""));
    if (Number.isFinite(ts)) return ts;
    return fallbackMs;
  };

  const channelPriority = (channel) => {
    const key = String(channel || "").trim().toLowerCase();
    if (!key) return 0;
    if (key === "telegram") return 50;
    if (key === "discord" || key === "slack" || key === "whatsapp") return 40;
    if (key === "tui") return -100;
    return 10;
  };

  let best = null;
  for (const sessionsPath of candidates) {
    try {
      if (!fs.existsSync(sessionsPath)) continue;
      const fileStat = fs.statSync(sessionsPath);
      const sessions = JSON.parse(fs.readFileSync(sessionsPath, "utf8"));
      for (const session of Object.values(sessions || {})) {
        const channel = String(session?.lastChannel || "").trim();
        const target = String(session?.lastTo || "").trim();
        const account = String(session?.lastAccountId || "").trim();
        if (!channel || !target) continue;
        const tsScore = scoreTs(session, fileStat.mtimeMs);
        const score = tsScore + channelPriority(channel);
        if (!best || score > best.score) {
          best = { channel, target, account, score };
        }
      }
    } catch {}
  }
  if (!best) return null;
  return { channel: best.channel, target: best.target, account: best.account };
}

function _resolveInstallerNotifyOverride() {
  const channel = String(process.env.QUAID_INSTALL_NOTIFY_CHANNEL || "").trim();
  const target = String(process.env.QUAID_INSTALL_NOTIFY_TARGET || "").trim();
  const account = String(process.env.QUAID_INSTALL_NOTIFY_ACCOUNT || "").trim();
  if (!channel || !target) return null;
  return { channel, target, account };
}

function resolvePinnedNotificationRoute() {
  return _resolveInstallerNotifyOverride() || _resolveLastChannelFromSessions();
}

function sendInstallerNotification(message) {
  if (!AGENT_MODE || !IS_OPENCLAW) return false;
  if (String(process.env.QUAID_INSTALL_NOTIFY || "1").trim() === "0") return false;

  const cli = _resolveInstallerMessageCli();
  const lastChannel = _resolveInstallerNotifyOverride() || _resolveLastChannelFromSessions();
  if (cli && lastChannel) {
    const args = [
      "message", "send",
      "--channel", lastChannel.channel,
      "--target", lastChannel.target,
      "--message", String(message || ""),
    ];
    if (lastChannel.account && lastChannel.account !== "default") {
      args.push("--account", lastChannel.account);
    }
    const cliRes = spawnSync(cli, args, {
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 15_000,
    });
    if (!cliRes.error && cliRes.status === 0) return true;
    if (!_installNotifyUnavailableLogged) {
      _installNotifyUnavailableLogged = true;
      const detail = String(cliRes.stderr || cliRes.stdout || "").trim();
      log.warn(`Installer notification via CLI failed: ${detail || `exit ${String(cliRes.status)}`}`);
    }
  }

  // Fallback once plugin is installed/configured: adapter notify path.
  const py = `
import os, sys
sys.path.insert(0, ${JSON.stringify(PLUGIN_DIR)})
from core.runtime.notify import notify_user
ok = notify_user(${JSON.stringify(message)})
print("ok" if ok else "no_channel")
`;
  const env = { ...process.env };
  const sep = process.platform === "win32" ? ";" : ":";
  env.QUAID_HOME = WORKSPACE;
  env.CLAWDBOT_WORKSPACE = WORKSPACE;
  env.PYTHONPATH = env.PYTHONPATH ? `${PLUGIN_DIR}${sep}${env.PYTHONPATH}` : PLUGIN_DIR;

  const res = spawnSync("python3", ["-c", py], {
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    env,
    timeout: 15_000,
  });
  if (res.error) {
    if (!_installNotifyUnavailableLogged) {
      _installNotifyUnavailableLogged = true;
      log.warn(`Installer notification unavailable: ${String(res.error.message || res.error)}`);
    }
    return false;
  }
  if (res.status !== 0) {
    const detail = String(res.stderr || res.stdout || "").trim();
    if (!_installNotifyUnavailableLogged) {
      _installNotifyUnavailableLogged = true;
      log.warn(`Installer notification unavailable: ${detail || "python exited non-zero"}`);
    }
    return false;
  }
  const out = String(res.stdout || "").trim();
  if (out === "ok") return true;
  if (out && out !== "ok" && !_installNotifyUnavailableLogged) {
    _installNotifyUnavailableLogged = true;
    log.warn(`Installer notification status: ${out}`);
  }
  return false;
}

function notifyInstallCheckpoint(step, total, title, detail, funLine = "") {
  if (String(process.env.QUAID_INSTALL_NOTIFY_PROGRESS || "1").trim() === "0") return;
  const lines = [
    `🛠️ Quaid install checkpoint ${step}/${total}: ${title}`,
    detail,
  ];
  if (funLine) lines.push(funLine);
  sendInstallerNotification(lines.join("\n"));
}

function notifyInstallCompletion(owner, models, embeddings, systems) {
  if (String(process.env.QUAID_INSTALL_NOTIFY_COMPLETE || "1").trim() === "0") return;
  const summary = [
    "✅ Quaid install complete.",
    `Owner: ${owner.display}`,
    `Workspace: ${WORKSPACE}`,
    `Models: deep=${models.highModel}, fast=${models.lowModel}`,
    `Embeddings: ${embeddings.embedModel}`,
    `Notification channel: ${models.notifChannel || "last_used"}`,
    "No memory mutants detected.",
  ].join("\n");
  sendInstallerNotification(summary);
}

function notifyInstallWarmupNotice() {
  if (String(process.env.QUAID_INSTALL_NOTIFY_PROGRESS || "1").trim() === "0") return;
  sendInstallerNotification(
    "⏳ Quaid install needs to restart the OpenClaw gateway to apply changes.\n" +
    "This pause is expected and can take 2-5 minutes while the gateway comes back online."
  );
}

// =============================================================================
// Main
// =============================================================================
async function main() {
  try {
    if (AGENT_MODE) {
      log.info("Agent mode enabled: using non-interactive defaults where prompts are normally required.");
      log.info(`Workspace override: ${WORKSPACE}`);
    }
    notifyInstallCheckpoint(0, TOTAL_INSTALL_STEPS, "boot", "Installer started in agent mode.", "Spinning up Rekall vibes...");
    const pluginSrc = await step1_preflight();
    notifyInstallCheckpoint(1, TOTAL_INSTALL_STEPS, "preflight", "Dependencies checked and plugin source resolved.", "All systems nominal.");
    const owner = await step2_owner();
    notifyInstallCheckpoint(2, TOTAL_INSTALL_STEPS, "identity", `Owner tagged as ${owner.display}.`, "Memory now has a name.");
    const models = await step3_models();
    notifyInstallCheckpoint(3, TOTAL_INSTALL_STEPS, "models", `Deep=${models.highModel}, Fast=${models.lowModel}.`, "Brains selected.");
    const embeddings = await step4_embeddings();
    notifyInstallCheckpoint(4, TOTAL_INSTALL_STEPS, "embeddings", `Embedding model set to ${embeddings.embedModel}.`, "Semantic radar online.");
    const systems = { memory: true, journal: true, projects: true, workspace: true };
    const schedule = await step6_schedule(embeddings, models.advancedSetup, models.janitorAskFirst);
    notifyInstallCheckpoint(
      5, TOTAL_INSTALL_STEPS, "janitor",
      "Janitor policy and schedule configured. Next step may pause while gateway/plugin restarts and warms up.",
      "Night shift assigned. Warmup can take a minute or two."
    );
    notifyInstallWarmupNotice();
    log.info("Heads up: OpenClaw gateway now needs a restart to apply changes. A 2-5 minute pause here is expected while it comes back online.");
    await step7_install(pluginSrc, owner, models, embeddings, systems, schedule?.approvalPolicies || null);
    notifyInstallCheckpoint(6, TOTAL_INSTALL_STEPS, "install", "Plugin installed, config written, migration/registration complete.", "Blueprint phase complete.");
    await step8_validate(owner, models, embeddings, systems);
    notifyInstallCheckpoint(7, TOTAL_INSTALL_STEPS, "validation", "Smoke checks passed.", "No richters spotted.");
    notifyInstallCompletion(owner, models, embeddings, systems);

    // In test mode, write results for the test runner to verify
    if (_testAnswers && process.env.QUAID_TEST_RESULTS) {
      fs.writeFileSync(process.env.QUAID_TEST_RESULTS, JSON.stringify({
        success: true,
        owner,
        models: { provider: models.provider, highModel: models.highModel, lowModel: models.lowModel },
        embeddings,
        systems,
        schedule,
        workspace: WORKSPACE,
        answersUsed: _testIdx,
      }, null, 2));
    }
  } catch (err) {
    if (err.message === "Setup cancelled.") process.exit(0);
    if (_testAnswers && process.env.QUAID_TEST_RESULTS) {
      fs.writeFileSync(process.env.QUAID_TEST_RESULTS, JSON.stringify({
        success: false,
        error: err.message,
        stack: err.stack,
        answersUsed: _testIdx,
      }, null, 2));
    }
    console.error(`\n${C.red("[x] Unexpected error:")} ${err.message}`);
    console.error(err.stack);
    process.exit(1);
  }
}

main();
