#!/usr/bin/env python3
"""Validate and summarize Quaid E2E summary artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check Quaid E2E summary artifact")
    p.add_argument(
        "--summary",
        default="/tmp/quaid-e2e-last-summary.json",
        help="Path to summary JSON (default: /tmp/quaid-e2e-last-summary.json)",
    )
    p.add_argument(
        "--require-status",
        choices=["success", "failed", "skipped"],
        help="Fail unless summary.status matches this value",
    )
    p.add_argument(
        "--max-duration-seconds",
        type=int,
        default=0,
        help="Fail if duration_seconds exceeds this value (0 disables check)",
    )
    p.add_argument(
        "--print-json",
        action="store_true",
        help="Print normalized summary JSON instead of compact text output",
    )
    return p.parse_args()


def load_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"[e2e-summary] ERROR: summary not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"[e2e-summary] ERROR: failed to parse summary JSON: {exc}")
    if not isinstance(raw, dict):
        raise SystemExit("[e2e-summary] ERROR: summary JSON must be an object")
    return raw


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary)
    s = load_summary(summary_path)

    status = str(s.get("status") or "unknown")
    duration = int(s.get("duration_seconds") or 0)
    suite = str(s.get("suite") or "")
    failure = s.get("failure") or {}
    if not isinstance(failure, dict):
        failure = {}
    stages = s.get("stages") or {}
    if not isinstance(stages, dict):
        stages = {}

    if args.require_status and status != args.require_status:
        raise SystemExit(
            f"[e2e-summary] ERROR: status mismatch (expected={args.require_status}, got={status})"
        )
    if args.max_duration_seconds > 0 and duration > args.max_duration_seconds:
        raise SystemExit(
            "[e2e-summary] ERROR: duration exceeded "
            f"(duration={duration}s max={args.max_duration_seconds}s)"
        )

    if args.print_json:
        print(json.dumps(s, indent=2))
    else:
        failed_stage = str(failure.get("stage") or "")
        failed_reason = str(failure.get("reason") or "")
        stage_counts = {
            "passed": 0,
            "skipped": 0,
            "pending": 0,
            "running": 0,
            "other": 0,
        }
        for value in stages.values():
            v = str(value or "")
            if v in stage_counts:
                stage_counts[v] += 1
            else:
                stage_counts["other"] += 1
        print(
            "[e2e-summary] "
            f"status={status} suite={suite} duration_s={duration} "
            f"stages_passed={stage_counts['passed']} stages_skipped={stage_counts['skipped']} "
            f"failed_stage={failed_stage or '-'} failed_reason={failed_reason or '-'} "
            f"path={summary_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
