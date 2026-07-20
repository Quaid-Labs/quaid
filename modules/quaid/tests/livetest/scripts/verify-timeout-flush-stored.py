#!/usr/bin/env python3
"""Verify that a timeout flush stored at least one fact."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _timeout_flush_rows(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print(f"FAIL: missing rolling extraction log: {path}", file=sys.stderr)
        return None

    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("event") == "rolling_flush" and row.get("signal_type") == "timeout":
            rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that a timeout rolling_flush stored a new fact.")
    parser.add_argument(
        "--instance",
        default=os.environ.get("INSTANCE") or os.environ.get("QUAID_INSTANCE"),
        help="Quaid instance ID; defaults to INSTANCE or QUAID_INSTANCE.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="Path to rolling-extraction.jsonl. Defaults under ~/.quaid/instances/<instance>.",
    )
    parser.add_argument(
        "--tail",
        type=int,
        default=30,
        help="Number of recent metric rows to scan. Ignored when --count-only or --after-count is used.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--count-only",
        action="store_true",
        help="Print the current total timeout rolling_flush count and exit.",
    )
    mode.add_argument(
        "--after-count",
        type=int,
        default=None,
        help="Only consider timeout rolling_flush rows after this baseline count.",
    )
    args = parser.parse_args()

    if args.log is None:
        instance = str(args.instance or "").strip()
        if not instance:
            print("FAIL: --instance or INSTANCE/QUAID_INSTANCE is required", file=sys.stderr)
            return 1
        log_path = (
            Path.home()
            / ".quaid"
            / "instances"
            / instance
            / "logs"
            / "daemon"
            / "rolling-extraction.jsonl"
        )
    else:
        log_path = args.log.expanduser()

    rows = _timeout_flush_rows(log_path)
    if rows is None:
        return 1
    timeout_flush_count = len(rows)
    if args.count_only:
        print(timeout_flush_count)
        return 0
    if args.after_count is not None:
        baseline = max(0, int(args.after_count))
        if baseline > timeout_flush_count:
            print(
                f"FAIL: after-count baseline {baseline} is newer than timeout flush count {timeout_flush_count}",
                file=sys.stderr,
            )
            return 1
        rows = rows[baseline:]
    else:
        rows = rows[-max(1, int(args.tail or 1)) :]
    if not rows:
        if args.after_count is not None:
            print("FAIL: no timeout rolling_flush found after baseline", file=sys.stderr)
        else:
            print("FAIL: no timeout rolling_flush found", file=sys.stderr)
        return 1

    row = rows[-1]
    stored = _int_value(row.get("final_facts_stored"))
    print(
        json.dumps(
            {
                "signal_type": row.get("signal_type"),
                "processing_signal_type": row.get("processing_signal_type"),
                "final_facts_stored": stored,
                "final_facts_skipped": row.get("final_facts_skipped"),
                "skip_buckets": row.get("skip_buckets"),
                "selected_timeout_flush_count": len(rows),
                "timeout_flush_count": timeout_flush_count,
            },
            sort_keys=True,
        )
    )
    if stored < 1:
        print("FAIL: latest timeout rolling_flush stored no new facts", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
