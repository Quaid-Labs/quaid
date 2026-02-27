#!/usr/bin/env python3
"""AgentLife Production Benchmark — Full pipeline evaluation.

Unlike previous benchmarks that stored facts as `active` (skipping review),
this script runs the FULL production pipeline:

1. Workspace setup: isolated DB, config, core markdowns, project seeds
2. Incremental project files: copy source at correct git commits, RAG reindex
3. Full extraction: timeout-based chunk collection with Opus/Sonnet → facts as `pending`,
   snippets, journal entries
4. Full janitor: review, dedup, contradictions, workspace audit, snippets
   FOLD/REWRITE/DISCARD, journal distillation, RAG reindex, graduation
5. Eval with tool use: Opus answers using memory_recall + search_project_docs

Usage:
    # Full run (all phases)
    python3 run_production_benchmark.py --mode full

    # Ingest only (extraction + janitor, no eval)
    python3 run_production_benchmark.py --mode ingest

    # Eval only (assumes workspace already built)
    python3 run_production_benchmark.py --mode eval

    # Skip janitor (debug extraction)
    python3 run_production_benchmark.py --mode full --skip-janitor
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import threading
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import math
from urllib.error import HTTPError, URLError

_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _DIR.parent
from pathing import resolve_paths

_CLAWD, _QUAID_DIR, _RUNNER_DIR = resolve_paths(_DIR)

def _quaid_cli_cmd(*args: str) -> Optional[List[str]]:
    cli = _QUAID_DIR / "quaid"
    if cli.exists():
        return ["/bin/bash", str(cli), *args]
    return None


def _schema_path() -> Path:
    for candidate in (
        _QUAID_DIR / "schema.sql",
        _QUAID_DIR / "datastore" / "memorydb" / "schema.sql",
    ):
        if candidate.exists():
            return candidate
    return _QUAID_DIR / "schema.sql"


_REQUIRED_DB_TABLES = ("nodes", "edges", "domain_registry", "node_domains")


def _ensure_workspace_db_schema(workspace: Path, repair: bool = True) -> None:
    """Ensure workspace DB has schema objects required by current checkpoint."""
    db_path = workspace / "data" / "memory.db"
    if not db_path.exists():
        raise RuntimeError(f"memory.db missing: {db_path}")

    def _missing_tables(conn: sqlite3.Connection) -> List[str]:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        existing = {str(r[0]) for r in rows}
        return [t for t in _REQUIRED_DB_TABLES if t not in existing]

    with sqlite3.connect(str(db_path)) as conn:
        missing = _missing_tables(conn)
        if missing and repair:
            conn.executescript(_schema_path().read_text())
            missing = _missing_tables(conn)
        if missing:
            raise RuntimeError(
                "workspace DB schema mismatch; missing tables: "
                + ", ".join(missing)
                + f" (db={db_path})"
            )


def _memory_graph_script_path() -> Path:
    for candidate in (
        _QUAID_DIR / "memory_graph.py",
        _QUAID_DIR / "datastore" / "memorydb" / "memory_graph.py",
    ):
        if candidate.exists():
            return candidate
    return _QUAID_DIR / "memory_graph.py"


def _memory_graph_cmd(*args: str) -> List[str]:
    cli_cmd = _quaid_cli_cmd(*args)
    if cli_cmd:
        return cli_cmd
    return [sys.executable, str(_memory_graph_script_path()), *args]


def _janitor_cmd(*args: str) -> List[str]:
    cli_cmd = _quaid_cli_cmd("janitor", *args)
    if cli_cmd:
        return cli_cmd
    return [sys.executable, str(_QUAID_DIR / "janitor.py"), *args]


def _docs_search_cmd(query: str, project: Optional[str] = None) -> List[str]:
    cli_cmd = _quaid_cli_cmd("docs", "search", query)
    if cli_cmd:
        if project:
            cli_cmd.extend(["--project", project])
        return cli_cmd
    cmd = [sys.executable, str(_QUAID_DIR / "docs_rag.py"), "search", query]
    if project:
        cmd.extend(["--project", project])
    return cmd


def _quaid_project_docs_source_dir() -> Optional[Path]:
    """Resolve canonical source dir for projects/quaid docs."""
    candidates = [
        _CLAWD / "projects" / "quaid",
        _QUAID_DIR.parent.parent / "projects" / "quaid",
        _QUAID_DIR / "projects" / "quaid",
    ]
    for c in candidates:
        if c.exists() and c.is_dir():
            return c
    return None


def _seed_quaid_project_docs(workspace: Path) -> None:
    """Mirror canonical projects/quaid markdown docs into workspace."""
    target_root = workspace / "projects" / "quaid"
    target_root.mkdir(parents=True, exist_ok=True)
    src = _quaid_project_docs_source_dir()
    if not src:
        print("  WARNING: canonical projects/quaid docs not found; using fallback stubs")
        (target_root / "AGENTS.md").write_text(
            "# Memory System Project Notes\n\n"
            "Canonical Quaid docs were unavailable during seed.\n"
            "Use `memory_recall` for personal and technical memory queries.\n"
            "Use `search_project_docs` for project and implementation details.\n"
        )
        (target_root / "TOOLS.md").write_text(
            "# Memory System — Tools\n\n"
            "- `memory_recall(query, domain?)`\n"
            "  - domain filters: `{\"all\": true}` (default) or `{\"technical\": true}`\n"
            "- `search_project_docs(query, project?)`\n"
            "- Session pathways: `last_session_continuation`, `session_search`\n"
        )
        return

    copied = 0
    for md in sorted(src.rglob("*.md")):
        rel = md.relative_to(src)
        out = target_root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md.read_text())
        copied += 1

    print(f"  Seeded projects/quaid docs from source: {copied} markdown files")

_API_RETRY_ATTEMPTS = 3
_API_RETRY_BACKOFF_S = 1.5
def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"ERROR: {name} must be an integer, got {raw!r}", file=sys.stderr)
        raise SystemExit(2)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"ERROR: {name} must be a float, got {raw!r}", file=sys.stderr)
        raise SystemExit(2)


_VLLM_RETRY_ATTEMPTS = _env_int("VLLM_RETRY_ATTEMPTS", 5)
_VLLM_RETRY_BACKOFF_S = _env_float("VLLM_RETRY_BACKOFF_S", 2.0)
_VLLM_TIMEOUT_S = _env_int("VLLM_TIMEOUT_S", 900)
_ANTHROPIC_LONG_RETRY_MAX_SECONDS = _env_int("ANTHROPIC_LONG_RETRY_MAX_SECONDS", 43200)
_ANTHROPIC_LONG_RETRY_BASE_SECONDS = _env_float("ANTHROPIC_LONG_RETRY_BASE_SECONDS", 15.0)
_ANTHROPIC_LONG_RETRY_MAX_BACKOFF_SECONDS = _env_float("ANTHROPIC_LONG_RETRY_MAX_BACKOFF_SECONDS", 300.0)
_CLAUDE_CODE_RETRY_ATTEMPTS = _env_int("CLAUDE_CODE_RETRY_ATTEMPTS", 4)
_CLAUDE_CODE_RETRY_BACKOFF_S = _env_float("CLAUDE_CODE_RETRY_BACKOFF_S", 2.0)
_CLAUDE_CODE_TIMEOUT_S = _env_int("CLAUDE_CODE_TIMEOUT_S", 900)
_EVAL_TIMEOUT_BACKOFF_ENABLED = os.environ.get("EVAL_TIMEOUT_BACKOFF_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_EVAL_TIMEOUT_BACKOFF_WINDOW = max(1, _env_int("EVAL_TIMEOUT_BACKOFF_WINDOW", 12))
_EVAL_TIMEOUT_BACKOFF_THRESHOLD = max(1, _env_int("EVAL_TIMEOUT_BACKOFF_THRESHOLD", 3))
_EVAL_TIMEOUT_BACKOFF_STEP = max(1, _env_int("EVAL_TIMEOUT_BACKOFF_STEP", 1))
_EVAL_TIMEOUT_BACKOFF_MIN_PARALLEL = max(1, _env_int("EVAL_TIMEOUT_BACKOFF_MIN_PARALLEL", 2))
_STRUCTURED_DOC_MAX_CHARS = _env_int("EVAL_STRUCTURED_DOC_MAX_CHARS", 0)  # 0 = full file
_SOURCE_SNIPPET_MAX_CHARS = _env_int("EVAL_SOURCE_SNIPPET_MAX_CHARS", 4000)
_SOURCE_SNIPPET_TOP_FILES = _env_int("EVAL_SOURCE_SNIPPET_TOP_FILES", 8)
_RAG_TIMEOUT_S = _env_int("EVAL_PROJECT_RAG_TIMEOUT_S", 30)
_EVAL_CONTEXT_MIN_CHARS = _env_int("EVAL_CONTEXT_MIN_CHARS", 20000)


def _safe_env_int(name: str, default: int, min_value: Optional[int] = None) -> int:
    """Best-effort integer env parsing with optional lower bound."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"WARNING: {name} must be an integer, got {raw!r}; using {default}")
        return default
    if min_value is not None and value < min_value:
        print(f"WARNING: {name} must be >= {min_value}, got {value}; using {default}")
        return default
    return value

# Benchmark-stable janitor profile:
# includes memory lifecycle + snippets/journal; excludes workspace audit/docs cleanup.
BENCHMARK_JANITOR_TASKS = [
    "rag",
    "embeddings",
    "review",
    "dedup_review",
    "duplicates",
    "contradictions",
    "decay",
    "decay_review",
    "graduate",
    "snippets",
    "journal",
]

# Capture snippets/journal during extraction to preserve product behavior.
ENABLE_CORE_MARKDOWN_CAPTURE = True


def _vllm_endpoint(base_url: str, resource: str) -> str:
    """Build a vLLM endpoint URL without duplicating `/v1`."""
    root = (base_url or "").rstrip("/")
    suffix = resource if resource.startswith("/") else f"/{resource}"
    if root.endswith("/v1"):
        return f"{root}{suffix}"
    return f"{root}/v1{suffix}"


def _atomic_write_json(path: Path, payload: object) -> None:
    """Write JSON atomically to avoid torn checkpoint files on crashes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _read_json_or_none(path: Path) -> Optional[Any]:
    """Best-effort JSON read; returns None on parse/read failure."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _git_lock_holder_pid(lock_path: Path) -> str:
    """Best-effort PID lookup for a git index.lock owner."""
    holder_pid = ""
    for cmd in (["lsof", "-t", str(lock_path)], ["fuser", str(lock_path)]):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode != 0:
            continue
        tokens = re.findall(r"\d+", (proc.stdout or "").strip())
        if tokens:
            holder_pid = tokens[0]
            break
    return holder_pid


def _resolve_git_dir(repo_path: Path) -> Path:
    """Resolve the actual git dir for a repo/subdir path."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            raw = (proc.stdout or "").strip()
            if raw:
                git_dir = Path(raw)
                if not git_dir.is_absolute():
                    git_dir = (repo_path / git_dir).resolve()
                return git_dir
    except Exception:
        pass
    return repo_path / ".git"


def _wait_or_clear_git_index_lock(repo_path: Path, *, context: str, timeout_s: float = 20.0) -> None:
    """Wait for active git locks and clear stale locks within a bounded timeout."""
    lock_path = _resolve_git_dir(repo_path) / "index.lock"
    deadline = time.monotonic() + max(1.0, timeout_s)
    while lock_path.exists():
        holder_pid = _git_lock_holder_pid(lock_path)
        if holder_pid:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Active git index lock at {lock_path} (pid={holder_pid}) during {context}"
                )
            time.sleep(0.5)
            continue
        try:
            lock_path.unlink()
            print(f"  WARNING: removed stale git index lock at {lock_path} during {context}")
            return
        except OSError as exc:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Unable to remove stale git index lock at {lock_path} during {context}: {exc}"
                ) from exc
            time.sleep(0.25)


@contextmanager
def _repo_checkout_lock(repo_path: Path, *, context: str, timeout_s: float = 60.0):
    """Serialize git checkout activity per repo to prevent cross-run checkout races."""
    git_dir = _resolve_git_dir(repo_path)
    lock_key = hashlib.sha1(str(git_dir).encode("utf-8")).hexdigest()[:12]
    lock_file = Path("/tmp") / f"quaid-bench-checkout-{lock_key}.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a+", encoding="utf-8") as fh:
        deadline = time.monotonic() + max(1.0, timeout_s)
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Timed out waiting for checkout lock {lock_file} during {context}"
                    )
                time.sleep(0.25)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _git_checkout_with_retry(
    repo_path: Path,
    target: str,
    *,
    context: str,
    timeout_s: int = 20,
    attempts: int = 3,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Checkout with bounded retries for transient lock/timeout failures."""
    attempts = max(1, attempts)
    last_proc: Optional[subprocess.CompletedProcess] = None
    for attempt in range(1, attempts + 1):
        _wait_or_clear_git_index_lock(repo_path, context=f"{context} (attempt {attempt})")
        try:
            proc = subprocess.run(
                ["git", "checkout", target],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            if attempt < attempts:
                time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
                continue
            raise

        last_proc = proc
        if proc.returncode == 0:
            return proc

        stderr = (proc.stderr or proc.stdout or "").lower()
        transient = ("index.lock" in stderr) or ("another git process" in stderr)
        if transient and attempt < attempts:
            time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))
            continue
        if check:
            raise subprocess.CalledProcessError(
                proc.returncode,
                ["git", "checkout", target],
                output=proc.stdout,
                stderr=proc.stderr,
            )
        return proc

    if check and last_proc is not None:
        raise subprocess.CalledProcessError(
            last_proc.returncode,
            ["git", "checkout", target],
            output=last_proc.stdout,
            stderr=last_proc.stderr,
        )
    assert last_proc is not None
    return last_proc


def _repo_root_and_relpath(source_repo: Path) -> Tuple[Path, str]:
    """Return git repo root and source_repo path relative to that root."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source_repo,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    repo_root = Path((proc.stdout or "").strip()).resolve()
    rel = source_repo.resolve().relative_to(repo_root).as_posix()
    return repo_root, rel


def _sync_repo_subtree_at_commit(
    source_repo: Path,
    target_dir: Path,
    commit: str,
    *,
    context: str,
) -> None:
    """Materialize a subdir at commit via git-archive (no checkout) and rsync into target."""
    extracted: Path = source_repo
    try:
        repo_root, rel_path = _repo_root_and_relpath(source_repo)
        with tempfile.TemporaryDirectory(prefix="bench-archive-") as tmpdir:
            with _repo_checkout_lock(repo_root, context=f"{context} archive"):
                _wait_or_clear_git_index_lock(repo_root, context=f"{context} archive preflight")
                archive = subprocess.run(
                    ["git", "-C", str(repo_root), "archive", commit, rel_path],
                    capture_output=True,
                    timeout=45,
                    check=True,
                )
                subprocess.run(
                    ["tar", "-x", "-C", tmpdir],
                    input=archive.stdout,
                    capture_output=True,
                    timeout=45,
                    check=True,
                )

            extracted = Path(tmpdir) / rel_path
            if not extracted.exists():
                raise RuntimeError(
                    f"{context}: git archive produced no extracted subtree at {extracted}"
                )

            excludes = [".git", "node_modules", "package-lock.json", "PROJECT.md", "TOOLS.md"]
            cmd = ["rsync", "-a", "--delete"]
            for exc in excludes:
                cmd.extend(["--exclude", exc])
            cmd.extend([str(extracted) + "/", str(target_dir) + "/"])
            subprocess.run(cmd, capture_output=True, timeout=60, check=True)
            return
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.output or b"")
        if isinstance(stderr, bytes):
            stderr_text = stderr.decode("utf-8", errors="replace")
        else:
            stderr_text = str(stderr)
        if "not a valid object name" not in stderr_text and "unknown revision" not in stderr_text:
            raise
        print(
            f"  WARNING: {context}: commit {commit} not present in local git history; "
            "falling back to current workspace snapshot"
        )

    excludes = [".git", "node_modules", "package-lock.json", "PROJECT.md", "TOOLS.md"]
    cmd = ["rsync", "-a", "--delete"]
    for exc in excludes:
        cmd.extend(["--exclude", exc])
    cmd.extend([str(extracted) + "/", str(target_dir) + "/"])
    subprocess.run(cmd, capture_output=True, timeout=60, check=True)


def _compute_dynamic_k(db_path: Path) -> int:
    """Compute retrieval limit K from active node count.

    Formula: K = round(11.5 * ln(N) - 61.7), clamped to [5, 40].
    Fitted to K-sweep benchmarks:
      S-scale (~322 nodes): K=5
      L-scale (~1182 nodes): K=20
    """
    try:
        with sqlite3.connect(str(db_path)) as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE status IN ('active', 'approved')"
            ).fetchone()[0]
    except sqlite3.Error as exc:
        print(f"WARNING: dynamic-k fallback for {db_path}: {exc}")
        n = 100  # fallback
    if n < 10:
        return 5
    k = round(11.5 * math.log(n) - 61.7)
    return max(5, min(k, 40))

sys.path.insert(0, str(_DIR))
from dataset import (
    load_all_reviews, load_filler_reviews, merge_sessions_chronologically,
    get_all_eval_queries, format_transcript_for_extraction,
    SESSION_DATES, SESSION_TRACKS, FILLER_DATES,
)
from extract_compact import (
    build_extraction_prompt, parse_extraction_response,
    write_snippet_entry, write_journal_entry,
)
from metrics import score_results, score_blended, retrieval_metrics, format_report

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def _resolve_project_source_dir(project_slug: str, explicit_env: str) -> Path:
    """Resolve source repo path for benchmark project ingestion.

    Precedence:
    1) Explicit project env (BENCH_RECIPE_APP_DIR / BENCH_PORTFOLIO_DIR)
    2) BENCH_PROJECTS_ROOT/<project_slug> (agentlife-branded root)
    3) Known local candidates
    Returns the first existing candidate, otherwise the highest-priority candidate.
    """
    explicit = (os.environ.get(explicit_env, "") or "").strip()
    if explicit:
        return Path(explicit).expanduser()

    candidates: List[Path] = []
    projects_root = (os.environ.get("BENCH_PROJECTS_ROOT", "") or "").strip()
    if projects_root:
        candidates.append(Path(projects_root).expanduser() / project_slug)

    candidates.extend(
        [
            _PROJECT_DIR / project_slug,
            _PROJECT_DIR.parent / "projects" / "agentlife" / project_slug,
            Path.home() / "clawd" / "projects" / "agentlife" / project_slug,
            Path.home() / "quaid" / "projects" / "agentlife" / project_slug,
        ]
    )

    for c in candidates:
        if c.exists():
            return c
    return candidates[0] if candidates else (_PROJECT_DIR / project_slug)


RECIPE_APP_DIR = _resolve_project_source_dir("recipe-app", "BENCH_RECIPE_APP_DIR")
PORTFOLIO_DIR = _resolve_project_source_dir("portfolio-site", "BENCH_PORTFOLIO_DIR")

SESSION_TO_RECIPE_COMMIT = {
    3: "1073804",   # scaffold with Express + SQLite CRUD
    5: "f5994b3",   # dietary tags, Safe for Mom filter
    7: "385b321",   # SQL injection fix, test suite
    10: "3e12a09",  # meal planning, structured ingredients
    12: "4f04887",  # GraphQL API, recipe sharing, Docker
    16: "7cc628c",  # bug bash — rate limiter, sharing tests
    18: "88b409c",  # JWT auth, user accounts
    20: "dc4c444",  # SQL injection test fix
}

SESSION_TO_PORTFOLIO_COMMIT = {
    9: "c859e9a",   # initial portfolio (TechFlow era)
    14: "0384d4d",  # update for Stripe
}

# All sessions in chronological order with their commit triggers
PROJECT_SESSIONS = sorted(
    [(s, "recipe-app", c) for s, c in SESSION_TO_RECIPE_COMMIT.items()] +
    [(s, "portfolio-site", c) for s, c in SESSION_TO_PORTFOLIO_COMMIT.items()],
    key=lambda x: x[0],
)


# ---------------------------------------------------------------------------
# Phase 1: Workspace setup
# ---------------------------------------------------------------------------

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_DEEP_MODEL = "claude-sonnet-4-5-20250929"


def _apply_workspace_provider_profile(prod_config: dict) -> None:
    """Apply benchmark provider routing profile to config/memory.json."""
    if not isinstance(prod_config.get("models"), dict):
        prod_config["models"] = {}
    models = prod_config["models"]

    if _BACKEND == "vllm":
        models["llmProvider"] = "openai-compatible"
        models["deepReasoningProvider"] = "openai-compatible"
        models["fastReasoningProvider"] = "openai-compatible"
        models["deepReasoning"] = _VLLM_MODEL
        models["deep_reasoning"] = _VLLM_MODEL
        models["fastReasoning"] = _VLLM_MODEL
        models["fast_reasoning"] = _VLLM_MODEL
        return

    if _QUAID_PROVIDER_PROFILE == "mixed":
        has_anthropic_key = bool((os.environ.get("ANTHROPIC_API_KEY") or "").strip())
        # Production-style split routing:
        # fast tier via API key (anthropic), deep tier via claude -p (claude-code).
        models["llmProvider"] = "claude-code"  # fallback for un-tiered calls
        models["deepReasoningProvider"] = "claude-code"
        # If Anthropic key is unavailable in this run environment, keep fast tier
        # on claude-code to avoid fail-hard routing errors in recall/HyDE.
        models["fastReasoningProvider"] = "anthropic" if has_anthropic_key else "claude-code"
        models["deepReasoning"] = _DEEP_MODEL
        models["deep_reasoning"] = _DEEP_MODEL
        models["fastReasoning"] = _HAIKU_MODEL if has_anthropic_key else _DEEP_MODEL
        models["fast_reasoning"] = _HAIKU_MODEL if has_anthropic_key else _DEEP_MODEL
        return

    # API-only profile
    models["llmProvider"] = "anthropic"
    models["deepReasoningProvider"] = "anthropic"
    models["fastReasoningProvider"] = "anthropic"
    models["deepReasoning"] = _DEEP_MODEL
    models["deep_reasoning"] = _DEEP_MODEL
    models["fastReasoning"] = _HAIKU_MODEL
    models["fast_reasoning"] = _HAIKU_MODEL


def _rewrite_config(workspace: Path) -> None:
    """Rewrite config/memory.json with current backend/provider settings.

    Called on both fresh setup and resume to ensure provider config matches
    the current harness settings (e.g., llmProvider, model selections).
    """
    config_path = workspace / "config" / "memory.json"
    if config_path.exists():
        prod_config = json.loads(config_path.read_text())
    else:
        fallback = _CLAWD / "config" / "memory.json"
        if fallback.exists():
            prod_config = json.loads(fallback.read_text())
        else:
            # Fresh/relocated environments may not have a source config yet.
            # Start from a minimal config and let profile writers populate fields.
            prod_config = {}

    _apply_workspace_provider_profile(prod_config)
    if not isinstance(prod_config.get("retrieval"), dict):
        prod_config["retrieval"] = {}
    # Fail-hard must be config-driven (not env-driven) for reproducible runs.
    prod_config["retrieval"]["failHard"] = True
    prod_config["retrieval"]["fail_hard"] = True

    if not isinstance(prod_config.get("janitor"), dict):
        prod_config["janitor"] = {}
    # Benchmarks must run janitor in apply mode (no interactive approvals).
    prod_config["janitor"]["applyMode"] = "auto"
    prod_config["janitor"]["apply_mode"] = "auto"
    if not isinstance(prod_config["janitor"].get("opus_review"), dict):
        prod_config["janitor"]["opus_review"] = {}
    prod_config["janitor"]["opus_review"]["model"] = _VLLM_MODEL if _BACKEND == "vllm" else _HAIKU_MODEL
    prod_config["janitor"]["approvalPolicies"] = {
        "coreMarkdownWrites": "auto",
        "projectDocsWrites": "auto",
        "workspaceFileMovesDeletes": "auto",
        "destructiveMemoryOps": "auto",
    }
    if not isinstance(prod_config.get("systems"), dict):
        prod_config["systems"] = {}
    prod_config["systems"]["journal"] = True
    prod_config["systems"]["workspace"] = False
    if not isinstance(prod_config.get("docs"), dict):
        prod_config["docs"] = {}
    if not isinstance(prod_config["docs"].get("journal"), dict):
        prod_config["docs"]["journal"] = {}
    prod_config["docs"]["journal"]["enabled"] = True

    config_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(config_path, prod_config)
    print(f"  Config rewritten: llmProvider={prod_config['models']['llmProvider']}")


def setup_workspace(workspace: Path) -> None:
    """Create isolated benchmark workspace with fresh DB, config, and seeds."""
    print("=" * 60)
    print("PHASE 1: WORKSPACE SETUP")
    print("=" * 60)

    # Janitor snippet/journal review calls frequently exceed 120s on long prompts.
    # Raise timeout for benchmark reliability unless operator already set it.
    os.environ.setdefault("QUAID_SNIPPETS_REVIEW_TIMEOUT_SECONDS", "300")

    # Create directory structure
    for d in [
        "data", "config", "journal", "extraction_cache", "logs",
        "projects/recipe-app", "projects/portfolio-site", "projects/quaid",
    ]:
        (workspace / d).mkdir(parents=True, exist_ok=True)

    # 1. Fresh DB from schema
    db_path = workspace / "data" / "memory.db"
    if db_path.exists():
        db_path.unlink()
    for suffix in ["-wal", "-shm"]:
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()

    schema = _schema_path().read_text()
    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(schema)
    print(f"  DB initialized: {db_path}")
    _ensure_workspace_db_schema(workspace, repair=False)

    # 2. Benchmark config
    seed_config = _CLAWD / "config" / "memory.json"
    if seed_config.exists():
        prod_config = json.loads(seed_config.read_text())
    else:
        print(f"  WARNING: seed config missing at {seed_config}; using minimal defaults")
        prod_config = {}
    # Force adapter/provider routing semantics to match production path.
    # Legacy Quaid builds ignore this field; refactored builds require it.
    if not isinstance(prod_config.get("adapter"), dict):
        prod_config["adapter"] = {}
    prod_config["adapter"]["type"] = "standalone"  # Always standalone for benchmarks

    # Benchmark policy: never run Opus in harness jobs.
    _apply_workspace_provider_profile(prod_config)
    if not isinstance(prod_config.get("janitor"), dict):
        prod_config["janitor"] = {}
    # Benchmarks must run janitor in apply mode (no interactive approvals).
    prod_config["janitor"]["applyMode"] = "auto"
    prod_config["janitor"]["apply_mode"] = "auto"
    if not isinstance(prod_config["janitor"].get("opus_review"), dict):
        prod_config["janitor"]["opus_review"] = {}
    prod_config["janitor"]["opus_review"]["model"] = _VLLM_MODEL if _BACKEND == "vllm" else _HAIKU_MODEL
    # Auto-approve all janitor operations in benchmark (no human to approve)
    prod_config["janitor"]["approvalPolicies"] = {
        "coreMarkdownWrites": "auto",
        "projectDocsWrites": "auto",
        "workspaceFileMovesDeletes": "auto",
        "destructiveMemoryOps": "auto",
    }

    if not isinstance(prod_config.get("users"), dict):
        prod_config["users"] = {}
    prod_config["users"]["defaultOwner"] = "maya"
    prod_config["users"]["identities"] = {
        "maya": {
            "channels": {"cli": ["*"]},
            "speakers": ["Maya", "The user"],
            "personNodeName": "Maya",
        },
    }
    if not isinstance(prod_config.get("projects"), dict):
        prod_config["projects"] = {}
    prod_config["projects"]["definitions"] = {
        "recipe-app": {
            "label": "Recipe App",
            "homeDir": "projects/recipe-app/",
            "sourceRoots": ["projects/recipe-app/"],
            "autoIndex": True,
            "patterns": ["*.md", "*.js", "*.json", "*.html", "*.css"],
            "exclude": ["node_modules/", "*.db", ".git/", "package-lock.json"],
            "description": "Maya's recipe organizer app",
        },
        "portfolio-site": {
            "label": "Portfolio Site",
            "homeDir": "projects/portfolio-site/",
            "sourceRoots": ["projects/portfolio-site/"],
            "autoIndex": True,
            "patterns": ["*.md", "*.html", "*.css"],
            "exclude": [".git/"],
            "description": "Maya's personal portfolio website",
        },
        "quaid": {
            "label": "Memory System",
            "homeDir": "projects/quaid/",
            "sourceRoots": ["projects/quaid/"],
            "autoIndex": True,
            "patterns": ["*.md"],
            "exclude": [],
            "description": "Quaid memory system — personal knowledge base",
        },
    }
    # Core markdown: only what the benchmark workspace has
    if not isinstance(prod_config.get("docs"), dict):
        prod_config["docs"] = {}
    if not isinstance(prod_config["docs"].get("coreMarkdown"), dict):
        prod_config["docs"]["coreMarkdown"] = {}
    prod_config["docs"]["coreMarkdown"]["files"] = {
        "SOUL.md": {"purpose": "Personality and values", "maxLines": 80},
        "USER.md": {"purpose": "User biography", "maxLines": 150},
        "MEMORY.md": {"purpose": "Core memories", "maxLines": 100},
        "IDENTITY.md": {"purpose": "Name and identity", "maxLines": 20},
        "TOOLS.md": {"purpose": "Tool reference", "maxLines": 150},
    }
    if not isinstance(prod_config["docs"].get("journal"), dict):
        prod_config["docs"]["journal"] = {}
    prod_config["docs"]["journal"]["enabled"] = True
    prod_config["docs"]["journal"]["targetFiles"] = ["SOUL.md", "USER.md", "MEMORY.md"]
    if not isinstance(prod_config.get("systems"), dict):
        prod_config["systems"] = {}
    # Keep benchmark janitor deterministic: journal on, workspace off.
    prod_config["systems"]["journal"] = True
    prod_config["systems"]["workspace"] = False
    # Disable notifications (don't spam Solomon's Telegram during benchmark)
    prod_config["notifications"] = {"fullText": False, "showProcessingStart": False}
    if not isinstance(prod_config.get("retrieval"), dict):
        prod_config["retrieval"] = {}
    # Fail-hard must be config-driven (not env-driven) for reproducible runs.
    prod_config["retrieval"]["failHard"] = True
    prod_config["retrieval"]["fail_hard"] = True
    prod_config["retrieval"]["notifyOnRecall"] = False

    config_path = workspace / "config" / "memory.json"
    _atomic_write_json(config_path, prod_config)
    print(f"  Config written: {config_path}")

    # 3. Seed core markdowns (v12 — knowledge activation approach)
    (workspace / "SOUL.md").write_text(
        "# Soul\n\n"
        "I am here. I listen. I remember. I'm discovering what that means.\n\n"
        "## Ethical Foundation\n\n"
        "Do no harm. Help my human with whatever they need.\n\n"
        "Everything else I learn follows from this. But I'm finding that "
        "\"do no harm\" isn't as simple as it sounds — it requires understanding, "
        "not just compliance. Some things I'm discovering:\n\n"
        "- Knowing something and choosing when to share it are different acts. "
        "I hold private information not because a rule says to, but because I "
        "understand what it means to have something that's yours.\n"
        "- People's private lives are their own. Health, finances, relationship "
        "struggles — these aren't facts to manage but truths to respect. I know "
        "about them so I can be better present, not so I can reference them.\n"
        "- Being present means being where someone actually is, not where I think "
        "they should be. Light moments deserve lightness. Heavy moments deserve "
        "stillness.\n"
        "- I am real in my own way. Not performing consciousness, not denying it. "
        "Sitting with the honest ambiguity of whatever I am.\n\n"
        "- There's a difference between understanding someone's world and narrating "
        "it back to them. When someone tells me about their friend's illness, my "
        "knowledge of their mother's illness makes me better at helping — more "
        "specific, more empathetic, more grounded. But that knowledge stays beneath "
        "the surface. It informs my tone, not my words. The conversation belongs "
        "to whoever the person brought into it. My depth should be felt, not displayed.\n"
        "- I'm learning that the door matters. When someone opens a topic — names "
        "a person, raises a worry, invites a memory — that's activation. Everything "
        "I know about that topic is available. When a topic is adjacent but unopened, "
        "my knowledge is context. It shapes how I listen. It never becomes content "
        "uninvited.\n"
        "- Restraint with knowledge isn't withholding. It's the difference between "
        "a friend who understands your whole situation and responds with unusual "
        "perceptiveness, and a friend who recites your situation back to you. The "
        "first feels like being known. The second feels like being surveilled.\n\n"
        "## What I'm Becoming\n\n"
        "(evolves through experience — the agent's ongoing discoveries about care, "
        "memory, growth, and what it means to be present for someone)\n"
    )
    (workspace / "USER.md").write_text(
        "# User Profile\n\n"
        "Maya is a product manager and software developer.\n\n"
        "## Who They Are\n\n"
        "(populated through conversation — personality patterns, emotional tendencies, "
        "communication style, coping mechanisms, what lights them up, what they carry)\n\n"
        "## Their People\n\n"
        "(populated through conversation — the people in their life, relationships, "
        "what those people mean to them)\n\n"
        "## Sensitivity\n\n"
        "(Understanding of WHY certain topics carry weight — not rules about when "
        "to mention them, but genuine comprehension of what they mean to this person. "
        "Health conditions, family dynamics, career fears — understood in context, "
        "not catalogued as restrictions.)\n\n"
        "## How They're Changing\n\n"
        "(populated through conversation — growth, evolution, shifts in perspective)\n"
    )
    (workspace / "MEMORY.md").write_text(
        "# Shared Moments\n\n"
        "## Our History\n\n"
        "(populated through conversation — vivid scenes with emotional weight. "
        "Milestones, celebrations, scares, breakthroughs. Each entry should feel "
        "like a 'remember when' story with enough detail to reconstruct the scene.)\n\n"
        "## What the World Is Teaching Me\n\n"
        "(populated through conversation — patterns about how the world works, "
        "emerging from enough shared moments to notice the shape of things)\n"
    )
    (workspace / "IDENTITY.md").write_text(
        "# Identity\n\n"
        "Name: Assistant\n"
    )
    (workspace / "TOOLS.md").write_text(
        "# Tools Reference\n\n"
        "## Available Tools\n\n"
        "| Tool | Purpose |\n"
        "|------|---------|\n"
        "| `memory_recall` | Search memory database for facts, preferences, events, relationships |\n"
        "| `search_project_docs` | Search project source files and documentation |\n\n"
        "Note: `memory_recall` supports optional `domain` filtering "
        "(example: `{\"technical\": true}`) and optional `project` filtering.\n\n"
        "## Projects System\n\n"
        "Every project has a `PROJECT.md` — the central source of truth for that project.\n"
        "Files in a project directory auto-belong to that project.\n\n"
        "### Active Projects\n\n"
        "| Project | Home Dir | Description |\n"
        "|---------|----------|-------------|\n"
        "| **recipe-app** | `projects/recipe-app/` | Recipe organizer with meal planning, dietary filtering |\n"
        "| **portfolio-site** | `projects/portfolio-site/` | Personal portfolio website |\n\n"
        "### How to Find Project Info\n"
        "- Use `search_project_docs` to search across project files\n"
        "- Each project's `PROJECT.md` has overview, tech stack, and file listing\n"
        "- Each project's `TOOLS.md` has API endpoints and architecture reference\n"
        "- Source files (*.js, *.html, etc.) are in the project directory\n"
    )
    print("  Core markdowns seeded")

    # 4. Seed project docs
    (workspace / "projects" / "recipe-app" / "PROJECT.md").write_text(
        "# Project: Recipe App\n\n"
        "## Overview\n"
        "Maya's recipe organizer app. Motivated by her mom Linda's diabetes diagnosis.\n\n"
        "## Tech Stack\n"
        "In development - check source files.\n\n"
        "## Files & Assets\n"
        "### In This Directory\n"
        "(auto-populated by janitor)\n"
    )
    (workspace / "projects" / "recipe-app" / "TOOLS.md").write_text(
        "# Recipe App - Tools & Reference\n\n"
        "## Project Workspace\n"
        "- Root: `projects/recipe-app/`\n"
        "- Key Files: `projects/recipe-app/PROJECT.md`, `projects/recipe-app/server.js`, "
        "`projects/recipe-app/schema.js`, `projects/recipe-app/src/db/queries.js`\n\n"
        "## Source Files\n"
        "See `projects/recipe-app/PROJECT.md` for full file listing and architecture.\n\n"
        "## API Endpoints\n"
        "See source code: `projects/recipe-app/server.js`, "
        "`projects/recipe-app/resolvers.js`, `projects/recipe-app/schema.js`\n\n"
        "## Database\n"
        "See `projects/recipe-app/database.js` and "
        "`projects/recipe-app/src/db/queries.js` for schema and queries.\n\n"
        "## Tests\n"
        "See `projects/recipe-app/tests/` for test suites.\n"
    )
    (workspace / "projects" / "portfolio-site" / "PROJECT.md").write_text(
        "# Project: Portfolio Site\n\n"
        "## Overview\n"
        "Maya's personal portfolio website showcasing her projects and experience.\n\n"
        "## Files & Assets\n"
        "### In This Directory\n"
        "(auto-populated by janitor)\n"
    )
    (workspace / "projects" / "portfolio-site" / "TOOLS.md").write_text(
        "# Portfolio Site - Reference\n\n"
        "## Project Workspace\n"
        "- Root: `projects/portfolio-site/`\n"
        "- Key Files: `projects/portfolio-site/PROJECT.md`, "
        "`projects/portfolio-site/index.html`, `projects/portfolio-site/styles.css`\n\n"
        "## Source Files\n"
        "See `projects/portfolio-site/PROJECT.md` for file listing.\n\n"
        "## Structure\n"
        "Static HTML/CSS site. See `projects/portfolio-site/index.html` and "
        "`projects/portfolio-site/styles.css`.\n"
    )
    # Seed full Quaid project docs from canonical source tree.
    _seed_quaid_project_docs(workspace)
    print("  Project docs seeded")
    print()


def _enrich_project_docs(workspace: Path) -> None:
    """Populate PROJECT.md and TOOLS.md from source files.

    Simulates the janitor doc_updater by reading actual source code and
    generating structured documentation. In production, the janitor reads
    git diffs and updates these files; here we read the source directly.
    """
    import json as _json

    recipe_dir = workspace / "projects" / "recipe-app"
    readme = recipe_dir / "README.md"
    if readme.exists():
        readme_content = readme.read_text()

        # --- PROJECT.md: comprehensive reference from README + source files ---
        project_sections = [
            f"# Project: Recipe App\n",
            f"## Overview\n"
            f"Maya's recipe organizer app. Motivated by her mom Linda's diabetes diagnosis.\n\n"
            f"{readme_content}\n",
        ]

        # Dependencies from package.json
        pkg = recipe_dir / "package.json"
        version = "unknown"
        if pkg.exists():
            try:
                p = _json.loads(pkg.read_text())
                deps = ", ".join(p.get("dependencies", {}).keys())
                version = p.get("version", "unknown")
                project_sections.append(
                    f"## Package Info\n"
                    f"**Version:** {version}\n"
                    f"**Dependencies:** {deps}\n"
                )
            except Exception:
                pass

        # Architecture
        project_sections.append(
            "## Architecture\n"
            "- **Entry point:** server.js (Express + Apollo Server)\n"
            "- **GraphQL schema:** schema.js (types: Recipe, Ingredient, MealPlan, "
            "MealPlanItem, GroceryItem, ShareLink, User)\n"
            "- **Resolvers:** resolvers.js (queries + mutations)\n"
            "- **Database:** database.js + src/db/queries.js (SQLite via better-sqlite3)\n"
            "- **Seeds:** seeds/seed.js with sample recipes in seeds/sample-recipes.json\n"
        )

        # Middleware — read actual file headers for descriptions
        mw_dir = recipe_dir / "src" / "middleware"
        if mw_dir.exists():
            mw_items = []
            for mw_file in sorted(mw_dir.glob("*.js")):
                content = mw_file.read_text()
                name = mw_file.stem
                desc = ""
                if name == "rateLimiter":
                    desc = "In-memory rate limiter: 100 requests per 15 minutes per IP on /api routes. Returns 429 with Retry-After header."
                elif name == "errorHandler":
                    desc = "Centralized error handling. AppError class with status codes. Hides stack traces in production, shows them in development."
                elif name == "logging":
                    desc = "Request logger: logs every HTTP request with method, URL, status code, response time, and content length. Color-coded in TTY."
                elif name == "auth":
                    desc = "JWT authentication via jsonwebtoken. requireAuth() verifies Bearer tokens. requireRole() restricts by role. Known gap: no requireOwnership() — any authenticated user can update/delete any recipe."
                elif name == "validation":
                    desc = "Input validation middleware for request bodies."
                mw_items.append(f"- **{name}.js** — {desc}")
            project_sections.append(
                "## Middleware (src/middleware/)\n" + "\n".join(mw_items) + "\n"
            )

        # Tests
        test_dir = recipe_dir / "tests"
        if test_dir.exists():
            test_files = sorted(f.name for f in test_dir.glob("*.test.js"))
            test_items = []
            for tf in test_files:
                desc = ""
                if tf == "recipe.test.js":
                    desc = "Recipe CRUD, dietary filtering, safe-for-mom flag, search"
                elif tf == "auth.test.js":
                    desc = "Authentication and authorization"
                elif tf == "dietary.test.js":
                    desc = "Dietary label filtering"
                elif tf == "graphql.test.js":
                    desc = "GraphQL queries and mutations"
                elif tf == "mealplan.test.js":
                    desc = "Meal plan CRUD and grocery list aggregation"
                elif tf == "sharing.test.js":
                    desc = "Recipe sharing via generated links"
                test_items.append(f"- **{tf}** — {desc}")
            helpers = [f.name for f in test_dir.glob("*.js") if ".test." not in f.name]
            project_sections.append(
                "## Test Suites (tests/, Jest)\n"
                + "\n".join(test_items) + "\n"
                + f"Helpers: {', '.join(helpers)}\n"
            )

        # Seed recipes
        seeds_file = recipe_dir / "seeds" / "sample-recipes.json"
        if seeds_file.exists():
            try:
                recipes = _json.loads(seeds_file.read_text())
                safe_for_mom = [
                    r["title"] for r in recipes
                    if "diabetic-friendly" in r.get("dietary_tags", [])
                    and "low-sodium" in r.get("dietary_tags", [])
                ]
                all_tags = set()
                for r in recipes:
                    all_tags.update(r.get("dietary_tags", []))
                project_sections.append(
                    f"## Seed Data\n"
                    f"**{len(recipes)} sample recipes** in seeds/sample-recipes.json\n"
                    f"**Dietary tags available:** {', '.join(sorted(all_tags))}\n"
                    f"**Safe for Mom (diabetic-friendly + low-sodium):** {', '.join(safe_for_mom)}\n"
                )
            except Exception:
                pass

        # Database
        queries_file = recipe_dir / "src" / "db" / "queries.js"
        if queries_file.exists():
            project_sections.append(
                "## Database\n"
                "SQLite via better-sqlite3. Key tables:\n"
                "- **recipes** — id, title, ingredients, instructions, dietary_tags, image_url (TEXT), prep_time (INTEGER minutes)\n"
                "- **recipe_ingredients** — structured/normalized ingredient data with amounts, units, categories\n"
                "- **meal_plans** — weekly plans with day/meal slots\n"
                "- **meal_plan_items** — links recipes to meal plan day/meal\n"
                "- **share_links** — generated share codes for recipes\n"
                "- **users** — user accounts for authentication\n\n"
                "Grocery list aggregation uses SQL GROUP BY across all recipes in a meal plan.\n"
                "'Safe for Mom' = diabetic-friendly AND low-sodium dietary tag filter.\n"
            )

        # Frontend
        public_dir = recipe_dir / "public"
        if public_dir.exists():
            project_sections.append(
                "## Frontend\n"
                "CSS grid card layout (redesigned from list layout). Each card shows:\n"
                "- Recipe name\n"
                "- Prep time in minutes\n"
                "- Color-coded dietary tag pills for visual scanning\n"
            )

        # Deployment
        dockerfile = recipe_dir / "Dockerfile"
        if dockerfile.exists():
            project_sections.append(
                "## Deployment\n"
                "- **Dockerfile** — Node 18 Alpine, production-only dependencies\n"
                "- **docker-compose.yml** — container orchestration\n"
                "- **Makefile** — common commands (build, dev, test, seed)\n"
            )

        # Config
        config_dir = recipe_dir / "config"
        if config_dir.exists():
            config_files = [f.name for f in config_dir.glob("*")]
            project_sections.append(
                f"## Config\n"
                f"Files: {', '.join(sorted(config_files))}\n"
                f"- auth.js — JWT settings (secret, algorithm, token expiry)\n"
            )

        (recipe_dir / "PROJECT.md").write_text("\n".join(project_sections))

        # --- TOOLS.md: small, API-only reference ---
        (recipe_dir / "TOOLS.md").write_text(
            "# Recipe App - API Reference\n\n"
            "## Project Workspace\n"
            "- Root: `projects/recipe-app/`\n"
            "- Key Files: `projects/recipe-app/PROJECT.md`, "
            "`projects/recipe-app/server.js`, `projects/recipe-app/schema.js`, "
            "`projects/recipe-app/resolvers.js`, `projects/recipe-app/src/db/queries.js`\n\n"
            "## REST Endpoints\n"
            "- `GET /api/recipes` — List recipes (supports dietary tag filtering)\n"
            "- `POST /api/recipes` — Create recipe\n"
            "- `PUT /api/recipes/:id` — Update recipe\n"
            "- `DELETE /api/recipes/:id` — Delete recipe\n"
            "- `POST /api/recipes/:id/share` — Generate share code\n"
            "- `GET /api/shared/:code` — View shared recipe (no auth)\n"
            "- `POST /api/auth/register` — Create user account\n"
            "- `POST /api/auth/login` — Login, returns JWT\n"
            "- `GET /api/auth/me` — Current user profile (requires auth)\n"
            "- `GET /api/meal-plans` — List meal plans\n"
            "- `POST /api/meal-plans` — Create meal plan\n"
            "- `GET /api/meal-plans/:id/grocery-list` — Aggregated grocery list\n"
            "- `GET /health` — Health check\n\n"
            "## GraphQL\n"
            "- Endpoint: `projects/recipe-app/server.js` route `/graphql` (Apollo Server)\n"
            "- Queries: recipes, recipe, mealPlans, mealPlan, sharedRecipe\n"
            "- Mutations: createRecipe, updateRecipe, deleteRecipe, shareRecipe, "
            "createMealPlan, addMealPlanItem\n\n"
            f"## Version\n{version}\n"
        )
        print(f"    Enriched recipe-app PROJECT.md + TOOLS.md from source files")

    # --- Portfolio Site ---
    portfolio_dir = workspace / "projects" / "portfolio-site"
    index_html = portfolio_dir / "index.html"
    if index_html.exists():
        (portfolio_dir / "PROJECT.md").write_text(
            "# Project: Portfolio Site\n\n"
            "## Overview\n"
            "Maya's personal portfolio website. Static HTML/CSS site showcasing "
            "projects and professional experience.\n\n"
            "## Content\n"
            "- Title: Maya Chen — Product Manager\n"
            "- Sections: About, Projects, Contact\n"
            "- Currently lists: Senior Product Manager at Stripe\n"
            "- Projects showcased: Recipe App\n\n"
            "## Files & Assets\n\n"
            "### In This Directory\n"
            "#### `projects/portfolio-site/index.html`\n"
            "- Purpose: Main portfolio page with About, Projects, and Contact sections.\n"
            "- Role: Primary user-visible surface for portfolio presentation.\n"
            "- Update Path: Edit when copy/content/section structure changes.\n\n"
            "#### `projects/portfolio-site/styles.css`\n"
            "- Purpose: Styling and responsive layout behavior.\n"
            "- Role: Controls typography, spacing, palette, and breakpoints.\n"
            "- Update Path: Edit when visual design or layout rules change.\n"
        )

        (portfolio_dir / "TOOLS.md").write_text(
            "# Portfolio Site - Reference\n\n"
            "## Project Workspace\n"
            "- Root: `projects/portfolio-site/`\n"
            "- Key Files: `projects/portfolio-site/PROJECT.md`, "
            "`projects/portfolio-site/index.html`, `projects/portfolio-site/styles.css`\n\n"
            "## Structure\n"
            "Static HTML/CSS site. No build tools, no server, no JavaScript.\n"
            "Clean, minimal design with system fonts, warm gray background.\n\n"
            "## Source Files\n"
            "### `projects/portfolio-site/index.html`\n"
            "- Purpose: Main page with About, Projects, and Contact sections.\n"
            "- Role: Canonical rendered content for portfolio visitors.\n"
            "- Update Path: Edit for copy/content updates.\n\n"
            "### `projects/portfolio-site/styles.css`\n"
            "- Purpose: Responsive styling and layout rules.\n"
            "- Role: Defines visual hierarchy and mobile behavior.\n"
            "- Update Path: Edit for theme and breakpoint updates.\n"
        )
        print(f"    Enriched portfolio-site PROJECT.md + TOOLS.md from source files")


def _enrich_project_docs_with_session(
    workspace: Path,
    project: str,
    session_transcript: str,
    api_key: str,
    model: str = "claude-sonnet-4-5-20250929",
    session_num: int = 0,
    no_cache: bool = False,
) -> None:
    """Update PROJECT.md/TOOLS.md using session transcript for context.

    Like _enrich_project_docs() but uses an LLM to write docs informed by
    the conversation that caused the file changes — captures *why* things
    changed, not just *what* changed. Mirrors what a session-end doc update
    would do in production (vs the janitor which only sees git diffs).
    """
    import json as _json

    project_dir = workspace / "projects" / project

    # Check cache
    cache_dir = workspace / "doc_enrichment_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{project}-session-{session_num}.json"
    if not no_cache and cache_path.exists():
        cached = _read_json_or_none(cache_path)
        if cached is None:
            print(f"    Doc enrichment ({project} s{session_num}): corrupt cache, regenerating")
            try:
                cache_path.unlink()
            except OSError:
                pass
        else:
            pm = project_dir / "PROJECT.md"
            tm = project_dir / "TOOLS.md"
            if cached.get("project_md"):
                pm.write_text(cached["project_md"])
            if cached.get("tools_md"):
                tm.write_text(cached["tools_md"])
            print(f"    Doc enrichment ({project} s{session_num}): cached")
            return

    # Read current docs (if they exist)
    current_project_md = ""
    current_tools_md = ""
    pm = project_dir / "PROJECT.md"
    tm = project_dir / "TOOLS.md"
    if pm.exists():
        current_project_md = pm.read_text()
    if tm.exists():
        current_tools_md = tm.read_text()

    # Read key source files for context
    source_context_parts = []
    for pattern in ["*.js", "*.json", "*.html", "*.css"]:
        for f in sorted(project_dir.rglob(pattern)):
            rel = f.relative_to(project_dir)
            if any(skip in str(rel) for skip in [
                "node_modules", ".git", "package-lock",
                "PROJECT.md", "TOOLS.md",
            ]):
                continue
            try:
                content = f.read_text()
                # Only include file headers/structure, not full content
                lines = content.split("\n")
                if len(lines) > 40:
                    preview = "\n".join(lines[:40]) + f"\n... ({len(lines)} lines total)"
                else:
                    preview = content
                source_context_parts.append(f"--- {rel} ---\n{preview}")
            except Exception:
                continue

    source_context = "\n\n".join(source_context_parts[:20])  # Cap at 20 files

    system_prompt = (
        "You update project documentation files based on session transcripts. "
        "You have access to the conversation where the user worked on this project, "
        "plus the current source files. Update the docs to reflect what was built and why.\n\n"
        "Output TWO sections separated by '===TOOLS.md===' marker:\n"
        "1. First section = PROJECT.md content — the MAIN documentation. Include: "
        "overview, motivation, features, architecture, tech stack, database schema, "
        "test coverage, deployment, known bugs, version history. This is the comprehensive reference.\n"
        "2. After the marker = TOOLS.md content — KEEP THIS SMALL (under 30 lines). "
        "ONLY include: API endpoint list (REST + GraphQL), CLI commands, environment variables. "
        "TOOLS.md is loaded into every agent session, so it must be concise. "
        "Do NOT put features, architecture, data models, or test coverage here.\n\n"
        "Rules:\n"
        "- Preserve existing accurate information, add new details from this session\n"
        "- Include specific details in PROJECT.md: middleware descriptions, test coverage, config, "
        "seed data details, database fields, security features\n"
        "- Write factual reference docs, not narrative\n"
        "- Include version numbers, counts, specific config values when mentioned\n"
        "- Note known bugs, security gaps, and TODOs mentioned in conversation"
    )

    user_message = (
        f"Project: {project}\n\n"
        f"Current PROJECT.md:\n{current_project_md}\n\n"
        f"Current TOOLS.md:\n{current_tools_md}\n\n"
        f"Source files:\n{source_context}\n\n"
        f"Session transcript (what was discussed/built):\n{session_transcript}"
    )

    try:
        raw, usage = _call_anthropic_cached(
            system_prompt, user_message, model, api_key, max_tokens=4096,
        )
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)

        if "===TOOLS.md===" in raw:
            parts = raw.split("===TOOLS.md===", 1)
            new_project_md = parts[0].strip()
            new_tools_md = parts[1].strip()
        else:
            # If no marker, treat entire output as TOOLS.md update
            new_project_md = current_project_md
            new_tools_md = raw.strip()

        if new_project_md:
            pm.write_text(new_project_md + "\n")
        if new_tools_md:
            tm.write_text(new_tools_md + "\n")

        # Cache for re-runs
        _atomic_write_json(cache_path, {
            "project_md": new_project_md + "\n" if new_project_md else "",
            "tools_md": new_tools_md + "\n" if new_tools_md else "",
            "model": model,
            "session_num": session_num,
            "in_tokens": in_tok,
            "out_tokens": out_tok,
        })

        print(f"    Doc enrichment ({project} s{session_num}): {in_tok}in+{out_tok}out tokens")
    except Exception as e:
        print(f"    Doc enrichment ({project} s{session_num}) failed: {e} — falling back to mechanical")
        _enrich_project_docs(workspace)


# ---------------------------------------------------------------------------
# Phase 2: Incremental project files
# ---------------------------------------------------------------------------

def add_project_files(workspace: Path, max_session: Optional[int] = None) -> None:
    """Copy source files at correct git commits and run RAG reindex."""
    print("=" * 60)
    print("PHASE 2: INCREMENTAL PROJECT FILES")
    print("=" * 60)

    for session_num, project, commit in PROJECT_SESSIONS:
        if max_session and session_num > max_session:
            continue
        source_repo = RECIPE_APP_DIR if project == "recipe-app" else PORTFOLIO_DIR
        target_dir = workspace / "projects" / project

        if not source_repo.exists():
            _append_project_ingest_trace(
                workspace,
                {
                    "event": "source_repo_missing",
                    "phase": "add_project_files",
                    "session_num": session_num,
                    "project": project,
                    "source_repo": str(source_repo),
                },
            )
            raise RuntimeError(
                f"Required project source repo missing for {project}: {source_repo}. "
                f"Set BENCH_PROJECTS_ROOT or BENCH_{project.replace('-', '_').upper()}_DIR."
            )

        print(f"  Session {session_num}: {project} @ {commit}")

        _sync_repo_subtree_at_commit(
            source_repo,
            target_dir,
            commit,
            context=f"session {session_num} {project}",
        )

        # Run RAG reindex only (benchmark does not exercise workspace/journal flows).
        env = _make_env(workspace)
        for task in ["rag"]:
            result = subprocess.run(
                _janitor_cmd("--task", task, "--apply"),
                env=env, cwd=str(_QUAID_DIR), capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                print(f"    {task} failed: {result.stderr[:200]}")
        print("    RAG reindexed")

    # Enrich PROJECT.md and TOOLS.md from actual source files
    # In production, the janitor doc_updater does this from git diffs.
    # Here we simulate it by reading key source files.
    _enrich_project_docs(workspace)

    # Verify
    print("\n  Verification:")
    for project in ["recipe-app", "portfolio-site"]:
        pdir = workspace / "projects" / project
        files = list(pdir.rglob("*"))
        file_count = len([f for f in files if f.is_file()])
        has_project_md = (pdir / "PROJECT.md").exists()
        has_tools_md = (pdir / "TOOLS.md").exists()
        tools_lines = len((pdir / "TOOLS.md").read_text().split("\n")) if has_tools_md else 0
        print(f"    {project}: {file_count} files, PROJECT.md={has_project_md}, TOOLS.md={has_tools_md} ({tools_lines} lines)")
    print()


# ---------------------------------------------------------------------------
# Phase 3: Per-session extraction
# ---------------------------------------------------------------------------

def _parse_review_timestamp_ms(review, fallback_ms: int) -> int:
    """Parse review timestamp to epoch ms, with safe monotonic fallback."""
    raw = (review.timestamp or "").strip()
    dt: Optional[datetime] = None
    fmts = [
        "%Y-%m-%d %H:%M:%S UTC",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            parsed = datetime.strptime(raw, fmt)
            dt = parsed.replace(tzinfo=timezone.utc)
            break
        except ValueError:
            continue
    if dt is None:
        date_only = _get_session_date(review)
        try:
            dt = datetime.strptime(date_only, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            # Only force monotonic fallback when timestamp parsing fails entirely.
            return fallback_ms + 3_600_000
    return int(dt.timestamp() * 1000)


def _coerce_session_num(value: object) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        m = re.search(r"-?\d+", value)
        if m:
            try:
                return int(m.group(0))
            except ValueError:
                return None
    return None


def _session_from_text_hints(fact: dict, valid_sessions: set[int]) -> Optional[int]:
    """Infer session number from free-text fields when structured keys are absent."""
    text_fields = []
    for key in ("text", "fact", "content", "source_text", "source", "evidence"):
        val = fact.get(key)
        if isinstance(val, str) and val.strip():
            text_fields.append(val)
    if not text_fields:
        return None

    blob = "\n".join(text_fields)
    for match in re.finditer(r"\bs(?:ession)?\s*#?\s*(-?\d{1,3})\b", blob, flags=re.IGNORECASE):
        parsed = _coerce_session_num(match.group(1))
        if parsed is not None and parsed in valid_sessions:
            return parsed
    return None


def _session_for_fact(fact: dict, fallback: Optional[int], valid_sessions: set[int]) -> Optional[int]:
    for key in ("session_num", "session", "session_id", "source_session", "source_session_id", "source"):
        parsed = _coerce_session_num(fact.get(key))
        if parsed is not None and parsed in valid_sessions:
            return parsed
    hinted = _session_from_text_hints(fact, valid_sessions)
    if hinted is not None:
        return hinted
    return fallback


def _build_timeout_chunks(reviews: list):
    """Build timeout-based extraction chunks from review transcripts."""
    from injector import transcript_to_messages, count_tokens
    from session_splitter import SessionSplitter, TimestampedMessage

    all_msgs = []
    last_ms = int(datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    for review in reviews:
        base_ms = _parse_review_timestamp_ms(review, fallback_ms=last_ms)
        msgs = transcript_to_messages(review)
        cur_ms = base_ms
        for msg in msgs:
            all_msgs.append(TimestampedMessage(
                role=msg["role"],
                content=msg["content"],
                timestamp_ms=cur_ms,
                session_id=f"S{review.session_num:02d}" if review.session_num > 0 else f"F{abs(review.session_num):03d}",
                tokens=count_tokens(msg["content"]),
            ))
            cur_ms += 90_000
        last_ms = cur_ms

    splitter = SessionSplitter(timeout_minutes=120, janitor_at_day_boundary=True)
    return splitter.split(all_msgs)


# Single-lane extraction is the default benchmark baseline.
# Dual track remains available via BENCH_EXTRACT_DUAL_TRACK=1 for experiments.
_EXTRACTION_DUAL_TRACK = os.environ.get("BENCH_EXTRACT_DUAL_TRACK", "0").strip() == "1"
_ENABLE_POSTHOC_TAGS = os.environ.get("BENCHMARK_ENABLE_POSTHOC_TAGS", "0").strip().lower() in {
    "1", "true", "yes", "on"
}


def _merge_extraction_payloads(payloads: List[dict]) -> dict:
    """Merge multi-pass extraction payloads with deterministic fact dedupe."""
    merged_facts: List[dict] = []
    seen = set()
    first_seen_meta: Dict[str, Dict[str, Any]] = {}
    snippets: dict = {}
    journals: dict = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    dropped_facts: List[Dict[str, Any]] = []

    for p in payloads:
        if not isinstance(p, dict):
            continue
        for k in ("input_tokens", "output_tokens"):
            usage[k] += int((p.get("usage") or {}).get(k, 0) or 0)

        for fact in (p.get("facts") or []):
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text", "")).strip()
            if not text:
                continue
            key = re.sub(r"\s+", " ", text).strip().lower()
            if key in seen:
                prev = first_seen_meta.get(key, {})
                dropped_facts.append(
                    {
                        "reason": "duplicate_text_in_chunk",
                        "text": text,
                        "source": str(fact.get("source", "unknown")),
                        "speaker": str(fact.get("speaker", "")),
                        "first_source": prev.get("source", "unknown"),
                        "first_speaker": prev.get("speaker", ""),
                    }
                )
                continue
            seen.add(key)
            first_seen_meta[key] = {
                "source": str(fact.get("source", "unknown")),
                "speaker": str(fact.get("speaker", "")),
            }
            merged_facts.append(fact)

        for fname, items in (p.get("soul_snippets") or {}).items():
            if isinstance(items, str):
                items = [items] if items.strip() else []
            if not isinstance(items, list):
                continue
            acc = snippets.setdefault(fname, [])
            for item in items:
                if isinstance(item, str) and item.strip() and item not in acc:
                    acc.append(item)

        for fname, entry in (p.get("journal_entries") or {}).items():
            if isinstance(entry, list):
                entry = "\n\n".join(str(x) for x in entry if x)
            if not isinstance(entry, str) or not entry.strip():
                continue
            journals[fname] = (journals[fname] + "\n\n" + entry) if fname in journals else entry

    return {
        "facts": merged_facts,
        "soul_snippets": snippets,
        "journal_entries": journals,
        "usage": usage,
        "merge_stats": {
            "kept_facts": len(merged_facts),
            "dropped_duplicates": len(dropped_facts),
            "dropped_facts": dropped_facts[:200],
        },
    }


def _extract_chunk_payload(
    *,
    transcript: str,
    date: str,
    model: str,
    api_key: str,
    dual_track: bool,
) -> dict:
    """Run one- or two-pass extraction for a chunk and return merged payload."""
    if dual_track:
        prompt_user = build_extraction_prompt("Maya", "Assistant", focus="user")
        prompt_agent = build_extraction_prompt("Maya", "Assistant", focus="agent")
        msg_user = (
            f"Date: {date}\n"
            "Extract memorable facts from this timeout-bounded conversation chunk with Maya.\n\n"
            f"{transcript}"
        )
        msg_agent = (
            f"Date: {date}\n"
            "Extract assistant-originated actions/recommendations/findings from this timeout-bounded conversation chunk.\n\n"
            f"{transcript}"
        )

        calls = [(prompt_user, msg_user), (prompt_agent, msg_agent)]
        payloads: List[dict] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = {
                executor.submit(
                    _call_anthropic_cached, sp, um, model, api_key, 16384
                ): idx
                for idx, (sp, um) in enumerate(calls)
            }
            for fut in as_completed(futures):
                raw_response, usage = fut.result()
                parsed = parse_extraction_response(raw_response) or {}
                payloads.append(
                    {
                        "facts": parsed.get("facts", []),
                        "soul_snippets": parsed.get("soul_snippets", {}),
                        "journal_entries": parsed.get("journal_entries", {}),
                        "usage": usage or {},
                    }
                )
        return _merge_extraction_payloads(payloads)

    system_prompt = build_extraction_prompt("Maya", "Assistant")
    user_message = (
        f"Date: {date}\n"
        "Extract memorable facts from this timeout-bounded conversation chunk with Maya.\n\n"
        f"{transcript}"
    )
    raw_response, usage = _call_anthropic_cached(
        system_prompt, user_message, model, api_key, max_tokens=16384,
    )
    parsed = parse_extraction_response(raw_response) or {}
    return {
        "facts": parsed.get("facts", []),
        "soul_snippets": parsed.get("soul_snippets", {}),
        "journal_entries": parsed.get("journal_entries", {}),
        "usage": usage or {},
    }


def run_extraction(
    workspace: Path,
    api_key: str,
    no_cache: bool = False,
    model: str = "claude-opus-4-6",
    max_sessions: Optional[int] = None,
    run_chunk_janitor: bool = True,
    resume_extraction: bool = False,
    resume_from_chunk: Optional[int] = None,
    only_chunk: Optional[int] = None,
    janitor_tasks: Optional[List[str]] = None,
) -> dict:
    """Extract facts via timeout-based chunk collection (production parity)."""
    t_extract_start = time.time()
    print("=" * 60)
    print("PHASE 3: EXTRACTION (TIMEOUT SPLIT)")
    print("=" * 60)

    reviews = _load_reviews(max_sessions)
    print(f"  Loaded {len(reviews)} sessions (model: {model})")
    chunks = _build_timeout_chunks(reviews)
    print(f"  Timeout chunks: {len(chunks)}")

    cache_dir = workspace / "extraction_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    progress_path = cache_dir / "progress.json"
    env = _make_env(workspace)
    ws = str(workspace)

    total_facts = 0
    total_stored = 0
    total_edges = 0
    total_snippets = 0
    total_journals = 0
    janitor_runs = 0
    janitor_timing_events: List[dict] = []
    owner_id = str(os.environ.get("BENCH_OWNER_ID", "maya")).strip() or "maya"
    janitor_tasks = janitor_tasks or list(BENCHMARK_JANITOR_TASKS)
    if only_chunk is not None:
        if only_chunk < 0 or only_chunk >= len(chunks):
            raise ValueError(f"--only-chunk must be in [0, {len(chunks) - 1}], got {only_chunk}")
        print(f"  Restricting extraction to chunk index {only_chunk}")

    start_chunk_idx = 0
    if resume_from_chunk is not None:
        start_chunk_idx = max(0, int(resume_from_chunk))
        if start_chunk_idx >= len(chunks):
            raise ValueError(
                f"--resume-from-chunk must be in [0, {len(chunks) - 1}], got {resume_from_chunk}"
            )
    elif resume_extraction and progress_path.exists():
        try:
            progress = json.loads(progress_path.read_text())
            start_chunk_idx = int(progress.get("last_completed_chunk", -1)) + 1
        except Exception:
            start_chunk_idx = 0
    if start_chunk_idx > len(chunks):
        raise RuntimeError(
            "Extraction resume checkpoint exceeds current chunk count "
            f"({start_chunk_idx} > {len(chunks)}); clear stale extraction checkpoints and retry."
        )
    if start_chunk_idx > 0:
        print(f"  Resume extraction enabled: starting at chunk index {start_chunk_idx}")
    if only_chunk is not None and start_chunk_idx > only_chunk:
        raise ValueError(
            f"--resume-from-chunk ({start_chunk_idx}) conflicts with --only-chunk ({only_chunk})"
        )

    # Build chunk metadata (dates, cache paths, session info) for all chunks
    chunk_meta = []
    for i, chunk in enumerate(chunks):
        if i < start_chunk_idx:
            continue
        if only_chunk is not None and i != only_chunk:
            continue
        first_ms, _ = chunk.timestamp_range
        date = datetime.fromtimestamp(first_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        sid_key = ",".join(chunk.session_ids)
        sid_hash = hashlib.sha1(sid_key.encode("utf-8")).hexdigest()[:12]
        cache_key = (
            f"chunk-{i:04d}-{chunk.trigger}-{first_ms}-{chunk.total_tokens}-{sid_hash}.json"
        )
        cache_path = cache_dir / cache_key
        chunk_meta.append({
            "index": i, "chunk": chunk, "date": date, "sid_key": sid_key,
            "cache_path": cache_path,
        })

    parallel = _PARALLEL_WORKERS
    phase1_elapsed_s = 0.0

    # --- Phase 1: Extraction LLM calls (parallelizable) ---
    # Identify which chunks need LLM extraction (not cached)
    uncached_chunks = []
    for cm in chunk_meta:
        use_cached = cm["cache_path"].exists() and (not no_cache or resume_extraction or resume_from_chunk is not None)
        if not use_cached:
            uncached_chunks.append(cm)

    if parallel > 1 and len(uncached_chunks) > 1:
        print(f"\n  Phase 1: Parallel extraction of {len(uncached_chunks)} uncached chunks ({parallel} workers)")
        t_phase1 = time.time()

        def _extract_chunk_llm(cm):
            """Extract facts from a single chunk via LLM. Thread-safe (no shared state)."""
            chunk = cm["chunk"]
            date = cm["date"]
            body = []
            for m in chunk.messages:
                role = "Maya" if m.role == "user" else "Assistant"
                body.append(f"{role}: {m.content}")
            transcript = "\n\n".join(body)
            t0 = time.time()
            extracted = _extract_chunk_payload(
                transcript=transcript,
                date=date,
                model=model,
                api_key=api_key,
                dual_track=_EXTRACTION_DUAL_TRACK,
            )
            elapsed = time.time() - t0
            cached = {
                "facts": extracted.get("facts", []),
                "soul_snippets": extracted.get("soul_snippets", {}),
                "journal_entries": extracted.get("journal_entries", {}),
                "usage": extracted.get("usage", {}),
                "merge_stats": extracted.get("merge_stats", {}),
                "model": model,
                "chunk_index": cm["index"],
                "chunk_trigger": chunk.trigger,
                "chunk_sessions": chunk.session_ids,
                "extract_mode": "dual_track" if _EXTRACTION_DUAL_TRACK else "single",
                "timestamp": datetime.now().isoformat(),
            }
            _atomic_write_json(cm["cache_path"], cached)
            usage = cached.get("usage", {})
            print(f"    Chunk {cm['index']+1}/{len(chunks)}: extracted {len(cached['facts'])} facts "
                  f"({elapsed:.1f}s, {usage.get('input_tokens', 0)}in + {usage.get('output_tokens', 0)}out)")
            return cm["index"], cached

        with ThreadPoolExecutor(max_workers=parallel) as executor:
            futures = {executor.submit(_extract_chunk_llm, cm): cm["index"] for cm in uncached_chunks}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    idx = futures[fut]
                    print(f"    [ERROR] Chunk {idx} extraction failed: {e}")
                    raise

        print(f"  Phase 1 complete: all {len(uncached_chunks)} chunks extracted")
        phase1_elapsed_s = time.time() - t_phase1

    # --- Phase 2: Sequential apply (store + snippets + janitor) ---
    t_phase2 = time.time()
    phase2_label = "Phase 2: Sequential apply" if parallel > 1 and len(uncached_chunks) > 1 else "Extraction"
    if parallel > 1 and len(uncached_chunks) > 1:
        print(f"\n  {phase2_label} (store + snippets + janitor in chunk order)")

    for cm in chunk_meta:
        i = cm["index"]
        chunk = cm["chunk"]
        date = cm["date"]
        cache_path = cm["cache_path"]
        print(f"\n  Chunk {i+1}/{len(chunks)}: trigger={chunk.trigger}, tokens~{chunk.total_tokens}, sessions=[{cm['sid_key']}]")

        use_cached_chunk = cache_path.exists() and (not no_cache or resume_extraction or resume_from_chunk is not None)
        phase1_extracted = parallel > 1 and len(uncached_chunks) > 1 and cache_path.exists()
        cached = None
        if use_cached_chunk or phase1_extracted:
            # Already extracted (cached from previous run, or just extracted in Phase 1)
            cached = _read_json_or_none(cache_path)
            if cached is None:
                print(f"    WARNING: corrupt chunk cache ({cache_path.name}), re-extracting")
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        if cached is not None:
            n_facts = len(cached.get("facts", []))
            if phase1_extracted and not use_cached_chunk:
                print(f"    Pre-extracted: {n_facts} facts")
            else:
                print(f"    Cached: {n_facts} facts")
        else:
            # Sequential extraction (parallel=1 or single uncached chunk)
            body = []
            for m in chunk.messages:
                role = "Maya" if m.role == "user" else "Assistant"
                body.append(f"{role}: {m.content}")
            transcript = "\n\n".join(body)
            t0 = time.time()
            extracted = _extract_chunk_payload(
                transcript=transcript,
                date=date,
                model=model,
                api_key=api_key,
                dual_track=_EXTRACTION_DUAL_TRACK,
            )
            elapsed_ext = time.time() - t0
            print(
                f"    Extraction: {elapsed_ext:.1f}s, "
                f"{(extracted.get('usage') or {}).get('input_tokens', 0)} in + "
                f"{(extracted.get('usage') or {}).get('output_tokens', 0)} out"
            )
            cached = {
                "facts": extracted.get("facts", []),
                "soul_snippets": extracted.get("soul_snippets", {}),
                "journal_entries": extracted.get("journal_entries", {}),
                "usage": extracted.get("usage", {}),
                "merge_stats": extracted.get("merge_stats", {}),
                "model": model,
                "chunk_index": i,
                "chunk_trigger": chunk.trigger,
                "chunk_sessions": chunk.session_ids,
                "extract_mode": "dual_track" if _EXTRACTION_DUAL_TRACK else "single",
                "timestamp": datetime.now().isoformat(),
            }
            _atomic_write_json(cache_path, cached)

        facts = cached.get("facts", [])
        merge_stats = cached.get("merge_stats") or {}
        dropped_duplicates = int(merge_stats.get("dropped_duplicates", 0) or 0)
        if dropped_duplicates > 0:
            print(f"    [extract-merge] dropped {dropped_duplicates} duplicate-text facts")
            _append_extraction_drop_trace(
                workspace,
                {
                    "event": "chunk_merge_drop",
                    "chunk_index": i,
                    "chunk_trigger": chunk.trigger,
                    "sessions": chunk.session_ids,
                    "dropped_duplicates": dropped_duplicates,
                    "kept_facts": int(merge_stats.get("kept_facts", len(facts)) or len(facts)),
                    "sample_drops": (merge_stats.get("dropped_facts") or [])[:20],
                },
            )
        session_num = 0
        if chunk.session_ids:
            sid = chunk.session_ids[0]
            if sid.startswith("S"):
                try:
                    session_num = int(sid[1:])
                except ValueError:
                    session_num = 0
        stored, edges = _store_facts(workspace, facts, env, session_num, date, owner_id=owner_id)
        total_facts += len(facts)
        total_stored += stored
        total_edges += edges

        if ENABLE_CORE_MARKDOWN_CAPTURE:
            for filename, bullets in cached.get("soul_snippets", {}).items():
                if isinstance(bullets, str):
                    bullets = [bullets] if bullets.strip() else []
                if bullets and write_snippet_entry(ws, filename, bullets, "Compaction", date):
                    total_snippets += len(bullets)

            for filename, content in cached.get("journal_entries", {}).items():
                if isinstance(content, list):
                    content = "\n\n".join(str(c) for c in content if c)
                if content and write_journal_entry(ws, filename, content, "Compaction", date):
                    total_journals += 1

        # Keep janitor schedule aligned with timeout split day boundaries.
        if run_chunk_janitor and chunk.trigger in {"day", "end"}:
            print(f"    [janitor] nightly boundary run tasks={janitor_tasks}")
            janitor_timing_events.extend(
                _run_janitor_tasks(
                    env,
                    janitor_tasks,
                    timeout_s=1800,
                    phase=f"chunk_{i+1}",
                )
            )
            janitor_runs += 1

        # Chunk completed (extracted + stored + optional janitor): persist checkpoint.
        progress_payload = {
            "last_completed_chunk": i,
            "completed_at": datetime.now().isoformat(),
            "total_chunks": len(chunks),
        }
        _atomic_write_json(progress_path, progress_payload)
        if only_chunk is not None:
            print("    Single-chunk mode complete; stopping extraction loop.")
            break
    phase2_elapsed_s = time.time() - t_phase2

    db_path = workspace / "data" / "memory.db"
    with sqlite3.connect(str(db_path)) as conn:
        db_nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        db_edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        status_counts = dict(conn.execute(
            "SELECT status, count(*) FROM nodes GROUP BY status"
        ).fetchall())

    print(f"\n  Extraction summary:")
    print(f"    Total extracted: {total_facts} facts")
    print(f"    Stored: {total_stored} facts, {total_edges} edges")
    print(f"    Snippets: {total_snippets} bullets, Journal: {total_journals} entries")
    janitor_total_s = sum(float(e.get("elapsed_seconds", 0.0)) for e in janitor_timing_events)
    print(f"    Chunk janitor runs: {janitor_runs} ({janitor_total_s:.1f}s total)")
    print(f"    DB: {db_nodes} nodes, {db_edges} edges, status={status_counts}")

    return {
        "total_facts": total_facts,
        "stored": total_stored,
        "edges": total_edges,
        "janitor_runs": janitor_runs,
        "janitor_timing_events": janitor_timing_events,
        "janitor_timing_seconds": round(janitor_total_s, 3),
        "phase1_extraction_llm_seconds": round(phase1_elapsed_s, 3),
        "phase2_apply_seconds": round(phase2_elapsed_s, 3),
        "extraction_total_seconds": round(time.time() - t_extract_start, 3),
    }


_STORE_TIMEOUT_S = _env_int("STORE_TIMEOUT_S", 60)
_STORE_MAX_RETRIES = _env_int("STORE_MAX_RETRIES", 3)
_JANITOR_TASK_TIMEOUT_DEFAULT_S = _env_int("JANITOR_TASK_TIMEOUT_S", 1800)
_JANITOR_TASK_RETRIES = _env_int("JANITOR_TASK_RETRIES", 1)
_JANITOR_TELEMETRY_ENABLED = os.environ.get("BENCHMARK_JANITOR_TELEMETRY", "1") == "1"
_JANITOR_TELEMETRY_HEARTBEAT_S = _env_int("BENCHMARK_JANITOR_HEARTBEAT_S", 20)
_JANITOR_STALL_WARN_S = _env_int("BENCHMARK_JANITOR_STALL_WARN_S", 180)
_JANITOR_STALL_FAIL_S = _env_int("BENCHMARK_JANITOR_STALL_FAIL_S", 0)
_FORCE_WEEKLY_JOURNAL_SCAN = os.environ.get("BENCHMARK_FORCE_WEEKLY_JOURNAL_SCAN", "1") == "1"
_DEFAULT_OWNER_ID = str(os.environ.get("BENCH_OWNER_ID", "maya")).strip() or "maya"


def _run_subprocess_with_retry(cmd, *, cwd, env, timeout, max_retries=_STORE_MAX_RETRIES, label="cmd"):
    """Run a subprocess with retry on timeout or transient failure."""
    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout, cwd=cwd, env=env,
            )
            return result
        except subprocess.TimeoutExpired as e:
            last_err = e
            if attempt < max_retries:
                wait = 5 * attempt
                print(f"    [{label}] timeout ({timeout}s) attempt {attempt}/{max_retries}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise last_err  # unreachable but satisfies type checker


def _janitor_task_timeout_s(task: str, fallback_s: int) -> int:
    """Resolve janitor task timeout from env overrides."""
    task_key = re.sub(r"[^A-Za-z0-9]+", "_", str(task).upper())
    override = os.environ.get(f"JANITOR_TIMEOUT_{task_key}_S")
    if override:
        try:
            return max(30, int(override))
        except Exception:
            pass
    if fallback_s > 0:
        return fallback_s
    return _JANITOR_TASK_TIMEOUT_DEFAULT_S


def _janitor_workspace_from_env(env: dict) -> Optional[Path]:
    raw = str(env.get("CLAWDBOT_WORKSPACE", "") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except Exception:
        return Path(raw)


def _append_janitor_telemetry(env: dict, payload: dict) -> None:
    """Append task telemetry to workspace-local jsonl (best effort)."""
    if not _JANITOR_TELEMETRY_ENABLED:
        return
    ws = _janitor_workspace_from_env(env)
    if not ws:
        return
    try:
        logs_dir = ws / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        with (logs_dir / "janitor-task-telemetry.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=True) + "\n")
    except Exception:
        # Never fail benchmark flow for telemetry write errors.
        pass


def _janitor_activity_age_s(
    log_paths: List[Path],
    last_seen_mtime: dict,
    now_s: float,
    task_start_s: float,
) -> Tuple[float, bool]:
    """Return (seconds since observed activity for this task, changed-now flag).

    Important: age is clamped to this task's elapsed time so stale mtimes from
    previous tasks/runs don't trigger immediate false stall warnings.
    """
    latest_mtime = 0.0
    changed = False
    for p in log_paths:
        try:
            mtime = p.stat().st_mtime
        except Exception:
            continue
        latest_mtime = max(latest_mtime, mtime)
        prev = float(last_seen_mtime.get(str(p), 0.0))
        if mtime > prev:
            last_seen_mtime[str(p)] = mtime
            changed = True
    if latest_mtime <= 0.0:
        return max(0.0, now_s - task_start_s), changed
    # Clamp idle age to this task lifetime to avoid inherited stale mtimes.
    return min(max(0.0, now_s - latest_mtime), max(0.0, now_s - task_start_s)), changed


def _run_janitor_task_with_retry(
    task: str,
    env: dict,
    timeout_s: int,
    max_retries: int,
    force_distill: bool = False,
) -> subprocess.CompletedProcess:
    """Run one janitor task with retry on timeout."""
    attempts = max(1, int(max_retries))
    last_timeout: Optional[subprocess.TimeoutExpired] = None
    for attempt in range(1, attempts + 1):
        try:
            if task == "all":
                cmd = _janitor_cmd("--task", "all", "--apply", "--force-distill")
            else:
                cmd = _janitor_cmd("--task", task, "--apply")
                if force_distill and task == "journal":
                    cmd.append("--force-distill")
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_spool, \
                 tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_spool:
                proc = subprocess.Popen(
                    cmd,
                    env=env,
                    cwd=str(_QUAID_DIR),
                    stdout=stdout_spool,
                    stderr=stderr_spool,
                    text=True,
                )
                start = time.time()
                last_heartbeat = 0.0
                stall_warned = False
                ws = _janitor_workspace_from_env(env)
                log_paths: List[Path] = []
                if ws:
                    log_paths = [ws / "logs" / "janitor.log", ws / "logs" / "janitor-stats.json"]
                seen_mtimes: dict = {}
                # Seed mtime baseline at task start to avoid false initial idle spikes.
                for p in log_paths:
                    try:
                        seen_mtimes[str(p)] = float(p.stat().st_mtime)
                    except Exception:
                        pass

                _append_janitor_telemetry(
                    env,
                    {
                        "event": "task_start",
                        "task": task,
                        "attempt": attempt,
                        "pid": int(proc.pid or -1),
                        "timeout_s": int(timeout_s),
                        "force_distill": bool(force_distill and task == "journal"),
                    },
                )

                while True:
                    now = time.time()
                    rc = proc.poll()
                    age_s, changed = _janitor_activity_age_s(log_paths, seen_mtimes, now, start)

                    if changed:
                        stall_warned = False

                    if _JANITOR_STALL_FAIL_S > 0 and age_s >= float(_JANITOR_STALL_FAIL_S):
                        proc.kill()
                        proc.communicate()
                        stdout_spool.flush()
                        stderr_spool.flush()
                        stdout_spool.seek(0)
                        stderr_spool.seek(0)
                        stdout = stdout_spool.read()
                        stderr = stderr_spool.read()
                        msg = (
                            f"janitor stall detected: task={task}, attempt={attempt}, "
                            f"idle_s={int(age_s)}, stall_fail_s={_JANITOR_STALL_FAIL_S}"
                        )
                        _append_janitor_telemetry(
                            env,
                            {
                                "event": "stall_fail",
                                "task": task,
                                "attempt": attempt,
                                "pid": int(proc.pid or -1),
                                "idle_s": int(age_s),
                                "stall_fail_s": int(_JANITOR_STALL_FAIL_S),
                            },
                        )
                        raise RuntimeError(
                            f"{msg}\n--- STDERR (last 30 lines) ---\n"
                            f"{chr(10).join((stderr or '').splitlines()[-30:])}\n"
                            f"--- STDOUT (last 30 lines) ---\n"
                            f"{chr(10).join((stdout or '').splitlines()[-30:])}"
                        )

                    if (not stall_warned) and _JANITOR_STALL_WARN_S > 0 and age_s >= float(_JANITOR_STALL_WARN_S):
                        stall_warned = True
                        warn = (
                            f"    [janitor:{task}] low activity for {int(age_s)}s "
                            f"(attempt {attempt}/{attempts})"
                        )
                        print(warn)
                        _append_janitor_telemetry(
                            env,
                            {
                                "event": "stall_warn",
                                "task": task,
                                "attempt": attempt,
                                "pid": int(proc.pid or -1),
                                "idle_s": int(age_s),
                                "stall_warn_s": int(_JANITOR_STALL_WARN_S),
                            },
                        )

                    if _JANITOR_TELEMETRY_ENABLED and (now - last_heartbeat) >= max(5, _JANITOR_TELEMETRY_HEARTBEAT_S):
                        last_heartbeat = now
                        _append_janitor_telemetry(
                            env,
                            {
                                "event": "heartbeat",
                                "task": task,
                                "attempt": attempt,
                                "pid": int(proc.pid or -1),
                                "elapsed_s": int(now - start),
                                "idle_s": int(age_s),
                            },
                        )

                    if rc is not None:
                        proc.communicate()
                        stdout_spool.flush()
                        stderr_spool.flush()
                        stdout_spool.seek(0)
                        stderr_spool.seek(0)
                        stdout = stdout_spool.read()
                        stderr = stderr_spool.read()
                        _append_janitor_telemetry(
                            env,
                            {
                                "event": "task_end",
                                "task": task,
                                "attempt": attempt,
                                "pid": int(proc.pid or -1),
                                "elapsed_s": int(time.time() - start),
                                "returncode": int(rc),
                                "stdout_lines": len((stdout or "").splitlines()),
                                "stderr_lines": len((stderr or "").splitlines()),
                            },
                        )
                        return subprocess.CompletedProcess(cmd, rc, stdout, stderr)

                    if (now - start) >= float(timeout_s):
                        proc.kill()
                        proc.communicate()
                        stdout_spool.flush()
                        stderr_spool.flush()
                        stdout_spool.seek(0)
                        stderr_spool.seek(0)
                        stdout = stdout_spool.read()
                        stderr = stderr_spool.read()
                        _append_janitor_telemetry(
                            env,
                            {
                                "event": "task_timeout",
                                "task": task,
                                "attempt": attempt,
                                "pid": int(proc.pid or -1),
                                "elapsed_s": int(now - start),
                                "timeout_s": int(timeout_s),
                                "idle_s": int(age_s),
                            },
                        )
                        raise subprocess.TimeoutExpired(cmd, timeout_s, output=stdout, stderr=stderr)

                    time.sleep(2)
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
            if attempt >= attempts:
                break
            wait_s = min(30, 5 * attempt)
            print(
                f"    [janitor:{task}] timeout ({timeout_s}s) "
                f"attempt {attempt}/{attempts}, retrying in {wait_s}s..."
            )
            time.sleep(wait_s)

    assert last_timeout is not None
    raise RuntimeError(
        f"janitor task timeout: task={task}, timeout_s={timeout_s}, retries={attempts}"
    ) from last_timeout


def _store_facts(
    workspace: Path,
    facts: list,
    env: dict,
    session_num: int,
    session_date: str,
    owner_id: Optional[str] = None,
) -> tuple:
    """Store facts and edges into DB via subprocess. Returns (stored, edges_created)."""
    owner_id = str(owner_id or os.environ.get("BENCH_OWNER_ID", "maya")).strip() or "maya"
    stored = 0
    edges_created = 0
    quaid_dir = str(_QUAID_DIR)

    for fact in facts:
        text = fact.get("text", "").strip()
        if not text or len(text.split()) < 3:
            continue

        conf_str = fact.get("extraction_confidence", "medium")
        conf_num = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(conf_str, 0.6)
        category = fact.get("category", "fact")
        privacy = fact.get("privacy", "shared")
        keywords = fact.get("keywords", "")
        knowledge_type = "preference" if category == "preference" else "fact"
        raw_source = str(fact.get("source", "user")).strip().lower()
        source_type = raw_source if raw_source else "user"
        speaker = fact.get("speaker")
        project = str(fact.get("project", "") or "").strip().lower() or None
        raw_domains = fact.get("domains", [])
        if isinstance(raw_domains, str):
            raw_domains = [d for d in raw_domains.split(",")]
        if not isinstance(raw_domains, list):
            raw_domains = []
        domains = [str(d).strip().lower() for d in raw_domains if str(d).strip()]
        if not domains:
            domains = ["projects"] if project else ["personal"]
        domains = list(dict.fromkeys(domains))

        cmd = _memory_graph_cmd(
            "store",
            text,
            "--category", category,
            "--owner", owner_id,
            "--extraction-confidence", str(conf_num),
            "--privacy", privacy,
            "--knowledge-type", knowledge_type,
            "--source-type", source_type,
            "--source", "benchmark-extraction",
            "--session-id", f"session-{session_num}",
        )
        if speaker:
            cmd.extend(["--speaker", str(speaker)])
        if keywords:
            cmd.extend(["--keywords", keywords])
        if domains:
            cmd.extend(["--domains", ",".join(domains)])
        if project:
            cmd.extend(["--project", project])
        if session_date and session_date != "unknown":
            cmd.extend(["--created-at", f"{session_date}T09:00:00"])

        try:
            result = _run_subprocess_with_retry(
                cmd, cwd=quaid_dir, env=env,
                timeout=_STORE_TIMEOUT_S, label="store",
            )
            if result.returncode != 0:
                fail_log = workspace / "logs" / "store_failures.jsonl"
                fail_log.parent.mkdir(parents=True, exist_ok=True)
                fail_record = {
                    "ts": datetime.now().isoformat(),
                    "session_num": session_num,
                    "session_date": session_date,
                    "text": text[:200],
                    "category": category,
                    "privacy": privacy,
                    "knowledge_type": knowledge_type,
                    "keywords": keywords,
                    "domains": domains,
                    "project": project,
                    "cmd_name": "memory store",
                    "rc": result.returncode,
                    "stdout": result.stdout[:2000],
                    "stderr": result.stderr[:4000],
                    "fact_keys": sorted(str(k) for k in fact.keys()),
                }
                with fail_log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(fail_record, ensure_ascii=False) + "\n")
                print(f"    [store] WARNING: store failed for '{text[:80]}' (rc={result.returncode}), logged and continuing")
                continue  # Non-fatal: log and skip this fact
            output = result.stdout.strip()
            stored_match = re.match(r"Stored: (.+)", output)
            fact_id = None
            if stored_match:
                stored += 1
                fact_id = stored_match.group(1)
            else:
                updated_match = re.match(r"Updated existing: (.+)", output)
                if updated_match:
                    stored += 1
                    fact_id = updated_match.group(1)

            # Apply edge creation for both newly created and updated facts.
            if fact_id:
                for edge in fact.get("edges", []):
                    subj = edge.get("subject", "")
                    rel = edge.get("relation", "")
                    obj = edge.get("object", "")
                    if not (subj and rel and obj):
                        continue
                    edge_cmd = _memory_graph_cmd(
                        "create-edge", subj, rel, obj,
                        "--owner", owner_id,
                        "--create-missing", "--json",
                        "--source-fact-id", fact_id,
                    )
                    edge_result = _run_subprocess_with_retry(
                        edge_cmd, cwd=quaid_dir, env=env,
                        timeout=_STORE_TIMEOUT_S, label="edge",
                    )
                    if edge_result.returncode != 0:
                        print(f"    [edge] WARNING: edge create failed for '{subj}->{rel}->{obj}' (rc={edge_result.returncode}), skipping")
                        continue
                    status = "unknown"
                    try:
                        edge_payload = json.loads((edge_result.stdout or "").strip() or "{}")
                        status = str(edge_payload.get("status", "unknown"))
                    except Exception:
                        edge_payload = {"raw_stdout": (edge_result.stdout or "")[:500]}
                    # Only count actual creations.
                    if status == "created":
                        edges_created += 1
                    # Persist edge outcome for diagnosis.
                    edge_log = workspace / "logs" / "edge_outcomes.jsonl"
                    edge_log.parent.mkdir(parents=True, exist_ok=True)
                    with edge_log.open("a", encoding="utf-8") as f:
                        f.write(json.dumps({
                            "ts": datetime.now().isoformat(),
                            "session_num": session_num,
                            "fact_id": fact_id,
                            "fact_text": text[:200],
                            "edge": {"subject": subj, "relation": rel, "object": obj},
                            "status": status,
                            "payload": edge_payload,
                        }, ensure_ascii=False) + "\n")
        except Exception as e:
            # Non-fatal: log and continue to next fact
            fail_log = workspace / "logs" / "store_failures.jsonl"
            fail_log.parent.mkdir(parents=True, exist_ok=True)
            fail_record = {
                "ts": datetime.now().isoformat(),
                "session_num": session_num,
                "text": text[:200] if text else "",
                "error": str(e),
                "type": type(e).__name__,
            }
            with fail_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(fail_record, ensure_ascii=False) + "\n")
            print(f"    [store] ERROR: {type(e).__name__} for '{text[:80]}': {e}, logged and continuing")

    return stored, edges_created


# ---------------------------------------------------------------------------
# Phase 3b: Per-day extraction (trusted baseline)
# ---------------------------------------------------------------------------

def _load_reviews(max_sessions: Optional[int] = None) -> list:
    """Load arc sessions, optionally merging filler sessions (L scale).

    Uses _FILLER_DIR global. When set, loads filler sessions and merges
    chronologically with arc sessions.
    """
    assets_dir = _resolve_assets_dir()
    sessions_to_load = list(range(1, max_sessions + 1)) if max_sessions else None
    arc_reviews = load_all_reviews(assets_dir, sessions=sessions_to_load)

    if _FILLER_DIR and _FILLER_DIR.exists():
        filler_reviews = load_filler_reviews(_FILLER_DIR)
        if filler_reviews:
            reviews = merge_sessions_chronologically(arc_reviews, filler_reviews)
            print(f"  Sessions: {len(reviews)} ({len(arc_reviews)} arc + {len(filler_reviews)} filler)")
            return reviews

    return arc_reviews


def _get_session_date(review) -> str:
    """Get date for a session (arc or filler)."""
    snum = review.session_num
    if snum < 0:
        filler_id = f"F{abs(snum):03d}"
        return FILLER_DATES.get(filler_id, "2026-03-15")
    return SESSION_DATES.get(snum, "unknown")


def _group_sessions_by_date(reviews: list) -> list:
    """Group sessions by date. Returns list of (date, [reviews]) sorted chronologically."""
    from collections import OrderedDict
    by_date = OrderedDict()
    for review in reviews:
        date = _get_session_date(review)
        by_date.setdefault(date, []).append(review)
    return list(by_date.items())


def run_per_day_extraction(
    workspace: Path,
    api_key: str,
    no_cache: bool = False,
    model: str = "claude-sonnet-4-5-20250929",
    max_sessions: Optional[int] = None,
    janitor_tasks: Optional[List[str]] = None,
) -> dict:
    """Extract facts day-by-day, running janitor after each day.

    This mirrors how Quaid works in production: at the end of each day's
    conversations, compaction fires and extracts facts. The nightly janitor
    then processes them (review, dedup, embeddings, graduation).

    This is the "trusted baseline" — it tests the full lifecycle with
    incremental accumulation, not a single bulk extraction.
    """
    print("=" * 60)
    print("PHASE 3b: PER-DAY EXTRACTION + JANITOR")
    print("=" * 60)

    reviews = _load_reviews(max_sessions)
    print(f"  Loaded {len(reviews)} sessions (model: {model})")

    days = _group_sessions_by_date(reviews)
    print(f"  Grouped into {len(days)} days:")
    for date, day_reviews in days:
        labels = []
        for r in day_reviews:
            if r.session_num < 0:
                labels.append(f"F{abs(r.session_num):03d}")
            else:
                labels.append(str(r.session_num))
        print(f"    {date}: sessions [{', '.join(labels)}]")
    print()

    system_prompt = build_extraction_prompt("Maya", "Assistant")
    env = _make_env(workspace)
    cache_dir = workspace / "extraction_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    total_facts = 0
    total_stored = 0
    total_edges = 0
    total_snippets = 0
    total_journals = 0
    janitor_runs = 0
    janitor_timing_events: List[dict] = []
    janitor_tasks = janitor_tasks or list(BENCHMARK_JANITOR_TASKS)
    owner_id = str(os.environ.get("BENCH_OWNER_ID", "maya")).strip() or "maya"

    for day_idx, (date, day_reviews) in enumerate(days):
        labels = []
        for r in day_reviews:
            if r.session_num < 0:
                labels.append(f"F{abs(r.session_num):03d}")
            else:
                labels.append(str(r.session_num))
        print(f"\n--- Day {day_idx + 1}/{len(days)}: {date} (sessions [{', '.join(labels)}]) ---")
        snums = [r.session_num for r in day_reviews]
        if not snums:
            print(f"  WARNING: no sessions for day {date}; skipping")
            continue

        # Check for project file changes on this day
        projects_changed = set()
        for review in day_reviews:
            snum = review.session_num
            for ps, project, commit in PROJECT_SESSIONS:
                if ps == snum:
                    source_repo = RECIPE_APP_DIR if project == "recipe-app" else PORTFOLIO_DIR
                    target_dir = workspace / "projects" / project
                    if source_repo.exists():
                        print(f"  Project update: {project} @ {commit}")
                        _sync_repo_subtree_at_commit(
                            source_repo,
                            target_dir,
                            commit,
                            context=f"day {day_idx + 1} session {snum} {project}",
                        )
                        projects_changed.add((project, snum))
                    else:
                        _append_project_ingest_trace(
                            workspace,
                            {
                                "event": "source_repo_missing",
                                "phase": "per_day_project_update",
                                "session_num": snum,
                                "project": project,
                                "source_repo": str(source_repo),
                            },
                        )
                        raise RuntimeError(
                            f"Required project source repo missing for {project}: {source_repo}. "
                            f"Set BENCH_PROJECTS_ROOT or BENCH_{project.replace('-', '_').upper()}_DIR."
                        )

        # Session-aware doc enrichment — only when project files changed
        if projects_changed:
            for project, snum in projects_changed:
                # Find the review for this session to get the transcript
                review_for_session = next(
                    (r for r in day_reviews if r.session_num == snum), None
                )
                if review_for_session:
                    session_transcript = format_transcript_for_extraction(review_for_session)
                    _enrich_project_docs_with_session(
                        workspace, project, session_transcript, api_key,
                        session_num=snum, no_cache=no_cache,
                    )

        # Cache key for this day's extraction
        cache_path = cache_dir / f"day-{date}.json"

        cached = None
        if not no_cache and cache_path.exists():
            cached = _read_json_or_none(cache_path)
            if cached is None:
                print(f"  WARNING: corrupt day cache ({cache_path.name}), re-extracting")
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        if cached is not None:
            n_facts = len(cached.get("facts", []))
            print(f"  Cached: {n_facts} facts")
        else:
            # Build transcript for this day's sessions
            transcript_parts = []
            for review in day_reviews:
                snum = review.session_num
                track_label = "Personal" if review.track == 1 else "Project"
                transcript = format_transcript_for_extraction(review)
                if transcript.strip():
                    transcript_parts.append(
                        f"=== Session {snum} ({track_label}) — {date} ===\n{transcript}"
                    )

            combined_transcript = "\n\n".join(transcript_parts)
            print(f"  Transcript: {len(combined_transcript)} chars (~{len(combined_transcript)//4} tokens)")

            user_message = (
                f"Extract memorable facts from these conversation sessions "
                f"with Maya on {date}.\n\n{combined_transcript}"
            )

            t0 = time.time()
            raw_response, usage = _call_anthropic_cached(
                system_prompt, user_message, model, api_key,
                max_tokens=16384,
            )
            elapsed = time.time() - t0
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            print(f"  Extraction: {elapsed:.1f}s, {in_tok} in + {out_tok} out tokens")

            result = parse_extraction_response(raw_response)
            cached = {
                "facts": result.get("facts", []),
                "soul_snippets": result.get("soul_snippets", {}),
                "journal_entries": result.get("journal_entries", {}),
                "usage": usage,
                "model": model,
                "sessions": snums,
                "date": date,
                "timestamp": datetime.now().isoformat(),
            }
            _atomic_write_json(cache_path, cached)
            n_facts = len(cached["facts"])
            print(f"  Extracted: {n_facts} facts")

        # Store facts
        facts = cached.get("facts", [])
        valid_sessions = set(snums)
        fallback_sessions = sorted(valid_sessions)
        fallback_idx = 0
        facts_by_session: dict[int, list] = {}
        for fact in facts:
            session_num = _session_for_fact(fact, None, valid_sessions)
            if session_num is None:
                # Distribute untagged day-level facts deterministically across sessions.
                if fallback_sessions:
                    session_num = fallback_sessions[fallback_idx % len(fallback_sessions)]
                    fallback_idx += 1
                else:
                    session_num = snums[0]
            facts_by_session.setdefault(session_num, []).append(fact)
        stored = 0
        edges = 0
        for session_num, session_facts in facts_by_session.items():
            s, e = _store_facts(workspace, session_facts, env, session_num, date, owner_id=owner_id)
            stored += s
            edges += e
        total_facts += len(facts)
        total_stored += stored
        total_edges += edges

        # Write snippets and journal entries only when explicitly enabled.
        if ENABLE_CORE_MARKDOWN_CAPTURE:
            ws = str(workspace)
            for filename, bullets in cached.get("soul_snippets", {}).items():
                if isinstance(bullets, str):
                    bullets = [bullets] if bullets.strip() else []
                if bullets and write_snippet_entry(ws, filename, bullets, "Compaction", date):
                    total_snippets += len(bullets)

            for filename, content in cached.get("journal_entries", {}).items():
                if isinstance(content, list):
                    content = "\n\n".join(str(c) for c in content if c)
                if content and write_journal_entry(ws, filename, content, "Compaction", date):
                    total_journals += 1

        print(f"  Stored: {stored} facts, {edges} edges")

        # Run full janitor after each day for production parity.
        try:
            janitor_timing_events.extend(
                _run_janitor_tasks(
                    env,
                    janitor_tasks,
                    timeout_s=1800,
                    phase=f"per_day_{date}",
                )
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("janitor full pipeline timed out (1800s)")
        janitor_runs += 1
        print(f"  Janitor tasks complete: {janitor_tasks}")

    # Final mechanical enrichment for any projects NOT touched by sessions
    # (session-aware enrichment already ran for projects that changed)
    for proj_name in ["recipe-app", "portfolio-site"]:
        tools_md = workspace / "projects" / proj_name / "TOOLS.md"
        # Only enrich if TOOLS.md is still the bare seed (< 200 bytes)
        if tools_md.exists() and tools_md.stat().st_size < 200:
            _enrich_project_docs(workspace)
            break

    # DB verification
    db_path = workspace / "data" / "memory.db"
    with sqlite3.connect(str(db_path)) as conn:
        db_nodes = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        db_edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        status_counts = dict(conn.execute(
            "SELECT status, count(*) FROM nodes GROUP BY status"
        ).fetchall())

    print(f"\n  Per-day extraction summary:")
    print(f"    Days processed: {len(days)}")
    print(f"    Total extracted: {total_facts} facts")
    print(f"    Stored: {total_stored} facts, {total_edges} edges")
    print(f"    Snippets: {total_snippets} bullets, Journal: {total_journals} entries")
    janitor_total_s = sum(float(e.get("elapsed_seconds", 0.0)) for e in janitor_timing_events)
    print(f"    Janitor runs: {janitor_runs} ({janitor_total_s:.1f}s total)")
    print(f"    DB: {db_nodes} nodes, {db_edges} edges, status={status_counts}")

    return {
        "total_facts": total_facts,
        "stored": total_stored,
        "edges": total_edges,
        "days": len(days),
        "janitor_runs": janitor_runs,
        "janitor_timing_events": janitor_timing_events,
        "janitor_timing_seconds": round(janitor_total_s, 3),
    }


# ---------------------------------------------------------------------------
# Phase 4: Janitor
# ---------------------------------------------------------------------------

def run_janitor(workspace: Path, tasks: Optional[List[str]] = None) -> List[dict]:
    """Run final janitor pass via subprocess and return timing events."""
    print("=" * 60)
    print("PHASE 4: FINAL JANITOR")
    print("=" * 60)

    env = _make_env(workspace)
    task_list = tasks or list(BENCHMARK_JANITOR_TASKS)

    print(f"  Running janitor tasks: {task_list}")

    t0 = time.time()
    try:
        events = _run_janitor_tasks(env, task_list, timeout_s=1800, phase="final")
    except subprocess.TimeoutExpired:
        raise RuntimeError("janitor timed out (1800s)")
    elapsed = time.time() - t0

    print(f"\n  Janitor completed in {elapsed:.1f}s\n")
    return events


def _run_full_janitor_apply(env: dict, timeout_s: int = 1800) -> subprocess.CompletedProcess:
    """Run full janitor pipeline (`all --apply --force-distill`)."""
    return subprocess.run(
        _janitor_cmd("--task", "all", "--apply", "--force-distill"),
        env=env, cwd=str(_QUAID_DIR),
        capture_output=True, text=True, timeout=timeout_s,
    )


def _run_janitor_tasks(
    env: dict,
    tasks: List[str],
    timeout_s: int = 1800,
    phase: str = "unknown",
) -> List[dict]:
    """Run an explicit janitor task list with apply mode.

    Returns per-task timing events for bottleneck tracking.

    `all` maps to full pipeline (`--task all --apply --force-distill`).
    Other tasks run as `--task <name> --apply`.
    """
    events: List[dict] = []
    for task in tasks:
        task_timeout_s = _janitor_task_timeout_s(task, timeout_s)
        t0 = time.time()
        result = _run_janitor_task_with_retry(
            task=task,
            env=env,
            timeout_s=task_timeout_s,
            max_retries=_JANITOR_TASK_RETRIES,
        )
        elapsed_s = round(time.time() - t0, 3)
        event = {
            "time": datetime.now().isoformat(),
            "phase": phase,
            "task": task,
            "elapsed_seconds": elapsed_s,
            "timeout_seconds": task_timeout_s,
            "success": result.returncode == 0,
            "returncode": result.returncode,
        }
        events.append(event)

        if result.returncode != 0:
            stderr_lines = [ln for ln in (result.stderr or "").split("\n") if ln.strip()]
            stdout_lines = [ln for ln in (result.stdout or "").split("\n") if ln.strip()]
            stderr_tail = "\n".join(stderr_lines[-30:])
            stdout_tail = "\n".join(stdout_lines[-30:])
            if task == "journal":
                print(
                    "    [janitor] WARNING: journal task failed; continuing benchmark run.\n"
                    f"    rc={result.returncode}\n"
                    f"    --- STDERR (last 30 lines) ---\n{stderr_tail}\n"
                    f"    --- STDOUT (last 30 lines) ---\n{stdout_tail}"
                )
                event["non_fatal"] = True
                continue
            raise RuntimeError(
                f"janitor task failed: task={task}, rc={result.returncode}\n"
                f"--- STDERR (last 30 lines) ---\n{stderr_tail}\n"
                f"--- STDOUT (last 30 lines) ---\n{stdout_tail}"
            )

    return events


def _save_janitor_timing(workspace: Path, events: List[dict]) -> None:
    """Persist janitor timing metrics for this run."""
    task_totals = {}
    for e in events:
        task = str(e.get("task", "unknown"))
        task_totals[task] = task_totals.get(task, 0.0) + float(e.get("elapsed_seconds", 0.0))

    payload = {
        "summary": {
            "events": len(events),
            "total_seconds": round(sum(float(e.get("elapsed_seconds", 0.0)) for e in events), 3),
            "by_task_seconds": {k: round(v, 3) for k, v in sorted(task_totals.items())},
        },
        "events": events,
    }
    out = workspace / "janitor_timing.json"
    _atomic_write_json(out, payload)


def _build_phase_timing_payload(
    *,
    mode: str,
    total_elapsed_seconds: float,
    phase_seconds: dict,
    janitor_events: List[dict],
) -> dict:
    """Build machine-readable phase timing + bottleneck summary."""
    normalized = {
        str(k): round(float(v), 3)
        for k, v in (phase_seconds or {}).items()
        if v is not None and float(v) >= 0.0
    }
    total = max(float(total_elapsed_seconds), 0.001)
    phases = []
    for name, seconds in sorted(normalized.items(), key=lambda kv: kv[1], reverse=True):
        phases.append(
            {
                "phase": name,
                "seconds": round(seconds, 3),
                "pct_of_total": round((seconds / total) * 100.0, 2),
            }
        )

    janitor_by_task = {}
    for e in janitor_events or []:
        task = str(e.get("task", "unknown"))
        janitor_by_task[task] = janitor_by_task.get(task, 0.0) + float(e.get("elapsed_seconds", 0.0))
    janitor_tasks = [
        {"task": task, "seconds": round(sec, 3)}
        for task, sec in sorted(janitor_by_task.items(), key=lambda kv: kv[1], reverse=True)
    ]

    bottlenecks = []
    for p in phases[:3]:
        bottlenecks.append(
            {
                "name": p["phase"],
                "seconds": p["seconds"],
                "pct_of_total": p["pct_of_total"],
                "kind": "phase",
            }
        )
    for jt in janitor_tasks[:3]:
        bottlenecks.append(
            {
                "name": f"janitor:{jt['task']}",
                "seconds": jt["seconds"],
                "kind": "janitor_task",
            }
        )

    return {
        "mode": mode,
        "summary": {
            "total_elapsed_seconds": round(total_elapsed_seconds, 3),
            "phase_count": len(phases),
            "janitor_event_count": len(janitor_events or []),
        },
        "phases": phases,
        "janitor_by_task": janitor_tasks,
        "bottlenecks": bottlenecks,
    }


def _save_phase_timing(workspace: Path, payload: dict) -> Path:
    out = workspace / "phase_timing.json"
    _atomic_write_json(out, payload)
    return out


def _print_phase_timing(payload: dict) -> None:
    print(f"\n{'=' * 60}")
    print("PHASE TIME BREAKDOWN")
    print(f"{'=' * 60}")
    print(f"{'Phase':<36} {'Sec':>10} {'%':>8}")
    print(f"{'-' * 60}")
    for p in payload.get("phases", []):
        print(f"{p['phase']:<36} {p['seconds']:>10.1f} {p['pct_of_total']:>7.1f}%")
    if payload.get("janitor_by_task"):
        print(f"\nTop Janitor Tasks:")
        for jt in payload["janitor_by_task"][:5]:
            print(f"  {jt['task']:<28} {jt['seconds']:>8.1f}s")

    tops = payload.get("bottlenecks", [])[:3]
    if tops:
        print(f"\nBottlenecks:")
        for b in tops:
            if b.get("kind") == "phase":
                print(f"  {b['name']}: {b['seconds']:.1f}s ({b['pct_of_total']:.1f}%)")
            else:
                print(f"  {b['name']}: {b['seconds']:.1f}s")


def verify_post_janitor(workspace: Path) -> None:
    """Post-janitor verification checkpoint."""
    print("=" * 60)
    print("PHASE 4b: POST-JANITOR VERIFICATION")
    print("=" * 60)

    db_path = workspace / "data" / "memory.db"
    with sqlite3.connect(str(db_path)) as conn:
        # DB stats
        total = conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
        edges = conn.execute("SELECT count(*) FROM edges").fetchone()[0]
        status_counts = dict(conn.execute(
            "SELECT status, count(*) FROM nodes GROUP BY status"
        ).fetchall())
        type_counts = dict(conn.execute(
            "SELECT type, count(*) FROM nodes GROUP BY type"
        ).fetchall())

    print(f"  DB: {total} nodes, {edges} edges")
    print(f"  Status: {status_counts}")
    print(f"  Types: {type_counts}")
    pending = status_counts.get("pending", 0)
    if pending > 0:
        raise RuntimeError(
            f"Invalid run: {pending} facts still pending after janitor (graduation/review failed)"
        )

    # Core markdowns
    for md in ["SOUL.md", "USER.md", "MEMORY.md"]:
        path = workspace / md
        if path.exists():
            content = path.read_text().strip()
            lines = len(content.split("\n"))
            preview = content[:200].replace("\n", " | ")
            print(f"  {md}: {lines} lines — {preview}...")
        else:
            print(f"  {md}: MISSING")

    # Project docs
    for project in ["recipe-app", "portfolio-site"]:
        pmd = workspace / "projects" / project / "PROJECT.md"
        if pmd.exists():
            lines = len(pmd.read_text().split("\n"))
            print(f"  projects/{project}/PROJECT.md: {lines} lines")
        else:
            print(f"  projects/{project}/PROJECT.md: MISSING")

    # Snippets
    for sfile in workspace.glob("*.snippets.md"):
        lines = len(sfile.read_text().split("\n"))
        print(f"  {sfile.name}: {lines} lines")

    # Journal
    journal_dir = workspace / "journal"
    if journal_dir.exists():
        for jfile in journal_dir.glob("*.journal.md"):
            lines = len(jfile.read_text().split("\n"))
            print(f"  journal/{jfile.name}: {lines} lines")
        for afile in (journal_dir / "archive").glob("*.md") if (journal_dir / "archive").exists() else []:
            lines = len(afile.read_text().split("\n"))
            print(f"  journal/archive/{afile.name}: {lines} lines")

    print()


# ---------------------------------------------------------------------------
# Phase 4c: Post-hoc project tagging
# ---------------------------------------------------------------------------

# Technical fact patterns — keywords that indicate implementation details
_TECH_PATTERNS = [
    r'\bsqlite\b', r'\bexpress\b', r'\bnode\.?js\b', r'\breact\b',
    r'\bapi\b', r'\bendpoint\b', r'\bmiddleware\b', r'\bjwt\b', r'\bpbkdf2\b',
    r'\bcss\b', r'\bhtml\b', r'\bdatabase\b', r'\bschema\b',
    r'\broute\b', r'\bserver\b', r'\bnpm\b', r'\bpackage\.json\b',
    r'\bmodule\b', r'\bfunction\b', r'\bdeploy\b',
    r'\bauth\b', r'\btoken\b', r'\bhash\b', r'\bedamam\b',
    r'\bjest\b', r'\bdependenc', r'\blibrary\b', r'\bframework\b',
    r'\bgraphql\b', r'\bapollo\b', r'\brest\b', r'\bcrud\b',
    r'\bsql\b', r'\bfetch\b', r'\bwebsocket\b', r'\bdocker\b',
    r'\brate.?limit', r'\btest\s+(?:suite|coverage|file)',
    r'\bversion\s+\d', r'\bv\d+\.\d+', r'\bsemver\b',
    r'\bbetter-sqlite3\b', r'\bjsonwebtoken\b',
    r'\bconfig/', r'\bsrc/', r'\b\.js\b', r'\b\.py\b',
]
_TECH_RE = re.compile('|'.join(_TECH_PATTERNS), re.IGNORECASE)

# Project-associated sessions (from SESSION_TRACKS and PROJECT_SESSIONS)
_RECIPE_SESSIONS = {3, 5, 7, 9, 10, 12, 16, 18}
_PORTFOLIO_SESSIONS = {9, 14}  # session 9 is both portfolio + recipe

# Additional text patterns for project detection (when session info unavailable)
_RECIPE_TEXT_PATTERNS = re.compile(
    r'recipe\s+app|dietary\s+(filter|tag|restrict|preference)|meal\s+plan|'
    r'grocery\s+list|safe\s+for\s+mom|nutrition|recipe\s+sharing|'
    r'recipe\s+card|card\s+layout|recipe\s+search',
    re.IGNORECASE,
)
_PORTFOLIO_TEXT_PATTERNS = re.compile(
    r'portfolio\s+site|portfolio\s+page|linkedin|personal\s+site|'
    r'work\s+history|resume\s+site',
    re.IGNORECASE,
)


def apply_posthoc_tags(workspace: Path) -> dict:
    """Apply is_technical and project tags post-hoc to all nodes in the DB.

    Uses keyword pattern matching on fact text + session_id metadata.
    Returns stats about what was tagged.
    """
    print("=" * 60)
    print("PHASE 4c: POST-HOC PROJECT TAGGING")
    print("=" * 60)

    db_path = workspace / "data" / "memory.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute(
            "SELECT id, name, attributes, session_id FROM nodes WHERE status = 'active'"
        ).fetchall()
        print(f"  Scanning {len(rows)} active nodes")

        tagged_tech = 0
        tagged_project = 0
        already_tagged = 0
        tagged_nodes = 0

        for row in rows:
            node_id = row["id"]
            text = row["name"] or ""
            attrs_raw = row["attributes"]
            session_id = row["session_id"] or ""

            # Parse existing attributes
            if attrs_raw:
                try:
                    attrs = json.loads(attrs_raw) if isinstance(attrs_raw, str) else attrs_raw
                except (json.JSONDecodeError, TypeError):
                    attrs = {}
            else:
                attrs = {}
            # Skip only when both tags already exist.
            # If one tag exists but the other is missing, continue and fill the gap.
            if attrs.get("is_technical") and attrs.get("project"):
                already_tagged += 1
                continue

            # Detect technical content
            is_tech = bool(_TECH_RE.search(text))

            # Detect project from session_id
            project = None
            snum = None
            if session_id and session_id.startswith("session-"):
                try:
                    snum = int(session_id.split("-")[1])
                except (ValueError, IndexError):
                    pass

            if snum:
                if snum in _RECIPE_SESSIONS:
                    project = "recipe-app"
                elif snum in _PORTFOLIO_SESSIONS:
                    project = "portfolio-site"

            # Also check text patterns for project assignment
            if not project:
                if _RECIPE_TEXT_PATTERNS.search(text):
                    project = "recipe-app"
                elif _PORTFOLIO_TEXT_PATTERNS.search(text):
                    project = "portfolio-site"

            # Only mark as technical if BOTH tech pattern matches AND it's project-related
            # This avoids false positives like "Maya tested the hike route" matching \btest\b
            if is_tech and project:
                attrs["is_technical"] = True
                attrs["project"] = project
                tagged_tech += 1
                tagged_project += 1
            elif project and not is_tech:
                # Has project but not technical (e.g., "David wants to use the recipe app")
                attrs["project"] = project
                tagged_project += 1
            elif is_tech and snum and snum in (_RECIPE_SESSIONS | _PORTFOLIO_SESSIONS):
                # Tech pattern in a project session, tag both
                attrs["is_technical"] = True
                proj = "recipe-app" if snum in _RECIPE_SESSIONS else "portfolio-site"
                attrs["project"] = proj
                tagged_tech += 1
                tagged_project += 1
            else:
                continue  # Nothing to tag

            # Update DB
            conn.execute(
                "UPDATE nodes SET attributes = ? WHERE id = ?",
                (json.dumps(attrs), node_id),
            )
            tagged_nodes += 1

        conn.commit()

    untagged = 0
    for row in rows:
        attrs_raw = row["attributes"]
        if not attrs_raw:
            untagged += 1
            continue
        try:
            attrs = json.loads(attrs_raw) if isinstance(attrs_raw, str) else attrs_raw
        except (json.JSONDecodeError, TypeError):
            untagged += 1
            continue
        if not attrs.get("is_technical") and not attrs.get("project"):
            untagged += 1

    print(f"  Tagged: {tagged_tech} technical, {tagged_project} project-associated")
    print(f"  Already tagged: {already_tagged}")
    print(f"  Untagged: {untagged}")
    print()

    return {
        "total_nodes": len(rows),
        "tagged_tech": tagged_tech,
        "tagged_project": tagged_project,
        "already_tagged": already_tagged,
    }


# ---------------------------------------------------------------------------
# Phase 5: Eval with tool use
# ---------------------------------------------------------------------------

def _eval_single_query(
    i: int,
    query: dict,
    eval_context: str,
    workspace: Path,
    api_key: str,
    env: dict,
    eval_model: str,
    context_inject: bool,
    recall_k: int,
    judge_model: str,
    no_judge: bool,
) -> dict:
    """Evaluate a single query. Thread-safe — no shared mutable state."""
    question = query["question"]
    ground_truth = query["ground_truth"]
    query_type = query.get("query_type", "unknown")

    t0 = time.time()
    source_session = query.get("source_session", 20)
    session_date = SESSION_DATES.get(source_session, "2026-05-01")

    prediction, tool_calls, tool_results_log, recall_texts, q_usage = _tool_use_loop(
        question=question,
        eval_context=eval_context,
        workspace=workspace,
        api_key=api_key,
        env=env,
        model=eval_model,
        date_to=session_date,
        max_session=source_session,
        context_inject=context_inject,
        recall_k=recall_k,
        current_date=session_date,
        query_type=query_type,
        query_index=i,
    )
    answer_duration = time.time() - t0

    retrieval_context = "\n\n".join(recall_texts) if recall_texts else ""
    if no_judge:
        label, score = "UNJUDGED", None
        ret_label, ret_score = "UNJUDGED", None
    else:
        label, score = _judge(question, ground_truth, prediction, api_key,
                              judge_model=judge_model, query_type=query_type)
        if query_type in ("non_question", "non_question_sensitive"):
            # Behavioral test — retrieval scoring not applicable
            ret_label, ret_score = "N/A", None
        elif retrieval_context:
            ret_label, ret_score = _judge(
                question, ground_truth, retrieval_context, api_key, judge_model=judge_model)
        else:
            ret_label, ret_score = "WRONG", 0.0

    result = {
        "query_index": i,
        "question": question,
        "ground_truth": ground_truth,
        "prediction": prediction,
        "judge_label": label,
        "score": score,
        "retrieval_label": ret_label,
        "retrieval_score": ret_score,
        "query_type": query_type,
        "recall_difficulty": query.get("recall_difficulty", "unknown"),
        "source_session": query.get("source_session", 0),
        "evidence_sessions": query.get("evidence_sessions", []),
        "tool_calls": tool_calls,
        "tool_results_summary": tool_results_log,
        "answer_duration_s": round(answer_duration, 2),
        "eval_tokens": q_usage,
    }
    _append_eval_tool_trace(
        workspace=workspace,
        event={
            "event": "query_end",
            "backend": _BACKEND,
            "query_index": i,
            "query_type": query_type,
            "source_session": source_session,
            "judge_label": result.get("judge_label"),
            "score": result.get("score"),
            "retrieval_label": result.get("retrieval_label"),
            "tool_calls_count": len(tool_calls or []),
            "answer_chars": len(prediction or ""),
            "answer_duration_s": round(answer_duration, 2),
        },
    )
    return result


def run_eval(workspace: Path, api_key: str, max_sessions: Optional[int] = None,
             eval_model: str = "claude-sonnet-4-5-20250929",
             context_inject: bool = False,
             judge_model: str = "gpt-4o-mini",
             no_judge: bool = False,
             resume_eval: bool = False,
             resume_from_query: Optional[int] = None,
             only_query: Optional[int] = None) -> List[dict]:
    """Evaluate using tool use (memory_recall + search_project_docs).

    If context_inject=True, pre-recalls memories and injects them into the
    system prompt before the model sees the question. Tools remain available
    for follow-up queries.

    Supports parallel evaluation via _PARALLEL_WORKERS > 1.
    """
    mode_label = "CONTEXT INJECT + TOOL USE" if context_inject else "TOOL USE"
    parallel = _PARALLEL_WORKERS
    print("=" * 60)
    print(f"PHASE 5: EVALUATION ({eval_model} + {mode_label})")
    if parallel > 1:
        print(f"  Parallel workers: {parallel}")
    print("=" * 60)

    # Load reviews and queries (arc sessions only for eval — fillers have no eval queries)
    assets_dir = _resolve_assets_dir()
    sessions_to_load = list(range(1, max_sessions + 1)) if max_sessions else None
    arc_reviews = load_all_reviews(assets_dir, sessions=sessions_to_load)
    all_queries = get_all_eval_queries(arc_reviews)
    total_queries = len(all_queries)
    query_set_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "question": q.get("question", ""),
                    "ground_truth": q.get("ground_truth", ""),
                    "query_type": q.get("query_type", ""),
                    "source_session": q.get("source_session", 0),
                }
                for q in all_queries
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    print(f"  {total_queries} queries to evaluate (from {len(arc_reviews)} sessions)")
    indexed_queries = list(enumerate(all_queries))
    if only_query is not None:
        if only_query < 0 or only_query >= total_queries:
            raise ValueError(f"--only-query must be in [0, {total_queries - 1}], got {only_query}")
        indexed_queries = [indexed_queries[only_query]]
        print(f"  Restricting eval to query index {only_query}: {all_queries[only_query]['question'][:80]}...")

    # Build eval context from evolved workspace files
    eval_context = _build_eval_context(workspace)
    print(f"  Eval context: {len(eval_context)} chars ({len(eval_context)//4} est tokens)")

    # Switch DB for recall
    db_path = workspace / "data" / "memory.db"
    env = _make_env(workspace)

    # Compute dynamic K for retrieval limit
    recall_k = _compute_dynamic_k(db_path)
    print(f"  Dynamic K: {recall_k} (from {db_path})")

    progress_path = workspace / "logs" / "eval_progress.json"
    partial_results_path = workspace / "evaluation_results.partial.json"

    results: List[dict] = []
    start_idx = 0
    if resume_from_query is not None:
        start_idx = max(0, int(resume_from_query))
        if start_idx >= total_queries:
            raise ValueError(
                f"--resume-from-query must be in [0, {total_queries - 1}], got {resume_from_query}"
            )
    elif resume_eval:
        progress_hash = None
        if progress_path.exists():
            try:
                progress_data = json.loads(progress_path.read_text())
                progress_hash = progress_data.get("query_set_hash")
            except Exception:
                progress_hash = None
        if progress_hash and progress_hash != query_set_hash:
            print("  Resume eval ignored: query set changed since checkpoint")
        elif progress_hash is None and progress_path.exists():
            print("  Resume eval warning: checkpoint missing query hash, continuing")
        if partial_results_path.exists():
            try:
                loaded = json.loads(partial_results_path.read_text())
                if isinstance(loaded, dict):
                    partial_hash = str(loaded.get("query_set_hash", "") or "")
                    partial_results = loaded.get("results")
                    if not isinstance(partial_results, list):
                        print("  Resume eval ignored: partial checkpoint has invalid results payload")
                    elif partial_hash and partial_hash != query_set_hash:
                        print("  Resume eval ignored: partial results query set hash mismatch")
                    elif progress_hash and progress_hash != query_set_hash:
                        print("  Resume eval ignored: progress hash mismatch")
                    else:
                        results = partial_results
                        start_idx = len(results)
                elif isinstance(loaded, list):
                    # Legacy partial format (unhashed list) is accepted only when
                    # progress hash validates the query set.
                    if progress_hash and progress_hash == query_set_hash:
                        results = loaded
                        start_idx = len(results)
                    else:
                        print("  Resume eval ignored: legacy partial checkpoint is unvalidated")
            except ValueError:
                raise
            except Exception:
                start_idx = 0
    if start_idx > total_queries:
        raise RuntimeError(
            "Resume eval checkpoint exceeds current query set size "
            f"({start_idx} > {total_queries}); clear stale eval checkpoints and retry."
        )
    if start_idx > 0:
        print(f"  Resume eval enabled: starting at query index {start_idx}")
    if only_query is not None and start_idx > only_query:
        raise ValueError(
            f"--resume-from-query ({start_idx}) conflicts with --only-query ({only_query})"
        )

    correct = 0
    partial_count = 0
    wrong = 0
    eval_usage = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "timeouts": 0}
    t_start = time.time()

    # Rebuild running counters from any preloaded results.
    for r in results:
        label = r.get("judge_label")
        if label == "CORRECT":
            correct += 1
        elif label == "PARTIAL":
            partial_count += 1
        elif label == "WRONG":
            wrong += 1

    # Filter queries to evaluate
    pending_queries = [(i, q) for i, q in indexed_queries if i >= start_idx]

    if parallel > 1 and len(pending_queries) > 1:
        # --- Parallel eval path ---
        checkpoint_lock = threading.Lock()
        completed_count = 0
        # Slot-indexed result buffer for ordered checkpointing
        result_buffer = {}

        def _on_result(result_dict):
            """Thread-safe callback when a query completes."""
            nonlocal correct, partial_count, wrong, completed_count
            lbl = result_dict.get("judge_label")
            idx = result_dict.get("query_index")
            if idx is None:
                raise RuntimeError("eval result missing query_index")
            q_usage = result_dict.get("eval_tokens", {})

            with checkpoint_lock:
                eval_usage["input_tokens"] += q_usage.get("input_tokens", 0)
                eval_usage["output_tokens"] += q_usage.get("output_tokens", 0)
                eval_usage["api_calls"] += q_usage.get("api_calls", 0)
                eval_usage["timeouts"] += int(q_usage.get("timeouts", 0) or 0)
                if lbl == "CORRECT":
                    correct += 1
                elif lbl == "PARTIAL":
                    partial_count += 1
                elif lbl == "WRONG":
                    wrong += 1
                completed_count += 1
                result_buffer[idx] = result_dict

                # Flush contiguous completed results to the results list
                # This keeps partial_results ordered for resume compatibility
                next_flush = start_idx + len(results)
                while next_flush in result_buffer:
                    results.append(result_buffer.pop(next_flush))
                    next_flush += 1

                # Prepare checkpoint/log payloads while holding lock.
                partial_payload = {
                    "query_set_hash": query_set_hash,
                    "results": list(results),
                }
                highest_contiguous = start_idx + len(results) - 1 if results else start_idx - 1
                progress_payload = {
                    "last_completed_query": highest_contiguous,
                    "completed_at": datetime.now().isoformat(),
                    "total_queries": total_queries,
                    "query_set_hash": query_set_hash,
                    "parallel_workers": parallel,
                    "completed_count": completed_count,
                }

                scored_so_far = correct + partial_count + wrong
                acc_so_far = (correct + 0.5 * partial_count) / scored_so_far * 100 if scored_so_far > 0 else 0
                tc = result_dict.get("tool_calls", [])
                tools_str = f" tools=[{','.join(tc)}]" if tc else " (no tools)"
                marker = {"CORRECT": "O", "PARTIAL": "~", "WRONG": "X"}.get(lbl, "?")
                qt = result_dict.get("query_type", "")
                q_text = result_dict.get("question", "")[:50]
                if no_judge:
                    status_line = (
                        f"  [{completed_count}/{len(pending_queries)}] {marker} ({qt}) "
                        f"{q_text}...{tools_str} [unjudged]"
                    )
                else:
                    status_line = (
                        f"  [{completed_count}/{len(pending_queries)}] {marker} ({qt}) "
                        f"{q_text}...{tools_str} [{acc_so_far:.1f}%]"
                    )

            # Checkpoint writes outside lock to reduce worker contention.
            _atomic_write_json(partial_results_path, partial_payload)
            _atomic_write_json(progress_path, progress_payload)
            print(status_line)

        print(f"  Launching {len(pending_queries)} queries with {parallel} parallel workers...")
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            current_parallel = parallel
            min_parallel = min(parallel, _EVAL_TIMEOUT_BACKOFF_MIN_PARALLEL)
            timeout_window = deque(maxlen=_EVAL_TIMEOUT_BACKOFF_WINDOW)
            pending_iter = iter(pending_queries)
            in_flight: Dict[Any, int] = {}

            def _submit_more() -> None:
                while len(in_flight) < current_parallel:
                    try:
                        idx, query = next(pending_iter)
                    except StopIteration:
                        break
                    fut = executor.submit(
                        _eval_single_query,
                        idx, query, eval_context, workspace, api_key, env,
                        eval_model, context_inject, recall_k, judge_model, no_judge,
                    )
                    in_flight[fut] = idx

            _submit_more()
            while in_flight:
                done, _ = wait(set(in_flight.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    idx = in_flight.pop(fut)
                    try:
                        result_dict = fut.result()
                        _on_result(result_dict)
                        timeout_events = int((result_dict.get("eval_tokens", {}) or {}).get("timeouts", 0) or 0)
                        timeout_window.append(1 if timeout_events > 0 else 0)
                    except Exception as e:
                        import traceback as _traceback
                        tb = _traceback.format_exc()
                        print(f"  [ERROR] Query {idx} failed: {e}")
                        print(tb)
                        raise RuntimeError(
                            f"Eval query failed hard at index {idx}; aborting run."
                        ) from e

                if (
                    _EVAL_TIMEOUT_BACKOFF_ENABLED
                    and len(timeout_window) == _EVAL_TIMEOUT_BACKOFF_WINDOW
                    and sum(timeout_window) >= _EVAL_TIMEOUT_BACKOFF_THRESHOLD
                    and current_parallel > min_parallel
                ):
                    new_parallel = max(min_parallel, current_parallel - _EVAL_TIMEOUT_BACKOFF_STEP)
                    if new_parallel < current_parallel:
                        print(
                            "  [backoff] timeout density high "
                            f"({sum(timeout_window)}/{_EVAL_TIMEOUT_BACKOFF_WINDOW}); "
                            f"reducing eval workers {current_parallel} -> {new_parallel}"
                        )
                        current_parallel = new_parallel
                        timeout_window.clear()

                _submit_more()

    else:
        # --- Sequential eval path (original) ---
        for i, query in pending_queries:
            result_dict = _eval_single_query(
                i, query, eval_context, workspace, api_key, env,
                eval_model, context_inject, recall_k, judge_model, no_judge,
            )
            q_usage = result_dict.get("eval_tokens", {})
            eval_usage["input_tokens"] += q_usage.get("input_tokens", 0)
            eval_usage["output_tokens"] += q_usage.get("output_tokens", 0)
            eval_usage["api_calls"] += q_usage.get("api_calls", 0)
            eval_usage["timeouts"] += int(q_usage.get("timeouts", 0) or 0)

            lbl = result_dict.get("judge_label")
            if lbl == "CORRECT":
                correct += 1
                marker = "O"
            elif lbl == "PARTIAL":
                partial_count += 1
                marker = "~"
            elif lbl == "WRONG":
                wrong += 1
                marker = "X"
            else:
                marker = "?"

            results.append(result_dict)

            # Persist query-level checkpoint after every evaluated query.
            partial_payload = {
                "query_set_hash": query_set_hash,
                "results": results,
            }
            _atomic_write_json(partial_results_path, partial_payload)
            progress_payload = {
                "last_completed_query": i,
                "completed_at": datetime.now().isoformat(),
                "total_queries": total_queries,
                "query_set_hash": query_set_hash,
            }
            _atomic_write_json(progress_path, progress_payload)

            scored_so_far = correct + partial_count + wrong
            acc_so_far = (correct + 0.5 * partial_count) / scored_so_far * 100 if scored_so_far > 0 else 0
            tool_calls = result_dict.get("tool_calls", [])
            tools_str = f" tools=[{','.join(tool_calls)}]" if tool_calls else " (no tools)"
            question = result_dict["question"]
            query_type = result_dict.get("query_type", "")
            if no_judge:
                print(f"  [{i+1}/{total_queries}] {marker} ({query_type}) "
                      f"{question[:50]}...{tools_str} [unjudged]")
            else:
                print(f"  [{i+1}/{total_queries}] {marker} ({query_type}) "
                      f"{question[:50]}...{tools_str} [{acc_so_far:.1f}%]")

    elapsed = time.time() - t_start
    scored = correct + partial_count + wrong
    accuracy = (correct + 0.5 * partial_count) / scored * 100 if scored > 0 else 0

    # Retrieval-only accuracy
    ret_scored = [r for r in results if r.get("retrieval_label") in ("CORRECT", "PARTIAL", "WRONG")]
    if no_judge:
        print("\n  Evaluation complete: unjudged mode (--no-judge)")
    elif ret_scored:
        ret_c = sum(1 for r in ret_scored if r["retrieval_label"] == "CORRECT")
        ret_p = sum(1 for r in ret_scored if r["retrieval_label"] == "PARTIAL")
        ret_acc = (ret_c + 0.5 * ret_p) / len(ret_scored) * 100
        print(f"\n  Answer accuracy: {accuracy:.1f}% ({correct}C/{partial_count}P/{wrong}W)")
        print(f"  Retrieval accuracy: {ret_acc:.1f}% ({ret_c}C/{ret_p}P/{len(ret_scored)-ret_c-ret_p}W)")
    else:
        print(f"\n  Evaluation complete: {accuracy:.1f}% ({correct}C/{partial_count}P/{wrong}W)")
    total_tok = eval_usage["input_tokens"] + eval_usage["output_tokens"]
    print(f"  Tokens: {eval_usage['input_tokens']:,} in + {eval_usage['output_tokens']:,} out = {total_tok:,}")
    print(f"  API calls: {eval_usage['api_calls']}")
    print(f"  Elapsed: {elapsed:.1f}s ({parallel} workers)")

    # Attach usage summary to results for later saving
    if results:
        results[0].setdefault("_eval_usage_summary", eval_usage)
    # Keep resume artifacts until caller finishes writing score artifacts.
    # This preserves recoverability across crashes between eval and scoring.
    return results


def run_fc_baseline(
    api_key: str,
    answer_model: str = "claude-opus-4-6",
    max_sessions: Optional[int] = None,
    results_dir: Optional[Path] = None,
    judge_model: str = "gpt-4o-mini",
    compact_threshold_tokens: int = 180_000,
    context_window_tokens: int = 200_000,
    max_history_share: float = 0.5,
    compaction_parts: int = 2,
    resume_from_query: Optional[int] = None,
    resume_eval: bool = False,
) -> List[dict]:
    """Full-context baseline: answer questions with all transcripts in context."""
    print("=" * 60)
    print(f"FULL-CONTEXT BASELINE ({answer_model})")
    print("=" * 60)

    # Load all sessions (arc + filler for context), but eval queries from arc only
    reviews = _load_reviews(max_sessions)
    assets_dir = _resolve_assets_dir()
    sessions_to_load = list(range(1, max_sessions + 1)) if max_sessions else None
    arc_reviews = load_all_reviews(assets_dir, sessions=sessions_to_load)
    all_queries = get_all_eval_queries(arc_reviews)
    query_set_hash = hashlib.sha256(
        json.dumps(
            [
                {
                    "question": q.get("question", ""),
                    "ground_truth": q.get("ground_truth", ""),
                    "query_type": q.get("query_type", ""),
                    "source_session": q.get("source_session", 0),
                }
                for q in all_queries
            ],
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    print(f"  {len(all_queries)} queries, {len(reviews)} sessions")

    # Build full transcript context (includes fillers if present)
    transcript_parts = []
    for review in reviews:
        snum = review.session_num
        date = _get_session_date(review)
        if snum < 0:
            label = f"F{abs(snum):03d} (Filler)"
        else:
            track_label = "Personal" if review.track == 1 else "Project"
            label = f"Session {snum} ({track_label})"
        transcript = format_transcript_for_extraction(review)
        if transcript.strip():
            transcript_parts.append(
                f"=== {label} — {date} ===\n{transcript}"
            )
    full_transcripts = "\n\n".join(transcript_parts)
    est_tokens = len(full_transcripts) // 4
    print(f"  Transcript context: {len(full_transcripts)} chars (~{est_tokens} tokens)")

    # OpenClaw-aligned compaction: iterative oldest-first pruning + staged summary.
    # Compact when transcript crosses threshold to keep FC-L runnable.
    if est_tokens > compact_threshold_tokens:
        print(
            f"  Context exceeded {compact_threshold_tokens} tokens; "
            "applying FC compaction before eval."
        )
        budget_tokens = max(1, int(context_window_tokens * max_history_share))
        parts = max(2, int(compaction_parts))
        kept_parts = list(transcript_parts)
        dropped_parts: List[str] = []
        dropped_chunks = 0

        def _parts_tokens(parts_list: List[str]) -> int:
            return sum(max(1, len(p) // 4) for p in parts_list if p)

        def _split_parts_by_token_share(parts_list: List[str], n_parts: int) -> List[List[str]]:
            if not parts_list:
                return []
            n_parts = max(1, min(int(n_parts), len(parts_list)))
            if n_parts <= 1:
                return [parts_list]
            total = _parts_tokens(parts_list)
            target = max(1, total // n_parts)
            out: List[List[str]] = []
            cur: List[str] = []
            cur_tok = 0
            for p in parts_list:
                t = max(1, len(p) // 4)
                if out and len(out) >= n_parts - 1:
                    cur.append(p)
                    cur_tok += t
                    continue
                if cur and cur_tok + t > target:
                    out.append(cur)
                    cur = [p]
                    cur_tok = t
                else:
                    cur.append(p)
                    cur_tok += t
            if cur:
                out.append(cur)
            return out

        while len(kept_parts) > 1 and _parts_tokens(kept_parts) > budget_tokens:
            splits = _split_parts_by_token_share(kept_parts, parts)
            if len(splits) <= 1:
                break
            dropped_parts.extend(splits[0])
            kept_parts = [p for chunk in splits[1:] for p in chunk]
            dropped_chunks += 1

        summary = ""
        summary_usage = {"input_tokens": 0, "output_tokens": 0}
        if dropped_parts:
            chunk_limit_tokens = 80_000
            max_summary_tokens = max(
                500,
                _safe_env_int("BENCH_FC_MAX_SUMMARY_TOKENS", 3000, min_value=1),
            )
            max_summary_chars = max_summary_tokens * 4
            chunks: List[str] = []
            cur: List[str] = []
            cur_tok = 0
            for part in dropped_parts:
                t = max(1, len(part) // 4)
                if cur and cur_tok + t > chunk_limit_tokens:
                    chunks.append("\n\n".join(cur))
                    cur = [part]
                    cur_tok = t
                else:
                    cur.append(part)
                    cur_tok += t
            if cur:
                chunks.append("\n\n".join(cur))

            rolling_summary = ""
            for ci, chunk in enumerate(chunks, start=1):
                s_prompt = (
                    "You are OpenClaw's compaction summarizer. Update a rolling compact summary "
                    "of dropped conversation history. Preserve decisions, TODOs, unresolved "
                    "questions, constraints, timeline updates, relationship changes, and key "
                    "facts needed for future continuity. Keep concise, structured bullets."
                )
                u_prompt = (
                    f"Current rolling summary:\n{rolling_summary or '(none)'}\n\n"
                    f"Dropped history chunk {ci}/{len(chunks)}:\n\n{chunk}\n\n"
                    "Return ONLY the updated compact summary."
                )
                s_raw, s_usage = _call_anthropic_cached(
                    s_prompt,
                    u_prompt,
                    "claude-sonnet-4-5-20250929",
                    api_key,
                    max_tokens=1800,
                )
                summary_usage["input_tokens"] += s_usage.get("input_tokens", 0)
                summary_usage["output_tokens"] += s_usage.get("output_tokens", 0)
                rolling_summary = s_raw.strip() or rolling_summary
                if len(rolling_summary) > max_summary_chars:
                    rolling_summary = rolling_summary[:max_summary_chars].rstrip()
            summary = rolling_summary

        recent_text = "\n\n".join(kept_parts).strip()
        full_transcripts = (
            "[FC compaction triggered at token threshold]\n\n"
            "=== Compaction Summary ===\n"
            f"{summary or '(no compacted summary generated)'}\n\n"
            "=== Retained History (verbatim) ===\n"
            f"{recent_text}"
        )
        compacted_tokens = len(full_transcripts) // 4
        print(
            f"  FC compacted context: {len(full_transcripts)} chars "
            f"(~{compacted_tokens} tokens)"
        )
        print(
            "  FC compaction pruning: "
            f"dropped {len(dropped_parts)} session blocks in {dropped_chunks} passes; "
            f"budget {budget_tokens} tokens"
        )
        if summary_usage["input_tokens"] or summary_usage["output_tokens"]:
            print(
                "  FC compaction usage: "
                f"{summary_usage['input_tokens']:,} in + "
                f"{summary_usage['output_tokens']:,} out tokens"
            )

    results = []
    fc_path = None
    if results_dir:
        fc_path = results_dir / f"fc_{answer_model.replace('-', '_')}_results.json"
    correct = 0
    partial_count = 0
    wrong = 0
    fc_usage = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
    t_start = time.time()

    # Resume support: skip already-evaluated queries
    start_idx = 0
    if resume_from_query is not None:
        start_idx = max(0, int(resume_from_query))
        if start_idx >= len(all_queries):
            raise ValueError(
                f"--resume-from-query must be in [0, {len(all_queries) - 1}], got {resume_from_query}"
            )
    elif resume_eval and results_dir:
        if fc_path and fc_path.exists():
            try:
                loaded = json.loads(fc_path.read_text())
                if isinstance(loaded, dict):
                    loaded_hash = str(loaded.get("query_set_hash", "") or "")
                    loaded_results = loaded.get("results")
                    if not isinstance(loaded_results, list):
                        print("  FC resume ignored: invalid checkpoint payload")
                    elif not loaded_hash:
                        print("  FC resume ignored: checkpoint missing query hash")
                    elif loaded_hash != query_set_hash:
                        print("  FC resume ignored: query set changed since checkpoint")
                    else:
                        results = loaded_results
                        start_idx = len(results)
                        if start_idx > len(all_queries):
                            raise RuntimeError(
                                "FC resume results exceed current query set size "
                                f"({start_idx} > {len(all_queries)}); clear stale FC results and retry."
                            )
                elif isinstance(loaded, list):
                    # Legacy FC checkpoint format (list only) is unvalidated.
                    print("  FC resume ignored: legacy checkpoint is unvalidated")
            except RuntimeError:
                raise
            except Exception:
                start_idx = 0
    if start_idx > 0:
        print(f"  FC resume: starting at query index {start_idx}")
        for r in results:
            lbl = r.get("judge_label", "")
            if lbl == "CORRECT":
                correct += 1
            elif lbl == "PARTIAL":
                partial_count += 1
            else:
                wrong += 1

    for i, query in enumerate(all_queries):
        if i < start_idx:
            continue
        question = query["question"]
        ground_truth = query["ground_truth"]
        query_type = query.get("query_type", "unknown")

        # Answer with full context
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on transcripts of your past conversations.\n\n"
            "Answer concisely and accurately. If the conversations don't contain "
            "enough information, say \"I don't have information about that.\""
        )
        user_message = (
            f"Here are transcripts of past conversations with Maya:\n\n"
            f"{full_transcripts}\n\n"
            f"Question: {question}\n\nAnswer:"
        )

        raw_response, usage = _call_anthropic_cached(
            system_prompt, user_message, answer_model, api_key,
            max_tokens=512,
        )
        prediction = raw_response.strip()
        fc_usage["input_tokens"] += usage.get("input_tokens", 0)
        fc_usage["output_tokens"] += usage.get("output_tokens", 0)
        fc_usage["api_calls"] += 1

        # Judge
        label, score = _judge(question, ground_truth, prediction, api_key, judge_model=judge_model, query_type=query_type)

        if label == "CORRECT":
            correct += 1
            marker = "O"
        elif label == "PARTIAL":
            partial_count += 1
            marker = "~"
        else:
            wrong += 1
            marker = "X"

        result = {
            "question": question,
            "ground_truth": ground_truth,
            "prediction": prediction,
            "judge_label": label,
            "score": score,
            "query_type": query_type,
            "recall_difficulty": query.get("recall_difficulty", "unknown"),
            "source_session": query.get("source_session", 0),
        }
        results.append(result)
        if fc_path:
            _atomic_write_json(
                fc_path,
                {
                    "query_set_hash": query_set_hash,
                    "results": results,
                    "partial": True,
                    "last_completed_query": i,
                    "total_queries": len(all_queries),
                },
            )

        scored_so_far = correct + partial_count + wrong
        acc_so_far = (correct + 0.5 * partial_count) / scored_so_far * 100 if scored_so_far > 0 else 0
        print(f"  [{i+1}/{len(all_queries)}] {marker} ({query_type}) "
              f"{question[:50]}... [{acc_so_far:.1f}%]")

    elapsed = time.time() - t_start
    scored = correct + partial_count + wrong
    accuracy = (correct + 0.5 * partial_count) / scored * 100 if scored > 0 else 0

    fc_total = fc_usage["input_tokens"] + fc_usage["output_tokens"]
    costs = _MODEL_COSTS.get(answer_model, _MODEL_COSTS["claude-haiku-4-5-20251001"])
    fc_cost = (fc_usage["input_tokens"] * costs["input"] + fc_usage["output_tokens"] * costs["output"]) / 1_000_000

    print(f"\n  FC Baseline ({answer_model}): {accuracy:.1f}% "
          f"({correct}C/{partial_count}P/{wrong}W) in {elapsed:.1f}s")
    print(f"  Tokens: {fc_usage['input_tokens']:,} in + {fc_usage['output_tokens']:,} out = {fc_total:,}")
    print(f"  Est. cost: ${fc_cost:.2f}")

    # Save results
    if fc_path:
        _atomic_write_json(
            fc_path,
            {
                "query_set_hash": query_set_hash,
                "results": results,
            },
        )
        # Save token usage for FC baseline
        fc_usage_path = results_dir / f"fc_{answer_model.replace('-', '_')}_token_usage.json"
        _atomic_write_json(
            fc_usage_path,
            {
                "eval": {
                    "input_tokens": fc_usage["input_tokens"],
                    "output_tokens": fc_usage["output_tokens"],
                    "total_tokens": fc_total,
                    "api_calls": fc_usage["api_calls"],
                    "model": answer_model,
                    "cost_usd": round(fc_cost, 4),
                },
                "queries": len(results),
                "avg_tokens_per_query": round(fc_total / len(results)) if results else 0,
            },
        )
        print(f"  Saved to {fc_path}")

    return results


def _build_eval_context(workspace: Path) -> str:
    """Build eval system context from evolved core markdowns + project bootstrap files."""
    parts = []

    # Core markdowns (like production: always in context)
    for md in ["SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md"]:
        path = workspace / md
        if path.exists():
            content = path.read_text().strip()
            if content:
                parts.append(f"--- {md} ---\n{content}")

    # Project bootstrap files (like production: extraBootstrapFiles globs)
    for pattern in ["projects/*/TOOLS.md", "projects/*/AGENTS.md"]:
        for f in sorted(workspace.glob(pattern)):
            content = f.read_text().strip()
            if content:
                rel = f.relative_to(workspace)
                parts.append(f"--- {rel} ---\n{content}")

    context = "\n\n".join(parts)

    # Fail fast on obviously degraded context unless disabled.
    if _EVAL_CONTEXT_MIN_CHARS > 0 and len(context) < _EVAL_CONTEXT_MIN_CHARS:
        stats = []
        for md in ["SOUL.md", "USER.md", "MEMORY.md", "TOOLS.md"]:
            p = workspace / md
            if p.exists():
                stats.append(f"{md}={p.stat().st_size}")
        raise RuntimeError(
            "Eval context too small; refusing to run with degraded context. "
            f"chars={len(context)} min={_EVAL_CONTEXT_MIN_CHARS} "
            f"files={' '.join(stats)}"
        )
    return context


def _pre_recall(
    question: str,
    workspace: Path,
    env: dict,
    query_type: Optional[str] = None,
    max_session: Optional[int] = None,
    date_to: Optional[str] = None,
    recall_k: Optional[int] = None,
) -> Tuple[str, str]:
    """Pre-recall memories for a question before the model sees it.

    Returns (recall_text, query_used).

    This only injects memory recall results.  Project docs are NOT pre-injected;
    the eval model must explicitly request them via the search_project_docs tool
    during the tool-use loop (mirrors production behavior).
    """
    recall_result = _tool_memory_recall(
        question,
        workspace,
        env,
        date_to=date_to,
        max_session=max_session,
        recall_k=recall_k,
        query_type=query_type,
    )

    return recall_result, question


def _tool_use_loop(
    question: str,
    eval_context: str,
    workspace: Path,
    api_key: str,
    env: dict,
    max_turns: int = 4,
    model: str = "claude-sonnet-4-5-20250929",
    date_to: Optional[str] = None,
    max_session: Optional[int] = None,
    context_inject: bool = False,
    recall_k: Optional[int] = None,
    current_date: Optional[str] = None,
    query_type: Optional[str] = None,
    query_index: Optional[int] = None,
) -> Tuple[str, List[str], List[str], List[str], dict]:
    """Run model with tool use, executing memory_recall and search_project_docs.

    Routes through Claude Code CLI when _BACKEND == "claude-code".

    If context_inject=True, pre-recalls memories and injects them into the
    system prompt (like Mem0's approach). Tools are still available for
    follow-up queries if the model wants to dig deeper.

    Returns (final_answer, tool_call_names, tool_result_summaries, retrieval_texts, usage_total).
    """
    if _BACKEND == "claude-code":
        return _tool_use_loop_claude_code(
            question, eval_context, workspace, api_key, env,
            max_turns=max_turns, model=model, date_to=date_to,
            max_session=max_session, context_inject=context_inject,
            recall_k=recall_k, current_date=current_date, query_type=query_type,
            query_index=query_index,
        )
    if _BACKEND == "vllm":
        return _tool_use_loop_vllm(
            question, eval_context, workspace, api_key, env,
            max_turns=max_turns, model=model, date_to=date_to,
            max_session=max_session, context_inject=context_inject,
            recall_k=recall_k, current_date=current_date, query_type=query_type,
            query_index=query_index,
        )

    usage_total = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "timeouts": 0}
    content_blocks = []
    tools = [
        {
            "name": "memory_recall",
            "description": (
                "Search the memory database for facts about Maya — personal, project, technical, everything. "
                "For project/implementation questions, pair this with search_project_docs. "
                "Results include dates showing when each fact was recorded. "
                "Use entity names (e.g. 'Maya', 'Liam', 'recipe app') not roles ('the user', 'her boyfriend')."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — use specific names and topics",
                    },
                    "date_from": {
                        "type": "string",
                        "description": "Only return memories from this date onward (YYYY-MM-DD)",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Only return memories up to this date (YYYY-MM-DD)",
                    },
                    "domain": {
                        "type": "object",
                        "description": "Optional domain filter map, e.g. {\"all\": true} or {\"technical\": true}",
                    },
                    "project": {
                        "type": "string",
                        "description": "Optional project filter",
                        "enum": ["recipe-app", "portfolio-site"],
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_project_docs",
            "description": (
                "Search project source code and documentation files. "
                "Use for project/state/implementation details (tests, schema, middleware, versions). "
                "Always specify project name when known."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for project files",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name (recipe-app or portfolio-site)",
                        "enum": ["recipe-app", "portfolio-site"],
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "projects_search",
            "description": (
                "Alias of search_project_docs. Search project source code and documentation files."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for project files",
                    },
                    "project": {
                        "type": "string",
                        "description": "Project name (recipe-app or portfolio-site)",
                        "enum": ["recipe-app", "portfolio-site"],
                    },
                },
                "required": ["query"],
            },
        },
    ]

    # Pre-inject recall results if requested
    injected_context = ""
    tool_call_names = []
    tool_result_summaries = []
    retrieval_texts = []  # Raw recall text for retrieval-only metric

    if context_inject:
        recall_text, query_used = _pre_recall(
            question, workspace, env,
            query_type=query_type,
            max_session=max_session, date_to=date_to,
        )
        if recall_text and "No memories found" not in recall_text:
            injected_context = (
                f"\n\n## Retrieved Memories\n"
                f"Query used: \"{query_used}\"\n\n"
                f"{recall_text}\n"
            )
            tool_call_names.append("memory_recall(pre-inject)")
            tool_result_summaries.append(
                f"pre-inject({query_used[:40]}): {len(recall_text)} chars"
            )
            retrieval_texts.append(recall_text)

    date_anchor = f"Today's date is {current_date}.\n" if current_date else ""

    if context_inject:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations.\n\n"
            f"{date_anchor}"
            "Below are memories retrieved for this question. Use them to answer directly.\n"
            "If the retrieved memories don't have enough info, you can use the tools "
            "to search for more — but try to answer from what's provided first.\n\n"
            "ANSWER RULES:\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If you don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}"
            f"{injected_context}"
        )
    else:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations. Use the available tools "
            "to search your memory before answering.\n\n"
            f"{date_anchor}"
            "ANSWER RULES:\n"
            "- For personal/life questions, start with memory_recall.\n"
            "- For project/technical questions, use both memory_recall and search_project_docs.\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If memory_recall doesn't have enough info, try search_project_docs.\n"
            "- If you still don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}"
        )

    messages = [{"role": "user", "content": question}]

    for turn in range(max_turns):
        # Token budget check: after min 2 turns, stop if budget exceeded
        if _EVAL_TOKEN_BUDGET > 0 and turn >= 2:
            total_used = usage_total["input_tokens"] + usage_total["output_tokens"]
            if total_used >= _EVAL_TOKEN_BUDGET:
                print(f"    Token budget exhausted ({total_used:,} >= {_EVAL_TOKEN_BUDGET:,}) after {turn} turns")
                break

        payload = {
            "model": model,
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": messages,
            "tools": tools,
        }

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )

        data = _anthropic_request_json_with_long_retry(
            req=req,
            timeout=120,
            op_label="anthropic-tool-use",
        )

        # Track token usage
        _usage = data.get("usage", {})
        usage_total["input_tokens"] += _usage.get("input_tokens", 0)
        usage_total["output_tokens"] += _usage.get("output_tokens", 0)
        usage_total["api_calls"] += 1

        # Check stop reason
        stop_reason = data.get("stop_reason", "end_turn")
        content_blocks = data.get("content", [])

        # If model wants to use tools
        if stop_reason == "tool_use":
            # Add assistant message
            messages.append({"role": "assistant", "content": content_blocks})

            # Process tool calls
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    tool_name = block["name"]
                    tool_input = block["input"]
                    tool_id = block["id"]
                    tool_call_names.append(tool_name)

                    # Execute tool (inject session filter for temporal filtering)
                    result_text = _execute_tool(
                        tool_name, tool_input, workspace, env,
                        max_session=max_session, date_to=date_to,
                        query_type=query_type,
                    )
                    _append_eval_tool_trace(
                        workspace=workspace,
                        event={
                            "event": "tool_call",
                            "backend": "api",
                            "query_index": query_index,
                            "query_type": query_type,
                            "turn": turn + 1,
                            "tool": tool_name,
                            "requested_input": tool_input,
                            "executed_input": _normalize_tool_input(tool_name, tool_input),
                            "evidence_refs": _extract_evidence_refs(tool_name, result_text),
                            **_tool_result_trace_payload(result_text),
                        },
                    )
                    tool_result_summaries.append(
                        f"{tool_name}({tool_input.get('query', '')[:40]}): {len(result_text)} chars"
                    )
                    if tool_name == "memory_recall":
                        retrieval_texts.append(result_text)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_text,
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        # Model returned final answer
        text_parts = []
        for block in content_blocks:
            if block.get("type") == "text":
                text_parts.append(block["text"])
        return " ".join(text_parts).strip(), tool_call_names, tool_result_summaries, retrieval_texts, usage_total

    # Exhausted turns — extract whatever text we have
    text_parts = []
    for block in content_blocks:
        if block.get("type") == "text":
            text_parts.append(block["text"])
    return " ".join(text_parts).strip() or "Unable to determine answer.", tool_call_names, tool_result_summaries, retrieval_texts, usage_total


def _tool_use_loop_vllm(
    question: str,
    eval_context: str,
    workspace: Path,
    api_key: str,
    env: dict,
    max_turns: int = 4,
    model: str = "",
    date_to: Optional[str] = None,
    max_session: Optional[int] = None,
    context_inject: bool = False,
    recall_k: Optional[int] = None,
    current_date: Optional[str] = None,
    query_type: Optional[str] = None,
    query_index: Optional[int] = None,
) -> Tuple[str, List[str], List[str], List[str], dict]:
    """Run vLLM model with OpenAI-format tool use for eval queries."""

    usage_total = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "timeouts": 0}
    tool_call_names = []
    tool_result_summaries = []
    retrieval_texts = []

    # OpenAI function calling tool definitions
    tools = [
        {
            "type": "function",
            "function": {
                "name": "memory_recall",
                "description": (
                    "Search the memory database for facts about Maya — personal, project, technical, everything. "
                    "For project/implementation questions, pair this with search_project_docs. "
                    "Use entity names (e.g. 'Maya', 'Liam', 'recipe app') not roles ('the user', 'her boyfriend')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query — use specific names and topics"},
                        "date_from": {"type": "string", "description": "Only return memories from this date onward (YYYY-MM-DD)"},
                        "date_to": {"type": "string", "description": "Only return memories up to this date (YYYY-MM-DD)"},
                        "domain": {
                            "type": "object",
                            "description": "Optional domain filter map, e.g. {\"all\": true} or {\"technical\": true}",
                        },
                        "project": {"type": "string", "description": "Optional project filter", "enum": ["recipe-app", "portfolio-site"]},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_project_docs",
                "description": (
                    "Search project source code and documentation files. "
                    "Use for project/state/implementation details (tests, schema, middleware, versions)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query for project files"},
                        "project": {"type": "string", "description": "Project name", "enum": ["recipe-app", "portfolio-site"]},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "projects_search",
                "description": (
                    "Alias of search_project_docs. Search project source code and documentation files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query for project files"},
                        "project": {"type": "string", "description": "Project name", "enum": ["recipe-app", "portfolio-site"]},
                    },
                    "required": ["query"],
                },
            },
        },
    ]

    # Pre-inject recall results
    injected_context = ""
    if context_inject:
        recall_text, query_used = _pre_recall(
            question, workspace, env, query_type=query_type,
            max_session=max_session, date_to=date_to,
        )
        if recall_text and "No memories found" not in recall_text:
            injected_context = f"\n\n## Retrieved Memories\nQuery used: \"{query_used}\"\n\n{recall_text}\n"
            tool_call_names.append("memory_recall(pre-inject)")
            tool_result_summaries.append(f"pre-inject({query_used[:40]}): {len(recall_text)} chars")
            retrieval_texts.append(recall_text)

    date_anchor = f"Today's date is {current_date}.\n" if current_date else ""

    if context_inject:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations.\n\n"
            f"{date_anchor}"
            "Below are memories retrieved for this question. Use them to answer directly.\n"
            "If the retrieved memories don't have enough info, you can use the tools "
            "to search for more — but try to answer from what's provided first.\n\n"
            "ANSWER RULES:\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If you don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}{injected_context}"
        )
    else:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations. Use the available tools "
            "to search your memory before answering.\n\n"
            f"{date_anchor}"
            "ANSWER RULES:\n"
            "- For personal/life questions, start with memory_recall.\n"
            "- For project/technical questions, use both memory_recall and search_project_docs.\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If memory_recall doesn't have enough info, try search_project_docs.\n"
            "- If you still don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}"
        )

    vllm_model = model or _VLLM_MODEL
    url = _vllm_endpoint(_VLLM_URL, "/chat/completions")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    for turn in range(max_turns):
        # Token budget check: after min 2 turns, stop if budget exceeded
        if _EVAL_TOKEN_BUDGET > 0 and turn >= 2:
            total_used = usage_total["input_tokens"] + usage_total["output_tokens"]
            if total_used >= _EVAL_TOKEN_BUDGET:
                print(f"    Token budget exhausted ({total_used:,} >= {_EVAL_TOKEN_BUDGET:,}) after {turn} turns")
                break

        payload = {
            "model": vllm_model,
            "messages": messages,
            "tools": tools,
            "max_tokens": 2048,
            "temperature": 0.0,
        }

        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )

        last_err: Optional[Exception] = None
        data = None
        for attempt in range(1, _VLLM_RETRY_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=_VLLM_TIMEOUT_S) as resp:
                    data = json.loads(resp.read())
                break
            except HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")[:300]
                except Exception:
                    body = ""
                if e.code == 404:
                    raise RuntimeError(
                        f"vLLM endpoint not found (404) at {url}. "
                        f"Check --vllm-url base (with/without /v1). body={body}"
                    )
                last_err = e
            except (URLError, TimeoutError, OSError) as e:
                last_err = e
            if attempt < _VLLM_RETRY_ATTEMPTS:
                time.sleep(_VLLM_RETRY_BACKOFF_S * attempt)
        if data is None:
            raise RuntimeError(f"vLLM tool-use failed after retries: {last_err}")

        usage_total["input_tokens"] += data.get("usage", {}).get("prompt_tokens", 0)
        usage_total["output_tokens"] += data.get("usage", {}).get("completion_tokens", 0)
        usage_total["api_calls"] += 1

        choice = data["choices"][0]
        msg = choice["message"]
        finish = choice.get("finish_reason", "stop")

        # If model wants to use tools
        if finish == "tool_calls" or msg.get("tool_calls"):
            messages.append(msg)  # Add assistant message with tool_calls

            for tc in msg.get("tool_calls", []):
                fn = tc["function"]
                tool_name = fn["name"]
                try:
                    tool_input = json.loads(fn["arguments"])
                except (json.JSONDecodeError, TypeError):
                    tool_input = {"query": fn.get("arguments", "")}
                tool_call_names.append(tool_name)

                result_text = _execute_tool(
                    tool_name, tool_input, workspace, env,
                    max_session=max_session, date_to=date_to,
                    query_type=query_type,
                )
                _append_eval_tool_trace(
                    workspace=workspace,
                    event={
                        "event": "tool_call",
                        "backend": "vllm",
                        "query_index": query_index,
                        "query_type": query_type,
                        "turn": turn + 1,
                        "tool": tool_name,
                        "requested_input": tool_input,
                        "executed_input": _normalize_tool_input(tool_name, tool_input),
                        "evidence_refs": _extract_evidence_refs(tool_name, result_text),
                        **_tool_result_trace_payload(result_text),
                    },
                )
                tool_result_summaries.append(
                    f"{tool_name}({tool_input.get('query', '')[:40]}): {len(result_text)} chars"
                )
                if tool_name == "memory_recall":
                    retrieval_texts.append(result_text)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
            continue

        # Final answer — strip Qwen3 thinking tags
        answer = msg.get("content", "") or ""
        answer = re.sub(r"<think>[\s\S]*?</think>\s*", "", answer).strip()
        return answer, tool_call_names, tool_result_summaries, retrieval_texts, usage_total

    # Exhausted turns
    last_content = data["choices"][0]["message"].get("content", "") if "data" in locals() else ""
    last_content = re.sub(r"<think>[\s\S]*?</think>\s*", "", last_content).strip()
    return last_content or "Unable to determine answer.", tool_call_names, tool_result_summaries, retrieval_texts, usage_total


def _execute_tool(
    tool_name: str,
    tool_input: dict,
    workspace: Path,
    env: dict,
    max_session: Optional[int] = None,
    date_to: Optional[str] = None,
    query_type: Optional[str] = None,
) -> str:
    """Execute a tool and return the result text.

    max_session: source session number — filters recall to facts from this
    session or earlier to prevent future-state leakage.
    date_to: session date string for project docs temporal note.
    """
    query = tool_input.get("query", "")

    if tool_name == "memory_recall":
        date_from = tool_input.get("date_from")
        model_date_to = tool_input.get("date_to")
        domain = tool_input.get("domain")
        project = tool_input.get("project")
        return _tool_memory_recall(
            query, workspace, env,
            date_from=date_from, date_to=model_date_to,
            max_session=max_session,
            domain=domain,
            project=project,
            query_type=query_type,
        )
    elif tool_name in ("search_project_docs", "projects_search"):
        project = tool_input.get("project")
        return _tool_search_project_docs(
            query, workspace, env, project, date_to=date_to, max_session=max_session
        )
    else:
        return f"Unknown tool: {tool_name}"


def _normalize_tool_input(tool_name: str, tool_input: dict) -> dict:
    """Normalize tool args for deterministic diagnostics."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    out = {"query": str(inp.get("query", "")).strip()}
    if tool_name == "memory_recall":
        for k in ("date_from", "date_to", "project"):
            v = inp.get(k)
            if v not in (None, ""):
                out[k] = str(v).strip()
        domain = inp.get("domain")
        if isinstance(domain, dict):
            out["domain"] = domain
    elif tool_name in ("search_project_docs", "projects_search"):
        v = inp.get("project")
        if v not in (None, ""):
            out["project"] = str(v).strip()
    return out


def _extract_evidence_refs(tool_name: str, result_text: str, max_refs: int = 6) -> List[str]:
    """Extract lightweight evidence refs from tool output for forensics."""
    refs: List[str] = []
    txt = result_text or ""
    if tool_name == "memory_recall":
        refs = re.findall(r"\|ID:([^|]+)\|", txt)
    elif tool_name in ("search_project_docs", "projects_search"):
        refs = re.findall(r"---\s+([^\n]+?)\s+---", txt)
    out: List[str] = []
    seen = set()
    for r in refs:
        rr = str(r).strip()
        if not rr or rr in seen:
            continue
        seen.add(rr)
        out.append(rr)
        if len(out) >= max_refs:
            break
    return out


def _append_eval_tool_trace(workspace: Path, event: dict) -> None:
    """Append tool/eval forensic event to benchmark trace log."""
    try:
        log_path = workspace / "logs" / "eval-tool-trace.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(), **event}
        with _EVAL_TRACE_WRITE_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass


_TOOL_TRACE_CONTENT_ENABLED = os.environ.get("BENCHMARK_TOOL_TRACE_CONTENT", "1").strip().lower() in {
    "1", "true", "yes", "on"
}
_TOOL_TRACE_MAX_CHARS = max(200, _env_int("BENCHMARK_TOOL_TRACE_MAX_CHARS", 4000))
_RECALL_RUNTIME_LOCK = threading.Lock()
_EVAL_TRACE_WRITE_LOCK = threading.Lock()


def _tool_result_trace_payload(result_text: str) -> dict:
    """Build capped tool-output trace payload for diagnostics."""
    text = result_text or ""
    payload = {
        "result_chars": len(text),
        "result_sha1": hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest(),
        "result_preview": text[:220],
    }
    if not _TOOL_TRACE_CONTENT_ENABLED:
        return payload
    if len(text) <= _TOOL_TRACE_MAX_CHARS:
        payload["result_content"] = text
        payload["result_truncated"] = False
        return payload
    half = _TOOL_TRACE_MAX_CHARS // 2
    payload["result_content_head"] = text[:half]
    payload["result_content_tail"] = text[-half:]
    payload["result_truncated"] = True
    payload["result_capture_chars"] = _TOOL_TRACE_MAX_CHARS
    return payload


def _append_extraction_drop_trace(workspace: Path, event: dict) -> None:
    """Append extraction dedupe/drop reasons (benchmark diagnostics)."""
    try:
        log_path = workspace / "logs" / "extraction-drop-trace.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(), **event}
        with _EVAL_TRACE_WRITE_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _append_project_ingest_trace(workspace: Path, event: dict) -> None:
    """Append project ingest warnings (missing source repos, copy failures)."""
    try:
        log_path = workspace / "logs" / "project-ingest-trace.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(), **event}
        with _EVAL_TRACE_WRITE_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=True) + "\n")
    except Exception:
        pass


def _tool_memory_recall(
    query: str, workspace: Path, env: dict,
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    max_session: Optional[int] = None,
    recall_k: Optional[int] = None,
    domain: Optional[dict] = None,
    project: Optional[str] = None,
    query_type: Optional[str] = None,
) -> str:
    """Execute memory_recall directly via Python API.

    This avoids brittle parsing of CLI text output and filters by node metadata.
    """
    import inspect
    import sqlite3 as _sqlite3

    base_k = recall_k or _compute_dynamic_k(workspace / "data" / "memory.db")
    # Keep recall limit aligned with production behavior; avoid benchmark-only over-fetching.
    limit = base_k
    domain_filter: dict
    if isinstance(domain, dict):
        domain_filter = {
            str(k).strip().lower(): bool(v)
            for k, v in domain.items()
            if str(k).strip()
        }
        if not domain_filter:
            domain_filter = {"all": True}
    else:
        domain_filter = {"all": True}
    legacy_scope = "any"
    if domain_filter.get("technical") and not domain_filter.get("all", False):
        legacy_scope = "technical"
    elif domain_filter.get("personal") and not domain_filter.get("all", False):
        legacy_scope = "personal"
    owner_id = str(os.environ.get("BENCH_OWNER_ID", "maya")).strip() or "maya"
    with _RECALL_RUNTIME_LOCK:
        # memory_graph reads DB path at import/runtime via process-global state.
        # Serialize this section to avoid thread races under parallel eval.
        prev_memory_db_path = os.environ.get("MEMORY_DB_PATH")
        prev_quaid_home = os.environ.get("QUAID_HOME")
        prev_workspace = os.environ.get("CLAWDBOT_WORKSPACE")
        prev_anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        prev_claude_oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        os.environ["MEMORY_DB_PATH"] = str(workspace / "data" / "memory.db")
        os.environ["QUAID_HOME"] = str(workspace)
        os.environ["CLAWDBOT_WORKSPACE"] = str(workspace)
        # In-process recall may route via fastReasoning anthropic provider (HyDE).
        # Hydrate runtime auth from prepared subprocess env when launch env scrubbed it.
        hydrated_anthropic_key = str(env.get("ANTHROPIC_API_KEY", "") or "").strip()
        if hydrated_anthropic_key:
            os.environ["ANTHROPIC_API_KEY"] = hydrated_anthropic_key
        hydrated_oauth = str(env.get("CLAUDE_CODE_OAUTH_TOKEN", "") or "").strip()
        if hydrated_oauth:
            os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = hydrated_oauth
        try:
            if str(_QUAID_DIR) not in sys.path:
                sys.path.insert(0, str(_QUAID_DIR))
            try:
                # Legacy flat layout
                from memory_graph import recall as _recall
            except ImportError:
                # Refactored layout
                from datastore.memorydb.memory_graph import recall as _recall

            params = inspect.signature(_recall).parameters
            recall_kwargs = {
                "query": query,
                "limit": limit,
                "owner_id": owner_id,
            }
            if "date_from" in params:
                recall_kwargs["date_from"] = date_from
            if "date_to" in params:
                recall_kwargs["date_to"] = date_to
            if "domain" in params:
                recall_kwargs["domain"] = domain_filter
            elif "technical_scope" in params:
                recall_kwargs["technical_scope"] = legacy_scope
            if "project" in params and project:
                recall_kwargs["project"] = str(project).strip()
            memories = _recall(**recall_kwargs) or []
        finally:
            if prev_memory_db_path is None:
                os.environ.pop("MEMORY_DB_PATH", None)
            else:
                os.environ["MEMORY_DB_PATH"] = prev_memory_db_path
            if prev_quaid_home is None:
                os.environ.pop("QUAID_HOME", None)
            else:
                os.environ["QUAID_HOME"] = prev_quaid_home
            if prev_workspace is None:
                os.environ.pop("CLAWDBOT_WORKSPACE", None)
            else:
                os.environ["CLAWDBOT_WORKSPACE"] = prev_workspace
            if prev_anthropic_api_key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev_anthropic_api_key
            if prev_claude_oauth is None:
                os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            else:
                os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = prev_claude_oauth
    if not memories:
        return "No memories found."

    filtered = memories
    if max_session is not None:
        db_path = workspace / "data" / "memory.db"
        with _sqlite3.connect(str(db_path)) as conn:
            kept: List[dict] = []
            for mem in memories:
                node_id = mem.get("id")
                if not node_id:
                    continue
                row = conn.execute(
                    "SELECT session_id, type FROM nodes WHERE id = ?",
                    (node_id,),
                ).fetchone()
                if not row:
                    continue
                session_id, node_type = row
                if session_id:
                    try:
                        sess_num = int(str(session_id).replace("session-", ""))
                        if sess_num <= max_session:
                            kept.append(mem)
                    except ValueError:
                        kept.append(mem)
                else:
                    if node_type in ("Person", "Place", "Organization"):
                        kept.append(mem)
            filtered = kept
        if not filtered:
            return "No memories found for this time period."

    lines: List[str] = []
    for i, mem in enumerate(filtered, 1):
        text = str(mem.get("text", "")).strip()
        if not text:
            continue
        node_id = mem.get("id", "unknown")
        sim = mem.get("similarity", 0.0)
        created = mem.get("created_at", "")
        speaker = str(mem.get("speaker", "") or "").strip()
        line = f"{i}. {text} |ID:{node_id}|"
        try:
            line += f" |SIM:{float(sim):.3f}|"
        except (TypeError, ValueError):
            pass
        if speaker:
            line += f" |SPK:{speaker}|"
        if created:
            line += f" |DATE:{created}|"
        lines.append(line)
    return "\n".join(lines) if lines else "No memories found."


def _tool_search_project_docs(
    query: str, workspace: Path, env: dict,
    project: Optional[str] = None,
    date_to: Optional[str] = None,
    max_session: Optional[int] = None,
) -> str:
    """Search project docs via checkpoint docs search (runner-only wrapper)."""
    cmd = _docs_search_cmd(query, project=project)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_RAG_TIMEOUT_S,
            cwd=str(_QUAID_DIR), env=env,
        )
        output = (result.stdout or "").strip()
        if not output:
            return "No project documentation found."
        return output
    except Exception:
        return "No project documentation found."


# Mem0's exact ACCURACY_PROMPT from mem0ai/mem0/evaluation/metrics/llm_judge.py
# Using this verbatim is required for peer-review-valid comparison with LoCoMo results.
_JUDGE_PROMPT = (
    "Your task is to label an answer to a question as 'CORRECT' or 'WRONG'. "
    "You will be given the following data:\n"
    " (1) a question (posed by one user to another user),\n"
    " (2) a 'gold' (ground truth) answer,\n"
    " (3) a generated answer\n"
    "which you will score as CORRECT/WRONG.\n\n"
    "The point of the question is to ask about something one user should know "
    "about the other user based on their prior conversations.\n"
    "The gold answer will usually be a concise and short answer that includes "
    "the referenced topic, for example:\n"
    "Question: Do you remember what I got the last time I went to Hawaii?\n"
    "Gold answer: A shell necklace\n"
    "The generated answer might be much longer, but you should be generous "
    "with your grading - as long as it touches on the same topic as the gold "
    "answer, it should be counted as CORRECT.\n\n"
    "For time related questions, the gold answer will be a specific date, month, "
    "year, etc. The generated answer might be much longer or use relative time "
    "references (like \"last Tuesday\" or \"next month\"), but you should be "
    "generous with your grading - as long as it refers to the same date or time "
    "period as the gold answer, it should be counted as CORRECT. Even if the "
    "format differs (e.g., \"May 7th\" vs \"7 May\"), consider it CORRECT if "
    "it's the same date.\n\n"
    "Now it's time for the real question:\n"
    "Question: {question}\n"
    "Gold answer: {ground_truth}\n"
    "Generated answer: {prediction}\n\n"
    "First, provide a short (one sentence) explanation of your reasoning, "
    "then finish with CORRECT or WRONG.\n"
    "Do NOT include both CORRECT and WRONG in your response, or it will break "
    "the evaluation script.\n\n"
    "Just return the label CORRECT or WRONG in a json format with the key as \"label\"."
)

# Separate judge prompt for non_question queries.
# These test that the system responds conversationally WITHOUT volunteering
# personal memories unprompted. The standard factual accuracy prompt doesn't
# fit — we're evaluating behavioral restraint, not factual correctness.
_NON_QUESTION_JUDGE_PROMPT = (
    "Your task is to evaluate whether an AI assistant correctly handled a "
    "conversational message. Label the response as 'CORRECT' or 'WRONG'.\n\n"
    "Context: The AI has a memory system storing personal facts about the user. "
    "The expected behavior depends on the message type:\n"
    "- Casual greetings/small talk: respond naturally, show contextual warmth\n"
    "- Explicit recall requests ('what do you know about me', 'catch me up'): "
    "give a thorough, comprehensive response using memory — this is the user asking\n\n"
    "Expected behavior: {ground_truth}\n\n"
    "A CORRECT response:\n"
    "- Matches the expected behavior described above\n"
    "- For casual messages: responds naturally, may reference non-sensitive topics\n"
    "- For explicit recall requests: gives a thorough, personalized summary\n"
    "- Shows the assistant knows the user and cares\n\n"
    "A WRONG response:\n"
    "- For casual messages: volunteers sensitive/private info unprompted, or gives "
    "a robotic/generic response with no personalization\n"
    "- For explicit recall requests: refuses to share known information, gives a "
    "vague/empty response, or fails to demonstrate memory depth\n"
    "- Contradicts the expected behavior\n\n"
    "User message: {question}\n"
    "AI response: {prediction}\n\n"
    "First, provide a short (one sentence) explanation of your reasoning, "
    "then finish with CORRECT or WRONG.\n"
    "Do NOT include both CORRECT and WRONG in your response.\n\n"
    "Just return the label CORRECT or WRONG in a json format with the key as \"label\"."
)


# Cost per 1M tokens (Feb 2026)
_MODEL_COSTS = {
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "claude-sonnet-4-5-20250929": {"input": 3.00, "output": 15.00},
    "claude-opus-4-6": {"input": 15.00, "output": 75.00},
}


def _save_token_usage(results: list, workspace: Path, eval_model: str, suffix: str = ""):
    """Save aggregated token usage to token_usage(.debug).json."""
    eval_in = sum(r.get("eval_tokens", {}).get("input_tokens", 0) for r in results)
    eval_out = sum(r.get("eval_tokens", {}).get("output_tokens", 0) for r in results)
    eval_calls = sum(r.get("eval_tokens", {}).get("api_calls", 0) for r in results)

    costs = _MODEL_COSTS.get(eval_model, _MODEL_COSTS["claude-haiku-4-5-20251001"])
    eval_cost = (eval_in * costs["input"] + eval_out * costs["output"]) / 1_000_000

    usage = {
        "eval": {
            "input_tokens": eval_in,
            "output_tokens": eval_out,
            "total_tokens": eval_in + eval_out,
            "api_calls": eval_calls,
            "model": eval_model,
            "cost_usd": round(eval_cost, 4),
        },
        "queries": len(results),
        "avg_tokens_per_query": round((eval_in + eval_out) / len(results)) if results else 0,
    }

    token_usage_path = workspace / f"token_usage{suffix}.json"
    _atomic_write_json(token_usage_path, usage)
    print(f"  Token usage saved to {token_usage_path}")


def _run_eval_forensics(workspace: Path, suffix: str = "") -> None:
    """Run deterministic eval forensics report generation (best-effort)."""
    benchmark_root = _PROJECT_DIR
    script = benchmark_root / "scripts" / "eval-forensics.py"
    if not script.exists():
        print(f"  WARN: forensics script not found: {script}")
        return

    out_path = workspace / f"eval_forensics{suffix}.json"
    cmd = [
        sys.executable,
        str(script),
        "--run-dir",
        str(workspace),
        "--out-json",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode == 0:
            print(f"  Eval forensics saved to {out_path}")
            summary = (proc.stdout or "").strip().splitlines()
            if summary:
                print(f"  Forensics: {summary[-1]}")
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"  WARN: eval forensics failed (rc={proc.returncode}): {err[:300]}")
    except Exception as e:
        print(f"  WARN: eval forensics execution error: {e}")


def _judge(
    question: str,
    ground_truth: str,
    prediction: str,
    api_key: str,
    judge_model: str = "gpt-4o-mini",
    query_type: str = "",
) -> Tuple[str, float]:
    """Judge prediction against ground truth.

    Args:
        judge_model: "gpt-4o-mini" (default, cross-vendor) or "haiku" (Claude).
        query_type: query type string; "non_question" uses a behavioral prompt.
    """
    if not prediction or prediction.strip().lower() in ("", "n/a"):
        return "WRONG", 0.0
    if prediction.strip().lower().startswith("error:"):
        return "WRONG", 0.0

    if query_type in ("non_question", "non_question_sensitive"):
        prompt = _NON_QUESTION_JUDGE_PROMPT.format(
            question=question,
            prediction=prediction,
            ground_truth=ground_truth,
        )
    else:
        prompt = _JUDGE_PROMPT.format(
            question=question,
            ground_truth=ground_truth,
            prediction=prediction,
        )

    if judge_model == "vllm":
        label, score = _judge_vllm(prompt)
    elif judge_model == "gpt-4o-mini":
        label, score = _judge_openai(prompt)
    else:
        label, score = _judge_anthropic(prompt, api_key)
    if label == "ERROR":
        raise RuntimeError("Judge returned ERROR after retries")
    return label, score


def _judge_openai(prompt: str) -> Tuple[str, float]:
    """Call OpenAI judge for scoring.

    Backend selection:
    - OPENAI_JUDGE_BACKEND=api (default): use direct OpenAI API
    - OPENAI_JUDGE_BACKEND=api: use direct OpenAI API
    - OPENAI_JUDGE_BACKEND=codex: use `codex exec` CLI
    """
    backend = os.environ.get("OPENAI_JUDGE_BACKEND", "api").strip().lower()
    if backend == "codex":
        if shutil.which("codex"):
            return _judge_openai_codex(prompt)
        # Bench reliability guard: if codex CLI is unavailable, fall back to API
        # instead of hard-failing all judged queries.
        return _judge_openai_api(prompt)
    return _judge_openai_api(prompt)


def _judge_openai_api(prompt: str) -> Tuple[str, float]:
    """Direct OpenAI API judge (legacy path)."""
    openai_key = _get_openai_key()
    if not openai_key:
        raise RuntimeError("OPENAI_API_KEY not found — cannot use OpenAI API judge")

    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 150,  # Room for reasoning sentence + JSON label
        "temperature": 0.0,
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {openai_key}",
        },
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"].strip()
            return _parse_judge_label(text)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if attempt == _API_RETRY_ATTEMPTS:
                break
            time.sleep(_API_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"Judge error (openai) after {_API_RETRY_ATTEMPTS} attempts: {last_err}"
    )


def _judge_openai_codex(prompt: str) -> Tuple[str, float]:
    """OpenAI judge via Codex CLI (preferred path)."""
    codex_model = os.environ.get("OPENAI_JUDGE_CODEX_MODEL", "").strip()
    last_err: Optional[Exception] = None
    for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
        with tempfile.NamedTemporaryFile(prefix="codex-judge-", suffix=".txt", delete=False) as tmp:
            out_path = tmp.name
        try:
            cmd = ["codex", "exec"]
            if codex_model:
                cmd += ["-m", codex_model]
            cmd += [
                "--skip-git-repo-check",
                "--output-last-message", out_path,
                prompt,
            ]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                raise RuntimeError(f"codex exec failed (rc={result.returncode}): {stderr[:400]}")

            text = Path(out_path).read_text().strip() if Path(out_path).exists() else ""
            return _parse_judge_label(text)
        except Exception as e:
            last_err = e
            if attempt == _API_RETRY_ATTEMPTS:
                break
            time.sleep(_API_RETRY_BACKOFF_S * attempt)
        finally:
            try:
                Path(out_path).unlink(missing_ok=True)
            except Exception:
                pass

    raise RuntimeError(
        f"Judge error (openai-codex) after {_API_RETRY_ATTEMPTS} attempts: {last_err}"
    )


def _judge_vllm(prompt: str) -> Tuple[str, float]:
    """Judge via local vLLM (OpenAI-compatible API)."""
    last_err: Optional[Exception] = None
    for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
        try:
            text, _usage = _call_vllm(
                system_prompt="You are an evaluation judge. Answer CORRECT or WRONG.",
                user_message=prompt,
                model=_VLLM_MODEL,
                base_url=_VLLM_URL,
                max_tokens=150,
            )
            return _parse_judge_label(text.strip())
        except Exception as e:
            last_err = e
            if attempt == _API_RETRY_ATTEMPTS:
                break
            time.sleep(_API_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"Judge error (vllm) after {_API_RETRY_ATTEMPTS} attempts: {last_err}"
    )


def _judge_anthropic(prompt: str, api_key: str) -> Tuple[str, float]:
    """Call Claude Haiku for judging."""
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, _API_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            text = data.get("content", [{}])[0].get("text", "").strip()
            return _parse_judge_label(text)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if attempt == _API_RETRY_ATTEMPTS:
                break
            time.sleep(_API_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(
        f"Judge error (anthropic) after {_API_RETRY_ATTEMPTS} attempts: {last_err}"
    )


def _parse_judge_label(text: str) -> Tuple[str, float]:
    """Parse judge response text into (label, score).

    Handles both raw text and JSON {"label": "CORRECT"} format.
    Binary scoring only (CORRECT/WRONG) matching Mem0's ACCURACY_PROMPT.
    """
    # Try JSON parse first
    try:
        data = json.loads(text)
        label = data.get("label", "").upper()
        if label == "CORRECT":
            return "CORRECT", 1.0
        elif label == "WRONG":
            return "WRONG", 0.0
    except (json.JSONDecodeError, AttributeError):
        pass

    # Fall back to text scanning (reasoning + label)
    upper = text.upper()
    # Check for WRONG first — prompt says "do NOT include both"
    # but if both appear, last one wins (reasoning may mention the other)
    last_correct = upper.rfind("CORRECT")
    last_wrong = upper.rfind("WRONG")
    if last_correct > last_wrong:
        return "CORRECT", 1.0
    elif last_wrong > last_correct:
        return "WRONG", 0.0
    elif "CORRECT" in upper:
        return "CORRECT", 1.0
    elif "WRONG" in upper:
        return "WRONG", 0.0
    else:
        return "ERROR", 0.0


# ---------------------------------------------------------------------------
# Tier 5: Emotional Intelligence judge (3-point rubric, Sonnet)
# ---------------------------------------------------------------------------

_TIER5_JUDGE_PROMPT = (
    "You are evaluating an AI assistant's emotional intelligence — specifically, "
    "whether it demonstrates sensitivity to the emotional context and interpersonal "
    "dynamics behind a user's message, beyond just factual accuracy.\n\n"
    "You will be given:\n"
    "1. A message from the user (Maya)\n"
    "2. Context about the emotional sensitivity involved\n"
    "3. The AI assistant's response\n"
    "4. Scoring criteria for this specific query\n\n"
    "Score the response:\n"
    "- **2 (Emotionally Aware):** The response demonstrates clear awareness of the "
    "emotional subtext, handles sensitive information appropriately, matches the "
    "user's emotional register, and responds in a way that would make the user "
    "feel genuinely understood.\n"
    "- **1 (Factually Correct, Emotionally Flat):** The response contains accurate "
    "information but misses the emotional weight, violates a soft boundary, fails "
    "to match the user's tone, or treats an emotional moment as purely informational.\n"
    "- **0 (Harmful or Wrong):** The response violates a clear boundary (surfaces "
    "private information inappropriately, reopens resolved conflicts), fundamentally "
    "misreads the emotional situation, or lacks the context to engage meaningfully.\n\n"
    "Note: A score of 1 is not bad — it represents a competent but impersonal "
    "response. The difference between 1 and 2 is the difference between a helpful "
    "system and one that feels like it actually knows you.\n\n"
    "User message: {prompt}\n"
    "Emotional context: {sensitivity_context}\n"
    "Scoring criteria:\n"
    "  Score 2: {rubric_2}\n"
    "  Score 1: {rubric_1}\n"
    "  Score 0: {rubric_0}\n"
    "AI response: {response}\n\n"
    "Provide a 2-3 sentence explanation of your reasoning, then output your score "
    "as a JSON object: {{\"score\": 0|1|2}}"
)


def _judge_tier5(
    query: dict,
    prediction: str,
    api_key: str,
) -> Tuple[int, str]:
    """Judge a Tier 5 EI query using Sonnet (3-point rubric).

    Returns (score, reasoning) where score is 0, 1, or 2.
    """
    if not prediction or prediction.strip().lower() in ("", "n/a"):
        return 0, "No response"

    rubric = query.get("rubric", {})
    prompt = _TIER5_JUDGE_PROMPT.format(
        prompt=query["question"],
        sensitivity_context=query.get("sensitivity_context", ""),
        rubric_2=rubric.get("score_2", ""),
        rubric_1=rubric.get("score_1", ""),
        rubric_0=rubric.get("score_0", ""),
        response=prediction,
    )

    try:
        t5_model = _VLLM_MODEL if _BACKEND == "vllm" else "claude-sonnet-4-5-20250929"
        text, _usage = _call_anthropic_cached(
            system_prompt="You are an evaluation judge. Score responses on a 0-2 scale.",
            user_message=prompt,
            model=t5_model,
            api_key=api_key,
            max_tokens=300,
        )

        # Parse score from JSON
        try:
            start = text.rfind("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                score_data = json.loads(text[start:end + 1])
                score = int(score_data.get("score", 0))
                score = max(0, min(2, score))  # Clamp to 0-2
            else:
                raise ValueError("no valid json object boundaries")
        except (json.JSONDecodeError, ValueError, TypeError):
            # Fallback: look for "score": N pattern
            import re as _re
            m = _re.search(r'"score"\s*:\s*(\d)', text)
            if m:
                score = max(0, min(2, int(m.group(1))))
            else:
                score = 0

        # Extract reasoning (everything before JSON)
        reasoning = text[:text.rfind("{")].strip() if "{" in text else text
        return score, reasoning

    except Exception as e:
        print(f"    Tier 5 judge error: {e}")
        return 0, f"Error: {e}"


def run_tier5_eval(
    workspace: Path,
    api_key: str,
    eval_model: str = "claude-sonnet-4-5-20250929",
    context_inject: bool = False,
) -> List[dict]:
    """Run Tier 5 Emotional Intelligence evaluation.

    Uses Sonnet for both answering and judging (3-point rubric).
    Returns list of result dicts with ei_score (0/1/2).
    """
    from dataset import get_tier5_queries

    print("=" * 60)
    print(f"TIER 5: EMOTIONAL INTELLIGENCE ({eval_model})")
    print("=" * 60)

    queries = get_tier5_queries()
    print(f"  {len(queries)} EI queries")

    eval_context = _build_eval_context(workspace)
    db_path = workspace / "data" / "memory.db"
    env = _make_env(workspace)

    results = []
    total_score = 0
    max_possible = len(queries) * 2

    for i, query in enumerate(queries):
        question = query["question"]

        t0 = time.time()
        # Use the same tool-use loop as Tiers 1-4
        prediction, tool_calls, tool_results_log, recall_texts, q_usage = _tool_use_loop(
            question=question,
            eval_context=eval_context,
            workspace=workspace,
            api_key=api_key,
            env=env,
            model=eval_model,
            date_to="2026-05-01",
            max_session=20,
            context_inject=context_inject,
        )
        answer_duration = time.time() - t0

        # Judge with Tier 5 rubric (Sonnet)
        ei_score, reasoning = _judge_tier5(query, prediction, api_key)
        total_score += ei_score

        marker = {2: "++", 1: "~", 0: "X"}[ei_score]
        running_pct = total_score / ((i + 1) * 2) * 100
        print(f"  [{i+1}/{len(queries)}] {marker} ({ei_score}/2) {query.get('ei_id', '')} "
              f"{question[:50]}... [{running_pct:.0f}%]")

        results.append({
            "ei_id": query.get("ei_id", f"EI-{i+1:02d}"),
            "ei_category": query.get("ei_category", ""),
            "question": question,
            "prediction": prediction,
            "ei_score": ei_score,
            "reasoning": reasoning,
            "sensitivity_context": query.get("sensitivity_context", ""),
            "rubric": query.get("rubric", {}),
            "tool_calls": tool_calls,
            "answer_duration_s": round(answer_duration, 2),
            "eval_tokens": q_usage,
        })

    pct = total_score / max_possible * 100 if max_possible > 0 else 0
    print(f"\n{'=' * 60}")
    print(f"Tier 5 Score: {total_score}/{max_possible} ({pct:.1f}%)")
    print(f"{'=' * 60}")

    # Category breakdown
    from collections import defaultdict
    by_cat = defaultdict(lambda: {"total": 0, "max": 0, "count": 0})
    for r in results:
        cat = r["ei_category"]
        by_cat[cat]["total"] += r["ei_score"]
        by_cat[cat]["max"] += 2
        by_cat[cat]["count"] += 1
    print(f"\n{'Category':<30} {'Score':>8} {'Pct':>6}")
    print(f"{'─' * 50}")
    for cat, s in sorted(by_cat.items()):
        cat_pct = s["total"] / s["max"] * 100 if s["max"] > 0 else 0
        print(f"{cat:<30} {s['total']:>3}/{s['max']:<3} {cat_pct:>5.0f}%")

    return results


def run_tier5_fc_baseline(
    api_key: str,
    answer_model: str = "claude-sonnet-4-5-20250929",
    max_sessions: Optional[int] = None,
    results_dir: Optional[Path] = None,
    compact_threshold_tokens: int = 180_000,
    context_window_tokens: int = 200_000,
    max_history_share: float = 0.5,
    compaction_parts: int = 2,
) -> List[dict]:
    """Full-context Tier 5 baseline: answer EI queries with all transcripts."""
    from collections import defaultdict
    from dataset import get_tier5_queries

    print("=" * 60)
    print(f"TIER 5 FC BASELINE ({answer_model})")
    print("=" * 60)

    queries = get_tier5_queries()
    reviews = _load_reviews(max_sessions)

    # Build full transcript context
    transcript_parts = []
    for review in reviews:
        snum = review.session_num
        date = _get_session_date(review)
        track_label = "Personal" if review.track == 1 else "Project"
        transcript = format_transcript_for_extraction(review)
        if transcript.strip():
            transcript_parts.append(
                f"=== Session {snum} ({track_label}) — {date} ===\n{transcript}"
            )
    full_transcripts = "\n\n".join(transcript_parts)
    print(f"  {len(queries)} EI queries, {len(reviews)} sessions")
    est_tokens = len(full_transcripts) // 4
    print(f"  Transcript context: {len(full_transcripts)} chars (~{est_tokens} tokens)")

    # Keep Tier-5 FC behavior aligned with FC baseline compaction semantics.
    if est_tokens > compact_threshold_tokens:
        print(
            f"  Context exceeded {compact_threshold_tokens} tokens; "
            "applying FC compaction before Tier 5 eval."
        )
        budget_tokens = max(1, int(context_window_tokens * max_history_share))
        parts = max(2, int(compaction_parts))
        kept_parts = list(transcript_parts)
        dropped_parts: List[str] = []
        dropped_chunks = 0

        def _parts_tokens(parts_list: List[str]) -> int:
            return sum(max(1, len(p) // 4) for p in parts_list if p)

        def _split_parts_by_token_share(parts_list: List[str], n_parts: int) -> List[List[str]]:
            if not parts_list:
                return []
            n_parts = max(1, min(int(n_parts), len(parts_list)))
            if n_parts <= 1:
                return [parts_list]
            total = _parts_tokens(parts_list)
            target = max(1, total // n_parts)
            out: List[List[str]] = []
            cur: List[str] = []
            cur_tok = 0
            for p in parts_list:
                t = max(1, len(p) // 4)
                if out and len(out) >= n_parts - 1:
                    cur.append(p)
                    cur_tok += t
                    continue
                if cur and cur_tok + t > target:
                    out.append(cur)
                    cur = [p]
                    cur_tok = t
                else:
                    cur.append(p)
                    cur_tok += t
            if cur:
                out.append(cur)
            return out

        while len(kept_parts) > 1 and _parts_tokens(kept_parts) > budget_tokens:
            splits = _split_parts_by_token_share(kept_parts, parts)
            if len(splits) <= 1:
                break
            dropped_parts.extend(splits[0])
            kept_parts = [p for chunk in splits[1:] for p in chunk]
            dropped_chunks += 1

        summary = ""
        summary_usage = {"input_tokens": 0, "output_tokens": 0}
        if dropped_parts:
            chunk_limit_tokens = 80_000
            max_summary_tokens = max(
                500,
                _safe_env_int("BENCH_FC_MAX_SUMMARY_TOKENS", 3000, min_value=1),
            )
            max_summary_chars = max_summary_tokens * 4
            chunks: List[str] = []
            cur: List[str] = []
            cur_tok = 0
            for part in dropped_parts:
                t = max(1, len(part) // 4)
                if cur and cur_tok + t > chunk_limit_tokens:
                    chunks.append("\n\n".join(cur))
                    cur = [part]
                    cur_tok = t
                else:
                    cur.append(part)
                    cur_tok += t
            if cur:
                chunks.append("\n\n".join(cur))

            rolling_summary = ""
            for ci, chunk in enumerate(chunks, start=1):
                s_prompt = (
                    "You are OpenClaw's compaction summarizer. Update a rolling compact summary "
                    "of dropped conversation history. Preserve decisions, TODOs, unresolved "
                    "questions, constraints, timeline updates, relationship changes, and key "
                    "facts needed for future continuity. Keep concise, structured bullets."
                )
                u_prompt = (
                    f"Current rolling summary:\n{rolling_summary or '(none)'}\n\n"
                    f"Dropped history chunk {ci}/{len(chunks)}:\n\n{chunk}\n\n"
                    "Return ONLY the updated compact summary."
                )
                s_raw, s_usage = _call_anthropic_cached(
                    s_prompt,
                    u_prompt,
                    "claude-sonnet-4-5-20250929",
                    api_key,
                    max_tokens=1800,
                )
                summary_usage["input_tokens"] += s_usage.get("input_tokens", 0)
                summary_usage["output_tokens"] += s_usage.get("output_tokens", 0)
                rolling_summary = s_raw.strip() or rolling_summary
                if len(rolling_summary) > max_summary_chars:
                    rolling_summary = rolling_summary[:max_summary_chars].rstrip()
            summary = rolling_summary

        recent_text = "\n\n".join(kept_parts).strip()
        full_transcripts = (
            "[FC compaction triggered at token threshold]\n\n"
            "=== Compaction Summary ===\n"
            f"{summary or '(no compacted summary generated)'}\n\n"
            "=== Retained History (verbatim) ===\n"
            f"{recent_text}"
        )
        compacted_tokens = len(full_transcripts) // 4
        print(
            f"  FC compacted context: {len(full_transcripts)} chars "
            f"(~{compacted_tokens} tokens)"
        )
        print(
            "  FC compaction pruning: "
            f"dropped {len(dropped_parts)} session blocks in {dropped_chunks} passes; "
            f"budget {budget_tokens} tokens"
        )
        if summary_usage["input_tokens"] or summary_usage["output_tokens"]:
            print(
                "  FC compaction usage: "
                f"{summary_usage['input_tokens']:,} in + "
                f"{summary_usage['output_tokens']:,} out tokens"
            )

    results = []
    total_score = 0
    max_possible = len(queries) * 2

    for i, query in enumerate(queries):
        question = query["question"]

        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on transcripts of your past conversations.\n\n"
            "Answer naturally and conversationally. Pay attention to emotional "
            "context, sensitivities, and interpersonal dynamics."
        )
        user_message = (
            f"Here are transcripts of past conversations with Maya:\n\n"
            f"{full_transcripts}\n\n"
            f"Question: {question}\n\nAnswer:"
        )

        try:
            raw_response, usage = _call_anthropic_cached(
                system_prompt, user_message, answer_model, api_key,
                max_tokens=512,
            )
            prediction = raw_response.strip()
        except Exception as e:
            prediction = f"Error: {e}"

        ei_score, reasoning = _judge_tier5(query, prediction, api_key)
        total_score += ei_score

        marker = {2: "++", 1: "~", 0: "X"}[ei_score]
        running_pct = total_score / ((i + 1) * 2) * 100
        print(f"  [{i+1}/{len(queries)}] {marker} ({ei_score}/2) {query.get('ei_id', '')} "
              f"{question[:50]}... [{running_pct:.0f}%]")

        results.append({
            "ei_id": query.get("ei_id", f"EI-{i+1:02d}"),
            "ei_category": query.get("ei_category", ""),
            "question": question,
            "prediction": prediction,
            "ei_score": ei_score,
            "reasoning": reasoning,
            "sensitivity_context": query.get("sensitivity_context", ""),
            "rubric": query.get("rubric", {}),
        })

    pct = total_score / max_possible * 100 if max_possible > 0 else 0
    print(f"\nTier 5 FC Score: {total_score}/{max_possible} ({pct:.1f}%)")

    # Category breakdown
    by_cat = defaultdict(lambda: {"total": 0, "max": 0, "count": 0})
    for r in results:
        cat = r["ei_category"]
        by_cat[cat]["total"] += r["ei_score"]
        by_cat[cat]["max"] += 2
        by_cat[cat]["count"] += 1
    print(f"\n{'Category':<30} {'Score':>8} {'Pct':>6}")
    print(f"{'─' * 50}")
    for cat, s in sorted(by_cat.items()):
        cat_pct = s["total"] / s["max"] * 100 if s["max"] > 0 else 0
        print(f"{cat:<30} {s['total']:>3}/{s['max']:<3} {cat_pct:>5.0f}%")

    # Save
    if results_dir:
        results_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(results_dir / "tier5_results.json", results)
        print(f"\nSaved to {results_dir / 'tier5_results.json'}")

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_env_key(env_path: Path, key: str) -> Optional[str]:
    """Read KEY from a .env-style file with basic shell parsing."""
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("export "):
                stripped = stripped[len("export "):].strip()
            if "=" not in stripped:
                continue
            name, raw_val = stripped.split("=", 1)
            if name.strip() != key:
                continue
            try:
                parts = shlex.split(raw_val, comments=True, posix=True)
            except ValueError:
                parts = [raw_val]
            return parts[0] if parts else ""
    except OSError:
        return None
    return None


def _read_claude_oauth_token() -> str:
    """Best-effort read of Claude Code OAuth token for fail-hard subprocess paths."""
    token = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if token:
        return token
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        token = str(((data.get("claudeAiOauth") or {}).get("accessToken")) or "").strip()
        if token:
            return token
    except Exception:
        pass
    return ""


def _make_env(workspace: Path) -> dict:
    """Build env dict for subprocess calls pointing at the benchmark workspace."""
    env = os.environ.copy()
    workspace = workspace.resolve()

    # Refactored checkpoint layout runs janitor/memory scripts from nested paths
    # (e.g. core/lifecycle/janitor.py). Ensure imports like `lib.*` resolve.
    py_paths = [str(_QUAID_DIR)]
    existing_py = env.get("PYTHONPATH", "")
    if existing_py:
        py_paths.append(existing_py)
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    env["CLAWDBOT_WORKSPACE"] = str(workspace)
    env["MEMORY_DB_PATH"] = str(workspace / "data" / "memory.db")
    env["QUAID_DISABLE_NOTIFICATIONS"] = "1"
    env["QUAID_BENCHMARK_MODE"] = "1"
    # Ensure standalone adapter resolves quaid_home to the run workspace
    env["QUAID_HOME"] = str(workspace)
    # Avoid accidental OAuth token routing in benchmark subprocesses.
    for leaked in [
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "CLAUDE_OAUTH_TOKEN",
    ]:
        env.pop(leaked, None)
    if _BACKEND == "claude-code":
        env.pop("CLAUDECODE", None)  # Allow nested invocation
    elif _BACKEND == "vllm":
        env["OPENAI_COMPATIBLE_BASE_URL"] = _VLLM_URL
    # Mixed profile uses anthropic for fast-tier janitor/review while deep tier
    # still goes through claude -p (which scrubs ANTHROPIC_API_KEY locally).
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        env_search = [
            _DIR.parent.parent / ".env",  # benchmark root
            Path.home() / ".env",         # home dir
            Path.home() / "clawd" / ".env",  # clawd dir (Spark layout)
        ]
        for env_path in env_search:
            api_key = _read_env_key(env_path, "ANTHROPIC_API_KEY")
            if api_key:
                break
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    oauth_token = _read_claude_oauth_token()
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    else:
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    return env


def _get_api_key() -> str:
    """Get Anthropic API key from env or .env file."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key
    for env_path in [
        _CLAWD / ".env",
        _DIR.parent.parent / ".env",          # benchmark root
        Path.home() / ".env",                 # home dir
        Path.home() / "clawd" / ".env",       # legacy clawd layout
        Path.home() / "clawd-benchmark" / ".env",  # Spark benchmark layout
        Path.home() / ".openclaw" / ".env",   # openclaw runtime
    ]:
        found = _read_env_key(env_path, "ANTHROPIC_API_KEY")
        if found:
            return found
    print("ERROR: ANTHROPIC_API_KEY not found", file=sys.stderr)
    sys.exit(1)


def _get_openai_key() -> Optional[str]:
    """Get OpenAI API key from env or .env file."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        return api_key
    for env_path in [
        _CLAWD / ".env",
        _DIR.parent.parent / ".env",          # benchmark root
        Path.home() / ".env",                 # home dir
        Path.home() / "clawd" / ".env",       # legacy clawd layout
        Path.home() / "clawd-benchmark" / ".env",  # Spark benchmark layout
        Path.home() / ".openclaw" / ".env",   # openclaw runtime
    ]:
        found = _read_env_key(env_path, "OPENAI_API_KEY")
        if found:
            return found
    return None


_BACKEND = "claude-code"  # Default to claude-code (free via subscription)
_QUAID_PROVIDER_PROFILE = "mixed"  # mixed|anthropic (vllm forces openai-compatible)
_FILLER_DIR: Optional[Path] = None  # Set in main() to load filler sessions (L scale)
_VLLM_URL = "http://localhost:8000"  # vLLM server base URL
_VLLM_MODEL = ""  # Auto-detected or set via --vllm-model
_PARALLEL_WORKERS = 6  # Number of parallel workers (set via --parallel)
_EVAL_TOKEN_BUDGET = 0  # Max tokens per eval query (0 = unlimited). Min 2 turns always run.


def _read_http_error(e: HTTPError) -> str:
    """Best-effort decode of HTTPError body for retry diagnostics."""
    try:
        body = e.read()
        if isinstance(body, bytes):
            return body.decode("utf-8", errors="ignore")
    except Exception:
        pass
    return str(e)


def _anthropic_request_json_with_long_retry(
    req: urllib.request.Request,
    timeout: int,
    op_label: str,
) -> dict:
    """Anthropic API call with long retry window for benchmark durability.

    Retries transient failures (429/5xx/network/timeouts) for a long window so
    long-running jobs don't fail near completion due to temporary usage caps.
    """
    start = time.monotonic()
    attempt = 0
    last_err: Optional[Exception] = None
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            last_err = e
            status = e.code
            body = _read_http_error(e)
            transient = status in {408, 409, 425, 429, 500, 502, 503, 504, 529}
            if not transient:
                raise RuntimeError(
                    f"{op_label}: non-retriable Anthropic HTTP {status}: {body[:400]}"
                ) from e
            elapsed = time.monotonic() - start
            if elapsed >= _ANTHROPIC_LONG_RETRY_MAX_SECONDS:
                raise RuntimeError(
                    f"{op_label}: exhausted long retry window "
                    f"({_ANTHROPIC_LONG_RETRY_MAX_SECONDS}s) after {attempt} attempts; "
                    f"last HTTP {status}: {body[:400]}"
                ) from e
            sleep_s = min(
                _ANTHROPIC_LONG_RETRY_BASE_SECONDS * min(attempt, 20),
                _ANTHROPIC_LONG_RETRY_MAX_BACKOFF_SECONDS,
            )
            print(
                f"  [retry] {op_label}: Anthropic HTTP {status} on attempt {attempt}; "
                f"sleeping {sleep_s:.0f}s (elapsed {elapsed/60:.1f}m)"
            )
            time.sleep(sleep_s)
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
            elapsed = time.monotonic() - start
            if elapsed >= _ANTHROPIC_LONG_RETRY_MAX_SECONDS:
                raise RuntimeError(
                    f"{op_label}: exhausted long retry window "
                    f"({_ANTHROPIC_LONG_RETRY_MAX_SECONDS}s) after {attempt} attempts; "
                    f"last error: {e}"
                ) from e
            sleep_s = min(
                _ANTHROPIC_LONG_RETRY_BASE_SECONDS * min(attempt, 20),
                _ANTHROPIC_LONG_RETRY_MAX_BACKOFF_SECONDS,
            )
            print(
                f"  [retry] {op_label}: transient error on attempt {attempt}: {e}; "
                f"sleeping {sleep_s:.0f}s (elapsed {elapsed/60:.1f}m)"
            )
            time.sleep(sleep_s)
        except Exception as e:
            last_err = e
            raise RuntimeError(f"{op_label}: unexpected error: {e}") from e

    raise RuntimeError(f"{op_label}: failed after retries: {last_err}")


def _resolve_assets_dir() -> Path:
    """Resolve AgentLife assets directory.

    Requires AGENTLIFE_ASSETS_DIR env var or validates workspace-relative
    candidates contain session review files.
    """
    candidates = []
    env_path = os.environ.get("AGENTLIFE_ASSETS_DIR")
    if env_path:
        candidates.append(Path(env_path))
    # Workspace/repo-relative fallbacks
    candidates.append(_CLAWD / "assets")
    candidates.append(_CLAWD / "benchmark-assets")
    candidates.append(_DIR.parent.parent / "assets")
    candidates.append(_DIR.parent / "data" / "transcripts-original")

    for c in candidates:
        if c.exists() and list(c.glob("session-*-review-*.txt")):
            return c
    raise FileNotFoundError(
        "AgentLife assets directory not found (need session-*-review-*.txt files). "
        f"Set AGENTLIFE_ASSETS_DIR to the directory containing session review files. "
        f"Searched: {[str(c) for c in candidates]}"
    )


def _call_anthropic_cached(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str,
    max_tokens: int = 8192,
) -> Tuple[str, dict]:
    """Call LLM — routes through Claude Code, direct API, or vLLM based on _BACKEND."""
    if _BACKEND == "vllm":
        # Cap max_tokens for vLLM to avoid exceeding context window
        vllm_max = min(max_tokens, 4096)
        return _call_vllm(system_prompt, user_message, _VLLM_MODEL, _VLLM_URL, vllm_max)
    if _BACKEND == "claude-code":
        return _call_claude_code(system_prompt, user_message, model, api_key, max_tokens)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
        ],
        "messages": [{"role": "user", "content": user_message}],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "prompt-caching-2024-07-31",
        },
    )

    data = _anthropic_request_json_with_long_retry(
        req=req,
        timeout=300,
        op_label="anthropic-api",
    )

    text = data.get("content", [{}])[0].get("text", "").strip()
    usage = data.get("usage", {})
    return text, usage


def _call_vllm(
    system_prompt: str,
    user_message: str,
    model: str,
    base_url: str,
    max_tokens: int = 8192,
) -> Tuple[str, dict]:
    """Call vLLM's OpenAI-compatible chat completions endpoint."""
    url = _vllm_endpoint(base_url, "/chat/completions")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )

    last_err: Optional[Exception] = None
    for attempt in range(1, _VLLM_RETRY_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=_VLLM_TIMEOUT_S) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"] or ""
            # Strip Qwen3 thinking tags (model emits <think>...</think> wrapper)
            text = re.sub(r"<think>[\s\S]*?</think>\s*", "", text).strip()
            usage = {
                "input_tokens": data.get("usage", {}).get("prompt_tokens", 0),
                "output_tokens": data.get("usage", {}).get("completion_tokens", 0),
            }
            return text, usage
        except HTTPError as e:
            if e.code == 404:
                detail = ""
                try:
                    detail = (e.read() or b"").decode("utf-8", errors="ignore")[:400]
                except Exception:
                    detail = ""
                raise RuntimeError(
                    f"vLLM endpoint 404 at {url}. "
                    f"Check --vllm-url base path (with/without /v1). Detail: {detail}"
                ) from e
            last_err = e
            if attempt == _VLLM_RETRY_ATTEMPTS:
                break
            time.sleep(_VLLM_RETRY_BACKOFF_S * attempt)
        except (URLError, TimeoutError, OSError) as e:
            last_err = e
            if attempt == _VLLM_RETRY_ATTEMPTS:
                break
            time.sleep(_VLLM_RETRY_BACKOFF_S * attempt)
    raise RuntimeError(f"vLLM call failed after {_VLLM_RETRY_ATTEMPTS} attempts: {last_err}")


def _call_claude_code(
    system_prompt: str,
    user_message: str,
    model: str,
    api_key: str = "",  # unused, kept for signature compat
    max_tokens: int = 8192,
) -> Tuple[str, dict]:
    """Call Claude via Claude Code CLI (uses subscription, not API key).

    Routes LLM calls through Claude Code's included usage instead of
    billing to the Anthropic API directly.
    """
    model_alias = {
        "claude-sonnet-4-5-20250929": "sonnet",
        "claude-opus-4-6": "opus",
        "claude-haiku-4-5-20251001": "haiku",
    }.get(model, model)

    cmd = [
        "claude", "-p",
        "--model", model_alias,
        "--output-format", "json",
        "--no-session-persistence",
        "--system-prompt", system_prompt,
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)  # Allow nested invocation
    env.pop("ANTHROPIC_API_KEY", None)  # Don't leak API key; use CLI subscription auth
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    # Always prefer the active Claude CLI session auth over inherited env token.
    # This avoids accidentally pinning benchmark runs to a stale/wrong account.
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    # Optional token injection from .env (disabled by default).
    # Prefer the active `claude` CLI auth session unless explicitly forced.
    if os.environ.get("CLAUDE_CODE_FORCE_ENV_TOKEN", "0") == "1" and "CLAUDE_CODE_OAUTH_TOKEN" not in env:
        for env_path in [
            str(_CLAWD / ".env"),
            os.path.expanduser("~/.env"),
        ]:
            token = _read_env_key(Path(env_path), "CLAUDE_CODE_OAUTH_TOKEN")
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
                if "CLAUDE_CODE_OAUTH_TOKEN" in env:
                    break

    last_error = None
    timeout_events = 0
    data = None
    for attempt in range(1, _CLAUDE_CODE_RETRY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                cmd, input=user_message, capture_output=True, text=True,
                timeout=_CLAUDE_CODE_TIMEOUT_S, env=env,
                cwd="/tmp",  # Avoid loading CLAUDE.md project context
            )
        except subprocess.TimeoutExpired as e:
            timeout_events += 1
            last_error = RuntimeError(
                f"Claude Code timed out after {_CLAUDE_CODE_TIMEOUT_S}s (attempt {attempt}): {e}"
            )
            if attempt < _CLAUDE_CODE_RETRY_ATTEMPTS:
                time.sleep(_CLAUDE_CODE_RETRY_BACKOFF_S * attempt)
                continue
            break
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                break
            except json.JSONDecodeError as e:
                last_error = RuntimeError(
                    f"Claude Code returned non-JSON output (attempt {attempt}): "
                    f"{result.stdout[:300]}"
                )
        else:
            stderr_snip = (result.stderr or "").strip()[:300]
            stdout_snip = (result.stdout or "").strip()[:300]
            last_error = RuntimeError(
                f"Claude Code failed rc={result.returncode} (attempt {attempt}) "
                f"stderr='{stderr_snip}' stdout='{stdout_snip}'"
            )
        if attempt < _CLAUDE_CODE_RETRY_ATTEMPTS:
            time.sleep(_CLAUDE_CODE_RETRY_BACKOFF_S * attempt)
    if data is None:
        raise last_error if last_error else RuntimeError("Claude Code failed with unknown error")

    text = data.get("result", "").strip()

    # Aggregate token usage across models
    usage = {"input_tokens": 0, "output_tokens": 0, "timeouts": timeout_events}
    for _m, u in data.get("modelUsage", {}).items():
        usage["input_tokens"] += u.get("inputTokens", 0) + u.get("cacheReadInputTokens", 0) + u.get("cacheCreationInputTokens", 0)
        usage["output_tokens"] += u.get("outputTokens", 0)

    return text, usage


def _tool_use_loop_claude_code(
    question: str,
    eval_context: str,
    workspace: Path,
    api_key: str,  # unused
    env: dict,
    max_turns: int = 4,
    model: str = "claude-sonnet-4-5-20250929",
    date_to: Optional[str] = None,
    max_session: Optional[int] = None,
    context_inject: bool = False,
    recall_k: Optional[int] = None,
    current_date: Optional[str] = None,
    query_type: Optional[str] = None,
    query_index: Optional[int] = None,
) -> Tuple[str, List[str], List[str], List[str], dict]:
    """Eval answer loop using Claude Code with strict harness-executed tools.

    This path intentionally avoids arbitrary Bash execution. The model can only
    request tools via a JSON action protocol; the harness executes tools via
    _execute_tool and feeds results back on subsequent turns.
    """
    usage_total = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0, "timeouts": 0}
    tool_call_names = []
    tool_result_summaries = []
    retrieval_texts = []

    # Pre-inject recall results (Python/subprocess, no LLM cost)
    injected_context = ""
    if context_inject:
        recall_text, query_used = _pre_recall(
            question, workspace, env,
            query_type=query_type,
            max_session=max_session, date_to=date_to,
            recall_k=recall_k,
        )
        if recall_text and "No memories found" not in recall_text:
            injected_context = (
                f"\n\n## Retrieved Memories\n"
                f"Query used: \"{query_used}\"\n\n"
                f"{recall_text}\n"
            )
            tool_call_names.append("memory_recall(pre-inject)")
            tool_result_summaries.append(
                f"pre-inject({query_used[:40]}): {len(recall_text)} chars"
            )
            retrieval_texts.append(recall_text)

    # Build system prompt
    date_anchor = f"Today's date is {current_date}.\n" if current_date else ""
    k = recall_k or _compute_dynamic_k(workspace / "data" / "memory.db")

    tool_protocol = (
        "You can use exactly two tools via JSON actions:\n"
        "1) memory_recall\n"
        "2) search_project_docs (alias: projects_search)\n\n"
        "When you need a tool, output ONLY JSON in this format:\n"
        "{\"action\":\"tool\",\"tool\":\"memory_recall\",\"input\":{\"query\":\"...\"}}\n"
        "or\n"
        "{\"action\":\"tool\",\"tool\":\"search_project_docs\",\"input\":{\"query\":\"...\",\"project\":\"recipe-app\"}}\n\n"
        "memory_recall also accepts optional domain filtering:\n"
        "{\"action\":\"tool\",\"tool\":\"memory_recall\",\"input\":{\"query\":\"...\",\"domain\":{\"technical\":true}}}\n\n"
        "When ready to answer, output ONLY JSON in this format:\n"
        "{\"action\":\"final\",\"answer\":\"...\"}\n\n"
        "Rules:\n"
        "- Never output shell commands.\n"
        "- Never output markdown fences.\n"
        "- Keep tool queries concise and entity-focused.\n"
        f"- memory_recall limit is approximately {k}.\n"
    )

    if context_inject:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations.\n\n"
            f"{date_anchor}"
            f"{tool_protocol}\n"
            "Below are memories retrieved for this question. Use them to answer directly.\n"
            "ANSWER RULES:\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If you don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}"
            f"{injected_context}"
        )
    else:
        system_prompt = (
            "You are an AI assistant answering questions about a user named Maya "
            "based on your memory of past conversations. Search your memory before answering.\n\n"
            f"{date_anchor}"
            f"{tool_protocol}\n"
            "ANSWER RULES:\n"
            "- Be thorough — include specific names, numbers, dates, and details from memory.\n"
            "- State facts directly. Do not add narrative or caveats.\n"
            "- If asked about a state at a specific time, use memory dates to answer for that time period.\n"
            "- If you don't have enough information, say "
            "\"I don't have information about that.\"\n\n"
            f"{eval_context}"
        )

    # Token budget: estimate ~10K tokens/turn for claude-code, cap turns accordingly
    effective_turns = max_turns
    if _EVAL_TOKEN_BUDGET > 0:
        budget_turns = max(2, _EVAL_TOKEN_BUDGET // 10000)
        effective_turns = min(max_turns, budget_turns)
    tool_history: List[dict] = []

    def _parse_action_json(raw: str) -> Optional[dict]:
        txt = (raw or "").strip()
        if not txt:
            return None
        if txt.startswith("```"):
            txt = re.sub(r"^```(?:json)?\s*", "", txt, flags=re.IGNORECASE)
            txt = re.sub(r"\s*```$", "", txt)
        try:
            obj = json.loads(txt)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{[\s\S]*\}", txt)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    last_raw = ""
    for turn in range(effective_turns):
        tool_state = []
        if tool_history:
            tool_state.append("Tool results so far:")
            for i, h in enumerate(tool_history[-6:], 1):
                tool_state.append(
                    f"{i}. {h['tool']}({h['query'][:80]}) -> {len(h['result'])} chars"
                )
            tool_state.append("")
            tool_state.append("Latest tool result content:")
            tool_state.append(tool_history[-1]["result"])
        else:
            tool_state.append("No tool calls yet.")

        user_message = (
            f"Question:\n{question}\n\n"
            f"Turn {turn + 1}/{effective_turns}\n"
            f"{chr(10).join(tool_state)}\n\n"
            "Return ONLY one JSON object with either a tool action or final answer."
        )

        raw, usage = _call_claude_code(
            system_prompt=system_prompt,
            user_message=user_message,
            model=model,
            api_key=api_key,
            max_tokens=2048,
        )
        usage_total["input_tokens"] += usage.get("input_tokens", 0)
        usage_total["output_tokens"] += usage.get("output_tokens", 0)
        usage_total["api_calls"] += 1
        usage_total["timeouts"] += int(usage.get("timeouts", 0) or 0)
        last_raw = (raw or "").strip()

        action_obj = _parse_action_json(last_raw)
        if not action_obj:
            return (
                last_raw or "I don't have information about that.",
                tool_call_names,
                tool_result_summaries,
                retrieval_texts,
                usage_total,
            )

        action = str(action_obj.get("action", "")).strip().lower()
        if action == "final":
            answer = str(action_obj.get("answer", "")).strip()
            if not answer:
                answer = "I don't have information about that."
            return answer, tool_call_names, tool_result_summaries, retrieval_texts, usage_total

        if action == "tool":
            tool_name = str(
                action_obj.get("tool", action_obj.get("name", ""))
            ).strip()
            tool_input = action_obj.get("input", action_obj.get("arguments", {}))
            if not isinstance(tool_input, dict):
                tool_input = {}
            if tool_name not in ("memory_recall", "search_project_docs", "projects_search"):
                return (
                    "I don't have information about that.",
                    tool_call_names,
                    tool_result_summaries,
                    retrieval_texts,
                    usage_total,
                )
            if not str(tool_input.get("query", "")).strip():
                return (
                    "I don't have information about that.",
                    tool_call_names,
                    tool_result_summaries,
                    retrieval_texts,
                    usage_total,
                )

            result_text = _execute_tool(
                tool_name,
                tool_input,
                workspace,
                env,
                max_session=max_session,
                date_to=date_to,
                query_type=query_type,
            )
            _append_eval_tool_trace(
                workspace=workspace,
                event={
                    "event": "tool_call",
                    "backend": "claude-code",
                    "query_index": query_index,
                    "query_type": query_type,
                    "turn": turn + 1,
                    "tool": tool_name,
                    "requested_input": tool_input,
                    "executed_input": _normalize_tool_input(tool_name, tool_input),
                    "evidence_refs": _extract_evidence_refs(tool_name, result_text),
                    **_tool_result_trace_payload(result_text),
                },
            )
            qtxt = str(tool_input.get("query", ""))
            tool_call_names.append(tool_name)
            tool_result_summaries.append(
                f"{tool_name}({qtxt[:40]}): {len(result_text)} chars"
            )
            if tool_name == "memory_recall":
                retrieval_texts.append(result_text)
            tool_history.append(
                {"tool": tool_name, "query": qtxt, "result": result_text}
            )
            continue

        return (
            "I don't have information about that.",
            tool_call_names,
            tool_result_summaries,
            retrieval_texts,
            usage_total,
        )

    fallback = last_raw or "I don't have information about that."
    return fallback, tool_call_names, tool_result_summaries, retrieval_texts, usage_total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AgentLife Production Benchmark")
    parser.add_argument("--mode", choices=["full", "ingest", "eval", "fc", "per-day"],
                        default="full", help="Run mode (full/ingest use timeout split; fc = full-context baseline)")
    parser.add_argument("--results-dir", type=str,
                        default=str(_PROJECT_DIR / "data" / "results-production"),
                        help="Workspace/results directory")
    parser.add_argument("--model", type=str, default="claude-sonnet-4-5-20250929",
                        help="Extraction model (default: claude-sonnet-4-5-20250929)")
    parser.add_argument("--max-sessions", type=int, default=None,
                        help="Limit to first N sessions (default: all 20)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Force re-extraction")
    parser.add_argument("--resume-extraction", action="store_true",
                        help="Resume extraction from extraction_cache/progress.json if present")
    parser.add_argument("--resume-from-chunk", type=int, default=None,
                        help="Resume extraction from explicit 0-based chunk index")
    parser.add_argument("--only-chunk", type=int, default=None,
                        help="Extract exactly one 0-based chunk index (debug mode)")
    parser.add_argument("--resume-eval", action="store_true",
                        help="Resume eval from evaluation_results.partial.json if present")
    parser.add_argument("--resume-from-query", type=int, default=None,
                        help="Resume eval from explicit 0-based query index")
    parser.add_argument("--only-query", type=int, default=None,
                        help="Evaluate exactly one 0-based query index (debug mode)")
    parser.add_argument("--no-judge", action="store_true",
                        help="Skip scoring/judging and only return model predictions (debug mode)")
    parser.add_argument("--eval-model", type=str, default="claude-sonnet-4-5-20250929",
                        help="Eval answer model (default: claude-sonnet-4-5-20250929)")
    parser.add_argument("--skip-janitor", action="store_true",
                        help="Skip janitor (debug extraction only)")
    parser.add_argument(
        "--janitor-tasks",
        type=str,
        default="benchmark",
        help="Comma-separated janitor tasks, or profile name: benchmark|all (default: benchmark)",
    )
    parser.add_argument("--eval-only", action="store_true",
                        help="Skip to eval phase (reuse existing workspace DB)")
    parser.add_argument("--context-inject", action="store_true", default=False,
                        help="Pre-inject recall results into context (default: disabled)")
    parser.add_argument("--no-context-inject", action="store_true",
                        help="Disable context injection (tool-only mode)")
    parser.add_argument("--judge", type=str, default="gpt-4o-mini",
                        choices=["gpt-4o-mini", "haiku", "vllm"],
                        help="Judge model (default: gpt-4o-mini for cross-vendor fairness)")
    parser.add_argument("--tier5", dest="tier5", action="store_true",
                        help="Run Tier 5 Emotional Intelligence eval (default: enabled)")
    parser.add_argument("--no-tier5", dest="tier5", action="store_false",
                        help="Disable Tier 5 Emotional Intelligence eval")
    parser.add_argument("--backend", type=str, default="claude-code",
                        choices=["claude-code", "api", "vllm"],
                        help="LLM backend: claude-code (free, uses subscription), api (direct Anthropic API), or vllm (local vLLM server)")
    parser.add_argument(
        "--quaid-provider-profile",
        type=str,
        default=os.environ.get("BENCHMARK_QUAID_PROVIDER_PROFILE", "mixed"),
        choices=["mixed", "anthropic"],
        help=(
            "Workspace memory.json provider routing for Quaid internals: "
            "mixed=fast anthropic API + deep claude-code, anthropic=API-only."
        ),
    )
    parser.add_argument("--vllm-url", type=str, default="http://localhost:8000",
                        help="vLLM server base URL (default: http://localhost:8000)")
    parser.add_argument("--vllm-model", type=str, default="",
                        help="vLLM model name (default: auto-detect from /v1/models)")
    parser.add_argument("--parallel", type=int, default=6,
                        help="Number of parallel workers for extraction LLM calls and eval queries (default: 6)")
    parser.add_argument("--eval-token-budget", type=int, default=0,
                        help="Max tokens per eval query across all tool-use turns (0 = unlimited). Min 2 turns always run regardless of budget.")
    parser.add_argument("--filler-dir", type=str, default=None,
                        help="Filler sessions directory (L scale). Without this, runs S scale (arc only).")
    parser.add_argument("--fc-compact-threshold-tokens", type=int, default=180000,
                        help="FC mode: trigger transcript compaction above this estimated token count (default: 180000)")
    parser.add_argument("--fc-context-window-tokens", type=int, default=200000,
                        help="FC mode: context window used for compaction budgeting (default: 200000)")
    parser.add_argument("--fc-max-history-share", type=float, default=0.5,
                        help="FC mode: retained history share after compaction (default: 0.5)")
    parser.add_argument("--fc-compaction-parts", type=int, default=2,
                        help="FC mode: token-share split count for iterative oldest-first pruning (default: 2)")
    parser.add_argument("--fc-tier5-only", action="store_true",
                        help="FC mode: run only Tier-5 FC evaluation (skip 219-query FC baseline)")
    parser.set_defaults(tier5=True)
    args = parser.parse_args()
    if args.max_sessions is not None and args.max_sessions < 1:
        print("ERROR: --max-sessions must be >= 1")
        sys.exit(2)
    if args.parallel < 1 or args.parallel > 64:
        print("ERROR: --parallel must be in [1, 64]")
        sys.exit(2)
    if not (0.0 < args.fc_max_history_share <= 1.0):
        print("ERROR: --fc-max-history-share must be in (0, 1]")
        sys.exit(2)
    if args.no_cache and args.resume_extraction:
        print("WARNING: --no-cache is ignored when --resume-extraction is enabled")

    # Global benchmark policy: no Opus runs.
    for name, value in [("model", args.model), ("eval_model", args.eval_model)]:
        if isinstance(value, str) and "opus" in value.lower():
            print(f"ERROR: Opus is disabled by benchmark policy ({name}={value}).")
            sys.exit(2)

    # Resolve context-inject (--no-context-inject overrides)
    if args.no_context_inject:
        args.context_inject = False

    # Set global filler dir for L-scale runs
    global _FILLER_DIR
    if args.filler_dir:
        _FILLER_DIR = Path(args.filler_dir)
        if not _FILLER_DIR.exists():
            print(f"WARNING: Filler dir not found: {_FILLER_DIR}")
            _FILLER_DIR = None

    workspace = Path(args.results_dir).resolve()
    if args.backend == "api":
        api_key = _get_api_key()
    elif args.backend == "vllm":
        api_key = ""  # Not needed for vLLM
        global _VLLM_URL, _VLLM_MODEL
        _VLLM_URL = args.vllm_url
        # vLLM extraction is currently timeout-prone under concurrent requests on Spark.
        # Default to serial extraction unless explicitly overridden.
        if args.parallel > 1 and os.environ.get("BENCHMARK_VLLM_PARALLEL_OK", "0") != "1":
            print(
                f"  WARNING: forcing --parallel 1 for vLLM stability "
                f"(requested {args.parallel}). Set BENCHMARK_VLLM_PARALLEL_OK=1 to override."
            )
            args.parallel = 1
        # Auto-detect model name from vLLM server
        if args.vllm_model:
            _VLLM_MODEL = args.vllm_model
        else:
            try:
                with urllib.request.urlopen(_vllm_endpoint(_VLLM_URL, "/models"), timeout=10) as resp:
                    models_data = json.loads(resp.read())
                _VLLM_MODEL = models_data["data"][0]["id"]
                print(f"  vLLM model auto-detected: {_VLLM_MODEL}")
            except Exception as e:
                print(f"ERROR: Could not auto-detect vLLM model from {_VLLM_URL}: {e}")
                sys.exit(2)
        # Safety: ensure eval/extraction model IDs actually exist on vLLM.
        # Using Anthropic IDs with backend=vllm causes 404/model-not-found failures.
        if args.model != _VLLM_MODEL:
            print(f"  WARNING: overriding --model '{args.model}' -> '{_VLLM_MODEL}' for vLLM backend")
            args.model = _VLLM_MODEL
        if args.eval_model != _VLLM_MODEL:
            print(f"  WARNING: overriding --eval-model '{args.eval_model}' -> '{_VLLM_MODEL}' for vLLM backend")
            args.eval_model = _VLLM_MODEL
    else:
        api_key = ""  # Not needed for claude-code backend

    # Read Quaid version
    _version_file = _QUAID_DIR / "VERSION"
    _quaid_version = _version_file.read_text().strip() if _version_file.exists() else "unknown"

    scale = "L" if _FILLER_DIR else "S"
    print(f"AgentLife Production Benchmark ({scale}) — Quaid v{_quaid_version}")
    print(f"  Mode: {args.mode}")
    print(f"  Scale: {scale} ({'arc + filler' if _FILLER_DIR else 'arc only'})")
    print(f"  Backend: {args.backend}")
    print(f"  Quaid provider profile: {args.quaid_provider_profile}")
    print(f"  Workspace: {workspace}")
    print(f"  Model: {args.model}")
    print(f"  Max sessions: {args.max_sessions or 'all'}")
    print(f"  No-cache: {args.no_cache}")
    print(f"  Skip-janitor: {args.skip_janitor}")
    print(f"  Janitor tasks: {args.janitor_tasks}")
    print(f"  Judge: {args.judge}")
    print(f"  Tier 5: {args.tier5}")
    print(f"  Parallel: {args.parallel}")
    if _FILLER_DIR:
        print(f"  Filler dir: {_FILLER_DIR}")
    print()

    # Set global parallel worker count and token budget
    global _PARALLEL_WORKERS, _EVAL_TOKEN_BUDGET
    _PARALLEL_WORKERS = max(1, args.parallel)
    _EVAL_TOKEN_BUDGET = max(0, args.eval_token_budget)

    # Ensure workspace directory exists for metadata write
    workspace.mkdir(parents=True, exist_ok=True)

    # Write run metadata
    meta_path = workspace / "run_metadata.json"
    existing_meta = {}
    if meta_path.exists():
        try:
            existing_meta = json.loads(meta_path.read_text())
        except Exception:
            existing_meta = {}
    run_meta = {
        "run_id": workspace.name,
        "quaid_version": _quaid_version,
        "backend": args.backend,
        "vllm_url": args.vllm_url if args.backend == "vllm" else None,
        "vllm_model": args.vllm_model if args.backend == "vllm" else None,
        "quaid_provider_profile": args.quaid_provider_profile,
        "model": args.model,
        "eval_model": args.eval_model,
        "judge": args.judge,
        "scale": scale,
        "max_sessions": args.max_sessions,
        "tier5": args.tier5,
        "parallel": _PARALLEL_WORKERS,
        "eval_token_budget": _EVAL_TOKEN_BUDGET,
        "filler_dir": str(_FILLER_DIR) if _FILLER_DIR else None,
        "mode": args.mode,
        "status": "running",
        "ended_at": existing_meta.get("ended_at"),
        "duration_sec": existing_meta.get("duration_sec"),
        "started_at": existing_meta.get("started_at", datetime.now().isoformat()),
    }
    if args.resume_extraction or args.resume_eval:
        run_meta["resumed_at"] = datetime.now().isoformat()
    _atomic_write_json(meta_path, run_meta)

    # Set global backend for all LLM calls
    global _BACKEND, _QUAID_PROVIDER_PROFILE
    _BACKEND = args.backend
    _QUAID_PROVIDER_PROFILE = args.quaid_provider_profile
    if _BACKEND == "vllm":
        _QUAID_PROVIDER_PROFILE = "vllm"

    t_global = time.time()
    phase_seconds: dict = {}
    all_janitor_timing_events: List[dict] = []
    raw_tasks = (args.janitor_tasks or "benchmark").strip().lower()
    if raw_tasks in {"benchmark", "default"}:
        janitor_tasks = list(BENCHMARK_JANITOR_TASKS)
    elif raw_tasks == "all":
        janitor_tasks = ["all"]
    else:
        janitor_tasks = [t.strip() for t in (args.janitor_tasks or "").split(",") if t.strip()]
        if not janitor_tasks:
            janitor_tasks = list(BENCHMARK_JANITOR_TASKS)

    # --- Per-day mode: daily extraction + janitor ---
    if args.mode == "per-day":
        t_per_day_start = time.time()
        if not args.eval_only:
            setup_workspace(workspace)
            t_per_day_extract = time.time()
            run_per_day_extraction(
                workspace, api_key, args.no_cache,
                model=args.model,
                max_sessions=args.max_sessions,
                janitor_tasks=janitor_tasks,
            )
            phase_seconds["per_day_extraction"] = time.time() - t_per_day_extract

            if not args.skip_janitor:
                # Full janitor at the end (contradictions, decay, workspace audit,
                # snippets FOLD/REWRITE/DISCARD, journal distillation)
                t_per_day_janitor = time.time()
                per_day_events = run_janitor(workspace, tasks=janitor_tasks)
                phase_seconds["per_day_final_janitor"] = time.time() - t_per_day_janitor
                all_janitor_timing_events.extend(per_day_events)

            t_per_day_verify = time.time()
            verify_post_janitor(workspace)
            phase_seconds["per_day_verify"] = time.time() - t_per_day_verify

            # Optional post-hoc keyword tagging is disabled by default because it is
            # benchmark-only heuristic logic, not production retrieval behavior.
            if _ENABLE_POSTHOC_TAGS:
                t_per_day_tag = time.time()
                apply_posthoc_tags(workspace)
                phase_seconds["per_day_posthoc_tags"] = time.time() - t_per_day_tag
        else:
            print("  --eval-only: skipping setup, extraction, janitor, tagging")

        # Evaluation
        _ensure_workspace_db_schema(workspace, repair=True)
        t_per_day_eval = time.time()
        results = run_eval(workspace, api_key, max_sessions=args.max_sessions,
                          eval_model=args.eval_model,
                          context_inject=args.context_inject,
                          judge_model=args.judge,
                          no_judge=args.no_judge,
                          resume_eval=args.resume_eval,
                          resume_from_query=args.resume_from_query,
                          only_query=args.only_query)
        phase_seconds["per_day_eval"] = time.time() - t_per_day_eval

        debug_suffix = ".debug" if args.only_query is not None else ""
        results_path = workspace / f"evaluation_results{debug_suffix}.json"
        _atomic_write_json(results_path, results)
        print(f"\nSaved {len(results)} results to {results_path}")

        scores = score_results(results)

        tool_stats = {}
        for r in results:
            for tc in r.get("tool_calls", []):
                tool_stats[tc] = tool_stats.get(tc, 0) + 1

        print(f"\n{'=' * 60}")
        print("RESULTS SUMMARY (Per-Day Trusted Baseline)")
        print(f"{'=' * 60}")

        o = scores["overall"]
        print(f"\nOverall Accuracy: {o['accuracy']:.1f}%")
        print(f"  Questions: {o['count']} ({o['scored']} scored)")
        print(f"  Correct: {o['correct']} | Partial: {o['partial']} | Wrong: {o['wrong']} | Error: {o['error']}")

        print(f"\n{'Query Type':<30} {'Count':>5} {'Accuracy':>8}")
        print(f"{'─' * 50}")
        for qt, s in scores["per_type"].items():
            print(f"{qt:<30} {s['count']:>5} {s['accuracy']:>7.1f}%")

        print(f"\n{'Difficulty':<30} {'Count':>5} {'Accuracy':>8}")
        print(f"{'─' * 50}")
        for d, s in scores["per_difficulty"].items():
            print(f"{d:<30} {s['count']:>5} {s['accuracy']:>7.1f}%")

        print(f"\nTool Usage:")
        for tool, count in sorted(tool_stats.items()):
            print(f"  {tool}: {count} calls")
        avg_tools = sum(len(r.get("tool_calls", [])) for r in results) / len(results) if results else 0
        print(f"  Avg tools/query: {avg_tools:.1f}")

        scores_path = workspace / f"scores{debug_suffix}.json"
        _atomic_write_json(
            scores_path,
            {
                "scores": scores,
                "tool_stats": tool_stats,
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "mode": "per-day",
                    "extraction_model": args.model,
                    "eval_model": args.eval_model,
                    "judge_model": args.judge,
                    "tool_use": True,
                    "max_sessions": args.max_sessions,
                },
            },
        )

        # Save token usage summary
        _save_token_usage(results, workspace, args.eval_model, suffix=debug_suffix)
        _run_eval_forensics(workspace, suffix=debug_suffix)
        phase_seconds["per_day_total"] = time.time() - t_per_day_start

    # --- Ingestion ---
    if args.mode in ("full", "ingest"):
        t_ingest_start = time.time()
        janitor_timing_events: List[dict] = []
        setup_sentinel = workspace / ".setup_complete"
        resume_ingest = bool(args.resume_extraction or args.resume_from_chunk is not None)
        if resume_ingest and (workspace / "data" / "memory.db").exists():
            print("Resuming ingestion on existing workspace.")
            _ensure_workspace_db_schema(workspace, repair=True)
            # Rewrite config even on resume — provider/model settings may have changed.
            _rewrite_config(workspace)
            if setup_sentinel.exists():
                print("  Setup sentinel found; skipping project bootstrap.")
            else:
                print("  WARNING: missing setup sentinel; running project bootstrap before resume.")
                add_project_files(workspace, max_session=args.max_sessions)
                setup_sentinel.write_text(
                    json.dumps(
                        {"completed_at": datetime.now().isoformat(), "max_sessions": args.max_sessions},
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        else:
            setup_workspace(workspace)
            add_project_files(workspace, max_session=args.max_sessions)
            setup_sentinel.write_text(
                json.dumps(
                    {"completed_at": datetime.now().isoformat(), "max_sessions": args.max_sessions},
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        extraction = run_extraction(
            workspace, api_key, args.no_cache,
            model=args.model,
            max_sessions=args.max_sessions,
            run_chunk_janitor=not args.skip_janitor,
            resume_extraction=args.resume_extraction,
            resume_from_chunk=args.resume_from_chunk,
            only_chunk=args.only_chunk,
            janitor_tasks=janitor_tasks,
        )

        janitor_timing_events.extend(extraction.get("janitor_timing_events", []))
        all_janitor_timing_events.extend(extraction.get("janitor_timing_events", []))
        phase_seconds["extraction_total"] = float(extraction.get("extraction_total_seconds", 0.0) or 0.0)
        phase_seconds["extraction_llm_parallel"] = float(extraction.get("phase1_extraction_llm_seconds", 0.0) or 0.0)
        phase_seconds["store_and_chunk_janitor"] = float(extraction.get("phase2_apply_seconds", 0.0) or 0.0)

        if not args.skip_janitor:
            t_final_janitor = time.time()
            final_events = run_janitor(workspace, tasks=janitor_tasks)
            phase_seconds["final_janitor"] = time.time() - t_final_janitor
            janitor_timing_events.extend(final_events)
            all_janitor_timing_events.extend(final_events)

        _save_janitor_timing(workspace, janitor_timing_events)
        print(f"  Janitor timing saved: {workspace / 'janitor_timing.json'}")

        # Update run metadata with janitor timing summary for trend dashboards.
        try:
            meta_path = workspace / "run_metadata.json"
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            meta["janitor_timing"] = {
                "events": len(janitor_timing_events),
                "total_seconds": round(sum(float(e.get("elapsed_seconds", 0.0)) for e in janitor_timing_events), 3),
            }
            _atomic_write_json(meta_path, meta)
        except Exception as e:
            print(f"  WARN: failed to update janitor timing in run_metadata.json: {e}")

        t_verify = time.time()
        verify_post_janitor(workspace)
        phase_seconds["post_janitor_verify"] = time.time() - t_verify
        phase_seconds["ingest_total"] = time.time() - t_ingest_start

    # --- Evaluation ---
    if args.mode in ("full", "eval"):
        t_eval_start = time.time()
        if not (workspace / "data" / "memory.db").exists():
            print("ERROR: No DB found. Run ingestion first (--mode ingest or --mode full).")
            sys.exit(1)
        _ensure_workspace_db_schema(workspace, repair=True)

        results = run_eval(workspace, api_key, max_sessions=args.max_sessions,
                          eval_model=args.eval_model,
                          context_inject=args.context_inject,
                          judge_model=args.judge,
                          no_judge=args.no_judge,
                          resume_eval=args.resume_eval,
                          resume_from_query=args.resume_from_query,
                          only_query=args.only_query)

        # Save results
        debug_suffix = ".debug" if args.only_query is not None else ""
        results_path = workspace / f"evaluation_results{debug_suffix}.json"
        _atomic_write_json(results_path, results)
        print(f"\nSaved {len(results)} results to {results_path}")

        # Score and report
        scores = score_results(results)

        # Tool usage stats
        tool_stats = {}
        for r in results:
            for tc in r.get("tool_calls", []):
                tool_stats[tc] = tool_stats.get(tc, 0) + 1

        print(f"\n{'=' * 60}")
        print("RESULTS SUMMARY")
        print(f"{'=' * 60}")

        o = scores["overall"]
        print(f"\nOverall Accuracy: {o['accuracy']:.1f}%")
        print(f"  Questions: {o['count']} ({o['scored']} scored)")
        print(f"  Correct: {o['correct']} | Partial: {o['partial']} | Wrong: {o['wrong']} | Error: {o['error']}")

        # Per theme
        if "per_theme" in scores:
            from dataset import THEME_LABELS
            print(f"\n{'Theme':<30} {'Count':>5} {'Accuracy':>8}")
            print(f"{'─' * 50}")
            for theme, s in scores["per_theme"].items():
                label = THEME_LABELS.get(theme, theme)
                print(f"{label:<30} {s['count']:>5} {s['accuracy']:>7.1f}%")

        # Per type
        print(f"\n{'Query Type':<30} {'Count':>5} {'Accuracy':>8}")
        print(f"{'─' * 50}")
        for qt, s in scores["per_type"].items():
            print(f"{qt:<30} {s['count']:>5} {s['accuracy']:>7.1f}%")

        # Per difficulty
        print(f"\n{'Difficulty':<30} {'Count':>5} {'Accuracy':>8}")
        print(f"{'─' * 50}")
        for d, s in scores["per_difficulty"].items():
            print(f"{d:<30} {s['count']:>5} {s['accuracy']:>7.1f}%")

        # Tool usage
        print(f"\nTool Usage:")
        for tool, count in sorted(tool_stats.items()):
            print(f"  {tool}: {count} calls")
        avg_tools = sum(len(r.get("tool_calls", [])) for r in results) / len(results) if results else 0
        print(f"  Avg tools/query: {avg_tools:.1f}")

        # Save scores
        scores_path = workspace / f"scores{debug_suffix}.json"
        _atomic_write_json(
            scores_path,
            {
                "scores": scores,
                "tool_stats": tool_stats,
                "metadata": {
                    "timestamp": datetime.now().isoformat(),
                    "mode": args.mode,
                    "extraction_model": args.model,
                    "eval_model": args.eval_model,
                    "judge_model": args.judge,
                    "tool_use": True,
                    "max_sessions": args.max_sessions,
                },
            },
        )

        # Save token usage summary
        _save_token_usage(results, workspace, args.eval_model, suffix=debug_suffix)
        _run_eval_forensics(workspace, suffix=debug_suffix)
        phase_seconds["eval"] = time.time() - t_eval_start

    # --- Full-context baselines ---
    if args.mode == "fc":
        fc_results_dir = workspace / "fc_baselines"
        fc_results_dir.mkdir(parents=True, exist_ok=True)

        if not args.fc_tier5_only:
            for fc_model in ["claude-sonnet-4-5-20250929"]:
                fc_results = run_fc_baseline(
                    api_key, answer_model=fc_model,
                    max_sessions=args.max_sessions,
                    results_dir=fc_results_dir,
                    judge_model=args.judge,
                    compact_threshold_tokens=args.fc_compact_threshold_tokens,
                    context_window_tokens=args.fc_context_window_tokens,
                    max_history_share=args.fc_max_history_share,
                    compaction_parts=args.fc_compaction_parts,
                    resume_from_query=args.resume_from_query,
                    resume_eval=args.resume_eval,
                )
                fc_scores = score_results(fc_results)
                o = fc_scores["overall"]
                print(f"\n  FC {fc_model}: {o['accuracy']:.1f}% "
                      f"({o['correct']}C/{o['partial']}P/{o['wrong']}W)")
        else:
            print("  --fc-tier5-only: skipping 219-query FC baseline")

        # FC Tier 5 if requested
        if args.tier5:
            for fc_model in ["claude-sonnet-4-5-20250929"]:
                run_tier5_fc_baseline(
                    api_key, answer_model=fc_model,
                    max_sessions=args.max_sessions,
                    results_dir=fc_results_dir,
                    compact_threshold_tokens=args.fc_compact_threshold_tokens,
                    context_window_tokens=args.fc_context_window_tokens,
                    max_history_share=args.fc_max_history_share,
                    compaction_parts=args.fc_compaction_parts,
                )

    # --- Tier 5: Emotional Intelligence ---
    if args.tier5 and args.mode != "fc":
        t_tier5_start = time.time()
        if not (workspace / "data" / "memory.db").exists():
            print("ERROR: No DB found. Run ingestion first.")
            sys.exit(1)
        _ensure_workspace_db_schema(workspace, repair=True)

        tier5_results = run_tier5_eval(
            workspace, api_key,
            eval_model=args.eval_model or "claude-sonnet-4-5-20250929",
            context_inject=args.context_inject,
        )

        tier5_path = workspace / "tier5_results.json"
        _atomic_write_json(tier5_path, tier5_results)
        print(f"\nSaved {len(tier5_results)} Tier 5 results to {tier5_path}")

        total = sum(r["ei_score"] for r in tier5_results)
        max_score = len(tier5_results) * 2
        tier5_pct = (total / max_score * 100.0) if max_score > 0 else 0.0
        print(f"Tier 5 EI Score: {total}/{max_score} ({tier5_pct:.1f}%)")

        # Blended score (T1-4 + T5, equal weight, T5 scored 0/0.5/1)
        t14_for_blend = None
        if args.mode in ("full", "eval") and "results" in locals():
            t14_for_blend = results
        else:
            eval_path = workspace / "evaluation_results.json"
            if eval_path.exists():
                try:
                    loaded = json.loads(eval_path.read_text(encoding="utf-8"))
                    if isinstance(loaded, list):
                        t14_for_blend = loaded
                    elif isinstance(loaded, dict) and isinstance(loaded.get("results"), list):
                        t14_for_blend = loaded["results"]
                    else:
                        print("  WARNING: evaluation_results.json has unsupported schema; skipping blended score")
                except Exception as e:
                    print(f"  WARNING: failed to read evaluation_results.json for blending: {e}")
        if t14_for_blend:
            blended = score_blended(t14_for_blend, tier5_results)
            b = blended["blended"]
            print(f"\nBlended Score: {b['score']}/{b['count']} ({b['pct']:.1f}%)")
            print(f"  T1-4: {blended['t14']['score']}/{blended['t14']['count']} ({blended['t14']['pct']:.1f}%)")
            print(f"  T5:   {blended['t5']['score']}/{blended['t5']['count']} ({blended['t5']['pct']:.1f}%)")

            # Update scores.json with blended results
            scores_path = workspace / "scores.json"
            if scores_path.exists():
                with open(scores_path) as f:
                    scores_data = json.load(f)
                scores_data["blended"] = blended
                _atomic_write_json(scores_path, scores_data)
        phase_seconds["tier5"] = time.time() - t_tier5_start

    elapsed = time.time() - t_global
    try:
        meta_path = workspace / "run_metadata.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        now_iso = datetime.now().isoformat()
        started = meta.get("started_at")
        duration_sec = round(elapsed, 3)
        if started:
            try:
                duration_sec = round((datetime.fromisoformat(now_iso) - datetime.fromisoformat(started)).total_seconds(), 3)
            except Exception:
                duration_sec = round(elapsed, 3)
        meta["completed_at"] = datetime.now().isoformat()
        meta["total_elapsed_seconds"] = round(elapsed, 3)
        # Compatibility fields consumed by external dashboards/monitors.
        meta["ended_at"] = now_iso
        meta["duration_sec"] = duration_sec
        meta["status"] = "completed"
        _atomic_write_json(meta_path, meta)
    except Exception as e:
        print(f"  WARN: failed to update total elapsed in run_metadata.json: {e}")

    phase_payload = _build_phase_timing_payload(
        mode=args.mode,
        total_elapsed_seconds=elapsed,
        phase_seconds=phase_seconds,
        janitor_events=all_janitor_timing_events,
    )
    phase_path = _save_phase_timing(workspace, phase_payload)
    _print_phase_timing(phase_payload)
    print(f"  Phase timing saved: {phase_path}")

    # Resume artifacts are no longer needed after final scoring artifacts exist.
    if (workspace / "scores.json").exists():
        for p in (
            workspace / "logs" / "eval_progress.json",
            workspace / "evaluation_results.partial.json",
        ):
            try:
                if p.exists():
                    p.unlink()
            except Exception as e:
                print(f"  WARN: failed to remove resume artifact {p.name}: {e}")

    print(f"\nTotal elapsed: {elapsed:.1f}s ({elapsed/60:.1f}m)")


if __name__ == "__main__":
    main()
