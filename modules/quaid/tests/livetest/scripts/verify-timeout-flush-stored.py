#!/usr/bin/env python3
"""Verify that the latest timeout flush stored at least one fact."""

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


def _timeout_flush_rows(path: Path, tail: int) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-tail:]
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
    parser = argparse.ArgumentParser(
        description="Check that the latest timeout rolling_flush stored a new fact."
    )
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
        help="Number of recent metric rows to scan.",
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

    rows = _timeout_flush_rows(log_path, max(1, int(args.tail or 1)))
    if rows is None:
        return 1
    if not rows:
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
