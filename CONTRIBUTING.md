# Contributing to Quaid

Thanks for considering a contribution.

## Before You Start

- Open an issue for significant changes so we can align scope first.
- Keep PRs focused and small where possible.
- Prioritize correctness and reproducibility over novelty.

## Local Setup

```bash
git clone https://github.com/steadman-labs/quaid.git
cd quaid/plugins/quaid
npm ci
python3 -m pip install pytest ruff
```

## Validation

From `plugins/quaid`:

```bash
npm run check:runtime-pairs:strict
npm run lint
npm test
python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120
```

For integration-heavy changes, also run:

```bash
npm run test:all
```

## Design Expectations

- Keep adapter/orchestrator/core boundaries explicit.
- Avoid silent fallback behavior that hides failures.
- Use config-driven behavior where practical; avoid hardcoded operational constants.
- Update docs when behavior changes.

## PR Checklist

- [ ] Tests updated or added
- [ ] Docs updated (README/architecture/reference as needed)
- [ ] No secrets or local artifacts included
- [ ] Runtime TS/JS pair checks pass
- [ ] Lint passes

## Style Notes

- TypeScript/JavaScript: ESLint (flat config)
- Python: Ruff (correctness-focused baseline)
- Keep comments concise and focused on non-obvious logic

## Security

If you find a security issue, see `SECURITY.md` before opening a public issue.
