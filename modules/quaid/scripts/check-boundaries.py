#!/usr/bin/env python3
"""Enforce subsystem import boundaries via an explicit allow matrix.

Rules:
- `core` is the composition root and may import all subsystems.
- Non-core subsystems may only import approved dependencies.
- `datastore` must not import `core`.
- `lib` is utility-only; boundary imports from `lib` are disallowed unless
  explicitly allowlisted as a composition exception.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUBSYSTEMS = {"adaptors", "core", "datastore", "ingest", "orchestrator", "lib"}
ALLOWED_IMPORTS = {
    "core": set(SUBSYSTEMS),
    "datastore": {"datastore", "lib"},
    "ingest": {"ingest", "core", "lib"},
    "adaptors": {"adaptors", "core", "lib"},
    "orchestrator": {"orchestrator", "core", "lib"},
    "lib": {"lib"},
}
ALLOWLIST = {
    # Core composition points intentionally bind datastore implementations.
    ("core/lifecycle/datastore_runtime.py", "datastore"),
    ("core/services/memory_service.py", "datastore"),
    ("core/docs/updater.py", "datastore"),
    ("core/plugins/memorydb_contract.py", "datastore"),
    ("core/lifecycle/soul_snippets.py", "datastore"),
    # Adapter selection composition point.
    ("lib/adapter.py", "adaptors"),
    # Archive compatibility shim forwards to datastore-owned implementation.
    ("lib/archive.py", "datastore"),
}

PY_IMPORT_RE = re.compile(r"^\s*from\s+([a-zA-Z_][\w\.]*)\s+import\s+|^\s*import\s+([a-zA-Z_][\w\.]*)")
PY_DYNAMIC_IMPORT_RE = re.compile(
    r"""__import__\(\s*['"]([a-zA-Z_][\w\.]*)['"]|importlib\.import_module\(\s*['"]([a-zA-Z_][\w\.]*)['"]"""
)
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
        for m in PY_DYNAMIC_IMPORT_RE.finditer(text):
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
    if not src:
        return []

    rel = path.relative_to(ROOT).as_posix()
    violations: list[str] = []
    allowed_targets = ALLOWED_IMPORTS.get(src, {src})
    for target in extract_targets(path):
        if target == src:
            continue
        if (rel, target) in ALLOWLIST:
            continue
        if src == "core" and target == "datastore":
            violations.append(
                f"{rel}: core must not import datastore directly unless allowlisted composition point"
            )
            continue
        if target not in allowed_targets:
            if src == "datastore" and target == "core":
                violations.append(f"{rel}: datastore must not import core directly")
            else:
                allow_desc = ", ".join(sorted(allowed_targets))
                violations.append(
                    f"{rel}: {src} must not import {target} directly (allowed: {allow_desc})"
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
