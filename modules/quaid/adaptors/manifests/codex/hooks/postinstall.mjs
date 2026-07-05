#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";

function escapeShellSingle(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function escapeShellDefault(value) {
  return String(value).replace(/["\\$`]/g, "\\$&");
}

function resolveWorkspace() {
  return path.resolve(
    String(
      process.env.QUAID_HOME
      || process.env.QUAID_WORKSPACE
      || process.cwd()
    ).trim(),
  );
}

function resolveQuaidBinary(workspace) {
  const candidates = [
    path.join(workspace, "modules", "quaid", "quaid"),
    path.join(workspace, "plugins", "quaid", "quaid"),
    path.join(workspace, "quaid"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  return "quaid";
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function writeJson(filePath, payload) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, JSON.stringify(payload, null, 2) + "\n", "utf8");
  fs.renameSync(tmpPath, filePath);
}

function pruneManagedHooks(groups, managedCommands) {
  const list = Array.isArray(groups) ? groups : [];
  const kept = [];
  for (const group of list) {
    const hooks = Array.isArray(group?.hooks) ? group.hooks : [];
    const remainingHooks = hooks.filter((hook) => {
      const command = String(hook?.command || "");
      return !managedCommands.some((token) => command.includes(token));
    });
    if (!remainingHooks.length) continue;
    kept.push({ ...group, hooks: remainingHooks });
  }
  return kept;
}

function removeTomlTopLevelKey(text, key) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const re = new RegExp(`^\\s*${key}\\s*=`);
  let inTable = false;
  const kept = [];
  for (const line of lines) {
    if (/^\s*\[/.test(line)) inTable = true;
    if (!inTable && re.test(line)) continue;
    kept.push(line);
  }
  return `${kept.join("\n").replace(/\n*$/, "\n")}`;
}

function upsertTomlBool(text, tableName, key, value) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const tableLine = `[${tableName}]`;
  const valueLine = `${key} = ${value ? "true" : "false"}`;

  let tableIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === tableLine) {
      tableIndex = i;
      break;
    }
  }

  if (tableIndex === -1) {
    const prefix = normalized.trimEnd();
    return `${prefix}${prefix ? "\n\n" : ""}${tableLine}\n${valueLine}\n`;
  }

  let sectionEnd = lines.length;
  for (let i = tableIndex + 1; i < lines.length; i++) {
    if (/^\s*\[[^\]]+\]\s*$/.test(lines[i])) {
      sectionEnd = i;
      break;
    }
  }

  for (let i = tableIndex + 1; i < sectionEnd; i++) {
    if (new RegExp(`^\\s*${key}\\s*=`).test(lines[i])) {
      lines[i] = valueLine;
      return `${lines.join("\n").replace(/\n*$/, "\n")}`;
    }
  }

  lines.splice(sectionEnd, 0, valueLine);
  return `${lines.join("\n").replace(/\n*$/, "\n")}`;
}

function removeTomlBool(text, tableName, key) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const tableLine = `[${tableName}]`;

  let tableIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === tableLine) {
      tableIndex = i;
      break;
    }
  }
  if (tableIndex === -1) {
    return `${lines.join("\n").replace(/\n*$/, "\n")}`;
  }

  let sectionEnd = lines.length;
  for (let i = tableIndex + 1; i < lines.length; i++) {
    if (/^\s*\[[^\]]+\]\s*$/.test(lines[i])) {
      sectionEnd = i;
      break;
    }
  }

  const re = new RegExp(`^\\s*${key}\\s*=`);
  const kept = [
    ...lines.slice(0, tableIndex + 1),
    ...lines.slice(tableIndex + 1, sectionEnd).filter((line) => !re.test(line)),
    ...lines.slice(sectionEnd),
  ];
  return `${kept.join("\n").replace(/\n*$/, "\n")}`;
}

function ensureTomlTable(text, tableName) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const tableLine = `[${tableName}]`;
  if (lines.some((line) => line.trim() === tableLine)) {
    return `${lines.join("\n").replace(/\n*$/, "\n")}`;
  }
  const prefix = normalized.trimEnd();
  return `${prefix}${prefix ? "\n\n" : ""}${tableLine}\n`;
}

function upsertTomlStringInTable(text, tableName, key, quotedValue) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const tableLine = `[${tableName}]`;
  const valueLine = `${key} = ${quotedValue}`;

  let tableIndex = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === tableLine) {
      tableIndex = i;
      break;
    }
  }

  if (tableIndex === -1) {
    const prefix = normalized.trimEnd();
    return `${prefix}${prefix ? "\n\n" : ""}${tableLine}\n${valueLine}\n`;
  }

  let sectionEnd = lines.length;
  for (let i = tableIndex + 1; i < lines.length; i++) {
    if (/^\s*\[[^\]]+\]\s*$/.test(lines[i])) {
      sectionEnd = i;
      break;
    }
  }

  for (let i = tableIndex + 1; i < sectionEnd; i++) {
    if (new RegExp(`^\\s*${key}\\s*=`).test(lines[i])) {
      lines[i] = valueLine;
      return `${lines.join("\n").replace(/\n*$/, "\n")}`;
    }
  }

  lines.splice(sectionEnd, 0, valueLine);
  return `${lines.join("\n").replace(/\n*$/, "\n")}`;
}

function stripManagedHookTomlBlocks(text, managedCommands) {
  const normalized = String(text || "").replace(/\r\n/g, "\n");
  const lines = normalized ? normalized.split("\n") : [];
  const kept = [];
  let block = null;
  const hookEventRe = /^\s*\[\[hooks\.[A-Za-z0-9_-]+\]\]\s*$/;

  const flushBlock = () => {
    if (!block) return;
    const body = block.join("\n");
    if (!managedCommands.some((token) => body.includes(token))) {
      kept.push(...block);
    }
    block = null;
  };

  for (const line of lines) {
    if (hookEventRe.test(line)) {
      flushBlock();
      block = [line];
      continue;
    }
    if (block) {
      if (hookEventRe.test(line) || /^\s*\[[^\]]+\]\s*$/.test(line)) {
        flushBlock();
        kept.push(line);
      } else {
        block.push(line);
      }
      continue;
    }
    kept.push(line);
  }
  flushBlock();
  return `${kept.join("\n").replace(/\n*$/, "\n")}`;
}

function tomlString(value) {
  return JSON.stringify(String(value));
}

function canonicalJson(value) {
  if (Array.isArray(value)) {
    return value.map(canonicalJson);
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      const nested = canonicalJson(value[key]);
      if (nested !== undefined) {
        out[key] = nested;
      }
    }
    return out;
  }
  return value;
}

function sha256TomlVersion(value) {
  const serialized = JSON.stringify(canonicalJson(value));
  return `sha256:${createHash("sha256").update(serialized).digest("hex")}`;
}

function codexHookEventKey(eventName) {
  return String(eventName || "")
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replace(/[-\s]+/g, "_")
    .toLowerCase();
}

function normalizedCodexCommandHookHash(eventName, group, hook) {
  const normalizedHook = {
    type: "command",
    command: String(hook?.command || ""),
    timeout: Math.max(1, Number(hook?.timeout || 600)),
    async: Boolean(hook?.async),
  };
  if (hook?.statusMessage) {
    normalizedHook.statusMessage = String(hook.statusMessage);
  }
  return sha256TomlVersion({
    event_name: codexHookEventKey(eventName),
    ...(group?.matcher ? { matcher: String(group.matcher) } : {}),
    hooks: [normalizedHook],
  });
}

function isManagedQuaidHook(hook, managedCommands) {
  const command = String(hook?.command || "");
  return managedCommands.some((token) => command.includes(token));
}

function upsertCodexHookTrustState(text, hooksPath, hooksConfig, managedCommands) {
  let updated = text;
  const events = hooksConfig?.hooks && typeof hooksConfig.hooks === "object" ? hooksConfig.hooks : {};
  for (const [eventName, groups] of Object.entries(events)) {
    if (!Array.isArray(groups)) continue;
    groups.forEach((group, groupIndex) => {
      const hooks = Array.isArray(group?.hooks) ? group.hooks : [];
      hooks.forEach((hook, handlerIndex) => {
        if (!isManagedQuaidHook(hook, managedCommands)) return;
        const key = `${hooksPath}:${codexHookEventKey(eventName)}:${groupIndex}:${handlerIndex}`;
        const tableName = `hooks.state.${JSON.stringify(key)}`;
        const trustedHash = normalizedCodexCommandHookHash(eventName, group, hook);
        updated = upsertTomlStringInTable(
          updated,
          tableName,
          "trusted_hash",
          JSON.stringify(trustedHash),
        );
        // Quaid owns these hook entries; installation should leave them runnable.
        updated = upsertTomlBool(updated, tableName, "enabled", true);
      });
    });
  }
  return updated;
}

function managedHookTomlBlocks(desired) {
  const blocks = [];
  for (const [eventName, groups] of Object.entries(desired)) {
    for (const group of groups) {
      for (const hook of (Array.isArray(group?.hooks) ? group.hooks : [])) {
        blocks.push(
          `[[hooks.${eventName}]]`,
          `[[hooks.${eventName}.hooks]]`,
          `type = ${tomlString(hook.type || "command")}`,
          `command = ${tomlString(hook.command || "")}`,
          "timeout = 30",
          "async = false",
          "",
        );
      }
    }
  }
  return blocks.join("\n");
}

const workspace = resolveWorkspace();
const quaidBinary = resolveQuaidBinary(workspace);
const quaidCommand = escapeShellSingle(quaidBinary);
const defaultHome = escapeShellDefault(workspace);
const envPrefix = [
  `QUAID_HOME="\${QUAID_HOME:-${defaultHome}}"`,
  `OPENCLAW_WORKSPACE="\${OPENCLAW_WORKSPACE:-${defaultHome}}"`,
  'CODEX_PROJECT_DIR="${CODEX_PROJECT_DIR:-$PWD}"',
].join(" ");

const codexDir = path.join(os.homedir(), ".codex");
const hooksPath = path.join(codexDir, "hooks.json");
const configPath = path.join(codexDir, "config.toml");
const configJsonPath = path.join(codexDir, "config.json");

const managedCommands = [
  "hook-session-init",
  "hook-inject",
  "hook-codex-stop",
  "hook-subagent-start",
  "hook-subagent-stop",
];

const desiredHooks = {
  SessionStart: [
    {
      hooks: [
        {
          type: "command",
          command: `${envPrefix} ${quaidCommand} hook-session-init`,
          statusMessage: "Quaid loading project context",
        },
      ],
    },
  ],
  UserPromptSubmit: [
    {
      hooks: [
        {
          type: "command",
          command: `${envPrefix} ${quaidCommand} hook-inject`,
          statusMessage: "Quaid recalling memory",
        },
      ],
    },
  ],
  Stop: [
    {
      hooks: [
        {
          type: "command",
          command: `${envPrefix} ${quaidCommand} hook-codex-stop`,
          timeout: 120,
        },
      ],
    },
  ],
};

const hooksConfig = readJson(hooksPath, {});
if (!hooksConfig || typeof hooksConfig !== "object" || Array.isArray(hooksConfig)) {
  throw new Error(`Invalid Codex hooks config at ${hooksPath}`);
}
if (!hooksConfig.hooks || typeof hooksConfig.hooks !== "object" || Array.isArray(hooksConfig.hooks)) {
  hooksConfig.hooks = {};
}

for (const [eventName, groups] of Object.entries(desiredHooks)) {
  const existingGroups = pruneManagedHooks(hooksConfig.hooks[eventName], managedCommands);
  hooksConfig.hooks[eventName] = [...existingGroups, ...groups];
}

writeJson(hooksPath, hooksConfig);

const trustCandidates = Array.from(
  new Set(
    [process.cwd(), fs.realpathSync.native(process.cwd())]
      .map((entry) => String(entry || "").trim())
      .filter(Boolean),
  ),
);

const configJson = readJson(configJsonPath, {});
if (!configJson || typeof configJson !== "object" || Array.isArray(configJson)) {
  throw new Error(`Invalid Codex config JSON at ${configJsonPath}`);
}
configJson.hooks = hooksConfig.hooks;
configJson.features = {
  ...(configJson.features && typeof configJson.features === "object" && !Array.isArray(configJson.features)
    ? configJson.features
    : {}),
  hooks: true,
};
delete configJson.features.codex_hooks;
configJson.projects = {
  ...(configJson.projects && typeof configJson.projects === "object" && !Array.isArray(configJson.projects)
    ? configJson.projects
    : {}),
};
for (const candidate of trustCandidates) {
  configJson.projects[candidate] = {
    ...(configJson.projects[candidate] && typeof configJson.projects[candidate] === "object"
      ? configJson.projects[candidate]
      : {}),
    trust_level: "trusted",
  };
}
writeJson(configJsonPath, configJson);

let currentToml = fs.existsSync(configPath) ? fs.readFileSync(configPath, "utf8") : "";
// Keep Codex hooks in hooks.json only. Codex warns and executes duplicate hooks
// when the same events are present in both hooks.json and inline config.toml.
let updatedToml = removeTomlTopLevelKey(currentToml, "hooks");
// Enable the current Codex hooks feature flag. codex_hooks is deprecated and
// ignored by Codex 0.139+.
updatedToml = removeTomlBool(updatedToml, "features", "codex_hooks");
updatedToml = upsertTomlBool(updatedToml, "features", "hooks", true);
updatedToml = stripManagedHookTomlBlocks(updatedToml, managedCommands);
for (const candidate of trustCandidates) {
  updatedToml = upsertTomlStringInTable(
    updatedToml,
    `projects.${JSON.stringify(candidate)}`,
    "trust_level",
    JSON.stringify("trusted"),
  );
}
updatedToml = upsertCodexHookTrustState(
  updatedToml,
  hooksPath,
  hooksConfig,
  managedCommands,
);
if (updatedToml !== currentToml) {
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const tmpPath = `${configPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, updatedToml, "utf8");
  fs.renameSync(tmpPath, configPath);
}

console.log(`[quaid][adapter:codex][postinstall] Codex hooks configured in ${hooksPath}`);
console.log(`[quaid][adapter:codex][postinstall] Codex config mirrored in ${configJsonPath}`);
