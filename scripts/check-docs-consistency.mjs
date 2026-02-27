#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const ROOT = path.resolve(path.dirname(__filename), '..');

function read(rel) {
  return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function readJson(rel) {
  return JSON.parse(read(rel));
}

const checks = [
  {
    file: 'README.md',
    require: [
      'active knowledge layer',
      'v0.2.1-alpha',
      'docs/releases/v0.2.1-alpha.md',
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
  const tools = fs.readFileSync(toolsPath, 'utf8');
  const startMarker = '<!-- AUTO-GENERATED:DOMAIN-LIST:START -->';
  const endMarker = '<!-- AUTO-GENERATED:DOMAIN-LIST:END -->';
  const start = tools.indexOf(startMarker);
  const end = tools.indexOf(endMarker);

  if (start === -1 || end === -1 || end <= start) {
    errors.push('projects/quaid/TOOLS.md: missing or malformed AUTO-GENERATED domain markers');
    return;
  }

  const block = tools
    .slice(start + startMarker.length, end)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- `'));

  const cfg = readJson('config/memory.json');
  const domains = cfg?.retrieval?.domains;
  if (!domains || typeof domains !== 'object') {
    errors.push('config/memory.json: missing retrieval.domains object');
    return;
  }

  const expected = Object.entries(domains).map(
    ([key, desc]) => `- \`${key}\`: ${String(desc)}`
  );

  if (block.length !== expected.length) {
    errors.push(
      `projects/quaid/TOOLS.md: domain block count mismatch (found ${block.length}, expected ${expected.length})`
    );
    return;
  }

  for (let i = 0; i < expected.length; i += 1) {
    if (block[i] !== expected[i]) {
      errors.push(
        `projects/quaid/TOOLS.md: domain entry mismatch at index ${i + 1}; expected "${expected[i]}"`
      );
      return;
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
