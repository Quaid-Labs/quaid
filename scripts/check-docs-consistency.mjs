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
      'v0.2.0-alpha',
      'docs/releases/v0.2.0-alpha.md',
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
      'adaptors/openclaw/knowledge/orchestrator.ts',
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
      'adaptors/openclaw/knowledge/orchestrator.ts',
      'total_recall planning pass',
    ],
    forbid: [
      '| `docs_search` |',
      'adaptors/openclaw/index.ts` | Plugin entry point (SOURCE OF TRUTH)',
    ],
  },
];

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

if (errors.length) {
  console.error('[docs-check] FAILED');
  for (const err of errors) console.error(`  - ${err}`);
  process.exit(1);
}

console.log('[docs-check] PASS');
