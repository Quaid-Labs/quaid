"""Tests for benchmark monitor, dashboard phase parsing, and eval forensics.

Covers monitor_benchmarks.py pure/semi-pure functions and
eval-forensics.py deterministic analysis logic.
"""

import json
import os
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import monitor_benchmarks from codex-bench scripts
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "agents" / "codex-bench" / "scripts"
# Fallback: try standard paths
if not _SCRIPTS_DIR.exists():
    for candidate in [
        Path.home() / "quaid" / "agents" / "codex-bench" / "scripts",
        Path.home() / "quaid" / "util" / "agents" / "codex-bench" / "scripts",
    ]:
        if candidate.exists():
            _SCRIPTS_DIR = candidate
            break

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import monitor_benchmarks as mb  # noqa: E402


# ---------------------------------------------------------------------------
# Try importing eval-forensics (has hyphen in name, needs importlib)
# ---------------------------------------------------------------------------

_forensics = None
try:
    import importlib.util
    _ef_path = _SCRIPTS_DIR / "eval-forensics.py"
    if _ef_path.exists():
        spec = importlib.util.spec_from_file_location("eval_forensics", str(_ef_path))
        _forensics = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_forensics)
except Exception:
    pass


# ===================================================================
# monitor_benchmarks.py — Pure Functions
# ===================================================================


class TestDetectKind:
    """Tests for detect_kind: run type classification by directory name."""

    def test_locomo(self, tmp_path):
        d = tmp_path / "locomo-quaid-r1"
        d.mkdir()
        assert mb.detect_kind(d) == "locomo"

    def test_longmemeval(self, tmp_path):
        d = tmp_path / "longmemeval-quaid-r1"
        d.mkdir()
        assert mb.detect_kind(d) == "longmemeval"

    def test_agentlife_quaid(self, tmp_path):
        d = tmp_path / "quaid-s-r388-20260303"
        d.mkdir()
        assert mb.detect_kind(d) == "agentlife"

    def test_agentlife_al_prefix(self, tmp_path):
        d = tmp_path / "al-s-r1-20260303"
        d.mkdir()
        assert mb.detect_kind(d) == "agentlife"

    def test_unknown(self, tmp_path):
        d = tmp_path / "mystery-run"
        d.mkdir()
        assert mb.detect_kind(d) == "unknown"

    def test_case_insensitive(self, tmp_path):
        d = tmp_path / "LOCOMO-Big-Run"
        d.mkdir()
        assert mb.detect_kind(d) == "locomo"


class TestFailureMarker:
    """Tests for failure_marker: log failure detection."""

    def test_traceback(self):
        assert mb.failure_marker("Traceback (most recent call last):\n  File ...") is True

    def test_runtime_error(self):
        assert mb.failure_marker("RuntimeError: something broke") is True

    def test_timeout(self):
        assert mb.failure_marker("subprocess.TimeoutExpired: 900s") is True

    def test_fatal(self):
        assert mb.failure_marker("FATAL: cannot continue") is True

    def test_error_marker(self):
        assert mb.failure_marker("ERROR: bad config") is True

    def test_all_attempts_failed(self):
        assert mb.failure_marker("All attempts failed") is True

    def test_clean_log(self):
        assert mb.failure_marker("Run completed successfully. Score: 0.85") is False

    def test_empty_log(self):
        assert mb.failure_marker("") is False


class TestHasCompletion:
    """Tests for has_completion: completion artifact detection."""

    def test_agentlife_scores(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        assert mb.has_completion(run_dir, "agentlife") is False
        (run_dir / "scores.json").write_text("{}")
        assert mb.has_completion(run_dir, "agentlife") is True

    def test_locomo_results(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        assert mb.has_completion(run_dir, "locomo") is False
        (run_dir / "locomo_results.json").write_text("{}")
        assert mb.has_completion(run_dir, "locomo") is True

    def test_locomo_scores_fallback(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text("{}")
        assert mb.has_completion(run_dir, "locomo") is True

    def test_longmemeval_results(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        assert mb.has_completion(run_dir, "longmemeval") is False
        (run_dir / "longmemeval_results.json").write_text("{}")
        assert mb.has_completion(run_dir, "longmemeval") is True

    def test_unknown_falls_back_to_scores(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text("{}")
        assert mb.has_completion(run_dir, "unknown") is True


class TestParseScore:
    """Tests for parse_score: score extraction from results files."""

    def test_agentlife_score(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text(json.dumps({
            "scores": {"overall": {"accuracy": 0.85}}
        }))
        assert mb.parse_score(run_dir, "agentlife") == 0.85

    def test_longmemeval_score(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "longmemeval_results.json").write_text(json.dumps({
            "metrics": {"overall_accuracy": 0.72}
        }))
        assert mb.parse_score(run_dir, "longmemeval") == 0.72

    def test_missing_file_returns_none(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        assert mb.parse_score(run_dir, "agentlife") is None

    def test_malformed_json_returns_none(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text("not json")
        assert mb.parse_score(run_dir, "agentlife") is None

    def test_missing_keys_returns_none(self, tmp_path):
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text(json.dumps({"scores": {}}))
        assert mb.parse_score(run_dir, "agentlife") is None


class TestFindLogFiles:
    """Tests for find_log_files: log file discovery."""

    def test_finds_sidecar_log(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "run1"
        run_dir.mkdir()
        sidecar = runs_dir / "run1.launch.log"
        sidecar.write_text("log content")
        logs = mb.find_log_files(runs_dir, "run1")
        assert sidecar in logs

    def test_finds_internal_logs(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "run1"
        run_dir.mkdir()
        (run_dir / "launch.log").write_text("a")
        (run_dir / "run.log").write_text("b")
        logs = mb.find_log_files(runs_dir, "run1")
        assert len(logs) == 2

    def test_no_logs_returns_empty(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "run1"
        run_dir.mkdir()
        assert mb.find_log_files(runs_dir, "run1") == []


class TestDefaultQueueItemValid:
    """Tests for default_queue_item_valid: queue item validation."""

    def test_valid_item(self):
        assert mb.default_queue_item_valid({"cmd": "python run.py", "results_dir": "runs/r1"}) is True

    def test_missing_cmd(self):
        assert mb.default_queue_item_valid({"results_dir": "runs/r1"}) is False

    def test_missing_results_dir(self):
        assert mb.default_queue_item_valid({"cmd": "python run.py"}) is False

    def test_empty_cmd(self):
        assert mb.default_queue_item_valid({"cmd": "", "results_dir": "r"}) is False

    def test_empty_dict(self):
        assert mb.default_queue_item_valid({}) is False


class TestShouldNotify:
    """Tests for should_notify: notification decision logic."""

    def test_always_mode(self):
        assert mb.should_notify({}, "always") is True
        assert mb.should_notify({"counts": {}, "actions": []}, "always") is True

    def test_action_mode_with_actions(self):
        assert mb.should_notify({"actions": ["resumed run1"]}, "action") is True

    def test_action_mode_no_actions(self):
        assert mb.should_notify({"actions": []}, "action") is False

    def test_problem_mode_with_failures(self):
        assert mb.should_notify({"counts": {"failed": 1}}, "problem") is True

    def test_problem_mode_with_incomplete(self):
        assert mb.should_notify({"counts": {"incomplete": 2}}, "problem") is True

    def test_problem_mode_all_clear(self):
        assert mb.should_notify({"counts": {"active": 1, "complete": 5}}, "problem") is False

    def test_unknown_mode(self):
        assert mb.should_notify({}, "unknown_mode") is False


class TestBuildNotifyMessage:
    """Tests for build_notify_message: message formatting."""

    def test_contains_counts(self):
        report = {
            "counts": {"active": 1, "complete": 3, "failed": 0, "incomplete": 1},
            "actions": [],
            "runs": [],
        }
        msg = mb.build_notify_message(report, "[test]")
        assert "[test]" in msg
        assert "active=1" in msg
        assert "complete=3" in msg

    def test_includes_actions(self):
        report = {
            "counts": {},
            "actions": ["resumed run1"],
            "runs": [],
        }
        msg = mb.build_notify_message(report, "[test]")
        assert "resumed run1" in msg

    def test_includes_problem_runs(self):
        report = {
            "counts": {},
            "actions": [],
            "runs": [{"name": "broken-run", "state": "failed", "kind": "agentlife", "score": None, "reason": "crash"}],
        }
        msg = mb.build_notify_message(report, "[test]")
        assert "broken-run" in msg
        assert "failed" in msg


class TestBuildAgentlifeResumeCmd:
    """Tests for build_agentlife_resume_cmd: resume command construction."""

    def test_basic_resume(self, tmp_path):
        run_dir = tmp_path / "quaid-s-r388"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({
            "mode": "full",
            "model": "claude-sonnet-4-5-20250929",
            "eval_model": "claude-sonnet-4-5-20250929",
            "judge": "gpt-4o-mini",
            "backend": "api",
            "parallel": 6,
        }))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert cmd is not None
        assert "--resume-extraction" in cmd
        assert "--resume-eval" in cmd
        assert "--mode" in cmd
        idx = cmd.index("--mode")
        assert cmd[idx + 1] == "full"

    def test_per_day_mode(self, tmp_path):
        run_dir = tmp_path / "quaid-pd-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"mode": "per-day"}))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        idx = cmd.index("--mode")
        assert cmd[idx + 1] == "per-day"

    def test_no_tier5(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"tier5": False}))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert "--no-tier5" in cmd

    def test_tier5_enabled_no_flag(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"tier5": True}))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert "--no-tier5" not in cmd

    def test_missing_metadata_uses_defaults(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert cmd is not None
        assert "--mode" in cmd

    def test_max_sessions(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"max_sessions": 10}))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert "--max-sessions" in cmd
        idx = cmd.index("--max-sessions")
        assert cmd[idx + 1] == "10"

    def test_parallel_clamped(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({"parallel": 100}))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        idx = cmd.index("--parallel")
        assert int(cmd[idx + 1]) <= 64

    def test_vllm_backend(self, tmp_path):
        run_dir = tmp_path / "quaid-r1"
        run_dir.mkdir()
        (run_dir / "run_metadata.json").write_text(json.dumps({
            "backend": "vllm",
            "vllm_url": "http://localhost:8000",
            "vllm_model": "meta-llama/Llama-2-7b",
        }))
        cmd = mb.build_agentlife_resume_cmd(run_dir)
        assert "--vllm-url" in cmd
        assert "--vllm-model" in cmd


class TestClassifyRun:
    """Tests for classify_run: run state classification."""

    def test_active_run(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "quaid-s-r1"
        run_dir.mkdir()
        active = {"quaid-s-r1": 12345}
        status = mb.classify_run(run_dir, runs_dir, active)
        assert status.state == "active"
        assert status.active_pid == 12345

    def test_complete_run(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "quaid-s-r1"
        run_dir.mkdir()
        (run_dir / "scores.json").write_text(json.dumps({"scores": {"overall": {"accuracy": 0.8}}}))
        status = mb.classify_run(run_dir, runs_dir, {})
        assert status.state == "complete"
        assert status.score == 0.8

    def test_failed_run(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "quaid-s-r1"
        run_dir.mkdir()
        (runs_dir / "quaid-s-r1.launch.log").write_text("Traceback (most recent call last):\n  boom")
        status = mb.classify_run(run_dir, runs_dir, {})
        assert status.state == "failed"

    def test_incomplete_run(self, tmp_path):
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        run_dir = runs_dir / "quaid-s-r1"
        run_dir.mkdir()
        status = mb.classify_run(run_dir, runs_dir, {})
        assert status.state == "incomplete"


class TestAtomicWriteText:
    """Tests for atomic_write_text: safe file writes."""

    def test_creates_file(self, tmp_path):
        p = tmp_path / "out.txt"
        mb.atomic_write_text(p, "hello")
        assert p.read_text() == "hello"

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "out.txt"
        p.write_text("old")
        mb.atomic_write_text(p, "new")
        assert p.read_text() == "new"

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "sub" / "dir" / "file.txt"
        mb.atomic_write_text(p, "deep")
        assert p.read_text() == "deep"

    def test_no_partial_on_crash(self, tmp_path):
        """Ensure no temp files left behind on success."""
        p = tmp_path / "out.txt"
        mb.atomic_write_text(p, "content")
        # Only the target file should exist
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0].name == "out.txt"


class TestReadWriteQueue:
    """Tests for read_queue + write_queue: benchmark queue I/O."""

    def test_read_empty_queue(self, tmp_path):
        q = tmp_path / "queue.json"
        assert mb.read_queue(q) == []

    def test_read_nonexistent_queue(self, tmp_path):
        q = tmp_path / "missing.json"
        assert mb.read_queue(q) == []

    def test_roundtrip(self, tmp_path):
        q = tmp_path / "queue.json"
        items = [
            {"cmd": "python run.py", "results_dir": "runs/r1"},
            {"cmd": "python run.py --mode per-day", "results_dir": "runs/r2"},
        ]
        mb.write_queue(q, items)
        loaded = mb.read_queue(q)
        assert len(loaded) == 2
        assert loaded[0]["cmd"] == "python run.py"

    def test_read_filters_non_dicts(self, tmp_path):
        q = tmp_path / "queue.json"
        q.write_text(json.dumps([{"cmd": "ok", "results_dir": "r"}, "not a dict", 42]))
        loaded = mb.read_queue(q)
        assert len(loaded) == 1


class TestBuildLocomoResumeCmd:
    """Tests for build_locomo_resume_cmd: locomo resume command."""

    def test_basic(self, tmp_path):
        run_dir = tmp_path / "locomo-r1"
        run_dir.mkdir()
        cmd = mb.build_locomo_resume_cmd(run_dir)
        assert "run_locomo.py" in cmd[1]
        assert "--skip-ingest" in cmd
        assert f"runs/{run_dir.name}" in cmd


# ===================================================================
# eval-forensics.py — Pure Functions
# ===================================================================


@pytest.mark.skipif(_forensics is None, reason="eval-forensics.py not found")
class TestForensicsPureFunctions:
    """Tests for eval-forensics.py pure analysis functions."""

    def test_base_query_type(self):
        assert _forensics._base_query_type("temporal_current (session 5)") == "temporal_current"
        assert _forensics._base_query_type("simple") == "simple"
        assert _forensics._base_query_type("") == ""

    def test_is_wrong(self):
        assert _forensics._is_wrong({"judge_label": "WRONG"}) is True
        assert _forensics._is_wrong({"judge_label": "CORRECT"}) is False
        assert _forensics._is_wrong({"label": "WRONG"}) is True
        assert _forensics._is_wrong({}) is False

    def test_parse_tool_payload_chars(self):
        assert _forensics._parse_tool_payload_chars(["Found 150 chars of context"]) == 150
        assert _forensics._parse_tool_payload_chars(["50 chars", "100 chars"]) == 150
        assert _forensics._parse_tool_payload_chars([]) == 0
        assert _forensics._parse_tool_payload_chars(["no numbers here"]) == 0

    def test_looks_idk(self):
        assert _forensics._looks_idk("I don't have information about that") is True
        assert _forensics._looks_idk("I do not have enough information") is True
        assert _forensics._looks_idk("I wasn't able to find that") is True
        assert _forensics._looks_idk("The recipe uses 2 cups of flour") is False
        assert _forensics._looks_idk("") is True  # empty = IDK

    def test_gt_terms(self):
        terms = _forensics._gt_terms("Maya started running 5k in March 2026")
        assert "maya" in terms
        assert "started" in terms
        assert "running" in terms
        assert "march" in terms
        # Short terms (<4 chars) excluded
        assert "5k" not in terms

    def test_gt_terms_max_limit(self):
        terms = _forensics._gt_terms("a" * 50 + " word1 word2 word3 word4 word5 word6 word7 word8 word9", max_terms=3)
        assert len(terms) <= 3

    def test_signal_in_tool_summary(self):
        assert _forensics._signal_in_tool_summary(
            "Maya started running",
            ["Found: maya has been running 5k regularly"],
        ) is True
        assert _forensics._signal_in_tool_summary(
            "Maya started running",
            ["No relevant memories found"],
        ) is False
        assert _forensics._signal_in_tool_summary("", []) is False


@pytest.mark.skipif(_forensics is None, reason="eval-forensics.py not found")
class TestBuildForensics:
    """Tests for build_forensics: structured analysis of eval results."""

    def test_empty_results(self, tmp_path):
        result = _forensics.build_forensics(tmp_path, [])
        assert result["total_queries"] == 0
        assert result["misses_total"] == 0

    def test_all_correct(self, tmp_path):
        rows = [
            {"judge_label": "CORRECT", "query_type": "simple"},
            {"judge_label": "CORRECT", "query_type": "temporal_current"},
        ]
        result = _forensics.build_forensics(tmp_path, rows)
        assert result["total_queries"] == 2
        assert result["misses_total"] == 0

    def test_counts_misses(self, tmp_path):
        rows = [
            {"judge_label": "CORRECT", "query_type": "simple"},
            {"judge_label": "WRONG", "query_type": "temporal_current", "source_session": 5,
             "tool_calls": [], "tool_results_summary": [], "ground_truth": "x", "prediction": "y"},
            {"judge_label": "WRONG", "query_type": "simple", "source_session": 3,
             "tool_calls": [], "tool_results_summary": [], "ground_truth": "a", "prediction": "b"},
        ]
        result = _forensics.build_forensics(tmp_path, rows)
        assert result["total_queries"] == 3
        assert result["misses_total"] == 2

    def test_type_breakdown(self, tmp_path):
        rows = [
            {"judge_label": "WRONG", "query_type": "temporal_current (session 5)", "source_session": 5,
             "tool_calls": [], "tool_results_summary": [], "ground_truth": "x", "prediction": "y"},
            {"judge_label": "WRONG", "query_type": "temporal_current (session 8)", "source_session": 8,
             "tool_calls": [], "tool_results_summary": [], "ground_truth": "x", "prediction": "y"},
            {"judge_label": "CORRECT", "query_type": "simple"},
        ]
        result = _forensics.build_forensics(tmp_path, rows)
        by_type = result.get("miss_rate_by_type", {})
        assert by_type.get("temporal_current", {}).get("misses", 0) == 2
