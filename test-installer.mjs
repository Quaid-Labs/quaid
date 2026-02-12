#!/usr/bin/env node
// =============================================================================
// Quaid Installer — Automated Test Runner
// =============================================================================
// Tests setup-quaid.mjs across fresh/dirty environments and option combos.
// Usage: node test-installer.mjs [--filter <pattern>] [--keep-envs]
//
// Each scenario creates an isolated workspace, runs the installer in test mode,
// and verifies the output (files created, config correctness, DB state).
// =============================================================================

import { execSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const INSTALLER = path.join(__dirname, "setup-quaid.mjs");
const PLUGIN_SOURCE = path.resolve(__dirname, "../../plugins/quaid");

let keepEnvs = false;
let filterPattern = null;
for (let i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === "--keep-envs") keepEnvs = true;
  if (process.argv[i] === "--filter" && process.argv[i + 1]) filterPattern = process.argv[++i];
}

// =============================================================================
// Test infrastructure
// =============================================================================
const results = [];

function assert(condition, msg) {
  if (!condition) throw new Error(`ASSERTION FAILED: ${msg}`);
}

function fileExists(dir, ...parts) {
  return fs.existsSync(path.join(dir, ...parts));
}

function readJSON(dir, ...parts) {
  return JSON.parse(fs.readFileSync(path.join(dir, ...parts), "utf8"));
}

function readFile(dir, ...parts) {
  return fs.readFileSync(path.join(dir, ...parts), "utf8");
}

function createTempWorkspace(name) {
  const dir = path.join(os.tmpdir(), `quaid-test-${name}-${Date.now()}`);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function setupDirtyEnvironment(workspace) {
  // Simulate 2 weeks of usage: existing config, DB, markdown files, journal entries
  fs.mkdirSync(path.join(workspace, "config"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "data"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "journal", "archive"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "logs"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "projects", "my-app"), { recursive: true });
  fs.mkdirSync(path.join(workspace, "plugins", "quaid"), { recursive: true });

  // Copy real plugin source (needed for Python imports)
  copyDirSync(PLUGIN_SOURCE, path.join(workspace, "plugins", "quaid"));

  // Existing workspace markdown files with content
  fs.writeFileSync(path.join(workspace, "SOUL.md"), `# SOUL

## Personality
I am a helpful assistant. I enjoy solving problems and learning new things.
I prefer concise communication and technical precision.

## Communication Style
- Direct and clear
- Use examples when explaining
- Avoid unnecessary filler
`);

  fs.writeFileSync(path.join(workspace, "USER.md"), `# USER

## About
Name: Test User
Location: San Francisco, CA
Occupation: Software engineer at TechCorp

## Preferences
- Prefers dark mode
- Uses vim keybindings
- Drinks coffee, not tea
- Works on macOS
`);

  fs.writeFileSync(path.join(workspace, "MEMORY.md"), `# MEMORY

## Key Facts
- Working on a React app with TypeScript
- Uses PostgreSQL for the database
- Has a cat named Pixel
- Birthday is March 15
`);

  fs.writeFileSync(path.join(workspace, "TOOLS.md"), `# TOOLS

## APIs
- GitHub: personal access token in env
- Slack: webhook for notifications
`);

  fs.writeFileSync(path.join(workspace, "AGENTS.md"), `# AGENTS

## Configuration
Main agent handles general tasks.
`);

  fs.writeFileSync(path.join(workspace, "HEARTBEAT.md"), `# HEARTBEAT.md

# Periodic checks

## Existing Task
**Schedule:** Every 6 hours
**Action:** Check system health
`);

  // Existing config
  fs.writeFileSync(path.join(workspace, "config", "memory.json"), JSON.stringify({
    systems: { memory: true, journal: true, projects: true, workspace: true },
    models: { provider: "anthropic", highReasoning: "claude-opus-4-6", lowReasoning: "claude-haiku-4-5" },
    ollama: { url: "http://localhost:11434", embeddingModel: "nomic-embed-text", embeddingDim: 768 },
  }, null, 2));

  // Existing database (create tables via schema.sql)
  const dbPath = path.join(workspace, "data", "memory.db");
  const schemaPath = path.join(workspace, "plugins", "quaid", "schema.sql");
  if (fs.existsSync(schemaPath)) {
    spawnSync("python3", ["-c", `
import sqlite3
conn = sqlite3.connect('${dbPath}')
with open('${schemaPath}') as f:
    conn.executescript(f.read())
# Add some existing data
conn.execute("INSERT INTO nodes (id, name, type, owner_id, status, confidence, source) VALUES ('test-1', 'Test User likes coffee', 'Preference', 'test-user', 'active', 0.9, 'test')")
conn.execute("INSERT INTO nodes (id, name, type, owner_id, status, confidence, source) VALUES ('test-2', 'Test User is a software engineer', 'Fact', 'test-user', 'active', 0.95, 'test')")
conn.commit()
conn.close()
    `], { stdio: "pipe" });
  }

  // Journal entries
  fs.writeFileSync(path.join(workspace, "journal", "SOUL.journal.md"), `# SOUL Journal

## 2026-02-10 — Compaction
Learned that the user appreciates directness. Updated communication style.

## 2026-02-08 — Compaction
First interaction. User seems technical and prefers concise answers.
`);

  // Project with PROJECT.md
  fs.writeFileSync(path.join(workspace, "projects", "my-app", "PROJECT.md"), `# My App

A React application with TypeScript.

## Stack
- React 18
- TypeScript 5
- PostgreSQL
`);
}

function copyDirSync(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (entry.name === "__pycache__" || entry.name === ".pytest_cache" ||
        entry.name === "tests" || entry.name === "node_modules" ||
        entry.name === ".git") continue;
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    // Skip symlinks (node_modules/.bin etc)
    try {
      const stat = fs.lstatSync(srcPath);
      if (stat.isSymbolicLink()) continue;
      if (stat.isDirectory()) {
        copyDirSync(srcPath, destPath);
      } else {
        fs.copyFileSync(srcPath, destPath);
      }
    } catch { continue; }
  }
}

// Ensure plugin source exists in workspace (simulates release package)
function ensurePluginSource(workspace) {
  const pluginDest = path.join(workspace, "plugins", "quaid");
  if (!fs.existsSync(pluginDest) || fs.readdirSync(pluginDest).length === 0) {
    copyDirSync(PLUGIN_SOURCE, pluginDest);
  }
}

async function runScenario(name, description, setupFn, answers, verifyFn, extraEnv = {}) {
  if (filterPattern && !name.includes(filterPattern)) {
    results.push({ name, status: "SKIP", description });
    return;
  }

  const workspace = createTempWorkspace(name);
  const answersPath = path.join(workspace, "_test_answers.json");
  const resultsPath = path.join(workspace, "_test_results.json");

  try {
    // Setup environment
    if (setupFn) setupFn(workspace);
    // Always ensure plugin source is present (simulates release package)
    ensurePluginSource(workspace);

    // Write answers file
    fs.writeFileSync(answersPath, JSON.stringify({ answers }));

    // Run installer
    const env = {
      ...process.env,
      CLAWDBOT_WORKSPACE: workspace,
      QUAID_TEST_ANSWERS: answersPath,
      QUAID_TEST_RESULTS: resultsPath,
      QUAID_QUIET: "1",
      ...extraEnv,
    };

    const result = spawnSync("node", [INSTALLER], {
      env,
      cwd: workspace,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 60000,
    });

    const stdout = result.stdout || "";
    const stderr = result.stderr || "";

    // Check if results file was written
    let testResults = null;
    if (fs.existsSync(resultsPath)) {
      testResults = JSON.parse(fs.readFileSync(resultsPath, "utf8"));
    }

    if (result.status !== 0 && !testResults?.success) {
      // Check if it's an expected failure (e.g., preflight should fail on some scenarios)
      try {
        verifyFn(workspace, testResults, stdout, stderr, result.status);
        results.push({ name, status: "PASS", description });
      } catch (err) {
        results.push({
          name, status: "FAIL", description,
          error: err.message,
          stdout: stdout.slice(-500),
          stderr: stderr.slice(-500),
          exitCode: result.status,
        });
      }
      return;
    }

    // Run verification
    verifyFn(workspace, testResults, stdout, stderr, result.status);
    results.push({ name, status: "PASS", description });

  } catch (err) {
    results.push({
      name, status: "FAIL", description,
      error: err.message,
    });
  } finally {
    if (!keepEnvs) {
      try { fs.rmSync(workspace, { recursive: true, force: true }); } catch {}
    } else {
      console.log(`  [kept] ${workspace}`);
    }
  }
}

// =============================================================================
// Answer sequences for each scenario
// =============================================================================
// The answers array must match the exact order of interactive prompts.
// Step 1 preflight: may prompt "Create a backup now?" (confirm) on dirty
// Step 2 identity: confirm detected name OR text entry
// Step 3 models: select provider, (optional text for models), select notif level
// Step 4 embeddings: (optional ollama prompts), select model
// Step 5 systems: confirm keep all OR individual confirms
// Step 6 janitor: select hour, confirm understanding
// Step 7 install: (optional confirm migration)
// Step 8 validation: no prompts

// Fresh install, all defaults (Anthropic, keep all systems, 3AM janitor)
// Prompt order: step2(confirm name), step3(provider, notif), step4(embed model),
//               step5(keep all), step6(hour, confirm), step7(no migration on fresh)
const ANSWERS_FRESH_DEFAULTS = [
  true,              // step2: confirm detected name
  "anthropic",       // step3: provider
  "normal",          // step3: notification level
  "none",            // step4: embedding model (none = keyword only)
  true,              // step5: keep all systems
  "3",               // step6: janitor hour
  true,              // step6: confirm understanding
];

// Fresh install, OpenAI provider, custom models
// Non-anthropic providers add: waitForKey (skipped in test), then text prompts for models
const ANSWERS_FRESH_OPENAI = [
  true,              // step2: confirm name
  "openai",          // step3: provider (non-anthropic → model text prompts)
  "gpt-4o",          // step3: high reasoning model
  "gpt-4o-mini",     // step3: low reasoning model
  "verbose",         // step3: notification level
  "none",            // step4: embedding model
  true,              // step5: keep all
  "4",               // step6: hour
  true,              // step6: confirm
];

// Fresh install, disable some systems
const ANSWERS_FRESH_PARTIAL_SYSTEMS = [
  true,              // step2: confirm name
  "anthropic",       // step3: provider
  "quiet",           // step3: notif level
  "none",            // step4: embed model
  false,             // step5: don't keep all
  true,              // step5: Memory? yes
  false,             // step5: Journal? no
  false,             // step5: Projects? no
  true,              // step5: Workspace? yes
  "skip",            // step6: skip janitor
];

// Fresh install, custom name entry
const ANSWERS_FRESH_CUSTOM_NAME = [
  false,             // step2: decline detected name
  "Douglas Quaid",   // step2: enter custom name
  "anthropic",       // step3: provider
  "normal",          // step3: notif level
  "none",            // step4: embed model
  true,              // step5: keep all
  "custom",          // step6: custom hour
  "5",               // step6: enter hour
  true,              // step6: confirm
];

// Dirty install with backup, all defaults, migration declined
const ANSWERS_DIRTY_BACKUP_NO_MIGRATE = [
  // step1: backup? yes
  true,
  // step2: confirm detected
  true,
  // step3: anthropic, normal
  "anthropic", "normal",
  // step4: none
  "none",
  // step5: keep all
  true,
  // step6: 3AM, confirm
  "3", true,
  // step7: migrate? no
  false,
];

// Dirty install, no backup, with migration
const ANSWERS_DIRTY_NO_BACKUP_WITH_MIGRATE = [
  // step1: backup? no
  false,
  // step2: confirm detected
  true,
  // step3: anthropic, debug
  "anthropic", "debug",
  // step4: none
  "none",
  // step5: keep all
  true,
  // step6: 2AM, confirm
  "2", true,
  // step7: migrate? yes
  true,
];

// Dirty install, change provider to OpenRouter
const ANSWERS_DIRTY_OPENROUTER = [
  // step1: backup? yes
  true,
  // step2: decline, enter new name
  false, "Melina",
  // step3: openrouter, high model, low model, normal notif
  "openrouter", "anthropic/claude-opus-4-6", "anthropic/claude-haiku-4-5", "normal",
  // step4: none
  "none",
  // step5: don't keep all; memory=yes, journal=yes, projects=no, workspace=no
  false, true, true, false, false,
  // step6: 3AM, confirm
  "3", true,
  // step7: migrate? no
  false,
];

// Dirty install, Ollama local provider
const ANSWERS_DIRTY_OLLAMA = [
  // step1: backup? no
  false,
  // step2: confirm detected
  true,
  // step3: ollama, high model, low model, normal
  "ollama", "llama3.1:70b", "llama3.1:8b", "normal",
  // step4: none (since we select "none" for embeddings)
  "none",
  // step5: keep all
  true,
  // step6: skip
  "skip",
  // step7: migrate? no
  false,
];

// =============================================================================
// Verification helpers
// =============================================================================
function verifyFreshInstall(workspace, testResults, stdout, stderr, exitCode) {
  assert(exitCode === 0, `Expected exit 0, got ${exitCode}. stderr: ${stderr.slice(-200)}`);
  assert(testResults?.success, `Installer reported failure: ${testResults?.error}`);

  // Core directories
  assert(fileExists(workspace, "config"), "config/ directory missing");
  assert(fileExists(workspace, "data"), "data/ directory missing");
  assert(fileExists(workspace, "journal"), "journal/ directory missing");
  assert(fileExists(workspace, "journal", "archive"), "journal/archive/ directory missing");
  assert(fileExists(workspace, "logs"), "logs/ directory missing");
  assert(fileExists(workspace, "projects"), "projects/ directory missing");

  // Config
  assert(fileExists(workspace, "config", "memory.json"), "config/memory.json missing");
  const config = readJSON(workspace, "config", "memory.json");
  assert(config.systems, "config missing systems section");
  assert(config.models, "config missing models section");
  assert(config.ollama, "config missing ollama section");
  assert(config.users, "config missing users section");

  // Database
  assert(fileExists(workspace, "data", "memory.db"), "data/memory.db missing");

  // Workspace markdown files
  assert(fileExists(workspace, "SOUL.md"), "SOUL.md missing");
  assert(fileExists(workspace, "USER.md"), "USER.md missing");
  assert(fileExists(workspace, "MEMORY.md"), "MEMORY.md missing");

  // HEARTBEAT.md (if janitor was scheduled)
  // Plugin source
  assert(fileExists(workspace, "plugins", "quaid", "memory_graph.py"), "plugin memory_graph.py missing");
  assert(fileExists(workspace, "plugins", "quaid", "schema.sql"), "plugin schema.sql missing");
}

function verifyConfig(workspace, expected) {
  const config = readJSON(workspace, "config", "memory.json");

  if (expected.provider) {
    assert(config.models.provider === expected.provider,
      `Expected provider ${expected.provider}, got ${config.models.provider}`);
  }
  if (expected.highModel) {
    assert(config.models.highReasoning === expected.highModel,
      `Expected highModel ${expected.highModel}, got ${config.models.highReasoning}`);
  }
  if (expected.lowModel) {
    assert(config.models.lowReasoning === expected.lowModel,
      `Expected lowModel ${expected.lowModel}, got ${config.models.lowReasoning}`);
  }
  if (expected.embedModel) {
    assert(config.ollama.embeddingModel === expected.embedModel,
      `Expected embedModel ${expected.embedModel}, got ${config.ollama.embeddingModel}`);
  }
  if (expected.systems) {
    for (const [key, val] of Object.entries(expected.systems)) {
      assert(config.systems[key] === val,
        `Expected systems.${key}=${val}, got ${config.systems[key]}`);
    }
  }
  if (expected.ownerId) {
    assert(config.users.defaultOwner === expected.ownerId,
      `Expected owner ${expected.ownerId}, got ${config.users.defaultOwner}`);
  }
  if (expected.notifLevel) {
    assert(config.notifications.level === expected.notifLevel,
      `Expected notif level ${expected.notifLevel}, got ${config.notifications.level}`);
  }
  if (expected.baseUrl !== undefined) {
    assert(config.models.baseUrl === expected.baseUrl,
      `Expected baseUrl ${expected.baseUrl}, got ${config.models.baseUrl}`);
  }
}

// =============================================================================
// Scenarios
// =============================================================================
async function runAllScenarios() {
  console.log("\n  Quaid Installer Test Runner\n  ===========================\n");

  // --- FRESH INSTALLS ---

  await runScenario(
    "fresh-defaults",
    "Fresh install, Anthropic, all defaults",
    null, // no setup (fresh)
    ANSWERS_FRESH_DEFAULTS,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        provider: "anthropic",
        highModel: "claude-opus-4-6",
        lowModel: "claude-haiku-4-5",
        embedModel: "none",
        systems: { memory: true, journal: true, projects: true, workspace: true },
        notifLevel: "normal",
      });
      // Janitor scheduled
      assert(fileExists(ws, "HEARTBEAT.md"), "HEARTBEAT.md missing");
      const hb = readFile(ws, "HEARTBEAT.md");
      assert(hb.includes("Quaid Janitor"), "HEARTBEAT.md missing janitor section");
      assert(hb.includes("03:00"), "HEARTBEAT.md missing 3AM schedule");
      // Journal files created
      assert(fileExists(ws, "journal", "SOUL.journal.md"), "SOUL.journal.md missing");
      assert(fileExists(ws, "journal", "USER.journal.md"), "USER.journal.md missing");
      assert(fileExists(ws, "journal", "MEMORY.journal.md"), "MEMORY.journal.md missing");
    }
  );

  await runScenario(
    "fresh-openai",
    "Fresh install, OpenAI provider, custom models, verbose",
    null,
    ANSWERS_FRESH_OPENAI,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        provider: "openai",
        highModel: "gpt-4o",
        lowModel: "gpt-4o-mini",
        notifLevel: "verbose",
      });
    }
  );

  await runScenario(
    "fresh-partial-systems",
    "Fresh install, disable journal + projects, skip janitor",
    null,
    ANSWERS_FRESH_PARTIAL_SYSTEMS,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        systems: { memory: true, journal: false, projects: false, workspace: true },
        notifLevel: "quiet",
      });
      // No journal files when journal disabled
      assert(!fileExists(ws, "journal", "SOUL.journal.md"),
        "SOUL.journal.md should NOT exist when journal disabled");
      // No HEARTBEAT entry when janitor skipped
      if (fileExists(ws, "HEARTBEAT.md")) {
        const hb = readFile(ws, "HEARTBEAT.md");
        assert(!hb.includes("Quaid Janitor"), "HEARTBEAT.md should NOT have janitor when skipped");
      }
    }
  );

  await runScenario(
    "fresh-custom-name",
    "Fresh install, custom name entry, custom janitor hour",
    null,
    ANSWERS_FRESH_CUSTOM_NAME,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, { ownerId: "douglas-quaid" });
      assert(res.owner.display === "Douglas Quaid", `Expected display name "Douglas Quaid", got "${res.owner.display}"`);
      assert(res.owner.id === "douglas-quaid", `Expected id "douglas-quaid", got "${res.owner.id}"`);
      // Custom 5AM schedule
      const hb = readFile(ws, "HEARTBEAT.md");
      assert(hb.includes("05:00"), "HEARTBEAT.md should have 5AM schedule");
    }
  );

  // --- DIRTY INSTALLS ---

  await runScenario(
    "dirty-backup-no-migrate",
    "Dirty install, backup yes, migration declined",
    setupDirtyEnvironment,
    ANSWERS_DIRTY_BACKUP_NO_MIGRATE,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      // Backup directory created
      const backupDirs = fs.readdirSync(ws).filter(f => f.startsWith(".quaid-backup-"));
      assert(backupDirs.length > 0, "No backup directory found");
      const backupDir = path.join(ws, backupDirs[0]);
      assert(fileExists(backupDir, "SOUL.md"), "Backup missing SOUL.md");
      assert(fileExists(backupDir, "USER.md"), "Backup missing USER.md");
      assert(fileExists(backupDir, "memory.json"), "Backup missing memory.json");
      assert(fileExists(backupDir, "memory.db"), "Backup missing memory.db");
      // Original files still intact
      const soul = readFile(ws, "SOUL.md");
      assert(soul.includes("Personality"), "SOUL.md content was overwritten");
      // Config was overwritten with new values
      verifyConfig(ws, { provider: "anthropic" });
    }
  );

  await runScenario(
    "dirty-no-backup-with-migrate",
    "Dirty install, no backup, migration yes (skips LLM call in test)",
    setupDirtyEnvironment,
    ANSWERS_DIRTY_NO_BACKUP_WITH_MIGRATE,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      // No backup directory
      const backupDirs = fs.readdirSync(ws).filter(f => f.startsWith(".quaid-backup-"));
      assert(backupDirs.length === 0, "Backup directory should not exist");
      verifyConfig(ws, { notifLevel: "debug" });
      // HEARTBEAT.md should have janitor AND existing content
      const hb = readFile(ws, "HEARTBEAT.md");
      assert(hb.includes("Quaid Janitor"), "HEARTBEAT.md missing janitor");
      assert(hb.includes("Existing Task"), "HEARTBEAT.md lost existing content");
      assert(hb.includes("02:00"), "HEARTBEAT.md should have 2AM schedule");
    }
  );

  await runScenario(
    "dirty-openrouter",
    "Dirty install, OpenRouter, custom name, partial systems",
    setupDirtyEnvironment,
    ANSWERS_DIRTY_OPENROUTER,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        provider: "openai", // openrouter uses openai format
        highModel: "anthropic/claude-opus-4-6",
        lowModel: "anthropic/claude-haiku-4-5",
        baseUrl: "https://openrouter.ai/api/v1",
        systems: { memory: true, journal: true, projects: false, workspace: false },
      });
      assert(res.owner.display === "Melina", `Expected "Melina", got "${res.owner.display}"`);
    }
  );

  await runScenario(
    "dirty-ollama-provider",
    "Dirty install, Ollama as LLM provider, skip janitor",
    setupDirtyEnvironment,
    ANSWERS_DIRTY_OLLAMA,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        provider: "openai", // ollama uses openai format
        highModel: "llama3.1:70b",
        lowModel: "llama3.1:8b",
        baseUrl: "http://localhost:11434/v1",
      });
    }
  );

  await runScenario(
    "dirty-existing-projects",
    "Dirty install with existing project directories",
    (ws) => {
      setupDirtyEnvironment(ws);
      // Add a second project
      fs.mkdirSync(path.join(ws, "projects", "api-server"), { recursive: true });
      fs.writeFileSync(path.join(ws, "projects", "api-server", "PROJECT.md"),
        "# API Server\nA REST API built with Express.\n");
    },
    ANSWERS_DIRTY_BACKUP_NO_MIGRATE,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      // Both projects should still exist
      assert(fileExists(ws, "projects", "my-app", "PROJECT.md"), "my-app PROJECT.md missing");
      assert(fileExists(ws, "projects", "api-server", "PROJECT.md"), "api-server PROJECT.md missing");
    }
  );

  await runScenario(
    "dirty-heartbeat-idempotent",
    "Dirty install twice — HEARTBEAT.md should not duplicate janitor section",
    (ws) => {
      setupDirtyEnvironment(ws);
      // Simulate a previous install by adding janitor to HEARTBEAT
      const hbPath = path.join(ws, "HEARTBEAT.md");
      let hb = fs.readFileSync(hbPath, "utf8");
      hb += "\n## Quaid Janitor (03:00 daily)\n\n**Schedule:** Old schedule\n";
      fs.writeFileSync(hbPath, hb);
    },
    ANSWERS_DIRTY_BACKUP_NO_MIGRATE,
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      const hb = readFile(ws, "HEARTBEAT.md");
      const matches = hb.match(/## Quaid Janitor/g);
      assert(matches && matches.length === 1,
        `Expected exactly 1 Quaid Janitor section, found ${matches?.length || 0}`);
    }
  );

  // --- DATA PRESERVATION & MIGRATION ---

  await runScenario(
    "dirty-data-preservation",
    "Dirty install — verify existing DB facts survive reinstall",
    (ws) => {
      setupDirtyEnvironment(ws);
      // Add distinctive facts to the DB that we'll verify after install
      const dbPath = path.join(ws, "data", "memory.db");
      spawnSync("python3", ["-c", `
import sqlite3
conn = sqlite3.connect('${dbPath}')
conn.execute("INSERT INTO nodes (id, name, type, owner_id, status, confidence, source) VALUES ('test-3', 'User has a cat named Pixel', 'Fact', 'test-user', 'active', 0.95, 'test')")
conn.execute("INSERT INTO nodes (id, name, type, owner_id, status, confidence, source) VALUES ('test-4', 'User birthday is March 15', 'Fact', 'test-user', 'active', 0.9, 'test')")
conn.execute("INSERT INTO nodes (id, name, type, owner_id, status, confidence, source) VALUES ('test-5', 'User prefers vim keybindings', 'Preference', 'test-user', 'active', 0.85, 'test')")
conn.commit()
conn.close()
      `], { stdio: "pipe" });
    },
    [
      // step1: backup? yes (preserve data)
      true,
      // step2: confirm name
      true,
      // step3: anthropic, normal
      "anthropic", "normal",
      // step4: none
      "none",
      // step5: keep all
      true,
      // step6: 3AM, confirm
      "3", true,
      // step7: migrate? no (just preserve existing)
      false,
    ],
    (ws, res, stdout) => {
      assert(res?.success, `Installer failed: ${res?.error}`);

      // Verify ALL pre-existing facts survived in the DB
      const dbPath = path.join(ws, "data", "memory.db");
      const checkResult = spawnSync("python3", ["-c", `
import sqlite3, json
conn = sqlite3.connect('${dbPath}')
rows = conn.execute("SELECT name, type, status FROM nodes WHERE owner_id = 'test-user' ORDER BY name").fetchall()
print(json.dumps([{"name": r[0], "type": r[1], "status": r[2]} for r in rows]))
conn.close()
      `], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });

      const facts = JSON.parse(checkResult.stdout.trim());
      const names = facts.map(f => f.name);

      assert(names.includes("Test User likes coffee"), "Pre-existing fact 'likes coffee' missing from DB");
      assert(names.includes("Test User is a software engineer"), "Pre-existing fact 'software engineer' missing from DB");
      assert(names.includes("User has a cat named Pixel"), "Pre-existing fact 'cat named Pixel' missing from DB");
      assert(names.includes("User birthday is March 15"), "Pre-existing fact 'birthday March 15' missing from DB");
      assert(names.includes("User prefers vim keybindings"), "Pre-existing fact 'vim keybindings' missing from DB");

      // All should still be active
      for (const f of facts) {
        assert(f.status === "active", `Fact "${f.name}" should be active, got ${f.status}`);
      }

      // SOUL.md and USER.md content should be preserved
      const soul = readFile(ws, "SOUL.md");
      assert(soul.includes("helpful assistant"), "SOUL.md content was overwritten");
      const user = readFile(ws, "USER.md");
      assert(user.includes("San Francisco"), "USER.md content was overwritten");
    }
  );

  await runScenario(
    "dirty-migration-verified",
    "Dirty install with migration — verify markdown facts imported into DB",
    (ws) => {
      setupDirtyEnvironment(ws);
      // Add distinctive content to SOUL.md and USER.md for migration
      fs.writeFileSync(path.join(ws, "SOUL.md"), `# SOUL

## Personality
I am a thoughtful assistant who values precision and clarity.
I prefer to explain things with concrete examples rather than abstractions.
I enjoy debugging complex systems and finding root causes.

## Communication Style
Direct and clear, avoiding unnecessary filler words.
Uses technical terminology when appropriate.
`);
      fs.writeFileSync(path.join(ws, "USER.md"), `# USER

## About
Name: Douglas Quaid
Location: Mars Colony, Venusville District
Occupation: Construction worker turned resistance fighter

## Preferences
Prefers action over deliberation when under pressure.
Enjoys exploring memories of Earth before the relocation.
Drinks Martian water, not recycled station water.
Has a wife named Melina who works at the Last Resort.
`);
    },
    [
      // step1: backup? no
      false,
      // step2: confirm name
      true,
      // step3: anthropic, normal
      "anthropic", "normal",
      // step4: none
      "none",
      // step5: keep all
      true,
      // step6: 3AM, confirm
      "3", true,
      // step7: migrate? yes
      true,
    ],
    (ws, res, stdout) => {
      assert(res?.success, `Installer failed: ${res?.error}`);

      // Verify migration imported facts from SOUL.md and USER.md
      const dbPath = path.join(ws, "data", "memory.db");
      const checkResult = spawnSync("python3", ["-c", `
import sqlite3
conn = sqlite3.connect('${dbPath}')
rows = conn.execute("SELECT name, type FROM nodes WHERE source = 'migration'").fetchall()
print(len(rows))
for r in rows:
    print(f"{r[1]}: {r[0]}")
conn.close()
      `], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });

      const output = checkResult.stdout.trim();
      const count = parseInt(output.split("\n")[0]);
      assert(count > 0, `Expected migration to import facts, got ${count}. Output: ${output}`);

      // Check that content from both files made it into the DB
      const allFacts = output.toLowerCase();
      assert(allFacts.includes("mars") || allFacts.includes("quaid") || allFacts.includes("melina"),
        "Expected USER.md content (Mars/Quaid/Melina) in migrated facts");
    },
    { QUAID_TEST_MOCK_MIGRATION: "1" }
  );

  await runScenario(
    "dirty-no-ollama",
    "Dirty install with Ollama unavailable — should fall back to keyword-only",
    setupDirtyEnvironment,
    [
      // step1: backup? no
      false,
      // step2: confirm name
      true,
      // step3: anthropic, normal
      "anthropic", "normal",
      // step4: Ollama not found → "Install Ollama now?" → no
      // (no embed model prompt since Ollama unavailable → auto keyword-only)
      false,
      // step5: keep all
      true,
      // step6: 3AM, confirm
      "3", true,
      // step7: migrate? no
      false,
    ],
    (ws, res, stdout) => {
      assert(res?.success, `Installer failed: ${res?.error}`);
      // Should have fallen back to keyword-only (no embeddings)
      const config = readJSON(ws, "config", "memory.json");
      assert(config.ollama.embeddingModel === "none",
        `Expected embedModel "none", got "${config.ollama.embeddingModel}"`);
      assert(config.ollama.embeddingDim === 0,
        `Expected embedDim 0, got ${config.ollama.embeddingDim}`);
      // Existing data should still be intact
      assert(fileExists(ws, "data", "memory.db"), "DB should still exist");
      const soul = readFile(ws, "SOUL.md");
      assert(soul.includes("helpful assistant"), "SOUL.md content should be preserved");
    },
    { QUAID_TEST_NO_OLLAMA: "1" }
  );

  // --- EDGE CASES ---

  await runScenario(
    "fresh-all-systems-off",
    "Fresh install, all 4 systems disabled",
    null,
    [
      true,              // step2: confirm name
      "anthropic",       // step3: provider
      "quiet",           // step3: notif level
      "none",            // step4: embed model
      false,             // step5: don't keep all
      false,             // step5: Memory? no
      false,             // step5: Journal? no
      false,             // step5: Projects? no
      false,             // step5: Workspace? no
      "skip",            // step6: skip
    ],
    (ws, res, stdout) => {
      // Should still succeed even with everything off
      assert(res?.success, "Should succeed with all systems off");
      verifyConfig(ws, {
        systems: { memory: false, journal: false, projects: false, workspace: false },
      });
      // No journal files
      assert(!fileExists(ws, "journal", "SOUL.journal.md"), "No journal files when disabled");
    }
  );

  await runScenario(
    "fresh-together-provider",
    "Fresh install, Together AI provider",
    null,
    [
      true,                                              // step2: confirm name
      "together",                                        // step3: provider (non-anthropic)
      "meta-llama/Llama-3.1-405B-Instruct-Turbo",       // step3: high model
      "meta-llama/Llama-3.1-8B-Instruct-Turbo",         // step3: low model
      "normal",                                          // step3: notif level
      "none",                                            // step4: embed model
      true,                                              // step5: keep all
      "3",                                               // step6: hour
      true,                                              // step6: confirm
    ],
    (ws, res, stdout) => {
      verifyFreshInstall(ws, res, stdout, "", 0);
      verifyConfig(ws, {
        provider: "openai",
        highModel: "meta-llama/Llama-3.1-405B-Instruct-Turbo",
        lowModel: "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        baseUrl: "https://api.together.xyz/v1",
      });
      const config = readJSON(ws, "config", "memory.json");
      assert(config.models.apiKeyEnv === "TOGETHER_API_KEY",
        `Expected TOGETHER_API_KEY, got ${config.models.apiKeyEnv}`);
    }
  );

  // --- REPORT ---
  console.log("\n  Results\n  -------");
  let passed = 0, failed = 0, skipped = 0;
  for (const r of results) {
    const icon = r.status === "PASS" ? "\x1b[32m✓\x1b[0m"
               : r.status === "FAIL" ? "\x1b[31m✗\x1b[0m"
               : "\x1b[33m-\x1b[0m";
    console.log(`  ${icon} ${r.name}: ${r.description}`);
    if (r.status === "FAIL") {
      console.log(`    \x1b[31mError: ${r.error}\x1b[0m`);
      if (r.stderr) console.log(`    stderr: ${r.stderr.slice(-200)}`);
      failed++;
    } else if (r.status === "SKIP") {
      skipped++;
    } else {
      passed++;
    }
  }
  console.log(`\n  ${passed} passed, ${failed} failed, ${skipped} skipped\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runAllScenarios();
