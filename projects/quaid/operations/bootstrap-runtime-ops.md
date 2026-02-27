# Bootstrap and Runtime Ops

This doc defines where bootstrap automation lives, how it relates to `~/quaid/dev`, and where credentials belong.

## Ownership model
- `~/quaid/dev` is the clean source repo intended for GitHub.
- `~/quaid/bootstrap` is the machine-local runtime/bootstrap automation repo.
- Bootstrap scripts are operational tooling; keep local secrets and host credentials out of `~/quaid/dev`.

## Bootstrap flow
- Local runtime install/reinstall is performed from `~/quaid/bootstrap/scripts/bootstrap-local.sh`.
- E2E orchestration is performed from `~/quaid/bootstrap/scripts/run-quaid-e2e.sh`.
- Runtime workspaces (`~/quaid/test`, `~/quaid/e2e-test`) are provisioned from `~/quaid/dev` via worktree/bootstrap automation.

## Credentials and auth
- Host mode (OpenClaw): use gateway auth profiles (OAuth/API key) configured by bootstrap profile application.
- Standalone mode (MCP/CLI outside host runtime): use local env keys (`.env`) where needed.
- Secrets must stay in local-only bootstrap profile files (`profiles/*.local.json`) and never be committed into `~/quaid/dev`.

## Notification safety for automation
- E2E runs should default to quiet notifications to avoid spamming user channels.
- Raise notification verbosity only for explicit notification UX testing.

## Sync discipline
- Changes to runtime/bootstrap scripts should be reflected in project docs under `projects/quaid/operations/`.
- Changes to core/plugin behavior must be implemented in `~/quaid/dev` and then validated through bootstrap/test runtime.

## Git remote policy
- `~/quaid/dev` may have both:
  - `checkpoint` (NAS/local backup remote for normal pushes)
  - `origin`/`github` (GitHub remotes for release publishing)
- Default policy: push to `checkpoint` only.
- Use `scripts/push-backup.sh` for normal push intent.
- Use `scripts/release-push.sh` for GitHub release intent.
- `main` pushes are blocked by default in local hook policy unless explicit release override is set.
- GitHub pushes require explicit release mode:
  - `QUAID_RELEASE=1 ...` (handled by `scripts/release-push.sh`)
- Enforcement is in repo hook path:
  - `git-hooks/pre-push` -> `scripts/push-guard.sh`
