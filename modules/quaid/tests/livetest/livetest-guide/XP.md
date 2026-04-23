# XP: Cross-Platform Project Linking

Run this after every lane has passed all named milestones (M1 through M7).
XP itself runs on the first two lanes that finish — the coordinator doesn't
wait on the slowest lane to start. If OC is still running M7 when CC and CDX
are done, XP runs between CC and CDX.

This is explicitly a user-behavior test. The agent on each platform should be
able to discover how to link and use a cross-instance project without being
given function names.

Throughout the procedure, `PLATFORM1` is the first lane to finish; `PLATFORM2`
is the second. Substitute the actual platform names before running.

## Phase 0 — Pick a shared project

XP reuses the `livetest-agentmsg-<LANE>` projects M4 already created and
re-registered at the end of Part A. Pick one as the shared target for XP —
typically the one owned by `PLATFORM1`. The shared name is just
`livetest-agentmsg-xp` (no lane suffix) so both instances can reference the
same project without colliding on the per-lane suffix.

Copy the source-root on the remote so both instances can resolve the same
path:

```bash
ssh REMOTE_HOST 'cp -R \
  ~/quaidcode/dev/modules/quaid/tests/livetest/livetest-guide/data/test-project \
  ~/quaid/projects/livetest-agentmsg-xp-src'
```

## Phase 1 — Create and add a doc on PLATFORM1

Ask the `PLATFORM1` agent naturally (no function names):

- `Can you create a project called livetest-agentmsg-xp pointing at`
  `~/quaid/projects/livetest-agentmsg-xp-src?`
- `Please register the README and the api.py file into that project.`
- `Add a project document saying the north pier beacon is offline and the`
  `maintenance window starts at 02:15 UTC.`

Verify from the shell:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 registry list | grep livetest-agentmsg-xp"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 docs list --project livetest-agentmsg-xp"
```

If the doc exists on disk but is not listed, register manually:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 registry register <path> \
  --project livetest-agentmsg-xp"
```

Force indexing on the PLATFORM1 instance:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 docs update --apply"
```

Verify recall on PLATFORM1:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 recall 'north pier beacon' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"
```

Then ask PLATFORM1 agent: `What does the cross-live project say about the
beacon?`. The agent answers from the doc.

## Phase 2 — Link the same project on PLATFORM2 and add a doc

Phase 2 assumes PLATFORM1 has landed. PLATFORM2 links to the existing
project rather than creating fresh.

Ask the `PLATFORM2` agent naturally:

- `Do you see the existing livetest-agentmsg-xp project? Can we add a doc`
  `to it?`
- `Add a project document that says the codeword Ember Glass means pager`
  `escalation level 2.`

Verify from the shell:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 registry list | grep livetest-agentmsg-xp"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 docs list --project livetest-agentmsg-xp"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 recall 'Ember Glass' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"
```

Pass: PLATFORM2 uses the existing project rather than needing a new one;
PLATFORM2 can add a doc and Quaid can recall it on that instance.

## Phase 3 — Cross-link docs across instances

Each adapter maintains its own docs index. After both docs are registered,
each instance only has its own doc indexed. Cross-link by registering each
doc in the other instance's registry, then run `docs update --apply` on
both.

**Before `docs update --apply`, sanity-check project registry for orphans.**
`docs update --apply` will recreate scaffold dirs for any project with a
live registry entry, including leftovers from prior M5 Part B test runs. If
`quaid project list` shows stale `misc--*-m5-test` entries, delete them
first.

```bash
# Register PLATFORM1's beacon doc in PLATFORM2's instance
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 registry register \
  <path-to-beacon-doc> --project livetest-agentmsg-xp"

# Register PLATFORM2's Ember Glass doc in PLATFORM1's instance
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 registry register \
  <path-to-ember-glass-doc> --project livetest-agentmsg-xp"

# Force index on both
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 docs update --apply"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 docs update --apply"
```

Verify cross-instance CLI recall before asking agents conversationally:

```bash
# PLATFORM2 must find beacon (PLATFORM1-added doc)
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 recall 'north pier beacon' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"

# PLATFORM1 must find Ember Glass (PLATFORM2-added doc)
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 recall 'Ember Glass' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"
```

If either CLI recall fails after `docs update --apply`, stop and report to
the coordinator — cross-link registration or indexing is not working and
conversational Phase 4 will also fail.

## Phase 4 — Cross-recall both directions

Ask PLATFORM2 agent (content-specific phrasing so the model matches the doc
instead of PROJECT.md):

- `Can you search the livetest-agentmsg-xp project docs for anything about`
  `the north pier beacon?`

Ask PLATFORM1 agent:

- `Can you search the livetest-agentmsg-xp project docs for anything about`
  `Ember Glass escalation?`

Optional provenance follow-up: `How did you know that?`

Note: the generic "What does the project say about X?" framing matches
PROJECT.md in the vector index and misses content docs. Use docs-specific
phrasing that names the concept explicitly.

## Pass

- PLATFORM2 answers from the PLATFORM1-added doc.
- PLATFORM1 answers from the PLATFORM2-added doc.
- Answers are grounded in Quaid project context, not raw disk browsing as
  the first move.

## Fail

- Either side cannot see the same project.
- Either side cannot retrieve the other side's doc.
- Agents only succeed when given explicit command names.
