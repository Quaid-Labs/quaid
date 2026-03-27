# Privacy History Scrub

Use this runbook only when private data has already been pushed to a public or
shared remote. Current-tree cleanup is not enough; reachable git history and
remote blob caches must also be addressed.

## Scope

This procedure is for:

- historical private paths
- private hostnames
- local-only handles or ids
- old bot/local commit identities
- tracked private local-profile files or generated artifacts

Do not run history rewrites from an active working tree. Use a fresh mirror
clone so the normal dev checkout stays untouched.

## Inputs

Before starting, make sure your ignored local config is up to date:

- `.quaid-dev.local.json`
- `privacy.blockedStrings`

That file should contain any legacy private markers that must not remain
reachable on GitHub.

## Rewrite Flow

1. Create a fresh mirror clone of the target remote.
2. Install `git-filter-repo` in an isolated virtualenv if it is not already
   available.
3. Prepare:
   - a `--replace-text` file for private string substitutions
   - a `--mailmap` file that rewrites local/bot identities to the required
     public-safe release identity
4. Remove any known local-only tracked files or generated artifacts from
   history with `--path ... --invert-paths`.
5. Run `git filter-repo` in the mirror clone.
6. Verify the rewritten mirror with the same privacy audit used by release:

   ```bash
   QUAID_DEV_LOCAL_CONFIG=/abs/path/to/.quaid-dev.local.json \
     node scripts/privacy-audit.mjs --history-only
   ```

7. If the audit passes, force-push the rewritten branch tips to the affected
   remotes.
8. After the push, contact GitHub Support to purge cached blobs and search
   results for the removed content.

## Replace-Text Guidance

The replace-text file should cover:

- legacy private ids
- private hostnames
- absolute machine paths
- local-only handles
- private email addresses that appeared in file content

Prefer neutral placeholders such as:

- `<telegram-id>`
- `example.local`
- `~/<username>/`
- `user@hostname.local`
- `owner`

Do not put real private values into tracked docs or scripts.

## Mailmap Guidance

Use `--mailmap` to rewrite author and committer identities from local/bot
values to the required public-safe identity:

- `Solomon Steadman <168413654+solstead@users.noreply.github.com>`

Cover every leaked local email variant that appeared in history.

## Remote Branches

Rewrite every published branch that still contains the leaked history. At
minimum that may include:

- `main`
- `canary`
- any still-published maintenance or hardening branches that inherited the same
  commits

Do not assume `main` is clean just because the current tree is clean. Verify the
actual branch tips before deciding what needs a rewrite.

## GitHub Support

Force-pushing rewritten history is not sufficient on its own. GitHub can keep
blob caches and search indexes for removed content until Support purges them.

Use the template in [github-support-history-purge-template.md](./github-support-history-purge-template.md)
after the rewrite has been force-pushed.

## Verification Checklist

After force-push:

- rerun `node scripts/privacy-audit.mjs --history-only` against the rewritten
  repo
- verify leaked strings no longer appear in `git log -S`
- verify author/committer history no longer contains local or bot identities
- confirm release/canary gates now pass the privacy step
- send the GitHub Support purge request
