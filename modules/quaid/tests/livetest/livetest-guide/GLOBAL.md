# GLOBAL: Host-wide systems

The GLOBAL phase runs once at the very end of a full test suite, after every
lane has finished its per-platform milestones and XP has completed. It
covers systems that cannot be cleanly scoped to a single lane — either
because they span all instances on the host or because running them
concurrently would contaminate other lanes' results.

Currently the only GLOBAL test is the janitor review cycle. If new
unavoidably-global surfaces appear, add them here.

## Janitor review cycle

Depends on M2 having stored enough facts across lanes to give the janitor a
non-trivial plan.

### Pass

- Dry-run completes within ~60 s and reports a non-empty plan.
- `--apply` completes (first run can take 15–30 min because the janitor LLM
  reviews accumulated memories across all instances); state advances for
  reviewed rows from `pending` → `approved`.
- Snippets, journals, and `PROJECT.log` entries appear under visible home
  (`~/quaid/`) with fresh timestamps matching the apply window.
- No duplicate or orphan snippet / journal rows; registry deletes propagate.

### Procedure

1. **Pre-state capture.** Snapshot the current visible-home artifact counts
   so you can diff after the apply:

   ```bash
   ssh REMOTE_HOST "for f in ~/quaid/identity/SOUL.md ~/quaid/identity/USER.md \
     ~/quaid/identity/ENVIRONMENT.md; do \
     echo -n \"\$f: \"; wc -l < \"\$f\" 2>/dev/null || echo '(missing)'; done"
   ssh REMOTE_HOST "ls ~/quaid/snippets/ 2>/dev/null | wc -l"
   ssh REMOTE_HOST "ls ~/quaid/journals/ 2>/dev/null | wc -l"
   ```

2. **Dry-run.** Confirm the plan before applying:

   ```bash
   ssh REMOTE_HOST "\$QCLI janitor --task all --dry-run"
   ```

   Must complete within ~60 s and produce a non-empty plan. Note the
   approximate count of pending rows it intends to review.

3. **Apply.** This is slow — budget 15–30 min for the first apply. LLM
   review of each fact batch is the bottleneck.

   ```bash
   ssh REMOTE_HOST "\$QCLI janitor --task all --apply --approve"
   ```

   Stream output; watch for per-batch progress.

4. **Post-state verification.** Confirm the identity files grew and new
   artifacts appeared:

   ```bash
   ssh REMOTE_HOST "for f in ~/quaid/identity/SOUL.md ~/quaid/identity/USER.md \
     ~/quaid/identity/ENVIRONMENT.md; do \
     echo -n \"\$f: \"; wc -l < \"\$f\"; done"
   ssh REMOTE_HOST "tail -40 ~/quaid/journals/SOUL.md 2>/dev/null | head -20"
   ssh REMOTE_HOST "ls ~/quaid/snippets/ | head -10"
   ```

   `PROJECT.log` entries for any linked projects should have fresh lines
   matching the apply window. The "Solomon runs a project called Quaid"
   passing mention from the rolling transcript should have become a line in
   either `misc--<instance>/PROJECT.log` or `quaid/PROJECT.log` depending on
   whether a Quaid project was linked in the instance.

5. **State advancement.** Spot-check that reviewed rows advanced from
   `pending` → `approved`:

   ```bash
   ssh REMOTE_HOST "sqlite3 ~/.quaid/instances/\$INSTANCE/data/memory.db \
     \"SELECT status, COUNT(*) FROM nodes GROUP BY status;\""
   ```

   Expect: the approved count grew compared to pre-apply; pending count
   shrank. Raw counts vary by how much got extracted during M2.

### PWN vs FAIL

- Dry-run hangs > 60 s — FAIL (regression in checkpoint bypass).
- Apply completes but no state advances — FAIL.
- Snippets / journals / `PROJECT.log` entries never materialize after apply
  — FAIL.
- Apply reports a per-row LLM error on a small subset but completes
  otherwise — PWN-note with the error class.
