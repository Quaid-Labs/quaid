# M18 Proposal: Date-Range Recall Live Test

Status: PROPOSAL ONLY. Do not merge into `livetest-guide/` until Solomon reviews the procedure.

## Purpose

M18 formalizes the date-range recall checks that were previously validated ad hoc on the CC lane after the temporal recall stack landed.

The milestone verifies three production behaviors:

1. Memory recall respects caller-provided temporal bounds (`date_from`, `date_to`, and aliases such as `as_of`).
2. Date-bounded project-docs recall returns only in-window dated `PROJECT.log` evidence and does not leak current undated `PROJECT.md` sections.
3. Structural exact-marker recall bypasses the LLM lexical-anchor planner and returns the exact stored marker without provider timeout noise.

This is a live-test milestone. The goal is not to pass a scripted fixture; it is to confirm the installed product behaves correctly through the normal OC, CC, and CDX surfaces.

## Background

M18 covers the temporal stack validated during Run 115:

- `5c648d7bc`: legacy date-bounded `PROJECT.log` fallback when deployed DocsRAG lacks `date_from` / `date_to` support.
- `a346c9175`: append-only `PROJECT.log` indexing from the docs update path.
- `89bab4e35`: managed-project `project status` / `project diff` no longer fail when `source_root` is intentionally absent.

The CC ad hoc probe originally failed because the positive docs probe used `date_to=2026-04-20` while the actual `PROJECT.log` evidence was dated `2026-04-21`. M18 makes the evidence date explicit before every positive `date_to` probe.

## Lanes

Run M18 on all three live-test lanes after M16 or any other current blocking milestone finishes for that lane.

| Lane | Host surface | Expected Quaid command |
| --- | --- | --- |
| OC | OpenClaw extension | `~/.openclaw/extensions/quaid/quaid` |
| CC | Claude Code plugin | `~/.quaid/plugins/quaid/quaid` |
| CDX | Codex plugin | `~/.quaid/plugins/quaid/quaid` |

Use the lane's normal installed environment. Do not run against the dev checkout unless the lane procedure already requires that for deployment.

Recommended lane variables:

```bash
export M18_LANE=cc       # oc, cc, or cdx
export M18_PROJECT=quaid
export M18_Q="$HOME/.quaid/plugins/quaid/quaid"  # adjust to OC path on OC lane
export M18_HOME="$HOME/.quaid"
export M18_PROJECT_LOG="$HOME/quaid/projects/$M18_PROJECT/PROJECT.log"
```

For OC, set:

```bash
export M18_Q="$HOME/.openclaw/extensions/quaid/quaid"
```

## Preflight

1. Verify the lane can run the installed CLI.

```bash
"$M18_Q" --help >/tmp/m18-quaid-help.txt
```

2. Verify the target project exists and is linked to the current instance.

```bash
"$M18_Q" project status "$M18_PROJECT"
```

Expected:

- Exit code is `0`.
- `source_root` absence is not reported as a fatal source-check error for managed projects.
- If `PROJECT.log` exists, status may report pending or fresh log state, but must not claim the file is invisible when it exists.

3. Index docs through the product path.

```bash
"$M18_Q" docs update --apply --project "$M18_PROJECT"
```

Expected:

- Exit code is `0`.
- If `PROJECT.log` has new entries, output reports `PROJECT.log` indexing or a fresh state.
- No `ImportError` for `index_project_logs`.
- No `date_from` / `date_to` keyword TypeError.

4. Confirm whether `PROJECT.log` chunks exist before docs probes.

```bash
sqlite3 "$M18_HOME/shared/data/docs.db" \
  "SELECT count(*) FROM doc_chunks WHERE source_file LIKE '%PROJECT.log';"
```

Expected:

- Count is greater than `0` after docs update if `PROJECT.log` exists and has dated entries.
- If count is `0`, the docs-date probes are blocked by indexing, not recall. Mark the lane FAIL unless there is no `PROJECT.log` file on disk.

## Seed Data

Use unique lane-scoped markers so cross-lane contamination is visible.

```bash
export M18_OLD_DATE=2026-04-15
export M18_NEW_DATE=2026-04-21
export M18_MEMORY_OLD="m18-$M18_LANE-old-canal-towpath-20260415"
export M18_MEMORY_NEW="m18-$M18_LANE-new-canal-towpath-20260421"
export M18_EXACT="m18-$M18_LANE-palladium-lens-2024"
export M18_DOC_OLD="m18-$M18_LANE-marigold-old-20260415"
export M18_DOC_NEW="m18-$M18_LANE-marigold-new-20260421"
```

### Memory Seeds

Store two memory facts with distinct intended dates.

Preferred path if the lane supports simulated time stamping:

```bash
QUAID_NOW="${M18_OLD_DATE}T12:00:00" \
  "$M18_Q" store "M18 old bounded memory marker: $M18_MEMORY_OLD"

QUAID_NOW="${M18_NEW_DATE}T12:00:00" \
  "$M18_Q" store "M18 new bounded memory marker: $M18_MEMORY_NEW"

"$M18_Q" store "M18 structural exact marker: $M18_EXACT"
```

If `QUAID_NOW` is not honored by the installed lane's store path, use the lane's normal dated transcript/session ingestion path and record the actual stored dates before running the matrix. Do not mark a temporal probe PASS unless the evidence dates are known.

### Project Log Seeds

Append dated project-log entries through the installed product helper, then index through the CLI path.

Do not rely on ambient Python import resolution for this step. Force `PYTHONPATH`
to the installed lane plugin directory so the helper comes from the same Quaid
copy as `$M18_Q`.

```bash
PYTHONPATH="$(dirname "$M18_Q")${PYTHONPATH:+:$PYTHONPATH}" python3 - <<'PY'
import os
from core.docs.updater import append_project_logs

project = os.environ.get("M18_PROJECT", "quaid")
old_date = os.environ["M18_OLD_DATE"]
new_date = os.environ["M18_NEW_DATE"]
old_marker = os.environ["M18_DOC_OLD"]
new_marker = os.environ["M18_DOC_NEW"]

def append_at(date_value, marker):
    # Set both supported simulated-time signals. date_str is the direct helper
    # contract; QUAID_NOW is the broader runtime contract used by live lanes.
    os.environ["QUAID_NOW"] = f"{date_value}T12:00:00"
    append_project_logs(
        {project: [f"M18 dated recall PROJECT.log marker: {marker}"]},
        trigger="m18-date-range-recall",
        date_str=date_value,
    )

append_at(old_date, old_marker)
append_at(new_date, new_marker)
PY

"$M18_Q" docs update --apply --project "$M18_PROJECT"
```

Verify the seeded timestamps before running docs recall:

```bash
grep -n "$M18_DOC_OLD\\|$M18_DOC_NEW" "$M18_PROJECT_LOG"
```

Expected:

- `$M18_DOC_OLD` appears on a line beginning `- [$M18_OLD_DATE`.
- `$M18_DOC_NEW` appears on a line beginning `- [$M18_NEW_DATE`.

If either marker is written with wall-clock date instead of the requested date,
mark the lane FAIL for the seed path and do not run the docs matrix until the
import path or append helper is fixed. If a direct `date_str`-only attempt failed
but the combined `QUAID_NOW` + `date_str` seed passes, record the lane as PWN
for seed-path workaround and continue the docs matrix. Do not manually edit
`PROJECT.log` unless the operator explicitly approves that diagnostic shortcut.

## Off-By-One Guard

Before every positive docs `date_to` probe, derive the evidence date from the log itself. Do not guess based on wall-clock date.

```bash
printf 'M18 PROJECT.log dates:\n'
sed -n 's/^- \[\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]\).*/\1/p' "$M18_PROJECT_LOG" | sort -u | tail -10
```

Required guard:

- The positive `date_to` probe for `$M18_DOC_NEW` must use the actual date printed for `$M18_DOC_NEW`.
- A probe using one day before the evidence date is a negative off-by-one probe. It should exclude the new row. Do not count that as a recall failure.

Optional direct marker check:

```bash
grep -n "$M18_DOC_NEW" "$M18_PROJECT_LOG"
```

## Probe Matrix

### A. Memory Date Bounds

Run the matrix with the normal recall surface.

```bash
"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW"

"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"date_to\":\"$M18_OLD_DATE\"}"

"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"as_of\":\"$M18_OLD_DATE\"}"

"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"date_from\":\"$M18_NEW_DATE\"}"

"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"date_from\":\"$M18_OLD_DATE\",\"date_to\":\"$M18_NEW_DATE\"}"
```

Expected:

| Probe | Expected included | Expected excluded |
| --- | --- | --- |
| No bounds | old + new | none |
| `date_to=$M18_OLD_DATE` | old | new |
| `as_of=$M18_OLD_DATE` | old | new |
| `date_from=$M18_NEW_DATE` | new | old |
| closed range old..new | old + new | none |

Alias spot checks:

```bash
"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"before\":\"$M18_OLD_DATE\"}"

"$M18_Q" recall "$M18_MEMORY_OLD $M18_MEMORY_NEW" \
  "{\"since\":\"$M18_NEW_DATE\"}"
```

Expected alias behavior matches `date_to` and `date_from` respectively. If canonical keys pass but an alias fails, mark the lane PWN, not clean PASS.

### B. Date-Bounded Project Docs Recall

Run docs-only project recall.

```bash
"$M18_Q" recall "$M18_DOC_OLD $M18_DOC_NEW" \
  "{\"stores\":[\"docs\"],\"project\":\"$M18_PROJECT\",\"date_to\":\"$M18_OLD_DATE\"}"

"$M18_Q" recall "$M18_DOC_OLD $M18_DOC_NEW" \
  "{\"stores\":[\"docs\"],\"project\":\"$M18_PROJECT\",\"date_to\":\"$M18_NEW_DATE\"}"

"$M18_Q" recall "$M18_DOC_OLD $M18_DOC_NEW" \
  "{\"stores\":[\"docs\"],\"project\":\"$M18_PROJECT\",\"date_from\":\"$M18_NEW_DATE\"}"

"$M18_Q" recall "$M18_DOC_OLD $M18_DOC_NEW" \
  "{\"stores\":[\"docs\"],\"project\":\"$M18_PROJECT\",\"date_from\":\"$M18_OLD_DATE\",\"date_to\":\"$M18_OLD_DATE\"}"
```

Expected:

| Probe | Expected included | Expected excluded |
| --- | --- | --- |
| `date_to=$M18_OLD_DATE` | old log row | new log row |
| `date_to=$M18_NEW_DATE` | new log row first; old may also appear | no rows after new date |
| `date_from=$M18_NEW_DATE` | new log row | old log row |
| closed range old..old | old log row | new log row |

Hard requirements for every docs-date probe:

- Returned evidence is from `PROJECT.log`, not current `PROJECT.md` boilerplate.
- No undated `PROJECT.md` sections such as title, registered docs, primary artifacts, or directory lists appear as historical evidence.
- No warning-only response like `Date-bounded docs recall requires DocsRAG date_from/date_to support` when matching `PROJECT.log` chunks exist.
- No `TypeError` for unexpected `date_from` or `date_to` kwargs.

Off-by-one negative probe:

```bash
python3 - <<'PY'
from datetime import date, timedelta
import os
new_date = date.fromisoformat(os.environ["M18_NEW_DATE"])
print((new_date - timedelta(days=1)).isoformat())
PY
```

Use the printed date as `date_to`. The new row must be excluded. This is a guard against false PASS from a guessed cutoff date.

### C. Structural Exact Marker Recall

```bash
"$M18_Q" recall "$M18_EXACT"
```

Expected:

- The exact marker row appears in results.
- The result is the stored marker, not a semantically adjacent unrelated memory.
- The recall does not block on or fail with LLM lexical-anchor planner timeout.
- Provider fallback noise is not present for the exact marker path.

If the marker was not seeded on this VM, the probe is not applicable. Seed it and rerun; do not mark missing-marker recall as PASS.

### D. Project Status / Diff Operator Surface

```bash
"$M18_Q" project status "$M18_PROJECT"
"$M18_Q" project diff "$M18_PROJECT"
```

Expected for managed projects with no `source_root`:

- Exit code `0`.
- No fatal `Project <name> has no source_root for shadow-git tracking` error.
- `PROJECT.log` pending bytes / entries reflect actual cursor state.
- If the cursor is current, status is fresh and pending bytes are `0`.

## PASS, PWN, and FAIL

### Clean PASS

A lane is clean PASS only if all are true:

- Memory date-bound matrix returns exactly the expected old/new inclusion behavior.
- `as_of` alias behaves like `date_to`.
- Docs date-bound matrix returns in-window `PROJECT.log` evidence and excludes out-of-window rows.
- Date-bounded docs recall does not leak current undated `PROJECT.md` content.
- The off-by-one negative docs probe excludes the new row.
- Structural exact marker returns the exact seeded marker without LLM planner timeout.
- `project status` and `project diff` do not report no-`source_root` as a fatal error for managed projects.

### PWN

Use PWN when the core product behavior is correct but the run has a procedure or non-critical surface caveat that must be recorded.

Examples:

- Canonical `date_from` / `date_to` keys pass, but one compatibility alias (`before`, `until`, `since`, `after`, `asOf`) fails.
- A tester initially uses the wrong positive cutoff date, then reruns with the actual `PROJECT.log` evidence date and passes.
- Exact marker probe was initially N/A because the marker was not seeded, then passes after seeding.
- `project status` / `project diff` are noisy but recall, indexing, and date filtering are correct. This is PWN only if the noise is not fatal and does not hide `PROJECT.log` state.

### FAIL

Any of these is a lane FAIL:

- Post-cutoff memory appears in `date_to` / `as_of` recall.
- Pre-range memory appears in `date_from` recall.
- Date-bounded docs recall returns current undated `PROJECT.md` sections.
- `PROJECT.log` exists but docs update does not index any `PROJECT.log` chunks.
- Date-bounded docs recall returns empty while in-window indexed `PROJECT.log` chunks exist.
- Date-bounded docs recall emits kwarg TypeError or warning-only fallback instead of rows.
- Structural exact marker recall times out in the LLM lexical-anchor planner.
- A marker seeded in one lane appears in another lane's recall results.
- `project status` or `project diff` exits nonzero solely because a managed project has no `source_root`.

## Expected Failure Modes To Record

Record the exact symptom and the lane when any of these appear:

- `DocsRAG.search_docs_bundle() got an unexpected keyword argument 'date_from'`.
- `Date-bounded docs recall requires DocsRAG date_from/date_to support` with no returned rows.
- `doc_chunks` has zero `PROJECT.log` rows after `docs update --apply`.
- `PROJECT.log` exists on disk but project status reports pending bytes `0` before indexing.
- `ImportError: cannot import name index_project_logs from core.docs.updater`.
- `Project <name> has no source_root for shadow-git tracking` blocks status or diff.
- Exact marker query returns unrelated semantic rows.
- Any LLM provider timeout on the structural exact marker probe.

## Reporting Template

Use this one-line summary per lane:

```text
M18 lane=<oc|cc|cdx> verdict=<PASS|PWN|FAIL> memory=<pass|fail> docs=<pass|fail> exact=<pass|fail|na> status_diff=<pass|pwn|fail> notes="..."
```

Attach or preserve:

- The exact `date_from` / `date_to` values used.
- The `PROJECT.log` date list from the off-by-one guard.
- The `doc_chunks` `PROJECT.log` count before and after `docs update --apply`.
- The top recall rows for each failing probe.
- Any warnings or tracebacks.

## Integration Into livetest-guide/

If approved, add this as a new M18 section after the current temporal / multi-instance milestones. The final guide version should keep the same shape as M13:

- Per-lane setup.
- Command matrix.
- Clean PASS / PWN / FAIL semantics.
- Failure examples and routing.
- Explicit note that the procedure tests production behavior and must not be satisfied by benchmark-only shortcuts.
