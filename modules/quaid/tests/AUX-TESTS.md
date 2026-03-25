# Auxiliary Live Tests

Short, targeted tests (5–15 min each) that can be run on-demand to validate specific behaviors
without a full M0–M13 suite. Each test is independent — you can run any subset.

These are not replacements for the full suite. They target edge cases discovered during live
test runs that don't have unit test coverage and are hard to trigger in normal usage.

---

## AUX-1: `rolling-restart-idle` — Idle Timeout After Daemon Restart

**What it tests:** A session in-flight when the daemon is killed gets a preliminary cursor
written, so `check_idle_sessions()` can discover it on restart and eventually fire the idle
timeout even with no new transcript activity.

**Regression for:** M4 (OC PASS-WITH-NOTE in Run 16) — sessions invisible to idle detection
after restart because no cursor had been written yet.

**Platform:** CC (easier to drive transcript + kill daemon)
**Estimated time:** 12–15 min

### Steps

1. **Setup**: Fresh CC session. Confirm daemon is running (`quaid status`).
2. **Send 3–4 turns of conversation** to accumulate transcript content.
3. **Kill the daemon** immediately after transcript update (before idle timeout fires):
   ```bash
   pkill -f extraction_daemon
   ```
4. **Restart the daemon**:
   ```bash
   quaid daemon start
   ```
5. **Wait idle timeout** (default 60s after restart) without sending any more messages.
6. **Verify**: Check daemon logs — should see `[daemon] idle: firing idle extraction` for that session. Recall should return content from the pre-kill transcript.

**Pass:** Idle extraction fires within 90s of daemon restart with no new activity.
**Fail:** No idle extraction fires; session is stuck unextracted.

---

## AUX-2: `rolling-concurrent-reset` — Rolling Not Dropped Under Concurrent Reset

**What it tests:** When a rolling signal and a reset/compaction signal are both pending for the
same session, the rolling signal is NOT consumed/discarded by the dedup logic when reset processes
first.

**Regression for:** M2 (OC PASS-WITH-NOTE in Run 16) — dedup loop was deleting higher-priority
rolling signals when a lower-priority reset processed.

**Platform:** OC or CC
**Estimated time:** 10–12 min

### Steps

1. **Setup**: Active session with rolling extraction enabled.
2. **Trigger rolling signal** (long enough conversation to cross rolling threshold).
3. **Immediately trigger `/compact`** before the rolling extraction completes.
4. **Verify via logs**: Both rolling AND compaction should extract. Check daemon log for:
   - `[daemon] processing signal: rolling` for the rolling extraction
   - `[daemon] processing signal: compaction` or `reset` for the compaction
5. **Verify content**: Recall should return content from both the mid-session rolling chunk
   AND the final compaction extraction.

**Pass:** Both rolling and compaction extract; no signal is silently dropped.
**Fail:** Only one extracts (rolling dropped), or daemon logs show "discarding duplicate."

---

## AUX-3: `embed-retry-ollama-gap` — Embed Retry After Ollama Outage

**What it tests:** Facts stored without embeddings (because Ollama was down at extraction time)
get backfilled by the daemon's periodic embed-retry task when Ollama comes back.

**Regression for:** Post-run-16 fix `623f5d21` — previously, facts with `embedding IS NULL`
were invisible to vector recall forever.

**Platform:** OC or CC (whichever has easier Ollama access)
**Estimated time:** 10–12 min

### Steps

1. **Stop Ollama**:
   ```bash
   pkill ollama  # or: brew services stop ollama
   ```
2. **Send 3–4 turns of conversation** and trigger extraction (via `/compact` or wait for idle).
3. **Verify null embeddings**: Check the memory DB:
   ```bash
   sqlite3 $QUAID_HOME/$QUAID_INSTANCE/data/memory.db \
     "SELECT count(*) FROM nodes WHERE embedding IS NULL AND state='active'"
   ```
   Expect count > 0.
4. **Restart Ollama**:
   ```bash
   ollama serve &  # or: brew services start ollama
   ```
5. **Wait 5–6 min** for the daemon embed-retry interval to fire.
6. **Verify**: Rerun the sqlite query — count should be 0 (or lower). Daemon log should show
   `[daemon] embed-retry: backfilled N missing embedding(s)`.
7. **Verify recall**: The content from step 2 should now appear in `quaid recall` results.

**Pass:** Null embeddings backfilled within 6 min of Ollama coming back online.
**Fail:** Embeddings remain null; content not reachable via recall.

---

## AUX-4: `docs-update-never-indexed` — Never-Indexed Docs Picked Up by CLI

**What it tests:** `quaid docs update --apply` indexes docs that were registered but never
indexed (e.g. registered via `quaid registry register` without a source-files path, so
`last_indexed_at IS NULL`).

**Regression for:** Post-run-16 fix `623f5d21` — `cmd_update_stale()` skipped `source_files IS NULL`
docs entirely, leaving them invisible to docs search.

**Platform:** Either
**Estimated time:** 5–7 min

### Steps

1. **Register a new doc** without source-files:
   ```bash
   echo "# Test Doc\nThis is a never-indexed test document." > /tmp/aux-test-doc.md
   quaid registry register /tmp/aux-test-doc.md --project misc--$QUAID_INSTANCE
   ```
2. **Verify it's not yet indexed**:
   ```bash
   quaid docs list --project misc--$QUAID_INSTANCE
   # last_indexed_at should be NULL for the new doc
   ```
3. **Run docs update**:
   ```bash
   quaid docs update --apply
   ```
4. **Verify it was indexed**: Output should include `Indexed: /tmp/aux-test-doc.md (N chunks)`.
5. **Verify searchable**:
   ```bash
   quaid docs search "never-indexed test document"
   ```
   Should return the doc.

**Pass:** Never-indexed doc is discovered and indexed by `docs update --apply`.
**Fail:** Doc is skipped; "All docs up-to-date." printed despite un-indexed doc.

---

## AUX-5: `idle-timeout-post-restart-no-activity` — Daemon Discovers Pre-Existing Idle Sessions

**What it tests:** When the daemon starts fresh (e.g. after system reboot), it discovers sessions
that already have cursors written (from a previous daemon run) and have been idle past the
threshold. Those sessions get extracted without any new transcript activity.

**Platform:** CC
**Estimated time:** 10–12 min

### Steps

1. **Setup**: Active CC session with cursor already written (run a few turns, let idle extraction
   fire once to write cursor).
2. **Kill the daemon and wait 90s** (past idle timeout):
   ```bash
   pkill -f extraction_daemon
   sleep 90
   ```
3. **Add content to the transcript** while daemon is down (2–3 more turns in the CC session).
4. **Start the daemon**:
   ```bash
   quaid daemon start
   ```
5. **Wait 30–60s** without any further activity.
6. **Verify**: Daemon log should show idle discovery for the existing cursor session and extraction
   of the new transcript content.

**Pass:** Daemon finds existing cursor sessions on startup and extracts without new triggers.
**Fail:** Pre-existing cursor sessions are ignored; content never extracted.

---

## AUX-6: `xp-cross-registration-docs-update` — Cross-Registered Doc Update Without XP Ceremony

**What it tests:** When a doc is indexed on CC and the same file is registered on OC via
`quaid registry register`, running `quaid docs update --apply` on OC picks it up without
needing to go through the full XP cross-registration ceremony.

**Platform:** Both CC and OC
**Estimated time:** 8–10 min

### Steps

1. **On CC**: Create and index a doc in a shared project:
   ```bash
   echo "# Shared Doc\nCross-platform test content." > ~/quaid/projects/shared-test/shared.md
   quaid registry register ~/quaid/projects/shared-test/shared.md --project shared-test
   quaid docs update --apply
   ```
2. **On OC**: Register the same file without re-indexing:
   ```bash
   quaid registry register ~/quaid/projects/shared-test/shared.md --project shared-test
   ```
3. **On OC**: Run docs update:
   ```bash
   quaid docs update --apply
   ```
4. **Verify on OC**:
   ```bash
   quaid docs search "cross-platform test content"
   ```
   Should return the doc.

**Pass:** OC can search the doc after `docs update --apply` without XP link ceremony.
**Fail:** Doc not found; OC requires full XP flow to access CC-side docs.

---

## AUX-7: `stale-signal-orphan-reprocess` — Pre-Restart Signals Reprocessed on Startup

**What it tests:** Signal files left on disk from a previous daemon run (e.g. daemon killed
mid-extraction) are picked up and reprocessed on daemon restart.

**Platform:** CC
**Estimated time:** 8–10 min

### Steps

1. **Setup**: Active CC session.
2. **Send content** to trigger a rolling or idle signal.
3. **Kill the daemon immediately** as it starts processing the signal (SIGKILL to prevent cleanup):
   ```bash
   pkill -9 -f extraction_daemon
   ```
4. **Verify signal file still on disk**:
   ```bash
   ls $QUAID_HOME/$QUAID_INSTANCE/data/extraction-signals/
   # Should see at least one *.json signal file
   ```
5. **Restart the daemon**:
   ```bash
   quaid daemon start
   ```
6. **Wait for reprocessing** (30–60s).
7. **Verify**: Signal files should be gone (processed). Daemon log should show extraction running.
   Recall should return content from the pre-kill transcript.

**Pass:** Orphaned signal files reprocessed on restart; content available in recall.
**Fail:** Signal files sit unprocessed; content never extracted.

---

## Running Aux Tests

Each test can be run standalone. There is no prescribed order.

```bash
# Example: run AUX-4 (quick, ~5 min, no daemon restart needed)
# Follow the steps in the AUX-4 section above

# Example: run AUX-1 + AUX-7 back to back (both test restart behavior)
# AUX-1 first, then AUX-7 (they don't conflict)
```

When a test passes or fails, note it in the run log with the commit hash being tested.

---

*Generated post-Run 16 based on PASS-WITH-NOTE items and post-run fixes.*
