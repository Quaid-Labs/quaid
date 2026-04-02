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

const workspace = resolveWorkspace();
const instance = String(process.env.QUAID_INSTANCE || "codex-main").trim() || "codex-main";
const quaidBinary = resolveQuaidBinary(workspace);
const quaidCommand = escapeShellSingle(quaidBinary);
const defaultHome = escapeShellDefault(workspace);
const defaultInstance = escapeShellDefault(instance);
const envPrefix = [
  `QUAID_HOME="\${QUAID_HOME:-${defaultHome}}"`,
  `CLAWDBOT_WORKSPACE="\${CLAWDBOT_WORKSPACE:-${defaultHome}}"`,
  `QUAID_INSTANCE="\${QUAID_INSTANCE:-${defaultInstance}}"`,
].join(" ");

const hooksPath = path.join(os.homedir(), ".codex", "hooks.json");
const configPath = path.join(os.homedir(), ".codex", "config.toml");

const managedCommands = [
  "hook-session-init",
  "hook-inject",
  "hook-codex-stop",
];

const desiredHooks = {
  SessionStart: [
    {
      matcher: "startup|resume",
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

const currentToml = fs.existsSync(configPath) ? fs.readFileSync(configPath, "utf8") : "";
const updatedToml = upsertTomlBool(currentToml, "features", "codex_hooks", true);
if (updatedToml !== currentToml) {
  fs.mkdirSync(path.dirname(configPath), { recursive: true });
  const tmpPath = `${configPath}.tmp-${process.pid}-${Date.now()}`;
  fs.writeFileSync(tmpPath, updatedToml, "utf8");
  fs.renameSync(tmpPath, configPath);
}

console.log(`[quaid][adapter:codex][postinstall] Codex hooks configured in ${hooksPath}`);
