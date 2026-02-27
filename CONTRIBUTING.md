# Contributing to Quaid

Thanks for considering a contribution.

## Before You Start

- Open an issue for significant changes so we can align scope first.
- Keep PRs focused and small where possible.
- Prioritize correctness and reproducibility over novelty.
- If you want a starter task, check `docs/GOOD-FIRST-ISSUES.md`.

## Contribution Rules

- One PR should map to one primary issue/topic.
- Avoid unrelated bundling in the same PR.
- Prefer incremental PRs over large, multi-domain rewrites.

## AI-Assisted Contributions

AI-assisted contributions are welcome, with explicit accountability.

- Disclose AI assistance in the PR description when relevant.
- The submitter owns correctness, security, and maintainability.
- Run required validation; do not rely on generated code alone.
- Do not introduce hidden fallback behavior that can mask failures or raise provider costs.

## Local Setup

```bash
git clone https://github.com/Steadman-Labs/quaid.git
cd quaid/modules/quaid
npm ci
python3 -m pip install pytest ruff
```

Optional (recommended) local hook setup:

```bash
python3 -m pip install pre-commit
cd ../..
pre-commit install
```

## Validation

From `modules/quaid`:

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

Before pushing release-facing updates:

```bash
cd ../..
bash scripts/release-check.sh
```

See `docs/RELEASE.md` for ownership guard details and expected attribution settings.

## Design Expectations

- Keep adapter/orchestrator/core boundaries explicit.
- Avoid silent fallback behavior that hides failures.
- Treat `failHard` as config-owned (`retrieval.failHard` / `retrieval.fail_hard`), not env-owned.
- `failHard=true`: do not fallback. `failHard=false`: fallback allowed only with loud diagnostics.
- Use config-driven behavior where practical; avoid hardcoded operational constants.
- Update docs when behavior changes.
- Align major decisions with `VISION.md`.

## PR Checklist

- [ ] Tests updated or added
- [ ] Docs updated (README/architecture/reference as needed)
- [ ] No secrets or local artifacts included
- [ ] Runtime TS/JS pair checks pass
- [ ] Lint passes

## Attribution Policy

Public history should use the maintainer canonical GitHub noreply identity.

- Name: `solstead`
- Email: `168413654+solstead@users.noreply.github.com`

Do not use personal/private email addresses in public commits.

## Style Notes

- TypeScript/JavaScript: ESLint (flat config)
- Python: Ruff (correctness-focused baseline)
- Keep comments concise and focused on non-obvious logic

## Security

If you find a security issue, see `SECURITY.md` before opening a public issue.
