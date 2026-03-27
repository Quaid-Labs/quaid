# GitHub Support History Purge Template

Use this after the rewritten history has already been force-pushed.

Replace the bracketed placeholders before sending.

```text
Subject: Request to purge cached private data after repository history rewrite

Hello GitHub Support,

We force-pushed rewritten history for the repository:

- Repository: [owner/repo]
- Affected branches: [main, canary, ...]
- Rewrite completed at: [UTC timestamp]

The rewrite removed previously committed private data that should no longer be
reachable through cached blobs or search results.

Please purge any cached copies or search indexes that still expose the removed
content for this repository.

Notes:
- The affected content has already been removed from branch history and
  force-pushed.
- We are specifically asking for cache/search cleanup for the removed blobs.

If you need exact rewritten commit ranges or branch tips, I can provide them.

Thank you.
```
