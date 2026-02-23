# Security Policy

## Reporting a Vulnerability

Please do not open public issues for suspected vulnerabilities.

Report privately to the maintainer with:

- A clear description of the issue
- Reproduction steps or proof-of-concept
- Impact assessment (what can be accessed or changed)
- Suggested mitigation (if available)

Include your Quaid version and host mode (`openclaw`, `mcp`, `cli`) when possible.

## Scope Notes

Quaid is local-first software, but security still matters:

- Local data leakage
- Unsafe command execution paths
- Auth/profile handling bugs
- Destructive operations without sufficient safeguards

## Response

Best effort:

- Acknowledge report quickly
- Confirm impact/reproducibility
- Ship fix and update release notes

## Disclosure Guidance

- Keep vulnerabilities private until a patch or mitigation is available.
- Scrub credentials, tokens, and local paths from shared logs.
