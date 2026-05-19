# Datastore Events M33 Post-Facade Decision Boundary

Status: decision-boundary record only; no runtime implementation selected
Owner: W1 runtime/datastore with W3 ownership for recall-quality slices
Plan source: post-M32 closure review across M9-M32 operations docs

## Purpose

M32 closed the fourth and final facade lifecycle emitter slice. The facade now
has explicit `processLifecycleEvent()` branches for:

- `CompactionSignal` -> `session.compaction` -> M26 daemon compaction signal
- `ResetSignal` -> `session.reset` -> M27 daemon reset signal
- `TimeoutSignal` -> `session.timeout` -> M25 daemon timeout signal
- `AgentEndSignal` -> `session.agent_end` -> M24 daemon `session_end` signal

M33 does not select the next runtime patch. It records the decision boundary
after the four-emitter facade family so autonomous agents do not treat a
remaining deferred item as implicitly approved work.

## Current Boundary

1. The M29-M32 facade emitter family is complete and closed through W4/W3/W6/W8.
2. M24-M27 remain the only owners of daemon signal writing for their default
   lifecycle events.
3. M28 remains the only event-bus wake/start parity owner after a lifecycle
   bridge writes a compatible daemon signal.
4. OpenClaw hook paths and adapter direct-signal writers remain unchanged.
5. MemoryDB still owns `session_chunks` recall/write projection.
6. SessionDB first-party metadata and request ownership are closed through M14
   and M16, but `SessionDB.capabilities.recall` remains `[]`.
7. M19 source-window metadata enrichment is closed, while source-window selector
   ownership and recall policy remain deferred.
8. Hidden request-mode CLI flags remain operator/debug controls; default request
   routing and public CLI promotion remain deferred.
9. Broad compatibility-alias retirement, `notedb.core` plugin-id rename, and
   `.ego` import/export integration remain separate product/operator decisions.

## Decision Buckets

### A. Lifecycle Hook Migration And Adapter Retirement

Includes:

- OpenClaw hook migration to facade lifecycle emitters.
- Adapter direct-signal retirement, if ever approved.
- Daemon restart/stop automation from lifecycle events.

Owner:

- W1 can implement only after explicit Solomon/Hermes approval selects a narrow
  runtime slice.

Required gates after selection:

- W4 live validation is required.
- W6 review is required.
- W8 static validation is required.
- W3 review is required if timing or payload changes can affect recall/session
  evidence or source-window behavior.

Non-selection rule:

- Do not alter adapter hook wiring, direct `write_signal()` callsites, daemon
  process lifecycle, wake behavior, signal shapes, or duplicate-suppression
  semantics without a selected plan.

### B. Recall And Source-Window Ownership

Includes:

- Source-window selector ownership or SessionDB recall capability.
- Source-window ranking/planner policy changes.
- Any change to MemoryDB `session_chunks` recall/write ownership.

Owner:

- W3 owns the recall-quality plan and benchmark boundary. W1 should not start
  implementation unless W3 approves the selected behavior contract.

Required gates after selection:

- W3 benchmark/recall review is required before runtime lands.
- W4 live validation is required for a product-visible runtime slice.
- W6 review and W8 static validation are required.

Non-selection rule:

- Do not add SessionDB recall selectors, move `session_chunks`, change
  source-window row selection, alter ranking/planner policy, or change output
  token/context behavior as an incidental datastore refactor.

### C. Request Routing And CLI Exposure

Includes:

- Whether direct request mode should become the extraction default.
- Whether hidden request-mode CLI flags should become public user-facing flags.
- Consolidating extraction routing mode kwargs into a future options object.

Owner:

- W1 can implement after explicit operator approval and W3 review where recall
  data shape or persistence timing can change.

Required gates after selection:

- W4 installed CLI/runtime smoke is required.
- W6 review and W8 static validation are required.
- W3 review is required for recall-visible routing behavior.

Non-selection rule:

- Do not change defaults, public help output, environment/config routing, or
  failHard fallback behavior without a selected plan.

### D. Compatibility, Naming, And Product Export

Includes:

- Broad `datastore.notedb` / `core.plugins.notedb_contract` compatibility-alias
  retirement.
- `notedb.core` plugin-id rename.
- `.ego` import/export integration.

Owner:

- Operator/product decision first; W1 or the relevant product owner implements
  only after the compatibility and installed-alpha impact are selected.

Required gates after selection:

- Explicit user-data/installed-alpha migration plan.
- W8 integration/release guidance before any public movement.
- W4 installed upgrade/runtime validation when runtime behavior changes.

Non-selection rule:

- Do not remove aliases, rename plugin ids, or change `.ego` surfaces as cleanup.

## What W1 Can Do Without A New Runtime Selection

Allowed:

- Correct stale operations-doc cross-references.
- Keep status/inventory records aligned with already-closed commits.
- Run local read-only audits and summarize decision boundaries.
- Prepare a draft plan that explicitly says no runtime work is approved.

Not allowed without selection:

- New runtime lifecycle behavior.
- Adapter hook rewiring.
- Recall/source-window policy movement.
- CLI/default routing behavior changes.
- Alias retirement or product export/import work.

## W4 Status

W4 live validation is not needed for this decision-boundary record or other
docs-only status syncs. W4 is needed and should be dispatched in parallel with
W6/W8 only after a selected runtime behavior slice lands.

## Next Action

Solomon/Hermes should select one of the decision buckets above, or explicitly
pause datastore runtime work after the M32 facade-family closure. Until then,
W1 should avoid runtime edits and limit work to docs/status alignment or
read-only audits.
