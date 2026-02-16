"""Tests for notify.py — notification formatting and delivery logic."""

import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure the plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from notify import (
    notify_memory_recall,
    notify_memory_extraction,
    notify_janitor_summary,
    notify_doc_update,
    notify_docs_search,
    notify_daily_memories,
    notify_user,
    get_last_channel,
    ChannelInfo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_channel_info():
    """Create a standard ChannelInfo for testing."""
    return ChannelInfo(
        channel="telegram",
        target="12345",
        account_id="default",
        session_key="agent:main:main",
    )


def _patch_notify_user():
    """Patch notify_user to capture the message instead of sending."""
    return patch("notify.notify_user", return_value=True)


# ---------------------------------------------------------------------------
# notify_memory_recall
# ---------------------------------------------------------------------------

class TestNotifyMemoryRecall:
    """Tests for notify_memory_recall() message formatting."""

    def test_empty_memories_returns_false(self):
        """No memories = nothing to notify."""
        assert notify_memory_recall([], dry_run=True) is False

    def test_direct_matches_formatted(self):
        """Direct matches include similarity percentage."""
        memories = [
            {"text": "Solomon lives in Bali", "similarity": 85, "via": "vector"},
            {"text": "Solomon has a cat named Madu", "similarity": 72, "via": "vector"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=70, dry_run=False)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Memory Context Loaded" in msg
            assert "Direct Matches" in msg
            assert "[85%]" in msg
            assert "[72%]" in msg
            assert "Solomon lives in Bali" in msg

    def test_low_similarity_filtered_out(self):
        """Memories below min_similarity are filtered."""
        memories = [
            {"text": "Solomon lives in Bali", "similarity": 85, "via": "vector"},
            {"text": "Unrelated low match", "similarity": 50, "via": "vector"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70)
            msg = mock_send.call_args[0][0]
            assert "[85%]" in msg
            assert "Unrelated low match" not in msg
            assert "1 low-confidence" in msg

    def test_all_below_threshold_returns_false(self):
        """If all memories are below threshold, nothing to show."""
        memories = [
            {"text": "Low match", "similarity": 30, "via": "vector"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=70)
            assert result is False
            mock_send.assert_not_called()

    def test_graph_discoveries_shown_separately(self):
        """Graph discoveries get their own section."""
        memories = [
            {"text": "Solomon lives in Bali", "similarity": 90, "via": "vector"},
            {"text": "Madu is Solomon's cat", "similarity": 0, "via": "graph"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70)
            msg = mock_send.call_args[0][0]
            assert "Direct Matches" in msg
            assert "Graph Discoveries" in msg
            assert "Madu is Solomon's cat" in msg

    def test_graph_only_no_direct_matches(self):
        """Graph discoveries shown even without direct matches."""
        memories = [
            {"text": "Madu is Solomon's cat", "similarity": 0, "via": "graph"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=70)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Graph Discoveries" in msg
            assert "Direct Matches" not in msg

    @patch("notify._notify_full_text", return_value=False)
    def test_long_text_truncated(self, _mock_ft):
        """Memory text longer than 120 chars is truncated."""
        long_text = "A" * 200
        memories = [{"text": long_text, "similarity": 90, "via": "vector"}]
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70)
            msg = mock_send.call_args[0][0]
            assert "..." in msg
            # Truncated to 117 + "..."
            assert "A" * 118 not in msg

    def test_source_breakdown_included(self):
        """Source breakdown adds query and source counts."""
        memories = [
            {"text": "Solomon lives in Bali", "similarity": 90, "via": "vector"},
        ]
        breakdown = {
            "vector_count": 3,
            "graph_count": 1,
            "query": "where does Solomon live",
            "pronoun_resolved": True,
            "owner_person": "Solomon Steadman",
        }
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70, source_breakdown=breakdown)
            msg = mock_send.call_args[0][0]
            assert "3 vector" in msg
            assert "1 graph" in msg
            assert "where does Solomon live" in msg
            assert "Solomon Steadman" in msg

    def test_source_breakdown_pronoun_no_person(self):
        """Pronoun resolved without explicit person shows checkmark."""
        memories = [
            {"text": "Solomon lives in Bali", "similarity": 90, "via": "vector"},
        ]
        breakdown = {
            "vector_count": 1,
            "graph_count": 0,
            "pronoun_resolved": True,
        }
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70, source_breakdown=breakdown)
            msg = mock_send.call_args[0][0]
            assert "Pronoun:" in msg

    def test_string_memory_fallback(self):
        """Non-dict memories are treated as strings with zero similarity."""
        memories = ["Simple string memory"]
        # String memories have similarity 0, so they'll be filtered at default 70
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=0)
            # similarity=0 and via="vector", so it won't pass min_similarity=0 check
            # Actually 0 >= 0 is True, so it should show
            assert result is True


# ---------------------------------------------------------------------------
# notify_memory_extraction
# ---------------------------------------------------------------------------

class TestNotifyMemoryExtraction:
    """Tests for notify_memory_extraction() message formatting."""

    def test_nothing_to_report_returns_false(self):
        """No facts stored, no edges, no details = nothing to report."""
        assert notify_memory_extraction(0, 0, 0, dry_run=True) is False

    def test_basic_summary(self):
        """Summary line includes counts."""
        with _patch_notify_user() as mock_send:
            result = notify_memory_extraction(5, 2, 3, trigger="compaction")
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Memory Extraction" in msg
            assert "5 stored" in msg
            assert "2 skipped" in msg
            assert "3 edges" in msg
            assert "Context compacted" in msg

    def test_trigger_labels(self):
        """Trigger names map to human-readable labels."""
        for trigger, expected in [
            ("compaction", "Context compacted"),
            ("reset", "Session reset (/new)"),
            ("extraction", "Extraction complete"),
        ]:
            with _patch_notify_user() as mock_send:
                notify_memory_extraction(1, 0, 0, trigger=trigger)
                msg = mock_send.call_args[0][0]
                assert expected in msg

    def test_details_with_stored_fact(self):
        """Stored facts get checkmark emoji."""
        details = [
            {"text": "Solomon prefers dark mode", "status": "stored"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(1, 0, 0, details=details)
            msg = mock_send.call_args[0][0]
            assert "Solomon prefers dark mode" in msg

    def test_details_with_duplicate(self):
        """Duplicate facts show reason."""
        details = [
            {"text": "Solomon lives in Bali", "status": "duplicate", "reason": "existing fact #42"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(0, 1, 0, details=details)
            msg = mock_send.call_args[0][0]
            assert "existing fact #42" in msg

    def test_details_with_skipped(self):
        """Skipped facts show reason in parentheses."""
        details = [
            {"text": "Too vague", "status": "skipped", "reason": "too short"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(0, 1, 0, details=details)
            msg = mock_send.call_args[0][0]
            assert "too short" in msg

    def test_details_with_edges(self):
        """Facts with edges show them indented."""
        details = [
            {"text": "Wendy is Solomon's mother", "status": "stored",
             "edges": ["Wendy parent_of Solomon"]},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(1, 0, 1, details=details)
            msg = mock_send.call_args[0][0]
            assert "Wendy parent_of Solomon" in msg

    @patch("notify._notify_full_text", return_value=False)
    def test_long_detail_text_truncated(self, _mock_ft):
        """Fact text in details is truncated at 80 chars."""
        details = [
            {"text": "X" * 100, "status": "stored"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(1, 0, 0, details=details)
            msg = mock_send.call_args[0][0]
            assert "..." in msg
            # Should be truncated to 80 + "..."
            assert "X" * 81 not in msg

    def test_zero_stored_with_details_still_shows(self):
        """Even with 0 stored, if details exist, show them."""
        details = [
            {"text": "Something was skipped", "status": "skipped", "reason": "low quality"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_extraction(0, 1, 0, details=details)
            assert result is True


# ---------------------------------------------------------------------------
# notify_janitor_summary
# ---------------------------------------------------------------------------

class TestNotifyJanitorSummary:
    """Tests for notify_janitor_summary() message formatting."""

    def test_basic_summary(self):
        """Shows duration and changes."""
        metrics = {"total_duration_seconds": 45, "llm_calls": 12, "errors": 0}
        changes = {"reviewed": 20, "kept": 18, "deleted": 2}
        with _patch_notify_user() as mock_send:
            result = notify_janitor_summary(metrics, changes)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Nightly Janitor Complete" in msg
            assert "45s" in msg
            assert "12" in msg  # LLM calls
            assert "20" in msg  # reviewed

    def test_duration_minutes(self):
        """Duration >= 60s shown in minutes."""
        metrics = {"total_duration_seconds": 120}
        changes = {"kept": 5}
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "2.0min" in msg

    def test_errors_shown(self):
        """Errors are displayed when present."""
        metrics = {"total_duration_seconds": 10, "errors": 3}
        changes = {}
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "3" in msg

    def test_no_changes_message(self):
        """Empty changes dict shows 'No changes applied'."""
        metrics = {"total_duration_seconds": 5}
        changes = {}
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "No changes applied" in msg

    def test_all_change_types(self):
        """All recognized change types are displayed."""
        metrics = {"total_duration_seconds": 90}
        changes = {
            "reviewed": 10,
            "kept": 8,
            "deleted": 1,
            "fixed": 2,
            "merged": 3,
            "edges_created": 5,
            "contradictions_found": 1,
            "duplicates_rejected": 4,
            "decayed": 2,
        }
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "10" in msg  # reviewed
            assert "8" in msg   # kept
            assert "Merged" in msg
            assert "Decayed" in msg
            assert "Contradictions" in msg

    def test_zero_counts_hidden(self):
        """Zero-count changes are not shown."""
        metrics = {"total_duration_seconds": 5}
        changes = {"reviewed": 5, "deleted": 0, "merged": 0}
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "Reviewed" in msg
            # Deleted: 0 should not appear as a listed change
            assert "Deleted" not in msg

    def test_no_llm_calls_hidden(self):
        """When llm_calls is 0, the line is not shown."""
        metrics = {"total_duration_seconds": 5, "llm_calls": 0}
        changes = {"kept": 1}
        with _patch_notify_user() as mock_send:
            notify_janitor_summary(metrics, changes)
            msg = mock_send.call_args[0][0]
            assert "LLM calls" not in msg


# ---------------------------------------------------------------------------
# notify_doc_update
# ---------------------------------------------------------------------------

class TestNotifyDocUpdate:
    """Tests for notify_doc_update() message formatting."""

    def test_basic_doc_update(self):
        """Basic doc update notification includes filename and trigger."""
        with _patch_notify_user() as mock_send:
            result = notify_doc_update("/path/to/projects/quaid/janitor-reference.md", "janitor")
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "janitor-reference.md" in msg
            assert "nightly maintenance" in msg

    def test_summary_included(self):
        """Summary is included when provided."""
        with _patch_notify_user() as mock_send:
            notify_doc_update("docs/foo.md", "compact", summary="Added new section")
            msg = mock_send.call_args[0][0]
            assert "Added new section" in msg

    @patch("notify._notify_full_text", return_value=False)
    def test_summary_truncated(self, _mock_ft):
        """Long summary is truncated to 200 chars."""
        long_summary = "A" * 300
        with _patch_notify_user() as mock_send:
            notify_doc_update("docs/foo.md", "compact", summary=long_summary)
            msg = mock_send.call_args[0][0]
            assert "..." in msg

    def test_trigger_descriptions(self):
        """All trigger types map to descriptions."""
        triggers = {
            "compact": "conversation compaction",
            "reset": "session reset",
            "janitor": "nightly maintenance",
            "manual": "manual request",
            "on-demand": "staleness detection",
        }
        for trigger, expected in triggers.items():
            with _patch_notify_user() as mock_send:
                notify_doc_update("docs/test.md", trigger)
                msg = mock_send.call_args[0][0]
                assert expected in msg


# ---------------------------------------------------------------------------
# notify_docs_search
# ---------------------------------------------------------------------------

class TestNotifyDocsSearch:
    """Tests for notify_docs_search() message formatting."""

    def test_empty_results_returns_false(self):
        assert notify_docs_search("test query", []) is False

    def test_results_formatted(self):
        results = [
            {"doc": "memory-system-design.md", "section": "Architecture", "score": 0.92},
            {"doc": "janitor-reference.md", "section": "Pipeline", "score": 0.85},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_docs_search("memory architecture", results)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "memory architecture" in msg
            assert "memory-system-design.md" in msg
            assert "92%" in msg

    def test_max_five_results(self):
        """Only top 5 results shown, with overflow count."""
        results = [{"doc": f"doc{i}.md", "section": f"Sec{i}", "score": 0.9 - i * 0.1}
                    for i in range(8)]
        with _patch_notify_user() as mock_send:
            notify_docs_search("query", results)
            msg = mock_send.call_args[0][0]
            assert "3 more" in msg


# ---------------------------------------------------------------------------
# notify_daily_memories
# ---------------------------------------------------------------------------

class TestNotifyDailyMemories:
    """Tests for notify_daily_memories() message formatting."""

    def test_empty_returns_false(self):
        assert notify_daily_memories([]) is False

    def test_groups_by_category(self):
        memories = [
            {"text": "Solomon lives in Bali", "category": "fact"},
            {"text": "Prefers dark mode", "category": "preference"},
            {"text": "Another fact", "category": "fact"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_daily_memories(memories)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "3 memories" in msg
            assert "Fact" in msg
            assert "Preference" in msg


# ---------------------------------------------------------------------------
# get_last_channel
# ---------------------------------------------------------------------------

class TestGetLastChannel:
    """Tests for get_last_channel() session lookup."""

    def test_returns_none_when_no_sessions_file(self):
        """No sessions file returns None."""
        with patch("notify.get_sessions_path", return_value=None):
            assert get_last_channel() is None

    def test_returns_channel_info(self, tmp_path):
        """Valid sessions file returns ChannelInfo."""
        sessions = {
            "agent:main:main": {
                "lastChannel": "telegram",
                "lastTo": "12345",
                "lastAccountId": "default",
            }
        }
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps(sessions))

        with patch("notify.get_sessions_path", return_value=sessions_file):
            info = get_last_channel()
            assert info is not None
            assert info.channel == "telegram"
            assert info.target == "12345"

    def test_missing_session_key_returns_none(self, tmp_path):
        """Session key not in file returns None."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({"other_session": {}}))

        with patch("notify.get_sessions_path", return_value=sessions_file):
            assert get_last_channel() is None

    def test_missing_channel_returns_none(self, tmp_path):
        """Session without lastChannel returns None."""
        sessions = {
            "agent:main:main": {"lastTo": "12345"}
        }
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps(sessions))

        with patch("notify.get_sessions_path", return_value=sessions_file):
            assert get_last_channel() is None


# ---------------------------------------------------------------------------
# notify_user (subprocess integration)
# ---------------------------------------------------------------------------

class TestNotifyUser:
    """Tests for notify_user() subprocess call."""

    def test_no_channel_returns_false(self):
        """No channel info returns False."""
        with patch("notify.get_last_channel", return_value=None):
            assert notify_user("test message") is False

    def test_dry_run_does_not_call_subprocess(self):
        """dry_run prints command but doesn't execute."""
        with patch("notify.get_last_channel", return_value=_make_channel_info()), \
             patch("subprocess.run") as mock_run:
            result = notify_user("test message", dry_run=True)
            assert result is True
            mock_run.assert_not_called()

    def test_successful_send(self):
        """Successful subprocess call returns True."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("notify.get_last_channel", return_value=_make_channel_info()), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            result = notify_user("hello world")
            assert result is True
            call_args = mock_run.call_args[0][0]
            assert "clawdbot" in call_args
            assert "hello world" in call_args

    def test_failed_send(self):
        """Failed subprocess call returns False."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "connection refused"
        with patch("notify.get_last_channel", return_value=_make_channel_info()), \
             patch("subprocess.run", return_value=mock_result):
            result = notify_user("hello world")
            assert result is False

    def test_timeout_returns_false(self):
        """Subprocess timeout returns False."""
        import subprocess
        with patch("notify.get_last_channel", return_value=_make_channel_info()), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            result = notify_user("hello world")
            assert result is False

    def test_non_default_account(self):
        """Non-default account ID adds --account flag."""
        info = ChannelInfo(
            channel="whatsapp",
            target="999",
            account_id="work",
            session_key="agent:main:main",
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("notify.get_last_channel", return_value=info), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            notify_user("test")
            call_args = mock_run.call_args[0][0]
            assert "--account" in call_args
            assert "work" in call_args
