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
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"
TMP_ROOT = ROOT / ".tmp" / "pytest-runs"

INTEGRATION_MARK = "pytestmark = pytest.mark.integration"
REGRESSION_MARK = "pytestmark = pytest.mark.regression"


@dataclass
class Result:
    target: str
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


def expand_targets(files: list[Path], mode: str) -> list[str]:
    """Return pytest targets, splitting proven-safe heavy files by class."""
    targets: list[str] = []
    for file_path in files:
        rel = file_path.relative_to(ROOT).as_posix()
        if (
            mode == "unit"
            and file_path.name == "test_store_recall.py"
            and os.environ.get("QUAID_PYTEST_SPLIT_STORE_RECALL", "1") != "0"
        ):
            # The recall unit file is intentionally broad and slow when run as
            # one subprocess. Split by stable classes so the prepush timeout
            # remains a real hung-test guard instead of a suite-size limit.
            targets.extend(
                [
                    f"{rel}::test_print_recall_results_emits_empty_message",
                    f"{rel}::test_print_recall_results_handles_docs_rows_without_id",
                    f"{rel}::TestStoreValidation",
                    f"{rel}::TestStoreBasic",
                    f"{rel}::TestRecallBasic",
                    f"{rel}::TestStoreDedup",
                    f"{rel}::TestInjectionBlocklist",
                    f"{rel}::TestStoreKeywords",
                    f"{rel}::TestGatewayRecoveryScan",
                    f"{rel}::TestTimestampOverride",
                    f"{rel}::TestRecallTelemetry",
                    f"{rel}::TestRecallFastHookInjectContract",
                    f"{rel}::TestNormalizeDomainFilter",
                    f"{rel}::TestNormalizeDomainBoost",
                    f"{rel}::TestDomainBoostRecallIntegration",
                    f"{rel}::TestDomainFilterAllTrue",
                    f"{rel}::TestDomainFilterTaggedPlusUnscoped",
                    f"{rel}::TestScoreThreshold",
                    f"{rel}::TestRecallLimitEdgeCases",
                ]
            )
            continue
        if (
            mode == "regression"
            and file_path.name == "test_golden_recall.py"
            and os.environ.get("QUAID_PYTEST_SPLIT_GOLDEN_RECALL", "1") != "0"
        ):
            # Separate subprocesses keep unittest.mock patches, environment, and
            # temp DBs isolated while allowing the slow golden recall groups to
            # run in parallel under the existing regression worker pool.
            targets.extend(
                [
                    f"{rel}::TestGoldenRecall",
                    f"{rel}::TestRecallMetrics",
                    f"{rel}::TestAdversarialRecall",
                ]
            )
            continue
        targets.append(rel)
    return targets


def cleanup_stale_tmp_dirs(base: Path, older_than_hours: int = 24) -> None:
    """Best-effort cleanup of stale per-run temp directories."""
    if not base.exists():
        return
    cutoff = time.time() - (older_than_hours * 3600)
    for child in base.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child, ignore_errors=True)
        except Exception:
            continue


def run_one(
    target: str,
    timeout_s: int,
    marker_expr: str | None = None,
    tmpdir: Path | None = None,
    unit_sandbox_root: Path | None = None,
) -> Result:
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
    cmd.append(target)
    t0 = time.time()
    env = os.environ.copy()
    if tmpdir is not None:
        env["TMPDIR"] = str(tmpdir)
        env["TMP"] = str(tmpdir)
        env["TEMP"] = str(tmpdir)
    if unit_sandbox_root is not None:
        home = unit_sandbox_root / "home"
        quaid_home = unit_sandbox_root / "quaid-home"
        xdg_config = home / ".config"
        xdg_cache = home / ".cache"
        xdg_state = home / ".local" / "state"
        for p in (home, quaid_home, xdg_config, xdg_cache, xdg_state):
            p.mkdir(parents=True, exist_ok=True)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows compatibility
        env["QUAID_HOME"] = str(quaid_home)
        env["XDG_CONFIG_HOME"] = str(xdg_config)
        env["XDG_CACHE_HOME"] = str(xdg_cache)
        env["XDG_STATE_HOME"] = str(xdg_state)
    # Use process-group isolation so timeout cleanup can reap pytest children.
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    pytest_pattern = (
        "pytest -q -o addopts= -o faulthandler_timeout=45 "
        f"{target}"
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
        out = (stdout or "") + ("\n" + stderr if stderr else "")
        status = "PASS" if proc.returncode == 0 else "FAIL"
        rc = proc.returncode if proc.returncode is not None else 1
        return Result(target, rc, time.time() - t0, status, out)
    except subprocess.TimeoutExpired:
        # Terminate whole process group first, then force-kill if needed.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except Exception:
            pass
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # Descendants may keep inherited pipe FDs open; don't block teardown.
            stdout, stderr = "", ""
        # Last-resort cleanup: kill lingering pytest descendants for this file.
        if shutil.which("pkill"):
            subprocess.run(["pkill", "-TERM", "-f", pytest_pattern], check=False)
            time.sleep(0.2)
            subprocess.run(["pkill", "-KILL", "-f", pytest_pattern], check=False)
        out = (stdout or "") + ("\n" + stderr if stderr else "")
        return Result(target, 124, time.time() - t0, "TIMEOUT", out)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel pytest runner with isolation")
    parser.add_argument(
        "--mode",
        choices=["unit", "integration", "regression", "adapter_openclaw", "all"],
        default="unit",
    )
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 4) // 2)))
    parser.add_argument("--timeout", type=int, default=120, help="Per-file timeout in seconds")
    parser.add_argument(
        "--no-temp-cleanup",
        action="store_true",
        help="Disable stale temp cleanup and per-run temp sandboxing",
    )
    parser.add_argument(
        "--no-unit-sandbox",
        action="store_true",
        help="Disable HOME/QUAID_HOME sandboxing for --mode unit",
    )
    args = parser.parse_args()

    files = gather_files(args.mode)
    targets = expand_targets(files, args.mode)
    if not targets:
        print(f"[pytest:{args.mode}] no files found")
        return 0

    run_tmpdir: Path | None = None
    unit_sandbox_root: Path | None = None
    if not args.no_temp_cleanup:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        cleanup_stale_tmp_dirs(TMP_ROOT, older_than_hours=24)
        run_tmpdir = Path(tempfile.mkdtemp(prefix="run-", dir=str(TMP_ROOT)))
        if args.mode == "unit" and not args.no_unit_sandbox:
            unit_sandbox_root = run_tmpdir / "unit-sandbox"
            unit_sandbox_root.mkdir(parents=True, exist_ok=True)

    print(f"[pytest:{args.mode}] files={len(files)} targets={len(targets)} workers={args.workers} timeout={args.timeout}s")
    if unit_sandbox_root is not None:
        print(f"[pytest:{args.mode}] unit sandbox: {unit_sandbox_root}")

    results: list[Result] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            marker_expr = "adapter_openclaw" if args.mode == "adapter_openclaw" else None
            fut_map = {
                ex.submit(run_one, target, args.timeout, marker_expr, run_tmpdir, unit_sandbox_root): target
                for target in targets
            }
            for fut in concurrent.futures.as_completed(fut_map):
                res = fut.result()
                results.append(res)
                print(f"[{res.status:7}] {res.target} ({res.duration_s:.1f}s)")
    finally:
        if run_tmpdir is not None:
            shutil.rmtree(run_tmpdir, ignore_errors=True)

    results.sort(key=lambda r: r.target)
    failed = [r for r in results if r.rc != 0]

    total_time = sum(r.duration_s for r in results)
    wall_time = max((r.duration_s for r in results), default=0.0)
    print(f"\n[pytest:{args.mode}] summary: {len(results)-len(failed)}/{len(results)} files passed")
    print(f"[pytest:{args.mode}] total-file-time={total_time:.1f}s max-file-time={wall_time:.1f}s")

    if failed:
        print("\nFailed files diagnostics:")
        for r in failed:
            tail = "\n".join((r.output or "").strip().splitlines()[-40:])
            print(f"\n--- {r.target} [{r.status}] ---\n{tail}\n")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
