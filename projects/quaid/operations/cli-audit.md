# CLI Audit (2026-02-22)

Purpose: evaluate current `quaid` CLI commands for practical runtime value in alpha, identify low-value/overlapping commands, and prioritize UX improvements.

## Command Surface

### Keep (high value, core operations)
- `search`, `find`, `get`, `forget`
- `store`, `edge`
- `stats`, `doctor`
- `janitor`
- `mcp-server`
- `docs` (search/check/update/changelog)
- `registry` / `project` / `projects`
- `config` (now includes interactive menu)

### Keep but mark advanced
- `export`
- `migrate`
- `re-embed`
- `updater`
- low-level `memory_graph.py` passthrough commands

### Potential deprecations (not removed yet)
- `find` (overlaps with `search`; keep for now because no-rerank quick lookup is useful in debugging)
- direct low-level commands exposed via passthrough that bypass guardrails

## Implemented Improvements

1. Interactive config UX:
- Added `quaid config edit` menu-driven editor.
- Added `quaid config path`.
- Added `quaid config set <dotted.key> <value>`.
- Added shared helper `modules/quaid/config_cli.py`.

2. CLI consistency:
- Wired config subcommands in both:
  - `modules/quaid/quaid`
  - release wrapper path used by packaging scripts in `scripts/`
- Added `doctor` alias in the dev wrapper (`health` parity).

3. Install/bootstrap schedule default:
- Installer default janitor time set to `04:00` (recommended).
- Bootstrap runtime profiles now seed HEARTBEAT with a 04:00 janitor block.

## Remaining UX Improvements (next pass)

1. Add `quaid config doctor`:
- Validate model/provider mapping coherence.
- Validate adapter type + gateway endpoint availability.
- Validate notification/channel settings.

2. Add `quaid config diff`:
- Compare effective config vs template defaults.

3. Narrow `doctor` output levels:
- `doctor --quick` (fast local checks)
- `doctor --full` (gateway/plugin/schedule deep checks)

4. Command grouping in `help`:
- Default help: concise user-facing commands.
- `help --all`: include advanced and low-level passthrough commands.

5. Add structured output mode:
- `--json` for `doctor`, `stats`, and `config show`.

## Notes

- No command removals were made in this pass to avoid breaking existing workflows.
- Focus was on making configuration safe and navigable first, then tightening command scope in follow-up.
