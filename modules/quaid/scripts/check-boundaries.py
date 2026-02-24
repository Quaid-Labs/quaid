#!/usr/bin/env python3
"""Enforce subsystem import boundaries.

Rules:
- Non-core subsystems must not import each other directly.
- `datastore` must not import `core` modules directly.
- Cross-subsystem interaction should route through `core` services/contracts.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSYSTEMS = {"adaptors", "core", "datastore", "ingest", "orchestrator"}
ALLOWLIST = {
    # Core composition points intentionally bind datastore implementations.
    ("core/lifecycle/datastore_runtime.py", "datastore"),
    ("core/services/memory_service.py", "datastore"),
}

PY_IMPORT_RE = re.compile(r"^\s*from\s+([a-zA-Z_][\w\.]*)\s+import\s+|^\s*import\s+([a-zA-Z_][\w\.]*)")
TS_IMPORT_RE = re.compile(r"^\s*import(?:.+from\s+)?[\"']([^\"']+)[\"']")


def subsystem_for(path: Path) -> str | None:
    try:
        rel = path.relative_to(ROOT)
    except ValueError:
        return None
    if not rel.parts:
        return None
    head = rel.parts[0]
    return head if head in SUBSYSTEMS else None


def resolve_ts_target(path: Path, spec: str) -> str | None:
    if spec.startswith("."):
        target = (path.parent / spec).resolve()
        return subsystem_for(target)
    head = spec.split("/", 1)[0]
    if head in SUBSYSTEMS:
        return head
    return None


def extract_targets(path: Path) -> list[str]:
    targets: list[str] = []
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".py":
        for line in text.splitlines():
            m = PY_IMPORT_RE.match(line)
            if not m:
                continue
            mod = m.group(1) or m.group(2) or ""
            head = mod.split(".", 1)[0]
            if head in SUBSYSTEMS:
                targets.append(head)
    elif path.suffix in {".ts", ".js", ".mjs"}:
        for line in text.splitlines():
            m = TS_IMPORT_RE.match(line)
            if not m:
                continue
            target = resolve_ts_target(path, m.group(1))
            if target:
                targets.append(target)
    return targets


def check_file(path: Path) -> list[str]:
    src = subsystem_for(path)
    if not src or src == "core":
        return []

    rel = path.relative_to(ROOT).as_posix()
    violations: list[str] = []
    for target in extract_targets(path):
        if target == src:
            continue
        if (rel, target) in ALLOWLIST:
            continue
        if src == "datastore" and target == "core":
            violations.append(f"{rel}: datastore must not import core directly")
            continue
        if target in {"adaptors", "datastore", "ingest", "orchestrator"}:
            violations.append(
                f"{rel}: {src} must not import {target} directly; route through core contracts/services"
            )
    return violations


def main() -> int:
    files = [
        p
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".ts", ".js", ".mjs"}
        and "tests" not in p.parts
        and "__pycache__" not in p.parts
        and "node_modules" not in p.parts
    ]
    violations: list[str] = []
    for path in files:
        violations.extend(check_file(path))
    if violations:
        print("[boundary-check] FAIL")
        for v in sorted(set(violations)):
            print(f" - {v}")
        return 1
    print("[boundary-check] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

