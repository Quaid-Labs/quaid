#!/usr/bin/env python3
"""Parallel, isolated pytest runner for Quaid Python tests.

- Classifies tests into unit/integration/regression tiers
- Runs each file in an isolated pytest subprocess
- Supports parallel workers and per-file timeouts
- Emits concise diagnostics for hung/failed files
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import time
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"

INTEGRATION_MARK = "pytestmark = pytest.mark.integration"
REGRESSION_MARK = "pytestmark = pytest.mark.regression"


@dataclass
class Result:
    file: Path
    rc: int
    duration_s: float
    status: str
    output: str


def classify_test_file(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    if INTEGRATION_MARK in text:
        return "integration"
    if REGRESSION_MARK in text:
        return "regression"
    return "unit"


def has_pytest_marker(path: Path, marker: str) -> bool:
    """True when file includes marker usage text."""
    text = path.read_text(encoding="utf-8", errors="ignore")
    return f"pytest.mark.{marker}" in text


def gather_files(mode: str) -> list[Path]:
    files = sorted(TESTS_DIR.glob("test_*.py"))
    if mode == "adapter_openclaw":
        return [f for f in files if has_pytest_marker(f, "adapter_openclaw")]
    if mode == "all":
        return files
    out: list[Path] = []
    for f in files:
        kind = classify_test_file(f)
        if mode == kind:
            out.append(f)
    return out


def run_one(file_path: Path, timeout_s: int, marker_expr: str | None = None) -> Result:
    rel = file_path.relative_to(ROOT)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-o",
        "addopts=",
        "-o",
        "faulthandler_timeout=45",
    ]
    if marker_expr:
        cmd.extend(["-m", marker_expr])
    cmd.append(str(rel))
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        status = "PASS" if proc.returncode == 0 else "FAIL"
        return Result(file_path, proc.returncode, time.time() - t0, status, out)
    except subprocess.TimeoutExpired as e:
        def _to_text(v):
            if v is None:
                return ""
            if isinstance(v, bytes):
                return v.decode("utf-8", errors="replace")
            return str(v)
        out_stdout = _to_text(e.stdout)
        out_stderr = _to_text(e.stderr)
        out = out_stdout + (("\n" + out_stderr) if out_stderr else "")
        return Result(file_path, 124, time.time() - t0, "TIMEOUT", out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel pytest runner with isolation")
    parser.add_argument(
        "--mode",
        choices=["unit", "integration", "regression", "adapter_openclaw", "all"],
        default="unit",
    )
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--timeout", type=int, default=120, help="Per-file timeout in seconds")
    args = parser.parse_args()

    files = gather_files(args.mode)
    if not files:
        print(f"[pytest:{args.mode}] no files found")
        return 0

    print(f"[pytest:{args.mode}] files={len(files)} workers={args.workers} timeout={args.timeout}s")

    results: list[Result] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        marker_expr = "adapter_openclaw" if args.mode == "adapter_openclaw" else None
        fut_map = {ex.submit(run_one, f, args.timeout, marker_expr): f for f in files}
        for fut in concurrent.futures.as_completed(fut_map):
            res = fut.result()
            results.append(res)
            rel = res.file.relative_to(ROOT)
            print(f"[{res.status:7}] {rel} ({res.duration_s:.1f}s)")

    results.sort(key=lambda r: str(r.file))
    failed = [r for r in results if r.rc != 0]

    total_time = sum(r.duration_s for r in results)
    wall_time = max((r.duration_s for r in results), default=0.0)
    print(f"\n[pytest:{args.mode}] summary: {len(results)-len(failed)}/{len(results)} files passed")
    print(f"[pytest:{args.mode}] total-file-time={total_time:.1f}s max-file-time={wall_time:.1f}s")

    if failed:
        print("\nFailed files diagnostics:")
        for r in failed:
            rel = r.file.relative_to(ROOT)
            tail = "\n".join((r.output or "").strip().splitlines()[-40:])
            print(f"\n--- {rel} [{r.status}] ---\n{tail}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
