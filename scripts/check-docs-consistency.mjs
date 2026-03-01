#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), '..');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

const checks = [
  {
    file: 'README.md',
    require: [
      'active knowledge layer',
      'v0.2.9-alpha',
      'docs/releases/v0.2.9-alpha.md',
    ],
    forbid: [
      'docs/releases/v0.20.0-alpha.md',
    ],
  },
  {
    file: 'docs/ARCHITECTURE.md',
    require: [
      'projects_search',
      'adaptors/openclaw/adapter.ts',
      'adaptors/openclaw/maintenance.py',
      'route datastores',
    ],
    forbid: [
      'docs_search     -> docs_rag.search()',
    ],
  },
  {
    file: 'docs/AI-REFERENCE.md',
    require: [
      'projects_search',
      'adaptors/openclaw/adapter.ts',
      'orchestrator/default-orchestrator.ts',
      'total_recall planning pass',
    ],
    forbid: [
      '| `docs_search` |',
      'adaptors/openclaw/index.ts` | Plugin entry point (SOURCE OF TRUTH)',
    ],
  },
];

function checkToolsDomainBlock(errors) {
  const toolsPath = path.join(ROOT, 'projects/quaid/TOOLS.md');
  const defaultsPath = path.join(ROOT, 'modules/quaid/datastore/memorydb/domain_defaults.py');
  const tools = fs.readFileSync(toolsPath, 'utf8');
  const defaults = fs.readFileSync(defaultsPath, 'utf8');
  const startMarker = '<!-- AUTO-GENERATED:DOMAIN-LIST:START -->';
  const endMarker = '<!-- AUTO-GENERATED:DOMAIN-LIST:END -->';
  const start = tools.indexOf(startMarker);
  const end = tools.indexOf(endMarker);

  if (start === -1 || end === -1 || end <= start) {
    errors.push('projects/quaid/TOOLS.md: missing or malformed AUTO-GENERATED domain markers');
    return;
  }

  const expected = new Map();
  for (const line of defaults.split('\n')) {
    const m = line.match(/^\s*"([^"]+)":\s*"([^"]*)",?\s*$/);
    if (!m) continue;
    expected.set(m[1], m[2]);
  }
  if (!expected.size) {
    errors.push('modules/quaid/datastore/memorydb/domain_defaults.py: failed to parse default domain map');
    return;
  }

  const block = tools.slice(start + startMarker.length, end);
  const actual = new Map();
  for (const line of block.split('\n')) {
    const m = line.trim().match(/^- `([^`]+)`: (.+)$/);
    if (!m) continue;
    actual.set(m[1], m[2]);
  }

  for (const [key, desc] of expected.entries()) {
    if (!actual.has(key)) {
      errors.push(`projects/quaid/TOOLS.md: missing domain '${key}' in AUTO-GENERATED block`);
      continue;
    }
    if (actual.get(key) !== desc) {
      errors.push(
        `projects/quaid/TOOLS.md: domain '${key}' description drift (expected '${desc}', got '${actual.get(key)}')`,
      );
    }
  }
  for (const key of actual.keys()) {
    if (!expected.has(key)) {
      errors.push(`projects/quaid/TOOLS.md: unknown domain '${key}' in AUTO-GENERATED block`);
    }
  }
}

const errors = [];
for (const check of checks) {
  let text = '';
  try {
    text = read(check.file);
  } catch (err) {
    errors.push(`${check.file}: unreadable (${String(err)})`);
    continue;
  }

  for (const needle of check.require) {
    if (!text.includes(needle)) {
      errors.push(`${check.file}: missing required text: ${needle}`);
    }
  }
  for (const needle of check.forbid) {
    if (text.includes(needle)) {
      errors.push(`${check.file}: contains forbidden text: ${needle}`);
    }
  }
}

checkToolsDomainBlock(errors);

if (errors.length) {
  console.error('[docs-check] FAILED');
  for (const err of errors) console.error(`  - ${err}`);
  process.exit(1);
}

console.log('[docs-check] PASS');
