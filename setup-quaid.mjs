#!/usr/bin/env node
// =============================================================================
// Quaid Knowledge Layer Plugin — Guided Installer
// =============================================================================
// Interactive installer using @clack/prompts (resolved from OpenClaw).
// Supports two modes:
//   - Standalone (default): Uses hidden Quaid home (~/.quaid) with visible workspace (~/quaid)
//   - OpenClaw: detected via OPENCLAW_WORKSPACE env or openclaw on PATH
//
// Author: Steadman Labs (https://github.com/quaid-labs)
// License: MIT
// =============================================================================

import { execSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import {
  adapterSelectOptions,
  loadAdapterManifests,
  resolveAdapterHookScript,
  syncBuiltinAdapterManifests,
} from "./modules/quaid/lib/adapter-manifests.mjs";
import { shouldStartExtractionDaemonAfterInstall } from "./lib/install-daemon-policy.mjs";
import {
  deriveInstallerLlmProviderSetting,
  installerDefaultProvider,
  installerFallbackModelDefaults,
  installerFallbackProviders,
  resolveInstallerProvider,
} from "./lib/install-model-defaults.mjs";
import {
  deepMergeMissing,
  readJsonObject,
  writeJsonObject,
} from "./lib/install-config-hydration.mjs";
import { ensureOpenClawExtensionDependencies } from "./lib/openclaw-extension-deps.mjs";
import { ensureOpenClawAgentModelDefault } from "./lib/openclaw-agent-model-default.mjs";
import {
  captureOpenClawManagedState,
  composeOpenClawManagedStateSnapshots,
  readOpenClawManagedStateSnapshot,
  restoreOpenClawManagedState,
  writeOpenClawManagedStateSnapshot,
} from "./lib/openclaw-managed-state.mjs";
import { sanitizeOpenClawNativeMemoryPlugins } from "./lib/openclaw-plugin-sanitizer.mjs";
import { renderQuaidBanner } from "./lib/quaid_banner.mjs";
import {
  SHARED_AUTH_KIND_LABELS,
  getSharedAuthCredential,
  inferSharedAuthKind,
  sharedAuthRegistryPath,
  writeSharedAuthCredential,
} from "./lib/auth-registry.mjs";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Platform check — Quaid requires macOS or Linux
if (os.platform() === 'win32') {
  console.error('error: Quaid requires macOS or Linux. Windows is not supported.');
  process.exit(1);
}

// Dev machine guard — never install Quaid on the Quaid development machine.
// The dev checkout at ~/quaidcode/dev/modules/quaid is the unambiguous signal.
// All installs must target the livetest VM, not the box where we develop.
const _devCheckoutMarker = path.join(os.homedir(), 'quaidcode', 'dev', 'modules', 'quaid');
if (fs.existsSync(_devCheckoutMarker) && !process.env.QUAID_ALLOW_DEV_INSTALL) {
  console.error('error: This looks like the Quaid dev machine (found ~/quaidcode/dev/modules/quaid/).');
  console.error('       Never install Quaid on the dev box — install on the livetest VM instead.');
  console.error('       To override (not recommended): QUAID_ALLOW_DEV_INSTALL=1 node setup-quaid.mjs');
  process.exit(1);
}

function parseInstallArgs(argv) {
  const opts = {
    workspace: "",
    ownerName: "",
    adapter: "",
    source: "",
    ref: "",
    githubRepo: "",
    artifact: "",
    agent: false,
    claudeCode: false,
    force: false,
    addInstance: false,
    dryRun: false,
    survey: false,
    help: false,
    allPlatforms: false,
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
    if (arg === "--adapter") {
      const next = argv[i + 1] || "";
      if (!next || next.startsWith("--")) {
        opts.errors.push("--adapter requires a value");
      } else {
        opts.adapter = next;
        i++;
      }
      continue;
    }
    if (arg.startsWith("--adapter=")) {
      const value = arg.slice("--adapter=".length);
      if (!value) {
        opts.errors.push("--adapter requires a non-empty value");
      } else {
        opts.adapter = value;
      }
      continue;
    }
    if (arg === "--claude-code") {
      opts.claudeCode = true;
      continue;
    }
    if (arg === "--all-platforms") {
      opts.allPlatforms = true;
      continue;
    }
    if (arg === "--force") {
      opts.force = true;
      continue;
    }
    if (arg === "--add-instance") {
      opts.addInstance = true;
      continue;
    }
    if (arg === "--dry-run") {
      opts.dryRun = true;
      continue;
    }
    if (arg === "--survey") {
      opts.survey = true;
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
  --workspace <path>  Deprecated. Installer home is fixed to ~/.quaid
  --owner-name <name> Person name used to tag memories (recommended for --agent)
  --adapter <id>      Force adapter/platform id (e.g. standalone, claude-code, openclaw, codex)
  --source <kind>     Plugin source: local (default), github, artifact
  --ref <git-ref>     Git ref/commit to install when --source github
  --github-repo <r>   GitHub repo for github source (default: quaid-labs/quaid)
  --artifact <path>   Local file path or URL to .tar.gz when --source artifact
  --agent             Non-interactive agent mode (accepts sane defaults)
  --all-platforms     Install every currently installable platform by reusing
                      the normal per-platform install flow in sequence
  --claude-code       Install for Claude Code (hooks + OAuth provider)
  --add-instance      Allow install on a host that already has Quaid and
                      provision a new silo. For OpenClaw, also bind gateway
                      env to the requested instance.
  --force             Allow a full reinstall on a host that already has Quaid.
  --dry-run           Run all prompts and checks but skip writes — outputs the
                      install plan and exits. Useful for validating interactive
                      UX and comparing against agent-mode output.
  --survey            With --dry-run, print the canonical pre-install survey
                      derived from resolved installer values.
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
const FORCED_ADAPTER_TYPE = String(INSTALL_ARGS.adapter || "").trim().toLowerCase().replace(/_/g, "-");
const INSTALL_SOURCE = String(INSTALL_ARGS.source || process.env.QUAID_INSTALL_SOURCE || "local").trim().toLowerCase();
const INSTALL_REF = String(INSTALL_ARGS.ref || process.env.QUAID_INSTALL_REF || "main").trim();
const INSTALL_GITHUB_REPO = String(INSTALL_ARGS.githubRepo || process.env.QUAID_INSTALL_GITHUB_REPO || "quaid-labs/quaid").trim();
const INSTALL_ARTIFACT = String(INSTALL_ARGS.artifact || process.env.QUAID_INSTALL_ARTIFACT || "").trim();
const SURVEY_ONLY = !!INSTALL_ARGS.survey;
const INSTALL_ALL_PLATFORMS = !!INSTALL_ARGS.allPlatforms;
const FORCE_INSTALL = !!INSTALL_ARGS.force;
const ADD_INSTANCE_MODE = !!INSTALL_ARGS.addInstance;
const ALLOW_EXISTING_INSTALL = FORCE_INSTALL || ADD_INSTANCE_MODE;
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
if (SURVEY_ONLY && !INSTALL_ARGS.dryRun) {
  console.error("[x] --survey requires --dry-run.");
  process.exit(2);
}
if (INSTALL_ALL_PLATFORMS && (FORCED_ADAPTER_TYPE || INSTALL_ARGS.claudeCode)) {
  console.error("[x] --all-platforms cannot be combined with --adapter or --claude-code.");
  process.exit(2);
}

const FIXED_QUAID_HOME = path.resolve(path.join(os.homedir(), ".quaid"));
const FIXED_VISIBLE_HOME = path.resolve(path.join(os.homedir(), "quaid"));
let _preinstallOpenClawManagedState = null;

function _normalizeInstallPath(raw) {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (value === "~") return os.homedir();
  if (value.startsWith("~/")) return path.resolve(path.join(os.homedir(), value.slice(2)));
  return path.resolve(value);
}

function _enforceFixedInstallHome() {
  const requested = _normalizeInstallPath(
    INSTALL_ARGS.workspace ||
    process.env.QUAID_WORKSPACE ||
    process.env.QUAID_HOME
  );
  if (requested && requested !== FIXED_QUAID_HOME) {
    console.error("[x] Quaid installs now always use a fixed home path.");
    console.error(`    Requested: ${requested}`);
    console.error(`    Required:  ${FIXED_QUAID_HOME}`);
    console.error("    Remove the custom workspace override and re-run install.");
    process.exit(2);
  }
}

_enforceFixedInstallHome();

// --- Constants ---
const VERSION = "0.3.0-alpha";
const HOOKS_PR_URL = "https://github.com/openclaw/openclaw/releases/tag/v2026.3.7";
const MIN_GATEWAY_VERSION = "2026.3.7";
const PROJECT_URL = "https://github.com/quaid-labs/quaid";
const TOTAL_INSTALL_STEPS = 7;
const MINIMAL_QUAID_PROJECT_AGENTS_MD = `# Quaid — Operating Guide

## File Placement — MANDATORY RULES

**You MUST NOT write any file to \`/tmp/\`, \`/var/tmp/\`, or anywhere under \`~/quaid/\` or \`~/.quaid/\` except through Quaid's own managed flows.** Quaid home is not a dumping ground. Every file must either live in a tracked project or be written at a real working path and immediately registered into one.

**Before writing any file or delegating work to a sub-agent, pick the first matching rule:**

1. **Existing project owns this work** -> place the file inside that project's directory.
2. **Throwaway / one-off / scratch / quick / hello-world** -> use the misc project as the owner, but do not treat \`~/quaid/\` or \`~/.quaid/\` as the working directory:
   \`\`\`bash
   # The misc project is pre-created. Confirm it exists:
   quaid project show misc--$QUAID_INSTANCE
   # Write the file at a real working path, then register it to misc:
   quaid registry register /absolute/path/to/hello.py --project misc--$QUAID_INSTANCE
   \`\`\`
   Prefer a user-visible working path or the active repo. Always tell the user the file is tracked by the misc project and offer to promote it to a real project.
3. **Durable new work** -> create a project first, then write files:
   \`\`\`bash
   quaid project create <name> --source-root <path>
   # THEN write files / spawn sub-agents
   \`\`\`
4. **User specifies a path outside the project system** -> write there, then register the file so the project tracks it:
   \`\`\`bash
   # Write the file at the user's requested path (e.g. ~/my-scripts/tool.py)
   # Then link it into the owning project:
   quaid registry register ~/my-scripts/tool.py --project <name>
   \`\`\`
   Always tell the user the file is tracked via the registry even though it lives outside the project directory.

**Example — user asks for a throwaway script:**
> "Can you write a quick hello world script?"

Correct response:
\`\`\`bash
# Step 1: confirm misc project exists
quaid project show misc--$QUAID_INSTANCE
# Step 2: write the file at a real working path
# Step 3: register it to misc
quaid registry register /absolute/path/to/hello.py --project misc--$QUAID_INSTANCE
\`\`\`
Tell the user: "I tracked it under the misc project so it stays in Quaid's project system."
Do NOT write to \`/tmp/hello.py\` or any other path.

**Example — user asks to build a new tool:**
> "I have a Python script. Can you build it into a proper CLI tool?"

Correct response:
\`\`\`bash
# Step 1: create a project BEFORE doing any work or spawning sub-agents
quaid project create my-cli-tool --source-root /path/to/script
# Step 2: then proceed with the work
\`\`\`
`;

function authKindsForProvider(provider = "") {
  const normalized = String(provider || "").trim().toLowerCase();
  if (normalized === "openai") return ["codex_oauth", "openai_api"];
  return ["anthropic_oauth", "anthropic_api"];
}

function allSharedAuthKinds() {
  return Object.keys(SHARED_AUTH_KIND_LABELS);
}

function authKindPromptLabel(kind = "") {
  return SHARED_AUTH_KIND_LABELS[String(kind || "").trim()] || String(kind || "").trim();
}

// AI agents: this is the canonical pre-install survey contract.
// Only include fields listed here when presenting a survey to a human.
// Everything not listed here is out of scope for the survey.
// Execution order is mandatory:
//   1. Read AI-INSTALL.md and this file
//   2. Present the survey in the conversation
//   3. Wait for human approval or edits
//   4. Only then run install
// Do not keep exploring source files once you already have enough information
// to present the survey. For a thin "follow AI-INSTALL.md" prompt, the survey
// is the next required assistant output.
// Before the survey, scope is intentionally narrow:
//   - allowed reads: AI-INSTALL.md and this file
//   - allowed shell checks: only minimal checks needed to fill defaults
//     (for example RAM / Ollama availability)
//   - do not inspect adapter source, memory config files, or unrelated repo docs
//   - do not run exploratory commands like find/pwd/broad grep before the survey
// Thin prompt contract:
//   - if the human only says "follow AI-INSTALL.md" with adapter/owner,
//     do not rediscover those values
//   - if adapter/platform and owner are already given,
//     execute the survey command immediately after the initial read; do not
//     keep planning, run pwd/ls, or re-read the guide
//   - run `node setup-quaid.mjs --agent --dry-run --survey ...` first and use
//     that output as the conversational survey
//   - do not plan aloud or keep browsing once you can fill the survey
//   - the next assistant output must be the survey rendered in the order of
//     AGENT_SURVEY_CONTRACT.fields, then a request for approval
//   - use the literal first-response template from AI-INSTALL.md; do not invent
//     a different survey shape during thin-prompt installs
const AGENT_SURVEY_CONTRACT = {
  sourceOfTruth: "setup-quaid.mjs",
  rule: "Only the fields listed here belong in the pre-install survey.",
  firstCommand:
    "node setup-quaid.mjs --agent --dry-run --survey --adapter <target-platform> --owner-name <owner-name>",
  preSurveyRule:
    "Before the survey, read only AI-INSTALL.md and setup-quaid.mjs plus minimal environment checks needed to fill defaults. If adapter/platform and owner are already provided, execute the survey command immediately after that initial read. Do not browse adapter files, memory config files, unrelated repo docs, or run exploratory shell commands like pwd/ls/find.",
  outputRule:
    "Render the survey fields in contract order, show the selected value for each, and end with: Do you want to change any of these before I run install?",
  firstResponseRule:
    "For thin-prompt installs, the first assistant response must be the survey itself with no planning preamble, using the AI-INSTALL.md first-response template.",
  preferredMechanism:
    "For thin-prompt installs, prefer `node setup-quaid.mjs --agent --dry-run --survey ...` and use its output as the survey instead of hand-synthesizing one.",
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
      required: false,
      notes: [
        "Only include this field for OpenClaw installs.",
        "For non-OpenClaw installs, omit notification routing channel from the survey entirely.",
        "Do not mention OpenClaw channels, last_used, or routing fallbacks on Codex/Claude Code installs.",
        "For OpenClaw installs, survey the explicit runtime notification channel.",
        "If no active OpenClaw route is detected, the survey may show last_used fallback.",
      ],
    },
    {
      id: "platform_compatibility_notices",
      label: "Platform compatibility notices",
      source: "adapter manifest install.compatibilityWarnings",
      required: true,
      notes: [
        "Always include this field in the survey.",
        "If the adapter manifest has no warnings, render the value as none.",
      ],
    },
  ],
  notes: [
    "Do not add survey sections for internal installer steps with no user choice.",
    "Do not use test-only controls like QUAID_TEST_ANSWERS in normal AI install guidance unless explicitly running a test harness.",
    "Workspace file import is not a standalone survey field unless the installer actually prompts for it.",
    "Installer home is fixed to ~/.quaid (visible workspace: ~/quaid) and is not a user-selectable field.",
    "Janitor runs automatically by default and is not a survey field unless the human explicitly asks to change janitor behavior.",
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
    shell("openclaw config get workspace 2>/dev/null </dev/null") ||
    readWorkspaceFromOpenClawConfig()
  );
}
const IS_CLAUDE_CODE = INSTALL_ARGS.claudeCode || process.env.QUAID_INSTALL_CLAUDE_CODE === "1";
const IS_OPENCLAW = !IS_CLAUDE_CODE && !!(process.env.OPENCLAW_WORKSPACE || which("openclaw"));
const WORKSPACE = FIXED_QUAID_HOME;
const VISIBLE_HOME = FIXED_VISIBLE_HOME;
const AGENT_MODE = INSTALL_ARGS.agent || process.env.QUAID_INSTALL_AGENT === "1" || !process.stdin.isTTY;
const DEBUG_SETUP = AGENT_MODE || process.env.QUAID_INSTALL_DEBUG === "1" || process.env.DEBUG_SETUP === "1";
const DRY_RUN = !!(INSTALL_ARGS.dryRun || process.env.QUAID_INSTALL_DRY_RUN === "1");
const MODULES_PLUGIN_DIR = path.join(WORKSPACE, "modules", "quaid");
const LEGACY_PLUGIN_DIR = path.join(WORKSPACE, "plugins", "quaid");
const PLUGIN_DIR = fs.existsSync(path.join(MODULES_PLUGIN_DIR, "package.json"))
  ? MODULES_PLUGIN_DIR
  : LEGACY_PLUGIN_DIR;
const LEGACY_CONFIG_DIR = path.join(WORKSPACE, "config");
const RUNTIME_DIR = path.join(WORKSPACE, "runtime");
const RUNTIME_NOTES_DIR = path.join(RUNTIME_DIR, "notes");
const PROJECTS_DIR = path.join(VISIBLE_HOME, "projects");
const ADAPTER_REGISTRY_DIR = path.join(WORKSPACE, "adaptors");
const HIDDEN_INSTANCES_DIR = path.join(WORKSPACE, "instances");
const VISIBLE_INSTANCES_DIR = path.join(VISIBLE_HOME, "instances");
const SHARED_DATA_DIR = path.join(WORKSPACE, "data");
const SHARED_LOGS_DIR = path.join(WORKSPACE, "logs");

process.env.QUAID_HOME = WORKSPACE;
process.env.QUAID_VISIBLE_HOME = VISIBLE_HOME;
process.env.QUAID_WORKSPACE = WORKSPACE;

function _resolvedInstallerPathInstanceId(instanceId = "") {
  return String(instanceId || resolvedInstallerInstanceId()).trim();
}

function hiddenInstanceDir(instanceId = "") {
  const resolved = _resolvedInstallerPathInstanceId(instanceId);
  return resolved ? path.join(HIDDEN_INSTANCES_DIR, resolved) : HIDDEN_INSTANCES_DIR;
}

function visibleInstanceDir(instanceId = "") {
  const resolved = _resolvedInstallerPathInstanceId(instanceId);
  return resolved ? path.join(VISIBLE_INSTANCES_DIR, resolved) : VISIBLE_INSTANCES_DIR;
}

function instanceConfigPath(instanceId = "") {
  const resolved = _resolvedInstallerPathInstanceId(instanceId);
  return resolved ? path.join(hiddenInstanceDir(resolved), "config.json") : "";
}

function hiddenInstanceDataDir(instanceId = "") {
  const resolved = _resolvedInstallerPathInstanceId(instanceId);
  return resolved ? path.join(hiddenInstanceDir(resolved), "data") : SHARED_DATA_DIR;
}

function hiddenInstanceLogsDir(instanceId = "") {
  const resolved = _resolvedInstallerPathInstanceId(instanceId);
  return resolved ? path.join(hiddenInstanceDir(resolved), "logs") : SHARED_LOGS_DIR;
}

function hiddenInstanceDbPath(instanceId = "") {
  return path.join(hiddenInstanceDataDir(instanceId), "memory.db");
}

function hiddenInstanceInstallStatePath(instanceId = "") {
  return path.join(hiddenInstanceDataDir(instanceId), "installed-at.json");
}

function _seedJanitorInstallCheckpoint(instanceId = "") {
  const resolved = String(instanceId || "").trim();
  if (!resolved) return;
  try {
    const checkpointPath = path.join(hiddenInstanceLogsDir(resolved), "janitor", "checkpoint-all.json");
    fs.mkdirSync(path.dirname(checkpointPath), { recursive: true });
    const existing = readJsonObject(checkpointPath) || {};
    const status = String(existing?.status || "").trim().toLowerCase();
    const installSeeded = Boolean(existing?.install_seeded);
    const completedAt = String(existing?.last_completed_at || "").trim();
    if (status === "running" || installSeeded || completedAt) return;
    if (status && status !== "completed") return;
    const nowIso = new Date().toISOString();
    const seeded = {
      ...existing,
      task: String(existing?.task || "all"),
      started_at: String(existing?.started_at || nowIso),
      heartbeat_at: nowIso,
      last_completed_at: nowIso,
      status: "completed",
      install_seeded: true,
    };
    writeJsonObject(checkpointPath, seeded);
    log.info(`Seeded janitor health checkpoint: ${checkpointPath}`);
  } catch (err) {
    log.warn(`Could not seed janitor health checkpoint: ${String(err?.message || err)}`);
  }
}

function runtimePendingInstallMigrationPath() {
  return path.join(RUNTIME_DIR, "pending-install-migration.json");
}

let _adapterManifests = [];
let _existingInstallDetected = false;
let _chainedPlatformInstall = false;
let _chainedPlatformQueue = [];
let _sharedCredentialSelection = "unset";
let _releaseOpenClawHostLock = null;

function _refreshAdapterManifests() {
  try {
    syncBuiltinAdapterManifests({ workspace: WORKSPACE, installerDir: __dirname });
  } catch {
    // Non-fatal: installer can continue with built-in fallbacks.
  }
  _adapterManifests = loadAdapterManifests(WORKSPACE);
}

function _adapterManifestById(id) {
  const key = String(id || "").trim().toLowerCase();
  return _adapterManifests.find((m) => String(m?.id || "").toLowerCase() === key) || null;
}

function _adapterOptionsForSelect() {
  const options = adapterSelectOptions(_adapterManifests);
  if (options.length > 0) return options;
  return [
    { value: "claude-code", label: "Claude Code", hint: "hooks + OAuth for Claude Code CLI" },
    { value: "openclaw", label: "OpenClaw", hint: "gateway-integrated runtime" },
    { value: "codex", label: "Codex", hint: "hooks + app-server sidecar runtime" },
  ];
}

function _remainingInstallableAdapterOptions(excludeAdapterId = "") {
  _refreshAdapterManifests();
  const excluded = String(excludeAdapterId || "").trim().toLowerCase();
  return _adapterOptionsForSelect()
    .map((opt) => {
      const installState = _readAdapterInstallState(opt.value);
      return { ...opt, installState };
    })
    .filter((opt) => String(opt.value || "").trim().toLowerCase() !== excluded)
    .filter((opt) => opt.installState.status === "can_install");
}

function _beginChainedPlatformInstall(adapterId, queuedAdapters = []) {
  _platformOverride = adapterId;
  _instanceIdOverride = "";
  delete process.env.QUAID_INSTANCE;
  syncInstallerInstanceEnv(adapterId);
  _chainedPlatformInstall = true;
  _chainedPlatformQueue = Array.isArray(queuedAdapters)
    ? queuedAdapters.map((v) => String(v || "").trim().toLowerCase()).filter(Boolean)
    : [];

  const warnings = _adapterCompatibilityWarnings(adapterId);
  if (warnings.length > 0) {
    for (const msg of warnings) {
      log.warn(`  ${msg}`);
    }
  }
}

async function promptNextPlatformInstall(installedPlatform) {
  if (_chainedPlatformQueue.length > 0) {
    const [nextAdapter, ...queuedAdapters] = _chainedPlatformQueue;
    _beginChainedPlatformInstall(nextAdapter, queuedAdapters);
    return true;
  }
  if (AGENT_MODE || DRY_RUN || SURVEY_ONLY || _testAnswers || FORCED_ADAPTER_TYPE) {
    return false;
  }
  const installed = String(installedPlatform || resolvedInstallerPlatform() || "").trim().toLowerCase();
  const remaining = _remainingInstallableAdapterOptions(installed);
  if (remaining.length === 0) return false;

  const answer = handleCancel(await select({
    message: `You've installed Quaid on ${_installerPlatformLabel()}. Other supported platforms were detected. Install another?`,
    initialValue: "exit",
    options: [
      ...(remaining.length > 1 ? [{
        value: "__install_all__",
        label: "Install All Available",
        hint: `Install ${remaining.map((opt) => opt.label).join(", ")} in sequence`,
      }] : []),
      ...remaining.map((opt) => ({
        value: opt.value,
        label: opt.label,
        hint: opt.hint,
      })),
      { value: "exit", label: "Exit", hint: "Finish installation" },
    ],
  }));
  if (answer === "exit") return false;

  if (answer === "__install_all__") {
    const [firstAdapter, ...queuedAdapters] = remaining.map((opt) => opt.value);
    _beginChainedPlatformInstall(firstAdapter, queuedAdapters);
    return true;
  }

  _beginChainedPlatformInstall(answer, []);
  return true;
}

function _adapterCompatibilityWarnings(adapterId) {
  const manifest = _adapterManifestById(adapterId);
  if (!manifest || !manifest.install || !Array.isArray(manifest.install.compatibilityWarnings)) {
    return [];
  }
  return manifest.install.compatibilityWarnings
    .map((v) => String(v || "").trim())
    .filter(Boolean);
}

function _requiredAuthKindsForAdapterProvider(adapterType, provider = "") {
  const platform = String(adapterType || resolvedInstallerPlatform() || "").trim().toLowerCase();
  if (!platform) return [];
  if (platform === "claude-code") return ["anthropic_oauth", "anthropic_api"];
  if (platform === "codex" || platform === "openclaw") {
    return authKindsForProvider(provider || installerDefaultProvider(platform));
  }
  return [];
}

async function _ensureCompatibleSharedCredentialForInstall(adapterType, provider, sharedAuthTokenPath) {
  const requiredKinds = _requiredAuthKindsForAdapterProvider(adapterType, provider);
  if (requiredKinds.length === 0) return;

  // If a compatible credential already exists in the registry, all good.
  const compatibleCredential = getSharedAuthCredential(WORKSPACE, requiredKinds);
  if (compatibleCredential?.token) return;

  // No compatible credential — check what IS there to generate a useful mismatch message.
  const currentCredential = getSharedAuthCredential(WORKSPACE, allSharedAuthKinds());
  const currentKind = String(currentCredential?.kind || "").trim();
  const currentToken = String(currentCredential?.token || "").trim();
  if (!currentKind || !currentToken) return;

  const requiredLabels = requiredKinds.map((kind) => authKindPromptLabel(kind)).join(" or ");
  const providerLabel = String(provider || installerDefaultProvider(adapterType) || "provider").trim().toLowerCase();
  const mismatchMessage =
    `${_installerPlatformLabel(adapterType)} requires ${requiredLabels} for its ${providerLabel} lane, `
    + `but the shared registry currently has ${authKindPromptLabel(currentKind)}.`;

  if (!_existingInstallDetected && !AGENT_MODE) {
    log.warn(mismatchMessage);
    log.info(C.dim(`Canonical registry: ${sharedAuthTokenPath}`));
    const newToken = handleCancel(await text({
      message: `${_installerPlatformLabel(adapterType)} ${providerLabel} credential:`,
      placeholder: `paste ${requiredLabels} token here`,
      validate: (v) => (!v || !v.trim()) ? "Credential is required." : undefined,
    }));
    if (newToken && newToken.trim()) {
      const inferredKind = inferSharedAuthKind(newToken.trim()) || "";
      if (!requiredKinds.includes(inferredKind)) {
        bail(
          `Install incomplete: credential kind ${authKindPromptLabel(inferredKind || "unknown")} `
          + `does not satisfy ${_installerPlatformLabel(adapterType)} ${providerLabel}. `
          + `Expected ${requiredLabels}.`
        );
      }
      if (!DRY_RUN) {
        writeSharedAuthCredential(WORKSPACE, inferredKind, newToken.trim());
        _sharedCredentialSelection = "reset";
        log.success(`Credential stored in shared registry as ${authKindPromptLabel(inferredKind)} at ${sharedAuthTokenPath}`);
      } else {
        log.info(C.dim(`(dry run) Would store ${authKindPromptLabel(inferredKind)} at ${sharedAuthTokenPath}`));
      }
      return;
    }
  }

  bail(
    `Install incomplete: shared auth credential kind ${authKindPromptLabel(currentKind || "unknown")} `
    + `does not satisfy ${_installerPlatformLabel(adapterType)} ${providerLabel}. `
    + `Expected ${requiredLabels}. Registry: ${sharedAuthTokenPath}`
  );
}

function _normalizeAdapterInstallState(raw) {
  const status = String(raw?.status || "").trim().toLowerCase();
  const reason = String(raw?.reason || "").trim();
  if (status === "already_installed" || status === "cannot_install") {
    return { status, reason };
  }
  return { status: "can_install", reason };
}

function _readAdapterInstallState(adapterId) {
  try {
    return _normalizeAdapterInstallState(_runAdapterInstallerJson(adapterId, [
      "import json, os, sys",
      "sys.path.insert(0, os.environ.get('QUAID_PYTHONPATH', ''))",
      "from lib.adapter import _load_adapter_class_from_manifest",
      "aid = os.environ.get('QUAID_ADAPTER_TYPE', '').strip()",
      "adapter_cls = _load_adapter_class_from_manifest(aid)",
      "state = adapter_cls.installer_install_state(os.environ.get('QUAID_HOME', ''))",
      "if not isinstance(state, dict):",
      "    raise RuntimeError('installer_install_state must return a dict')",
      "print(json.dumps({'status': str(state.get('status', '')).strip().lower(), 'reason': str(state.get('reason', '')).strip()}))",
    ]));
  } catch {
    return { status: "can_install", reason: "" };
  }
}

function _formatAdapterInstallHint(status, baseHint, reason = "") {
  const stateLabel = status === "already_installed"
    ? "already installed"
    : status === "cannot_install"
      ? "cannot install"
      : "can install";
  const detail = String(reason || "").trim() || String(baseHint || "").trim();
  return detail ? `${stateLabel} · ${detail}` : stateLabel;
}

function _readAdapterInstallerCapabilities(adapterId) {
  const normalized = String(adapterId || "").trim().toLowerCase();
  if (!normalized) return null;
  const instanceId = resolvedInstallerInstanceId(normalized);
  const probeEnv = {
    ...process.env,
    QUAID_HOME: WORKSPACE,
    QUAID_VISIBLE_HOME: VISIBLE_HOME,
    QUAID_WORKSPACE: WORKSPACE,
    QUAID_PYTHONPATH: path.join(__dirname, "modules", "quaid"),
    QUAID_ADAPTER_TYPE: normalized,
  };
  if (instanceId) probeEnv.QUAID_INSTANCE = instanceId;
  const py = [
    "import json, os, sys",
    "sys.path.insert(0, os.environ.get('QUAID_PYTHONPATH', ''))",
    "from lib.adapter import _instantiate_adapter_from_manifest",
    "aid = os.environ.get('QUAID_ADAPTER_TYPE', '').strip()",
    "adapter = _instantiate_adapter_from_manifest(aid)",
    "providers = list(adapter.installer_supported_providers() or [])",
    "out = {'providers': [str(p).strip().lower() for p in providers if str(p).strip()], 'modelDefaults': {}, 'defaultFastProvider': '', 'defaultDeepProvider': '', 'supportsLiveModelValidation': False}",
    "try:",
    "    out['defaultFastProvider'] = str(adapter.get_fast_provider_default() or '').strip().lower()",
    "except Exception:",
    "    out['defaultFastProvider'] = ''",
    "try:",
    "    out['defaultDeepProvider'] = str(adapter.get_deep_provider_default() or '').strip().lower()",
    "except Exception:",
    "    out['defaultDeepProvider'] = ''",
    "try:",
    "    out['supportsLiveModelValidation'] = bool(adapter.installer_supports_live_model_validation())",
    "except Exception:",
    "    out['supportsLiveModelValidation'] = False",
    "for p in out['providers']:",
    "    deep = ''",
    "    fast = ''",
    "    try:",
    "        deep = str(adapter.get_deep_model_default(p) or '').strip()",
    "    except Exception:",
    "        deep = ''",
    "    try:",
    "        fast = str(adapter.get_fast_model_default(p) or '').strip()",
    "    except Exception:",
    "        fast = ''",
    "    if not (deep and fast):",
    "        d = adapter.installer_default_models(p)",
    "        if isinstance(d, dict):",
    "            deep = deep or str(d.get('deep', '')).strip()",
    "            fast = fast or str(d.get('fast', '')).strip()",
    "    deep_effort = ''",
    "    fast_effort = ''",
    "    try:",
    "        d_eff = adapter.installer_default_models(p)",
    "        if isinstance(d_eff, dict):",
    "            deep_effort = str(d_eff.get('deepEffort', '')).strip()",
    "            fast_effort = str(d_eff.get('fastEffort', '')).strip()",
    "    except Exception:",
    "        deep_effort = ''",
    "        fast_effort = ''",
    "    if deep and fast:",
    "        row = {'deep': deep, 'fast': fast}",
    "        if deep_effort:",
    "            row['deepEffort'] = deep_effort",
    "        if fast_effort:",
    "            row['fastEffort'] = fast_effort",
    "        out['modelDefaults'][p] = row",
    "resolved_default_provider = out['defaultDeepProvider'] or out['defaultFastProvider'] or 'default'",
    "deep = ''",
    "fast = ''",
    "try:",
    "    deep = str(adapter.get_deep_model_default(resolved_default_provider) or '').strip()",
    "except Exception:",
    "    deep = ''",
    "try:",
    "    fast = str(adapter.get_fast_model_default(resolved_default_provider) or '').strip()",
    "except Exception:",
    "    fast = ''",
    "if deep and fast:",
    "    out['modelDefaults'][resolved_default_provider] = {'deep': deep, 'fast': fast}",
    "print(json.dumps(out))",
  ].join("\n");
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] reading adapter installer capabilities: adapter=${normalized} instance=${instanceId}`));
  }
  const res = python3Spawn(["-c", py], {
    encoding: "utf8",
    env: probeEnv,
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 15000,
  });
  if (res.error) {
    if (DEBUG_SETUP) {
      log.warn(`[step3_models] capability probe error for ${normalized}: ${String(res.error.message || res.error)}`);
    }
    return null;
  }
  if (res.status !== 0) {
    if (DEBUG_SETUP) {
      const stderr = String(res.stderr || "").trim();
      const stdout = String(res.stdout || "").trim();
      log.warn(
        `[step3_models] capability probe failed for ${normalized}: `
        + `${stderr || stdout || `exit ${String(res.status)}`}`
      );
    }
    return null;
  }
  try {
    const parsed = JSON.parse(String(res.stdout || "{}"));
    if (!parsed || typeof parsed !== "object") return null;
    if (DEBUG_SETUP) {
      const providers = Array.isArray(parsed.providers) ? parsed.providers.join(",") : "";
      log.info(C.dim(`[step3_models] capability probe complete: adapter=${normalized} providers=${providers || "(none)"}`));
    }
    return parsed;
  } catch {
    if (DEBUG_SETUP) {
      log.warn(`[step3_models] capability probe returned invalid JSON for ${normalized}`);
    }
    return null;
  }
}

function _runAdapterInstallerJson(adapterId, pyLines) {
  const normalized = String(adapterId || "").trim().toLowerCase();
  if (!normalized) return null;
  const instanceId = resolvedInstallerInstanceId(normalized);
  const helperEnv = {
    ...process.env,
    QUAID_HOME: WORKSPACE,
    QUAID_VISIBLE_HOME: VISIBLE_HOME,
    QUAID_WORKSPACE: WORKSPACE,
    QUAID_PYTHONPATH: path.join(__dirname, "modules", "quaid"),
    QUAID_ADAPTER_TYPE: normalized,
  };
  if (instanceId) helperEnv.QUAID_INSTANCE = instanceId;
  const res = python3Spawn(["-c", pyLines.join("\n")], {
    encoding: "utf8",
    env: helperEnv,
    stdio: ["pipe", "pipe", "pipe"],
  });
  if (res.error) {
    throw res.error;
  }
  if (res.status !== 0) {
    const stderr = String(res.stderr || "").trim();
    const stdout = String(res.stdout || "").trim();
    throw new Error(stderr || stdout || `adapter installer helper exited ${String(res.status)}`);
  }
  try {
    return JSON.parse(String(res.stdout || "{}"));
  } catch (err) {
    throw new Error(`adapter installer helper returned invalid JSON: ${err.message}`);
  }
}

function _reviewAdapterInstallerModelPair(adapterId, provider, deepModel, fastModel) {
  return _runAdapterInstallerJson(adapterId, [
    "import json, os, sys",
    "sys.path.insert(0, os.environ.get('QUAID_PYTHONPATH', ''))",
    "from lib.adapter import _instantiate_adapter_from_manifest",
    "aid = os.environ.get('QUAID_ADAPTER_TYPE', '').strip()",
    "adapter = _instantiate_adapter_from_manifest(aid)",
    `review = adapter.installer_review_model_pair(${JSON.stringify(String(provider || ""))}, ${JSON.stringify(String(deepModel || ""))}, ${JSON.stringify(String(fastModel || ""))})`,
    "if not isinstance(review, dict):",
    "    raise RuntimeError('installer_review_model_pair must return a dict')",
    "print(json.dumps(review))",
  ]);
}

function _validateAdapterInstallerModelPairLive(adapterId, provider, deepModel, fastModel) {
  return _runAdapterInstallerJson(adapterId, [
    "import json, os, sys",
    "sys.path.insert(0, os.environ.get('QUAID_PYTHONPATH', ''))",
    "from lib.adapter import _instantiate_adapter_from_manifest",
    "aid = os.environ.get('QUAID_ADAPTER_TYPE', '').strip()",
    "adapter = _instantiate_adapter_from_manifest(aid)",
    `result = adapter.installer_validate_model_pair_live(${JSON.stringify(String(provider || ""))}, ${JSON.stringify(String(deepModel || ""))}, ${JSON.stringify(String(fastModel || ""))})`,
    "if not isinstance(result, dict):",
    "    raise RuntimeError('installer_validate_model_pair_live must return a dict')",
    "print(json.dumps(result))",
  ]);
}

function _sharedModelOverride(adapterId) {
  const platformKey = String(adapterId || resolvedInstallerPlatform() || "").trim().toLowerCase();
  // Model/provider defaults are platform-specific by design.
  // Do not read model lanes from shared/config/global.
  const candidates = [
    path.join(WORKSPACE, "shared", "config", platformKey, "config.json"),
  ];
  for (const cfgPath of candidates) {
    if (!fs.existsSync(cfgPath)) continue;
    try {
      const raw = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const models = raw?.models || {};
      const provider = String(
        models.llm_provider || models.llmProvider || models.provider || ""
      ).trim().toLowerCase();
      const deep = String(
        models.deep_reasoning || models.deepReasoning || ""
      ).trim();
      const fast = String(
        models.fast_reasoning || models.fastReasoning || ""
      ).trim();
      if (!deep || !fast) continue;
      return { provider, deep, fast, source: cfgPath };
    } catch {
      // Ignore malformed shared files.
    }
  }
  return null;
}

function runAdapterInstallHook(adapterId, hookName) {
  const manifest = _adapterManifestById(adapterId);
  if (!manifest) return true;
  const scriptPath = resolveAdapterHookScript(manifest, hookName);
  if (!scriptPath || !fs.existsSync(scriptPath)) return true;

  const ext = path.extname(scriptPath).toLowerCase();
  let cmd = scriptPath;
  let args = [];
  if (ext === ".mjs" || ext === ".js" || ext === ".cjs") {
    cmd = process.execPath || "node";
    args = [scriptPath];
  } else if (ext === ".py") {
    cmd = python3Command();
    args = [scriptPath];
  }
  const res = spawnSync(cmd, args, {
    stdio: "inherit",
    env: {
      ...process.env,
      QUAID_HOME: WORKSPACE,
      QUAID_VISIBLE_HOME: VISIBLE_HOME,
      QUAID_WORKSPACE: WORKSPACE,
      QUAID_ADAPTER_ID: String(adapterId || ""),
      QUAID_ADAPTER_MANIFEST_PATH: String(manifest.__path || ""),
      QUAID_ADAPTER_REGISTRY_DIR: ADAPTER_REGISTRY_DIR,
      QUAID_ADAPTER_HOOK: String(hookName || ""),
    },
  });
  if (res.status !== 0) {
    throw new Error(`Adapter hook failed (${hookName}) for ${adapterId}: exit ${res.status}`);
  }
  return true;
}

/**
 * Returns the canonical visible projects directory.
 * Must be called after syncInstallerInstanceEnv() has run.
 */
function instanceProjectsDir() {
  return PROJECTS_DIR;
}
const OLLAMA_BASE_URL = (process.env.OLLAMA_URL || "http://localhost:11434")
  .replace(/\/v1\/?$/, "")
  .replace(/\/+$/, "");
const OLLAMA_TAGS_URL = `${OLLAMA_BASE_URL}/api/tags`;
const OLLAMA_PS_URL = `${OLLAMA_BASE_URL}/api/ps`;

// Mutable platform override — set by CLI/env override or interactive platform selection prompt.
// Allows the prompt to override IS_OPENCLAW / IS_CLAUDE_CODE after they're set.
let _platformOverride = FORCED_ADAPTER_TYPE;

// Mutable instance ID override — set by the instance ID prompt in step1().
// Takes precedence over the adapter-derived default.
let _instanceIdOverride = "";
// In agent mode, seed the override immediately from QUAID_INSTANCE env so that
// early module-level calls to syncInstallerInstanceEnv() (e.g. PY_ENV_SETUP)
// use the operator-specified instance rather than the platform default.
if (AGENT_MODE) {
  const _envInstance = String(process.env.QUAID_INSTANCE || "").trim();
  if (_envInstance) _instanceIdOverride = _envInstance;
}

function resolvedInstallerPlatform() {
  if (_platformOverride) return _platformOverride;
  // Infer platform from QUAID_INSTANCE prefix — set by the tester/env before
  // agent-driven install runs, and more reliable than binary detection when
  // multiple platforms coexist on the same host (e.g. alfie has openclaw,
  // codex, and claude on PATH simultaneously, making IS_OPENCLAW always true).
  const instanceId = (process.env.QUAID_INSTANCE || "").trim();
  if (instanceId.startsWith("codex-")) return "codex";
  if (instanceId.startsWith("claude-code-")) return "claude-code";
  if (IS_CLAUDE_CODE) return "claude-code";
  if (IS_OPENCLAW) return "openclaw";
  return "";
}

function resolvedInstallerInstanceId(adapterType = "") {
  if (_instanceIdOverride) return _instanceIdOverride;
  const platform = String(adapterType || resolvedInstallerPlatform()).trim();
  if (platform === "standalone") return "standalone";
  return "";
}

function syncInstallerInstanceEnv(adapterType = "") {
  const instance = resolvedInstallerInstanceId(adapterType);
  if (instance) process.env.QUAID_INSTANCE = instance;
  else delete process.env.QUAID_INSTANCE;
  return instance;
}

/**
 * List existing Quaid instance names by scanning WORKSPACE/instances for
 * directories that contain config.json.
 */
function listExistingInstances() {
  try {
    const instancesDir = HIDDEN_INSTANCES_DIR;
    if (!fs.existsSync(instancesDir)) return [];
    return fs.readdirSync(instancesDir)
      .filter(name => {
        if (name.startsWith(".")) return false;
        const cfgPath = path.join(instancesDir, name, "config.json");
        return fs.existsSync(cfgPath);
      })
      .sort();
  } catch { return []; }
}

function detectExistingInstallState() {
  const instances = listExistingInstances();
  const sharedGlobalConfig = path.join(WORKSPACE, "shared", "config", "global", "config.json");
  const sharedPlatformConfig = path.join(
    WORKSPACE,
    "shared",
    "config",
    String(resolvedInstallerPlatform() || "").trim().toLowerCase(),
    "config.json"
  );
  const legacyConfig = path.join(LEGACY_CONFIG_DIR, "config.json");
  const activeDb = hiddenInstanceDbPath();
  const legacyDb = path.join(WORKSPACE, "data", "memory.db");
  const pluginMarker = fs.existsSync(path.join(PLUGIN_DIR, "package.json"));
  const hasInstall = (
    instances.length > 0
    || fs.existsSync(sharedGlobalConfig)
    || fs.existsSync(sharedPlatformConfig)
    || fs.existsSync(legacyConfig)
    || fs.existsSync(activeDb)
    || fs.existsSync(legacyDb)
    || pluginMarker
  );
  return { hasInstall, instances };
}

function _existingInstallGuardMessage(installState) {
  const instances = Array.isArray(installState?.instances) ? installState.instances : [];
  const existingInstanceId = String(process.env.QUAID_INSTANCE || "").trim();
  const details = instances.length > 0
    ? `Existing instances: ${instances.join(", ")}`
    : "Existing Quaid files were detected on this host.";
  const requested = existingInstanceId
    ? `Requested instance: ${existingInstanceId}`
    : "Requested instance: (default installer instance)";
  const platform = String(resolvedInstallerPlatform() || "").trim().toLowerCase();
  const modeHint = platform === "openclaw"
    ? "Re-run with --add-instance to create another silo and bind the OpenClaw gateway env to it, or use --force to intentionally re-run the full install."
    : "Re-run with --add-instance to provision another silo, or use --force to intentionally re-run the full install."
  return [
    "Quaid is already installed on this host.",
    details,
    requested,
    "",
    "A second full install is blocked by default so the installer does not rewrite active host configuration accidentally.",
    modeHint,
  ].join("\n");
}

function resolveExistingOwnerIdentity() {
  const candidates = [];
  const instances = listExistingInstances();
  for (const instanceId of instances) {
    candidates.push(instanceConfigPath(instanceId));
  }
  const platformKey = String(resolvedInstallerPlatform() || "").trim().toLowerCase();
  if (platformKey) {
    candidates.push(path.join(WORKSPACE, "shared", "config", platformKey, "config.json"));
  }
  candidates.push(path.join(WORKSPACE, "shared", "config", "global", "config.json"));
  candidates.push(path.join(LEGACY_CONFIG_DIR, "config.json"));
  for (const cfgPath of candidates) {
    if (!fs.existsSync(cfgPath)) continue;
    try {
      const raw = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const users = raw?.users || {};
      const defaultOwner = String(users.defaultOwner || "").trim();
      const identity = users.identities?.[defaultOwner] || {};
      const display = String(
        identity.personNodeName
        || (Array.isArray(identity.speakers) ? identity.speakers[0] : "")
        || ""
      ).trim();
      if (defaultOwner) {
        return { display: display || defaultOwner, id: defaultOwner, source: cfgPath };
      }
    } catch {}
  }
  return null;
}

/**
 * Prompt the user to confirm or customise their Quaid instance ID.
 * Called once after the platform is known. Sets _instanceIdOverride.
 *
 * Default = adapter name (e.g. "claude-code"). User can override to any valid name.
 * Showing existing instances lets them opt-in to shared memory knowingly.
 */
async function promptInstanceId(adapterType) {
  if (AGENT_MODE || _testAnswers) {
    // Non-interactive: honor QUAID_INSTANCE env if explicitly provided by the operator
    // (e.g. a specific silo for a live test or agent install), otherwise use the
    // adapter-derived default.
    const envInstance = String(process.env.QUAID_INSTANCE || "").trim();
    if (envInstance && !_instanceIdOverride) {
      _instanceIdOverride = envInstance;
    }
    syncInstallerInstanceEnv(adapterType);
    return;
  }

  // If QUAID_INSTANCE is already set in the environment (e.g. re-install over
  // an existing setup, or set explicitly by the operator), honour it silently.
  const envInstance = String(process.env.QUAID_INSTANCE || "").trim();
  if (envInstance && !_instanceIdOverride) {
    _instanceIdOverride = envInstance;
    log.info(C.dim(`Instance ID: ${envInstance} (from QUAID_INSTANCE env — skipping prompt)`));
    return;
  }

  const defaultId = adapterType || resolvedInstallerPlatform();
  const existing = listExistingInstances();

  const INSTANCE_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;
  const RESERVED = new Set([
    "shared", "projects", "config", "data", "logs", "temp", "tmp",
    "quaid", "plugins", "lib", "core", "docs", "assets", "release",
    "scripts", "test", "tests", "benchmark", "node_modules",
  ]);

  log.message("");
  log.message(C.bold("Instance ID"));
  log.message(
    "Each Quaid install gets an instance ID — a short name for its memory silo.\n" +
    "Two installs with the " + C.bold("same") + " ID share memory. Different IDs = separate memory.\n" +
    "The ID becomes a pair of folders under Quaid:\n" +
    "  hidden state: " + C.dim(WORKSPACE + "/instances/<id>/") + "\n" +
    "  visible files: " + C.dim(VISIBLE_HOME + "/instances/<id>/")
  );

  if (existing.length > 0) {
    log.message("Existing instances: " + existing.map(n => C.cyan(n)).join("  "));
  } else {
    log.message(C.dim("No existing instances found under " + HIDDEN_INSTANCES_DIR));
  }

  if (adapterType === "claude-code") {
    log.message(
      C.dim("Claude Code instances are derived from the active project path on first hook use.\n" +
            "  Do not preseed claude-code-main or a global QUAID_INSTANCE during install.")
    );
  } else if (adapterType === "openclaw") {
    log.message(
      C.dim("OpenClaw: each agent can get its own ID for an independent memory and personality.")
    );
  }

  log.message("");

  const answer = handleCancel(await text({
    message: "Instance ID",
    placeholder: defaultId,
    initialValue: defaultId,
    validate(value) {
      const v = (value || "").trim() || defaultId;
      if (!INSTANCE_RE.test(v))
        return "Must start with a letter or digit, contain only [a-zA-Z0-9._-], max 64 chars.";
      if (RESERVED.has(v.toLowerCase()))
        return `'${v}' is reserved. Try something like '${adapterType}-personal'.`;
    },
  }));

  const chosen = (String(answer || "").trim()) || defaultId;
  _instanceIdOverride = chosen;
  syncInstallerInstanceEnv();

  if (!existing.includes(chosen)) {
    log.success(`Instance: ${C.cyan(chosen)} ${C.dim("(new silo)")}`);
  } else {
    log.info(`Instance: ${C.cyan(chosen)} ${C.dim("(existing — memory will be shared)")}`);
  }
  log.message("");
}

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

function _platformSupportsTimeoutCompaction(adapterType = "") {
  const platform = String(adapterType || resolvedInstallerPlatform() || "").trim().toLowerCase();
  // Timeout-triggered compaction is only supported on OpenClaw today.
  return platform === "openclaw";
}

function _platformUsesHostManagedLlmByDefault(adapterType = "") {
  const platform = String(adapterType || resolvedInstallerPlatform() || "").trim().toLowerCase();
  // Quaid now uses explicit provider auth for all launch platforms.
  return false;
}

function _installerPlatformLabel() {
  const platform = String(resolvedInstallerPlatform() || "").trim().toLowerCase();
  if (platform === "openclaw") return "OpenClaw";
  if (platform === "claude-code") return "Claude Code";
  if (platform === "codex") return "Codex";
  return "Standalone mode";
}

// Python env setup — always set canonical Quaid root, plus workspace hint.
const PY_ENV_SETUP =
  `os.environ['QUAID_HOME'] = ${JSON.stringify(WORKSPACE)}\n` +
  `os.environ['OPENCLAW_WORKSPACE'] = ${JSON.stringify(WORKSPACE)}\n` +
  `os.environ['QUAID_INSTANCE'] = ${JSON.stringify(syncInstallerInstanceEnv())}`;

// Step-specific quotes — each tied to the step's theme
const STEP_QUOTES = {
  preflight:  "Get ready for a surprise!",
  identity:   "If I'm not me, then who the hell am I?",
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
  "nomic-embed-text":    { dim: 768,  ramGB: 0.3, quality: "Best", rank: 1 },
  "qwen3-embedding:8b":  { dim: 4096, ramGB: 6,   quality: "High", rank: 2 },
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

function findPackageRootFrom(startPath, allowedNames = new Set(["openclaw"])) {
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
  const allowed = new Set(["openclaw"]);

  for (const cli of ["openclaw"]) {
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
      if (!entry.name.startsWith("openclaw")) continue;
      const dir = path.join(npmRoot, entry.name);
      const root = findPackageRootFrom(dir, allowed) || (fs.existsSync(path.join(dir, "package.json")) ? dir : null);
      if (root) roots.add(root);
    }
  }

  for (const dir of [
    path.join(os.homedir(), "openclaw"),
    path.join(os.homedir(), "openclaw-source"),
    "/opt/homebrew/lib/node_modules/openclaw",
    "/usr/local/lib/node_modules/openclaw",
    "/usr/lib/node_modules/openclaw",
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
  if (_isPlatform("openclaw")) {
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

const _noop = () => {};
const _activeInstallerSpinners = new Set();

function _pauseActiveSpinnersForInput() {
  const paused = [];
  for (const spin of Array.from(_activeInstallerSpinners)) {
    try {
      if (spin && typeof spin._pauseForPrompt === "function" && spin._pauseForPrompt()) {
        paused.push(spin);
      }
    } catch {
      // Best-effort only — input should still proceed.
    }
  }
  return paused;
}

function _resumePausedSpinners(paused) {
  for (const spin of paused) {
    try {
      if (spin && typeof spin._resumeAfterPrompt === "function") {
        spin._resumeAfterPrompt();
      }
    } catch {
      // Best-effort only — losing a spinner is less harmful than breaking input.
    }
  }
}

async function _withPausedSpinners(fn) {
  const paused = _pauseActiveSpinnersForInput();
  try {
    return await fn();
  } finally {
    _resumePausedSpinners(paused);
  }
}

function _makeManagedSpinner(factory) {
  return () => {
    const base = factory();
    let running = false;
    let paused = false;
    let lastMessage = null;

    const managed = {
      start(message) {
        if (running) {
          return undefined;
        }
        lastMessage = typeof message === "string" ? message : lastMessage;
        paused = false;
        running = true;
        _activeInstallerSpinners.add(managed);
        return base.start(message);
      },
      stop(message, code) {
        paused = false;
        running = false;
        _activeInstallerSpinners.delete(managed);
        return base.stop(message, code);
      },
      message(message) {
        lastMessage = typeof message === "string" ? message : lastMessage;
        if (running && typeof base.message === "function") {
          return base.message(message);
        }
        return undefined;
      },
      _pauseForPrompt() {
        if (!running) return false;
        running = false;
        paused = true;
        _activeInstallerSpinners.delete(managed);
        try {
          base.stop("Waiting for input...");
        } catch {
          // Ignore stop issues; prompt should still render.
        }
        return true;
      },
      _resumeAfterPrompt() {
        if (!paused || lastMessage === null) return;
        paused = false;
        running = true;
        _activeInstallerSpinners.add(managed);
        try {
          base.start(lastMessage);
        } catch {
          running = false;
          _activeInstallerSpinners.delete(managed);
        }
      },
    };

    return managed;
  };
}

const intro = SURVEY_ONLY ? _noop : _intro;
const outro = SURVEY_ONLY ? _noop : _outro;
const note = SURVEY_ONLY ? _noop : _note;
const cancel = _cancel;
const isCancel = _isCancel;
const log = SURVEY_ONLY
  ? {
      info: _noop,
      warn: _noop,
      error: _noop,
      success: _noop,
      message: _noop,
      step: _noop,
    }
  : _log;

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
    : async (opts) => _withPausedSpinners(() => _clack.select(opts));
const confirm = _testAnswers
  ? async (opts) => { const a = _nextAnswer("confirm", opts.message); log.info(C.dim(`[test] confirm "${opts.message}" → ${a}`)); return a; }
  : AGENT_MODE
    ? async (opts) => {
        const v = opts?.initialValue;
        const picked = v === undefined ? true : !!v;
        log.info(C.dim(`[agent] confirm "${opts.message}" → ${picked}`));
        return picked;
      }
    : async (opts) => _withPausedSpinners(() => _clack.confirm(opts));
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
    : async (opts) => _withPausedSpinners(() => _clack.text(opts));
function _emitInstallerStatus(kind, message, code) {
  const text = typeof message === "string" ? message.trim() : "";
  if (!text || SURVEY_ONLY) return;

  const hasRed = text.includes("\u001b[31m");
  const hasYellow = text.includes("\u001b[33m");
  const hasDim = text.includes("\u001b[2m");
  const looksWarning = /\b(failed|failure|could not|unavailable|skipped|needs attention)\b/i.test(text);

  if (kind === "start") {
    log.step(text);
    return;
  }
  if (kind === "message") {
    log.message(text);
    return;
  }
  if (code === 2 || hasRed) {
    log.error(text);
    return;
  }
  if (hasYellow || looksWarning) {
    log.warn(text);
    return;
  }
  if (hasDim) {
    log.message(text);
    return;
  }
  log.success(text);
}

const spinnerFactory = _testAnswers
  ? () => ({ start: (m) => log.info(C.dim(`[test] spinner: ${m}`)), stop: (m) => log.info(C.dim(`[test] done: ${m}`)), message: _noop })
  : SURVEY_ONLY
    ? () => ({ start: _noop, stop: _noop, message: _noop })
    : () => ({
        start: (m) => _emitInstallerStatus("start", m),
        stop: (m, code) => _emitInstallerStatus("stop", m, code),
        message: (m) => _emitInstallerStatus("message", m),
      });
const spinner = _makeManagedSpinner(spinnerFactory);

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

function _sleepMs(ms) {
  const waitMs = Number.isFinite(ms) ? Math.max(0, Math.floor(ms)) : 0;
  if (waitMs <= 0) return;
  runCliWithTimeout(
    process.execPath,
    ["-e", `setTimeout(() => process.exit(0), ${waitMs});`],
    waitMs + 2_000,
  );
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

/** Returns true if launchd has the gateway service registered (running or starting). */
function _gatewayRegisteredInLaunchd() {
  const lctl = spawnSync("launchctl", ["print", `gui/${process.getuid()}/ai.openclaw.gateway`], {
    encoding: "utf8", stdio: ["pipe", "pipe", "pipe"],
  });
  const out = (lctl.stdout || "") + (lctl.stderr || "");
  return out.includes("state =") || out.includes("pid =");
}

function _gatewayServiceLooksStopped(snapshot) {
  const text = `${snapshot.statusText}\n${snapshot.probeText}`.toLowerCase();
  return text.includes("not loaded")
    || text.includes("reachable: no")
    || text.includes("econnrefused")
    || text.includes("connect failed");
}

async function ensureGatewayReadyOrThrow(cli, context, timeoutMs = 12_000) {
  if (!_isPlatform("openclaw") || !cli) return;
  if (await waitForGatewayWarmup(timeoutMs)) return;

  // The gateway may have come up during the warmup window but just missed the
  // deadline — do a final extended HTTP wait (up to 60s extra) before running
  // the expensive snapshot + recovery logic. This avoids racing against a
  // slow launchd bootstrap or plugin-triggered restart.
  log.warn(`Gateway not ready after initial ${Math.round(timeoutMs / 1000)}s warmup during ${context}; waiting up to 60s more.`);
  if (await waitForGatewayWarmup(60_000)) return;

  let snapshot = _gatewayStatusSnapshot(cli);
  log.warn(`Gateway warmup failed during ${context}: ${_formatGatewaySnapshot(snapshot)}`);

  // One last HTTP health check after the expensive snapshot (which itself takes
  // up to 36s during which the gateway may have finished bootstrapping).
  if (_gatewayHttpCode("/health", "GET", null) === 200) return;

  const serviceInLaunchd = _gatewayRegisteredInLaunchd();
  if (_gatewayServiceLooksMissing(snapshot) && !serviceInLaunchd) {
    log.warn("Gateway service appears missing after restart; attempting service install recovery.");
    const installRes = runCliWithTimeout(cli, ["gateway", "install"], 30_000);
    if (installRes.status !== 0) {
      // gateway install may exit non-zero even when it succeeds (e.g. plist already exists
      // warning). Check HTTP health before treating the non-zero exit as fatal.
      if (await waitForGatewayWarmup(30_000)) return;
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
  } else if (serviceInLaunchd) {
    // Service is registered in launchd but HTTP not yet ready — still bootstrapping after
    // plugin registration triggered a restart.  Give it extra time; do not attempt recovery.
    log.warn(`Gateway not yet HTTP-ready during ${context}; launchd has it registered — waiting up to 60s.`);
    if (await waitForGatewayWarmup(60_000)) return;
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
  return python3Spawn(["-c", "import sqlite_vec"], { stdio: "pipe" }).status === 0;
}

function _installSqliteVec() {
  const attempts = [
    [python3Command(), ["-m", "pip", "install", "sqlite-vec"]],
    [python3Command(), ["-m", "pip", "install", "--user", "sqlite-vec"]],
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
    if (!parsed.agents || typeof parsed.agents !== "object") parsed.agents = {};
    parsed.agents.list = [
      {
        id: "main",
        default: true,
        name: "Default",
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

// ─── Instance Prefix Contract ─────────────────────────────────────────────────
// Every platform's gateway/config writer MUST enforce prefix ownership:
// only instance names that start with the platform's own prefix may be written
// into that platform's gateway default. Any foreign prefix is silently rejected.
//
// This prevents cross-contamination when multiple adapters are installed on the
// same machine (e.g. OC + CC + future platforms). Each adapter owns its prefix
// namespace exclusively:
//   openclaw  → "openclaw-*"
//   claude-code → "claude-code-*"   (derives instance from CLAUDE_PROJECT_DIR; no gateway default)
//   <future>  → "<platform>-*"
//
// When adding a new platform's gateway-default writer, always call
// _assertInstancePrefix(instanceId, "<platform>") before writing.
// ─────────────────────────────────────────────────────────────────────────────
function _assertInstancePrefix(instanceId, platformPrefix) {
  const id = String(instanceId || "").trim();
  return id.startsWith(platformPrefix);
}

function _ensureOpenClawRuntimeInstanceEnv(instanceId = "openclaw") {
  // ─── QUAID_INSTANCE Lifecycle ──────────────────────────────────────────────
  //
  // QUAID_INSTANCE has two resolution tiers:
  //
  //   1. GATEWAY DEFAULT (this code):
  //      Written to openclaw.json env.vars at install time. Used by the quaid
  //      plugin at gateway startup, before any per-agent session context exists.
  //      This is the "fallback identity" — it answers "which silo am I?" when
  //      no per-call override is present.
  //
  //   2. PER-CALL OVERRIDE (runtime):
  //      Each per-agent call injects its own QUAID_INSTANCE via buildPythonEnv()
  //      in the OC TS adapter. Python's _bootstrap_instance_env() uses setdefault
  //      semantics — it skips if QUAID_INSTANCE is already set in the process env.
  //      So the per-call value always wins over the gateway default.
  //
  // For Claude Code: QUAID_INSTANCE is derived from CLAUDE_PROJECT_DIR via
  // lib.instance.instance_slug_from_project_dir() — no gateway default needed.
  //
  // This value does NOT break per-agent isolation. It is only the fallback for
  // plugin startup and for calls that don't provide their own override.
  // ──────────────────────────────────────────────────────────────────────────
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed.env || typeof parsed.env !== "object" || Array.isArray(parsed.env)) {
      parsed.env = {};
    }
    if (!parsed.env.vars || typeof parsed.env.vars !== "object" || Array.isArray(parsed.env.vars)) {
      parsed.env.vars = {};
    }
    const nextInstance = String(instanceId || "").trim() || "openclaw";
    // Enforce prefix ownership: OC gateway only accepts openclaw-prefixed instances.
    if (!_assertInstancePrefix(nextInstance, "openclaw")) return false;
    const currentInstance = String(parsed.env.vars.QUAID_INSTANCE || "").trim();
    const currentHome = String(parsed.env.vars.QUAID_HOME || "").trim();
    const currentWorkspace = String(parsed.env.vars.OPENCLAW_WORKSPACE || "").trim();
    const currentInstanceTop = String(parsed.env.QUAID_INSTANCE || "").trim();
    const currentHomeTop = String(parsed.env.QUAID_HOME || "").trim();
    const currentWorkspaceTop = String(parsed.env.OPENCLAW_WORKSPACE || "").trim();
    if (
      currentInstance === nextInstance &&
      currentHome === WORKSPACE &&
      currentWorkspace === WORKSPACE &&
      currentInstanceTop === nextInstance &&
      currentHomeTop === WORKSPACE &&
      currentWorkspaceTop === WORKSPACE
    ) {
      return false;
    }
    // Write to env.vars (per-process env block read by the OC Python plugin layer)
    parsed.env.vars.QUAID_INSTANCE = nextInstance;
    parsed.env.vars.QUAID_HOME = WORKSPACE;
    parsed.env.vars.OPENCLAW_WORKSPACE = WORKSPACE;
    // Also write to top-level env keys (read by OC gateway for plugin startup env)
    parsed.env.QUAID_INSTANCE = nextInstance;
    parsed.env.QUAID_HOME = WORKSPACE;
    parsed.env.OPENCLAW_WORKSPACE = WORKSPACE;
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

function _resolveOpenClawGatewayEnvInstanceId(instanceId = "") {
  const _normalizeLabel = (value) => {
    const raw = String(value || "").trim().toLowerCase();
    if (!raw) return "";
    return raw.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  };
  const explicit = String(instanceId || "").trim();
  if (explicit) return explicit;
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const fromVars = String(parsed?.env?.vars?.QUAID_INSTANCE || "").trim();
    if (fromVars && fromVars !== "openclaw") return fromVars;
    const fromTop = String(parsed?.env?.QUAID_INSTANCE || "").trim();
    if (fromTop && fromTop !== "openclaw") return fromTop;

    const agentsList = Array.isArray(parsed?.agents?.list) ? parsed.agents.list : [];
    let selectedLabel = "";
    for (const agent of agentsList) {
      if (!agent || typeof agent !== "object") continue;
      const id = _normalizeLabel(agent.id);
      if (id === "main" || agent.default === true) {
        selectedLabel = id || "main";
        break;
      }
    }
    if (!selectedLabel && agentsList.length === 1) {
      selectedLabel = _normalizeLabel(agentsList[0]?.id);
    }
    if (!selectedLabel && parsed?.agents?.main && typeof parsed.agents.main === "object") {
      selectedLabel = "main";
    }
    if (selectedLabel) return `openclaw-${selectedLabel}`;
  } catch (err) {
    log.warn(`Could not read OpenClaw runtime env while reconciling gateway launch agent: ${String(err)}`);
  }
  return "openclaw-main";
}

function _runPlistBuddyCommand(plistPath, command) {
  return spawnSync("/usr/libexec/PlistBuddy", ["-c", command, plistPath], {
    encoding: "utf8",
    stdio: "pipe",
  });
}

function _ensureOpenClawGatewayLaunchAgentEnv(instanceId = "") {
  if (process.platform !== "darwin") return false;
  const plistPath = path.join(os.homedir(), "Library", "LaunchAgents", "ai.openclaw.gateway.plist");
  if (!fs.existsSync(plistPath)) {
    log.warn(`OpenClaw gateway launch agent plist not found; skipping env reconcile: ${plistPath}`);
    return false;
  }

  const resolvedInstance = _resolveOpenClawGatewayEnvInstanceId(instanceId);
  if (!_assertInstancePrefix(resolvedInstance, "openclaw")) {
    throw new Error(`OpenClaw gateway launch agent requires an openclaw-prefixed instance, got: ${resolvedInstance}`);
  }

  const expected = {
    QUAID_HOME: WORKSPACE,
    QUAID_VISIBLE_HOME: VISIBLE_HOME,
    OPENCLAW_WORKSPACE: WORKSPACE,
    QUAID_INSTANCE: resolvedInstance,
  };

  let changed = false;
  const ensureEnvDict = _runPlistBuddyCommand(plistPath, "Print :EnvironmentVariables");
  if (ensureEnvDict.status !== 0) {
    const addDict = _runPlistBuddyCommand(plistPath, "Add :EnvironmentVariables dict");
    if (addDict.status !== 0) {
      const detail = String(addDict.stderr || addDict.stdout || "").trim();
      throw new Error(`could not create EnvironmentVariables in ${plistPath}: ${detail || "unknown error"}`);
    }
    changed = true;
  }

  for (const [key, value] of Object.entries(expected)) {
    const current = _runPlistBuddyCommand(plistPath, `Print :EnvironmentVariables:${key}`);
    const currentValue = String(current.stdout || "").trim();
    if (current.status === 0 && currentValue === value) continue;
    let write = _runPlistBuddyCommand(plistPath, `Set :EnvironmentVariables:${key} ${JSON.stringify(value)}`);
    if (write.status !== 0) {
      write = _runPlistBuddyCommand(plistPath, `Add :EnvironmentVariables:${key} string ${JSON.stringify(value)}`);
    }
    if (write.status !== 0) {
      const detail = String(write.stderr || write.stdout || "").trim();
      throw new Error(`could not set EnvironmentVariables:${key} in ${plistPath}: ${detail || "unknown error"}`);
    }
    changed = true;
  }

  if (!changed) return false;

  const guiTarget = `gui/${process.getuid()}`;
  let reload = spawnSync("launchctl", ["bootout", guiTarget, plistPath], {
    encoding: "utf8",
    stdio: "pipe",
  });
  if (reload.status !== 0) {
    reload = spawnSync("launchctl", ["unload", plistPath], {
      encoding: "utf8",
      stdio: "pipe",
    });
  }

  reload = spawnSync("launchctl", ["bootstrap", guiTarget, plistPath], {
    encoding: "utf8",
    stdio: "pipe",
  });
  if (reload.status !== 0) {
    reload = spawnSync("launchctl", ["load", plistPath], {
      encoding: "utf8",
      stdio: "pipe",
    });
  }
  if (reload.status !== 0) {
    const detail = String(reload.stderr || reload.stdout || "").trim();
    throw new Error(`updated ${plistPath} but failed to reload ai.openclaw.gateway: ${detail || "unknown error"}`);
  }
  return true;
}

function _sanitizeOpenClawQuaidPluginEntry() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const tmpPath = `${cfgPath}.tmp-${process.pid}-${Date.now()}`;
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const entries = parsed?.plugins?.entries;
    const quaid = entries?.quaid;
    if (!quaid || typeof quaid !== "object") {
      return false;
    }
    const hasWorkspace = Object.prototype.hasOwnProperty.call(quaid, "workspace");
    const hasHooks = Object.prototype.hasOwnProperty.call(quaid, "hooks");
    if (!hasWorkspace && !hasHooks) return false;
    if (hasWorkspace) delete quaid.workspace;
    if (hasHooks) delete quaid.hooks;
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

const OPENCLAW_PLUGIN_STAGE_EXCLUDE_NAMES = new Set([
  ".git",
  ".pytest_cache",
  ".ruff_cache",
  ".tmp",
  "__pycache__",
  "coverage",
  "logs",
  "node_modules",
  "pytest-home",
  "tests",
]);

function _copyOpenClawPluginSource(srcDir, destDir) {
  const root = path.resolve(srcDir);
  fs.cpSync(srcDir, destDir, {
    recursive: true,
    dereference: true,
    filter: (source) => {
      const rel = path.relative(root, path.resolve(source));
      if (!rel) return true;
      const parts = rel.split(path.sep).filter(Boolean);
      return !parts.some((part) => OPENCLAW_PLUGIN_STAGE_EXCLUDE_NAMES.has(part));
    },
  });
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

function _ensureOpenClawDefaultAgentModel() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    return !!ensureOpenClawAgentModelDefault(cfgPath).changed;
  } catch {
    return false;
  }
}

function _sanitizeOpenClawNativeMemoryPlugins() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    return !!sanitizeOpenClawNativeMemoryPlugins(cfgPath).changed;
  } catch {
    return false;
  }
}

function _openClawManagedStatePath() {
  return path.join(WORKSPACE, "shared", "config", "openclaw", "managed-openclaw.json");
}

function _openClawManagedStateGuardScriptPath() {
  return path.join(WORKSPACE, "scripts", "openclaw-config-guard.mjs");
}

function _openClawManagedStateGuardLogDir() {
  return path.join(WORKSPACE, "logs", "openclaw");
}

function _captureOpenClawManagedState() {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    return captureOpenClawManagedState(cfgPath);
  } catch {
    return null;
  }
}

function _restoreOpenClawManagedState(snapshot) {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  try {
    return restoreOpenClawManagedState(cfgPath, snapshot);
  } catch {
    return { changed: false, reason: "restore-failed" };
  }
}

function _persistOpenClawManagedState(snapshot) {
  try {
    const snapshotPath = _openClawManagedStatePath();
    fs.mkdirSync(path.dirname(snapshotPath), { recursive: true });
    return writeOpenClawManagedStateSnapshot(snapshotPath, snapshot);
  } catch {
    return false;
  }
}

function _loadPersistedOpenClawManagedState() {
  try {
    return readOpenClawManagedStateSnapshot(_openClawManagedStatePath());
  } catch {
    return null;
  }
}

function _buildOpenClawManagedStateGuardScript() {
  const workspaceJson = JSON.stringify(WORKSPACE);
  return `#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";

const WORKSPACE = ${workspaceJson};
const CONFIG_PATH = path.join(os.homedir(), ".openclaw", "openclaw.json");
const SNAPSHOT_PATH = path.join(WORKSPACE, "shared", "config", "openclaw", "managed-openclaw.json");
const STATE_PATH = path.join(WORKSPACE, "shared", "config", "openclaw", "managed-openclaw.guard-state.json");
const GUI_TARGET = "gui/" + process.getuid();
const GATEWAY_SERVICE = GUI_TARGET + "/ai.openclaw.gateway";

function isRecord(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

function normalizeAllow(value) {
  return Array.isArray(value)
    ? value.map((entry) => String(entry || "").trim()).filter(Boolean)
    : [];
}

function readJson(filePath) {
  if (!fs.existsSync(filePath)) return null;
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJsonAtomically(filePath, value) {
  const tmpPath = \`\${filePath}.tmp-\${process.pid}-\${Date.now()}\`;
  try {
    fs.mkdirSync(path.dirname(filePath), { recursive: true });
    fs.writeFileSync(tmpPath, \`\${JSON.stringify(value, null, 2)}\\n\`, "utf8");
    fs.renameSync(tmpPath, filePath);
  } finally {
    try {
      if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath);
    } catch {}
  }
}

function ensureAllowId(plugins, pluginId, changedBits) {
  const allow = normalizeAllow(plugins.allow);
  if (allow.includes(pluginId)) return;
  allow.push(pluginId);
  plugins.allow = allow;
  changedBits.push(\`plugins.allow:\${pluginId}\`);
}

function sanitizeNativeMemoryPlugins(parsed, changedBits) {
  if (!isRecord(parsed.plugins)) parsed.plugins = {};
  const plugins = parsed.plugins;
  const nativeIds = ["active-memory", "memory-core", "memory-wiki"];
  const allow = normalizeAllow(plugins.allow);
  const nextAllow = allow.filter((entry) => !nativeIds.includes(entry));
  if (nextAllow.length !== allow.length) {
    plugins.allow = nextAllow;
    changedBits.push("plugins.allow:native-memory");
  }
  if (!isRecord(plugins.entries)) plugins.entries = {};
  for (const pluginId of nativeIds) {
    if (Object.prototype.hasOwnProperty.call(plugins.entries, pluginId)) {
      delete plugins.entries[pluginId];
      changedBits.push(\`plugins.entries.\${pluginId}\`);
    }
  }
  if (!isRecord(plugins.slots)) plugins.slots = {};
  if (String(plugins.slots.memory || "").trim() !== "quaid") {
    plugins.slots.memory = "quaid";
    changedBits.push("plugins.slots.memory");
  }
}

function reconcile(parsed, snapshot) {
  const changedBits = [];
  if (!isRecord(parsed.plugins)) parsed.plugins = {};
  const plugins = parsed.plugins;

  for (const pluginId of Array.isArray(snapshot.requiredAllow) ? snapshot.requiredAllow : []) {
    ensureAllowId(plugins, String(pluginId || "").trim(), changedBits);
  }

  if (isRecord(snapshot.entries)) {
    if (!isRecord(plugins.entries)) plugins.entries = {};
    for (const [pluginId, entry] of Object.entries(snapshot.entries)) {
      if (!isRecord(entry)) continue;
      const current = isRecord(plugins.entries[pluginId]) ? plugins.entries[pluginId] : null;
      const currentRaw = current ? JSON.stringify(current) : "";
      const nextRaw = JSON.stringify(entry);
      if (currentRaw !== nextRaw) {
        plugins.entries[pluginId] = cloneJson(entry);
        changedBits.push(\`plugins.entries.\${pluginId}\`);
      }
    }
  }

  if (isRecord(snapshot.channels) && isRecord(snapshot.channels.matrix)) {
    if (!isRecord(parsed.channels)) parsed.channels = {};
    const current = isRecord(parsed.channels.matrix) ? parsed.channels.matrix : null;
    const currentRaw = current ? JSON.stringify(current) : "";
    const nextRaw = JSON.stringify(snapshot.channels.matrix);
    if (currentRaw !== nextRaw) {
      parsed.channels.matrix = cloneJson(snapshot.channels.matrix);
      changedBits.push("channels.matrix");
    }
  }

  if (isRecord(snapshot.agents)) {
    const defaultPrimary = String(snapshot.agents.defaultPrimary || "").trim();
    if (defaultPrimary) {
      if (!isRecord(parsed.agents)) parsed.agents = {};
      if (!isRecord(parsed.agents.defaults)) parsed.agents.defaults = {};
      if (!isRecord(parsed.agents.defaults.model)) parsed.agents.defaults.model = {};
      if (String(parsed.agents.defaults.model.primary || "").trim() !== defaultPrimary) {
        parsed.agents.defaults.model.primary = defaultPrimary;
        changedBits.push("agents.defaults.model.primary");
      }
    }

    const listPrimaries = isRecord(snapshot.agents.listPrimaries) ? snapshot.agents.listPrimaries : {};
    if (!isRecord(parsed.agents)) parsed.agents = {};
    if (!Array.isArray(parsed.agents.list)) parsed.agents.list = [];
    for (const agent of parsed.agents.list) {
      if (!isRecord(agent)) continue;
      const agentId = String(agent.id || "").trim();
      const desired = String(listPrimaries[agentId] || "").trim();
      if (!agentId || !desired) continue;
      if (!isRecord(agent.model)) agent.model = {};
      if (String(agent.model.primary || "").trim() !== desired) {
        agent.model.primary = desired;
        changedBits.push(\`agents.list.\${agentId}.model.primary\`);
      }
    }
  }

  sanitizeNativeMemoryPlugins(parsed, changedBits);
  return changedBits;
}

const snapshot = readJson(SNAPSHOT_PATH);
const parsed = readJson(CONFIG_PATH);
if (!snapshot || !parsed) process.exit(0);

const changedBits = reconcile(parsed, snapshot);
if (changedBits.length === 0) process.exit(0);

writeJsonAtomically(CONFIG_PATH, parsed);

const now = Date.now();
const state = readJson(STATE_PATH) || {};
const lastRestartMs = Number(state.lastRestartMs || 0);
const restartCooldownMs = 15000;
if (now - lastRestartMs >= restartCooldownMs) {
  let restart = spawnSync("launchctl", ["kickstart", "-k", GATEWAY_SERVICE], { stdio: "pipe" });
  let restartMethod = "launchctl";
  if (restart.status !== 0) {
    restart = spawnSync("openclaw", ["gateway", "restart"], { stdio: "pipe" });
    restartMethod = "openclaw";
  }
  state.lastRestartMs = now;
  state.lastRestartStatus = Number(restart.status || 0);
  state.lastRestartMethod = restartMethod;
  state.lastRestartDetail = String(restart.stderr || restart.stdout || "").trim();
  state.lastChangedBits = changedBits;
  writeJsonAtomically(STATE_PATH, state);
} else {
  state.lastChangedBits = changedBits;
  writeJsonAtomically(STATE_PATH, state);
}
`;
}

function _installOpenClawManagedStateGuard() {
  if (!_isPlatform("openclaw")) return false;
  if (process.platform !== "darwin") return false;

  const scriptPath = _openClawManagedStateGuardScriptPath();
  const logDir = _openClawManagedStateGuardLogDir();
  const plistPath = path.join(os.homedir(), "Library", "LaunchAgents", "ai.openclaw.quaid-config-guard.plist");
  const outPath = path.join(logDir, "config-guard.log");
  const errPath = path.join(logDir, "config-guard.err.log");
  const guiTarget = `gui/${process.getuid()}`;
  const serviceTarget = `${guiTarget}/ai.openclaw.quaid-config-guard`;
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>ai.openclaw.quaid-config-guard</string>
  <key>ProgramArguments</key>
  <array>
    <string>${process.execPath}</string>
    <string>${scriptPath}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key>
    <string>${os.homedir()}</string>
    <key>PATH</key>
    <string>${process.env.PATH || "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"}</string>
    <key>QUAID_HOME</key>
    <string>${WORKSPACE}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>30</integer>
  <key>WatchPaths</key>
  <array>
    <string>${path.join(os.homedir(), ".openclaw", "openclaw.json")}</string>
  </array>
  <key>StandardOutPath</key>
  <string>${outPath}</string>
  <key>StandardErrorPath</key>
  <string>${errPath}</string>
</dict>
</plist>
`;

  try {
    fs.mkdirSync(path.dirname(scriptPath), { recursive: true });
    fs.mkdirSync(logDir, { recursive: true });
    fs.mkdirSync(path.dirname(plistPath), { recursive: true });
    fs.writeFileSync(scriptPath, _buildOpenClawManagedStateGuardScript(), { encoding: "utf8", mode: 0o755 });
    fs.chmodSync(scriptPath, 0o755);
    if (fs.existsSync(plistPath)) {
      spawnSync("launchctl", ["bootout", guiTarget, plistPath], { stdio: "pipe" });
      spawnSync("launchctl", ["unload", plistPath], { stdio: "pipe" });
    }
    fs.writeFileSync(plistPath, plist, "utf8");
    spawnSync("launchctl", ["enable", serviceTarget], { stdio: "pipe" });
    let load = spawnSync("launchctl", ["bootstrap", guiTarget, plistPath], { stdio: "pipe" });
    if (load.status !== 0) {
      load = spawnSync("launchctl", ["load", plistPath], { stdio: "pipe" });
    }
    if (load.status !== 0) {
      const detail = String(load.stderr || load.stdout || "").trim();
      log.warn(`failed to install OpenClaw config guard: ${detail || "unknown error"}`);
      return false;
    }
    const kick = spawnSync("launchctl", ["kickstart", "-k", serviceTarget], { stdio: "pipe" });
    const probe = spawnSync("launchctl", ["print", serviceTarget], { stdio: "pipe" });
    if (probe.status !== 0) {
      const detail = String(probe.stderr || probe.stdout || kick.stderr || kick.stdout || "").trim();
      log.warn(`OpenClaw config guard failed to register in launchctl: ${detail || "unknown error"}`);
      return false;
    }
    return true;
  } catch (err) {
    log.warn(`failed to install OpenClaw config guard: ${String(err?.message || err)}`);
    return false;
  }
}

async function _reassertOpenClawPostRestartState(context = "gateway restart", managedSnapshot = null) {
  if (!_isPlatform("openclaw")) return false;
  const cli = canRun("openclaw") ? "openclaw" : "";
  if (!cli) return false;

  const changedBits = [];
  const stateRestore = _restoreOpenClawManagedState(managedSnapshot);
  if (stateRestore.changed) changedBits.push(...(stateRestore.changedBits || []));
  if (_ensureOpenClawPluginsAllowQuaid()) changedBits.push("plugins.allow");
  if (_sanitizeOpenClawNativeMemoryPlugins()) changedBits.push("native-memory-plugins");
  if (_sanitizeOpenClawQuaidPluginEntry()) changedBits.push("plugins.entries.quaid");

  if (changedBits.length === 0) return false;

  log.info(
    `Re-sanitized OpenClaw config after ${context}: ${changedBits.join(", ")}. Restarting gateway to apply changes.`,
  );
  const restartRes = runCliWithTimeout(cli, ["gateway", "restart"], 30_000);
  if (restartRes.status !== 0) {
    const msg = renderCliFailure(restartRes, 30_000);
    log.warn(`gateway restart after ${context} re-sanitize exited non-zero: ${msg || "unknown"}`);
  }
  await waitForGatewayWarmup(30_000);
  return true;
}

function _registerOpenClawQuaidPlugin(pluginPath) {
  const cli = canRun("openclaw") ? "openclaw" : "";
  if (!cli) return { ok: false, reason: "OpenClaw CLI not found" };
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
    // Stage runtime source only. Copying the full dev tree includes node_modules
    // and local caches that can exceed 1GB and make M0 appear hung before the
    // direct install-record bypass has a chance to run.
    log.info("Staging OpenClaw plugin runtime source (excluding generated caches/node_modules)...");
    _copyOpenClawPluginSource(pluginPath, stagedPluginPath);
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

  // Pre-clean stale extension/config before direct repair. Avoid OpenClaw plugin
  // CLI repair calls here; current beta CLIs can hang or emit "Plugin not found"
  // before the direct install-record bypass has a chance to run.
  removeStaleExtensionDir();
  _sanitizeOpenClawPluginInstallSources();
  // OpenClaw plugin discovery reads Dirent.isDirectory() and does not follow
  // symlinked extension directories. Keep a real directory at extensionDir.
  try {
    fs.mkdirSync(path.dirname(extensionDir), { recursive: true });
    fs.rmSync(extensionDir, { recursive: true, force: true });
    log.info("Provisioning OpenClaw extension directory...");
    _copyOpenClawPluginSource(stagedPluginPath, extensionDir);
  } catch (err) {
    return { ok: false, reason: `failed to provision extension directory: ${String(err)}` };
  }
  log.info("Provisioning OpenClaw extension runtime dependencies...");
  const depsResult = ensureOpenClawExtensionDependencies({
    extensionDir,
    pluginDir: pluginPath,
  });
  if (!depsResult.ok) {
    return { ok: false, reason: `failed to provision plugin dependencies: ${depsResult.reason}` };
  }
  {
    // sourcePath must be in the macOS secure temp dir (/var/folders/) for the OC
    // gateway to accept the install record. Use TMPDIR env rather than os.tmpdir()
    // which may resolve to /private/tmp in some environments.
    const secureTmpBase = process.env.TMPDIR || os.tmpdir();
    const secureSourcePath = path.join(secureTmpBase, `quaid-plugin-stage-${process.pid}-${Date.now()}`);
    let pluginVersion = "0.0.0";
    try {
      const pkgRaw = fs.readFileSync(path.join(stagedPluginPath, "package.json"), "utf8");
      pluginVersion = JSON.parse(pkgRaw).version || pluginVersion;
    } catch {}
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    const tmpPath = `${cfgPath}.tmp-install-${process.pid}-${Date.now()}`;
    try {
      const raw = fs.existsSync(cfgPath) ? fs.readFileSync(cfgPath, "utf8") : "{}";
      const parsed = JSON.parse(raw);
      const plugins = parsed.plugins || (parsed.plugins = {});
      const installs = plugins.installs || (plugins.installs = {});
      const installedAt = new Date().toISOString();
      installs.quaid = {
        source: "path",
        sourcePath: secureSourcePath,
        installPath: extensionDir,
        version: pluginVersion,
        installedAt,
      };
      fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
      fs.renameSync(tmpPath, cfgPath);
      log.info("Registered quaid plugin via direct install record write.");
    } catch (err) {
      try { if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath); } catch {}
      return { ok: false, reason: `failed to write plugin install record: ${String(err)}` };
    }
  }

  // Fresh installs can fail plugin enable if the trust list was cleared during
  // stale-state cleanup and not restored before enable.
  _ensureOpenClawPluginsAllowQuaid();

  // Enable the plugin by directly editing the config JSON instead of using the CLI.
  // The CLI's sha256 safety check races with the running gateway writing to openclaw.json,
  // causing "Config overwrite" errors that cannot be resolved by stopping the gateway
  // (LaunchAgent KeepAlive restarts it immediately).
  {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    const tmpPath = `${cfgPath}.tmp-enable-${process.pid}-${Date.now()}`;
    try {
      const raw = fs.readFileSync(cfgPath, "utf8");
      const parsed = JSON.parse(raw);
      const plugins = parsed.plugins || (parsed.plugins = {});
      const entries = plugins.entries || (plugins.entries = {});
      const quaid = entries.quaid || (entries.quaid = {});
      quaid.enabled = true;
      const slots = plugins.slots || (plugins.slots = {});
      slots.memory = "quaid";
      fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
      fs.renameSync(tmpPath, cfgPath);
      log.info("Enabled quaid plugin via direct config write (bypassed CLI sha256 race).");
    } catch (err) {
      try { if (fs.existsSync(tmpPath)) fs.unlinkSync(tmpPath); } catch {}
      return { ok: false, reason: `plugins enable (direct) failed: ${String(err)}` };
    }
  }

  // Verify the plugin is in the config (direct read, no CLI — avoids preflight race
  // where the CLI loads the plugin and its adapter fails to find runtime files
  // from the extension copy before the gateway has restarted).
  {
    const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
    try {
      const raw = fs.readFileSync(cfgPath, "utf8");
      const parsed = JSON.parse(raw);
      const enabled = parsed?.plugins?.entries?.quaid?.enabled;
      if (!enabled) {
        return { ok: false, reason: "quaid plugin not enabled in config after direct write" };
      }
    } catch (err) {
      return { ok: false, reason: `could not verify plugin config: ${String(err)}` };
    }
  }

  // Restart gateway to pick up the plugin config change.
  // Note: on some hosts (e.g. SSH + Aqua LaunchAgent) `openclaw gateway restart`
  // exits non-zero even when the restart succeeds. Don't hard-fail here — the
  // caller runs ensureGatewayReadyOrThrow immediately after, which is the real
  // health gate.
  const restartRes = runCliWithTimeout(cli, ["gateway", "restart"], 30_000);
  if (restartRes.status !== 0) {
    const msg = renderCliFailure(restartRes, 30_000);
    log.warn(`gateway restart exited non-zero (will verify health next): ${msg || "unknown"}`);
  }

  try {
    fs.rmSync(stagedPluginPath, { recursive: true, force: true });
  } catch {}

  return { ok: true, reason: "" };
}

function _readOpenClawPluginState(options = {}) {
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  const extensionDir = path.join(os.homedir(), ".openclaw", "extensions", "quaid");
  const cli = canRun("openclaw") ? "openclaw" : "";
  const skipPluginList = !!options.skipPluginList;
  let pluginEnabled = false;
  let memorySlotBound = false;
  let installPath = "";
  let pluginListed = false;
  let pluginListCheckOk = false;
  try {
    if (fs.existsSync(cfgPath)) {
      const parsed = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
      const plugins = parsed?.plugins;
      pluginEnabled = !!plugins?.entries?.quaid?.enabled;
      memorySlotBound = String(plugins?.slots?.memory || "").trim() === "quaid";
      installPath = String(plugins?.installs?.quaid?.installPath || "").trim();
    }
  } catch {}
  if (cli && !skipPluginList) {
    const listAttempts = 3;
    const listTimeoutMs = 60_000;
    const listRetryDelayMs = 5_000;
    for (let attempt = 1; attempt <= listAttempts; attempt += 1) {
      try {
        const listRes = runCliWithTimeout(cli, ["plugins", "list"], listTimeoutMs);
        const listText = `${_safeTrim(listRes.stdout)}\n${_safeTrim(listRes.stderr)}`.trim().toLowerCase();
        const discovered = /(^|[^a-z0-9_-])quaid([^a-z0-9_-]|$)/m.test(listText);
        if (listRes.status === 0 || discovered) {
          pluginListCheckOk = true;
          pluginListed = discovered;
          break;
        }
      } catch {}
      if (attempt < listAttempts) _sleepMs(listRetryDelayMs);
    }
  }
  return {
    extensionDir,
    extensionExists: fs.existsSync(extensionDir),
    pluginEnabled,
    memorySlotBound,
    installPath,
    installPathExists: !!installPath && fs.existsSync(installPath),
    pluginListCheckOk,
    pluginListed,
  };
}

function _openClawPluginDirectRegistrationOk(state) {
  return !!(
    state
    && state.extensionExists
    && state.pluginEnabled
    && state.memorySlotBound
    && state.installPathExists
  );
}

function _warnOpenClawPluginListDiagnostic(state, context = "validation") {
  if (!state || (state.pluginListCheckOk && state.pluginListed)) return;
  log.warn(
    `OpenClaw plugins list did not report quaid during ${context}; `
    + "continuing because direct registration checks passed "
    + `(pluginListCheckOk=${!!state.pluginListCheckOk}, pluginListed=${!!state.pluginListed}).`
  );
}

function _ensureOpenClawPluginRegistered(pluginPath) {
  const state = _readOpenClawPluginState({ skipPluginList: true });
  if (_openClawPluginDirectRegistrationOk(state)) {
    const listState = _readOpenClawPluginState();
    if (listState.pluginListCheckOk && listState.pluginListed) {
      return { ok: true, reason: "", repaired: false };
    }
  }
  const reg = _registerOpenClawQuaidPlugin(pluginPath);
  return { ...reg, repaired: true };
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
  await _withPausedSpinners(async () => {
    log.message(C.dim(msg));
    if (process.stdin.isTTY) {
      process.stdin.setRawMode(true);
      process.stdin.resume();
      await new Promise((resolve) => process.stdin.once("data", resolve));
      process.stdin.setRawMode(false);
      process.stdin.pause();
    }
  });
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
  if (SURVEY_ONLY) return;
  const lines = renderQuaidBanner(C, {
    subtitle: "by Solomon Steadman",
    footerRight: `v${VERSION}`,
    title: " LONG-TERM MEMORY SYSTEM ",
  });
  console.log(lines.join("\n"));
}

function stepHeader(num, total, title, quote) {
  if (SURVEY_ONLY) return;
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

  _refreshAdapterManifests();
  log.info(C.dim(`Quaid system home: ${WORKSPACE}`));
  log.info(C.dim(`Quaid visible home: ${VISIBLE_HOME}`));

  // Snapshot existing files BEFORE any openclaw commands — those commands load
  // the quaid plugin which creates data/memory.db, giving a false "dirty" signal.
  const _existingFiles = [
    "SOUL.md",
    "USER.md",
    "ENVIRONMENT.md",
    "TOOLS.md",
    "AGENTS.md",
    "IDENTITY.md",
    "HEARTBEAT.md",
    "TODO.md",
  ]
    .filter(f => fs.existsSync(path.join(VISIBLE_HOME, f)));
  const _hasConfig = fs.existsSync(path.join(LEGACY_CONFIG_DIR, "config.json"));
  const _hasDb = fs.existsSync(hiddenInstanceDbPath());

  const s = spinner();

  if (INSTALL_ALL_PLATFORMS && !_platformOverride && _chainedPlatformQueue.length === 0) {
    const installableAdapterOptions = _adapterOptionsForSelect().map((opt) => {
      const installState = _readAdapterInstallState(opt.value);
      return {
        ...opt,
        disabled: installState.status !== "can_install",
      };
    }).filter((opt) => !opt.disabled);
    const [firstAdapter, ...queuedAdapters] = installableAdapterOptions.map((opt) => opt.value);
    if (!firstAdapter) {
      bail("No installable platforms were detected on this system.");
    }
    _beginChainedPlatformInstall(firstAdapter, queuedAdapters);
  }

  // Platform selection — ask here so platform-specific preflight runs with the right target.
  if (!AGENT_MODE && !FORCED_ADAPTER_TYPE && !_platformOverride) {
    const adapterOptions = _adapterOptionsForSelect().map((opt) => {
      const installState = _readAdapterInstallState(opt.value);
      return {
        ...opt,
        hint: C.dim(_formatAdapterInstallHint(installState.status, opt.hint, installState.reason)),
        disabled: installState.status !== "can_install",
      };
    });
    const installableAdapterOptions = adapterOptions.filter((opt) => !opt.disabled);
    const firstSelectable = installableAdapterOptions[0];
    if (!firstSelectable) {
      bail("No installable platforms were detected on this system.");
    }
    let platform = "";
    if (INSTALL_ALL_PLATFORMS) {
      platform = resolvedInstallerPlatform() ? "" : "__install_all__";
    } else {
      platform = handleCancel(await select({
        message: "Which platform are you installing for?",
        initialValue: firstSelectable?.value || "claude-code",
        options: [
          ...(installableAdapterOptions.length > 1 ? [{
            value: "__install_all__",
            label: "Install All Available",
            hint: `Install ${installableAdapterOptions.map((opt) => opt.label).join(", ")} in sequence`,
          }] : []),
          ...adapterOptions,
        ],
      }));
    }
    if (platform === "__install_all__") {
      const [firstAdapter, ...queuedAdapters] = installableAdapterOptions.map((opt) => opt.value);
      _beginChainedPlatformInstall(firstAdapter, queuedAdapters);
    } else {
      _platformOverride = platform;
      syncInstallerInstanceEnv();

      // Show platform-specific compatibility warnings
      const warnings = _adapterCompatibilityWarnings(platform);
      if (warnings.length > 0) {
        for (const msg of warnings) {
          log.warn(`  ${msg}`);
        }
      }
    }

    s.start(`Checking ${resolvedInstallerPlatform() || platform} environment...`);
  } else {
    s.start(`Checking ${resolvedInstallerPlatform() || "installer"} environment...`);
  }

  _ensureOpenClawHostConfigLock();

  const installState = detectExistingInstallState();
  _existingInstallDetected = !!installState.hasInstall;
  if (_existingInstallDetected) {
    if (!ALLOW_EXISTING_INSTALL && !_chainedPlatformInstall) {
      bail(_existingInstallGuardMessage(installState));
    }
    const details = installState.instances.length > 0
      ? ` (${installState.instances.length} existing instance${installState.instances.length === 1 ? "" : "s"})`
      : "";
    log.info(C.dim(`Existing Quaid install detected${details}. First-install-only setup will be skipped.`));
    if (ADD_INSTANCE_MODE) {
      log.info(C.dim("Add-instance mode enabled: installer will provision the new silo without rewriting the OpenClaw fallback instance."));
    } else if (_chainedPlatformInstall) {
      log.info(C.dim("Additional-platform install enabled: reusing the existing Quaid home while adding another host integration."));
    } else if (FORCE_INSTALL) {
      log.warn("Force mode enabled: installer is allowed to re-run against an existing host install.");
    }
  }

  // External adapter hooks can perform preflight checks or env bootstrap.
  s.message(`Running ${resolvedInstallerPlatform() || "platform"} preflight...`);
  runAdapterInstallHook(resolvedInstallerPlatform(), "preinstall");

  const sharedAuthTokenPath = sharedAuthRegistryPath(WORKSPACE);
  const sharedCredentialKinds = allSharedAuthKinds();
  const existingSharedCredential = getSharedAuthCredential(WORKSPACE, sharedCredentialKinds);
  const existingSharedToken = existingSharedCredential?.token || "";
  const sharedEnvToken = String([
    process.env.ANTHROPIC_API_KEY || "",
    process.env.OPENAI_OAUTH_TOKEN || "",
    process.env.OPENAI_API_KEY || "",
    process.env.CLAUDE_CODE_OAUTH_TOKEN || "",
  ].find(Boolean) || "").trim();
  const hasSharedCredential = !!(existingSharedToken || sharedEnvToken);

  if (!_existingInstallDetected) {
    if (!AGENT_MODE) {
      const tokenAction = hasSharedCredential
        ? handleCancel(await select({
            message: "Quaid shared provider credential:",
            options: [
              ...(existingSharedToken || sharedEnvToken ? [{ value: "keep", label: "Use current credential" }] : []),
              { value: "reset", label: "Enter a new credential" },
            ],
          }))
        : "reset";

      if (tokenAction === "reset") {
        _sharedCredentialSelection = "reset";
        log.info(C.dim("Use one global shared credential for Quaid background calls."));
        log.info(C.dim("Recommended: Anthropic OAuth token from `claude setup-token`."));
        log.info(C.dim("OpenAI-backed lanes are experimental and benchmark lower than Anthropic."));
        log.info(C.dim("Paste the credential below (canonical registry: " + sharedAuthTokenPath + ")"));
        const newToken = handleCancel(await text({
          message: "Shared Quaid credential:",
          placeholder: "paste token here",
          validate: (v) => (!v || !v.trim()) ? "Credential is required." : undefined,
        }));
        if (newToken && newToken.trim()) {
          const kind = inferSharedAuthKind(newToken.trim()) || "anthropic_oauth";
          if (!DRY_RUN) {
            writeSharedAuthCredential(WORKSPACE, kind, newToken.trim());
            log.success(`Credential stored in shared registry as ${authKindPromptLabel(kind)} at ${sharedAuthTokenPath}`);
          } else {
            log.info(C.dim(`(dry run) Would store ${authKindPromptLabel(kind)} at ${sharedAuthTokenPath}`));
          }
        }
      }
      if (tokenAction === "keep") {
        _sharedCredentialSelection = "keep";
      }
    } else if (!hasSharedCredential) {
      if (DRY_RUN) {
        log.warn("(dry run) No shared auth credential — would print out-of-band instructions and exit in real run.");
      } else {
        note(
          [
            "Quaid needs one shared provider credential for background LLM calls.",
            "",
            "IMPORTANT: Do NOT paste the credential into this conversation.",
            "",
            "Ask the user to complete these steps in a NEW terminal window:",
            "",
            "  Preferred: claude setup-token",
            "  Then register it:",
            `    quaid auth refresh --kind anthropic_oauth 'YOUR_TOKEN_HERE'`,
            "  or",
            `    quaid auth refresh --kind anthropic_api 'YOUR_TOKEN_HERE'`,
            "",
            "  Experimental alternatives:",
            `    quaid auth refresh --kind codex_oauth 'YOUR_TOKEN_HERE'`,
            "  or",
            `    quaid auth refresh --kind openai_api 'YOUR_TOKEN_HERE'`,
            "",
            "Then re-run the installer.",
            "",
            `Registry path: ${sharedAuthTokenPath}`,
          ].join("\n"),
          "Shared Auth Credential Required — Action Needed"
        );
        bail("Install incomplete: shared auth credential not found. Register it and re-run.");
      }
    }
  }

  if (_isPlatform("claude-code")) {
    // --- Claude Code mode ---
    s.message("Checking Claude Code...");
    const hasClaude = canRun("claude");
    if (!hasClaude) {
      s.stop(C.yellow("Claude Code CLI not found"), 2);
      log.warn("Install Claude Code: https://docs.anthropic.com/en/docs/claude-code");
      log.warn("Continuing anyway — CLI is needed at runtime, not install time.");
    }
    if (!DRY_RUN) {
      fs.mkdirSync(WORKSPACE, { recursive: true });
    }
  } else if (_isPlatform("openclaw")) {
    // --- OpenClaw installed ---
    s.message("Scanning for OpenClaw...");
    if (!canRun("openclaw")) {
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
    s.message("Checking OpenClaw gateway status...");
    const gatewayHealthCode = _gatewayHttpCode("/health", "GET", null);
    if (gatewayHealthCode !== 200) {
      s.stop(C.red("Gateway offline"), 2);
      note(
        "OpenClaw gateway must be running before installing Quaid.\n\n" +
        "Start it with:\n" +
        "  openclaw gateway\n\n" +
        "Or start it via launchd, then retry the Quaid installer.",
        "Gateway offline"
      );
      bail("OpenClaw gateway must be running before installing Quaid.");
    }

    // --- Onboarding / agents list ---
    const cfgCli = "openclaw";
    if (!_preinstallOpenClawManagedState) {
      _preinstallOpenClawManagedState = _captureOpenClawManagedState();
    }
    s.message("Checking OpenClaw agent configuration...");
    let hasAgent = _readAgentsList(cfgCli).some((a) => a && typeof a === "object" && a.id);
    if (!hasAgent) {
      hasAgent = _ensureAgentsList(cfgCli, detectWorkspaceFromCli());
    }
    const _ocRuntimeInstance = resolvedInstallerInstanceId();
    const runtimeEnvChanged = _ocRuntimeInstance
      ? _ensureOpenClawRuntimeInstanceEnv(_ocRuntimeInstance)
      : false;
    const agentModelChanged = _ensureOpenClawDefaultAgentModel();
    const responsesEndpointChanged = _ensureOpenClawResponsesEndpoint();
    if (responsesEndpointChanged || agentModelChanged || runtimeEnvChanged) {
      s.message("Restarting OpenClaw gateway...");
      const restart = spawnSync(cfgCli, ["gateway", "restart"], { encoding: "utf8", stdio: "pipe" });
      if (restart.status !== 0) {
        const detail = String(restart.stderr || restart.stdout || "").trim();
        log.warn(`OpenClaw gateway restart during preflight exited non-zero (will verify health next): ${detail || "unknown"}`);
      }
      s.message("Waiting for gateway to come online...");
      await waitForGatewayWarmup(30_000);
      await _reassertOpenClawPostRestartState("preflight config reconcile", _preinstallOpenClawManagedState);
    }
    _ensureOpenClawCompactionModeDefault();
    s.stop(C.green("OpenClaw") + " gateway running");
  } else {
    // --- Non-OpenClaw installs: ensure workspace directory exists ---
    s.message("Checking workspace directory...");
    fs.mkdirSync(WORKSPACE, { recursive: true });
    s.stop(C.green(_installerPlatformLabel()) + C.dim(` — workspace: ${WORKSPACE}`));
  }

  // --- Python ---
  s.start("Checking Python 3.10+...");
  const resolvedPython = resolvePython3Binary();
  if (!resolvedPython) {
    s.stop(C.red("Python 3 not found"), 2);
    const installed = await tryBrewInstall("python@3.12", "Python 3.12");
    if (!installed) bail("Python 3.10+ is required.");
    s.start("Rechecking Python...");
  }
  const pyVer = String(python3Spawn(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], {
    encoding: "utf8",
    stdio: "pipe",
  }).stdout || "").trim();
  const pyOk = python3Spawn(["-c", "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"], { stdio: "pipe" }).status === 0;
  if (!pyOk) {
    s.stop(C.red(`Python ${pyVer} — too old`), 2);
    const installed = await tryBrewInstall("python@3.12", "Python 3.12");
    if (!installed) bail("Python 3.10+ is required.");
    s.start("Rechecking Python...");
    const newVer = String(python3Spawn(["-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"], {
      encoding: "utf8",
      stdio: "pipe",
    }).stdout || "").trim();
    const newOk = python3Spawn(["-c", "import sys; exit(0 if sys.version_info >= (3, 10) else 1)"], { stdio: "pipe" }).status === 0;
    if (!newOk) bail("Python 3.10+ required. Update your PATH to use Homebrew Python.");
    s.stop(C.green(`Python ${newVer}`));
  } else {
    s.stop(C.green(`Python ${pyVer}`));
  }

  // --- SQLite ---
  s.start("Checking SQLite 3.35+...");
  const sqliteVer = String(python3Spawn(["-c", "import sqlite3; print(sqlite3.sqlite_version)"], {
    encoding: "utf8",
    stdio: "pipe",
  }).stdout || "").trim();
  const sqliteOk = python3Spawn(["-c", "import sqlite3; parts=[int(x) for x in sqlite3.sqlite_version.split('.')]; exit(0 if (parts[0],parts[1])>=(3,35) else 1)"], { stdio: "pipe" }).status === 0;
  if (!sqliteOk) {
    s.stop(C.red(`SQLite ${sqliteVer} — too old`), 2);
    log.warn("Python's sqlite3 module uses the system SQLite. Installing Python via Homebrew links it to a modern SQLite.");
    const installed = await tryBrewInstall("python@3.12", "Python 3.12 (with modern SQLite)");
    if (!installed) bail("SQLite 3.35+ required for FTS5 + JSON support.");
    s.start("Rechecking SQLite...");
    const newVer = String(python3Spawn(["-c", "import sqlite3; print(sqlite3.sqlite_version)"], {
      encoding: "utf8",
      stdio: "pipe",
    }).stdout || "").trim();
    s.stop(C.green(`SQLite ${newVer}`));
  } else {
    s.stop(C.green(`SQLite ${sqliteVer}`));
  }

  // --- FTS5 ---
  s.start("Checking FTS5 support...");
  const fts5Ok = python3Spawn(["-c", "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(content)'); c.close()"], { stdio: "pipe" }).status === 0;
  if (!fts5Ok) {
    s.stop(C.red("FTS5 not available"), 2);
    log.warn("FTS5 is included in Homebrew's SQLite.");
    const installed = await tryBrewInstall("sqlite", "SQLite (with FTS5)");
    if (installed) {
      shell("brew reinstall python@3.12 2>/dev/null || brew reinstall python 2>/dev/null || true");
    }
    if (!installed) bail("SQLite FTS5 support is required.");
    s.start("Rechecking FTS5...");
    const ok = python3Spawn(["-c", "import sqlite3; c=sqlite3.connect(':memory:'); c.execute('CREATE VIRTUAL TABLE t USING fts5(content)'); c.close()"], { stdio: "pipe" }).status === 0;
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
    const hasHookSymbols = gatewayHasHookSymbols(gwDir);
    if (!hasHookSymbols) {
      s.message("Gateway hook symbols are bundled/renamed; using version + health gate...");
    }
    s.stop(C.green(hasHookSymbols ? "Gateway hooks present" : "Gateway lifecycle support (version-gated)"));
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
  // Uses snapshots from before openclaw commands (which create data/memory.db)
  if (!_existingInstallDetected && (_existingFiles.length > 0 || _hasConfig || _hasDb)) {
    log.warn("Existing Quaid files were found.");
    log.warn("Create a backup before updating config or database files.");

    const doBackup = handleCancel(await confirm({ message: "Create a backup now?" }));
    if (doBackup) {
      const backupSpinner = spinner();
      backupSpinner.start("Creating backup...");
      const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
      const backupDir = path.join(VISIBLE_HOME, `.quaid-backup-${ts}`);
      fs.mkdirSync(backupDir, { recursive: true });
      let count = 0;
      for (const f of ["SOUL.md", "USER.md", "ENVIRONMENT.md", "TOOLS.md", "AGENTS.md", "IDENTITY.md", "HEARTBEAT.md", "TODO.md"]) {
        const src = path.join(VISIBLE_HOME, f);
        if (fs.existsSync(src)) { fs.copyFileSync(src, path.join(backupDir, f)); count++; }
      }
      if (_hasConfig) { fs.copyFileSync(path.join(LEGACY_CONFIG_DIR, "config.json"), path.join(backupDir, "config.json")); count++; }
      if (_hasDb) { fs.copyFileSync(hiddenInstanceDbPath(), path.join(backupDir, "memory.db")); count++; }
      backupSpinner.stop(C.green("Backup created"));

      note(
        `${C.green(count + " files")} backed up to:\n${C.bcyan(backupDir)}\n\n` +
        `Backups are stored alongside your workspace.\n` +
        `Restore manually from this folder if you decide to roll back the install.`,
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

  if (_existingInstallDetected) {
    const existing = resolveExistingOwnerIdentity();
    if (existing?.id) {
      log.info(C.dim("Reusing existing owner identity from prior install."));
      log.info(C.dim(`Source: ${existing.source}`));
      log.success(`Owner: ${C.bcyan(existing.display)} ${C.dim(`(${existing.id})`)}`);
      return { display: existing.display, id: existing.id };
    }
    log.warn("Existing install detected but owner identity was not found in existing config; falling back to prompt.");
  }

  log.info(C.bold("Every memory is stored against an owner name."));
  log.info(C.bold("This is how Quaid keeps memories namespaced — one owner per person."));
  log.info(C.dim("Tell us your real name so memory tags stay human-readable."));
  log.message("");

  const seedName =
    String(INSTALL_ARGS.ownerName || process.env.QUAID_OWNER_NAME || "").trim();
  if (AGENT_MODE && !seedName && !_existingInstallDetected) {
    throw new Error("Agent mode requires --owner-name or QUAID_OWNER_NAME so memories are tagged to the person.");
  } else if (AGENT_MODE && !seedName && _existingInstallDetected) {
    throw new Error(
      "Agent mode detected an existing install but could not resolve owner identity from existing config. "
      + "Provide --owner-name (or QUAID_OWNER_NAME) to proceed."
    );
  }

  const display = handleCancel(await text({
    message: "What is your name so we can tag your memories?",
    initialValue: seedName || undefined,
    placeholder: "Your Name",
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

  const janitorAskFirst = false; // Auto-apply by default

  // Platform was already selected in step1_preflight
  let adapterType = resolvedInstallerPlatform() || "claude-code";

  // Use platform default LLM provider — no advanced setup needed
  const _modelSpinner = spinner();
  _modelSpinner.start("Reading adapter/provider defaults...");
  const forcedProvider = String(process.env.QUAID_INSTALL_PROVIDER || "").trim().toLowerCase();
  let provider = installerDefaultProvider(adapterType);
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] begin adapter=${adapterType} agentMode=${AGENT_MODE ? "1" : "0"} workspace=${WORKSPACE}`));
  }
  syncInstallerInstanceEnv(adapterType);
  // Instance IDs are runtime-owned and provision automatically on first use.
  // Installer keeps the deterministic default (<platform>-main) unless the
  // operator sets QUAID_INSTANCE explicitly before running setup.

  const adapterCaps = _readAdapterInstallerCapabilities(adapterType) || {};
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] adapter capabilities loaded for ${adapterType}`));
  }
  const supportsTimeoutCompaction = _platformSupportsTimeoutCompaction(adapterType);
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] timeout compaction support for ${adapterType}: ${supportsTimeoutCompaction ? "yes" : "no"}`));
  }
  const autoCompactionOnTimeout = supportsTimeoutCompaction; // Auto-enable if supported

  const supportedProviders = Array.isArray(adapterCaps.providers) && adapterCaps.providers.length > 0
    ? adapterCaps.providers
    : installerFallbackProviders(adapterType);
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] provider surface for ${adapterType}: ${supportedProviders.join(",")}`));
  }
  const hostManagedLlmDefault = _platformUsesHostManagedLlmByDefault(adapterType) ;
  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] host-managed llm default for ${adapterType}: ${hostManagedLlmDefault ? "yes" : "no"}`));
  }
  const adapterDefaultProvider = String(
    adapterCaps.defaultDeepProvider || adapterCaps.defaultFastProvider || ""
  ).trim().toLowerCase();

  const providerOptions = [
    { value: "anthropic",  label: "Anthropic (Claude)", hint: "Recommended" },
    { value: "openai",     label: "OpenAI",             hint: "Experimental" },
    { value: "openrouter", label: "OpenRouter",         hint: "Experimental — multi-provider gateway" },
    { value: "together",   label: "Together AI",        hint: "Experimental" },
    { value: "ollama",     label: "Ollama (local)",     hint: "Experimental — quality depends on model size" },
  ].filter((opt) => supportedProviders.includes(opt.value));

  let sharedOverride = null;
  if (hostManagedLlmDefault && adapterDefaultProvider) {
    const detectionHint = supportedProviders.includes(adapterDefaultProvider)
      ? ""
      : " (custom/unknown provider)";
    log.info(C.dim(`Detected ${adapterType} host provider (adapter): ${adapterDefaultProvider}${detectionHint}`));
  } else if (hostManagedLlmDefault) {
    log.warn(`${adapterType} adapter could not detect a host default provider.`);
  }
  if (hostManagedLlmDefault) {
    log.info(C.dim(`Using ${adapterType} host-managed LLM defaults. Configure alternate providers later in settings.`));
  } else {
    sharedOverride = _sharedModelOverride(adapterType);
    if (DEBUG_SETUP) {
      log.info(C.dim(`[step3_models] shared model override ${sharedOverride ? `found at ${sharedOverride.source}` : "not found"}`));
    }
    provider = resolveInstallerProvider(adapterType, supportedProviders, {
      sharedOverrideProvider: sharedOverride?.provider || "",
      forcedProvider,
    });
    if (sharedOverride?.provider && supportedProviders.includes(sharedOverride.provider)) {
      log.info(C.dim(`Provider override from shared config: ${provider} (${sharedOverride.source})`));
    }
    if (forcedProvider && supportedProviders.includes(forcedProvider)) {
      log.info(`Provider override: ${C.bcyan(provider)} ${C.dim("(QUAID_INSTALL_PROVIDER)")}`);
    } else if (forcedProvider && !supportedProviders.includes(forcedProvider)) {
      log.warn(`Ignoring QUAID_INSTALL_PROVIDER=${forcedProvider} (unsupported by adapter '${adapterType}').`);
    }
  }

  if (provider === "openai") {
    log.warn("OpenAI lanes are experimental in alpha.");
    log.warn("Benchmarks and live tests showed materially worse memory quality than Anthropic.");
    log.warn("Prefer Anthropic unless you are blocked on credentials.");
  }

  const sharedAuthTokenPath = sharedAuthRegistryPath(WORKSPACE);
  await _ensureCompatibleSharedCredentialForInstall(adapterType, provider, sharedAuthTokenPath);



  let highModel, lowModel;
  let deepReasoningEffort = "high";
  let fastReasoningEffort = "none";
  let modelsExplicitlyProvided = false;
  let envDeepModel = "";
  let envFastModel = "";
  const adapterDefaults = (
    adapterCaps.modelDefaults && typeof adapterCaps.modelDefaults === "object"
      ? adapterCaps.modelDefaults[String(provider || "").trim().toLowerCase()]
      : null
  ) || installerFallbackModelDefaults(adapterType, provider);
  envDeepModel = String(process.env.QUAID_INSTALL_DEEP_MODEL || "").trim();
  envFastModel = String(process.env.QUAID_INSTALL_FAST_MODEL || "").trim();
  if (sharedOverride?.deep && sharedOverride?.fast && (!sharedOverride.provider || sharedOverride.provider === provider)) {
    highModel = sharedOverride.deep;
    lowModel = sharedOverride.fast;
    modelsExplicitlyProvided = true;
    log.info(C.dim(`Model lanes overridden by shared config: deep=${highModel} fast=${lowModel}`));
  } else if (adapterDefaults && adapterDefaults.deep && adapterDefaults.fast) {
    highModel = String(adapterDefaults.deep);
    lowModel = String(adapterDefaults.fast);
    const adapterDeepEffort = String(adapterDefaults.deepEffort || "").trim().toLowerCase();
    const adapterFastEffort = String(adapterDefaults.fastEffort || "").trim().toLowerCase();
    if (adapterDeepEffort) deepReasoningEffort = adapterDeepEffort;
    if (adapterFastEffort) fastReasoningEffort = adapterFastEffort;
  } else if (provider === "anthropic") {
    // Global fallback when adapter does not define lane defaults.
    highModel = "claude-sonnet-4-5";
    lowModel = "claude-haiku-4-5";
  } else if (provider === "ollama") {
    highModel = "llama3.1:70b";
    lowModel = "llama3.1:8b";
  } else {
    highModel = "gpt-4o";
    lowModel = lowModelFor(highModel);
  }
  if (provider === "openai-codex") {
    // Codex gateway lanes degrade extraction quality when effort is omitted.
    deepReasoningEffort = "medium";
    fastReasoningEffort = "medium";
  }

  if (DEBUG_SETUP) {
    log.info(C.dim(`[step3_models] resolved defaults provider=${provider} deep=${highModel || "(unset)"} fast=${lowModel || "(unset)"}`));
  }

  if (false) {
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
    modelsExplicitlyProvided = true;
  }

  _modelSpinner.stop(C.green(`Provider: ${provider}`));

  if (!hostManagedLlmDefault && provider !== "anthropic") {
    log.warn(C.bold("Non-Anthropic providers are experimental. Prompts are tuned for Claude."));
    log.warn(C.bold("Extraction quality may vary. You can switch providers later in config."));
    log.message("");
    await waitForKey();
  }

  let modelReview = null;
  const reviewSpinner = spinner();
  reviewSpinner.start("Reviewing fast/deep model pairing...");
  try {
    modelReview = _reviewAdapterInstallerModelPair(adapterType, provider, highModel, lowModel) || null;
    if (modelReview?.needsClarification) {
      reviewSpinner.stop(C.yellow("Model pair needs confirmation"));
    } else {
      reviewSpinner.stop(C.green("Model pair reviewed"));
    }
  } catch (err) {
    reviewSpinner.stop(C.yellow("Model pair review unavailable"));
    log.warn(`Could not review installer model pair via adapter '${adapterType}': ${err.message}`);
  }

  if (modelReview?.needsClarification && !modelsExplicitlyProvided) {
    const clarificationReason = String(modelReview.reason || "").trim()
      || `No adapter fast/deep mapping is defined for provider '${provider}'.`;
    log.warn(clarificationReason);
    if (AGENT_MODE) {
      if (envDeepModel && envFastModel) {
        highModel = envDeepModel;
        lowModel = envFastModel;
        modelsExplicitlyProvided = true;
        log.info(C.dim(`Using agent-provided model pair: deep=${highModel} fast=${lowModel}`));
      } else {
        sendInstallerAgentNotice(
          [
            `Unknown or unmapped provider detected during install for adapter '${adapterType}'.`,
            `Current fast model: ${String(lowModel || "(unset)")}`,
            `Current deep model: ${String(highModel || "(unset)")}`,
            "",
            "Quaid needs the user to confirm or correct these model IDs before it can operate reliably.",
            "Fast model = quick routing, reranking, and classification work.",
            "Deep model = heavier reasoning, extraction, and complex tasks.",
          ].join("\n"),
          {
            severity: "warning",
            source: "installer",
            dedupeKey: "installer-unknown-provider",
            ttlSeconds: 900,
          }
        );
        throw new Error(
          `${clarificationReason} Agent mode needs explicit fast/deep model IDs from the user before install can continue.`
        );
      }
    } else {
      note(
        [
          clarificationReason,
          "",
          "Quaid needs two model IDs for this provider:",
          "  - Fast model: quick routing, reranking, and classification work.",
          "  - Deep model: extraction, full reasoning, and more complex tasks.",
          "",
          "Use provider/model format when possible (for example: openai/gpt-5.4-mini).",
        ].join("\n"),
        C.bmag("MODEL PAIR REQUIRED")
      );
      lowModel = handleCancel(await text({
        message: "Fast reasoning model:",
        placeholder: String(lowModel || "provider/model"),
        initialValue: String(lowModel || ""),
        validate: (v) => String(v || "").trim().length === 0 ? "Fast model is required" : undefined,
      }));
      highModel = handleCancel(await text({
        message: "Deep reasoning model:",
        placeholder: String(highModel || "provider/model"),
        initialValue: String(highModel || ""),
        validate: (v) => String(v || "").trim().length === 0 ? "Deep model is required" : undefined,
      }));
      modelsExplicitlyProvided = true;
    }
  }

  if (!AGENT_MODE || modelsExplicitlyProvided || !modelReview?.needsClarification) {
    log.info(`Deep reasoning: ${C.bcyan(highModel)}  |  Fast reasoning: ${C.bcyan(lowModel)}`);
  }

  if (adapterCaps.supportsLiveModelValidation) {
    let pingPassed = false;
    while (!pingPassed) {
      const ping = spinner();
      ping.start(`Validating ${adapterType} deep/fast models (provider PING)...`);
      try {
        const validation = _validateAdapterInstallerModelPairLive(adapterType, provider, highModel, lowModel) || {};
        if (validation.supported === false) {
          ping.stop(C.dim(`${adapterType} adapter does not support live model validation`));
        } else {
          ping.stop(C.green(String(validation.message || `${adapterType} model validation passed`)));
        }
        pingPassed = true;
      } catch (err) {
        ping.stop(C.red(`${adapterType} model validation failed`), 2);
        if (AGENT_MODE) throw err;
        const shouldRetry = handleCancel(await confirm({
          message: "Retry with different model IDs?",
          initialValue: true,
        }));
        if (!shouldRetry) throw err;
        lowModel = handleCancel(await text({
          message: "Fast reasoning model:",
          placeholder: String(lowModel || "provider/model"),
          initialValue: String(lowModel || ""),
          validate: (v) => String(v || "").trim().length === 0 ? "Fast model is required" : undefined,
        }));
        highModel = handleCancel(await text({
          message: "Deep reasoning model:",
          placeholder: String(highModel || "provider/model"),
          initialValue: String(highModel || ""),
          validate: (v) => String(v || "").trim().length === 0 ? "Deep model is required" : undefined,
        }));
        log.info(`Deep reasoning: ${C.bcyan(highModel)}  |  Fast reasoning: ${C.bcyan(lowModel)}`);
      }
    }
  } else {
    log.info(C.dim(`${adapterType} adapter does not expose live model validation during install.`));
  }

  // API key — the bot passes its key to Quaid at runtime.
  // No need to check env here.
  const keyEnv = keyEnvFor(provider);
  const llmProviderSetting = deriveInstallerLlmProviderSetting(
    adapterType,
    provider,
    highModel,
    lowModel,
    hostManagedLlmDefault,
  );

  // Notifications
  let notifLevel = "normal";
  if (false) {
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
  const pinnedNotifyRoute = _isPlatform("openclaw") ? resolvePinnedNotificationRoute() : null;
  const notifChannel = _isPlatform("openclaw")
    ? (pinnedNotifyRoute?.channel || "last_used")
    : "";
  if (_isPlatform("openclaw") && pinnedNotifyRoute?.channel) {
    log.info(C.dim(`Notifications will be pinned to the OpenClaw channel '${pinnedNotifyRoute.channel}' during install.`));
  } else if (_isPlatform("openclaw")) {
    log.warn("No active OpenClaw notification route detected; falling back to last_used until a channel is established.");
  }
  log.info(C.dim("You can ask your agent to change notification routing or level anytime."));

  const preset = (() => {
    if (notifLevel === "quiet") return { janitor: "off", extraction: "off", retrieval: "off" };
    if (notifLevel === "verbose") return { janitor: "full", extraction: "summary", retrieval: "summary" };
    if (notifLevel === "debug") return { janitor: "full", extraction: "full", retrieval: "full" };
    return { janitor: "summary", extraction: "summary", retrieval: "off" };
  })();

  const advancedNotif = false && handleCancel(await confirm({
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
    apiFormat: llmProviderSetting,
    apiKeyEnv: keyEnv,
    baseUrl: baseUrlFor(provider),
    deepReasoningEffort,
    fastReasoningEffort,
    notifLevel,
    notifConfig,
    notifChannel,
    advancedSetup: false,
    adapterType,
    janitorAskFirst,
    autoCompactionOnTimeout,
  };
}

// =============================================================================
// Step 4: Embeddings
// =============================================================================
/**
 * Detect an already-configured embedding setup from a shared config object.
 *
 * Returns { provider, embedModel, embedDim } if any provider's embedding is
 * configured, or null if no embedding setup is found.
 *
 * Add a new branch here when a new embeddings provider is added.
 */
function detectSharedEmbeddings(cfg) {
  if (!cfg || typeof cfg !== "object") return null;

  // ollama (current provider)
  if (cfg.ollama?.embeddingModel && cfg.ollama.embeddingModel !== "none") {
    return {
      provider: "ollama",
      embedModel: cfg.ollama.embeddingModel,
      embedDim: cfg.ollama.embeddingDim || 0,
    };
  }

  // future providers:
  // if (cfg.openai?.embeddingModel) return { provider: "openai", embedModel: cfg.openai.embeddingModel, embedDim: cfg.openai.embeddingDim || 1536 };
  // if (cfg.cohere?.embeddingModel) return { provider: "cohere", embedModel: cfg.cohere.embeddingModel, embedDim: cfg.cohere.embeddingDim || 1024 };

  return null;
}

async function step4_embeddings() {
  stepHeader(4, TOTAL_INSTALL_STEPS, "EMBEDDINGS", STEP_QUOTES.embeddings);

  const envSpinner = spinner();
  envSpinner.start("Checking embeddings environment...");

  // Embeddings config is machine-wide — only ask once per machine.
  // Check platform-shared first, then global shared fallback.
  const platformKey = resolvedInstallerPlatform();
  const sharedSearchPaths = [
    path.join(WORKSPACE, "shared", "config", platformKey, "config.json"),
    path.join(WORKSPACE, "shared", "config", "global", "config.json"),
  ];
  for (const sharedConfigPath of sharedSearchPaths) {
    if (!fs.existsSync(sharedConfigPath)) continue;
    try {
      const sharedCfg = JSON.parse(fs.readFileSync(sharedConfigPath, "utf8"));
      const found = detectSharedEmbeddings(sharedCfg);
      if (found) {
        envSpinner.stop(C.green("Embeddings inherited from shared config"));
        log.info(C.dim("Embeddings already configured in shared config — inheriting."));
        log.info(`  provider: ${C.cyan(found.provider)}  model: ${C.cyan(found.embedModel)}  dim: ${found.embedDim || "auto"}`);
        log.info(C.dim(`  source: ${sharedConfigPath}`));
        log.info(C.dim("  If you want to change embedding defaults later, it is best to use your agents for Quaid config changes."));
        log.message("");
        return { embedModel: found.embedModel, embedDim: found.embedDim };
      }
    } catch { /* malformed shared config — fall through to normal setup */ }
  }

  log.info(C.dim("Embeddings power semantic search — turning text into vectors"));
  log.info(C.dim("so Quaid can find relevant memories by meaning, not just keywords."));
  const { total: totalRam, free: freeRam } = getSystemRAM();
  log.info(C.dim(`System RAM: ${totalRam}GB total, ~${freeRam}GB available`));
  log.info(C.dim(`"nomic-embed-text" is the default embedding engine and uses about 300MB of RAM when kept alive.`));
  log.info(C.dim("Keeping it alive is recommended so embeddings stay responsive."));

  // Check Ollama
  let ollamaRunning = false;
  if (process.env.QUAID_TEST_NO_OLLAMA) {
    // Test mode: simulate Ollama not installed/running
    ollamaRunning = false;
  } else {
    try { execSync(`curl -sf ${JSON.stringify(OLLAMA_TAGS_URL)}`, { stdio: "pipe" }); ollamaRunning = true; } catch {}
  }

  envSpinner.stop(
    ollamaRunning
      ? C.green("Embeddings environment checked")
      : C.yellow("Embeddings environment checked — Ollama needs attention")
  );

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
    log.info("Ollama not found — installing (required for semantic recall).");
    {
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
    }
  }

  let embedModel, embedDim;

  if (ollamaRunning) {
    const inspectSpinner = spinner();
    inspectSpinner.start("Inspecting local Ollama models...");
    const { total, free } = getSystemRAM();
    const pulledModels = getOllamaModels();
    const loadedModels = getLoadedOllamaModels();
    inspectSpinner.stop(C.green("Ollama embeddings environment ready"));

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
    // Use nomic-embed-text — required, no choice needed
    embedModel = "nomic-embed-text";
    embedDim = 768;
    log.info(`Embedding model: ${C.bcyan("nomic-embed-text")} (768 dim, ~274MB download)`);

    {

      // Check if model is pulled, if not pull it
      const hasPulled = installedEmbedModels.includes(embedModel);
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

      // Configure Ollama to keep the model loaded (OLLAMA_KEEP_ALIVE=-1).
      // Without this, Ollama unloads the model after 5 minutes of inactivity,
      // causing cold-start latency that can exceed hook deadlines.
      const ollamaPlist = "/opt/homebrew/opt/ollama/homebrew.mxcl.ollama.plist";
      if (fs.existsSync(ollamaPlist)) {
        try {
          // Try Set first (key exists); fall back to Add (key missing).
          execSync(
            `/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:OLLAMA_KEEP_ALIVE -1" "${ollamaPlist}" 2>/dev/null` +
            ` || /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:OLLAMA_KEEP_ALIVE string -1" "${ollamaPlist}"`,
            { stdio: "pipe" }
          );
          execSync("brew services restart ollama", { stdio: "pipe", timeout: 15000 });
          log.success("Ollama keep-alive configured — model stays loaded between calls");
        } catch {
          log.warn("Could not configure Ollama keep-alive automatically.");
          log.warn("Set OLLAMA_KEEP_ALIVE=-1 in the Ollama service environment manually.");
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

  log.message("");
  await waitForKey();
  return { embedModel, embedDim };
}

// =============================================================================
// Step 5: Janitor Schedule
// =============================================================================
async function step6_schedule(embeddings = {}, advancedSetup = false, janitorAskFirst = true) {
  stepHeader(5, TOTAL_INSTALL_STEPS, "JANITOR", STEP_QUOTES.janitor);

  log.info(C.dim("The janitor runs automatically from the extraction daemon."));
  log.info(C.dim("It reviews facts, deduplicates, detects contradictions, and maintains docs."));
  log.info(C.dim("Default schedule: 4:00 AM daily. Use your agents if you need to change janitor behavior later."));

  const scheduleHour = 4;

  let scheduled = true; // Daemon handles scheduling automatically

  const approvalPolicies = {
    coreMarkdownWrites: "auto",
    projectDocsWrites: "auto",
    destructiveMemoryOps: "auto",
  };

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
    "If `runtime/notes/delayed-llm-requests.json` exists:",
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
  const janitorLogDir = path.join(hiddenInstanceLogsDir(), "janitor");
  const logPath = path.join(janitorLogDir, "launchd.log");
  const errPath = path.join(janitorLogDir, "launchd-err.log");

  // Ensure log directory exists
  fs.mkdirSync(janitorLogDir, { recursive: true });

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

function installCodexDaemonLaunchAgent(instanceId) {
  if (process.platform !== "darwin") {
    log.warn("Persistent Codex daemon supervision is launchd-only on macOS.");
    return false;
  }

  const normalizedInstance = String(instanceId || resolvedInstallerInstanceId("codex")).trim();
  if (!normalizedInstance) {
    log.warn("Codex daemon launch agent requires a real instance ID; skipping installer-time launch agent.");
    return false;
  }
  const quaidCmd = fs.existsSync(path.join(PLUGIN_DIR, "quaid"))
    ? path.join(PLUGIN_DIR, "quaid")
    : "quaid";
  const label = `com.quaid.daemon.${normalizedInstance}`;
  const plistPath = path.join(os.homedir(), "Library", "LaunchAgents", `${label}.plist`);
  const daemonLogDir = path.join(hiddenInstanceLogsDir(normalizedInstance), "daemon");
  const outPath = path.join(daemonLogDir, "launchd.log");
  const errPath = path.join(daemonLogDir, "launchd-err.log");

  fs.mkdirSync(daemonLogDir, { recursive: true });

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${quaidCmd}</string>
    <string>daemon</string>
    <string>run</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>QUAID_HOME</key>
    <string>${WORKSPACE}</string>
    <key>QUAID_VISIBLE_HOME</key>
    <string>${VISIBLE_HOME}</string>
    <key>OPENCLAW_WORKSPACE</key>
    <string>${WORKSPACE}</string>
    <key>QUAID_INSTANCE</key>
    <string>${normalizedInstance}</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>WorkingDirectory</key>
  <string>${PLUGIN_DIR}</string>
  <key>StandardOutPath</key>
  <string>${outPath}</string>
  <key>StandardErrorPath</key>
  <string>${errPath}</string>
</dict>
</plist>
`;

  try {
    if (fs.existsSync(plistPath)) {
      spawnSync("launchctl", ["bootout", `gui/${process.getuid()}`, plistPath], { stdio: "pipe" });
      spawnSync("launchctl", ["unload", plistPath], { stdio: "pipe" });
    }
    fs.mkdirSync(path.dirname(plistPath), { recursive: true });
    fs.writeFileSync(plistPath, plist, "utf8");
    let load = spawnSync("launchctl", ["bootstrap", `gui/${process.getuid()}`, plistPath], { stdio: "pipe" });
    if (load.status !== 0) {
      load = spawnSync("launchctl", ["load", plistPath], { stdio: "pipe" });
    }
    if (load.status !== 0) {
      const detail = String(load.stderr || load.stdout || "").trim();
      log.warn(`launchctl daemon load failed for ${normalizedInstance}: ${detail || "unknown error"}`);
      return false;
    }
    return true;
  } catch (err) {
    log.warn(`failed to install Codex daemon launch agent: ${String(err?.message || err)}`);
    return false;
  }
}

function installWindowsScheduledTask(hour) {
  // Windows Task Scheduler for nightly janitor.
  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const taskName = "QuaidJanitor";
  const janitorLogDir = path.join(hiddenInstanceLogsDir(), "janitor");
  const logPath = path.join(janitorLogDir, "schtasks.log");

  fs.mkdirSync(janitorLogDir, { recursive: true });

  // Build a wrapper script that sets env vars and runs janitor
  const batchPath = path.join(PLUGIN_DIR, "janitor-scheduled.bat");
  const batchContent = `@echo off
set QUAID_HOME=${WORKSPACE}
set PYTHONPATH=${PLUGIN_DIR}
"${quaidCmd}" janitor --task all --apply --time-budget 3600 >> "${logPath}" 2>&1
`;

  try {
    fs.writeFileSync(batchPath, batchContent);

    // Delete existing task if present (ignore errors)
    spawnSync("schtasks", ["/delete", "/tn", taskName, "/f"], { stdio: "pipe" });

    // Create daily task
    const startTime = `${String(hour).padStart(2, "0")}:30`;
    const result = spawnSync("schtasks", [
      "/create",
      "/tn", taskName,
      "/tr", batchPath,
      "/sc", "daily",
      "/st", startTime,
      "/rl", "LIMITED",
      "/f",
    ], { stdio: "pipe" });

    return result.status === 0;
  } catch {
    return false;
  }
}

function installCrontabSchedule(hour) {
  // Linux/other: crontab entry for nightly janitor.
  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const janitorLogDir = path.join(hiddenInstanceLogsDir(), "janitor");
  const logPath = path.join(janitorLogDir, "cron.log");

  fs.mkdirSync(janitorLogDir, { recursive: true });

  const envVars = `QUAID_HOME='${WORKSPACE}' PYTHONPATH='${PLUGIN_DIR}'`;
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
  for (const dir of [
    HIDDEN_INSTANCES_DIR,
    VISIBLE_INSTANCES_DIR,
    PROJECTS_DIR,
    ADAPTER_REGISTRY_DIR,
    RUNTIME_DIR,
    RUNTIME_NOTES_DIR,
    path.join(WORKSPACE, "shared", "config"),
  ]) {
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
  const runtimeUpdateSrc = path.join(pluginSrc, "update-quaid.mjs");
  if (fs.existsSync(runtimeUpdateSrc)) {
    fs.copyFileSync(runtimeUpdateSrc, path.join(PLUGIN_DIR, "update-quaid.mjs"));
  }
  for (const stalePath of [
    path.join(PLUGIN_DIR, "tests"),
    path.join(PLUGIN_DIR, "scripts"),
    path.join(PLUGIN_DIR, "adaptors", "openclaw", "clawdbot.plugin.json"),
  ]) {
    try {
      fs.rmSync(stalePath, { recursive: true, force: true });
    } catch {}
  }
  const skipBinShim = String(process.env.QUAID_INSTALL_SKIP_BIN_SHIM || "").trim() === "1";
  if (skipBinShim) {
    log.info("Skipping quaid CLI shim update (QUAID_INSTALL_SKIP_BIN_SHIM=1).");
  } else {
    const shimPath = ensureQuaidCliShim(PLUGIN_DIR);
    if (shimPath) {
      log.info(`Updated CLI shim: ${shimPath} -> ${path.join(PLUGIN_DIR, "quaid")}`);
    } else {
      log.warn("Could not update quaid CLI shim automatically.");
    }
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
  const preservedOpenClawManagedState = _isPlatform("openclaw")
    ? composeOpenClawManagedStateSnapshots(
        _preinstallOpenClawManagedState,
        _captureOpenClawManagedState(),
      )
    : null;
  // Installer creates only shared/runtime state. Per-instance silos are created
  // on first hook use, once the adapter has the real instance ID.
  const resolvedInstanceId = String(process.env.QUAID_INSTANCE || "").trim();
  if (resolvedInstanceId) {
    const hiddenRoot = hiddenInstanceDir(resolvedInstanceId);
    const visibleRoot = visibleInstanceDir(resolvedInstanceId);
    for (const dir of [hiddenRoot, path.join(hiddenRoot, "data"), path.join(hiddenRoot, "logs"), visibleRoot]) {
      fs.mkdirSync(dir, { recursive: true });
    }
    log.info(`Created hidden instance directory: ${hiddenRoot}`);
    log.info(`Created visible instance directory: ${visibleRoot}`);
    for (const f of ensureVisibleIdentityStubs(visibleRoot)) {
      log.info(`Created ${f}`);
    }
    const instanceJournalDir = path.join(visibleRoot, "journal");
    if (!fs.existsSync(instanceJournalDir)) {
      fs.mkdirSync(instanceJournalDir, { recursive: true });
      log.info(`Created journal directory: ${instanceJournalDir}`);
    }
    _seedJanitorInstallCheckpoint(resolvedInstanceId);
    // Create misc project dir in projects/misc--{instanceId}/.
    // Lives as a real tracked project — all registry tooling works automatically.
    for (const bucket of [`misc--${resolvedInstanceId}`]) {
      const bucketDir = path.join(PROJECTS_DIR, bucket);
      if (!fs.existsSync(bucketDir)) {
        fs.mkdirSync(bucketDir, { recursive: true });
        log.info(`Created project bucket dir: projects/${bucket}/`);
      }
    }
  }
  if (_isPlatform("openclaw") && resolvedInstanceId) {
    const runtimeEnvReconciled = _ensureOpenClawRuntimeInstanceEnv(resolvedInstanceId);
    if (runtimeEnvReconciled) {
      log.info(`Reconciled OpenClaw runtime instance env to ${resolvedInstanceId}`);
      const restart = spawnSync("openclaw", ["gateway", "restart"], { encoding: "utf8", stdio: "pipe" });
      if (restart.status !== 0) {
        const detail = String(restart.stderr || restart.stdout || "").trim();
        log.warn(`OpenClaw gateway restart after runtime env reconcile exited non-zero (will verify health next): ${detail || "unknown"}`);
      }
      await waitForGatewayWarmup(30_000);
      await _reassertOpenClawPostRestartState("runtime env reconcile", preservedOpenClawManagedState);
    }
  }
  if (_isPlatform("claude-code")) {
    s.start("Configuring Claude Code hooks...");
    setupClaudeCodeHooks();
    s.stop(C.green("Claude Code hooks configured"));
    const claudeShimPath = ensureClaudeCliShim();
    if (claudeShimPath) {
      log.info(`Updated Claude Code CLI shim: ${claudeShimPath}`);
    } else {
      log.warn("Could not update Claude Code CLI shim automatically.");
    }
  }
  if (_isPlatform("codex")) {
    s.start("Configuring Codex hooks...");
    setupCodexHooks();
    s.stop(C.green("Codex hooks configured"));
    if (resolvedInstanceId) {
      s.start("Installing Codex daemon launch agent...");
      if (installCodexDaemonLaunchAgent(resolvedInstanceId)) {
        s.stop(C.green("Codex daemon launch agent installed"));
      } else {
        s.stop(C.yellow("Codex daemon launch agent not installed"));
        log.warn("Codex background extraction will rely on manual/one-shot daemon startup until launchd is available.");
      }
    } else {
      log.info("Skipping Codex daemon launch agent install until the first real instance is created by hook use.");
    }
  }

  if (resolvedInstanceId) {
    // Explicit instance install: initialize the target silo now. Standard
    // installs defer silo creation to first hook use.
    s.start("Initializing database...");
    const dbPath = hiddenInstanceDbPath(resolvedInstanceId);
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
    const initResult = python3Spawn(["-c", initScript], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
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
    const verifyResult = python3Spawn(["-c", verifyScript], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    const nodesTableCount = Number((verifyResult.stdout || "").trim());
    if (verifyResult.status !== 0 || !Number.isFinite(nodesTableCount) || nodesTableCount < 1) {
      s.stop(C.red("Database initialization failed"));
      const detail = (verifyResult.stderr || verifyResult.stdout || "").trim();
      throw new Error(detail || "nodes table missing after schema initialization");
    }
    try { fs.chmodSync(dbPath, 0o600); } catch {}
    s.stop(C.green("Database initialized"));
  } else {
    log.info("Skipping instance database initialization until the first hook creates a real instance silo.");
  }

  // Write config
  s.start("Writing configuration...");
  writeConfig(owner, models, embeddings, systems, janitorPolicies);
  s.stop(C.green("Config written"));

  // Installer-owned contract bootstrap: load config once so datastore init/config
  // hooks run exactly once (for all enabled datastores).
  if (resolvedInstanceId) {
    s.start("Bootstrapping datastores...");
    const domainInitScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from config import get_config
_cfg = get_config()
print('[+] Datastore init hooks complete')
`;
    const domainInitResult = python3Spawn(["-c", domainInitScript], {
      cwd: PLUGIN_DIR,
      encoding: "utf8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    if (domainInitResult.status !== 0) {
      const detail = String(domainInitResult.stderr || domainInitResult.stdout || "").trim();
      log.warn(`Datastore init hook bootstrap failed during install; continuing. ${detail || ""}`.trim());
    }
    s.stop(C.green("Datastore bootstrap complete"));
  } else {
    log.info("Skipping datastore bootstrap until the first hook creates an instance config.");
  }

  // Contract-owned project workspace dirs should exist after datastore init hooks.
  // Some runtime profiles trim plugin slots during bootstrap; guard here so
  // install always yields expected workspace shape.
  // misc is a tracked project in projects/misc--{instanceId}/
  const instanceMiscDir = resolvedInstanceId ? path.join(PROJECTS_DIR, `misc--${resolvedInstanceId}`) : "";
  const contractOwnedDirs = Array.from(new Set([PROJECTS_DIR, instanceProjectsDir(), ...(instanceMiscDir ? [instanceMiscDir] : [])]));
  const missingContractOwnedDirs = contractOwnedDirs.filter((dir) => !fs.existsSync(dir));
  s.start("Reconciling workspace structure...");
  if (missingContractOwnedDirs.length > 0) {
    for (const dir of missingContractOwnedDirs) {
      fs.mkdirSync(dir, { recursive: true });
    }
    log.warn(
      `Datastore init did not materialize ${missingContractOwnedDirs.length} contract-owned workspace dir(s); `
      + "installer created them as fallback."
    );
  }

  // The misc project is the only installer-owned bucket for ad-hoc drafts.
  // The installer only bootstraps local history after datastore init has run.
  if (instanceMiscDir && fs.existsSync(instanceMiscDir) && !fs.existsSync(path.join(instanceMiscDir, ".git"))) {
    const miscGitInit = spawnSync("git", ["init"], {
      cwd: instanceMiscDir,
      stdio: "pipe",
      encoding: "utf8",
    });
    if (miscGitInit.status === 0) {
      log.info("Initialized misc project local git history");
    } else {
      const detail = String(miscGitInit.stderr || miscGitInit.stdout || "").trim();
      log.warn(`Could not initialize misc/ git history${detail ? `: ${detail}` : ""}`);
    }
  } else if (instanceMiscDir && !fs.existsSync(instanceMiscDir)) {
    log.warn("misc project dir missing after datastore init hooks; skipping git history bootstrap.");
  }
  s.stop(C.green("Workspace structure ready"));

  s.start("Finalizing visible workspace...");
  s.stop(C.green("Visible workspace ready"));

  // Initialize git repo for the visible workspace (required for doc/project tracking)
  const gitDir = path.join(VISIBLE_HOME, ".git");
  if (!fs.existsSync(gitDir)) {
    s.start("Initializing git repository...");
    fs.mkdirSync(VISIBLE_HOME, { recursive: true });
    spawnSync("git", ["init"], { cwd: VISIBLE_HOME, stdio: "pipe" });
    // Create .gitignore for generated/editor artifacts in the visible workspace.
    const gitignore = [
      "# OS",
      ".DS_Store",
      "Thumbs.db",
      "",
      "# Python",
      "__pycache__/",
      "*.pyc",
      ".pytest_cache/",
      "",
      "# Build",
      "node_modules/",
      "build/",
      "",
    ].join("\n");
    const ignorePath = path.join(VISIBLE_HOME, ".gitignore");
    if (!fs.existsSync(ignorePath)) {
      fs.writeFileSync(ignorePath, gitignore);
    }
    // Initial commit so git diff/log have a baseline
    spawnSync("git", ["add", "-A"], { cwd: VISIBLE_HOME, stdio: "pipe" });
    const initCommit = spawnSync("git", ["commit", "-m", "Initial Quaid workspace"], { cwd: VISIBLE_HOME, stdio: "pipe" });
    if (initCommit.status !== 0) {
      const fallbackCommit = spawnSync(
        "git",
        ["-c", "user.name=Quaid Installer", "-c", "user.email=installer@local", "commit", "-m", "Initial Quaid workspace"],
        { cwd: VISIBLE_HOME, stdio: "pipe" },
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
  if (resolvedInstanceId) {
    s.start("Creating owner node...");
    const storeScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store
try:
    store(${JSON.stringify(owner.display)}, owner_id=${JSON.stringify(owner.id)}, category='person', source='installer')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
`;
    python3Spawn(["-c", storeScript], { cwd: PLUGIN_DIR, stdio: "pipe" });
    s.stop(C.green(`Owner node: ${owner.display}`));
  } else {
    log.info("Skipping owner node creation until the first hook creates an instance database.");
  }

  if (_isPlatform("openclaw")) {
    s.start("Registering Quaid plugin in OpenClaw...");
    const reg = _ensureOpenClawPluginRegistered(PLUGIN_DIR);
    if (!reg.ok) {
      s.stop(C.red("OpenClaw plugin registration failed"));
      throw new Error(reg.reason || "openclaw plugins install/enable failed");
    }
    if (reg.repaired) {
      log.info("OpenClaw add-instance install repaired a missing/stale plugin registration.");
    }
    s.message("Waiting for OpenClaw gateway to restart and warm up...");
    if (_ensureOpenClawPluginsAllowQuaid()) {
      log.info("Ensured plugins.allow includes: quaid");
    }
    const gatewayLaunchAgentEnvReconciled = _ensureOpenClawGatewayLaunchAgentEnv(resolvedInstanceId);
    if (gatewayLaunchAgentEnvReconciled) {
      log.info("Reconciled ai.openclaw.gateway launch agent env for Quaid");
    }
    await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "plugin registration", 60_000);
    await _reassertOpenClawPostRestartState("plugin registration", preservedOpenClawManagedState);
    await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "post-registration sanitizer", 60_000);
    s.message("Finalizing OpenClaw hook configuration...");
    enableRequiredOpenClawHooks();
    // enableRequiredOpenClawHooks writes openclaw.json directly, which may trigger a gateway
    // config reload. Give the gateway time to settle before proceeding.
    await waitForGatewayWarmup(30_000);
    await _reassertOpenClawPostRestartState("hook configuration", preservedOpenClawManagedState);
    const finalManagedState = composeOpenClawManagedStateSnapshots(
      preservedOpenClawManagedState,
      _loadPersistedOpenClawManagedState(),
      _captureOpenClawManagedState(),
    );
    if (finalManagedState && _persistOpenClawManagedState(finalManagedState)) {
      log.info("Persisted OpenClaw managed state snapshot for drift recovery");
    }
    let postHookReadyError = null;
    try {
      await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "post-hook sanitizer", 60_000);
    } catch (err) {
      postHookReadyError = err;
    }
    const guardInstalled = _installOpenClawManagedStateGuard();
    if (guardInstalled) {
      log.info("Installed OpenClaw managed-state guard");
    }
    if (postHookReadyError) {
      throw postHookReadyError;
    }
    if (guardInstalled) {
      await ensureGatewayReadyOrThrow(_resolveInstallerMessageCli(), "post-guard activation", 60_000);
    }
    s.stop(C.green("OpenClaw plugin registered and gateway ready"));
  }

  // Workspace migration is intentionally not part of installer flow.
  // Memory should accumulate naturally, or users can request migration later.
  const migrationCompleted = true;
  const mdFiles = [];

  if (resolvedInstanceId && !postInstallStateStabilized) {
    const postInstall = _stabilizePostInstallExtractionState(resolvedInstanceId);
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

    // Register any existing project directories (e.g. migrating from a previous install)
    if (resolvedInstanceId && existingDirs.length > 0) {
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
      const regResult = python3Spawn(["-c", registerScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      const regCount = (regResult.stdout || "").trim();
      s.stop(C.green(`Registered ${existingDirs.length} project(s), ${regCount} doc(s) indexed`));
    } else if (existingDirs.length > 0) {
      log.info("Skipping existing project docs registration until the first real instance is created.");
    }

    log.info(C.dim("Your agent can discover more projects — ask it to \"set up projects\""));

    // Install Quaid project reference docs and constitutional guidance.
    // Canonical projects/ lives at QUAID_HOME level, shared across all instances.
    const quaidProjDir = path.join(PROJECTS_DIR, "quaid");
    fs.mkdirSync(quaidProjDir, { recursive: true });
    const quaidProjSrc = path.join(__dirname, "projects", "quaid");
    if (fs.existsSync(quaidProjSrc)) {
      copyMissingDirSync(quaidProjSrc, quaidProjDir);
    }
    ensureProjectSeedFileFromTemplate(quaidProjSrc, quaidProjDir, "AGENTS.md", MINIMAL_QUAID_PROJECT_AGENTS_MD);
    ensureProjectSeedFileFromTemplate(quaidProjSrc, quaidProjDir, "TOOLS.md");
    ensureProjectSeedFileFromTemplate(quaidProjSrc, quaidProjDir, "PROJECT.md");
    const quaidSourceRoot = path.relative(WORKSPACE, PLUGIN_DIR).split(path.sep).join("/");
    const quaidSourceRoots = JSON.stringify(quaidSourceRoot ? [quaidSourceRoot] : []);
    // Register Quaid as a project unless it was already covered by existing project scan.
    const quaidAlreadyRegisteredViaExisting = existingDirs.includes("quaid");
    if (resolvedInstanceId) {
      s.start("Registering bundled project docs...");
      const regQuaidScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.docsdb.registry import DocsRegistry
from core.project_registry import create_project as create_global_project, get_project as get_global_project, link_project as link_global_project
reg = DocsRegistry()
if not get_global_project('quaid'):
    create_global_project(
        'quaid',
        description='Quaid runtime, memory, project-doc, and adapter reference docs.',
        initial_instance=${resolvedInstanceId ? JSON.stringify(resolvedInstanceId) : "None"},
    )
elif ${resolvedInstanceId ? "True" : "False"}:
    link_global_project('quaid', instance_id=${JSON.stringify(resolvedInstanceId)})
try:
    reg.create_project(
        'quaid',
        label='Quaid Knowledge Layer',
        home_dir='projects/quaid/',
        source_roots=${quaidSourceRoots},
        description='Quaid runtime, memory, project-doc, and adapter reference docs.',
    )
except ValueError:
    pass  # already exists
found = reg.auto_discover('quaid')
print(len(found))
`;
      const regQuaidResult = python3Spawn(["-c", regQuaidScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
      if (regQuaidResult.error) {
        throw regQuaidResult.error;
      }
      if (regQuaidResult.status !== 0) {
        const detail = String(regQuaidResult.stderr || regQuaidResult.stdout || "").trim();
        throw new Error(detail || "Bundled quaid project registration failed");
      }
      const quaidDocCount = (regQuaidResult.stdout || "").trim();
      if (quaidAlreadyRegisteredViaExisting) {
        log.info(`Quaid project re-linked and refreshed (${quaidDocCount} docs discovered in refresh pass)`);
      } else {
        log.info(`Quaid project installed (${quaidDocCount} new docs discovered)`);
      }
    } else {
      log.info("Skipping bundled project docs registration until the first real instance is created.");
    }

    // Register instance misc project in projects/misc--{instanceId}/.
    // Single catch-all bucket for work without a proper project home.
    if (resolvedInstanceId && resolvedInstanceId !== "standalone") {
      const sharedBuckets = [
        { name: `misc--${resolvedInstanceId}`, label: `Misc (${resolvedInstanceId})`,
          description: "Miscellaneous work without a dedicated project: drafts, one-offs, quick scripts." },
      ];
      for (const bucket of sharedBuckets) {
        const regScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.docsdb.registry import DocsRegistry
reg = DocsRegistry()
try:
    reg.create_project(${JSON.stringify(bucket.name)}, label=${JSON.stringify(bucket.label)},
        home_dir=${JSON.stringify(`projects/${bucket.name}/`)},
        description=${JSON.stringify(bucket.description)})
    created = True
except ValueError:
    created = False
from core.project_registry import create_project as create_global_project, get_project as get_global_project, link_project as link_global_project
if not get_global_project(${JSON.stringify(bucket.name)}):
    create_global_project(
        ${JSON.stringify(bucket.name)},
        description=${JSON.stringify(bucket.description)},
        initial_instance=${JSON.stringify(resolvedInstanceId)},
    )
else:
    link_global_project(${JSON.stringify(bucket.name)}, instance_id=${JSON.stringify(resolvedInstanceId)})
print('created' if created else 'exists')
`;
        const result = python3Spawn(["-c", regScript], {
          cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"],
          env: { ...process.env, QUAID_HOME: WORKSPACE, QUAID_VISIBLE_HOME: VISIBLE_HOME },
        });
        if (result.error) {
          throw result.error;
        }
        if (result.status !== 0) {
          const detail = String(result.stderr || result.stdout || "").trim();
          throw new Error(detail || `Shared bucket registration failed for ${bucket.name}`);
        }
        if ((result.stdout || "").trim() === "created") {
          log.info(`Registered shared bucket: ${bucket.name}`);
        } else {
          log.info(`Re-linked shared bucket: ${bucket.name}`);
        }
      }
    }

    // Keep projects/quaid/TOOLS.md domain block aligned after install.
    const syncToolsScript = path.join(pluginSrc, "scripts", "sync-tools-domain-block.py");
    if (fs.existsSync(syncToolsScript)) {
      python3Spawn([syncToolsScript, "--workspace", WORKSPACE], {
        cwd: __dirname,
        stdio: "pipe",
        env: { ...process.env, QUAID_HOME: WORKSPACE, QUAID_VISIBLE_HOME: VISIBLE_HOME, OPENCLAW_WORKSPACE: WORKSPACE },
      });
    }
    s.stop(C.green("Bundled project docs ready"));
  }

  if (resolvedInstanceId && !postInstallStateStabilized) {
    const postInstall = _stabilizePostInstallExtractionState(resolvedInstanceId);
    postInstallStateStabilized = true;
    log.info(
      `Marked prior sessions as extracted: seeded ${postInstall.cursorsSeeded} cursor(s), `
      + `cleared ${postInstall.pendingSignalsCleared} pending signal(s), `
      + `${postInstall.timeoutBuffersCleared} stale timeout buffer(s).`
    );
  }
  s.start("Running platform post-install tasks...");
  runAdapterInstallHook(resolvedInstallerPlatform(), "postinstall");
  s.stop(C.green("Platform post-install tasks complete"));
  log.success("Installation complete!");
  // Write install timestamp so the session-index watcher knows to ignore
  // sessions that predate this install (prevents orphan extraction fan-out
  // after a clean reinstall/wipe).
  if (resolvedInstanceId) {
    try {
      fs.mkdirSync(hiddenInstanceDataDir(resolvedInstanceId), { recursive: true });
      fs.writeFileSync(
        hiddenInstanceInstallStatePath(resolvedInstanceId),
        JSON.stringify({ installedAt: new Date().toISOString() }),
        { mode: 0o600 },
      );
    } catch {}
  }
  log.message("");
  try {
    const markerPath = runtimePendingInstallMigrationPath();
    fs.mkdirSync(path.dirname(markerPath), { recursive: true });
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
  const _validationInstanceId = String(process.env.QUAID_INSTANCE || "").trim();
  const dbPath = hiddenInstanceDbPath(_validationInstanceId);
  if (fs.existsSync(dbPath)) {
    const tableProbe = `
import sqlite3
c = sqlite3.connect(${JSON.stringify(dbPath)})
print(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0])
c.close()
`;
    const tableResult = python3Spawn(["-c", tableProbe], { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    const tables = tableResult.status === 0 ? (tableResult.stdout || "").trim() : "unknown";
    checks.push(`${C.green("■")} Database     ${C.dim("—")} ${tables} tables`);
  } else if (_validationInstanceId) {
    checks.push(`${C.red("■")} Database     ${C.dim("—")} MISSING`);
  } else {
    checks.push(`${C.yellow("■")} Database     ${C.dim("—")} Deferred until first hook`);
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

  checks.push(`${C.green("■")} LLM (deep)   ${C.dim("—")} ${models.highModel}`);
  checks.push(`${C.green("■")} LLM (fast)   ${C.dim("—")} ${models.lowModel}`);

  const _valInstanceId = (process.env.QUAID_INSTANCE || "").trim();
  const _platformKey = String(resolvedInstallerPlatform() || "").trim().toLowerCase();
  const _configCheckPath = _valInstanceId
    ? instanceConfigPath(_valInstanceId)
    : (_platformKey
        ? path.join(WORKSPACE, "shared", "config", _platformKey, "config.json")
        : path.join(WORKSPACE, "shared", "config", "global", "config.json"));
  if (fs.existsSync(_configCheckPath)) {
    checks.push(`${C.green("■")} Config       ${C.dim("—")} OK`);
  } else {
    checks.push(`${C.red("■")} Config       ${C.dim("—")} MISSING`);
  }

  checks.push(`${C.green("■")} Owner        ${C.dim("—")} ${owner.display} (${owner.id})`);

  const enabledSystems = Object.entries(systems).filter(([,v]) => v).map(([k]) => k).join(", ");
  checks.push(`${C.green("■")} Systems      ${C.dim("—")} ${enabledSystems}`);

  s.stop(C.green("Health checks complete"));
  note(checks.join("\n"), C.bmag("STATUS"));

  // Gateway must be reachable before the smoke test. Give it a short window to settle
  // after any install-triggered restart before bailing — a genuinely missing gateway
  // is an OpenClaw problem, not a Quaid install problem.
  if (_isPlatform("openclaw")) {
    s.start("Confirming OpenClaw gateway is online...");
    if (!(await waitForGatewayWarmup(15_000))) {
      s.stop(C.red("OpenClaw gateway unavailable"));
      cancel(
        "OpenClaw gateway is not running or not reachable.\n" +
        "Start the OpenClaw gateway and re-run the installer.\n" +
        "This is not a Quaid issue — Quaid requires the OC gateway to be up before installing."
      );
      process.exit(1);
    }
    s.stop(C.green("OpenClaw gateway reachable"));
  }
  s.start("Smoke test (store + recall)...");
  const smokeScript = `
import os, sys
${PY_ENV_SETUP}
os.environ['QUAID_QUIET'] = '1'
sys.path.insert(0, '.')
from datastore.memorydb.memory_graph import store, recall
owner_id = os.environ.get('QUAID_INSTALLER_SMOKE_OWNER_ID', '')
try:
    store('Quaid installer smoke test fact', owner_id=owner_id, category='fact', source='installer-test')
    results = recall('installer smoke test', owner_id=owner_id, limit=1)
    if results:
        print('OK')
    else:
        print('PARTIAL')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
    print('PARTIAL')
`;
  const smoke = python3Spawn(["-c", smokeScript], {
    cwd: PLUGIN_DIR,
    encoding: "utf8",
    env: { ...process.env, QUAID_INSTALLER_SMOKE_OWNER_ID: String(owner.id || "") },
    stdio: ["pipe", "pipe", "pipe"],
  });
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

  // Start the extraction daemon for platforms that rely on background
  // session/lifecycle processing so the first real session is ready immediately.
  const validationAdapterType = models?.adapterType || resolvedInstallerPlatform();
  if (_validationInstanceId && shouldStartExtractionDaemonAfterInstall(validationAdapterType)) {
    s.start("Starting extraction daemon...");
    const daemonScript = `
import os, sys
${PY_ENV_SETUP}
sys.path.insert(0, '.')
try:
    from core.extraction_daemon import ensure_alive
    ensure_alive()
    print('OK')
except Exception as e:
    print(f'warn: {e}', file=sys.stderr)
    print('SKIP')
`;
    const daemon = python3Spawn(["-c", daemonScript], { cwd: PLUGIN_DIR, encoding: "utf8", stdio: ["pipe", "pipe", "pipe"] });
    const daemonResult = (daemon.stdout || "").trim();
    if (daemonResult === "OK") {
      s.stop(C.green("Extraction daemon started"));
    } else {
      s.stop(C.yellow("Extraction daemon skipped — will start on first hook invocation"));
    }
  } else if (shouldStartExtractionDaemonAfterInstall(validationAdapterType)) {
    log.info("Skipping extraction daemon startup until the first real instance is created by hook use.");
  }

  if (_isPlatform("openclaw")) {
    const pluginState = _readOpenClawPluginState();
    const pluginDirectRegistrationOk = _openClawPluginDirectRegistrationOk(pluginState);
    if (!pluginDirectRegistrationOk) {
      s.stop(C.red("OpenClaw plugin validation failed"));
      throw new Error(
        "OpenClaw Quaid plugin is not fully registered after install "
        + `(extensionExists=${pluginState.extensionExists}, `
        + `pluginEnabled=${pluginState.pluginEnabled}, `
        + `memorySlotBound=${pluginState.memorySlotBound}, `
        + `installPathExists=${pluginState.installPathExists}, `
        + `pluginListCheckOk=${pluginState.pluginListCheckOk}, `
        + `pluginListed=${pluginState.pluginListed})`
      );
    }
    _warnOpenClawPluginListDiagnostic(pluginState, "post-install validation");
  }

  // Clear any deferred notices generated during install (smoke test, janitor
  // catch-up, etc.) so they don't contaminate the first real user session.
  const notesDir = path.join(WORKSPACE, ".runtime", "notes");
  const deferredPath = path.join(notesDir, "delayed-llm-requests.json");
  try {
    if (fs.existsSync(deferredPath)) {
      fs.unlinkSync(deferredPath);
      log.info("Cleared install-time deferred notices");
    }
  } catch {}

  const nextSteps = [
    `${C.bcyan("→")} Read the quick guide: ${C.bcyan("projects/quaid/USER-GUIDE.md")}`,
    `${C.bcyan("→")} Facts are extracted automatically on context compaction and new sessions`,
    `${C.bcyan("→")} The nightly janitor reviews, deduplicates, and maintains memories`,
    `${C.bcyan("→")} ${C.bold("It is best to use your agents for any Quaid config changes.")}`,
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
  const cliBin = shell("command -v openclaw 2>/dev/null") || "";
  if (cliBin) {
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

function gatewayHasHookSymbols(gwDir) {
  for (const sub of ["dist", "src"]) {
    const dir = path.join(gwDir, sub);
    if (!fs.existsSync(dir)) continue;
    const out = shell(`grep -rl "runBeforeCompaction\\|before_compaction" "${dir}" 2>/dev/null | head -1`);
    if (out) return true;  // before_compaction is the critical hook
  }
  // OpenClaw 2026.4+ bundles/minifies lifecycle internals and no longer keeps
  // stable hook symbol strings in dist/. The version gate above is the product
  // capability gate; this grep is diagnostic only.
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
  // OpenClaw v2026.4.5+ rejects unknown keys under plugins.entries.<id>.
  // Historical installers wrote plugins.entries.quaid.hooks, which causes
  // startup schema errors. Keep this helper as a no-op sanitization pass.
  const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
  log.info("Ensuring OpenClaw plugin entry is schema-safe");
  try {
    const raw = fs.readFileSync(cfgPath, "utf8");
    const parsed = JSON.parse(raw);
    const pluginEntry = parsed?.plugins?.entries?.quaid;
    let changed = false;
    if (pluginEntry && typeof pluginEntry === "object" && Object.prototype.hasOwnProperty.call(pluginEntry, "hooks")) {
      delete pluginEntry.hooks;
      changed = true;
    }

    if (changed) {
      const tmpPath = `${cfgPath}.tmp-hooks-${process.pid}-${Date.now()}`;
      fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
      fs.renameSync(tmpPath, cfgPath);
    }
  } catch (err) {
    throw new Error(`Could not enable required hooks via direct config write: ${String(err)}`);
  }
}

function resolveHostBinary(cmd, extraCandidates = []) {
  const quoted = String(cmd || "").trim();
  if (!quoted) return "";
  try {
    const found = spawnSync("sh", ["-c", `command -v '${quoted.replace(/'/g, "'\\''")}'`], {
      stdio: "pipe",
      encoding: "utf8",
    });
    const resolved = String(found.stdout || "").trim();
    if (found.status === 0 && resolved) return resolved;
  } catch {}

  const candidates = [...extraCandidates];
  try {
    const npmPrefix = spawnSync("npm", ["config", "get", "prefix"], { stdio: "pipe", encoding: "utf8" });
    const prefix = String(npmPrefix.stdout || "").trim();
    if (npmPrefix.status === 0 && prefix && prefix !== "undefined" && prefix !== "null") {
      candidates.push(path.join(prefix, "bin", quoted));
    }
  } catch {}

  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      if (fs.existsSync(candidate)) return candidate;
    } catch {}
  }
  return "";
}

function resolvePython3Binary() {
  for (const candidate of ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]) {
    try {
      if (candidate && fs.existsSync(candidate)) return candidate;
    } catch {}
  }
  return resolveHostBinary("python3");
}

function python3Command() {
  return resolvePython3Binary() || "python3";
}

function python3Spawn(args, options = {}) {
  return spawnSync(python3Command(), args, options);
}

function _shellQuote(value) {
  return `'${String(value || "").replace(/'/g, `'\\''`)}'`;
}

function buildClaudeCliWrapper(target) {
  try {
    const realTarget = fs.realpathSync(target);
    if (!realTarget || path.basename(realTarget) !== "cli.js" || !fs.existsSync(realTarget)) {
      return "";
    }
    return [
      "#!/bin/sh",
      `exec ${_shellQuote(process.execPath)} ${_shellQuote(realTarget)} \"$@\"`,
      "",
    ].join("\n");
  } catch {
    return "";
  }
}

function ensureCliShim(target, shimName, options = {}) {
  try {
    if (!target || !fs.existsSync(target)) {
      return "";
    }
    const preferredDirs = Array.from(new Set([
      ...String(process.env.PATH || "")
        .split(path.delimiter)
        .map((entry) => String(entry || "").trim())
        .filter(Boolean),
      path.join(os.homedir(), "bin"),
      path.join(os.homedir(), ".local", "bin"),
    ]));

    let shimDir = "";
    let selfTargetCollision = false;
    const resolvedTargetPath = path.resolve(String(target));
    for (const candidate of preferredDirs) {
      if (!candidate) continue;
      const normalized = candidate.replace(/^~(?=$|\/)/, os.homedir());
      if (
        normalized !== path.join(os.homedir(), "bin")
        && normalized !== path.join(os.homedir(), ".local", "bin")
        && normalized !== "/usr/local/bin"
        && normalized !== "/opt/homebrew/bin"
      ) {
        continue;
      }
      const candidateShimPath = path.join(normalized, shimName);
      if (path.resolve(candidateShimPath) === resolvedTargetPath) {
        selfTargetCollision = true;
        continue;
      }
      try {
        fs.mkdirSync(normalized, { recursive: true });
        fs.accessSync(normalized, fs.constants.W_OK);
        shimDir = normalized;
        break;
      } catch {
        // Keep looking for a writable PATH directory.
      }
    }

    if (!shimDir) {
      shimDir = path.join(os.homedir(), "bin");
      fs.mkdirSync(shimDir, { recursive: true });
    }

    const shimPath = path.join(shimDir, shimName);
    fs.rmSync(shimPath, { force: true });
    const wrapperScript = selfTargetCollision && typeof options.wrapperScript === "string"
      ? String(options.wrapperScript)
      : "";
    if (wrapperScript) {
      fs.writeFileSync(shimPath, wrapperScript, { encoding: "utf8", mode: 0o755 });
      fs.chmodSync(shimPath, 0o755);
    } else {
      fs.symlinkSync(target, shimPath);
    }

    // Ensure ~/bin is in the PATH exposed to OC agents via env.vars in openclaw.json.
    // OC's bash tool inherits this PATH so agents can call `quaid` without a full path.
    try {
      const cfgPath = path.join(os.homedir(), ".openclaw", "openclaw.json");
      if (fs.existsSync(cfgPath)) {
        const parsed = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
        const env = parsed.env || (parsed.env = {});
        const vars = env.vars || (env.vars = {});
        const existing = String(vars.PATH || "").trim();
        if (!existing.includes(shimDir)) {
          vars.PATH = existing ? `${shimDir}:${existing}` : `${shimDir}:/usr/local/bin:/usr/bin:/bin`;
          const tmpPath = `${cfgPath}.tmp-shim-${process.pid}-${Date.now()}`;
          fs.writeFileSync(tmpPath, JSON.stringify(parsed, null, 2) + "\n", "utf8");
          fs.renameSync(tmpPath, cfgPath);
          log.info(`Added ${shimDir} to OC agent PATH`);
        }
      }
    } catch {
      // PATH update is best-effort; shim still works via full path
    }

    return shimPath;
  } catch {
    return "";
  }
}

function ensureQuaidCliShim(pluginDirPath) {
  const target = path.join(pluginDirPath, "quaid");
  return ensureCliShim(target, "quaid");
}

function ensureClaudeCliShim() {
  const target = resolveHostBinary("claude", [
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
    path.join(os.homedir(), ".npm-global", "bin", "claude"),
    path.join(os.homedir(), ".local", "bin", "claude"),
  ]);
  return target ? ensureCliShim(target, "claude", { wrapperScript: buildClaudeCliWrapper(target) }) : "";
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
  // can coexist — each install's hooks point to its own quaid script.
  // Do not hardcode QUAID_INSTANCE into the hook command itself: CC project
  // settings own that env var so hooks stay per-project instead of leaking
  // across every workspace on the machine.
  const quaidBin = path.join(PLUGIN_DIR, "quaid");
  const quaidCmd = fs.existsSync(quaidBin) ? quaidBin : "quaid";
  const envPrefix = [
    `QUAID_HOME='${WORKSPACE}'`,
    `QUAID_VISIBLE_HOME='${VISIBLE_HOME}'`,
    "CLAUDE_PROJECT_DIR=\"${CLAUDE_PROJECT_DIR:-$PWD}\"",
  ].join(" ");

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
    SessionEnd: [{
      matcher: "",
      hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-extract` }],
    }],
    SubagentStart: [{
      matcher: "",
      hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-subagent-start` }],
    }],
    SubagentStop: [{
      matcher: "",
      hooks: [{ type: "command", command: `${envPrefix} ${quaidCmd} hook-subagent-stop` }],
    }],
  };

  let changed = false;
  for (const [event, hookList] of Object.entries(desiredHooks)) {
    if (!settings.hooks[event]) {
      settings.hooks[event] = hookList;
      changed = true;
    } else {
      // Check if quaid hooks already exist for this event
      const managedHookRe = /(hook-session-init|hook-inject|hook-extract|hook-subagent-start|hook-subagent-stop)/;
      settings.hooks[event] = settings.hooks[event]
        .map((entry) => {
          const hooks = Array.isArray(entry.hooks) ? entry.hooks.filter((h) => !managedHookRe.test(String(h?.command || ""))) : [];
          return { ...entry, hooks };
        })
        .filter((entry) => (entry.hooks || []).length > 0);
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

  // Write QUAID_HOME and PATH to the env block so all CC processes (hooks,
  // Bash tool calls, and agent ad-hoc CLI invocations) can find quaid.
  // QUAID_INSTANCE is NOT written here — it is derived per-project at
  // runtime via adapter.get_instance_name() reading CLAUDE_PROJECT_DIR.
  if (!settings.env) settings.env = {};
  if (settings.env.QUAID_HOME !== WORKSPACE) {
    settings.env.QUAID_HOME = WORKSPACE;
    changed = true;
  }
  if (settings.env.QUAID_VISIBLE_HOME !== VISIBLE_HOME) {
    settings.env.QUAID_VISIBLE_HOME = VISIBLE_HOME;
    changed = true;
  }
  // Add PLUGIN_DIR to PATH so `quaid` is callable without full path.
  const pluginBinDir = path.dirname(quaidBin);
  const existingPath = settings.env.PATH || "";
  if (!existingPath.includes(pluginBinDir)) {
    settings.env.PATH = existingPath
      ? `${pluginBinDir}:/opt/homebrew/bin:${existingPath}`
      : `${pluginBinDir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`;
    changed = true;
  }
  // Always remove QUAID_INSTANCE — it must not be baked into settings.env.
  // Instance identity is now derived per-project at runtime via
  // adapter.get_instance_name() reading CLAUDE_PROJECT_DIR.
  if ("QUAID_INSTANCE" in settings.env) {
    delete settings.env.QUAID_INSTANCE;
    changed = true;
  }

  if (changed) {
    fs.mkdirSync(path.dirname(settingsPath), { recursive: true });
    fs.writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
    log.info(`Claude Code hooks configured in ${settingsPath}`);
  } else {
    log.info("Claude Code hooks already configured");
  }

}

function setupCodexHooks() {
  const postinstallPath = path.join(
    PLUGIN_DIR,
    "adaptors",
    "manifests",
    "codex",
    "hooks",
    "postinstall.mjs",
  );
  if (!fs.existsSync(postinstallPath)) {
    throw new Error(`Codex postinstall hook script not found: ${postinstallPath}`);
  }
  const result = spawnSync(process.execPath || "node", [postinstallPath], {
    encoding: "utf8",
    stdio: "pipe",
    env: {
      ...process.env,
      QUAID_HOME: WORKSPACE,
      QUAID_VISIBLE_HOME: VISIBLE_HOME,
      OPENCLAW_WORKSPACE: WORKSPACE,
    },
  });
  if (result.status !== 0) {
    const detail = String(result.stderr || result.stdout || "").trim();
    throw new Error(detail || "Codex hooks setup failed");
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

function _isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function _deepDiffOverrides(base, target) {
  if (Array.isArray(target)) {
    if (!Array.isArray(base)) return target;
    const sameLength = base.length === target.length;
    if (!sameLength) return target;
    for (let i = 0; i < target.length; i += 1) {
      const left = base[i];
      const right = target[i];
      if (_isPlainObject(right) || Array.isArray(right)) {
        const nested = _deepDiffOverrides(left, right);
        if (nested !== undefined) return target;
      } else if (left !== right) {
        return target;
      }
    }
    return undefined;
  }
  if (_isPlainObject(target)) {
    if (!_isPlainObject(base)) return target;
    const out = {};
    for (const [key, value] of Object.entries(target)) {
      const nested = _deepDiffOverrides(base[key], value);
      if (nested !== undefined) out[key] = nested;
    }
    return Object.keys(out).length > 0 ? out : undefined;
  }
  return base === target ? undefined : target;
}

function writeConfig(owner, models, embeddings, systems, janitorPolicies = null) {
  const resolvedAdapterType = models.adapterType || resolvedInstallerPlatform() || "standalone";
  const adapterCapabilities = {};
  if (resolvedAdapterType === "openclaw") {
    // Preserve OC matrix transcript mirroring by default on fresh installs.
    // Lean per-instance configs rely on shared platform config layering.
    adapterCapabilities.preserve_transcript_mirror_session_prefixes = ["agent:main:matrix:channel:"];
  }
  const adapterConfig = { type: resolvedAdapterType };
  if (Object.keys(adapterCapabilities).length > 0) {
    adapterConfig.capabilities = adapterCapabilities;
  }
  const adapterPluginId = resolvedAdapterType === "openclaw"
    ? "openclaw.adapter"
    : resolvedAdapterType === "claude-code"
      ? "claude_code.adapter"
      : resolvedAdapterType === "codex"
        ? "codex.adapter"
        : "";
  const policies = janitorPolicies || {
    coreMarkdownWrites: "auto",
    projectDocsWrites: "auto",
    destructiveMemoryOps: "auto",
  };
  const config = {
    adapter: adapterConfig,
    plugins: {
      enabled: true,
      strict: true,
      apiVersion: 1,
      // Include module path explicitly; pathlib rglob does not reliably recurse
      // into symlinked plugin dirs across environments.
      paths: ["modules/quaid", "plugins"],
      allowList: ["memorydb.core", "docsdb.core", "core.extract", "openclaw.adapter", "claude_code.adapter", "codex.adapter"],
      slots: {
        adapter: adapterPluginId,
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
      fastReasoningEffort: models.fastReasoningEffort,
      deepReasoningEffort: models.deepReasoningEffort,
      fastReasoningContext: 200000,
      deepReasoningContext: 200000,
      fastReasoningMaxOutput: 8192,
      deepReasoningMaxOutput: 16384,
      batchBudgetPercent: 0.5,
    },
    capture: {
      enabled: true,
      strictness: "high",
      chunk_tokens: 8000,
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
      fail_hard: true,
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
      domains: {
        personal: "identity, preferences, relationships, life events",
        technical: "code, infra, APIs, architecture",
        project: "project status, tasks, files, milestones",
        work: "job/team/process decisions not deeply technical",
        health: "training, injuries, routines, wellness",
        finance: "budgeting, purchases, salary, bills",
        travel: "trips, moves, places, logistics",
        schedule: "dates, appointments, deadlines",
        research: "options considered, comparisons, tradeoff analysis",
        household: "home, chores, food planning, shared logistics",
        legal: "contracts, policy, and regulatory constraints",
      },
    },
    logging: {
      enabled: true,
      level: "info",
      retentionDays: 30,
      components: ["memory", "janitor"],
    },
    notifications: {
      level: models.notifLevel,
      janitor: { verbosity: models.notifConfig?.janitor ?? "summary", channel: models.notifChannel || "default" },
      extraction: { verbosity: models.notifConfig?.extraction ?? "summary", channel: models.notifChannel || "default" },
      retrieval: { verbosity: models.notifConfig?.retrieval ?? "off", channel: models.notifChannel || "default" },
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
          "ENVIRONMENT.md": { purpose: "Learned behaviors, environment observations, and shared history", maxLines: 100 },
        },
      },
      journal: {
        enabled: true,
        snippetsEnabled: true,
        mode: "distilled",
        journalDir: "journal",
        targetFiles: ["SOUL.md", "USER.md", "ENVIRONMENT.md"],
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
    rag: {
      docsDir: "docs",
      chunkMaxTokens: 800,
      chunkOverlapTokens: 100,
      maxResults: 5,
      searchLimit: 5,
      minSimilarity: 0.3,
    },
  };

  // Write exhaustive defaults to shared/global (fallback layer), and keep
  // shared/platform config thin with only platform-specific overrides.
  const platformKey = resolvedInstallerPlatform();
  const sharedGlobalConfigDir = path.join(WORKSPACE, "shared", "config", "global");
  const sharedGlobalConfigPath = path.join(sharedGlobalConfigDir, "config.json");
  const sharedPlatformConfigDir = path.join(WORKSPACE, "shared", "config", platformKey);
  const sharedPlatformConfigPath = path.join(sharedPlatformConfigDir, "config.json");
  const ollamaBlock = {
    url: (process.env.OLLAMA_URL || "http://localhost:11434").replace(/\/v1\/?$/, "").replace(/\/+$/, ""),
    embeddingModel: embeddings.embedModel,
    embeddingDim: embeddings.embedDim,
  };
  const baseGlobalConfig = JSON.parse(JSON.stringify(config));
  delete baseGlobalConfig.adapter;
  if (_isPlainObject(baseGlobalConfig.plugins) && _isPlainObject(baseGlobalConfig.plugins.slots)) {
    delete baseGlobalConfig.plugins.slots.adapter;
  }
  baseGlobalConfig.ollama = ollamaBlock;

  const existingGlobalConfig = readJsonObject(sharedGlobalConfigPath) || {};
  const mergedGlobalConfig = deepMergeMissing(baseGlobalConfig, existingGlobalConfig);
  if (JSON.stringify(existingGlobalConfig) !== JSON.stringify(mergedGlobalConfig)) {
    writeJsonObject(sharedGlobalConfigPath, mergedGlobalConfig);
    log.info(`Updated shared global base config: ${sharedGlobalConfigPath}`);
  } else {
    log.info(C.dim(`Shared global base config already contains installer defaults (${sharedGlobalConfigPath})`));
  }

  let sharedGlobalCfg = mergedGlobalConfig;
  if (!fs.existsSync(sharedPlatformConfigPath)) {
    fs.mkdirSync(sharedPlatformConfigDir, { recursive: true });
    fs.writeFileSync(sharedPlatformConfigPath, "{}\n");
    log.info(`Created blank shared platform config: ${sharedPlatformConfigPath}`);
  }

  // Keep platform config thin. Write only deltas from shared/global plus the
  // adapter identity + adapter plugin slot required for platform resolution.
  const platformConfig = _deepDiffOverrides(sharedGlobalCfg, config) || {};
  platformConfig.adapter = adapterConfig;
  const platformPlugins = _isPlainObject(platformConfig.plugins) ? platformConfig.plugins : {};
  platformPlugins.slots = _isPlainObject(platformPlugins.slots) ? platformPlugins.slots : {};
  platformPlugins.slots.adapter = adapterPluginId;
  platformConfig.plugins = platformPlugins;
  const platformConfigJson = JSON.stringify(platformConfig, null, 2) + "\n";

  // Write runtime platform overrides to shared platform config only. Instance
  // silos are created on first hook use and should not get install-time copies
  // of globally tunable settings.
  if (!fs.existsSync(sharedPlatformConfigPath) || fs.readFileSync(sharedPlatformConfigPath, "utf8").trim() === "{}") {
    fs.writeFileSync(sharedPlatformConfigPath, platformConfigJson);
    log.info(`Wrote shared platform override config: ${sharedPlatformConfigPath}`);
  }
}

function copyDirSync(src, dest, rel = "") {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (
      entry.name === "node_modules"
      || entry.name === ".git"
      || entry.name === "__pycache__"
      || entry.name === ".pytest_cache"
      || entry.name === ".tmp"
      || (rel === "" && entry.name === "tests")
      || (rel === "" && entry.name === "scripts")
      || entry.name.endsWith(".pyc")
    ) continue;
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      const nextRel = rel ? path.join(rel, entry.name) : entry.name;
      copyDirSync(srcPath, destPath, nextRel);
    } else if (entry.isFile() || entry.isSymbolicLink()) {
      // copyFileSync follows symlinks (dereferences to real content) — this is
      // intentional so that the dest dir contains real files, not broken links.
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function copyMissingDirSync(src, dest) {
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    if (
      entry.name === "node_modules"
      || entry.name === ".git"
      || entry.name === "__pycache__"
      || entry.name === ".pytest_cache"
      || entry.name === ".tmp"
      || entry.name.endsWith(".pyc")
    ) continue;
    const srcPath = path.join(src, entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyMissingDirSync(srcPath, destPath);
    } else if ((entry.isFile() || entry.isSymbolicLink()) && !fs.existsSync(destPath)) {
      fs.copyFileSync(srcPath, destPath);
    }
  }
}

function ensureProjectSeedFileFromTemplate(sourceDir, destDir, relPath, fallbackText = "") {
  const destPath = path.join(destDir, relPath);
  if (fs.existsSync(destPath)) return;
  const srcPath = sourceDir ? path.join(sourceDir, relPath) : "";
  fs.mkdirSync(path.dirname(destPath), { recursive: true });
  if (srcPath && fs.existsSync(srcPath)) {
    fs.copyFileSync(srcPath, destPath);
    return;
  }
  if (fallbackText) {
    fs.writeFileSync(destPath, fallbackText.trimEnd() + "\n", "utf8");
  }
}

function ensureVisibleIdentityStubs(visibleRoot) {
  fs.mkdirSync(visibleRoot, { recursive: true });
  const created = [];
  for (const f of ["SOUL.md", "USER.md", "ENVIRONMENT.md"]) {
    const fp = path.join(visibleRoot, f);
    if (fs.existsSync(fp)) continue;
    fs.writeFileSync(fp, `# ${f.replace(".md", "")}\n`, "utf8");
    created.push(f);
  }
  return created;
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

function _stabilizePostInstallExtractionState(instanceId = "") {
  const resolved = String(instanceId || resolvedInstallerInstanceId()).trim();
  if (!resolved) {
    return { cursorsSeeded: 0, pendingSignalsCleared: 0, timeoutBuffersCleared: 0 };
  }
  const dataDir = hiddenInstanceDataDir(resolved);
  const sessionMessagesDir = path.join(hiddenInstanceLogsDir(resolved), "quaid", "sessions");
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
  if (!_isPlatform("openclaw") || !canRun("curl")) return true;
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
  return shell("command -v openclaw 2>/dev/null") || "";
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

function sendInstallerAgentNotice(message, options = {}) {
  if (!AGENT_MODE) return false;
  if (String(process.env.QUAID_INSTALL_NOTIFY || "1").trim() === "0") return false;

  const severity = String(options.severity || "warning").trim().toLowerCase() || "warning";
  const source = String(options.source || "installer").trim() || "installer";
  const dedupeKey = String(options.dedupeKey || "").trim();
  const ttlSeconds = Number(options.ttlSeconds || 900);
  const pythonRoot = path.join(__dirname, "modules", "quaid");
  const py = `
import os, sys
sys.path.insert(0, ${JSON.stringify(pythonRoot)})
from core.runtime.notify import notify_agent
ok = notify_agent(
    ${JSON.stringify(String(message || ""))},
    severity=${JSON.stringify(severity)},
    source=${JSON.stringify(source)},
    dedupe_key=${dedupeKey ? JSON.stringify(dedupeKey) : "None"},
    ttl_seconds=${Number.isFinite(ttlSeconds) ? Math.max(0, Math.trunc(ttlSeconds)) : 900},
)
print("ok" if ok else "unavailable")
`;
  const env = { ...process.env };
  const sep = process.platform === "win32" ? ";" : ":";
  env.QUAID_HOME = WORKSPACE;
  env.OPENCLAW_WORKSPACE = WORKSPACE;
  env.PYTHONPATH = env.PYTHONPATH ? `${pythonRoot}${sep}${env.PYTHONPATH}` : pythonRoot;
  const installInstance = String(process.env.QUAID_INSTANCE || "").trim();
  if (installInstance) env.QUAID_INSTANCE = env.QUAID_INSTANCE || installInstance;
  else delete env.QUAID_INSTANCE;

  const res = python3Spawn(["-c", py], {
    encoding: "utf8",
    stdio: ["pipe", "pipe", "pipe"],
    env,
    timeout: 5_000,
  });
  if (res.error) {
    log.warn(`Installer agent notice unavailable: ${String(res.error.message || res.error)}`);
    return false;
  }
  if (res.status !== 0) {
    const detail = String(res.stderr || res.stdout || "").trim();
    log.warn(`Installer agent notice unavailable: ${detail || "python exited non-zero"}`);
    return false;
  }
  return String(res.stdout || "").trim() === "ok";
}

function sendInstallerNotification(message) {
  if (!AGENT_MODE || !_isPlatform("openclaw")) return false;
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
  env.OPENCLAW_WORKSPACE = WORKSPACE;
  env.PYTHONPATH = env.PYTHONPATH ? `${PLUGIN_DIR}${sep}${env.PYTHONPATH}` : PLUGIN_DIR;

  const res = python3Spawn(["-c", py], {
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
    `Quaid home: ${WORKSPACE}`,
    `Models: deep=${models.highModel}, fast=${models.lowModel}`,
    `Embeddings: ${embeddings.embedModel}`,
    "No memory mutants detected.",
  ];
  if (_isPlatform("openclaw")) {
    summary.push(`Notification channel: ${models.notifChannel || "default"}`);
  }
  sendInstallerNotification(summary.join("\n"));
}

function notifyInstallWarmupNotice() {
  if (!_isPlatform("openclaw")) return;
  if (String(process.env.QUAID_INSTALL_NOTIFY_PROGRESS || "1").trim() === "0") return;
  sendInstallerNotification(
    "⏳ Quaid install needs to restart the OpenClaw gateway to apply changes.\n" +
    "This pause is expected and can take 2-5 minutes while the gateway comes back online."
  );
}

// =============================================================================
// Install Plan
// =============================================================================

/**
 * Build a structured install plan from the resolved step outputs.
 * Written to disk after a real install and printed during --dry-run.
 * Comparing a dry-run plan (interactive mode) against a real agent-mode
 * install plan is the recommended way to verify parity between modes:
 *   node setup-quaid.mjs --dry-run > /tmp/plan-interactive.json
 *   cat WORKSPACE/runtime/last-install-plan.json
 * Any key divergence (e.g. an option enabled by agent mode that interactive
 * would not have offered) indicates a parity bug to investigate.
 */
function buildInstallPlan(pluginSrc, owner, models, embeddings, systems, schedule) {
  const platform = resolvedInstallerPlatform();
  const instanceId = String(process.env.QUAID_INSTANCE || "").trim();
  const authTokenPath = platform === "codex"
    ? path.join(WORKSPACE, "adaptors", "codex", ".auth-token")
    : path.join(WORKSPACE, "adaptors", "claude-code", ".auth-token");
  const authTokenPresent = (() => {
    try { return !!fs.readFileSync(authTokenPath, "utf8").trim(); } catch { return false; }
  })();

  return {
    schemaVersion: 1,
    mode: AGENT_MODE ? "agent" : "interactive",
    dryRun: DRY_RUN,
    existingInstallDetected: _existingInstallDetected,
    platform,
    workspace: WORKSPACE,
    instanceId,
    owner: owner?.display || owner?.name || null,
    models: {
      fast: models?.lowModel || null,
      deep: models?.highModel || null,
      provider: models?.provider || null,
    },
    notifications: {
      level: models?.notifLevel || null,
      janitor: models?.notifConfig?.janitor || null,
      extraction: models?.notifConfig?.extraction || null,
      retrieval: models?.notifConfig?.retrieval || null,
      channel: models?.notifChannel || null,
    },
    embeddings: {
      model: embeddings?.embedModel || null,
      provider: embeddings?.embedProvider || null,
      dim: embeddings?.embedDim || null,
    },
    options: {
      timeoutCompaction: models?.autoCompactionOnTimeout ?? false,
      janitorEnabled: !!(schedule?.janitorEnabled ?? true),
      journalEnabled: !!(systems?.journal ?? true),
      projectsEnabled: !!(systems?.projects ?? true),
    },
    authToken: {
      required: platform === "claude-code" || platform === "codex",
      present: authTokenPresent,
    },
    platformCapabilities: {
      supportsTimeoutCompaction: _platformSupportsTimeoutCompaction(platform),
      usesHostManagedLlm: _platformUsesHostManagedLlmByDefault(platform),
    },
    compatibilityWarnings: _adapterCompatibilityWarnings(platform),
    janitor: {
      askFirst: models?.janitorAskFirst ?? null,
      scheduleHour: schedule?.hour ?? null,
      scheduled: schedule?.scheduled ?? null,
      approvalPolicies: schedule?.approvalPolicies || null,
    },
    // Parity flags: raised when agent mode produces a plan that interactive
    // would not have offered. Consumers should treat any true flag as a bug.
    parityWarnings: (() => {
      const warnings = [];
      if ((models?.autoCompactionOnTimeout) && !_platformSupportsTimeoutCompaction(platform)) {
        warnings.push("timeoutCompaction enabled on platform that does not support it");
      }
      return warnings;
    })(),
    generatedAt: new Date().toISOString(),
  };
}

function formatPreInstallSurvey(plan) {
  const lines = ["Pre-install survey", ""];
  const modelBits = [];
  if (plan?.models?.provider) modelBits.push(`provider ${plan.models.provider}`);
  if (plan?.models?.deep) modelBits.push(`deep ${plan.models.deep}`);
  if (plan?.models?.fast) modelBits.push(`fast ${plan.models.fast}`);

  const embedBits = [];
  if (plan?.embeddings?.provider) embedBits.push(plan.embeddings.provider);
  if (plan?.embeddings?.model) embedBits.push(plan.embeddings.model);
  if (plan?.embeddings?.dim) embedBits.push(`${plan.embeddings.dim} dim`);

  const notifParts = [];
  if (plan?.notifications?.level) notifParts.push(`level ${plan.notifications.level}`);
  if (plan?.notifications?.janitor) notifParts.push(`janitor ${plan.notifications.janitor}`);
  if (plan?.notifications?.extraction) notifParts.push(`extraction ${plan.notifications.extraction}`);
  if (plan?.notifications?.retrieval) notifParts.push(`retrieval ${plan.notifications.retrieval}`);

  lines.push(`- Owner name: ${plan?.owner || "unknown"}`);
  lines.push(`- Adapter type: ${plan?.platform || "unknown"}`);
  lines.push(`- LLM provider + deep/fast models: ${modelBits.join(", ") || "unknown"}`);
  lines.push(`- Embeddings provider/model: ${embedBits.join(", ") || "unknown"}`);
  lines.push(`- Notification level + per-feature verbosity: ${notifParts.join(", ") || "unknown"}`);
  if (plan?.platform === "openclaw" && plan?.notifications?.channel) {
    lines.push(`- Notification routing channel: ${plan.notifications.channel}`);
  }
  lines.push(`- Platform compatibility notices: ${(Array.isArray(plan?.compatibilityWarnings) && plan.compatibilityWarnings.length > 0) ? plan.compatibilityWarnings.join(" | ") : "none"}`);
  lines.push("");
  lines.push("Do you want to change any of these before I run install?");
  return lines.join("\n");
}

function _acquireExclusiveInstallLock(lockFile) {
  fs.mkdirSync(path.dirname(lockFile), { recursive: true });
  fs.writeFileSync(lockFile, new Date().toISOString(), { flag: "wx" });
  const release = () => {
    try {
      fs.unlinkSync(lockFile);
    } catch {
      // ignore
    }
  };
  process.on("exit", release);
  process.on("SIGINT", () => {
    release();
    process.exit(130);
  });
  process.on("SIGTERM", () => {
    release();
    process.exit(143);
  });
  return release;
}

function _ensureOpenClawHostConfigLock() {
  if (DRY_RUN || _releaseOpenClawHostLock) return;
  if (!_isPlatform("openclaw")) return;

  const openClawRoot = path.join(os.homedir(), ".openclaw");
  const lockFile = path.join(openClawRoot, ".quaid-installer.lock");
  try {
    _releaseOpenClawHostLock = _acquireExclusiveInstallLock(
      lockFile,
      path.join(openClawRoot, "openclaw.json"),
    );
  } catch (lockErr) {
    if (lockErr.code === "EEXIST") {
      const lockedAt = (() => {
        try {
          return fs.readFileSync(lockFile, "utf8").trim();
        } catch {
          return "unknown";
        }
      })();
      console.error("\n[x] Another installer is already mutating the host OpenClaw config.");
      console.error(`    Config:    ${path.join(openClawRoot, "openclaw.json")}`);
      console.error(`    Lock file: ${lockFile}`);
      console.error(`    Started:   ${lockedAt}`);
      console.error("    Wait for the active installer to finish, or delete the stale lock file if it crashed.");
      process.exit(1);
    }
    console.warn(`[warn] Could not acquire OpenClaw host config lock (${lockErr.message}); proceeding without lock.`);
  }
}

// =============================================================================
// Main
// =============================================================================
async function main() {
  // --- Installer lock: prevent concurrent runs against the same workspace ---
  // Dry-run writes nothing, so it does not need exclusive access.
  const LOCK_FILE = path.join(RUNTIME_DIR, ".installer.lock");
  if (DRY_RUN) {
    // Skip lock in dry-run mode — no writes, no need to block concurrent installs.
  } else try {
    _acquireExclusiveInstallLock(LOCK_FILE);
  } catch (lockErr) {
    if (lockErr.code === "EEXIST") {
      const lockedAt = (() => { try { return fs.readFileSync(LOCK_FILE, "utf8").trim(); } catch { return "unknown"; } })();
      console.error(`\n[x] Another installer is already running against this workspace (${WORKSPACE}).`);
      console.error(`    Lock file: ${LOCK_FILE}`);
      console.error(`    Started:   ${lockedAt}`);
      console.error("    If the previous run crashed, delete the lock file and retry.");
      process.exit(1);
    }
    // Non-EEXIST (e.g. permissions) — warn but continue without lock
    console.warn(`[warn] Could not acquire installer lock (${lockErr.message}); proceeding without lock.`);
  }
  // --- End installer lock ---

  try {
    let continueInstalling = true;
    while (continueInstalling) {
      syncInstallerInstanceEnv();
      if (AGENT_MODE) {
        log.info("Agent mode enabled: using non-interactive defaults where prompts are normally required.");
        log.info(`Quaid home: ${WORKSPACE}`);
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
      let schedule = null;
      if (!_existingInstallDetected) {
        schedule = await step6_schedule(embeddings, false, models.janitorAskFirst);
        notifyInstallCheckpoint(
          5, TOTAL_INSTALL_STEPS, "janitor",
          "Janitor policy and schedule configured. Next step may pause while gateway/plugin restarts and warms up.",
          "Night shift assigned. Warmup can take a minute or two."
        );
      } else {
        notifyInstallCheckpoint(
          5, TOTAL_INSTALL_STEPS, "janitor",
          "Existing install detected — skipping first-install janitor scheduling step.",
          "Reusing existing janitor policy/schedule."
        );
      }
      notifyInstallWarmupNotice();
      if (_isPlatform("openclaw")) {
        log.info("Heads up: OpenClaw gateway now needs a restart to apply changes. A 2-5 minute pause here is expected while it comes back online.");
      }

      // --- Dry-run exit point ---
      // Steps 1-6 (prompts + detection) have run. No writes have occurred.
      // Emit the install plan and exit instead of proceeding to step7.
      if (DRY_RUN) {
        const plan = buildInstallPlan(pluginSrc, owner, models, embeddings, systems, schedule);
        if (plan.parityWarnings.length > 0) {
          log.warn("Parity warnings detected:");
          plan.parityWarnings.forEach(w => log.warn("  ! " + w));
        }
        if (SURVEY_ONLY) {
          console.log(formatPreInstallSurvey(plan));
          process.exit(0);
        }
        note(JSON.stringify(plan, null, 2), "Install Plan (dry run — no changes made)");
        outro(C.green("Dry run complete.") + C.dim(" Re-run without --dry-run to install."));
        process.exit(0);
      }

      await step7_install(pluginSrc, owner, models, embeddings, systems, schedule?.approvalPolicies || null);
      notifyInstallCheckpoint(6, TOTAL_INSTALL_STEPS, "install", "Plugin installed, config written, migration/registration complete.", "Blueprint phase complete.");
      await step8_validate(owner, models, embeddings, systems);
      notifyInstallCheckpoint(7, TOTAL_INSTALL_STEPS, "validation", "Smoke checks passed.", "No richters spotted.");

      // Write the install plan so it can be compared against a dry-run plan.
      // If they diverge (e.g. agent mode enabled an option interactive wouldn't
      // have offered), that is a parity bug to investigate.
      try {
        const planPath = path.join(RUNTIME_DIR, "last-install-plan.json");
        const plan = buildInstallPlan(pluginSrc, owner, models, embeddings, systems, schedule);
        fs.mkdirSync(path.dirname(planPath), { recursive: true });
        fs.writeFileSync(planPath, JSON.stringify(plan, null, 2) + "\n", { encoding: "utf8", mode: 0o644 });
        if (plan.parityWarnings.length > 0) {
          log.warn("Install plan parity warnings (check last-install-plan.json):");
          plan.parityWarnings.forEach(w => log.warn("  ! " + w));
        }
      } catch (e) {
        log.warn("Could not write last-install-plan.json: " + e.message);
      }

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
      continueInstalling = await promptNextPlatformInstall(resolvedInstallerPlatform());
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
