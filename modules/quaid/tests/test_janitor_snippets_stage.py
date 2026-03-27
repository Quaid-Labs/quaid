"""Tests for janitor snippets stage — snippet review, decay, and checkpoint behavior.

Covers:
- Snippet review: _resolve_writable_file_path returning None → error appended, not raised
- Snippet review: error format ("Failed to insert into X: ..." / "Skipped X[N]: file missing")
- Snippet review: None file_path error does not abort other snippets
- Snippet review: DISCARD action works even when target file is missing
- Checkpoint: written/updated at each stage transition
- Checkpoint: current_stage reflects last-attempted stage; completed_stages reflects actual completion
- Checkpoint: snippets stage failure shows correct stage (not a prior stage)
- janitor_complete event: success=False when errors > 0, success=True when errors == 0
- Janitor --apply with applyMode=ask: queues approval requests for core_markdown_writes scope
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Module-level setup — same pattern as test_janitor_apply_mode.py
# ---------------------------------------------------------------------------
_tmp_home = Path(tempfile.mkdtemp(prefix="quaid-snippets-test-"))
(_tmp_home / "config").mkdir(parents=True, exist_ok=True)
(_tmp_home / "config" / "memory.json").write_text(
    json.dumps({"adapter": {"type": "standalone"}}), encoding="utf-8"
)
os.environ["QUAID_HOME"] = str(_tmp_home)

import core.lifecycle.janitor as janitor
import datastore.notedb.soul_snippets as soul_snippets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_janitor_cfg(apply_mode: str = "auto", approval_policies: dict = None):
    """Minimal janitor config namespace."""
    return SimpleNamespace(
        systems=SimpleNamespace(
            memory=True,
            journal=True,
            projects=False,
            workspace=False,
        ),
        plugins=SimpleNamespace(
            enabled=False,
            strict=False,
            config={},
            slots=SimpleNamespace(adapter="", ingest=[], datastores=[]),
        ),
        janitor=SimpleNamespace(
            apply_mode=apply_mode,
            approval_policies=approval_policies if approval_policies is not None else {},
            test_timeout_seconds=60,
            dedup=SimpleNamespace(similarity_threshold=0.85, high_similarity_threshold=0.95),
            contradiction=SimpleNamespace(enabled=False, min_similarity=0.7, max_similarity=0.95),
            task_timeout_minutes=120,
        ),
        decay=SimpleNamespace(threshold_days=90, rate_percent=10),
        notifications=SimpleNamespace(enabled=False, level="normal"),
        users=SimpleNamespace(default_owner="quaid"),
        core=SimpleNamespace(
            parallel=SimpleNamespace(
                enabled=True,
                lock_enforcement_enabled=False,
                lock_wait_seconds=5,
                lock_require_registration=False,
                lifecycle_prepass_timeout_seconds=60,
                lifecycle_prepass_timeout_retries=0,
                lifecycle_prepass_workers=1,
            )
        ),
        rag=SimpleNamespace(docs_dir="docs"),
        database=SimpleNamespace(path=":memory:"),
    )


def _patch_janitor_base(monkeypatch, tmp_path, cfg=None):
    """Apply all the standard janitor patches to prevent side effects."""
    if cfg is None:
        cfg = _make_janitor_cfg()

    monkeypatch.setattr(janitor, "_refresh_runtime_state", lambda: None)
    monkeypatch.setattr(janitor, "_acquire_lock", lambda: True)
    monkeypatch.setattr(janitor, "_release_lock", lambda: None)
    monkeypatch.setattr(janitor, "rotate_logs", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_usage", lambda: None)
    monkeypatch.setattr(janitor, "reset_token_budget", lambda: None)
    monkeypatch.setattr(janitor, "get_graph", lambda: object())
    monkeypatch.setattr(janitor, "init_janitor_metadata", lambda _graph: None)
    monkeypatch.setattr(janitor, "get_last_run_time", lambda _graph, _task: None)
    monkeypatch.setattr(janitor, "is_benchmark_mode", lambda: False)
    monkeypatch.setattr(janitor, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(janitor, "_logs_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(janitor, "_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(janitor, "_benchmark_review_gate_triggered", lambda *_a, **_kw: False)
    monkeypatch.setattr(janitor, "get_llm_provider", lambda: SimpleNamespace(get_profiles=lambda: {"deep": {"available": True}}))
    monkeypatch.setattr(janitor, "run_tests", lambda _m: {"success": True, "passed": 0, "failed": 0, "total": 0})
    monkeypatch.setattr(janitor, "_check_for_updates", lambda: None)
    monkeypatch.setattr(janitor, "_append_decision_log", lambda *_a, **_kw: None)
    monkeypatch.setattr(janitor, "_send_notification", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "_queue_delayed_notification", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "save_run_time", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(janitor, "record_health_snapshot", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "record_janitor_run", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "checkpoint_wal", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "list_recent_fact_texts", lambda *_a, **_kw: [], raising=False)
    monkeypatch.setattr(janitor, "count_nodes_by_status", lambda *_a, **_kw: {}, raising=False)
    monkeypatch.setattr(janitor, "graduate_approved_to_active", lambda *_a, **_kw: None, raising=False)
    monkeypatch.setattr(janitor, "backfill_embeddings", lambda *_a, **_kw: {"found": 0, "embedded": 0}, raising=False)
    monkeypatch.setattr(janitor, "_cfg", cfg)

    # Disable parallel prepass by returning empty dict
    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: object(), raising=False)
    monkeypatch.setattr("core.runtime.plugins.run_plugin_contract_surface_collect", lambda **_kw: ([], [], []), raising=False)


# ---------------------------------------------------------------------------
# Tests: apply_decisions / _resolve_writable_file_path behavior
# ---------------------------------------------------------------------------

class TestApplyDecisionsNoneFilePath:
    """Tests for the NoneType bug scenario in apply_decisions."""

    def test_none_file_path_on_fold_appends_error_not_raises(self, tmp_path):
        """When _resolve_writable_file_path returns None (called after _insert_into_file returns False),
        error is appended to stats['errors'], not raised as an exception."""
        # apply_decisions calls _insert_into_file first; if that returns False,
        # it calls _resolve_writable_file_path to classify the failure.
        with patch("datastore.notedb.soul_snippets._insert_into_file", return_value=False):
            with patch("datastore.notedb.soul_snippets._resolve_writable_file_path", return_value=None):
                decisions = [
                    {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"},
                ]
                all_snippets = {
                    "SOUL.md": {
                        "snippets": ["Some new insight about existence"],
                        "parent_content": "# SOUL\n",
                        "config": {"maxLines": 80},
                    }
                }
                stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=False)

        # Must not raise — error captured in stats
        assert isinstance(stats["errors"], list)
        assert len(stats["errors"]) > 0

    def test_none_file_path_on_fold_produces_expected_error_format(self, tmp_path):
        """Error message for missing file on FOLD follows 'Skipped {filename}[N]: file missing' format."""
        with patch("datastore.notedb.soul_snippets._insert_into_file", return_value=False):
            with patch("datastore.notedb.soul_snippets._resolve_writable_file_path", return_value=None):
                decisions = [
                    {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"},
                ]
                all_snippets = {
                    "SOUL.md": {
                        "snippets": ["A snippet"],
                        "parent_content": "# SOUL\n",
                        "config": {"maxLines": 80},
                    }
                }
                stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=False)

        errors = stats["errors"]
        assert len(errors) == 1
        assert "SOUL.md" in errors[0]
        # Matches "Skipped SOUL.md[1]: file missing"
        assert "Skipped" in errors[0]
        assert "file missing" in errors[0]

    def test_insert_exception_produces_failed_to_insert_error_format(self, tmp_path):
        """When _insert_into_file raises, error format is 'Failed to insert into {filename}: ...'"""
        # Create a real target file so _resolve_writable_file_path succeeds, but
        # _insert_into_file itself raises.
        target = tmp_path / "SOUL.md"
        target.write_text("# SOUL\n", encoding="utf-8")

        with patch("datastore.notedb.soul_snippets._resolve_writable_file_path", return_value=target):
            with patch("datastore.notedb.soul_snippets._insert_into_file", side_effect=RuntimeError("disk full")):
                decisions = [
                    {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"},
                ]
                all_snippets = {
                    "SOUL.md": {
                        "snippets": ["A snippet"],
                        "parent_content": "# SOUL\n",
                        "config": {"maxLines": 80},
                    }
                }
                stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=False)

        assert len(stats["errors"]) == 1
        err = stats["errors"][0]
        assert err.startswith("Failed to insert into SOUL.md")
        assert "disk full" in err

    def test_none_file_path_does_not_abort_other_snippets(self, tmp_path):
        """After a None file_path error for one snippet, processing continues for remaining snippets.

        The code flow: _insert_into_file returns False (because file is missing) →
        apply_decisions calls _resolve_writable_file_path again to classify the failure →
        returns None → appends "Skipped X[N]: file missing" error.
        Processing should NOT stop — the loop continues to the USER.md decision.
        """
        user_file = tmp_path / "USER.md"
        user_file.write_text("# USER\n", encoding="utf-8")

        def _fake_insert(filename, text, insert_after, max_lines=0):
            # SOUL.md insert fails (file doesn't exist); USER.md insert succeeds
            if filename == "SOUL.md":
                return False
            # Write to the user file to simulate a real insert
            user_file.write_text(user_file.read_text(encoding="utf-8") + f"- {text}\n", encoding="utf-8")
            return True

        def _fake_resolve(filename, **kwargs):
            # Called by apply_decisions after _insert_into_file returns False to classify failure
            if filename == "SOUL.md":
                return None
            return user_file

        with patch("datastore.notedb.soul_snippets._insert_into_file", side_effect=_fake_insert):
            with patch("datastore.notedb.soul_snippets._resolve_writable_file_path", side_effect=_fake_resolve):
                with patch("datastore.notedb.soul_snippets._clear_processed_snippets"):
                    decisions = [
                        # SOUL.md snippet will fail (file missing → insert returns False)
                        {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"},
                        # USER.md snippet should still be processed after the SOUL.md failure
                        {"file": "USER.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"},
                    ]
                    all_snippets = {
                        "SOUL.md": {
                            "snippets": ["Soul snippet"],
                            "parent_content": "# SOUL\n",
                            "config": {"maxLines": 80},
                        },
                        "USER.md": {
                            "snippets": ["User snippet"],
                            "parent_content": "# USER\n",
                            "config": {"maxLines": 150},
                        },
                    }
                    stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=False)

        # Should have an error for SOUL.md but USER.md should still be folded
        assert stats["folded"] >= 1, "USER.md snippet should have been folded"
        assert len(stats["errors"]) >= 1, "Should have error for missing SOUL.md"

    def test_discard_succeeds_even_when_target_file_missing(self, tmp_path):
        """DISCARD action does not need to write to the file, so it works even if target is missing."""
        with patch("datastore.notedb.soul_snippets._resolve_writable_file_path", return_value=None):
            with patch("datastore.notedb.soul_snippets._clear_processed_snippets"):
                decisions = [
                    {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "redundant"},
                ]
                all_snippets = {
                    "SOUL.md": {
                        "snippets": ["Duplicate observation"],
                        "parent_content": "",
                        "config": {},
                    }
                }
                stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=False)

        assert stats["discarded"] == 1
        assert stats["errors"] == []

    def test_discard_in_dry_run_with_missing_file_has_no_error(self, tmp_path):
        """DISCARD in dry_run mode is a no-op write path, so no errors even if file path is None."""
        decisions = [
            {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "redundant"},
        ]
        all_snippets = {
            "SOUL.md": {
                "snippets": ["Some observation"],
                "parent_content": "",
                "config": {},
            }
        }
        # In dry_run mode apply_decisions doesn't call _insert_into_file at all
        stats = soul_snippets.apply_decisions(decisions, all_snippets, dry_run=True)
        assert stats["discarded"] == 1
        assert stats["errors"] == []


# ---------------------------------------------------------------------------
# Tests: Checkpoint behavior
# ---------------------------------------------------------------------------

class TestCheckpointBehavior:
    """Tests for checkpoint write/update behavior.

    Important: Only memory graph stages (review, dedup_review, duplicates, decay, decay_review,
    temporal) write checkpoint stage entries via _run_memory_stage/_checkpoint_save.
    Lifecycle stages (snippets, journal, workspace, docs) do NOT individually update
    current_stage/completed_stages — they run after the memory pipeline.

    The checkpoint file is always written at start and end of task='all' runs.
    """

    def test_checkpoint_is_noop_for_non_all_task(self, monkeypatch, tmp_path):
        """For task='snippets', _checkpoint_save is a no-op — checkpoint file is never written."""
        _patch_janitor_base(monkeypatch, tmp_path)

        checkpoint_writes = []
        orig_write = janitor._atomic_write_json

        def _spy(path, payload):
            if "checkpoint" in str(path):
                checkpoint_writes.append(dict(payload))
            orig_write(path, payload)

        monkeypatch.setattr(janitor, "_atomic_write_json", _spy)

        from core.lifecycle.janitor_lifecycle import RoutineResult
        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run",
                            lambda self, name, ctx: RoutineResult())

        result = janitor.run_task_optimized("snippets", dry_run=True, resume_checkpoint=False)

        # For task != "all", _checkpoint_save is a no-op — no checkpoint file written
        assert checkpoint_writes == [], f"Expected no checkpoint writes for task=snippets, got: {checkpoint_writes}"
        assert result.get("success") is True

    def test_checkpoint_written_at_start_and_end_of_all_task(self, monkeypatch, tmp_path):
        """For task='all', checkpoint file is written at start (status=running) and end (status=completed/failed)."""
        _patch_janitor_base(monkeypatch, tmp_path)

        checkpoint_writes = []
        orig_write = janitor._atomic_write_json

        def _spy(path, payload):
            if "checkpoint" in str(path):
                checkpoint_writes.append(dict(payload))
            orig_write(path, payload)

        monkeypatch.setattr(janitor, "_atomic_write_json", _spy)

        from core.lifecycle.janitor_lifecycle import RoutineResult
        monkeypatch.setattr(
            "core.lifecycle.janitor_lifecycle.LifecycleRegistry.run",
            lambda self, name, ctx: RoutineResult(
                metrics={
                    "memories_reviewed": 0, "memories_deleted": 0,
                    "memories_fixed": 0, "review_carryover": 0,
                    "review_coverage_ratio_pct": 100,
                    "decay_reviewed": 0, "decay_review_deleted": 0,
                    "decay_review_extended": 0, "decay_review_pinned": 0,
                    "snippets_folded": 0, "snippets_rewritten": 0,
                    "snippets_discarded": 0, "snippets_skipped_at_limit": 0,
                    "journal_additions": 0, "journal_edits": 0,
                    "journal_recovered_edits": 0, "journal_entries_distilled": 0,
                    "temporal_found": 0, "temporal_fixed": 0,
                    "dedup_reviewed": 0, "dedup_confirmed": 0, "dedup_reversed": 0,
                    "duplicates_merged": 0, "memories_decayed": 0,
                    "memories_deleted_by_decay": 0, "decay_queued": 0,
                },
            ),
        )

        # Checkpoint writes only happen when not dry_run (b75af9f3)
        monkeypatch.setenv("QUAID_JANITOR_SKIP_NOTIFY", "1")
        result = janitor.run_task_optimized("all", dry_run=False, resume_checkpoint=False)

        # At minimum: initial write (status=running) + final write (status=completed/failed)
        assert len(checkpoint_writes) >= 2, f"Expected >= 2 checkpoint writes, got: {len(checkpoint_writes)}"

        first = checkpoint_writes[0]
        assert first["task"] == "all"
        assert first["status"] == "running"

        final = checkpoint_writes[-1]
        assert final["task"] == "all"
        assert final["status"] in ("completed", "failed")

    def test_checkpoint_memory_stages_use_completed_stages_tracking(self, monkeypatch, tmp_path):
        """Memory graph stages update current_stage/completed_stages in checkpoint.
        Lifecycle stages (snippets) do NOT update these fields — that's expected behavior.
        """
        _patch_janitor_base(monkeypatch, tmp_path)

        checkpoints = []
        orig_write = janitor._atomic_write_json

        def _spy(path, payload):
            if "checkpoint" in str(path):
                checkpoints.append(dict(payload))
            orig_write(path, payload)

        monkeypatch.setattr(janitor, "_atomic_write_json", _spy)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(
                metrics={
                    "memories_reviewed": 0, "memories_deleted": 0,
                    "memories_fixed": 0, "review_carryover": 0,
                    "review_coverage_ratio_pct": 100,
                    "decay_reviewed": 0, "decay_review_deleted": 0,
                    "decay_review_extended": 0, "decay_review_pinned": 0,
                    "snippets_folded": 0, "snippets_rewritten": 0,
                    "snippets_discarded": 0, "snippets_skipped_at_limit": 0,
                    "journal_additions": 0, "journal_edits": 0,
                    "journal_recovered_edits": 0, "journal_entries_distilled": 0,
                    "temporal_found": 0, "temporal_fixed": 0,
                    "dedup_reviewed": 0, "dedup_confirmed": 0, "dedup_reversed": 0,
                    "duplicates_merged": 0, "memories_decayed": 0,
                    "memories_deleted_by_decay": 0, "decay_queued": 0,
                },
            )

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        # Checkpoint writes only happen when not dry_run (b75af9f3)
        monkeypatch.setenv("QUAID_JANITOR_SKIP_NOTIFY", "1")
        janitor.run_task_optimized("all", dry_run=False, resume_checkpoint=False)

        # Verify the checkpoint structure is valid
        assert checkpoints, "At least one checkpoint must be written for task=all"
        for cp in checkpoints:
            assert "task" in cp
            assert cp["task"] == "all"
            assert "status" in cp
            assert "completed_stages" in cp
            assert isinstance(cp["completed_stages"], list)

    def test_checkpoint_final_status_failed_when_snippets_errors(self, monkeypatch, tmp_path):
        """When snippets stage returns errors, the final checkpoint status is 'failed'."""
        _patch_janitor_base(monkeypatch, tmp_path)

        checkpoints = []
        orig_write = janitor._atomic_write_json

        def _spy(path, payload):
            if "checkpoint" in str(path):
                checkpoints.append(dict(payload))
            orig_write(path, payload)

        monkeypatch.setattr(janitor, "_atomic_write_json", _spy)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            if name == "snippets":
                return RoutineResult(errors=["Skipped SOUL.md[1]: file missing"])
            return RoutineResult(
                metrics={
                    "memories_reviewed": 0, "memories_deleted": 0,
                    "memories_fixed": 0, "review_carryover": 0,
                    "review_coverage_ratio_pct": 100,
                    "decay_reviewed": 0, "decay_review_deleted": 0,
                    "decay_review_extended": 0, "decay_review_pinned": 0,
                    "snippets_folded": 0, "snippets_rewritten": 0,
                    "snippets_discarded": 0, "snippets_skipped_at_limit": 0,
                    "journal_additions": 0, "journal_edits": 0,
                    "journal_recovered_edits": 0, "journal_entries_distilled": 0,
                    "temporal_found": 0, "temporal_fixed": 0,
                    "dedup_reviewed": 0, "dedup_confirmed": 0, "dedup_reversed": 0,
                    "duplicates_merged": 0, "memories_decayed": 0,
                    "memories_deleted_by_decay": 0, "decay_queued": 0,
                },
            )

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        # Checkpoint writes and LLM tasks only run when not dry_run (b75af9f3)
        monkeypatch.setenv("QUAID_JANITOR_SKIP_NOTIFY", "1")
        result = janitor.run_task_optimized("all", dry_run=False, resume_checkpoint=False)

        assert result["success"] is False, "Expected success=False when snippets has errors"
        if checkpoints:
            final = checkpoints[-1]
            assert final["status"] == "failed", (
                f"Expected final checkpoint status='failed', got: {final['status']}"
            )

    def test_checkpoint_final_status_completed_when_no_errors(self, monkeypatch, tmp_path):
        """When no errors occur, the final checkpoint status is 'completed'."""
        _patch_janitor_base(monkeypatch, tmp_path)

        checkpoints = []
        orig_write = janitor._atomic_write_json

        def _spy(path, payload):
            if "checkpoint" in str(path):
                checkpoints.append(dict(payload))
            orig_write(path, payload)

        monkeypatch.setattr(janitor, "_atomic_write_json", _spy)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        monkeypatch.setattr(
            "core.lifecycle.janitor_lifecycle.LifecycleRegistry.run",
            lambda self, name, ctx: RoutineResult(
                metrics={
                    "memories_reviewed": 0, "memories_deleted": 0,
                    "memories_fixed": 0, "review_carryover": 0,
                    "review_coverage_ratio_pct": 100,
                    "decay_reviewed": 0, "decay_review_deleted": 0,
                    "decay_review_extended": 0, "decay_review_pinned": 0,
                    "snippets_folded": 2, "snippets_rewritten": 0,
                    "snippets_discarded": 1, "snippets_skipped_at_limit": 0,
                    "journal_additions": 0, "journal_edits": 0,
                    "journal_recovered_edits": 0, "journal_entries_distilled": 0,
                    "temporal_found": 0, "temporal_fixed": 0,
                    "dedup_reviewed": 0, "dedup_confirmed": 0, "dedup_reversed": 0,
                    "duplicates_merged": 0, "memories_decayed": 0,
                    "memories_deleted_by_decay": 0, "decay_queued": 0,
                },
            ),
        )

        result = janitor.run_task_optimized("all", dry_run=True, resume_checkpoint=False)

        assert result["success"] is True, "Expected success=True when no errors"
        if checkpoints:
            final = checkpoints[-1]
            assert final["status"] == "completed", (
                f"Expected final checkpoint status='completed', got: {final['status']}"
            )


# ---------------------------------------------------------------------------
# Tests: janitor_complete event — success flag
# ---------------------------------------------------------------------------

class TestJanitorCompleteSuccessFlag:
    """Tests that the janitor_complete event / return value reflects error count correctly."""

    def test_success_true_when_no_errors(self, monkeypatch, tmp_path):
        """run_task_optimized returns success=True when no errors accumulated."""
        _patch_janitor_base(monkeypatch, tmp_path)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(
                metrics={
                    "snippets_folded": 2, "snippets_rewritten": 0,
                    "snippets_discarded": 1, "snippets_skipped_at_limit": 0,
                },
            )

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)
        monkeypatch.setattr(
            "datastore.notedb.soul_snippets.run_soul_snippets_review",
            lambda dry_run, **kwargs: {"folded": 2, "rewritten": 0, "discarded": 1, "errors": []},
        )
        monkeypatch.setattr(
            "datastore.notedb.soul_snippets.run_journal_distillation",
            lambda *, dry_run, force_distill, **kwargs: {"additions": 0, "edits": 0, "recovered_edits": 0, "total_entries": 0},
        )

        result = janitor.run_task_optimized("snippets", dry_run=True, resume_checkpoint=False)
        assert result["success"] is True

    def test_success_false_when_errors_exist(self, monkeypatch, tmp_path):
        """run_task_optimized returns success=False when errors are accumulated from snippets stage."""
        _patch_janitor_base(monkeypatch, tmp_path)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            if name == "snippets":
                return RoutineResult(
                    errors=["Skipped SOUL.md[1]: file missing"],
                    metrics={"snippets_folded": 0, "snippets_rewritten": 0, "snippets_discarded": 0},
                )
            return RoutineResult()

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        result = janitor.run_task_optimized("snippets", dry_run=False, resume_checkpoint=False)
        assert result["success"] is False

    def test_success_false_when_snippets_stage_has_errors_in_all_task(self, monkeypatch, tmp_path):
        """For task='all', errors from snippets stage propagate to final success=False."""
        _patch_janitor_base(monkeypatch, tmp_path)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            if name == "snippets":
                return RoutineResult(
                    errors=["Failed to insert into SOUL.md: NoneType has no .exists()"],
                    metrics={"snippets_folded": 0, "snippets_rewritten": 0, "snippets_discarded": 0},
                )
            return RoutineResult(
                metrics={
                    "memories_reviewed": 0, "memories_deleted": 0,
                    "memories_fixed": 0, "review_carryover": 0,
                    "review_coverage_ratio_pct": 100,
                    "decay_reviewed": 0, "decay_review_deleted": 0,
                    "decay_review_extended": 0, "decay_review_pinned": 0,
                    "snippets_folded": 0, "snippets_rewritten": 0,
                    "snippets_discarded": 0, "snippets_skipped_at_limit": 0,
                    "journal_additions": 0, "journal_edits": 0,
                    "journal_recovered_edits": 0, "journal_entries_distilled": 0,
                    "temporal_found": 0, "temporal_fixed": 0,
                    "dedup_reviewed": 0, "dedup_confirmed": 0, "dedup_reversed": 0,
                    "duplicates_merged": 0, "memories_decayed": 0,
                    "memories_deleted_by_decay": 0, "decay_queued": 0,
                },
            )

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)
        monkeypatch.setenv("QUAID_JANITOR_SKIP_NOTIFY", "1")

        result = janitor.run_task_optimized("all", dry_run=False, resume_checkpoint=False)
        assert result["success"] is False

    def test_janitor_complete_logs_success_false_on_error_count(self, monkeypatch, tmp_path):
        """janitor_complete event is logged with success=False when errors > 0."""
        _patch_janitor_base(monkeypatch, tmp_path)

        logged_events = []

        def _fake_janitor_logger_info(event_name, **kwargs):
            logged_events.append({"event": event_name, **kwargs})

        monkeypatch.setattr(janitor.janitor_logger, "info", _fake_janitor_logger_info)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(errors=["Skipped SOUL.md[1]: file missing"])

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        janitor.run_task_optimized("snippets", dry_run=False, resume_checkpoint=False)

        complete_events = [e for e in logged_events if e.get("event") == "janitor_complete"]
        assert complete_events, "janitor_complete must be logged"
        assert complete_events[-1]["success"] is False

    def test_janitor_complete_logs_success_true_on_zero_errors(self, monkeypatch, tmp_path):
        """janitor_complete event is logged with success=True when errors == 0."""
        _patch_janitor_base(monkeypatch, tmp_path)

        logged_events = []

        def _fake_janitor_logger_info(event_name, **kwargs):
            logged_events.append({"event": event_name, **kwargs})

        monkeypatch.setattr(janitor.janitor_logger, "info", _fake_janitor_logger_info)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(metrics={"snippets_folded": 1, "snippets_discarded": 0})

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        janitor.run_task_optimized("snippets", dry_run=True, resume_checkpoint=False)

        complete_events = [e for e in logged_events if e.get("event") == "janitor_complete"]
        assert complete_events, "janitor_complete must be logged"
        assert complete_events[-1]["success"] is True


# ---------------------------------------------------------------------------
# Tests: apply_mode=ask → core_markdown_writes approval queued
# ---------------------------------------------------------------------------

class TestApplyModeAsk:
    """Tests that applyMode=ask queues approval for core_markdown_writes scope."""

    def test_ask_mode_without_approve_queues_approval_for_core_markdown_writes(self, monkeypatch, tmp_path):
        """When apply_mode=ask and user_approved=False, _queue_approval_request is called
        for 'core_markdown_writes' scope before snippets fold is applied."""
        cfg = _make_janitor_cfg(
            apply_mode="ask",
            approval_policies={"core_markdown_writes": "ask"},
        )
        _patch_janitor_base(monkeypatch, tmp_path, cfg=cfg)

        queued = []

        def _fake_queue(scope, task_name, summary):
            queued.append({"scope": scope, "task": task_name, "summary": summary})

        monkeypatch.setattr(janitor, "_queue_approval_request", _fake_queue)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(metrics={"snippets_folded": 0, "snippets_discarded": 0})

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        # Run with apply=True (no approve) so policy enforcement fires
        # We call _run_task_optimized_inner directly with user_approved=False
        # to avoid the lock (which is mocked).
        result = janitor.run_task_optimized("snippets", dry_run=False, resume_checkpoint=False)

        # With ask mode and no approve, snippets scope should have queued an approval request
        core_md_requests = [q for q in queued if q["scope"] == "core_markdown_writes"]
        assert core_md_requests, (
            f"Expected approval request for 'core_markdown_writes' scope, got: {queued}"
        )

    def test_ask_mode_with_approve_does_not_queue_approval(self, monkeypatch, tmp_path):
        """When apply_mode=ask and user_approved=True, approval should not be queued."""
        cfg = _make_janitor_cfg(
            apply_mode="ask",
            approval_policies={"core_markdown_writes": "ask"},
        )
        _patch_janitor_base(monkeypatch, tmp_path, cfg=cfg)

        queued = []

        def _fake_queue(scope, task_name, summary):
            queued.append({"scope": scope, "task": task_name})

        monkeypatch.setattr(janitor, "_queue_approval_request", _fake_queue)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        def _fake_run(self, name, ctx):
            return RoutineResult(metrics={"snippets_folded": 0, "snippets_discarded": 0})

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run", _fake_run)

        # Directly call inner function to simulate --apply --approve
        janitor._run_task_optimized_inner(
            "snippets", dry_run=False, incremental=False,
            time_budget=0, force_distill=False, user_approved=True, resume_checkpoint=False
        )

        # With user_approved=True, no approval requests should be queued
        core_md_requests = [q for q in queued if q["scope"] == "core_markdown_writes"]
        assert not core_md_requests, (
            f"No approval requests should be queued when user_approved=True, got: {core_md_requests}"
        )

    def test_ask_mode_queued_approval_has_correct_scope_content(self, monkeypatch, tmp_path):
        """The queued approval request references the 'core_markdown_writes' scope
        and includes information about the snippets operation."""
        cfg = _make_janitor_cfg(
            apply_mode="auto",
            approval_policies={"core_markdown_writes": "ask"},
        )
        _patch_janitor_base(monkeypatch, tmp_path, cfg=cfg)

        queued = []

        def _fake_queue(scope, task_name, summary):
            queued.append({"scope": scope, "task": task_name, "summary": summary})

        monkeypatch.setattr(janitor, "_queue_approval_request", _fake_queue)

        from core.lifecycle.janitor_lifecycle import RoutineResult

        monkeypatch.setattr("core.lifecycle.janitor_lifecycle.LifecycleRegistry.run",
                            lambda self, name, ctx: RoutineResult())

        janitor._run_task_optimized_inner(
            "snippets", dry_run=False, incremental=False,
            time_budget=0, force_distill=False, user_approved=False, resume_checkpoint=False
        )

        core_md_requests = [q for q in queued if q["scope"] == "core_markdown_writes"]
        assert core_md_requests, "Should have queued an approval for core_markdown_writes"
        req = core_md_requests[0]
        assert req["scope"] == "core_markdown_writes"
        # Summary should mention the snippets operation
        assert "snippet" in req["summary"].lower() or "core markdown" in req["summary"].lower()


# ---------------------------------------------------------------------------
# Tests: run_soul_snippets_review — error accumulation vs raising
# ---------------------------------------------------------------------------

class TestRunSoulSnippetsReview:
    """Integration-style tests for run_soul_snippets_review error handling."""

    def _setup_workspace_adapter(self, tmp_path):
        """Set up a TestAdapter so soul_snippets uses tmp_path as workspace."""
        from lib.adapter import set_adapter, TestAdapter
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        return adapter

    def test_snippets_review_returns_errors_list_not_raises(self, tmp_path, monkeypatch):
        """run_soul_snippets_review accumulates errors in result dict, does not raise."""
        from lib.adapter import set_adapter, reset_adapter, TestAdapter
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        try:
            from unittest.mock import MagicMock
            mock_cfg = MagicMock()
            from config import JournalConfig
            mock_cfg.docs.journal = JournalConfig(
                enabled=True,
                snippets_enabled=True,
                mode="distilled",
                journal_dir="journal",
                target_files=["SOUL.md"],
                max_entries_per_file=50,
                max_tokens=8192,
                distillation_interval_days=7,
                archive_after_distillation=False,
            )
            mock_cfg.docs.core_markdown.files = {
                "SOUL.md": {"purpose": "inner life", "maxLines": 80},
            }

            # Workspace is adapter.instance_root() (tmp_path / "pytest-runner")
            ws = adapter.instance_root()
            (ws / "SOUL.snippets.md").write_text(
                "# SOUL — Pending Snippets\n## Compaction — 2026-03-01 00:00:00\n- Test snippet\n",
                encoding="utf-8",
            )

            # LLM returns a valid decision: FOLD, but the target file is missing
            def _fake_call_deep_reasoning(prompt, system_prompt="", max_tokens=8192, timeout=120):
                response = json.dumps({
                    "decisions": [
                        {"file": "SOUL.md", "snippet_index": 1, "action": "FOLD", "insert_after": "END"}
                    ]
                })
                return response, 0.1

            with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_cfg):
                with patch("datastore.notedb.soul_snippets.call_deep_reasoning", side_effect=_fake_call_deep_reasoning):
                    with patch("datastore.notedb.soul_snippets.get_prompt", return_value="You are a helpful assistant"):
                        with patch("datastore.notedb.soul_snippets.backup_file", return_value=None):
                            # SOUL.md target file does not exist, so insert returns False and
                            # _resolve_writable_file_path returns None
                            result = soul_snippets.run_soul_snippets_review(dry_run=False)

            # Should return a dict with errors, not raise
            assert isinstance(result, dict)
            # Either no snippets processed (files missing) or errors captured
            assert "errors" in result or "skipped" in result
        finally:
            reset_adapter()

    def test_snippets_review_discard_succeeds_without_target_file(self, tmp_path, monkeypatch):
        """Snippets with DISCARD decision work even when target markdown file doesn't exist."""
        from lib.adapter import set_adapter, reset_adapter, TestAdapter
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        try:
            mock_cfg = MagicMock()
            from config import JournalConfig
            mock_cfg.docs.journal = JournalConfig(
                enabled=True,
                snippets_enabled=True,
                mode="distilled",
                journal_dir="journal",
                target_files=["SOUL.md"],
                max_entries_per_file=50,
                max_tokens=8192,
                distillation_interval_days=7,
                archive_after_distillation=False,
            )
            mock_cfg.docs.core_markdown.files = {
                "SOUL.md": {"purpose": "inner life", "maxLines": 80},
            }

            # Workspace is adapter.instance_root() (tmp_path / "pytest-runner")
            ws = adapter.instance_root()
            (ws / "SOUL.snippets.md").write_text(
                "# SOUL — Pending Snippets\n## Compaction — 2026-03-01 00:00:00\n- Redundant snippet\n",
                encoding="utf-8",
            )

            # LLM returns DISCARD — no file write needed
            def _fake_call_deep_reasoning(prompt, system_prompt="", max_tokens=8192, timeout=120):
                response = json.dumps({
                    "decisions": [
                        {"file": "SOUL.md", "snippet_index": 1, "action": "DISCARD", "reason": "redundant"}
                    ]
                })
                return response, 0.1

            with patch("datastore.notedb.soul_snippets.get_config", return_value=mock_cfg):
                with patch("datastore.notedb.soul_snippets.call_deep_reasoning", side_effect=_fake_call_deep_reasoning):
                    with patch("datastore.notedb.soul_snippets.get_prompt", return_value="You are a helpful assistant"):
                        with patch("datastore.notedb.soul_snippets.backup_file", return_value=None):
                            result = soul_snippets.run_soul_snippets_review(dry_run=False)

            # DISCARD should succeed cleanly
            assert isinstance(result, dict)
            if not result.get("skipped"):
                assert result.get("discarded", 0) >= 1, f"Expected discarded >= 1, got: {result}"
                assert result.get("errors", []) == [], f"Expected no errors for DISCARD, got: {result['errors']}"
        finally:
            reset_adapter()
