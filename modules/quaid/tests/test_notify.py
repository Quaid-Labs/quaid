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

from lib.agent_notice import clear_pending_notices_by_source
from core.runtime.notify import (
    notify_memory_recall,
    notify_memory_extraction,
    notify_agent,
    format_janitor_summary_message,
    notify_user,
    get_last_channel,
    ChannelInfo,
    _notify_full_text,
    _resolve_channel,
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
    return patch("core.runtime.notify.notify_user", return_value=True)


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
            {"text": "Quaid lives in Bali", "similarity": 85, "via": "vector"},
            {"text": "Quaid has a cat named Richter", "similarity": 72, "via": "vector"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=70, dry_run=False)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Memory Recall" in msg
            assert "Direct Matches" in msg
            assert "[85%]" in msg
            assert "[72%]" in msg
            assert "Quaid lives in Bali" in msg

    def test_low_similarity_filtered_out(self):
        """Memories below min_similarity are filtered."""
        memories = [
            {"text": "Quaid lives in Bali", "similarity": 85, "via": "vector"},
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
            {"text": "Quaid lives in Bali", "similarity": 90, "via": "vector"},
            {"text": "Richter is Quaid's cat", "similarity": 0, "via": "graph"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70)
            msg = mock_send.call_args[0][0]
            assert "Direct Matches" in msg
            assert "Graph Discoveries" in msg
            assert "Richter is Quaid's cat" in msg

    def test_graph_only_no_direct_matches(self):
        """Graph discoveries shown even without direct matches."""
        memories = [
            {"text": "Richter is Quaid's cat", "similarity": 0, "via": "graph"},
        ]
        with _patch_notify_user() as mock_send:
            result = notify_memory_recall(memories, min_similarity=70)
            assert result is True
            msg = mock_send.call_args[0][0]
            assert "Graph Discoveries" in msg
            assert "Direct Matches" not in msg

    @patch("core.runtime.notify._notify_full_text", return_value=False)
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
            {"text": "Quaid lives in Bali", "similarity": 90, "via": "vector"},
        ]
        breakdown = {
            "vector_count": 3,
            "graph_count": 1,
            "query": "where does Quaid live",
            "pronoun_resolved": True,
            "owner_person": "Douglas Quaid",
        }
        with _patch_notify_user() as mock_send:
            notify_memory_recall(memories, min_similarity=70, source_breakdown=breakdown)
            msg = mock_send.call_args[0][0]
            assert "3 vector" in msg
            assert "1 graph" in msg
            assert "where does Quaid live" in msg
            assert "Douglas Quaid" in msg

    def test_source_breakdown_pronoun_no_person(self):
        """Pronoun resolved without explicit person shows checkmark."""
        memories = [
            {"text": "Quaid lives in Bali", "similarity": 90, "via": "vector"},
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

    def test_trigger_labels_full_only(self):
        """Trigger names are shown only when extraction verbosity is full."""
        for trigger, expected in [
            ("compaction", "Context compacted"),
            ("reset", "Session reset (/new)"),
            ("extraction", "Extraction complete"),
        ]:
            with _patch_notify_user() as mock_send, patch("config.get_config") as mock_cfg:
                mock_cfg.return_value.notifications.effective_level.return_value = "full"
                notify_memory_extraction(1, 0, 0, trigger=trigger)
                msg = mock_send.call_args[0][0]
                assert expected in msg

    def test_no_facts_summary_is_one_line(self):
        """Zero-result always-notify path is concise in summary mode."""
        with _patch_notify_user() as mock_send, patch("config.get_config") as mock_cfg:
            mock_cfg.return_value.notifications.effective_level.return_value = "summary"
            notify_memory_extraction(0, 0, 0, trigger="timeout", details=None, always_notify=True)
            msg = mock_send.call_args[0][0]
            assert "No facts found" in msg
            assert "Trigger:" not in msg

    def test_details_with_stored_fact(self):
        """Stored facts get checkmark emoji."""
        details = [
            {"text": "Quaid prefers dark mode", "status": "stored"},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(1, 0, 0, details=details)
            msg = mock_send.call_args[0][0]
            assert "Quaid prefers dark mode" in msg

    def test_details_with_duplicate(self):
        """Duplicate facts show reason."""
        details = [
            {"text": "Quaid lives in Bali", "status": "duplicate", "reason": "existing fact #42"},
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
            {"text": "Lori is Quaid's mother", "status": "stored",
             "edges": ["Lori parent_of Quaid"]},
        ]
        with _patch_notify_user() as mock_send:
            notify_memory_extraction(1, 0, 1, details=details)
            msg = mock_send.call_args[0][0]
            assert "Lori parent_of Quaid" in msg

    @patch("core.runtime.notify._notify_full_text", return_value=False)
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
# get_last_channel
# ---------------------------------------------------------------------------

class TestGetLastChannel:
    """Tests for get_last_channel() — delegates to adapter."""

    def test_returns_none_when_no_sessions_file(self):
        """No sessions file returns None (adapter returns None)."""
        from lib.adapter import StandaloneAdapter, set_adapter, reset_adapter
        set_adapter(StandaloneAdapter())  # Standalone always returns None
        try:
            assert get_last_channel() is None
        finally:
            reset_adapter()

    def test_returns_channel_info(self, tmp_path):
        """Valid sessions file returns ChannelInfo via OpenClawAdapter."""
        sessions = {
            "agent:main:main": {
                "lastChannel": "telegram",
                "lastTo": "12345",
                "lastAccountId": "default",
            }
        }
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps(sessions))

        from lib.adapter import set_adapter, reset_adapter
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        # Patch the internal method to point to our test file
        adapter._find_sessions_json = lambda: sessions_file
        set_adapter(adapter)
        try:
            info = get_last_channel()
            assert info is not None
            assert info.channel == "telegram"
            assert info.target == "12345"
        finally:
            reset_adapter()

    def test_missing_session_key_returns_none(self, tmp_path):
        """Session key not in file returns None."""
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps({"other_session": {}}))

        from lib.adapter import set_adapter, reset_adapter
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        adapter._find_sessions_json = lambda: sessions_file
        set_adapter(adapter)
        try:
            assert get_last_channel() is None
        finally:
            reset_adapter()

    def test_missing_channel_returns_none(self, tmp_path):
        """Session without lastChannel returns None."""
        sessions = {
            "agent:main:main": {"lastTo": "12345"}
        }
        sessions_file = tmp_path / "sessions.json"
        sessions_file.write_text(json.dumps(sessions))

        from lib.adapter import set_adapter, reset_adapter
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        adapter._find_sessions_json = lambda: sessions_file
        set_adapter(adapter)
        try:
            assert get_last_channel() is None
        finally:
            reset_adapter()


# ---------------------------------------------------------------------------
# notify_user (subprocess integration)
# ---------------------------------------------------------------------------

class TestNotifyUser:
    """Tests for notify_user() — delegates to adapter.notify()."""

    def test_no_channel_returns_false(self):
        """OpenClaw adapter with no channel info returns False."""
        from lib.adapter import set_adapter, reset_adapter
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        adapter._find_sessions_json = lambda: None
        set_adapter(adapter)
        try:
            assert notify_user("test message") is False
        finally:
            reset_adapter()


class TestNotifyAgent:
    def test_dedupes_repeated_notices(self, tmp_path):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path
        adapter.notify.return_value = True

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            assert notify_agent("provider down", dedupe_key="provider-down", ttl_seconds=3600) is True
            assert notify_agent("provider down", dedupe_key="provider-down", ttl_seconds=3600) is True

        assert adapter.notify.call_count == 1
        sent_message = adapter.notify.call_args.args[0]
        assert sent_message.startswith("[Quaid warning]")

    def test_dedupe_state_honors_quaid_now(self, tmp_path, monkeypatch):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path
        adapter.notify.return_value = True

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:00:00Z")
            assert notify_agent("provider down", dedupe_key="provider-down", ttl_seconds=3600) is True
            state_path = tmp_path / "agent-notice-state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            assert state["provider-down"] == 1773205200.0

            monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:30:00Z")
            assert notify_agent("provider down", dedupe_key="provider-down", ttl_seconds=3600) is True

            monkeypatch.setenv("QUAID_NOW", "2026-03-11T06:01:00Z")
            assert notify_agent("provider down", dedupe_key="provider-down", ttl_seconds=3600) is True

        assert adapter.notify.call_count == 2

    @pytest.mark.parametrize("source", ["provider", "llm_config"])
    def test_does_not_dedupe_active_provider_errors(self, tmp_path, source):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path
        adapter.notify.return_value = True

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            assert notify_agent(
                "configured model invalid-model-m6-probe does not exist",
                severity="error",
                source=source,
                dedupe_key=f"{source}:invalid-model-m6-probe",
                ttl_seconds=3600,
            ) is True
            assert notify_agent(
                "configured model invalid-model-m6-probe does not exist",
                severity="error",
                source=source,
                dedupe_key=f"{source}:invalid-model-m6-probe",
                ttl_seconds=3600,
            ) is True

        assert adapter.notify.call_count == 2

    def test_active_provider_error_notice_can_be_relayed_on_consecutive_turns(self, tmp_path):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path

        pending_notices = []

        def _queue_pending(message, **_kwargs):
            pending_notices.append(str(message))
            return True

        def _drain_pending():
            if not pending_notices:
                return ""
            messages = list(pending_notices)
            pending_notices.clear()
            body = "\n".join(f"• {message}" for message in messages)
            return (
                "The following are pending notifications for the user — please relay them in your response:\n\n"
                f"<quaid_system_message>\n{body}\n</quaid_system_message>"
            )

        adapter.notify.side_effect = _queue_pending
        adapter.get_pending_context.side_effect = _drain_pending

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            assert notify_agent(
                "HTTP 404 configured model invalid-model-m6-probe does not exist",
                severity="error",
                source="provider",
                dedupe_key="m6-provider-404",
                ttl_seconds=900,
            ) is True
            first_turn = adapter.get_pending_context()
            assert notify_agent(
                "HTTP 404 configured model invalid-model-m6-probe does not exist",
                severity="error",
                source="provider",
                dedupe_key="m6-provider-404",
                ttl_seconds=900,
            ) is True
            second_turn = adapter.get_pending_context()

        assert "invalid-model-m6-probe" in first_turn
        assert "invalid-model-m6-probe" in second_turn

    def test_falls_back_to_deferred_when_notify_returns_false(self, tmp_path):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path
        adapter.notify.return_value = False

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            ok = notify_agent("provider down", severity="error", source="provider")
            assert ok is True

        delayed = tmp_path / ".runtime" / "notes" / "delayed-llm-requests.json"
        payload = json.loads(delayed.read_text(encoding="utf-8"))
        requests = payload.get("requests", [])
        assert len(requests) == 1
        assert requests[0]["priority"] == "high"
        assert "[Quaid error] [provider] provider down" in requests[0]["message"]

    def test_openclaw_provider_error_uses_deferred_not_direct_notification(self, tmp_path):
        adapter = MagicMock()
        adapter.get_capability.side_effect = (
            lambda key, default=False: True if key == "turn_scoped_provider_notices" else default
        )
        adapter.data_dir.return_value = tmp_path / "data"
        adapter.instance_root.return_value = tmp_path
        adapter.notify.return_value = True

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            ok = notify_agent(
                "configured model invalid-model-m6-probe does not exist",
                severity="error",
                source="provider",
                dedupe_key="m6-provider-404",
                ttl_seconds=900,
            )

        assert ok is True
        adapter.notify.assert_not_called()
        delayed = tmp_path / ".runtime" / "notes" / "delayed-llm-requests.json"
        payload = json.loads(delayed.read_text(encoding="utf-8"))
        requests = payload.get("requests", [])
        assert len(requests) == 1
        assert requests[0]["source"] == "provider"
        assert requests[0]["priority"] == "high"
        assert "[Quaid error] [provider]" in requests[0]["message"]
        assert "invalid-model-m6-probe" in requests[0]["message"]

    def test_falls_back_to_deferred_when_notify_raises(self, tmp_path):
        adapter = MagicMock()
        adapter.data_dir.return_value = tmp_path
        adapter.instance_root.return_value = tmp_path
        adapter.notify.side_effect = RuntimeError("bad channel")

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            ok = notify_agent("janitor summary", severity="warning", source="janitor")
            assert ok is True

        delayed = tmp_path / ".runtime" / "notes" / "delayed-llm-requests.json"
        payload = json.loads(delayed.read_text(encoding="utf-8"))
        requests = payload.get("requests", [])
        assert len(requests) == 1
        assert requests[0]["priority"] == "normal"
        assert "[Quaid warning] [janitor] janitor summary" in requests[0]["message"]

    def test_clear_pending_notices_by_source_prunes_provider_rows(self, tmp_path, monkeypatch):
        from adaptors.claude_code.adapter import ClaudeCodeAdapter

        monkeypatch.setenv("QUAID_INSTANCE", "claude-code-clear-pending")
        adapter = ClaudeCodeAdapter(home=tmp_path)
        pending_path = adapter.data_dir() / "cc-pending-notifications.jsonl"
        pending_path.parent.mkdir(parents=True, exist_ok=True)
        pending_path.write_text(
            "\n".join(
                [
                    json.dumps({"message": "[Quaid error] [provider] invalid-model-a"}),
                    json.dumps({"message": "[Quaid error] [llm_config] invalid-model-b"}),
                    json.dumps({"message": "[Quaid warning] [janitor] nightly summary"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            removed = clear_pending_notices_by_source(sources={"provider", "llm_config"})

        assert removed == 2
        remaining = pending_path.read_text(encoding="utf-8")
        assert "invalid-model-a" not in remaining
        assert "invalid-model-b" not in remaining
        assert "nightly summary" in remaining

    def test_clear_pending_notices_by_source_scrubs_adapter_files_under_instance_data_dir(self, tmp_path):
        class _StandaloneLikeAdapter:
            def data_dir(self):
                return tmp_path / "instances" / "codex-test" / "data"

        adapter = _StandaloneLikeAdapter()
        pending_dir = adapter.data_dir()
        pending_dir.mkdir(parents=True, exist_ok=True)
        codex_pending = pending_dir / "codex-pending-notifications.jsonl"
        codex_pending.write_text(
            "\n".join(
                [
                    json.dumps({"message": "[Quaid error] [provider] stale-provider"}),
                    json.dumps({"message": "[Quaid warning] [janitor] nightly summary"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("lib.agent_notice.get_adapter", return_value=adapter):
            removed = clear_pending_notices_by_source(sources={"provider"})

        assert removed == 1
        remaining = codex_pending.read_text(encoding="utf-8")
        assert "stale-provider" not in remaining
        assert "nightly summary" in remaining


class TestDeferredNotifyCli:
    def test_deferred_status_cli_uses_wrapper_with_options(self, monkeypatch, capsys):
        import core.runtime.notify as notify_mod

        monkeypatch.setattr(
            notify_mod,
            "_ctx_get_deferred_notice_status",
            lambda limit=500, include_items=False: {
                "pending_count": 1,
                "kinds": {"janitor_summary": 1},
                "priorities": {"normal": 1},
                "items": [
                    {
                        "kind": "janitor_summary",
                        "priority": "normal",
                        "created_at": "2026-04-04T11:11:11+00:00",
                        "message": "Nightly janitor completed.",
                    }
                ] if include_items else [],
            },
        )
        monkeypatch.setattr(notify_mod.sys, "argv", ["notify.py", "--deferred-status", "--json", "--limit", "3"])

        notify_mod.main()

        out = json.loads(capsys.readouterr().out)
        assert out["pending_count"] == 1
        assert out["items"][0]["kind"] == "janitor_summary"


class TestNotifyFallbackVisibility:
    def test_notify_full_text_config_error_logs_warning_when_fail_hard_disabled(self, caplog):
        caplog.set_level("WARNING")
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=False):
            assert _notify_full_text() is False
        assert "Failed to read notifications.full_text config" in caplog.text

    def test_notify_full_text_config_error_raises_when_fail_hard_enabled(self):
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="fail-hard mode"):
                _notify_full_text()

    def test_notify_memory_extraction_config_error_falls_back_when_fail_hard_disabled(self):
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=False), \
             _patch_notify_user() as mock_send:
            assert notify_memory_extraction(1, 0, 0, always_notify=True) is True
            assert mock_send.called

    def test_notify_memory_extraction_config_error_raises_when_fail_hard_enabled(self):
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="fail-hard mode"):
                notify_memory_extraction(1, 0, 0, always_notify=True)

    def test_resolve_channel_config_error_warns_when_fail_hard_disabled(self, caplog):
        caplog.set_level("WARNING")
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=False):
            assert _resolve_channel("memory_recall") is None
        assert "Failed to resolve notification channel override" in caplog.text

    def test_resolve_channel_config_error_raises_when_fail_hard_enabled(self):
        with patch("config.get_config", side_effect=RuntimeError("bad config")), \
             patch("core.runtime.notify.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="Failed to resolve notification channel override"):
                _resolve_channel("memory_recall")

    def test_dry_run_does_not_call_subprocess(self):
        """dry_run prints command but doesn't execute."""
        from lib.adapter import set_adapter, reset_adapter, ChannelInfo
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(channel="telegram", target="123",
                                account_id="default", session_key="test")
        adapter.get_last_channel = lambda s="": mock_info
        adapter._resolve_message_cli = lambda: "openclaw"
        set_adapter(adapter)
        try:
            with patch("adaptors.openclaw.adapter.subprocess.run") as mock_run:
                result = notify_user("test message", dry_run=True)
                assert result is True
                mock_run.assert_not_called()
        finally:
            reset_adapter()

    def test_successful_send(self):
        """Successful subprocess call returns True."""
        from lib.adapter import set_adapter, reset_adapter, ChannelInfo
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(channel="telegram", target="123",
                                account_id="default", session_key="test")
        adapter.get_last_channel = lambda s="": mock_info
        set_adapter(adapter)
        mock_result = MagicMock()
        mock_result.returncode = 0
        try:
            adapter._resolve_message_cli = lambda: "openclaw"
            with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
                result = notify_user("hello world")
                assert result is True
                call_args = mock_run.call_args[0][0]
                assert call_args[0] == "openclaw"
                assert "hello world" in call_args
        finally:
            reset_adapter()

    def test_failed_send(self):
        """Failed subprocess call returns False."""
        from lib.adapter import set_adapter, reset_adapter, ChannelInfo
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        mock_info = ChannelInfo(channel="telegram", target="123",
                                account_id="default", session_key="test")
        adapter.get_last_channel = lambda s="": mock_info
        set_adapter(adapter)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "connection refused"
        try:
            with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result):
                result = notify_user("hello world")
                assert result is False
        finally:
            reset_adapter()

    def test_timeout_returns_false(self):
        """Subprocess timeout returns False."""
        import subprocess as _subprocess
        from lib.adapter import set_adapter, reset_adapter, ChannelInfo as _CI
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        mock_info = _CI(channel="telegram", target="123",
                        account_id="default", session_key="test")
        adapter.get_last_channel = lambda s="": mock_info
        set_adapter(adapter)
        try:
            with patch("adaptors.openclaw.adapter.subprocess.run",
                       side_effect=_subprocess.TimeoutExpired("cmd", 30)), \
                 patch("adaptors.openclaw.adapter.is_fail_hard_enabled", return_value=False):
                result = notify_user("hello world")
                assert result is False
        finally:
            reset_adapter()

    def test_non_default_account(self):
        """Non-default account ID adds --account flag."""
        from lib.adapter import set_adapter, reset_adapter, ChannelInfo as _CI
        from adaptors.openclaw.adapter import OpenClawAdapter
        adapter = OpenClawAdapter()
        info = _CI(channel="whatsapp", target="999",
                   account_id="work", session_key="test")
        adapter.get_last_channel = lambda s="": info
        adapter._resolve_message_cli = lambda: "openclaw"
        set_adapter(adapter)
        mock_result = MagicMock()
        mock_result.returncode = 0
        try:
            with patch("adaptors.openclaw.adapter.subprocess.run", return_value=mock_result) as mock_run:
                notify_user("test")
                call_args = mock_run.call_args[0][0]
                assert "--account" in call_args
                assert "work" in call_args
        finally:
            reset_adapter()


class TestJanitorSummaryFormatting:
    def test_includes_update_alert_block(self):
        msg = format_janitor_summary_message(
            metrics={"duration_seconds": 12.3, "errors": 0},
            applied_changes={
                "reviewed": 2,
                "update_available": {
                    "current": "0.2.15-alpha",
                    "latest": "0.2.16-alpha",
                    "message": "M6 recall rescue + update UX",
                    "url": "https://github.com/quaid-labs/quaid/releases/tag/v0.2.16-alpha",
                },
            },
        )
        assert "UPDATE AVAILABLE" in msg
        assert "v0.2.15-alpha → v0.2.16-alpha" in msg
        assert "Note: M6 recall rescue + update UX" in msg
        assert "curl -fsSL" in msg
        assert "Release notes:" in msg
