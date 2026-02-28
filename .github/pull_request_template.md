## Summary

Describe what changed and why.

## Scope

- [ ] This PR is focused on a single primary issue/topic
- [ ] No unrelated changes bundled

## Validation

- [ ] `npm run check:runtime-pairs:strict` (from `modules/quaid`)
- [ ] `npm run lint` (from `modules/quaid`)
- [ ] `npm test` (from `modules/quaid`)
- [ ] `python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120` (from `modules/quaid`)
- [ ] `bash scripts/release-check.sh` (from repo root, for release-facing changes)

## Architecture & Ops Guardrails

- [ ] Core/orchestrator/adapter boundaries preserved
- [ ] No hidden provider/model fallback behavior introduced
- [ ] Config/env changes documented
- [ ] Docs updated for behavior changes (`README`, `docs/ARCHITECTURE.md`, `docs/AI-REFERENCE.md`, etc.)

## Security & Hygiene

- [ ] No secret material added
- [ ] No plaintext credentials or private tokens in tracked files
- [ ] AI-assisted content reviewed and validated by submitter

## Notes for Reviewers

Anything reviewers should focus on.
