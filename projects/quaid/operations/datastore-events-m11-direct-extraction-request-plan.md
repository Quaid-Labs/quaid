# Datastore Events M11 Direct Extraction Request Routing Plan

Status: Python-API-only and hidden CLI request-mode slices complete
Owner: W1 runtime/datastore, W3 recall and identity-context review
Plan source: `projects/quaid/operations/datastore-events-m9-monitor-migration-plan.md`

## Precondition

Do not implement runtime code for M11 until:

1. M9.4 MemoryDB extraction publish request routing is closed through
   W4/W3/W6/W8.
2. M9.5 EvolutionDB snippet/journal request routing is closed through
   W4/W3/W6/W8.
3. M10 Slice 1 and Slice 2 rename work is closed, with compatibility aliases
   retained for installed alpha state.
4. W3 reviews the selected slice because direct extraction can write recallable
   facts, source evidence, snippets, and journal content.
5. W6 reviews the ownership boundary because this slice crosses ingest
   orchestration, MemoryDB, EvolutionDB, and CLI surfaces.
6. W8 confirms static coverage includes extraction, event/request routing,
   datastore contract/manifest checks, snippet/journal tests, and boundary
   checks.

This document records the first Python-API-only M11 runtime slice, the hidden
CLI exposure slice, and remaining deferred decisions. It does not approve
default behavior changes, public CLI promotion, or public push/release actions.

## Goal

M11 selects the previously deferred direct `extract_from_transcript()` / CLI
request-routing question from M9.4 and M9.5.

The goal is narrow: allow direct extraction callers to opt into the existing
MemoryDB and EvolutionDB request-event paths explicitly, while preserving the
current default direct helper behavior.

Selected request events already exist:

- `memory.extraction_publish.request.v1`
- `evolution.snippet_journal_write.request.v1`

M11 should not create new datastore events.

## Current Boundary

Current post-M10 path:

1. `ingest.extract.extract_from_transcript()` extracts raw facts, source chunks,
   snippets, journal entries, and project logs.
2. It calls `apply_extracted_payloads()` once.
3. `apply_extracted_payloads()` already accepts `memory_publish_mode` and
   `snippet_journal_write_mode`, both defaulting to `direct`.
4. The daemon final rolling flush passes both modes as `request`.
5. Direct `extract_from_transcript()` and CLI callers do not pass either mode,
   so they continue using synchronous direct helper calls.
6. Project-log queueing remains in the extraction orchestrator after MemoryDB
   publish and snippet/journal writes.

## Implementation Record

First runtime slice implemented by:

- `6eebe1a59` `refactor(datastore): expose direct extraction request modes`
- `539061237` `test(datastore): cover direct extraction mode guards`

Implemented behavior:

- `extract_from_transcript()` now accepts explicit `memory_publish_mode` and
  `snippet_journal_write_mode` keyword parameters, both defaulting to `direct`.
- The new parameters pass through unchanged to `apply_extracted_payloads()`.
- Existing Python and CLI callers keep the default `direct` / `direct` behavior.
- No CLI flags, new events, daemon route changes, environment sniffing, or hidden
  config routing were added.
- Tests pin default forwarding, independent mode forwarding, call-once behavior,
  invalid-mode raise-through, and no direct-helper fallback for invalid modes.

Validation:

- W4 source-proof PASS on R201 for `6eebe1a59`; `539061237` was test-only and
  needed no fresh live smoke.
- W3 runtime/recall APPROVED/no findings for `6eebe1a59`; `539061237` had no
  runtime/recall delta.
- W6 APPROVED after the test-only follow-up closed the invalid-mode and
  call-once coverage gaps.
- W8 static PASS and runtime HOLD closed for the pair.

CLI exposure slice implemented by:

- `6e413c648` `refactor(datastore): expose hidden CLI request modes`

Implemented behavior:

- `ingest.extract.main()` now builds its argparse parser through
  `_build_cli_parser()` and accepts hidden operator/debug flags:
  `--memory-publish-mode {direct,request}` and
  `--snippet-journal-write-mode {direct,request}`.
- Both flags default to `direct`, use `argparse.SUPPRESS`, and remain absent
  from normal `--help` output.
- Parsed values pass through to the existing `extract_from_transcript()`
  keyword parameters from `6eebe1a59`.
- Invalid values fail through argparse before extraction starts.
- No default request routing, public help entry, daemon route, wrapper, hook,
  event, environment/config routing, or recall behavior changed.
- Tests pin hidden help, literal argparse defaults and choices, independent and
  combined forwarding, representative environment-variable non-routing, and
  invalid-choice stop-before-extraction behavior for both flags.

Validation:

- W4 live/source-proof PASS on R201 for `6e413c648`.
- W3 runtime/recall APPROVED/no findings.
- W6 APPROVED-WITH-CONCERNS with W1 disposition confirming the
  `_build_cli_parser()` extraction is intentional and testability-only.
- W8 static PASS and runtime HOLD closed for the commit.

## Selected First Slice

First slice target: explicit opt-in request routing for direct extraction.

Implementation shape:

- Add explicit keyword arguments to `extract_from_transcript()`:
  - `memory_publish_mode: str = "direct"`
  - `snippet_journal_write_mode: str = "direct"`
- Add those as explicit function parameters, not `**kwargs`, so static checks
  and callers can see the contract and typos fail loudly.
- Pass those values through to `apply_extracted_payloads()` unchanged.
- Preserve the current default direct behavior for all existing Python callers.
- Do not add CLI flags in the first runtime slice. CLI exposure is a separate
  operator-facing surface and requires a follow-up reviewed plan/addendum before
  runtime implementation.
- Do not route direct extraction through request mode by environment sniffing,
  daemon detection, label matching, owner identity, or hidden global config.
- Do not bypass `apply_extracted_payloads()`; it remains the orchestration
  entrypoint for all direct and daemon paths.

The two mode kwargs remain intentionally separate, matching the M9.5 mode-matrix
decision. A consolidated routing-mode abstraction is deferred unless a later
cleanup plan selects it.

## Selected CLI Exposure Slice

Selected scope: hidden operator/debug CLI flags for the existing direct
extraction script only.

Implementation shape:

- Add explicit argparse options to `ingest.extract.main()`:
  - `--memory-publish-mode {direct,request}`
  - `--snippet-journal-write-mode {direct,request}`
- Keep both defaults as `direct`.
- Use `help=argparse.SUPPRESS` so the flags do not appear in normal `--help`.
  These are operator/debug routing controls, not a public user-facing feature.
- Pass parsed values through to `extract_from_transcript()` using the existing
  explicit keyword parameters from `6eebe1a59`.
- Let argparse reject invalid mode values before extraction starts.
- Do not add aliases, environment-variable routing, hidden config routing,
  owner/label sniffing, or default request behavior.
- Do not change the top-level `quaid` shell wrapper or hook/daemon routing.

Rationale:

- Hidden flags give W4/operator tooling a direct way to exercise the existing
  broker paths from the CLI without changing the normal installed user surface.
- Keeping the flags hidden avoids presenting request routing as an end-user
  feature toggle before default behavior and UX are separately reviewed.
- The runtime change should be pass-through only; all request-mode behavior and
  no-fallback validation remains owned by `apply_extracted_payloads()`.

## Mode Matrix

The mode matrix remains independently switchable:

- `direct` / `direct`: current default direct extraction behavior.
- `request` / `direct`: MemoryDB publish routes through the broker;
  snippet/journal writes use the synchronous EvolutionDB helper.
- `direct` / `request`: MemoryDB publish uses the synchronous helper;
  snippet/journal writes route through the broker.
- `request` / `request`: both selected datastore write families route through
  their existing request events.

In every combination:

- `extract_from_transcript()` calls `apply_extracted_payloads()` once.
- MemoryDB publish happens before snippet/journal writes.
- snippet/journal writes happen before project-log queueing.
- `publish_complete` remains after MemoryDB publish, snippet/journal writes,
  and project-log queueing.
- project-log queueing remains in its existing owner and is not moved into
  MemoryDB or EvolutionDB.

## Non-Targets

- no new event names
- no request routing by default for direct Python or CLI callers
- no public/user-facing CLI help entry for request routing
- no daemon routing change; the daemon already selects request mode explicitly
- no broad rewrite of extraction, chunking, LLM prompting, repair, or carry-fact
  behavior
- no change to MemoryDB fact/source/edge storage semantics
- no change to EvolutionDB snippet/journal file paths, file formats, duplicate
  handling, trigger labels, or journal archiving
- no project-log queue ownership change
- no recall ranking, scoring, planner, source-window, or source metadata policy
  change
- no `.ego` import/export behavior change
- no alias retirement or `notedb.core` plugin-id rename

## FailHard Policy

- Request broker, handler, validator, MemoryDB write, and EvolutionDB write
  failures must not fall back to the synchronous helper after request mode is
  selected.
- The request-mode primary path and synchronous helper path must not share a
  `try`/`except` scope that could catch broker, handler, or validator failures
  and fall through to direct-helper invocation. Each mode raises out of its own
  branch.
- Existing warn-before-raise validator discipline from M9.4/M9.5 must be
  preserved. New raise paths introduced by this slice must use the centralized
  warn-then-raise helper pattern when they surface runtime request failures.
- `failHard=true` must raise through the direct extraction caller.
- `failHard=false` may report degraded/failure metrics only where the existing
  helper/request path already does so; it must not claim facts, snippets, or
  journal entries were stored when the selected request path failed.

## Required Tests Before W4

Focused tests should prove:

- `extract_from_transcript()` defaults to `direct` / `direct` and preserves
  existing output shape.
- `extract_from_transcript(memory_publish_mode="request")` forwards request mode
  to `apply_extracted_payloads()` without changing snippet/journal mode.
- `extract_from_transcript(snippet_journal_write_mode="request")` forwards
  request mode without changing MemoryDB mode.
- `request` / `request` direct extraction routes through both existing broker
  events and produces the same visible fact/source/snippet/journal results as
  direct mode under controlled fake services.
- Invalid mode values raise loudly and do not partially write through a fallback
  route.
- Broker/handler/validator failure in either request path raises without
  invoking the corresponding synchronous helper fallback.
- Request/direct mode matrix preserves project-log queueing and
  `publish_complete` ordering.
- CLI behavior remains unchanged because first-slice runtime must not add CLI
  request-mode flags.
- Existing daemon request-mode tests continue to pass unchanged.

## Required CLI Tests Before W4

Focused tests for the CLI exposure slice should prove:

- `python3 ingest/extract.py --help` still exits 0 and does not show
  `--memory-publish-mode` or `--snippet-journal-write-mode`.
- Omitting both flags keeps CLI behavior at `direct` / `direct`.
- Passing `--memory-publish-mode request` forwards request mode to
  `extract_from_transcript()` without changing snippet/journal mode.
- Passing `--snippet-journal-write-mode request` forwards request mode to
  `extract_from_transcript()` without changing MemoryDB mode.
- Passing both request flags forwards `request` / `request`.
- Invalid mode values fail through argparse before extraction starts.
- No environment variable or hidden config can flip CLI routing modes. Prove
  this primarily by inspecting the argparse action configuration for the two
  hidden flags and confirming their defaults are literal `direct` values with
  no env/config sourcing. A representative runtime guard may also set a
  plausible variable such as `QUAID_MEMORY_PUBLISH_MODE=request` and assert the
  CLI default still forwards `direct` / `direct`.

## W4 Smoke

W4 should smoke runtime code only after W3/W6/W8 review:

- installed direct extraction with default CLI/Python behavior still writes via
  the existing direct path and produces the same visible outputs
- installed direct extraction with explicit request modes writes recallable facts
  plus visible snippet and journal content through the broker paths
- request-mode failure under failHard stops loudly with no direct-helper fallback
- identity/context or journal recall sees the same persisted content after
  request-mode writes
- project-log queueing still occurs after MemoryDB publish and snippet/journal
  writes
- M9.2 DocsDB, M9.3 session ingest, M9.4 MemoryDB daemon request, M9.5
  EvolutionDB daemon request, and M10 compatibility aliases remain healthy

For the CLI exposure slice, W4 should additionally smoke the installed
`ingest/extract.py` CLI with default direct behavior and with both hidden
request-mode flags passed explicitly.

## Deferred Decisions

- whether direct request mode should ever become the default
- whether the hidden CLI request-mode flags should ever become public
  user-facing flags
- whether to consolidate the two extraction routing mode kwargs into a future
  routing options object
- lifecycle persistence and SessionDB first-party manifest registration
- source-window metadata enrichment
- extraction producer routing through separate snippet/journal request events
  closed in M13 at `516732b88` + `9437788d`; default request routing remains
  deferred
- compatibility-alias retirement and `notedb.core` plugin-id rename
- `.ego` import/export integration
