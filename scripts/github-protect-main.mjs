#!/usr/bin/env node
import { spawnSync } from 'node:child_process';

const args = process.argv.slice(2);
const repoArgIdx = args.indexOf('--repo');
const repo = repoArgIdx >= 0 ? args[repoArgIdx + 1] : 'Steadman-Labs/quaid';
if (!repo || repo.startsWith('--')) {
  console.error('Usage: node scripts/github-protect-main.mjs --repo owner/name');
  process.exit(2);
}

const [owner, name] = repo.split('/');
if (!owner || !name) {
  console.error(`Invalid repo: ${repo}`);
  process.exit(2);
}

const payload = {
  required_status_checks: {
    strict: true,
    checks: [
      { context: 'quick' }
    ]
  },
  enforce_admins: true,
  required_pull_request_reviews: {
    dismiss_stale_reviews: true,
    require_code_owner_reviews: false,
    required_approving_review_count: 1,
    require_last_push_approval: false
  },
  restrictions: null,
  required_linear_history: false,
  allow_force_pushes: false,
  allow_deletions: false,
  block_creations: false,
  required_conversation_resolution: true,
  lock_branch: false,
  allow_fork_syncing: true
};

try {
  const result = spawnSync(
    'gh',
    [
      'api',
      '--method',
      'PUT',
      `repos/${owner}/${name}/branches/main/protection`,
      '-H',
      'Accept: application/vnd.github+json',
      '--input',
      '-'
    ],
    {
      input: JSON.stringify(payload),
      stdio: ['pipe', 'inherit', 'inherit'],
      encoding: 'utf8'
    }
  );
  if (result.status !== 0) {
    throw new Error(`gh api exited with ${result.status}`);
  }
  console.log(`[github-protect-main] PASS repo=${repo}`);
} catch (err) {
  console.error(`[github-protect-main] FAILED repo=${repo}`);
  process.exit(1);
}
