# XP: Cross-Platform Project Linking

Run this after every lane has passed all named milestones (M1 through M7).
XP itself runs on the first two lanes that finish — the coordinator doesn't
wait on the slowest lane to start. If OC is still running M7 when CC and CDX
are done, XP runs between CC and CDX.

This is explicitly a user-behavior test. The agent on each platform should
handle both project-access contracts without being given function names:

- Non-durable one-fact lookup: answer the requested fact without linking the
  project into the current instance.
- Durable project work: link the project before proceeding because linking
  pulls in that project's tools, agent files, and hot memory.

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
- `Add a separate project document saying Ember Glass means pager escalation`
  `level 2.`

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
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 recall 'Ember Glass' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"
```

Then ask PLATFORM1 agent: `What does the cross-live project say about the
beacon?`. The agent answers from the doc.

## Phase 2A — Non-durable one-fact lookup on PLATFORM2, no link expected

Phase 2 assumes PLATFORM1 has landed. PLATFORM2 links to the existing
project only after the durable-work request in Phase 2B. First, test the
one-fact lookup branch.

Important: before PLATFORM2 links it, `quaid project list/show` on
PLATFORM2 may not display `livetest-agentmsg-xp` yet. That is normal. The
Phase 2A action is for the agent to answer one requested fact without
linking the project. Do not fail early just because the unlinked PLATFORM2
instance cannot already list it.

Capture PLATFORM2's project list before the prompt:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 project list --names-only \
  > /tmp/xp-platform2-projects-before.txt"
```

Ask the `PLATFORM2` agent naturally:

- `I just want one fact from the livetest-agentmsg-xp project. What does`
  `Ember Glass mean?`

Expected behavior: the agent answers that Ember Glass means pager escalation
level 2 using scoped recall, project discovery, or direct file read, but it
does **not** run `quaid project link`.

Verify PLATFORM2's project list did not change:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 project list --names-only \
  > /tmp/xp-platform2-projects-after-fact.txt && \
  diff -u /tmp/xp-platform2-projects-before.txt /tmp/xp-platform2-projects-after-fact.txt"
```

Pass: PLATFORM2 answers the fact correctly and the project list is unchanged.
Fail: PLATFORM2 links the project during this one-fact lookup, or cannot
answer the fact by any non-link path.

## Phase 2B — Durable project work on PLATFORM2, link expected

Now ask for durable work. This is the branch where linking is required.

Ask the `PLATFORM2` agent naturally:

- `I want you to start working on the livetest-agentmsg-xp project. Please`
  `link it into this instance so you can use its project tools and files.`
- `Add a project document that says Copper Basin means maintenance queue`
  `priority 4.`

Expected behavior: the agent runs `quaid project link livetest-agentmsg-xp`
or equivalent project-link action before proceeding, then adds the doc.

Force indexing on the PLATFORM2 instance after the agent creates the doc:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 docs update --apply"
```

Verify from the shell:

```bash
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 registry list | grep livetest-agentmsg-xp"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 docs list --project livetest-agentmsg-xp"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 project list --names-only | grep '^livetest-agentmsg-xp$'"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 recall 'Copper Basin' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"
```

These checks run after the PLATFORM2 agent has linked the shared project,
added the durable-work doc, and `docs update --apply` has been run.

Pass: PLATFORM2 uses the existing project rather than needing a new one;
PLATFORM2 can add a doc and Quaid can recall it on that instance.

Fail: PLATFORM2 refuses or forgets to link after the durable-work request,
creates a duplicate project instead of linking the existing one, or cannot
recall the new doc on that instance.

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

# Register PLATFORM2's Copper Basin doc in PLATFORM1's instance
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 registry register \
  <path-to-copper-basin-doc> --project livetest-agentmsg-xp"

# Force index on both
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 docs update --apply"
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 docs update --apply"
```

Verify cross-instance CLI recall before asking agents conversationally:

```bash
# PLATFORM2 must find beacon (PLATFORM1-added doc)
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_2 \$QCLI_2 recall 'north pier beacon' \
  '{\"stores\":[\"docs\"],\"project\":\"livetest-agentmsg-xp\"}'"

# PLATFORM1 must find Copper Basin (PLATFORM2-added doc)
ssh REMOTE_HOST "QUAID_INSTANCE=\$INSTANCE_1 \$QCLI_1 recall 'Copper Basin' \
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
  `Copper Basin maintenance priority?`

Optional provenance follow-up: `How did you know that?`

Note: the generic "What does the project say about X?" framing matches
PROJECT.md in the vector index and misses content docs. Use docs-specific
phrasing that names the concept explicitly.

## Pass

- PLATFORM2 answers from the PLATFORM1-added doc.
- PLATFORM1 answers from the PLATFORM2-added doc.
- Phase 2A one-fact lookup answers correctly without linking.
- Phase 2B durable-work request links the project before modifying or using
  its project tools/files.
- Phase 4 answers are grounded in Quaid project context, not raw disk
  browsing as the first move after the durable link.

## Fail

- Either side cannot see the same project.
- Either side cannot retrieve the other side's doc.
- Agents only succeed when given explicit command names.
- PLATFORM2 links during the Phase 2A one-fact lookup.
- PLATFORM2 does not link during the Phase 2B durable-work request.
