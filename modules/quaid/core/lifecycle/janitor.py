#!/usr/bin/env python3
"""
Memory Janitor (Optimized) - Scalable LLM-powered maintenance for the memory graph.

Key optimizations:
1. Temporal filtering - only check new memories vs existing (not old-vs-old)
2. Vector similarity pre-filtering - cosine similarity >0.8 before LLM
3. Semantic clustering - group by domain, check within clusters
4. Robust timing and progress reporting
5. Incremental processing with metadata tracking

Tasks:
1. Find near-duplicates (85-94% similarity range) - candidates for merge  
2. Extract edges/relationships from fact text
3. Decay confidence on old unused facts
4. Detect potential contradictions (optimized)

Run modes:
  --dry-run  (default) Report findings without making changes
  --apply    Apply recommended changes

Usage:
  python3 janitor_optimized.py --task duplicates
  python3 janitor_optimized.py --task edges --apply
  python3 janitor_optimized.py --task all --dry-run
"""

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

# Import datastore internals directly for janitor-only maintenance logic.
from datastore.memorydb.memory_graph import (
    get_graph,
    MemoryGraph,
    Node,
    Edge,
    store as store_memory,
    store_contradiction,
    get_pending_contradictions,
    resolve_contradiction,
    mark_contradiction_false_positive,
    soft_delete,
    get_recent_dedup_rejections,
    resolve_dedup_review,
    queue_for_decay_review,
    get_pending_decay_reviews,
    resolve_decay_review,
    ensure_keywords_for_relation,
    get_edge_keywords,
    delete_edges_by_source_fact,
    create_edge,
    content_hash,
    hard_delete_node,
    store_edge_keywords,
)
from lib.config import get_db_path
from lib.tokens import extract_key_tokens as _lib_extract_key_tokens, STOPWORDS as _LIB_STOPWORDS, estimate_tokens
from lib.archive import archive_node as _archive_node
from core.runtime.logger import janitor_logger, rotate_logs
from config import get_config
from core.lifecycle.janitor_lifecycle import build_default_registry, RoutineContext
from core.llm.clients import (call_fast_reasoning, call_deep_reasoning, call_llm,
                         parse_json_response, reset_token_usage, get_token_usage,
                         estimate_cost, set_token_budget, reset_token_budget,
                         is_token_budget_exhausted,
                         DEEP_REASONING_TIMEOUT, FAST_REASONING_TIMEOUT)
from lib.runtime_context import (
    get_workspace_dir,
    get_data_dir,
    get_logs_dir,
    get_repo_slug,
    get_install_url,
    get_llm_provider,
)

# Configuration — resolved from config system
DB_PATH = get_db_path()
def _workspace() -> Path:
    return get_workspace_dir()

def _data_dir() -> Path:
    return get_data_dir()

def _logs_dir() -> Path:
    return get_logs_dir()

WORKSPACE = None  # Lazy — use _workspace() instead
_LIFECYCLE_REGISTRY = build_default_registry()

# Load config values (with fallbacks for safety)
def _get_config_value(getter, default):
    """Safely get config value with fallback."""
    try:
        return getter()
    except Exception:
        return default

# Thresholds - now loaded from config/memory.json
_cfg = get_config()
DUPLICATE_MIN_SIM = _cfg.janitor.dedup.similarity_threshold  # Lower bound for "might be duplicate"
DUPLICATE_MAX_SIM = _cfg.janitor.dedup.high_similarity_threshold  # Upper bound (auto-reject above)
CONTRADICTION_MIN_SIM = _cfg.janitor.contradiction.min_similarity  # Minimum similarity for contradiction checks
CONTRADICTION_MAX_SIM = _cfg.janitor.contradiction.max_similarity  # Maximum similarity for contradiction checks
CONFIDENCE_DECAY_DAYS = _cfg.decay.threshold_days  # Start decaying after this many days unused
CONFIDENCE_DECAY_RATE = _cfg.decay.rate_percent / 100.0  # Convert percent to decimal

# Fixed values (not in config)
RECALL_CANDIDATES_PER_NODE = 30  # Max candidates to recall per new memory



# Datastore-owned maintenance intelligence.
# Janitor remains orchestration, scheduling, logging, and notification.
from datastore.memorydb import maintenance_ops as _maintenance_ops

# Re-export datastore-owned maintenance symbols for backward compatibility.
for _name in dir(_maintenance_ops):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_maintenance_ops, _name)

def run_tests(metrics: JanitorMetrics) -> Dict[str, Any]:
    """Run npm test for quaid plugin and report pass/fail counts."""
    metrics.start_task("tests")
    plugin_dir = str(Path(__file__).parent)

    result = {"tests_passed": 0, "tests_failed": 0, "tests_total": 0, "success": False}

    try:
        proc = subprocess.run(
            ["npm", "test"],
            cwd=plugin_dir,
            capture_output=True,
            text=True,
            timeout=600  # vitest suite needs ~5min with Ollama embedding calls
        )

        output = proc.stdout + proc.stderr

        # Parse pass/fail from test-runner.js summary output
        # e.g. "Total: 20" / "Passed: 20" / "Failed: 0"
        total_match = re.search(r'Total:\s+(\d+)', output)
        passed_match = re.search(r'Passed:\s+(\d+)', output)
        failed_match = re.search(r'Failed:\s+(\d+)', output)

        if total_match and passed_match:
            result["tests_total"] = int(total_match.group(1))
            result["tests_passed"] = int(passed_match.group(1))
            result["tests_failed"] = int(failed_match.group(1)) if failed_match else 0
        else:
            # Fallback: vitest format "Tests  X failed | Y passed (Z)"
            vitest_match = re.search(r'Tests\s+(?:(\d+)\s+failed\s*\|\s*)?(\d+)\s+passed(?:\s*\((\d+)\))?', output)
            if vitest_match:
                result["tests_failed"] = int(vitest_match.group(1) or 0)
                result["tests_passed"] = int(vitest_match.group(2))
                result["tests_total"] = int(vitest_match.group(3) or result["tests_passed"] + result["tests_failed"])
            else:
                # Last fallback: check exit code
                result["tests_total"] = 1
                if proc.returncode == 0:
                    result["tests_passed"] = 1
                else:
                    result["tests_failed"] = 1

        result["success"] = proc.returncode == 0

        if proc.returncode == 0:
            print(f"  All tests passed ({result['tests_passed']}/{result['tests_total']})")
        else:
            print(f"  Tests failed: {result['tests_failed']}/{result['tests_total']}")
            # Print last few lines of output for context
            lines = output.strip().split('\n')
            for line in lines[-10:]:
                print(f"    {line}")

    except subprocess.TimeoutExpired:
        print(f"  Tests timed out after 600s")
        metrics.add_error("Unit tests timed out")
        result["tests_failed"] = 1
        result["tests_total"] = 1
    except FileNotFoundError:
        print(f"  npm not found")
        metrics.add_error("npm not found for unit tests")
    except Exception as e:
        print(f"  Test execution error: {e}")
        metrics.add_error(f"Unit test error: {e}")

    metrics.end_task("tests")
    return result


# =============================================================================
# Main Execution (Enhanced)
# =============================================================================

def _lock_file_path() -> Path:
    return _data_dir() / ".janitor.lock"
_lock_fd = None  # File descriptor for flock-based locking


def _acquire_lock() -> bool:
    """Acquire janitor lock using fcntl.flock (atomic, auto-releases on crash)."""
    global _lock_fd
    import fcntl
    try:
        _lock_file_path().parent.mkdir(parents=True, exist_ok=True)
        _lock_fd = open(_lock_file_path(), 'w')
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(f"{os.getpid()}\n{datetime.now().isoformat()}")
        _lock_fd.flush()
        return True
    except (IOError, OSError):
        if _lock_fd:
            _lock_fd.close()
            _lock_fd = None
        return False


def _release_lock():
    """Release janitor lock."""
    global _lock_fd
    import fcntl
    try:
        if _lock_fd:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_fd = None
        _lock_file_path().unlink(missing_ok=True)
    except Exception:
        pass


def _check_for_updates() -> Optional[Dict[str, str]]:
    """Check GitHub releases API for a newer Quaid version.

    Returns dict with 'latest', 'current', 'url' if update available, None otherwise.
    Caches result for 24 hours in janitor metadata table.
    """
    import urllib.request
    import urllib.error

    REPO = get_repo_slug()
    RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

    # Read current version
    version_file = Path(__file__).parent / "VERSION"
    if not version_file.exists():
        return None
    current = version_file.read_text().strip()
    if not current:
        return None

    # Check cache — skip if checked within 24h
    try:
        graph = get_graph()
        with graph._get_conn() as conn:
            row = conn.execute(
                "SELECT value, updated_at FROM metadata WHERE key = 'update_check'"
            ).fetchone()
            if row:
                last_check = datetime.fromisoformat(row["updated_at"])
                if datetime.now() - last_check < timedelta(hours=24):
                    cached = json.loads(row["value"])
                    if cached.get("latest") and cached["latest"] != current:
                        return cached
                    return None
    except Exception:
        pass

    # Fetch latest release from GitHub
    try:
        req = urllib.request.Request(RELEASES_URL, headers={
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "quaid-update-checker",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        latest_tag = data.get("tag_name", "").lstrip("v")
        html_url = data.get("html_url", f"https://github.com/{REPO}/releases")
    except (urllib.error.URLError, Exception) as e:
        print(f"  Update check failed (network): {e}")
        return None

    # Cache the result
    result = {"latest": latest_tag, "current": current, "url": html_url}
    try:
        graph = get_graph()
        with graph._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, datetime('now'))",
                ("update_check", json.dumps(result)),
            )
    except Exception:
        pass

    if latest_tag and latest_tag != current:
        # Only notify if latest is actually newer (semver comparison)
        try:
            from packaging.version import Version
            if Version(latest_tag) <= Version(current):
                return None
        except Exception:
            # Fallback: simple string comparison — still skip if equal
            pass
        return result
    return None


def run_task_optimized(task: str, dry_run: bool = True, incremental: bool = True,
                       time_budget: int = 0, force_distill: bool = False,
                       user_approved: bool = False, token_budget: int = 0):
    """Run optimized janitor task with comprehensive reporting.

    Args:
        time_budget: Wall-clock budget in seconds. 0 = unlimited.
            When set, tasks are skipped if remaining time is insufficient.
        force_distill: Force journal distillation regardless of interval.
        token_budget: Max total tokens for LLM calls. 0 = unlimited.
            When set, LLM calls return None after budget is exhausted.
    """
    if token_budget > 0:
        set_token_budget(token_budget)
    else:
        reset_token_budget()
    # Prevent concurrent janitor runs
    if not _acquire_lock():
        print("ERROR: Another janitor instance is already running. Exiting.")
        print(f"  Lock file: {_lock_file_path()}")
        print(f"  To force: delete the lock file and retry.")
        return {"error": "janitor_already_running", "success": False, "applied_changes": {}, "metrics": {}}

    try:
        return _run_task_optimized_inner(task, dry_run, incremental, time_budget, force_distill, user_approved)
    finally:
        # Avoid leaking per-run budget limits across long-lived Python processes.
        reset_token_budget()
        _release_lock()


def _resolve_apply_mode(args_apply: bool, args_approve: bool) -> tuple[bool, Optional[str]]:
    """Resolve final dry-run state from CLI flags + janitor.apply_mode config.

    Returns (dry_run, warning_message). warning_message is user-facing guidance
    when an apply request is downgraded to dry-run by policy.
    """
    if not args_apply:
        return True, None

    mode = str(getattr(_cfg.janitor, "apply_mode", "auto") or "auto").strip().lower()
    if mode == "auto":
        return False, None
    if mode == "dry_run":
        return True, (
            "apply blocked by janitor.applyMode=dry_run; running dry-run only."
        )
    if mode == "ask":
        if args_approve:
            return False, None
        return True, (
            "approval required by janitor.applyMode=ask. "
            "Re-run with --approve to apply changes."
        )
    return True, f"unknown janitor.applyMode={mode}; running dry-run for safety."


def _decision_log_path() -> Path:
    p = _logs_dir() / "janitor" / "decision-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append_decision_log(kind: str, payload: Dict[str, Any]) -> None:
    """Append janitor decision events for audit/debug."""
    try:
        row = {
            "ts": datetime.now().isoformat(),
            "kind": kind,
            **payload,
        }
        with _decision_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _pending_approvals_json_path() -> Path:
    p = _logs_dir() / "janitor" / "pending-approval-requests.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _pending_approvals_md_path() -> Path:
    p = _logs_dir() / "janitor" / "pending-approval-requests.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _queue_delayed_notification(
    message: str,
    kind: str = "janitor",
    priority: str = "normal",
) -> None:
    """Queue notification via event bus delayed channel."""
    if not message:
        return
    try:
        from core.runtime.events import queue_delayed_notification as _queue_event_delayed_notification
        result = _queue_event_delayed_notification(
            message,
            kind=kind,
            priority=priority,
            source="janitor",
        )
        item_id = str(((result.get("event") or {}).get("id")) or "")
        _append_decision_log(
            "delayed_notification_queued",
            {
                "id": item_id,
                "kind": kind,
                "priority": priority,
            },
        )
    except Exception as e:
        janitor_logger.warn("delayed_notification_queue_failed", kind=kind, priority=priority, error=str(e))


def _queue_approval_request(scope: str, task_name: str, summary: str) -> None:
    """Queue a pending approval request for heartbeat + user follow-up."""
    entry = {
        "id": hashlib.sha256(f"{scope}|{task_name}|{summary}".encode()).hexdigest()[:12],
        "created_at": datetime.now().isoformat(),
        "scope": scope,
        "task": task_name,
        "summary": summary,
        "status": "pending",
    }
    path = _pending_approvals_json_path()
    data: Dict[str, Any] = {"version": 1, "requests": []}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {"version": 1, "requests": []}
    except Exception:
        data = {"version": 1, "requests": []}

    reqs = data.get("requests", [])
    if not isinstance(reqs, list):
        reqs = []

    # De-dup same request id.
    is_new = not any(isinstance(r, dict) and r.get("id") == entry["id"] for r in reqs)
    if is_new:
        reqs.append(entry)
    data["requests"] = reqs
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    md = _pending_approvals_md_path()
    lines = [
        "# Janitor Pending Approval Requests",
        "",
        "These changes were held because policy is set to `ask`.",
        "Run with `--approve` once reviewed, or switch scope to `auto` in `quaid config edit`.",
        "",
    ]
    for r in reqs:
        if not isinstance(r, dict):
            continue
        lines.append(f"- [{r.get('status', 'pending')}] `{r.get('scope', 'unknown')}` "
                     f"({r.get('created_at', '?')}): {r.get('summary', '')}")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _append_decision_log("approval_queued", entry)
    if is_new:
        _queue_delayed_notification(
            f"[Quaid] ⚠️ Janitor held `{scope}` change: {summary}\n"
            f"Review logs/janitor/pending-approval-requests.md.\n"
            f"Run `quaid janitor --apply --task {task_name} --approve` to apply,\n"
            f"or switch this scope to auto via `quaid config edit`.",
            kind="approval_request",
            priority="high",
        )


def _run_task_optimized_inner(task: str, dry_run: bool = True, incremental: bool = True,
                              time_budget: int = 0, force_distill: bool = False,
                              user_approved: bool = False):
    """Inner implementation of run_task_optimized (with lock held)."""
    # Rotate logs at start of janitor run
    rotate_logs()
    reset_token_usage()

    janitor_logger.info("janitor_start", task=task, dry_run=dry_run, incremental=incremental)

    graph = get_graph()
    metrics = JanitorMetrics()

    # Initialize metadata tracking
    init_janitor_metadata(graph)

    # Determine if this is an incremental run
    last_run = None
    if incremental and task != "decay":  # Decay always needs full scan
        last_run = get_last_run_time(graph, task)

    print(f"\n{'='*80}")
    print(f"Memory Janitor - Task: {task}")
    print(f"Mode: {'DRY RUN' if dry_run else 'APPLY'}")
    print(f"Strategy: {'INCREMENTAL' if last_run else 'FULL SCAN'}")
    if time_budget > 0:
        print(f"Time budget: {time_budget}s")
    if last_run:
        print(f"Last run: {last_run.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}\n")

    def _scope_policy(scope: str) -> str:
        try:
            policy = str(_cfg.janitor.approval_policies.get(scope, "auto")).strip().lower()
            return policy if policy in ("auto", "ask", "dry_run") else "auto"
        except Exception:
            return "auto"

    def _can_apply_scope(scope: str, summary: str) -> bool:
        if dry_run:
            return False
        mode = _scope_policy(scope)
        if mode == "auto":
            return True
        if mode == "ask":
            if user_approved:
                return True
            _queue_approval_request(scope, task, summary)
            print(f"[policy] {scope}=ask, holding apply: {summary}")
            print("[policy] Re-run with --approve, or set this scope to auto in `quaid config edit`.")
            return False
        # dry_run policy
        _queue_approval_request(scope, task, summary + " (policy dry_run)")
        print(f"[policy] {scope}=dry_run, holding apply: {summary}")
        print("[policy] Switch scope to auto/ask in `quaid config edit` when ready.")
        return False

    # ⚠️ LLM PROVIDER CHECK — janitor needs a working LLM provider for most tasks.
    # The adapter layer handles provider selection and authentication.
    if task not in ("embeddings", "cleanup", "rag"):
        try:
            _llm = get_llm_provider()
            _profiles = _llm.get_profiles()
            if not _profiles.get("deep", {}).get("available"):
                raise RuntimeError("High-reasoning model not available")
        except Exception as _provider_err:
            print("⚠️" * 10)
            print(f"⚠️  WARNING: LLM provider not available: {_provider_err}")
            print("⚠️  The janitor needs a working LLM provider for review, dedup, and decay tasks.")
            print("⚠️  Check your adapter configuration (config/memory.json adapter.type, gateway status, etc.).")
            print("⚠️" * 10)
            print()

    # Time budget helper — returns remaining seconds, or skips the task
    # Safety margin: stop 30s before budget to allow report generation
    _BUDGET_SAFETY_MARGIN = 30

    def _time_remaining() -> float:
        """Seconds remaining in the time budget. float('inf') if unlimited."""
        if time_budget <= 0:
            return float('inf')
        return time_budget - metrics.total_duration() - _BUDGET_SAFETY_MARGIN

    def _skip_if_over_budget(task_label: str, min_seconds: int = 10) -> bool:
        """Check if we should skip a task due to time budget.

        Returns True (skip) if remaining time < min_seconds.
        """
        remaining = _time_remaining()
        if remaining < min_seconds:
            print(f"[{task_label}] SKIPPED — time budget exhausted "
                  f"({metrics.total_duration():.0f}s elapsed, "
                  f"{time_budget}s budget)\n")
            _budget_skipped.append(task_label)
            return True
        return False

    # System gate mapping — which system must be enabled for each task
    _TASK_SYSTEM_GATE = {
        # Memory system
        "embeddings": "memory", "review": "memory", "temporal": "memory",
        "dedup_review": "memory", "duplicates": "memory", "contradictions": "memory",
        "decay": "memory", "decay_review": "memory",
        # Journal system
        "snippets": "journal", "soul_snippets": "journal", "journal": "journal",
        # Projects system
        "docs_staleness": "projects", "docs_cleanup": "projects", "rag": "projects",
        # Workspace system
        "workspace": "workspace",
        # Infrastructure tasks (always run): tests, cleanup
    }

    def _system_enabled_or_skip(task_name: str, task_label: str) -> bool:
        """Check if the system gate for a task is enabled. Prints skip message if disabled."""
        system = _TASK_SYSTEM_GATE.get(task_name)
        if not system:
            return True  # Infrastructure tasks always run
        enabled = getattr(_cfg.systems, system, True)
        if not enabled:
            print(f"[{task_label}] SKIPPED — {system} system disabled\n")
        return enabled

    # Memory pipeline health tracking — initialized here so it's always
    # available for the report section even if an exception occurs early.
    memory_pipeline_ok = True

    # Task execution with timing
    applied_changes = {
        "backup_keychain": False,
        "backup_core": False,
        "duplicates_merged": 0,
        "edges_created": 0,
        "memories_decayed": 0,
        "memories_deleted_by_decay": 0,
        "contradictions_found": 0,
        "contradictions_resolved": 0,
        "contradictions_false_positive": 0,
        "contradictions_merged": 0,
        "memories_reviewed": 0,
        "memories_deleted": 0,
        "memories_fixed": 0,
        "dedup_reviewed": 0,
        "dedup_confirmed": 0,
        "dedup_reversed": 0,
        "decay_queued": 0,
        "decay_reviewed": 0,
        "decay_review_deleted": 0,
        "decay_review_extended": 0,
        "decay_review_pinned": 0,
        "workspace_phase": "skipped",
        "workspace_moved_to_docs": 0,
        "workspace_moved_to_memory": 0,
        "rag_files_indexed": 0,
        "rag_chunks_created": 0,
        "rag_files_skipped": 0,
        "temporal_found": 0,
        "temporal_fixed": 0,
        "graduated_to_active": 0,
        "tests_passed": 0,
        "tests_failed": 0,
        "tests_total": 0
    }

    # Track which tasks were skipped due to budget
    _budget_skipped = []

    try:
        # --- Task 0b: Embedding Backfill ---
        # (Moved before memory pipeline — embeddings needed for recall pass)
        if task in ("embeddings", "all") and _system_enabled_or_skip("embeddings", "Task 0b: Embedding Backfill") and not _skip_if_over_budget("Task 0b: Embeddings", 15):
            print("[Task 0b: Embedding Backfill]")
            backfill_result = backfill_embeddings(graph, metrics, dry_run=dry_run)
            print(f"  Found: {backfill_result['found']}, Embedded: {backfill_result['embedded']}")
            print(f"Task completed in {metrics.task_duration('embedding_backfill'):.2f}s\n")

        # --- Task 2: Review Pending Memories (Opus API) ---
        # Memory pipeline starts here. If any task (2-6) fails,
        # remaining memory tasks are skipped and graduation is blocked.
        # Infrastructure tasks (0, 0b, 1, 7, 8) still run regardless.
        def _run_memory_graph_stage(stage: str, stage_dry_run: bool) -> Any:
            return _LIFECYCLE_REGISTRY.run(
                "memory_graph_maintenance",
                RoutineContext(
                    cfg=_cfg,
                    dry_run=stage_dry_run,
                    workspace=_workspace(),
                    graph=graph,
                    options={"subtask": stage},
                ),
            )

        if task in ("review", "all") and _system_enabled_or_skip("review", "Task 2: Review Memories") and not _skip_if_over_budget("Task 2: Review Memories", 30):
            print("[Task 2: Review Pending Memories - Opus API]")
            metrics.start_task("review")
            lifecycle_result = _run_memory_graph_stage("review", dry_run)
            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
                memory_pipeline_ok = False
            applied_changes["memories_reviewed"] = lifecycle_result.metrics.get("memories_reviewed", 0)
            applied_changes["memories_deleted"] = lifecycle_result.metrics.get("memories_deleted", 0)
            applied_changes["memories_fixed"] = lifecycle_result.metrics.get("memories_fixed", 0)
            print(f"  Reviewed: {applied_changes['memories_reviewed']}")
            print(f"  {'Would delete' if dry_run else 'Deleted'}: {applied_changes['memories_deleted']}")
            print(f"  {'Would fix' if dry_run else 'Fixed'}: {applied_changes['memories_fixed']}")
            metrics.end_task("review")
            print(f"Task completed in {metrics.task_duration('review'):.2f}s\n")

        # --- Task 2a: Temporal Resolution (no LLM) ---
        if task in ("temporal", "all") and _system_enabled_or_skip("temporal", "Task 2a: Temporal") and not _skip_if_over_budget("Task 2a: Temporal", 10):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 2a: Resolve Temporal References] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 2a: Resolve Temporal References]")
                metrics.start_task("temporal_resolution")
                lifecycle_result = _run_memory_graph_stage("temporal", dry_run)
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                    memory_pipeline_ok = False
                applied_changes["temporal_found"] = lifecycle_result.metrics.get("temporal_found", 0)
                applied_changes["temporal_fixed"] = lifecycle_result.metrics.get("temporal_fixed", 0)
                print(f"  Found: {applied_changes['temporal_found']}, Fixed: {applied_changes['temporal_fixed']}")
                metrics.end_task("temporal_resolution")
                print(f"Task completed in {metrics.task_duration('temporal_resolution'):.2f}s\n")

        # --- Task 2b: Review Dedup Rejections (Opus) ---
        if task in ("dedup_review", "all") and _system_enabled_or_skip("dedup_review", "Task 2b: Dedup Review") and not _skip_if_over_budget("Task 2b: Dedup Review", 20):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 2b: Review Dedup Rejections] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 2b: Review Dedup Rejections - Opus API]")
                metrics.start_task("dedup_review")
                lifecycle_result = _run_memory_graph_stage("dedup_review", dry_run)
                for line in lifecycle_result.logs:
                    print(f"  {line}")
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                    memory_pipeline_ok = False
                applied_changes["dedup_reviewed"] = lifecycle_result.metrics.get("dedup_reviewed", 0)
                applied_changes["dedup_confirmed"] = lifecycle_result.metrics.get("dedup_confirmed", 0)
                applied_changes["dedup_reversed"] = lifecycle_result.metrics.get("dedup_reversed", 0)
                print(f"  Reviewed: {applied_changes['dedup_reviewed']}")
                print(f"  Confirmed: {applied_changes['dedup_confirmed']}")
                print(f"  Reversed (restored): {applied_changes['dedup_reversed']}")
                metrics.end_task("dedup_review")
                print(f"Task completed in {metrics.task_duration('dedup_review'):.2f}s\n")

        if task in ("duplicates", "all") and _system_enabled_or_skip("duplicates", "Task 3: Find Near-Duplicates"):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 3: Find Near-Duplicates] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 3: Find Near-Duplicates]")
                dedup_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "dedup merge operations"
                )
                metrics.start_task("duplicates")
                lifecycle_result = _run_memory_graph_stage("duplicates", dry_run or (not dedup_apply_allowed))
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                    memory_pipeline_ok = False
                applied_changes["duplicates_merged"] = lifecycle_result.metrics.get("duplicates_merged", 0)
                print(f"  Merged: {applied_changes['duplicates_merged']}")
                metrics.end_task("duplicates")
                print(f"Task completed in {metrics.task_duration('duplicates'):.2f}s\n")

        if task in ("contradictions", "all") and _system_enabled_or_skip("contradictions", "Task 4: Contradictions"):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 4: Verify Contradictions] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 4: Verify Contradictions]")
                metrics.start_task("contradictions")
                lifecycle_result = _run_memory_graph_stage("contradictions", dry_run)
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                    memory_pipeline_ok = False
                applied_changes["contradictions_found"] = lifecycle_result.metrics.get("contradictions_found", 0)
                if lifecycle_result.data.get("contradiction_findings"):
                    applied_changes["contradiction_findings"] = lifecycle_result.data.get("contradiction_findings")
                print(f"  Found: {applied_changes['contradictions_found']}")
                metrics.end_task("contradictions")
                print(f"Task completed in {metrics.task_duration('contradictions'):.2f}s\n")

                # --- Task 4b: Resolve Contradictions (Opus) ---
                if not memory_pipeline_ok:
                    print("[Task 4b: Resolve Contradictions] SKIPPED — pipeline aborted\n")
                else:
                    print("[Task 4b: Resolve Contradictions]")
                    contradiction_apply_allowed = _can_apply_scope(
                        "destructive_memory_ops",
                        "contradiction resolution operations"
                    )
                    metrics.start_task("contradiction_resolution")
                    lifecycle_result = _run_memory_graph_stage(
                        "contradictions_resolve",
                        dry_run or (not contradiction_apply_allowed),
                    )
                    for err in lifecycle_result.errors:
                        print(f"  {err}")
                        metrics.add_error(err)
                        memory_pipeline_ok = False
                    applied_changes["contradictions_resolved"] = lifecycle_result.metrics.get("contradictions_resolved", 0)
                    applied_changes["contradictions_false_positive"] = lifecycle_result.metrics.get("contradictions_false_positive", 0)
                    applied_changes["contradictions_merged"] = lifecycle_result.metrics.get("contradictions_merged", 0)
                    if lifecycle_result.data.get("contradiction_decisions"):
                        applied_changes["contradiction_decisions"] = lifecycle_result.data.get("contradiction_decisions")
                    print(f"  Resolved: {applied_changes['contradictions_resolved']}, "
                          f"False positives: {applied_changes['contradictions_false_positive']}, "
                          f"Merged: {applied_changes['contradictions_merged']}")
                    metrics.end_task("contradiction_resolution")
                    print(f"Task completed in {metrics.task_duration('contradiction_resolution'):.2f}s\n")

        # --- Task 5: Confidence Decay (no LLM) ---
        if task in ("decay", "all") and _system_enabled_or_skip("decay", "Task 5: Decay") and not _skip_if_over_budget("Task 5: Decay", 10):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 5: Confidence Decay] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 5: Confidence Decay]")
                decay_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "confidence decay updates/deletes"
                )
                decay_dry_run = dry_run or (not decay_apply_allowed)
                metrics.start_task("decay")
                lifecycle_result = _run_memory_graph_stage("decay", decay_dry_run)
                for line in lifecycle_result.logs:
                    print(f"  {line}")
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                applied_changes["memories_decayed"] = lifecycle_result.metrics.get("memories_decayed", 0)
                applied_changes["memories_deleted_by_decay"] = lifecycle_result.metrics.get("memories_deleted_by_decay", 0)
                applied_changes["decay_queued"] = lifecycle_result.metrics.get("decay_queued", 0)
                total_updated = (
                    applied_changes["memories_decayed"]
                    + applied_changes["memories_deleted_by_decay"]
                    + applied_changes["decay_queued"]
                )
                print(f"  {'Would update' if decay_dry_run else 'Updated'} {total_updated} memories")
                print(f"  Decayed: {applied_changes['memories_decayed']}")
                print(f"  Deleted: {applied_changes['memories_deleted_by_decay']}")
                print(f"  Queued for review: {applied_changes['decay_queued']}")
                metrics.end_task("decay")
                print(f"Task completed in {metrics.task_duration('decay'):.2f}s\n")

        # --- Task 5b: Review Decayed Memories (Opus) ---
        if task in ("decay_review", "all") and _system_enabled_or_skip("decay_review", "Task 5b: Decay Review") and not _skip_if_over_budget("Task 5b: Decay Review", 20):
            if task == "all" and not memory_pipeline_ok:
                print("[Task 5b: Review Decayed Memories] SKIPPED — pipeline aborted\n")
            else:
                print("[Task 5b: Review Decayed Memories - Opus API]")
                decay_review_apply_allowed = _can_apply_scope(
                    "destructive_memory_ops",
                    "decay review decisions"
                )
                decay_review_dry_run = dry_run or (not decay_review_apply_allowed)
                metrics.start_task("decay_review")
                lifecycle_result = _run_memory_graph_stage("decay_review", decay_review_dry_run)
                for line in lifecycle_result.logs:
                    print(f"  {line}")
                for err in lifecycle_result.errors:
                    print(f"  {err}")
                    metrics.add_error(err)
                    memory_pipeline_ok = False

                applied_changes["decay_reviewed"] = lifecycle_result.metrics.get("decay_reviewed", 0)
                applied_changes["decay_review_deleted"] = lifecycle_result.metrics.get("decay_review_deleted", 0)
                applied_changes["decay_review_extended"] = lifecycle_result.metrics.get("decay_review_extended", 0)
                applied_changes["decay_review_pinned"] = lifecycle_result.metrics.get("decay_review_pinned", 0)

                print(f"  Reviewed: {applied_changes['decay_reviewed']}")
                print(f"  Deleted: {applied_changes['decay_review_deleted']}")
                print(f"  Extended: {applied_changes['decay_review_extended']}")
                print(f"  Pinned: {applied_changes['decay_review_pinned']}")
                metrics.end_task("decay_review")
                print(f"Task completed in {metrics.task_duration('decay_review'):.2f}s\n")

        def _allow_doc_apply(doc_path: str, action: str) -> bool:
            doc_p = Path(doc_path)
            is_root_md = len(doc_p.parts) == 1 and doc_p.suffix.lower() == ".md"
            is_quaid_project_md = (
                len(doc_p.parts) >= 2
                and doc_p.parts[0] == "projects"
                and doc_p.parts[1] == "quaid"
                and doc_p.suffix.lower() == ".md"
            )
            if is_root_md:
                return _can_apply_scope("core_markdown_writes", f"docs {action}: {doc_path}")
            if is_quaid_project_md:
                return True
            return _can_apply_scope("project_docs_writes", f"project docs {action}: {doc_path}")

        parallel_lifecycle_results = {}
        if task == "all" and dry_run:
            try:
                parallel_lifecycle_results = _LIFECYCLE_REGISTRY.run_many(
                    [
                        ("workspace", RoutineContext(cfg=_cfg, dry_run=True, workspace=_workspace())),
                        ("docs_staleness", RoutineContext(
                            cfg=_cfg,
                            dry_run=True,
                            workspace=_workspace(),
                            allow_doc_apply=_allow_doc_apply,
                        )),
                        ("docs_cleanup", RoutineContext(
                            cfg=_cfg,
                            dry_run=True,
                            workspace=_workspace(),
                            allow_doc_apply=_allow_doc_apply,
                        )),
                        ("snippets", RoutineContext(cfg=_cfg, dry_run=True, workspace=_workspace())),
                        ("journal", RoutineContext(
                            cfg=_cfg,
                            dry_run=True,
                            workspace=_workspace(),
                            force_distill=force_distill,
                        )),
                    ],
                    max_workers=3,
                )
                print("[lifecycle] Parallel dry-run prepass completed for workspace/docs/snippets/journal")
            except Exception as e:
                print(f"[lifecycle] Parallel prepass unavailable, falling back to sequential: {e}")

        # --- Task 1: Workspace Audit (Opus API) ---
        # (Runs after memory pipeline — memory tasks are higher priority under time budget)
        if task in ("workspace", "all") and _system_enabled_or_skip("workspace", "Task 1: Workspace Audit") and not _skip_if_over_budget("Task 1: Workspace Audit", 30):
            print("[Task 1: Workspace Audit - Single-Pass Opus Review]")
            metrics.start_task("workspace_audit")
            workspace_apply_allowed = _can_apply_scope(
                "workspace_file_moves_deletes",
                "workspace file moves/deletes"
            )
            workspace_dry_run = dry_run or (not workspace_apply_allowed)

            lifecycle_result = parallel_lifecycle_results.get("workspace") or _LIFECYCLE_REGISTRY.run(
                "workspace",
                RoutineContext(cfg=_cfg, dry_run=workspace_dry_run, workspace=_workspace()),
            )
            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)

            applied_changes["workspace_phase"] = lifecycle_result.data.get("workspace_phase", "unknown")
            if lifecycle_result.data.get("bloated_files"):
                applied_changes["bloated_files"] = lifecycle_result.data["bloated_files"]

            applied_changes["workspace_moved_to_docs"] = lifecycle_result.metrics.get("workspace_moved_to_docs", 0)
            applied_changes["workspace_moved_to_memory"] = lifecycle_result.metrics.get("workspace_moved_to_memory", 0)
            applied_changes["workspace_trimmed"] = lifecycle_result.metrics.get("workspace_trimmed", 0)
            applied_changes["workspace_bloat_warnings"] = lifecycle_result.metrics.get("workspace_bloat_warnings", 0)
            applied_changes["workspace_project_detected"] = lifecycle_result.metrics.get("workspace_project_detected", 0)

            metrics.end_task("workspace_audit")
            print(f"Task completed in {metrics.task_duration('workspace_audit'):.2f}s\n")

        # --- Task 1b: Documentation Staleness Check ---
        # (Runs after memory pipeline — expensive Opus doc updates are lower priority)
        if task in ("docs_staleness", "all") and _system_enabled_or_skip("docs_staleness", "Task 1b: Doc Staleness") and not _skip_if_over_budget("Task 1b: Doc Staleness", 60):
            print("[Task 1b: Documentation Staleness Check]")
            metrics.start_task("docs_staleness")
            lifecycle_result = parallel_lifecycle_results.get("docs_staleness") or _LIFECYCLE_REGISTRY.run(
                "docs_staleness",
                RoutineContext(
                    cfg=_cfg,
                    dry_run=dry_run,
                    workspace=_workspace(),
                    allow_doc_apply=_allow_doc_apply,
                ),
            )
            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
            applied_changes["docs_updated"] = lifecycle_result.metrics.get("docs_updated", 0)
            metrics.end_task("docs_staleness")
            print(f"Task completed in {metrics.task_duration('docs_staleness'):.2f}s\n")

        # --- Task 1c: Documentation Cleanup (churn-based) ---
        if task in ("docs_cleanup", "all") and _system_enabled_or_skip("docs_cleanup", "Task 1c: Doc Cleanup") and not _skip_if_over_budget("Task 1c: Doc Cleanup", 60):
            print("[Task 1c: Documentation Cleanup]")
            metrics.start_task("docs_cleanup")
            lifecycle_result = parallel_lifecycle_results.get("docs_cleanup") or _LIFECYCLE_REGISTRY.run(
                "docs_cleanup",
                RoutineContext(
                    cfg=_cfg,
                    dry_run=dry_run,
                    workspace=_workspace(),
                    allow_doc_apply=_allow_doc_apply,
                ),
            )
            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
            applied_changes["docs_cleaned"] = lifecycle_result.metrics.get("docs_cleaned", 0)
            metrics.end_task("docs_cleanup")
            print(f"Task completed in {metrics.task_duration('docs_cleanup'):.2f}s\n")

        # --- Task 1d-snippets: Soul Snippets Review (Opus API, nightly) ---
        if task in ("snippets", "soul_snippets", "all") and _system_enabled_or_skip("snippets", "Task 1d-snippets: Snippets") and not _skip_if_over_budget("Task 1d-snippets: Snippets", 30):
            print("[Task 1d-snippets: Soul Snippets Review]")
            metrics.start_task("snippets")
            snippets_apply_allowed = _can_apply_scope(
                "core_markdown_writes",
                "snippets fold into root core markdown"
            )
            snippets_dry_run = dry_run or (not snippets_apply_allowed)
            lifecycle_result = parallel_lifecycle_results.get("snippets") or _LIFECYCLE_REGISTRY.run(
                "snippets",
                RoutineContext(cfg=_cfg, dry_run=snippets_dry_run, workspace=_workspace()),
            )
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
            applied_changes["snippets_folded"] = lifecycle_result.metrics.get("snippets_folded", 0)
            applied_changes["snippets_rewritten"] = lifecycle_result.metrics.get("snippets_rewritten", 0)
            applied_changes["snippets_discarded"] = lifecycle_result.metrics.get("snippets_discarded", 0)
            metrics.end_task("snippets")
            print(f"Task completed in {metrics.task_duration('snippets'):.2f}s\n")

        # --- Task 1d-journal: Journal Distillation (Opus API, weekly) ---
        if task in ("journal", "all") and _system_enabled_or_skip("journal", "Task 1d-journal: Journal") and not _skip_if_over_budget("Task 1d-journal: Journal", 30):
            print("[Task 1d-journal: Journal Distillation]")
            metrics.start_task("journal")
            journal_apply_allowed = _can_apply_scope(
                "core_markdown_writes",
                "journal distillation updates root core markdown"
            )
            journal_dry_run = dry_run or (not journal_apply_allowed)
            lifecycle_result = parallel_lifecycle_results.get("journal") or _LIFECYCLE_REGISTRY.run(
                "journal",
                RoutineContext(
                    cfg=_cfg,
                    dry_run=journal_dry_run,
                    workspace=_workspace(),
                    force_distill=force_distill,
                ),
            )
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
            applied_changes["journal_additions"] = lifecycle_result.metrics.get("journal_additions", 0)
            applied_changes["journal_edits"] = lifecycle_result.metrics.get("journal_edits", 0)
            applied_changes["journal_entries_distilled"] = lifecycle_result.metrics.get("journal_entries_distilled", 0)
            metrics.end_task("journal")
            print(f"Task completed in {metrics.task_duration('journal'):.2f}s\n")

        # --- Task 6: Extract Edges (DEPRECATED) ---
        # As of Feb 2026, edges are now created at extraction time (index.ts) when facts
        # are first captured. The janitor review (Task 2) handles edge updates for FIX
        # operations. This task is kept for backward compatibility with --task edges,
        # but is skipped by default in --task all runs.
        if task == "edges":
            # Manual run - still execute for backward compatibility
            print("[Task 6: Extract Edges (DEPRECATED - manual run)]")
            _errors_before_edges = len(metrics.errors)
            use_full_scan = not incremental  # --full-scan broadens candidate discovery
            candidates = find_edge_candidates_optimized(graph, metrics, full_scan=use_full_scan)
            print(f"Found {len(candidates)} facts without edges"
                  f"{' (full scan)' if use_full_scan else ' (pattern-filtered)'}\n")

            relations_list = _build_relations_list(graph)
            print(f"  Known relations: {relations_list}\n")

            metrics.start_task("edges_extraction")
            extracted = 0
            skipped = 0
            no_edge = 0

            # Process in token-aware batches via Opus
            edge_builder = TokenBatchBuilder(
                model_tier='deep',
                prompt_overhead_tokens=estimate_tokens(relations_list) + 500,
                tokens_per_item_fn=lambda f: estimate_tokens(f.get("text", "")) + 30,
                max_items=100
            )
            edge_batches = edge_builder.build_batches(candidates)
            total_batches = len(edge_batches)
            consecutive_failures = 0

            for batch_num, batch in enumerate(edge_batches, 1):
                print(f"  Batch {batch_num}/{total_batches} ({len(batch)} facts)...")

                extractions = batch_extract_edges(batch, graph, metrics,
                                                  relations_list=relations_list)

                # Check for total batch failure
                if all(e is None for e in extractions):
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print(f"  3 consecutive batch failures, stopping edge extraction")
                        metrics.add_error("Edge extraction stopped: 3 consecutive failures")
                        memory_pipeline_ok = False
                        break
                else:
                    consecutive_failures = 0

                for i, extraction in enumerate(extractions):
                    if extraction is None:
                        no_edge += 1
                        continue

                    subj = extraction["subject"]
                    rel = extraction["relation"]
                    obj = extraction["object"]
                    print(f"    {'Would create' if dry_run else 'Creating'}: "
                          f"{subj} --[{rel}]--> {obj}")

                    if not dry_run:
                        owner = batch[i].get("owner_id", _default_owner_id())
                        source_id = _resolve_entity_node(
                            graph, subj, extraction["subject_type"], owner_id=owner)
                        target_id = _resolve_entity_node(
                            graph, obj, extraction["object_type"], owner_id=owner)

                        if source_id and target_id:
                            edge = Edge.create(source_id, target_id, rel)
                            graph.add_edge(edge)
                            extracted += 1
                            # Refresh relations list if a new type was introduced
                            if rel not in relations_list:
                                relations_list = _build_relations_list(graph)
                                print(f"    New relation type: {rel}")
                                # Generate keywords for the new relation type
                                if ensure_keywords_for_relation(rel):
                                    print(f"    Generated keywords for '{rel}'")
                        else:
                            skipped += 1
                            print(f"    Skipped (could not resolve entities)")

            metrics.end_task("edges_extraction")
            applied_changes["edges_created"] = extracted
            print(f"\nEdge extraction: {extracted} created, {no_edge} no-edge, {skipped} skipped")
            print(f"Task completed in {metrics.task_duration('edges_discovery') + metrics.task_duration('edges_extraction'):.2f}s\n")

            # Check if edge extraction added errors
            if len(metrics.errors) > _errors_before_edges:
                memory_pipeline_ok = False

        # Skip Task 6 when running "all" (deprecated)
        elif task == "all":
            print("[Task 6: Extract Edges] SKIPPED — deprecated (edges created at extraction time)\n")

        # --- Task 7: RAG Reindex + Project Discovery (Ollama embeddings) ---
        if task in ("rag", "all") and _system_enabled_or_skip("rag", "Task 7: RAG Reindex") and not _skip_if_over_budget("Task 7: RAG Reindex", 15):
            print("[Task 7: RAG Reindex + Project Discovery]")
            metrics.start_task("rag_reindex")
            lifecycle_result = _LIFECYCLE_REGISTRY.run(
                "rag",
                RoutineContext(cfg=_cfg, dry_run=dry_run, workspace=_workspace()),
            )

            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)

            applied_changes["project_events_processed"] = lifecycle_result.metrics.get("project_events_processed", 0)
            applied_changes["project_files_discovered"] = lifecycle_result.metrics.get("project_files_discovered", 0)
            applied_changes["rag_files_indexed"] = lifecycle_result.metrics.get("rag_files_indexed", 0)
            applied_changes["rag_chunks_created"] = lifecycle_result.metrics.get("rag_chunks_created", 0)
            applied_changes["rag_files_skipped"] = lifecycle_result.metrics.get("rag_files_skipped", 0)

            print(f"\n  RAG Index Updated:")
            print(f"    Total files: {lifecycle_result.metrics.get('rag_total_files', 0)}")
            print(f"    Indexed: {lifecycle_result.metrics.get('rag_files_indexed', 0)}")
            print(f"    Skipped (unchanged): {lifecycle_result.metrics.get('rag_files_skipped', 0)}")
            print(f"    Chunks created: {lifecycle_result.metrics.get('rag_chunks_created', 0)}")

            metrics.end_task("rag_reindex")
            print(f"Task completed in {metrics.task_duration('rag_reindex'):.2f}s\n")

        # --- Task 8: Unit Tests (subprocess) ---
        # Only run tests in dev mode: set QUAID_DEV=1 or janitor.run_tests=true in config
        _dev_mode = os.environ.get("QUAID_DEV", "").strip() in ("1", "true", "yes")
        _run_tests_cfg = getattr(_cfg.janitor, "run_tests", False) or _dev_mode
        if task == "tests" or (task == "all" and _run_tests_cfg):
            if not _skip_if_over_budget("Task 8: Tests", 30):
                print("[Task 8: Unit Tests]")
                test_result = run_tests(metrics)
                applied_changes["tests_passed"] = test_result["tests_passed"]
                applied_changes["tests_failed"] = test_result["tests_failed"]
                applied_changes["tests_total"] = test_result["tests_total"]
                print(f"Task completed in {metrics.task_duration('tests'):.2f}s\n")

        # --- Task 9: Audit Table Cleanup ---
        if task in ("cleanup", "all") and not _skip_if_over_budget("Task 9: Cleanup", 5):
            print("[Task 9: Audit Table Cleanup]")
            metrics.start_task("cleanup")
            lifecycle_result = _LIFECYCLE_REGISTRY.run(
                "datastore_cleanup",
                RoutineContext(cfg=_cfg, dry_run=dry_run, workspace=_workspace(), graph=graph),
            )
            for line in lifecycle_result.logs:
                print(f"  {line}")
            for err in lifecycle_result.errors:
                print(f"  {err}")
                metrics.add_error(err)
            applied_changes["cleanup"] = lifecycle_result.data.get("cleanup", {})
            metrics.end_task("cleanup")
            print(f"Task completed in {metrics.task_duration('cleanup'):.2f}s\n")

        # --- Task 10: Update Check ---
        update_info = None
        if task in ("update_check", "all"):
            print("[Task 10: Update Check]")
            metrics.start_task("update_check")
            try:
                update_info = _check_for_updates()
                if update_info:
                    print(f"  ⚠️  UPDATE AVAILABLE: v{update_info['current']} → v{update_info['latest']}")
                    print(f"  ⚠️  Update: curl -fsSL {get_install_url()} | bash")
                    print(f"  ⚠️  Release: {update_info['url']}")
                    applied_changes["update_available"] = update_info
                else:
                    print("  Up to date")
            except Exception as e:
                print(f"  Update check error: {e}")
            metrics.end_task("update_check")
            print(f"Task completed in {metrics.task_duration('update_check'):.2f}s\n")

        # --- Health Snapshot ---
        if task == "all":
            try:
                health = graph.get_health_metrics()
                total = health["total_nodes"]
                with_emb_str = health.get("embedding_coverage", "0/0")
                with_emb = int(with_emb_str.split("/")[0]) if "/" in str(with_emb_str) else 0
                emb_pct = (with_emb / total * 100) if total > 0 else 0.0

                # Avg confidence across all statuses
                conf_stats = health.get("confidence_by_status", {})
                all_counts = sum(s.get("count", 0) for s in conf_stats.values())
                avg_conf = (sum(s.get("avg_confidence", 0) * s.get("count", 0) for s in conf_stats.values()) / all_counts) if all_counts > 0 else 0.0

                # Confidence distribution buckets
                conf_dist = {"0.0-0.3": 0, "0.3-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
                with graph._get_conn() as conn:
                    for row in conn.execute("SELECT confidence FROM nodes").fetchall():
                        c = row["confidence"]
                        if c < 0.3: conf_dist["0.0-0.3"] += 1
                        elif c < 0.5: conf_dist["0.3-0.5"] += 1
                        elif c < 0.7: conf_dist["0.5-0.7"] += 1
                        elif c < 0.9: conf_dist["0.7-0.9"] += 1
                        else: conf_dist["0.9-1.0"] += 1

                    conn.execute("""
                        INSERT INTO health_snapshots
                            (total_nodes, total_edges, avg_confidence, nodes_by_status,
                             confidence_distribution, staleness_distribution,
                             orphan_count, embedding_coverage)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        total,
                        health["total_edges"],
                        round(avg_conf, 4),
                        json.dumps({k: v.get("count", 0) for k, v in conf_stats.items()}),
                        json.dumps(conf_dist),
                        json.dumps(health.get("staleness_distribution", {})),
                        health.get("orphan_nodes", 0),
                        round(emb_pct, 1),
                    ))
                print(f"[Health Snapshot] Recorded: {total} nodes, {health['total_edges']} edges, avg conf {avg_conf:.3f}")
            except Exception as e:
                print(f"[Health Snapshot] Failed: {e}")

        # --- Final Step: Graduate approved → active ---
        # After all processing (review, dedup, contradiction, edges), promote
        # approved facts to active so they're never reprocessed by the pipeline.
        # CRITICAL: Only graduate if memory pipeline completed without errors.
        if task in ("all", "graduate") and not dry_run and _cfg.systems.memory:
            if task == "all" and not memory_pipeline_ok:
                error_count = len(metrics.errors)
                print(f"[Final: Graduate approved → active] BLOCKED")
                print(f"  {error_count} error(s) occurred during memory pipeline.")
                print(f"  Facts remain as approved/pending — will be reprocessed next run.\n")
            else:
                if task == "all":
                    print("[Final: Graduate approved → active]")
                else:
                    print("[Task: Graduate approved → active]")
                with graph._get_conn() as conn:
                    cursor = conn.execute(
                        "UPDATE nodes SET status = 'active' WHERE status = 'approved'"
                    )
                    graduated = cursor.rowcount
                applied_changes["graduated_to_active"] = graduated
                print(f"  Graduated {graduated} memories from approved → active\n")

    except Exception as e:
        metrics.add_error(f"Critical error in task {task}: {str(e)}")
        memory_pipeline_ok = False
        print(f"ERROR: {e}")

    # Benchmark-mode validity gate: janitor owns run validity semantics.
    if _is_benchmark_mode() and not dry_run and task in ("all", "graduate"):
        try:
            with graph._get_conn() as conn:
                pending = int(conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE status = 'pending'"
                ).fetchone()[0] or 0)
                approved = int(conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE status = 'approved'"
                ).fetchone()[0] or 0)
            if pending > 0 or approved > 0:
                msg = (
                    "Benchmark mode invalid state: "
                    f"pending={pending}, approved={approved} after janitor task={task}"
                )
                print(f"[benchmark] {msg}")
                metrics.add_error(msg)
                memory_pipeline_ok = False
        except Exception as e:
            metrics.add_error(f"Benchmark mode validation failed: {e}")
            memory_pipeline_ok = False

    # Generate comprehensive report
    final_metrics = metrics.summary()
    
    print(f"{'='*80}")
    print("JANITOR PERFORMANCE REPORT")
    print(f"{'='*80}")
    print(f"Total execution time: {final_metrics['total_duration_seconds']}s")
    print(f"LLM calls made: {final_metrics['llm_calls']}")
    print(f"LLM time total: {final_metrics['llm_time_seconds']}s")
    print(f"Errors encountered: {final_metrics['errors']}")
    
    if final_metrics['task_durations']:
        print(f"\nTask breakdown:")
        for task_name, duration in final_metrics['task_durations'].items():
            print(f"  {task_name}: {duration}s")
    
    print(f"\nChanges applied:")
    for change_type, count in applied_changes.items():
        print(f"  {change_type}: {count}")
    
    # Cost report
    usage = get_token_usage()
    cost = estimate_cost()
    if usage["api_calls"] > 0:
        print(f"\nAPI usage:")
        print(f"  Calls: {usage['api_calls']}")
        print(f"  Input tokens: {usage['input_tokens']:,}")
        print(f"  Output tokens: {usage['output_tokens']:,}")
        print(f"  Estimated cost: ${cost:.4f}")

    if _budget_skipped:
        print(f"\nSkipped due to time budget ({time_budget}s):")
        for label in _budget_skipped:
            print(f"  {label}")

    if final_metrics['errors'] > 0:
        print(f"\nRecent errors:")
        for error in final_metrics['error_details']:
            print(f"  {error['time']}: {error['error']}")

    print(f"{'='*80}")
    
    # Performance warning (adjusted for time-bounded execution)
    if final_metrics['total_duration_seconds'] > MAX_EXECUTION_TIME:  # Check CRITICAL first
        print(f"\nCRITICAL: Janitor exceeded time limit ({final_metrics['total_duration_seconds']/60:.1f}min > {MAX_EXECUTION_TIME/60:.1f}min)")
        print("Time-bounded execution may have missed some contradictions!")
    elif final_metrics['total_duration_seconds'] > 600:  # 10 minutes
        print(f"\nWARNING: Janitor took {final_metrics['total_duration_seconds']/60:.1f}min (>10min)")
        print("Consider running with --incremental or checking for performance issues")
    
    # Coverage warning for contradiction checks
    if 'contradictions_coverage' in applied_changes:
        coverage = applied_changes['contradictions_coverage']
        if coverage < 1.0:
            print(f"\n📊 INFO: Contradiction check coverage: {coverage*100:.1f}% due to time constraints")
    
    # Log completion with metrics
    success = final_metrics['errors'] == 0
    janitor_logger.info(
        "janitor_complete",
        task=task,
        success=success,
        duration_seconds=final_metrics['total_duration_seconds'],
        llm_calls=final_metrics['llm_calls'],
        errors=final_metrics['errors'],
        **{k: v for k, v in applied_changes.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0}
    )

    # Record run in janitor_runs table (enables incremental mode)
    if not dry_run:
        try:
            actions = sum(v for v in applied_changes.values() if isinstance(v, (int, float)) and v > 0)
            with graph._get_conn() as conn:
                conn.execute("""
                    INSERT INTO janitor_runs (task_name, started_at, completed_at, memories_processed, actions_taken, status)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (task, datetime.fromtimestamp(metrics.start_time).isoformat(), datetime.now().isoformat(),
                      applied_changes.get("memories_reviewed", 0) + applied_changes.get("duplicates_merged", 0),
                      actions, "completed" if success else "failed"))
        except Exception as e:
            print(f"  Warning: Failed to record janitor run: {e}")

    # Queue user notifications through lifecycle event bus (only for full runs, not dry-run)
    if task == "all" and not dry_run:
        try:
            from core.runtime.events import emit_event, process_events

            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            with graph._get_conn() as conn:
                rows = conn.execute("""
                    SELECT name as text
                    FROM nodes
                    WHERE type = 'Fact'
                      AND created_at >= ?
                      AND status IN ('pending', 'approved', 'active')
                    ORDER BY created_at DESC
                    LIMIT 25
                """, (today_start,)).fetchall()
            today_memories = [{"text": str(r["text"]), "category": "fact"} for r in rows]

            emit_event(
                name="janitor.run_completed",
                payload={
                    "metrics": final_metrics,
                    "applied_changes": applied_changes,
                    "today_memories": today_memories,
                },
                source="janitor",
                owner_id=_default_owner_id(),
                priority="normal",
            )
            process_events(limit=1, names=["janitor.run_completed"])
            print("[notify] Janitor completion event dispatched")

        except Exception as e:
            print(f"[notify] Failed to dispatch janitor completion event: {e}")

    # Return metrics for programmatic use
    # WAL checkpoint at end of run to reclaim WAL file space
    if task == "all" and not dry_run:
        try:
            with graph._get_conn() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # Checkpoint is best-effort

    return {
        "success": success,
        "memory_pipeline_ok": memory_pipeline_ok,
        "metrics": final_metrics,
        "applied_changes": applied_changes
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Memory Janitor (Optimized)")
    parser.add_argument("--task", choices=["embeddings", "workspace", "docs_staleness", "docs_cleanup", "snippets", "soul_snippets", "journal", "review", "dedup_review", "duplicates", "contradictions", "decay", "decay_review", "graduate", "edges", "rag", "tests", "cleanup", "update_check", "all"],
                       default="all", help="Task to run")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    parser.add_argument("--approve", action="store_true",
                        help="Confirm apply when janitor.applyMode is set to 'ask'")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no changes (default)")
    parser.add_argument("--full-scan", action="store_true", help="Force full scan instead of incremental")
    parser.add_argument("--force-distill", action="store_true",
                        help="Force journal distillation regardless of interval")
    parser.add_argument("--time-budget", type=int, default=0,
                        help="Wall-clock budget in seconds (0 = unlimited). Tasks are skipped when time runs low.")
    parser.add_argument("--token-budget", type=int, default=None,
                        help="Max total tokens (input+output) for LLM calls (0 = unlimited). "
                             "LLM calls are skipped when budget is exhausted.")

    args = parser.parse_args()

    # Resolve token budget precedence: CLI > config > env fallback (compat).
    try:
        config_token_budget = int(getattr(_cfg.janitor, "token_budget", 0) or 0)
    except Exception:
        config_token_budget = 0
    try:
        env_token_budget = int(os.environ.get("JANITOR_TOKEN_BUDGET", "0") or 0)
    except Exception:
        env_token_budget = 0
    effective_token_budget = (
        int(args.token_budget) if args.token_budget is not None
        else (config_token_budget if config_token_budget > 0 else env_token_budget)
    )
    if effective_token_budget > 0:
        source = "cli" if args.token_budget is not None else ("config" if config_token_budget > 0 else "env")
        print(f"[janitor] Token budget: {effective_token_budget:,} tokens (source: {source})")

    # dry_run is derived from apply flags and janitor apply policy.
    dry_run, apply_policy_warning = _resolve_apply_mode(args.apply, args.approve)
    if apply_policy_warning:
        print(f"[policy] {apply_policy_warning}")
    incremental = not args.full_scan

    result = run_task_optimized(args.task, dry_run=dry_run, incremental=incremental,
                                time_budget=args.time_budget,
                                force_distill=args.force_distill,
                                user_approved=args.approve,
                                token_budget=effective_token_budget)
    
    # Write stats to file for dashboard consumption
    stats_file = _logs_dir() / "janitor-stats.json"
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    usage = get_token_usage()
    stats_data = {
        "last_run": datetime.now().isoformat(),
        "task": args.task,
        "dry_run": dry_run,
        "success": result["success"],
        "applied_changes": result["applied_changes"],
        "metrics": result["metrics"],
        "api_usage": {
            "calls": usage["api_calls"],
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "estimated_cost_usd": estimate_cost(),
        }
    }
    with open(stats_file, "w") as f:
        json.dump(stats_data, f, indent=2)
    print(f"\n📊 Stats written to {stats_file}")
    
    # Exit with error code if janitor failed
    exit(0 if result["success"] else 1)
