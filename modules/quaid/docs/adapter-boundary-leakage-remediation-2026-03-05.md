# Adapter Boundary Leakage Remediation

Date: 2026-03-05
Branch: canary
Scope: Full remediation of bidirectional adapter boundary leakage identified by audit (`18 findings`).

## Why this work was done
The adapter/facade/core boundary had drifted in both directions:
- Core/facade contained OpenClaw-specific assumptions (session keys, provider aliases, message markers, notification transport details).
- Adapter still owned cross-platform business logic (privacy filtering, injection dedup state policy, recall formatting/grouping, query quality heuristics, compaction summary batching).

Goal: make adapter swappable by ensuring:
- Core policy is adapter-agnostic.
- Adapter only owns OpenClaw/platform transport details.
- Cross-platform policy/state logic lives in facade/core.

---

## Final status by audit bucket

### Must-fix pre-swap (7/7 complete)
1. `C001` Remove hardcoded `adaptors.openclaw.maintenance` default from lifecycle resolution.
2. `C002` Remove hardcoded `clawdbot/openclaw` notify transport assumption in core runtime notify path.
3. `C003` Remove hardcoded `agent:main:main` fallback from facade.
4. `C004` Remove hardcoded `GatewayRestart/System` filters from facade internals; use transcript callback path.
5. `C006` Move provider alias policy out of facade hardcoding into adapter-provided alias map.
6. `A001` Move injection dedup read/write business logic from adapter into facade.
7. `A002` Move privacy filtering from adapter into facade.

### Should-fix (5/5 complete)
8. `C005` Rename provider callback concept to adapter-agnostic naming (`getDefaultLLMProvider`).
9. `A003` Move compaction notification batch policy/state machine into facade.
10. `A004` Move recall response formatting/grouping into facade.
11. `A005` Move query quality/ack gate into facade.
12. `A006` Move compaction injection-log reset policy into facade.

### Backlog/code+docs items (6/6 complete)
13. `C007` Generalize OpenClaw-specific comments in events runtime.
14. `C008` Generalize notify env target resolution by channel.
15. `C009` Remove Telegram-specific API docstring example.
16. `C010` Replace “gateway config” wording with adapter-generic wording.
17. `A007` Confirm transcript preprocessing remains adapter-owned and wire core checks through callback path.
18. `A008` No required move; adapter transient HTTP retry classification remains intentionally transport-specific.

---

## Detailed implementation changes

## 1) Facade contract changes (core/facade.ts + core/facade.js)

### Dependency contract made adapter-agnostic
- Replaced `getGatewayDefaultProvider` with `getDefaultLLMProvider`.
- Added `providerAliases?: Record<string,string>` for adapter-provided provider mapping.
- Added `resolveDefaultSessionId?: () => string` to eliminate hardcoded session-key assumptions.

### Provider resolution migration
- Removed hardcoded alias logic from facade (`openai-codex`, `anthropic-claude-code`).
- Added alias normalization through `deps.providerAliases`.
- Provider fallback now uses `getDefaultLLMProvider` callback.

### Transcript skip policy migration
- Added facade helper `shouldSkipUserText(text)` using `deps.transcriptFormat.shouldSkipText`.
- Rewired extraction-log topic hint selection and `extractSessionId` filtering to use this callback path.
- Removed hardcoded `GatewayRestart:` / `System:` checks in facade internals.

### Session fallback migration
- `resolveMemoryStoreSessionId` now uses:
  - context session id
  - session-key callback
  - `resolveDefaultSessionId` callback
  - most-recent fallback
- No hardcoded `agent:main:main` in core/facade anymore.

### Adapter->core policy moves now owned by facade
Added facade methods:
- `isLowQualityQuery(query)`
- `filterMemoriesByPrivacy(memories, currentOwner)`
- `loadInjectedMemoryKeys(sessionId)`
- `saveInjectedMemoryKeys(sessionId, previousKeys, memories, maxEntries)`
- `resetInjectionDedupAfterCompaction(sessionId)`
- `formatRecallToolResponse(results)`
- `queueCompactionExtractionSummary(sessionId, stored, skipped, edges, notify)`

### Compaction batch state move
- Added compaction batch constants/state to facade.
- Batching, cooldown windowing, and summary payload construction now live in facade.
- Adapter now provides only the final notify transport callback.

---

## 2) Adapter refactor (adaptors/openclaw/adapter.ts + .js)

### Facade dependency wiring updates
- Adapter now passes:
  - `getDefaultLLMProvider: getGatewayDefaultProvider`
  - `providerAliases` map for OpenClaw provider IDs
  - `resolveDefaultSessionId` callback

### Removed adapter-owned business policy blocks
- Removed local query acknowledgment regex/word-count gate logic.
- Removed local privacy filter expression.
- Removed local injection-log read/parse/write/merge logic.
- Removed local compaction injection-log reset write logic.
- Removed local compaction summary batch state machine (type/state/function).
- Removed local recall grouping/formatting rendering logic.

### Adapter now delegates to facade for policy/state
- Uses `facade.isLowQualityQuery`.
- Uses `facade.filterMemoriesByPrivacy`.
- Uses `facade.loadInjectedMemoryKeys` / `saveInjectedMemoryKeys`.
- Uses `facade.resetInjectionDedupAfterCompaction`.
- Uses `facade.formatRecallToolResponse`.
- Uses `facade.queueCompactionExtractionSummary` with OpenClaw notify callback.

### What remains adapter-owned intentionally
- OpenClaw gateway transport and credentials.
- OpenClaw hook registration and event plumbing.
- OpenClaw transcript preprocessing and skip callbacks.
- OpenClaw-specific spawn/detached process behavior.
- OpenClaw-specific transient HTTP retry classification.

---

## 3) Lifecycle and runtime core decoupling

## core/lifecycle/janitor_lifecycle.py
- Removed hardcoded default module `adaptors.openclaw.maintenance`.
- Resolution strategy now:
  1. Active adapter manifest module (if configured).
  2. Generic adapter-folder discovery fallback (`adaptors/*/maintenance.py`) without adapter literals.
  3. Empty fallback if none found.
- `build_default_registry()` now appends workspace routine module only when resolved.

## core/runtime/notify.py
- Added `_resolve_message_cli()`:
  - Uses `QUAID_NOTIFY_CLI` or `QUAID_MESSAGE_CLI`.
  - No hardcoded CLI names.
- Added `_resolve_direct_target(channel, explicit_target)`:
  - Generic `QUAID_<CHANNEL>_TARGET` + `QUAID_NOTIFY_TARGET` fallback.
- Updated direct-send CLI error/help text to be adapter-neutral.

---

## 4) Core docs/comments cleanup

## core/runtime/events.py
- Generalized comments from “OpenClaw ...” to “Adapter ...”.

## core/interface/api.py
- Source example changed from `telegram` to adapter-neutral `chat`.

## core/lifecycle/workspace_audit.py
- Comment changed from “gateway config” to “adapter config”.

---

## 5) Test updates

## tests/facade.test.ts
Updated existing tests for new contracts:
- Default provider callback rename and alias map usage.
- Session default fallback via `resolveDefaultSessionId`.

Added new tests for migrated facade ownership:
- `isLowQualityQuery` gate behavior.
- `filterMemoriesByPrivacy` behavior.
- Injection dedup load/save/reset round-trip.
- `formatRecallToolResponse` grouping and breakdown.
- Compaction summary batching behavior.

---

## Validation evidence

### Runtime parity
- `npm run check:runtime-pairs:strict` -> PASS

### Targeted TypeScript tests
- `npm run test:run -- tests/facade.test.ts tests/chat-flow.integration.test.ts tests/delayed-requests.integration.test.ts` -> PASS

### Targeted Python tests
- `python3 -m pytest tests/test_janitor_lifecycle.py tests/test_notify.py tests/test_events.py` -> PASS

---

## File-level change log

- `modules/quaid/core/facade.ts`
- `modules/quaid/core/facade.js`
- `modules/quaid/adaptors/openclaw/adapter.ts`
- `modules/quaid/adaptors/openclaw/adapter.js`
- `modules/quaid/core/lifecycle/janitor_lifecycle.py`
- `modules/quaid/core/runtime/notify.py`
- `modules/quaid/core/runtime/events.py`
- `modules/quaid/core/interface/api.py`
- `modules/quaid/core/lifecycle/workspace_audit.py`
- `modules/quaid/tests/facade.test.ts`
- `modules/quaid/docs/adapter-boundary-leakage-remediation-2026-03-05.md`

---

## Known intentional non-changes
- Adapter transport-specific retry heuristics remain in adapter (`A008`) by design.
- OpenClaw transcript preprocessing remains adapter-owned and is now the source for skip behavior consumed by facade (`A007`).

---

## Swappability impact
After this remediation:
- Core no longer embeds OpenClaw literals for session key fallback, provider alias policy, notify transport CLI defaults, or marker filtering policy.
- Adapter sheds substantial cross-platform policy/state logic and delegates to facade.
- Remaining adapter responsibilities are predominantly transport, hook wiring, and platform-specific integration.
