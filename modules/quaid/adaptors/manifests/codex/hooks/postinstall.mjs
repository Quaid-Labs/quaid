#!/usr/bin/env node

import fs from "node:fs";
import os from "node:os";
import path from "node:path";

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
  codex_hooks: true,
};
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
// Codex 0.125+ expects inline HookEventsToml. Keep hooks.json for Quaid's own
// introspection, but do not point config.toml at it with the legacy string key.
let updatedToml = removeTomlTopLevelKey(currentToml, "hooks");
// Enable the codex_hooks feature flag.
updatedToml = upsertTomlBool(updatedToml, "features", "codex_hooks", true);
updatedToml = stripManagedHookTomlBlocks(updatedToml, managedCommands);
updatedToml = ensureTomlTable(updatedToml, "hooks");
updatedToml = `${updatedToml.replace(/\n*$/, "\n\n")}${managedHookTomlBlocks(desiredHooks)}`;
for (const candidate of trustCandidates) {
  updatedToml = upsertTomlStringInTable(
    updatedToml,
    `projects.${JSON.stringify(candidate)}`,
    "trust_level",
    JSON.stringify("trusted"),
  );
}
if (updatedToml !== currentToml) {
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const tmpPath = `${configPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, updatedToml, "utf8");
  fs.renameSync(tmpPath, configPath);
}

console.log(`[quaid][adapter:codex][postinstall] Codex hooks configured in ${hooksPath}`);
console.log(`[quaid][adapter:codex][postinstall] Codex config mirrored in ${configJsonPath}`);
