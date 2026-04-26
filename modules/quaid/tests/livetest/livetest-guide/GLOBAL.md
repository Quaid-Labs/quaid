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
  reviews accumulated memories across all instances). The graduation
  direction is `approved → active`: newly extracted facts land as
  `approved`, janitor promotes them to `active` after review. Check that
  the `active` count grew and the `approved` count shrank.
- Identity files, `*.snippets.md`, and `journal/*.journal.md` artifacts under
  `~/quaid/instances/<INSTANCE>/` show fresh-timestamp activity when the
  apply processed matching inputs. Identity file line counts may go DOWN
  after janitor, not up — janitor consolidates and prunes duplicates, not
  only appends.
- `PROJECT.log` activity is only required when the run has pending project-log
  queue items or the milestone explicitly seeded project-log entries.
- No duplicate or orphan snippet / journal rows; registry deletes
  propagate.

### Procedure

1. **Pre-state capture.** Snapshot per-instance visible-home artifact
   counts so you can diff after the apply. Identity and snippet/journal
   files live under `~/quaid/instances/<INSTANCE>/`, not a shared
   `~/quaid/identity/` (that path does not exist on current builds).

   ```bash
   ssh REMOTE_HOST "for f in SOUL.md USER.md ENVIRONMENT.md; do \
     for i in ~/quaid/instances/*/; do \
       echo -n \"\$i\$f: \"; wc -l < \"\$i\$f\" 2>/dev/null || echo '(missing)'; \
     done; done"
   ssh REMOTE_HOST "find ~/quaid/instances -maxdepth 2 -name '*.snippets.md' \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
   ssh REMOTE_HOST "find ~/quaid/instances -maxdepth 3 \
     \\( -name '*.journal.md' -o -name '.distillation-state.json' \\) \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
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

4. **Post-state verification.** Diff identity / snippet / journal files
   against the pre-state snapshot. Identity line counts may go DOWN
   (consolidation) or UP (new facts); both are legitimate. Snippet files
   are flat `*.snippets.md` files in the visible instance root. Journal files
   live under the singular `journal/` directory.

   ```bash
   ssh REMOTE_HOST "for f in SOUL.md USER.md ENVIRONMENT.md; do \
     for i in ~/quaid/instances/*/; do \
       echo -n \"\$i\$f: \"; wc -l < \"\$i\$f\" 2>/dev/null || echo '(missing)'; \
     done; done"
   ssh REMOTE_HOST "find ~/quaid/instances -maxdepth 2 -name '*.snippets.md' \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
   ssh REMOTE_HOST "find ~/quaid/instances -maxdepth 3 \
     \\( -name '*.journal.md' -o -name '.distillation-state.json' \\) \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
   ssh REMOTE_HOST "tail -40 ~/quaid/instances/*/journal/*.journal.md 2>/dev/null | head -40"
   ```

   `PROJECT.log` entries are not an unconditional janitor side effect. Check
   project-log freshness only when there are pending queue items or the
   milestone explicitly seeded project-log entries:

   ```bash
   ssh REMOTE_HOST "find ~/.quaid/instances -path '*/data/project-docs/project-log-queue/*' \
     -type f 2>/dev/null | head -30"
   ssh REMOTE_HOST "find ~/quaid/projects -name PROJECT.log \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
   ```

   When a queue item or seeded project-log entry exists, the matching
   `PROJECT.log` under `~/quaid/projects/<PROJECT>/PROJECT.log` should have
   fresh lines matching the apply window.

5. **State advancement (approved → active).** Spot-check that reviewed
   rows graduated from `approved` to `active`. Newly extracted facts
   start as `approved`; the janitor promotes them to `active`:

   ```bash
   ssh REMOTE_HOST "sqlite3 ~/.quaid/instances/\$INSTANCE/data/memory.db \
     \"SELECT status, COUNT(*) FROM nodes GROUP BY status;\""
   ```

   Expect: `active` count grew compared to pre-apply; `approved` count
   shrank. Raw counts vary by how much got extracted during M2. Apply
   output should also print a `Graduated N memories from approved to
   active` line.

### PWN vs FAIL

- Dry-run hangs > 60 s — FAIL (regression in checkpoint bypass).
- Apply completes but no state advances — FAIL.
- Matching `*.snippets.md` or `journal/*.journal.md` artifacts never
  materialize after apply even though the apply processed corresponding
  snippet or journal inputs — FAIL.
- `PROJECT.log` entries never materialize after apply when a project-log
  queue item or explicit seeded project-log entry existed — FAIL.
- Apply reports a per-row LLM error on a small subset but completes
  otherwise — PWN-note with the error class.
