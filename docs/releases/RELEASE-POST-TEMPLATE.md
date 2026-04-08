# Release Post Template

Use this when announcing a Quaid release publicly.

Draft the exact post first, get approval on the wording, then publish it after the
GitHub release is live.

## Short Post

```md
Quaid v<version> is out.

Quaid is a long-term memory system for coding agents. It extracts durable facts,
project context, and docs context across sessions so agents can recall what
matters without relying only on the current window.

This release:
- <highlight 1>
- <highlight 2>
- <highlight 3>

Currently supported / most-tested hosts:
- OpenClaw
- Claude Code
- Codex

Known limitations:
- <limitation 1>
- <limitation 2>

Release notes: <release-notes-link>
Install / repo: <repo-link>
```

## Long Post

```md
Quaid v<version> is out.

Quaid is a long-term memory system for coding agents. It gives agents durable
memory across sessions by extracting facts, maintaining project context, and
making prior context recallable when it matters.

What ships in this release:
- <highlight 1>
- <highlight 2>
- <highlight 3>

Current host support:
- OpenClaw: <support note>
- Claude Code: <support note>
- Codex: <support note>

Known limitations:
- <limitation 1>
- <limitation 2>
- <limitation 3>

If you want the detailed change list, read the release notes:
<release-notes-link>

Repo / install:
<repo-link>
```

## Required Links

- GitHub release: `<github-release-link>`
- Release notes doc: `docs/releases/v<version>.md`
- Repo/install entrypoint: `<repo-link>`

## Writing Rules

- Keep the first paragraph plain and literal: what Quaid is, not marketing copy.
- Mention current supported / most-tested hosts explicitly.
- Include the biggest known limitations instead of hiding them.
- Do not promise support or stability beyond what the release notes actually say.
- Keep the short version under roughly 12 lines when posted to chat/social channels.
