#!/usr/bin/env python3
"""Fail when adapter pending-notification files would contaminate a quiet test."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _pending_notice_files(data_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in data_dir.iterdir()
        if path.is_file() and path.name.endswith("-pending-notifications.jsonl")
    )


def _read_pending_entries(path: Path) -> tuple[list[dict[str, Any]], int]:
    entries: list[dict[str, Any]] = []
    malformed = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return [], 0
    except OSError:
        return [], 1
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            malformed += 1
            continue
        if not isinstance(entry, dict):
            malformed += 1
            continue
        message = str(entry.get("message") or "").strip()
        if message:
            entries.append(entry)
    return entries, malformed


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify no active Quaid notices are pending.")
    parser.add_argument(
        "--instance",
        default=os.environ.get("INSTANCE") or os.environ.get("QUAID_INSTANCE"),
        help="Quaid instance ID; defaults to INSTANCE or QUAID_INSTANCE.",
    )
    parser.add_argument(
        "--quaid-home",
        type=Path,
        default=Path(os.environ.get("QUAID_HOME", "~/.quaid")).expanduser(),
        help="Quaid home; defaults to QUAID_HOME or ~/.quaid.",
    )
    args = parser.parse_args()

    instance = str(args.instance or "").strip()
    if not instance:
        parser.error("--instance or INSTANCE/QUAID_INSTANCE is required")

    data_dir = args.quaid_home / "instances" / instance / "data"
    if not data_dir.is_dir():
        print(f"FAIL: instance data directory not found: {data_dir}", file=sys.stderr)
        return 1
    if not os.access(data_dir, os.R_OK | os.X_OK):
        print(f"FAIL: instance data directory is not readable/searchable: {data_dir}", file=sys.stderr)
        return 1

    pending: list[tuple[Path, list[dict[str, Any]], int]] = []
    try:
        pending_files = _pending_notice_files(data_dir)
    except OSError as exc:
        print(f"FAIL: could not inspect active pending notices under {data_dir}: {exc}", file=sys.stderr)
        return 1

    for path in pending_files:
        entries, malformed = _read_pending_entries(path)
        if entries or malformed:
            pending.append((path, entries, malformed))

    if not pending:
        print("Active pending notices: 0")
        return 0

    total_entries = sum(len(entries) for _, entries, _ in pending)
    total_malformed = sum(malformed for _, _, malformed in pending)
    print(
        f"FAIL: active pending notices remain: entries={total_entries} malformed={total_malformed}",
        file=sys.stderr,
    )
    for path, entries, malformed in pending:
        print(f"{path}: entries={len(entries)} malformed={malformed}", file=sys.stderr)
        for entry in entries[:5]:
            source = str(entry.get("source") or "").strip() or "unknown"
            message = str(entry.get("message") or "").strip()
            print(f"  [{source}] {message[:240]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
