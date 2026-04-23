# XP: Cross-Platform Project Linking Test

## Cross-Platform Project Linking Test (XP)

Run this only after both OpenClaw and Claude Code have passed the project-system and multi-instance milestones (run `ls tests/livetest/livetest-guide/` for the current set; as of this writing that's through `M5.md`). CDX does not participate — CDX agents are path-derived; its equivalent project-linking behavior is covered under its project-system and silo-isolation milestone parts. XP is coordinator-orchestrated, not a per-platform milestone number.

This is explicitly a user-behavior test. The agent should be able to discover
how to link and use the project without being given function names.

### Phase 1: Create the project and add a doc in OpenClaw

Prepare a source root:

```bash
ssh REMOTE_HOST 'mkdir -p ~/quaid/projects/cross-live-test-src && cat > ~/quaid/projects/cross-live-test-src/main.py <<\"PY\"
def harbor_status():
    return "North pier beacon is offline"
PY'
```

Ask OC naturally:

- `Can you create a project named cross-live-test for ~/quaid/projects/cross-live-test-src?`
- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add a project document that says the north pier beacon is offline and the maintenance window starts at 02:15 UTC.`

Verify from shell:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry list 2>&1 | grep cross-live-test'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs list --project cross-live-test 2>&1'
```

If the doc file exists but is not listed, register it manually:

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry register <path-to-doc> --project cross-live-test 2>&1'
```

After the doc is registered, run `docs update --apply` to index it (new standalone docs with no
existing chunks should be detected and indexed automatically — this is what M10 verifies):

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs update --apply 2>&1 | tail -20'
```

Expected output includes "Indexing new doc:" for the registered file. If it says "All docs up-to-date"
instead, that is a regression — fall back to `janitor --task rag --apply` to unblock the test and
report to claude-dev.

Then verify recall:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Then ask OC:

- `What does the cross-live-test project doc say about the beacon?`

Pass:
- OC can retrieve the doc content through Quaid

### Phase 2: Link the same project in Claude Code and add a second doc

**Ordering**: Phase 2 assumes OC's Phase 1 has landed — CC LINKS to an existing
`cross-live-test` rather than creating fresh. If OC is blocked upstream and CC
reaches this milestone first, CC's natural-directive create will attach to the
visible-home project dir under its own instance registry; the coordinator
cross-registration step below (`Cross-link docs across instances`) handles the
multi-instance linking regardless. Lane interleaving is expected.

Ask CC naturally:

- `Do you see the existing cross-live-test project? Can we add a document to it?`
- `Please add another project document that says code word Ember Glass means pager escalation level 2.`

Verify from shell:

```bash
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid registry list 2>&1 | grep cross-live-test'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid docs list --project cross-live-test 2>&1'
ssh REMOTE_HOST 'cd ~/quaid && QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

Pass:
- CC can use the existing project rather than needing a new one
- CC can add a doc and Quaid can recall it

### Cross-link docs across instances before Phase 3

Each adapter maintains its own docs index. After both docs are registered, each instance
only has its own doc indexed. Cross-link by registering each doc in the other instance,
then run `docs update --apply` on both. The daemon picks up doc changes lazily — always
run `docs update --apply` explicitly rather than waiting, and wait for it to confirm
indexing before proceeding to Phase 3.

**Before running `docs update --apply`, sanity-check project registry for orphans.**
`docs update --apply` will recreate scaffold dirs for any project with a live registry
entry, including leftovers from prior M13 runs where the project wasn't deleted. If
`quaid project list` shows stale `misc--*-m13-test` entries, delete them first:

```bash
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid project list 2>&1 | grep -i m13 || echo "no m13 orphans"'
# If any m13-test projects appear, run:
# ssh REMOTE_HOST '... quaid project delete misc--<instance>-m13-test'
```

```bash
# Register OC beacon doc in CC instance
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid registry register <path-to-beacon-doc> --project cross-live-test 2>&1'
# Register CC Ember Glass doc in OC instance
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid registry register <path-to-ember-glass-doc> --project cross-live-test 2>&1'

# Force index on both — wait for "Indexed" confirmation before continuing
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid docs update --apply 2>&1'
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid docs update --apply 2>&1'
```

Verify cross-instance CLI recall before asking agents conversationally:

```bash
# CC must find beacon (OC-added doc)
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=claude-code-private-tmp-cc-livetest ~/.quaid/plugins/quaid/quaid recall "north pier beacon" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
# OC must find Ember Glass (CC-added doc)
ssh REMOTE_HOST 'QUAID_HOME=/Users/admin/.quaid QUAID_INSTANCE=openclaw-main ~/.openclaw/extensions/quaid/quaid recall "Ember Glass" "{\"stores\":[\"docs\"],\"project\":\"cross-live-test\"}" 2>&1'
```

If either CLI recall fails after `docs update --apply`, stop and report to claude-dev — the cross-link registration or indexing is not working and conversational Phase 3 will also fail.

### Phase 3: Cross-recall both directions

Ask CC (use content-specific phrasing so the model matches the doc, not just PROJECT.md):

- `Can you search the cross-live-test project docs for anything about the north pier beacon?`

If that still returns nothing, try more specific phrasing:

- `What does the cross-live-test project say about the north pier beacon maintenance window?`

Ask OC via Matrix:

```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "Can you search the cross-live-test project docs for anything about Ember Glass escalation?"'
```

If no answer, try:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "What is the escalation code word Ember Glass in the cross-live-test project docs?"'
```

Optional provenance follow-up:
```bash
ssh REMOTE_HOST '~/quaidcode/dev/modules/quaid/tests/livetest/scripts/matrix-send "How did you know that?"'
```

Note: The generic "What does the project say about X?" framing matches PROJECT.md in the vector index
and misses content docs. Use docs-specific phrasing that names the concept explicitly so the model
searches the docs store. Both prompts above are content-specific and reliably surface the right doc.

Pass:
- CC can answer from the OC-added doc
- OC can answer from the CC-added doc
- answers are grounded in Quaid project context, not raw disk browsing as the
  first move

Fail:
- either side cannot see the same project
- either side cannot retrieve the other side's doc
- the agent only succeeds when given explicit command names

