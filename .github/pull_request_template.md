## Summary

Describe what changed and why.

## Validation

- [ ] `npm run check:runtime-pairs:strict` (from `plugins/quaid`)
- [ ] `npm run lint` (from `plugins/quaid`)
- [ ] `npm test` (from `plugins/quaid`)
- [ ] `python3 scripts/run_pytests.py --mode unit --workers 4 --timeout 120` (from `plugins/quaid`)
- [ ] `bash scripts/release-check.sh` (from repo root, for release-facing changes)

## Risk

- [ ] No secret material added
- [ ] No silent fallback behavior introduced
- [ ] Docs updated for behavior changes

## Notes

Anything reviewers should focus on.
