#!/usr/bin/env node

import { execSync } from 'node:child_process';

const expectedName = process.env.QUAID_OWNER_NAME || 'solstead';
const expectedEmail =
  process.env.QUAID_OWNER_EMAIL || '168413654+solstead@users.noreply.github.com';
const allowedIdentityPairs = new Set([
  `${expectedName}\x00${expectedEmail}`,
  // Transition alias: older canonical identity used in prior public commits.
  'Solomon Steadman\x00solstead@users.noreply.github.com',
]);

const bannedMessagePatterns = [
  /co-authored-by:/i,
  /claude code/i,
  /\balfie\b/i,
  /clawdbot@testbench\.local/i,
];

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

const failures = [];

const localName = runAllowFail('git config user.name');
const localEmail = runAllowFail('git config user.email');

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
    if (!allowedIdentityPairs.has(`${committerName}\x00${committerEmail}`)) {
      failures.push(
        `${id}: committer is "${committerName} <${committerEmail}>", expected one of allowed owner identities`,
      );
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
