#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const rawLocalConfigPath = process.env.QUAID_DEV_LOCAL_CONFIG || path.join(repoRoot, '.quaid-dev.local.json');
const localConfigPath = path.isAbsolute(rawLocalConfigPath)
  ? rawLocalConfigPath
  : path.resolve(repoRoot, rawLocalConfigPath);

const args = new Set(process.argv.slice(2));
const scanTree = !args.has('--history-only');
const scanHistory = !args.has('--tree-only');

function die(lines) {
  console.error('[privacy-audit] FAILED');
  for (const line of lines) console.error(`- ${line}`);
  process.exit(1);
}

function runGit(argsList, allowFail = false) {
  try {
    return execFileSync('git', argsList, {
      cwd: repoRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch (err) {
    if (allowFail) {
      return '';
    }
    throw err;
  }
}

function loadLocalConfig(configPath) {
  if (!fs.existsSync(configPath)) return {};
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function resolvePath(rawValue, baseDir) {
  const raw = String(rawValue || '').trim();
  if (!raw) return '';
  const expanded = raw.startsWith('~/')
    ? path.join(process.env.HOME || '~', raw.slice(2))
    : raw;
  if (path.isAbsolute(expanded)) return path.normalize(expanded);
  return path.normalize(path.resolve(baseDir, expanded));
}

function isPlaceholder(value) {
  const text = String(value || '').trim();
  if (!text) return true;
  return [
    'localhost',
    '127.0.0.1',
    '1000000000',
    'Test Operator',
    'operator',
  ].includes(text);
}

function collectBlockedStrings(localConfig) {
  const out = new Map();
  const pathsCfg = localConfig.paths && typeof localConfig.paths === 'object' ? localConfig.paths : {};
  const identityCfg = localConfig.identity && typeof localConfig.identity === 'object' ? localConfig.identity : {};
  const liveCfg = localConfig.liveTest && typeof localConfig.liveTest === 'object' ? localConfig.liveTest : {};
  const privacyCfg = localConfig.privacy && typeof localConfig.privacy === 'object' ? localConfig.privacy : {};

  const devRoot = resolvePath(pathsCfg.devRoot || '.', repoRoot) || repoRoot;
  const developmentDirectory = resolvePath(pathsCfg.developmentDirectory || '..', devRoot) || path.dirname(devRoot);
  const runtimeWorkspace = resolvePath(pathsCfg.runtimeWorkspace || path.relative(devRoot, path.join(developmentDirectory, 'test')), devRoot);
  const openclawSource = resolvePath(pathsCfg.openclawSource || path.relative(devRoot, path.join(developmentDirectory, 'openclaw-source')), devRoot);

  const add = (value, source) => {
    const text = String(value || '').trim();
    if (!text || isPlaceholder(text)) return;
    if (!out.has(text)) out.set(text, source);
  };

  add(devRoot, 'paths.devRoot');
  add(developmentDirectory, 'paths.developmentDirectory');
  add(runtimeWorkspace, 'paths.runtimeWorkspace');
  add(openclawSource, 'paths.openclawSource');

  if (Array.isArray(identityCfg.telegramAllowFrom)) {
    for (const entry of identityCfg.telegramAllowFrom) add(entry, 'identity.telegramAllowFrom');
  }
  add(liveCfg.remoteHost, 'liveTest.remoteHost');

  if (Array.isArray(privacyCfg.blockedStrings)) {
    for (const entry of privacyCfg.blockedStrings) add(entry, 'privacy.blockedStrings');
  }

  return out;
}

function scanTrackedTree(blocked) {
  const failures = [];
  const files = runGit(['ls-files', '-z'])
    .split('\0')
    .filter(Boolean)
    .filter((file) => ![
      'README.md',
      'NOTICE',
      'scripts/privacy-audit.mjs',
      'scripts/push-canary.sh',
    ].includes(file));

  for (const [needle, source] of blocked.entries()) {
    const encodedNeedle = Buffer.from(needle);
    for (const file of files) {
      const absPath = path.join(repoRoot, file);
      let buf;
      try {
        buf = fs.readFileSync(absPath);
      } catch {
        continue;
      }
      if (buf.includes(encodedNeedle)) {
        failures.push(`tracked file ${file} contains blocked marker from ${source}`);
      }
    }
  }
  return failures;
}

function scanReachableHistory(blocked) {
  const failures = [];
  for (const [needle, source] of blocked.entries()) {
    const raw = runGit(['log', '--all', '--format=%H%x09%s', '-S', needle, '--'], true);
    if (!raw) continue;
    const matches = raw
      .split('\n')
      .map((line) => line.trim())
      .filter(Boolean)
      .slice(0, 8)
      .map((line) => {
        const [sha, subject = ''] = line.split('\t');
        return `${sha.slice(0, 8)} ${subject}`;
      });
    if (matches.length > 0) {
      failures.push(
        `reachable history still contains blocked marker from ${source}: ${matches.join('; ')}`,
      );
    }
  }
  return failures;
}

const localConfig = loadLocalConfig(localConfigPath);
const blockedStrings = collectBlockedStrings(localConfig);

if (blockedStrings.size === 0) {
  die([
    `no local privacy markers found in ${path.relative(repoRoot, localConfigPath) || localConfigPath}`,
    'populate .quaid-dev.local.json with real local values and/or privacy.blockedStrings before release or canary push',
  ]);
}

const failures = [];
if (scanTree) failures.push(...scanTrackedTree(blockedStrings));
if (scanHistory) failures.push(...scanReachableHistory(blockedStrings));

if (failures.length) {
  die(failures);
}

console.log(
  `[privacy-audit] PASS markers=${blockedStrings.size} tree=${scanTree ? 'on' : 'off'} history=${scanHistory ? 'on' : 'off'}`,
);
