# Local Development Config

Quaid keeps public defaults in tracked files and private machine/operator data in
one ignored repo-local config file:

- tracked example: `.quaid-dev.example.json`
- ignored real file: `.quaid-dev.local.json`

Copy the example and edit the local file:

```bash
cp .quaid-dev.example.json .quaid-dev.local.json
```

## Path Rules

All paths inside `.quaid-dev.local.json` resolve from `paths.devRoot`.

- `devRoot` should point at the repo root for the current dev checkout
- `developmentDirectory` should point at the parent dev workspace that holds
  sibling checkouts such as `dev`, `test`, `bootstrap`, and `openclaw-source`
- use relative paths such as `../test` and `../openclaw-source`
- do not hardcode `~/quaid/` or `/Users/<name>/...` in tracked docs or scripts

Example:

```json
{
  "paths": {
    "devRoot": ".",
    "developmentDirectory": "..",
    "runtimeWorkspace": "../test",
    "openclawSource": "../openclaw-source"
  },
  "auth": {
    "anthropic": {
      "primaryKeyPath": "../secrets/anthropic-primary.txt",
      "secondaryKeyPath": "../secrets/anthropic-secondary.txt"
    }
  }
}
```

This keeps the dev tree portable if the checkout moves to a different parent
directory such as `~/quaidcode/`.

## Supported Fields

`paths`

- `devRoot`: repo root used for resolving other relative paths
- `developmentDirectory`: parent workspace for dev-only siblings (`dev`, `test`,
  `bootstrap`, `openclaw-source`, `backups`)
- `runtimeWorkspace`: runtime/test workspace path
- `openclawSource`: local OpenClaw checkout path

`identity`

- `bootstrapOwnerName`: owner name used by local bootstrap helpers
- `releaseOwnerName`: expected git/release author name
- `releaseOwnerEmail`: expected git/release author email; use a public-safe
  address here, not a local `.local` mailbox
- `defaultOwnerId`: default owner id written into local runtime config
- `personNodeName`: display name for the default owner
- `speakers`: speaker aliases for the default owner
- `telegramAllowFrom`: allowed Telegram ids for local testing
- `userSummary`: local `USER.md` summary used by runtime-profile application

`liveTest`

- `remoteHost`: live-test target host, such as `localhost` or a private lab host
- `remoteWorkspace`: remote runtime workspace path

`auth`

- `anthropic.primaryKeyPath`: local path to the primary Anthropic key file
- `anthropic.secondaryKeyPath`: local path to the secondary Anthropic key file
- resolve these paths from `paths.devRoot`
- keep the key files outside the repo or otherwise untracked
- store the secret values in those files, not in tracked JSON

`privacy`

- `blockedStrings`: local-only leak markers to scan for in tracked files and
  reachable git history before release or canary push
- use this for legacy Telegram ids, old hostnames, historical absolute paths,
  and old private handles that must not remain reachable on GitHub

## Consumers

These repo tools read `.quaid-dev.local.json` today:

- `modules/quaid/scripts/bootstrap-local.sh`
- `modules/quaid/scripts/run-quaid-e2e.sh`
- `modules/quaid/scripts/apply-runtime-profile.py`
- `modules/quaid/scripts/nightly-full-suite.sh`
- `scripts/privacy-audit.mjs`
- `scripts/release-owner-check.mjs`
- `scripts/push-canary.sh`

Benchmark and local automation tooling may also read the `auth` section for
provider key paths.

## Public Repo Rule

Keep these out of tracked files:

- personal names and ids used only for local testing
- private hostnames
- absolute machine paths
- local Telegram allowlists
- live-test pane layouts and destructive host-specific command sequences

Tracked files should use generic placeholders and relative paths rooted at
`devRoot`.

Before release, keep `privacy.blockedStrings` in your ignored local config up to
date with any legacy private markers that were ever pushed. The privacy audit
uses that local list to block release/canary while leaked history is still
reachable.
