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
  `approved`, janitor promotes them to `active` after review. When duplicate
  merge or delete work also runs, row counts may shrink; use the worker log
  and janitor stats to verify actual maintenance effects.
- Identity files and janitor telemetry show fresh activity when the apply
  processed matching inputs. Identity file line counts may go DOWN after
  janitor, not up — janitor consolidates and prunes duplicates, not only
  appends.
- `*.snippets.md` files are pending queues. After snippet review succeeds,
  they are normally reduced or deleted. Do not require post-apply snippet
  files to remain. Treat a consumed snippet queue as PASS when the matching
  instance telemetry or worker log reports successful snippet review
  decisions (`snippets_folded`, `snippets_rewritten`, `snippets_discarded`,
  or `preserved_pending`) with zero snippet errors.
- `journal/*.journal.md` and `.distillation-state.json` timestamps are only
  required to move when the apply had source journal entries to distill.
  A second GLOBAL run with no new journal entries may legitimately report
  `journal_entries_distilled=0` and leave journal files unchanged.
- `PROJECT.log` activity is only required when this GLOBAL run explicitly
  includes a project-docs worker drain or the milestone seeded project-log
  entries. Pending project-log queue items by themselves are a separate
  project-docs signal, not a janitor artifact failure.
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

   `PROJECT.log` entries are not an unconditional janitor side effect. Inspect
   the hidden project-log queue under `QUAID_HOME`, but only require
   project-log freshness when this GLOBAL run is explicitly testing a
   project-docs worker drain or the milestone seeded project-log entries:

   ```bash
   ssh REMOTE_HOST "QHOME=\"\${QUAID_HOME:-\$HOME/.quaid}\"; \
     find \"\$QHOME/data/project-docs/project-log-queue\" -type f 2>/dev/null | head -30"
   ssh REMOTE_HOST "find ~/quaid/projects -name PROJECT.log \
     -exec stat -f '%Sm %N' -t '%Y-%m-%dT%H:%M:%SZ' {} \\; 2>/dev/null | sort"
   ```

   When project-log draining is part of the GLOBAL verification, the matching
   `PROJECT.log` under `~/quaid/projects/<PROJECT>/PROJECT.log` should have
   fresh lines matching the worker drain window. If queue items are present but
   no project-docs drain was part of the GLOBAL run, route that as a
   project-docs follow-up instead of grading the janitor apply as failed.

5. **State advancement / maintenance effects.** Spot-check reviewed row
   movement. Newly extracted facts start as `approved`; the janitor promotes
   them to `active` after review. Active counts can also go DOWN when the
   same apply legitimately merges duplicates or deletes reviewed rows, so do
   not require active to increase on every run. Require the run to show at
   least one expected maintenance effect when the dry-run found work:
   `graduated_to_active`, `duplicates_merged`, `memories_deleted`,
   `memories_fixed`, snippet review decisions, journal distillation, or
   project-docs requests.

   ```bash
   ssh REMOTE_HOST "sqlite3 ~/.quaid/instances/\$INSTANCE/data/memory.db \
     \"SELECT status, COUNT(*) FROM nodes GROUP BY status;\""
   ```

   Expect: if approved rows were present and selected for review, `approved`
   shrinks and `active` usually grows. If duplicate merge or delete work ran,
   total/active row counts may shrink instead. Raw counts vary by how much
   got extracted during the milestones. Use the worker log / janitor stats to
   distinguish real no-op from valid maintenance.

### PWN vs FAIL

- Dry-run hangs > 60 s — FAIL (regression in checkpoint bypass).
- Apply completes but no maintenance effect occurs despite dry-run reporting
  actionable work — FAIL.
- Matching snippet inputs are present before apply, but the instance worker
  log/telemetry shows no snippet-review decision and no preserved-pending
  reason — FAIL.
- Matching journal inputs are present before apply, but the instance worker
  log/telemetry shows no journal distillation result and no explicit
  "no entries" / "not due" reason — FAIL.
- `PROJECT.log` entries never materialize after apply when this GLOBAL run
  explicitly included project-log worker drain verification or seeded
  project-log entries — FAIL.
- Apply reports a per-row LLM error on a small subset but completes
  otherwise — PWN-note with the error class.
