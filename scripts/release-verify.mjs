#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), '..');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function match(text, regex, label) {
  const m = text.match(regex);
  if (!m) throw new Error(`Could not parse ${label}`);
  return m[1];
}

function fileExists(rel) {
  return fs.existsSync(path.join(ROOT, rel));
}

const errors = [];
let version = '';

try {
  const pkg = JSON.parse(read('plugins/quaid/package.json'));
  version = String(pkg.version || '').trim();
  if (!version) errors.push('plugins/quaid/package.json: missing version');

  const versionFile = read('plugins/quaid/VERSION').trim();
  if (versionFile !== version) {
    errors.push(`plugins/quaid/VERSION (${versionFile}) != package.json (${version})`);
  }

  const setupMjs = read('setup-quaid.mjs');
  const setupMjsVersion = match(setupMjs, /const VERSION = "([^"]+)";/, 'setup-quaid.mjs VERSION');
  if (setupMjsVersion !== version) {
    errors.push(`setup-quaid.mjs (${setupMjsVersion}) != package.json (${version})`);
  }

  const setupSh = read('setup-quaid.sh');
  const setupShVersion = match(setupSh, /QUAID_VERSION="([^"]+)"/, 'setup-quaid.sh QUAID_VERSION');
  if (setupShVersion !== version) {
    errors.push(`setup-quaid.sh (${setupShVersion}) != package.json (${version})`);
  }

  const readme = read('README.md');
  const readmeRelease = match(readme, /Known limitations for \*\*(v[^*]+)\*\*/, 'README release marker');
  if (readmeRelease !== `v${version}`) {
    errors.push(`README release marker (${readmeRelease}) != v${version}`);
  }

  const releaseDocRel = `docs/releases/v${version}.md`;
  if (!fileExists(releaseDocRel)) {
    errors.push(`Missing release notes doc: ${releaseDocRel}`);
  }

  if (!readme.includes(releaseDocRel)) {
    errors.push(`README does not link ${releaseDocRel}`);
  }

  const installSh = read('install.sh');
  if (!installSh.includes('quaid-release.tar.gz')) {
    errors.push('install.sh missing quaid-release.tar.gz download path');
  }

  const installPs1 = read('install.ps1');
  if (!installPs1.includes('quaid-release.tar.gz')) {
    errors.push('install.ps1 missing quaid-release.tar.gz download path');
  }
} catch (err) {
  errors.push(String(err));
}

if (errors.length) {
  console.error('[release-verify] FAILED');
  for (const e of errors) console.error(`  - ${e}`);
  process.exit(1);
}

console.log(`[release-verify] PASS version=${version}`);
