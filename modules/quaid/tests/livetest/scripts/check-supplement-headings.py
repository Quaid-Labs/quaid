#!/usr/bin/env python3
"""Verify lane supplement milestone headings match the canonical guide titles."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[5]
LIVETEST_ROOT = REPO_ROOT / "modules/quaid/tests/livetest"
GUIDE_ROOT = LIVETEST_ROOT / "livetest-guide"
SUPPLEMENTS = (
    "TESTER.CC.md",
    "TESTER.CDX.md",
    "TESTER.OC.md",
)


def _normalize(value: str) -> str:
    lowered = value.lower().replace("`", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", lowered).split())


def _guide_titles() -> dict[str, str]:
    titles: dict[str, str] = {}
    for guide in sorted(GUIDE_ROOT.glob("M*.md")):
        first_line = guide.read_text(encoding="utf-8").splitlines()[0]
        match = re.match(r"^#\s+(M\d+):\s+(.+?)\s*$", first_line)
        if not match:
            continue
        milestone, title = match.groups()
        titles[milestone] = title
    return titles


def main() -> int:
    guide_titles = _guide_titles()
    errors: list[str] = []

    if not guide_titles:
        errors.append("no M<n> guide titles found")

    heading_re = re.compile(r"^(#{2,4})\s+(M\d+)\b(.*)$")
    for supplement in SUPPLEMENTS:
        path = LIVETEST_ROOT / supplement
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = heading_re.match(line)
            if not match:
                continue
            _, milestone, rest = match.groups()
            guide_title = guide_titles.get(milestone)
            if guide_title is None:
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: {milestone} has no "
                    f"matching livetest-guide/{milestone}.md"
                )
                continue
            heading_subject = _normalize(rest)
            canonical_subject = _normalize(guide_title)
            if canonical_subject not in heading_subject:
                errors.append(
                    f"{path.relative_to(REPO_ROOT)}:{line_no}: heading for {milestone} "
                    f"must include guide title '{guide_title}'"
                )

    if errors:
        print("[supplement-heading-check] FAILED", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("[supplement-heading-check] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
