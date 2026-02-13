#!/usr/bin/env node
// =============================================================================
// Quaid Memory Plugin — Guided Installer
// =============================================================================
// Interactive installer using @clack/prompts (resolved from OpenClaw).
// Run from your OpenClaw workspace root:  node setup-quaid.mjs
//
// Author: Solomon Steadman (https://github.com/solstead)
// License: MIT
// Prerequisites: OpenClaw installed, configured, and running.
// =============================================================================

import { execSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// --- Constants ---
const VERSION = "0.1.0-alpha";
const HOOKS_PR_URL = "https://github.com/openclaw/openclaw/pull/13287";
const PROJECT_URL = "https://github.com/steadman-labs/quaid";
const WORKSPACE = process.env.CLAWDBOT_WORKSPACE || process.cwd();
const PLUGIN_DIR = path.join(WORKSPACE, "plugins", "quaid");
const CONFIG_DIR = path.join(WORKSPACE, "config");
const DATA_DIR = path.join(WORKSPACE, "data");
const JOURNAL_DIR = path.join(WORKSPACE, "journal");
const LOGS_DIR = path.join(WORKSPACE, "logs");
const PROJECTS_DIR = path.join(WORKSPACE, "projects");

// Step-specific quotes — each tied to the step's theme
const STEP_QUOTES = {
  preflight:  "Get ready for a surprise!",
  identity:   "If I am not me, then who the hell am I?",
  models:     "What is it that you want, Mr. Quaid?",
  embeddings: "Ever heard of Rekall? They sell fake memories.",
  systems:    "A man is defined by his actions, not his memory.",
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

// --- Resolve @clack/prompts from the OpenClaw installation ---
let clack;
try {
  const cliBin = shell("which clawdbot || which openclaw", true);
  const resolved = fs.realpathSync(cliBin);
  const pkgRoot = path.join(path.dirname(resolved), "..");
  const clackPath = path.join(pkgRoot, "node_modules", "@clack", "prompts", "dist", "index.mjs");
  if (fs.existsSync(clackPath)) {
    clack = await import(clackPath);
  }
} catch { /* fall through */ }

if (!clack) {
  for (const base of [
    "/opt/homebrew/lib/node_modules/clawdbot",
    "/opt/homebrew/lib/node_modules/openclaw",
    "/usr/local/lib/node_modules/clawdbot",
    "/usr/local/lib/node_modules/openclaw",
  ]) {
    const p = path.join(base, "node_modules", "@clack", "prompts", "dist", "index.mjs");
    if (fs.existsSync(p)) {
      try { clack = await import(p); break; } catch { /* next */ }
    }
  }
}

if (!clack) {
  console.error(C.red("[x] Could not find @clack/prompts from your OpenClaw installation."));
  console.error("    Make sure OpenClaw is installed: npm install -g openclaw");
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
  : _clack.select;
const confirm = _testAnswers
  ? async (opts) => { const a = _nextAnswer("confirm", opts.message); log.info(C.dim(`[test] confirm "${opts.message}" → ${a}`)); return a; }
  : _clack.confirm;
const text = _testAnswers
  ? async (opts) => { const a = _nextAnswer("text", opts.message); log.info(C.dim(`[test] text "${opts.message}" → ${a}`)); return a; }
  : _clack.text;
const spinner = _testAnswers
  ? () => ({ start: (m) => log.info(C.dim(`[test] spinner: ${m}`)), stop: (m) => log.info(C.dim(`[test] done: ${m}`)) })
  : _clack.spinner;

// --- Helpers ---
function shell(cmd, trim = true) {
  try {
    const out = execSync(cmd, { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], cwd: os.homedir() });
    return trim ? out.trim() : out;
  } catch { return ""; }
}

function canRun(cmd) {
  return spawnSync("which", [cmd], { stdio: "pipe" }).status === 0;
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
  if (_testAnswers) return; // skip in test mode
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
    const raw = execSync("curl -sf http://localhost:11434/api/tags", { encoding: "utf8", cwd: os.homedir() });
    const data = JSON.parse(raw);
    return (data.models || []).map(m => m.name || "");
  } catch { return []; }
}

function getLoadedOllamaModels() {
  // Returns models currently loaded in VRAM (/api/ps)
  try {
    const raw = execSync("curl -sf http://localhost:11434/api/ps", { encoding: "utf8", cwd: os.homedir() });
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
  const lines = [
    "",
    C.bmag("              ██████    ██    ██   ██████   ██  ██████"),
    C.bmag("             ██    ██   ██    ██  ██    ██  ██  ██   ██"),
    C.bmag("             ██    ██   ██    ██  ████████  ██  ██   ██"),
    C.bmag("             ██ ▄▄ ██   ██    ██  ██    ██  ██  ██   ██"),
    C.bmag("              ██████    ▀██████▀  ██    ██  ██  ██████"),
    C.bmag("                 ▀▀"),
    " ".repeat(41) + C.dim("by Solomon Steadman"),
    " ".repeat(48) + C.dim(`v${VERSION}`),
    "",
    " ".repeat(19) + C.cyan("░▒▓") + C.bold(" LONG-TERM MEMORY SYSTEM ") + C.cyan("▓▒░"),
    "",
  ];
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
  stepHeader(1, 8, "PREFLIGHT", STEP_QUOTES.preflight);
  intro(C.dim("Checking your system..."));

  log.info(C.dim(`Workspace: ${WORKSPACE}`));

  // Snapshot existing files BEFORE any clawdbot commands — those commands load
  // the quaid plugin which creates data/memory.db, giving a false "dirty" signal.
  const _existingFiles = ["SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md", "AGENTS.md"]
    .filter(f => fs.existsSync(path.join(WORKSPACE, f)));
  const _hasConfig = fs.existsSync(path.join(CONFIG_DIR, "memory.json"));
  const _hasDb = fs.existsSync(path.join(DATA_DIR, "memory.db"));

  const s = spinner();

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
                    shell("openclaw status 2>/dev/null </dev/null");
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
    bail("OpenClaw gateway is not running.");
  }

  // --- Onboarding complete ---
  // Try both CLIs; pick whichever returns valid agent config (must contain "id")
  const cbAgents = shell("clawdbot config get agents 2>/dev/null </dev/null | strings");
  const ocAgents = shell("openclaw config get agents 2>/dev/null </dev/null | strings");
  const agentConfig = (cbAgents.includes('"id"') ? cbAgents : null) ||
                      (ocAgents.includes('"id"') ? ocAgents : null) || "";
  if (!agentConfig.includes('"id"')) {
    s.stop(C.red("Onboarding incomplete"), 2);
    note(
      "Quaid needs at least one agent configured.\n" +
      "Run the setup wizard first:\n\n" +
      "  openclaw setup\n\n" +
      "Then re-run this installer.",
      "Setup required"
    );
    bail("OpenClaw setup has not been completed.");
  }
  s.stop(C.green("OpenClaw") + " gateway running");

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

  // --- Gateway hooks ---
  s.start("Checking gateway memory hooks...");
  const gwDir = findGateway();
  if (!gwDir) {
    s.stop(C.red("Gateway not found"), 2);
    bail("Could not locate the OpenClaw gateway installation.");
  }
  const hasHooks = gatewayHasHooks(gwDir);
  if (!hasHooks) {
    s.stop(C.red("Memory hooks missing"), 2);
    note(
      `Your gateway is missing the memory hooks from PR #13287.\n` +
      `Quaid hooks into conversation events to extract memories.\n\n` +
      `Install from the hooks branch:\n` +
      `  npm install -g solstead/openclaw#plugin-memory-hooks\n\n` +
      `Or check: ${HOOKS_PR_URL}`,
      "Gateway patch required"
    );
    bail("Gateway hooks required. Patch and re-run.");
  }
  s.stop(C.green("Gateway hooks present"));

  // --- Plugin source ---
  s.start("Locating plugin source...");
  const pluginSrc = [
    path.join(__dirname, "plugins", "quaid"),
    PLUGIN_DIR,
  ].find(p => {
    try {
      return fs.existsSync(p) && fs.statSync(p).isDirectory() && fs.readdirSync(p).length > 0;
    } catch { return false; }
  });
  if (!pluginSrc) {
    s.stop(C.red("Plugin source not found"), 2);
    bail(`Expected at ${path.join(__dirname, "plugins", "quaid")} or ${PLUGIN_DIR}`);
  }
  s.stop(C.green("Plugin source found"));

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
      const backupDir = path.join(os.homedir(), ".quaid-backups", ts);
      fs.mkdirSync(backupDir, { recursive: true });
      let count = 0;
      for (const f of ["SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md", "AGENTS.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md"]) {
        const src = path.join(WORKSPACE, f);
        if (fs.existsSync(src)) { fs.copyFileSync(src, path.join(backupDir, f)); count++; }
      }
      if (_hasConfig) { fs.copyFileSync(path.join(CONFIG_DIR, "memory.json"), path.join(backupDir, "memory.json")); count++; }
      if (_hasDb) { fs.copyFileSync(path.join(DATA_DIR, "memory.db"), path.join(backupDir, "memory.db")); count++; }

      note(
        `${C.green(count + " files")} backed up to:\n${C.bcyan(backupDir)}`,
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
  stepHeader(2, 8, "IDENTITY", STEP_QUOTES.identity);

  log.info(C.bold("Every memory is stored against an owner name."));
  log.info(C.bold("This is how Quaid keeps memories namespaced — one owner per person."));
  log.info(C.dim("Multi-user support is planned. For now, this is your identity."));
  log.message("");

  let detected = shell("git config user.name 2>/dev/null") || process.env.USER || "";

  if (detected) {
    log.info(`Detected: ${C.bcyan(detected)}`);
    const ok = handleCancel(await confirm({ message: `Use "${detected}" as your owner name?` }));
    if (!ok) {
      detected = handleCancel(await text({
        message: "Enter your preferred name:",
        placeholder: detected,
        validate: v => v.length === 0 ? "Name is required" : undefined,
      }));
    }
  } else {
    detected = handleCancel(await text({
      message: "What's your name?",
      placeholder: "Your Name",
      validate: v => v.length === 0 ? "Name is required" : undefined,
    }));
  }

  const display = detected;
  const id = display.toLowerCase().replace(/\s+/g, "-");
  log.success(`Owner: ${C.bcyan(display)} ${C.dim(`(${id})`)}`);
  return { display, id };
}

// =============================================================================
// Step 3: Models + Notifications
// =============================================================================
async function step3_models() {
  stepHeader(3, 8, "MODELS", STEP_QUOTES.models);

  log.info(C.dim("Quaid uses two LLM tiers: high reasoning (extraction, review)"));
  log.info(C.dim("and low reasoning (reranking, classification)."));

  const provider = handleCancel(await select({
    message: "LLM provider",
    options: [
      { value: "anthropic",  label: "Anthropic (Claude)", hint: "Recommended" },
      { value: "openai",     label: "OpenAI",             hint: "Experimental" },
      { value: "openrouter", label: "OpenRouter",         hint: "Experimental — multi-provider gateway" },
      { value: "together",   label: "Together AI",        hint: "Experimental" },
      { value: "ollama",     label: "Ollama (local)",     hint: "Experimental — quality depends on model size" },
    ],
  }));

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
    log.info(`High reasoning: ${C.bcyan(highModel)}  |  Low reasoning: ${C.bcyan(lowModel)}`);
  } else if (provider === "ollama") {
    highModel = handleCancel(await text({
      message: "High reasoning model:",
      placeholder: "llama3.1:70b",
      initialValue: "llama3.1:70b",
    }));
    const defaultLow = lowModelFor(highModel);
    lowModel = handleCancel(await text({
      message: "Low reasoning model:",
      placeholder: defaultLow,
      initialValue: defaultLow,
    }));
  } else {
    highModel = handleCancel(await text({
      message: "High reasoning model:",
      placeholder: "gpt-4o",
      initialValue: "gpt-4o",
    }));
    const defaultLow = lowModelFor(highModel);
    lowModel = handleCancel(await text({
      message: "Low reasoning model:",
      placeholder: defaultLow,
      initialValue: defaultLow,
    }));
  }

  // API key — the bot passes its key to Quaid at runtime.
  // No need to check env here.
  const keyEnv = keyEnvFor(provider);

  // Notifications
  const notifLevel = handleCancel(await select({
    message: "Notification verbosity",
    initialValue: "normal",
    options: [
      { value: "quiet",   label: "Quiet",   hint: "Errors only" },
      { value: "normal",  label: "Normal",  hint: "Extraction + janitor summaries" },
      { value: "verbose", label: "Verbose", hint: "Also shows memory recall" },
      { value: "debug",   label: "Debug",   hint: "Full details on everything" },
    ],
  }));
  log.info(C.dim("Notifications are sent on whatever channel you use to talk to your bot."));

  return {
    provider,
    highModel,
    lowModel,
    apiFormat: provider === "anthropic" ? "anthropic" : "openai",
    apiKeyEnv: keyEnv,
    baseUrl: baseUrlFor(provider),
    notifLevel,
  };
}

// =============================================================================
// Step 4: Embeddings
// =============================================================================
async function step4_embeddings() {
  stepHeader(4, 8, "EMBEDDINGS", STEP_QUOTES.embeddings);

  log.info(C.dim("Embeddings power semantic search — turning text into vectors"));
  log.info(C.dim("so Quaid can find relevant memories by meaning, not just keywords."));

  // Check Ollama
  let ollamaRunning = false;
  if (process.env.QUAID_TEST_NO_OLLAMA) {
    // Test mode: simulate Ollama not installed/running
    ollamaRunning = false;
  } else {
    try { execSync("curl -sf http://localhost:11434/api/tags", { stdio: "pipe" }); ollamaRunning = true; } catch {}
  }

  if (!ollamaRunning && !process.env.QUAID_TEST_NO_OLLAMA && canRun("ollama")) {
    log.warn("Ollama is installed but not running.");
    const start = handleCancel(await confirm({ message: "Start Ollama now?" }));
    if (start) {
      const s = spinner();
      s.start("Starting Ollama...");
      try {
        execSync("brew services start ollama 2>/dev/null || (ollama serve &>/dev/null &)", { stdio: "pipe" });
        await sleep(3000);
        execSync("curl -sf http://localhost:11434/api/tags", { stdio: "pipe" });
        ollamaRunning = true;
        s.stop(C.green("Ollama started"));
      } catch {
        s.stop("Could not start Ollama");
        log.warn("You can start it manually later: ollama serve");
      }
    }
  }

  if (!ollamaRunning && (process.env.QUAID_TEST_NO_OLLAMA || !canRun("ollama"))) {
    log.warn("Ollama not found.");
    log.info(C.dim("Ollama runs embedding models locally — free, fast, and private."));
    log.info(C.dim("Without it, Quaid uses keyword search only (no semantic recall)."));
    const install = handleCancel(await confirm({ message: "Install Ollama now?" }));
    if (install) {
      const s = spinner();
      s.start("Installing Ollama...");
      try {
        if (canRun("brew")) {
          execSync("brew install ollama", { stdio: "pipe" });
          execSync("brew services start ollama 2>/dev/null || true", { stdio: "pipe" });
        } else {
          execSync("curl -fsSL https://ollama.ai/install.sh | sh", { stdio: "pipe" });
          execSync("ollama serve &>/dev/null &", { stdio: "pipe" });
        }
        await sleep(3000);
        execSync("curl -sf http://localhost:11434/api/tags", { stdio: "pipe" });
        ollamaRunning = true;
        s.stop(C.green("Ollama installed and running"));
      } catch {
        s.stop("Ollama install had issues");
        log.warn("You may need to start it manually: ollama serve");
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
    // No Ollama — keyword-only for now (cloud embeddings on roadmap)
    log.warn(C.bold("Ollama not available — semantic search requires local embeddings."));
    log.info(C.bold("Install Ollama later for vector search: https://ollama.ai"));
    log.info(C.dim("Cloud embedding support (OpenAI API) is on the roadmap."));
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
// Step 5: Systems Configuration
// =============================================================================
async function step5_systems() {
  stepHeader(5, 8, "SYSTEMS", STEP_QUOTES.systems);

  const sysInfo = {
    memory: {
      label: "Memory",
      desc: "Extract and recall facts from conversations",
      detail: "Writes to: data/memory.db. Calls LLM on compaction/reset for extraction, and on recall for reranking.",
    },
    journal: {
      label: "Journal",
      desc: "Personality evolution via snippets + reflective journal",
      detail: "Writes to: journal/*.journal.md, *.snippets.md, SOUL.md, USER.md. Nightly high-reasoning distillation merges learnings into core markdown.",
    },
    projects: {
      label: "Projects & Docs",
      desc: "Auto-update project docs when source files change",
      detail: "Writes to: projects/*/PROJECT.md, registered docs. Monitors git diffs and updates docs via LLM. Disable if another tool manages your docs.",
    },
    workspace: {
      label: "Workspace",
      desc: "Core markdown health monitoring (bloat, drift, staleness)",
      detail: "Reads+writes: SOUL.md, USER.md, MEMORY.md, etc. Monitors line counts, detects drift, suggests cleanups. Disable if you manage these files manually.",
    },
  };

  // Show what each system does
  note(
    Object.values(sysInfo).map(s =>
      `${C.bcyan(s.label)} — ${s.desc}\n${C.dim(s.detail)}`
    ).join("\n\n"),
    C.bmag("SUBSYSTEMS")
  );

  log.info(C.bold("All 4 systems are recommended. Only disable if you know there's a conflict"));
  log.info(C.bold("with another tool or workflow that manages the same files."));
  log.message("");

  const keepAll = handleCancel(await confirm({
    message: "Keep all systems enabled? (Recommended)",
    initialValue: true,
  }));

  const systems = { memory: true, journal: true, projects: true, workspace: true };

  if (!keepAll) {
    for (const [key, info] of Object.entries(sysInfo)) {
      const enabled = handleCancel(await confirm({
        message: `${C.bcyan(info.label)}: ${info.desc}`,
        initialValue: true,
      }));
      systems[key] = enabled;
      if (!enabled) log.warn(`${info.label} disabled — ${C.dim(info.detail.split(".")[0])}`);
    }
  }

  const enabled = Object.entries(systems).filter(([,v]) => v).map(([k]) => k);
  log.success(`Enabled: ${C.bcyan(enabled.join(", "))}`);

  return systems;
}

// =============================================================================
// Step 6: Janitor Schedule
// =============================================================================
async function step6_schedule(embeddings = {}) {
  stepHeader(6, 8, "JANITOR", STEP_QUOTES.janitor);

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
    initialValue: "3",
    options: [
      { value: "2",    label: "2:00 AM",  hint: "Early morning" },
      { value: "3",    label: "3:00 AM",  hint: "Recommended" },
      { value: "4",    label: "4:00 AM",  hint: "Early morning" },
      { value: "5",    label: "5:00 AM",  hint: "Before most wake up" },
      { value: "custom", label: "Custom",  hint: "Pick your own hour" },
      { value: "skip", label: "Skip",     hint: "I'll configure it myself" },
    ],
  }));

  if (hour === "skip") {
    log.warn("No schedule set.");
    log.warn("You MUST add a janitor entry to your HEARTBEAT.md manually.");
    log.warn(C.dim("See: https://github.com/steadman-labs/quaid#janitor-scheduling"));
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

  // Write janitor schedule to HEARTBEAT.md (the bot reads this on wake)
  const scheduled = installHeartbeatSchedule(scheduleHour);

  if (scheduled) {
    const ampm = scheduleHour === 0 ? "12:00 AM"
               : scheduleHour < 12 ? `${scheduleHour}:00 AM`
               : scheduleHour === 12 ? "12:00 PM"
               : `${scheduleHour - 12}:00 PM`;
    log.success(`Janitor scheduled for ${C.bcyan(ampm)} daily via HEARTBEAT.md`);
  } else {
    log.warn("Could not update HEARTBEAT.md automatically.");
    log.warn("Add this to your HEARTBEAT.md manually:");
    log.warn(C.dim(`  Janitor check: if ${scheduleHour}:00, run scripts/secure-janitor.sh --apply --task all`));
  }

  // Persistent warning — DO NOT REMOVE. The user must understand this.
  const scheduleLines = [
    C.yellow("The janitor runs via your bot's HEARTBEAT system."),
    C.yellow("This keeps your API key secure — never stored in cron or launchd."),
    "",
    C.bold("Your bot reads HEARTBEAT.md on each wake, checks the time,"),
    C.bold("and runs the janitor with secure key injection."),
  ];

  // Cloud embeddings key passthrough — uncomment when cloud embedding backend ships
  // if (embeddings.embedModel === "text-embedding-3-small") {
  //   scheduleLines.push("");
  //   scheduleLines.push(C.yellow("You chose cloud embeddings (OpenAI API)."));
  //   scheduleLines.push(C.yellow("OPENAI_API_KEY must also be passed through the heartbeat"));
  //   scheduleLines.push(C.yellow("to your janitor calls (embedding backfill + re-embed)."));
  // }

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

  return { hour: scheduleHour, scheduled };
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
  const minStart = hour === 0 ? 23 : hour - 1;
  const minEnd = hour === 23 ? 0 : hour + 1;
  const padH = (h) => String(h).padStart(2, "0");

  const janitorBlock = [
    "",
    `## Quaid Janitor (${padH(hour)}:00 daily)`,
    "",
    `**Schedule:** Check if current time is between ${padH(hour)}:00-${padH(minEnd)}:00 and janitor hasn't run today.`,
    "",
    "**IMPORTANT:** The janitor requires your LLM API key. It must be run through",
    "the bot's heartbeat — NOT from a standalone cron job or launchd agent.",
    "The bot passes its API key to the janitor subprocess at runtime.",
    "",
    "**To run:** `scripts/secure-janitor.sh --apply --task all`",
    "",
    "**Logic:**",
    `- If time is between ${padH(hour)}:00 and ${padH(minEnd)}:00 AND janitor hasn't run today:`,
    "  - Run: `./scripts/secure-janitor.sh --apply --task all`",
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
  ].join("\n");

  try {
    let content = "";
    if (fs.existsSync(heartbeatPath)) {
      content = fs.readFileSync(heartbeatPath, "utf8");
      // Remove any existing Quaid Janitor section
      content = content.replace(/\n## Quaid Janitor[\s\S]*?(?=\n## |\n*$)/g, "");
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

// =============================================================================
// Step 7: Install & Migrate
// =============================================================================
async function step7_install(pluginSrc, owner, models, embeddings, systems) {
  stepHeader(7, 8, "INSTALL", STEP_QUOTES.install);

  const s = spinner();

  // Create directories
  s.start("Creating directories...");
  for (const dir of [CONFIG_DIR, DATA_DIR, JOURNAL_DIR, LOGS_DIR, PROJECTS_DIR, path.join(JOURNAL_DIR, "archive")]) {
    fs.mkdirSync(dir, { recursive: true });
  }
  s.stop(C.green("Directories created"));

  // Copy plugin source
  if (!fs.existsSync(PLUGIN_DIR) || fs.readdirSync(PLUGIN_DIR).length === 0) {
    s.start("Installing plugin source...");
    fs.mkdirSync(PLUGIN_DIR, { recursive: true });
    copyDirSync(pluginSrc, PLUGIN_DIR);
    s.stop(C.green("Plugin installed"));
  } else {
    log.info("Plugin source already in place");
  }

  // Initialize database
  s.start("Initializing database...");
  const dbPath = path.join(DATA_DIR, "memory.db");
  const schemaPath = path.join(PLUGIN_DIR, "schema.sql");
  if (fs.existsSync(schemaPath)) {
    const initScript = `
import sqlite3
conn = sqlite3.connect('${dbPath}')
with open('${schemaPath}') as f:
    conn.executescript(f.read())
conn.close()
`;
    spawnSync("python3", ["-c", initScript], { stdio: "pipe" });
    try { fs.chmodSync(dbPath, 0o600); } catch {}
  }
  s.stop(C.green("Database initialized"));

  // Write config
  s.start("Writing configuration...");
  writeConfig(owner, models, embeddings, systems);
  s.stop(C.green("Config written"));

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
    spawnSync("git", ["commit", "-m", "Initial Quaid workspace"], { cwd: WORKSPACE, stdio: "pipe" });
    s.stop(C.green("Git repository initialized"));
  } else {
    log.info("Git repository already exists");
  }

  // Create owner Person node
  s.start("Creating owner node...");
  const storeScript = `
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store
try:
    store('${owner.display}', owner_id='${owner.id}', category='person', source='installer')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
`;
  spawnSync("python3", ["-c", storeScript], { cwd: PLUGIN_DIR, stdio: "pipe" });
  s.stop(C.green(`Owner node: ${owner.display}`));

  // Migration
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
      message: "Import facts from existing files into memory? (uses LLM, ~$0.15-0.50)",
      initialValue: false,
    }));
    if (doMigrate) {
      s.start("Extracting facts from workspace files...");
      const useMock = process.env.QUAID_TEST_MOCK_MIGRATION === "1";
      const migrateScript = useMock ? `
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store
files = ${JSON.stringify(mdFiles)}
total = 0
for fname in files:
    fpath = os.path.join('${WORKSPACE}', fname)
    with open(fpath) as f:
        lines = f.read().strip().split('\\n')
    for line in lines:
        line = line.strip().lstrip('- ')
        if line and not line.startswith('#') and len(line) > 15:
            cat = 'preference' if any(w in line.lower() for w in ['prefer', 'like', 'enjoy', 'favorite']) else 'fact'
            store(line, owner_id='${owner.id}', category=cat, source='migration')
            total += 1
print(total)
` : `
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store
from llm_clients import call_high_reasoning, parse_json_response
files = ${JSON.stringify(mdFiles)}
total = 0
for fname in files:
    fpath = os.path.join('${WORKSPACE}', fname)
    with open(fpath) as f:
        content = f.read().strip()
    if len(content) < 50:
        continue
    prompt = f"""Extract factual information from this document. Return a JSON array of objects:
[{{"fact": "...", "category": "fact|preference|belief|experience"}}]
Only extract clear, specific facts. Skip meta-information and formatting.
Document ({fname}):\\n{content}"""
    response, _ = call_high_reasoning(prompt, max_tokens=4000)
    if response:
        parsed = parse_json_response(response)
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and 'fact' in item:
                    store(item['fact'], owner_id='${owner.id}', category=item.get('category', 'fact'), source='migration')
                    total += 1
print(total)
`;
      const result = spawnSync("python3", ["-c", migrateScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      const factCount = (result.stdout || "").trim();
      s.stop(C.green(`Migration complete: ${factCount} facts extracted`));
      log.info(C.bold("These facts will be reviewed and deduplicated by the nightly janitor."));
      log.message("");
      await waitForKey();
    }
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
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from docs_registry import DocsRegistry
reg = DocsRegistry()
names = ${projNames}
total_docs = 0
for name in names:
    proj_dir = os.path.join('${PROJECTS_DIR}', name)
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

    // Install Quaid project reference docs (TOOLS.md, AGENTS.md, onboarding)
    const quaidProjDir = path.join(PROJECTS_DIR, "quaid");
    fs.mkdirSync(quaidProjDir, { recursive: true });
    const quaidProjSrc = path.join(__dirname, "projects", "quaid");
    for (const f of ["TOOLS.md", "AGENTS.md", "ARCHITECTURE.md", "project_onboarding.md"]) {
      const src = path.join(quaidProjSrc, f);
      const dst = path.join(quaidProjDir, f);
      if (fs.existsSync(src) && !fs.existsSync(dst)) {
        fs.copyFileSync(src, dst);
      }
    }
    // Create PROJECT.md for Quaid itself
    if (!fs.existsSync(path.join(quaidProjDir, "PROJECT.md"))) {
      fs.writeFileSync(path.join(quaidProjDir, "PROJECT.md"), [
        "# Quaid Memory System",
        "",
        "Persistent long-term memory plugin. Stores facts, relationships, and preferences",
        "in a local SQLite graph database. Retrieved automatically via hybrid search.",
        "",
        "## Key Files",
        "- `TOOLS.md` — CLI commands and agent tools reference",
        "- `AGENTS.md` — Instructions for how the agent should use memory",
        "- `ARCHITECTURE.md` — Full system architecture and design",
        "- `project_onboarding.md` — Guide for discovering and registering projects",
        "",
        "## Systems",
        "- **Memory** — Fact extraction, graph storage, hybrid recall",
        "- **Journal** — Slow-path learning, personality evolution",
        "- **Projects** — Documentation tracking, staleness detection, RAG search",
        "- **Workspace** — Core markdown monitoring, nightly maintenance",
        "",
      ].join("\n"));
    }
    // Register Quaid as a project
    const regQuaidScript = `
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from docs_registry import DocsRegistry
reg = DocsRegistry()
try:
    reg.create_project('quaid', label='Quaid Memory System', description='Memory plugin reference docs and agent instructions.')
except ValueError:
    pass  # already exists
found = reg.auto_discover('quaid')
print(len(found))
`;
    const regQuaidResult = spawnSync("python3", ["-c", regQuaidScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    const quaidDocCount = (regQuaidResult.stdout || "").trim();
    log.info(`Quaid project installed (${quaidDocCount} docs registered)`);
  }

  log.success("Installation complete!");
  log.message("");
  await waitForKey("Press any key to run validation...");
}

// =============================================================================
// Step 8: Validation
// =============================================================================
async function step8_validate(owner, models, embeddings, systems) {
  stepHeader(8, 8, "VALIDATION", STEP_QUOTES.validate);

  const s = spinner();
  s.start("Running health checks...");

  const checks = [];

  // Database
  const dbPath = path.join(DATA_DIR, "memory.db");
  if (fs.existsSync(dbPath)) {
    const tables = shell(`python3 -c "import sqlite3; c=sqlite3.connect('${dbPath}'); print(c.execute(\\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\\").fetchone()[0]); c.close()"`);
    checks.push(`${C.green("■")} Database     ${C.dim("—")} ${tables} tables`);
  } else {
    checks.push(`${C.red("■")} Database     ${C.dim("—")} MISSING`);
  }

  // Embeddings
  let ollamaOk = false;
  try { execSync("curl -sf http://localhost:11434/api/tags", { stdio: "pipe" }); ollamaOk = true; } catch {}
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
  s.start("Smoke test (store + recall)...");
  const smokeScript = `
import os, sys
os.environ['CLAWDBOT_WORKSPACE'] = '${WORKSPACE}'
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from memory_graph import store, recall
node_id = store('Quaid installer smoke test fact', owner_id='${owner.id}', category='fact', source='installer-test')
results = recall('installer smoke test', owner_id='${owner.id}', limit=1)
if results:
    print('OK')
else:
    print('PARTIAL')
`;
  const smoke = spawnSync("python3", ["-c", smokeScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
  const smokeResult = (smoke.stdout || "").trim();
  if (smokeResult === "OK") {
    s.stop(C.green("Smoke test passed — store and recall working"));
  } else {
    s.stop(C.yellow("Smoke test partial — store OK, recall needs embeddings"));
  }

  const nextSteps = [
    `${C.bcyan("→")} Facts are extracted automatically on context compaction and new sessions`,
    `${C.bcyan("→")} The nightly janitor reviews, deduplicates, and maintains memories`,
    `${C.bcyan("→")} Run the janitor now to discover and organize your projects:`,
    `   ${C.bcyan("cd plugins/quaid && python3 janitor.py --task workspace --apply")}`,
    `   The janitor will scan TOOLS.md and AGENTS.md for project specs`,
    `   and flag them for review. Your agent will walk you through the`,
    `   findings on your next conversation and help organize them.`,
    `${C.bcyan("→")} Run ${C.bcyan("quaid doctor")} anytime to check system health`,
    `${C.bcyan("→")} Run ${C.bcyan("quaid stats")} to see your memory database grow`,
    "",
    C.dim(`Docs: ${PROJECT_URL}`),
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
  const map = {
    openrouter: "https://openrouter.ai/api/v1",
    together: "https://api.together.xyz/v1",
    ollama: "http://localhost:11434/v1",
  };
  return map[provider] || null;
}

function findGateway() {
  for (const cli of ["clawdbot", "openclaw"]) {
    const bin = shell(`which ${cli}`);
    if (!bin) continue;
    try {
      const resolved = fs.realpathSync(bin);
      const candidate = path.join(path.dirname(resolved), "..");
      if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
    } catch {}
  }
  for (const candidate of [
    "/opt/homebrew/lib/node_modules/openclaw",
    "/opt/homebrew/lib/node_modules/clawdbot",
    "/usr/local/lib/node_modules/openclaw",
  ]) {
    if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
  }
  return null;
}

function gatewayHasHooks(gwDir) {
  for (const sub of ["dist", "src"]) {
    const dir = path.join(gwDir, sub);
    if (!fs.existsSync(dir)) continue;
    const out = shell(`grep -rl "runBeforeCompaction\\|before_compaction" "${dir}" 2>/dev/null | head -1`);
    const out2 = shell(`grep -rl "extraBootstrapFiles" "${dir}" 2>/dev/null | head -1`);
    if (out && out2) return true;
  }
  return false;
}

async function tryBrewInstall(pkg, label) {
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

function writeConfig(owner, models, embeddings, systems) {
  const config = {
    systems,
    models: {
      provider: models.apiFormat,
      apiKeyEnv: models.apiKeyEnv,
      baseUrl: models.baseUrl,
      lowReasoning: models.lowModel,
      highReasoning: models.highModel,
      lowReasoningContext: 200000,
      highReasoningContext: 200000,
      lowReasoningMaxOutput: 8192,
      highReasoningMaxOutput: 16384,
      batchBudgetPercent: 0.5,
    },
    capture: {
      enabled: true,
      strictness: "high",
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
      taskTimeoutMinutes: 60,
      opusReview: { enabled: true, batchSize: 50, maxTokens: 4000 },
      dedup: {
        similarityThreshold: 0.85,
        highSimilarityThreshold: 0.95,
        autoRejectThreshold: 0.98,
        grayZoneLow: 0.88,
        haikuVerifyEnabled: true,
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
      notifyOnRecall: true,
      reranker: { enabled: true, provider: "anthropic", topK: 20 },
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
      janitor: { verbosity: null, channel: "last_used" },
      extraction: { verbosity: null, channel: "last_used" },
      retrieval: { verbosity: null, channel: "last_used" },
      fullText: true,
      showProcessingStart: true,
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
      url: "http://localhost:11434",
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

// =============================================================================
// Main
// =============================================================================
async function main() {
  try {
    const pluginSrc = await step1_preflight();
    const owner = await step2_owner();
    const models = await step3_models();
    const embeddings = await step4_embeddings();
    const systems = await step5_systems();
    const schedule = await step6_schedule(embeddings);
    await step7_install(pluginSrc, owner, models, embeddings, systems);
    await step8_validate(owner, models, embeddings, systems);

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
