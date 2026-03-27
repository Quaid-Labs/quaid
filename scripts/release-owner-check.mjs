#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, '..');
const rawLocalConfigPath = process.env.QUAID_DEV_LOCAL_CONFIG || path.join(repoRoot, '.quaid-dev.local.json');
const localConfigPath = path.isAbsolute(rawLocalConfigPath)
  ? rawLocalConfigPath
  : path.resolve(repoRoot, rawLocalConfigPath);
const REQUIRED_OWNER_NAME = 'Solomon Steadman';
const REQUIRED_OWNER_EMAIL = '168413654+solstead@users.noreply.github.com';

function loadLocalConfig(configPath) {
  try {
    if (!fs.existsSync(configPath)) return {};
    const parsed = JSON.parse(fs.readFileSync(configPath, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function run(cmd) {
  return execSync(cmd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

function runAllowFail(cmd) {
  try {
    return { ok: true, out: run(cmd) };
  } catch (err) {
    return { ok: false, out: String(err?.stderr || err?.message || err) };
  }
}

function fail(lines) {
  console.error('[release-owner-check] FAILED');
  for (const line of lines) console.error(`- ${line}`);
  process.exit(1);
}

function isLocalOnlyEmail(email) {
  return /\.local\s*$/i.test(String(email || '').trim());
}

const localName = runAllowFail('git config user.name');
const localEmail = runAllowFail('git config user.email');
const localConfig = loadLocalConfig(localConfigPath);
const identityConfig =
  localConfig.identity && typeof localConfig.identity === 'object' ? localConfig.identity : {};
const expectedName = REQUIRED_OWNER_NAME;
const expectedEmail = REQUIRED_OWNER_EMAIL;
const allowedIdentityPairs = new Set([`${expectedName}\x00${expectedEmail}`]);
const bannedMessagePatterns = [
  /co-authored-by:/i,
  /claude code/i,
  /\b[a-z0-9._-]+\.local\b/i,
  /\b[a-z0-9._%+-]+@[a-z0-9.-]+\.local\b/i,
];
const failures = [];

if (isLocalOnlyEmail(expectedEmail)) {
  failures.push(
    `release owner email "${expectedEmail}" is local-only; set a public-safe email before release/canary push`,
  );
}

if (process.env.QUAID_OWNER_NAME && process.env.QUAID_OWNER_NAME !== expectedName) {
  failures.push(
    `QUAID_OWNER_NAME is "${process.env.QUAID_OWNER_NAME}", but Quaid commits must use "${expectedName}"`,
  );
}
if (process.env.QUAID_OWNER_EMAIL && process.env.QUAID_OWNER_EMAIL !== expectedEmail) {
  failures.push(
    `QUAID_OWNER_EMAIL is "${process.env.QUAID_OWNER_EMAIL}", but Quaid commits must use "${expectedEmail}"`,
  );
}
if (identityConfig.releaseOwnerName && String(identityConfig.releaseOwnerName).trim() !== expectedName) {
  failures.push(
    `.quaid-dev.local.json releaseOwnerName is "${String(identityConfig.releaseOwnerName).trim()}", expected "${expectedName}"`,
  );
}
if (identityConfig.releaseOwnerEmail && String(identityConfig.releaseOwnerEmail).trim() !== expectedEmail) {
  failures.push(
    `.quaid-dev.local.json releaseOwnerEmail is "${String(identityConfig.releaseOwnerEmail).trim()}", expected "${expectedEmail}"`,
  );
}

if (!localName.ok || localName.out !== expectedName) {
  failures.push(
    `git config user.name is "${localName.ok ? localName.out : '(unset)'}", expected "${expectedName}"`,
  );
}
if (!localEmail.ok || localEmail.out !== expectedEmail) {
  failures.push(
    `git config user.email is "${localEmail.ok ? localEmail.out : '(unset)'}", expected "${expectedEmail}"`,
  );
}
if (localEmail.ok && isLocalOnlyEmail(localEmail.out)) {
  failures.push(
    `git config user.email is "${localEmail.out}", which is local-only and not valid for public release/canary push`,
  );
}

const upstream = runAllowFail('git rev-parse --abbrev-ref --symbolic-full-name @{u}');
let range = 'HEAD';
if (upstream.ok && upstream.out) {
  range = `${upstream.out}..HEAD`;
} else {
  const headMinus = runAllowFail('git rev-parse --verify HEAD~20');
  range = headMinus.ok ? 'HEAD~20..HEAD' : 'HEAD';
}

const raw = runAllowFail(
  `git log --format=%H%x00%an%x00%ae%x00%cn%x00%ce%x00%s%x00%b%x1e ${range}`,
);

if (!raw.ok) {
  failures.push(`could not read commit history for range "${range}"`);
} else if (raw.out) {
  const records = raw.out
    .split('\x1e')
    .map((r) => r.trim())
    .filter(Boolean);

  for (const record of records) {
    const [sha, authorName, authorEmail, committerName, committerEmail, subject, body = ''] =
      record.split('\x00');
    const id = `${sha.slice(0, 8)} ${subject}`;
    if (!allowedIdentityPairs.has(`${authorName}\x00${authorEmail}`)) {
      failures.push(
        `${id}: author is "${authorName} <${authorEmail}>", expected one of allowed owner identities`,
      );
    }
    if (isLocalOnlyEmail(authorEmail)) {
      failures.push(`${id}: author email "${authorEmail}" is local-only`);
    }
    if (!allowedIdentityPairs.has(`${committerName}\x00${committerEmail}`)) {
      failures.push(
        `${id}: committer is "${committerName} <${committerEmail}>", expected one of allowed owner identities`,
      );
    }
    if (isLocalOnlyEmail(committerEmail)) {
      failures.push(`${id}: committer email "${committerEmail}" is local-only`);
    }
    const message = `${subject}\n${body}`;
    for (const pattern of bannedMessagePatterns) {
      if (pattern.test(message)) {
        failures.push(`${id}: commit message contains blocked text matching ${pattern}`);
        break;
      }
    }
  }
}

if (failures.length) {
  fail(failures);
}

console.log(
  `[release-owner-check] PASS owner=${expectedName} <${expectedEmail}> range=${range}`,
);
