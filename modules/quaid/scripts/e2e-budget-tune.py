#!/usr/bin/env python3
"""Recommend E2E runtime budgets from summary history."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import median
from typing import Any, Dict, List


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Tune E2E runtime budgets from history")
    p.add_argument(
        "--history",
        default="/tmp/quaid-e2e-summary-history.jsonl",
        help="Summary history JSONL path (default: /tmp/quaid-e2e-summary-history.jsonl)",
    )
    p.add_argument(
        "--suite",
        default="nightly",
        help="Filter by suite name (default: nightly)",
    )
    p.add_argument(
        "--status",
        default="success",
        choices=["success", "failed", "skipped", "all"],
        help="Filter by status (default: success)",
    )
    p.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum samples required to emit recommendation (default: 5)",
    )
    p.add_argument(
        "--buffer-ratio",
        type=float,
        default=1.2,
        help="Safety multiplier on percentile baseline (default: 1.2)",
    )
    return p.parse_args()


def _pct(sorted_vals: List[int], pct: float) -> int:
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return int(sorted_vals[0])
    idx = max(0, min(len(sorted_vals) - 1, int(math.ceil((pct / 100.0) * len(sorted_vals))) - 1))
    return int(sorted_vals[idx])


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def main() -> int:
    args = parse_args()
    history_path = Path(args.history)
    rows = _load_jsonl(history_path)
    if not rows:
        raise SystemExit(f"[e2e-budget] ERROR: no history rows found at {history_path}")

    filtered: List[Dict[str, Any]] = []
    for r in rows:
        if str(r.get("suite") or "") != args.suite:
            continue
        status = str(r.get("status") or "")
        if args.status != "all" and status != args.status:
            continue
        filtered.append(r)

    durations: List[int] = []
    for r in filtered:
        try:
            d = int(r.get("duration_seconds") or 0)
        except Exception:
            d = 0
        if d > 0:
            durations.append(d)

    if len(durations) < args.min_samples:
        raise SystemExit(
            "[e2e-budget] ERROR: insufficient samples "
            f"(have={len(durations)} need>={args.min_samples}) "
            f"for suite={args.suite} status={args.status}"
        )

    durations.sort()
    p50 = int(median(durations))
    p90 = _pct(durations, 90)
    p95 = _pct(durations, 95)
    p99 = _pct(durations, 99)
    recommended = int(math.ceil(p95 * max(1.0, args.buffer_ratio)))

    out = {
        "history_path": str(history_path),
        "suite": args.suite,
        "status_filter": args.status,
        "samples": len(durations),
        "duration_seconds": {
            "p50": p50,
            "p90": p90,
            "p95": p95,
            "p99": p99,
            "max": int(durations[-1]),
        },
        "buffer_ratio": args.buffer_ratio,
        "recommended_budget_seconds": recommended,
        "recommended_budget_minutes": round(recommended / 60.0, 2),
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
