"""Tests for llm_clients.py — JSON parsing, token usage, provider delegation."""

from concurrent.futures import ThreadPoolExecutor
import http.client
import os
import sys
import json
import threading
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real config/DB during import
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

import pytest

from core.llm.clients import (
    parse_json_response,
    validate_llm_output,
    ReviewDecision,
    reset_token_usage,
    get_token_usage,
    get_token_budget_usage,
    estimate_cost,
    call_fast_reasoning,
    call_deep_reasoning,
)
from lib.providers import LLMResult


# ---------------------------------------------------------------------------
# parse_json_response
# ---------------------------------------------------------------------------

class TestParseJsonResponse:
    """Tests for parse_json_response()."""

    def test_plain_json_dict(self):
        assert parse_json_response('{"key": "value"}') == {"key": "value"}

    def test_plain_json_array(self):
        assert parse_json_response('[1, 2, 3]') == [1, 2, 3]

    def test_json_fenced_with_backticks(self):
        text = '```json\n{"key": "value"}\n```'
        assert parse_json_response(text) == {"key": "value"}

    def test_json_fenced_without_json_label(self):
        text = '```\n{"key": "value"}\n```'
        assert parse_json_response(text) == {"key": "value"}

    def test_json_with_surrounding_text(self):
        text = 'Here is the result:\n{"key": "value"}\nThat was the output.'
        assert parse_json_response(text) == {"key": "value"}

    def test_array_with_surrounding_text(self):
        text = 'The keywords are: ["coffee", "espresso", "latte"] end.'
        assert parse_json_response(text) == ["coffee", "espresso", "latte"]

    def test_invalid_json_returns_none(self):
        assert parse_json_response("{not valid json}") is None

    def test_invalid_json_emits_parse_diagnostics(self, caplog):
        caplog.set_level("WARNING")
        assert parse_json_response("{not valid json}") is None
        assert "parse_json_response failed" in caplog.text

    def test_parse_diagnostics_do_not_leak_raw_content(self, caplog):
        caplog.set_level("WARNING")
        bad = '{"token":"my-super-secret-token",bad}'
        assert parse_json_response(bad) is None
        assert "parse_json_response failed" in caplog.text
        assert "my-super-secret-token" not in caplog.text

    def test_none_input_returns_none(self):
        assert parse_json_response(None) is None

    def test_empty_string_returns_none(self):
        assert parse_json_response("") is None

    def test_whitespace_only_returns_none(self):
        assert parse_json_response("   ") is None

    def test_nested_json(self):
        text = '{"outer": {"inner": [1, 2]}}'
        result = parse_json_response(text)
        assert result == {"outer": {"inner": [1, 2]}}

    def test_json_fenced_array(self):
        text = '```json\n["a", "b"]\n```'
        assert parse_json_response(text) == ["a", "b"]

    def test_multiple_fenced_blocks_first_wins(self):
        text = '```json\n{"first": true}\n```\nand\n```json\n{"second": true}\n```'
        result = parse_json_response(text)
        assert result is not None
        # Should parse at least one of them
        assert "first" in result or "second" in result

    def test_relaxed_parse_allows_multiline_string_content(self):
        text = '{"reasoning":"line one\\nline two","content":"alpha\\n beta"}'.replace("\\n", "\n")
        assert parse_json_response(text) == {
            "reasoning": "line one\nline two",
            "content": "alpha\n beta",
        }

    def test_relaxed_parse_allows_multiline_string_inside_fence(self):
        text = '```json\n{"reasoning":"line one\\nline two","ok":true}\n```'.replace("\\n", "\n")
        assert parse_json_response(text) == {
            "reasoning": "line one\nline two",
            "ok": True,
        }

    def test_fenced_json_with_trailing_junk_uses_balanced_object(self):
        text = '```json\n{"reasoning":"ok","content":"value"}\nextra trailing junk\n```'
        assert parse_json_response(text) == {
            "reasoning": "ok",
            "content": "value",
        }

    def test_repairs_invalid_backslash_escapes_inside_fenced_json_strings(self):
        text = (
            '```json\n'
            '{"decisions":[{"file":"SOUL.md","snippet_index":1,"action":"DISCARD",'
            '"reason":"Already over token cap for C:\\work\\cap"}]}\n'
            '```'
        )
        assert parse_json_response(text) == {
            "decisions": [
                {
                    "file": "SOUL.md",
                    "snippet_index": 1,
                    "action": "DISCARD",
                    "reason": "Already over token cap for C:\\work\\cap",
                }
            ]
        }

    def test_validate_llm_output_warns_on_unknown_keys(self, caplog):
        caplog.set_level("WARNING")
        parsed = [{"foo": "bar"}]
        results = validate_llm_output(parsed, ReviewDecision)
        assert results == []
        assert "dropping unknown keys" in caplog.text

    def test_validate_llm_output_does_not_log_raw_values(self, caplog):
        caplog.set_level("WARNING")
        parsed = [{"foo": "my-super-secret-token"}]
        results = validate_llm_output(parsed, ReviewDecision)
        assert results == []
        assert "my-super-secret-token" not in caplog.text

    def test_validate_llm_output_logs_non_dict_items_without_raw_values(self, caplog):
        caplog.set_level("WARNING")
        parsed = ["my-super-secret-token", {"file": "a.md", "snippet_index": 1, "action": "KEEP"}]

        results = validate_llm_output(parsed, ReviewDecision)

        assert len(results) == 1
        assert "Invalid LLM output item skipped: type=str" in caplog.text
        assert "my-super-secret-token" not in caplog.text


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------

class TestTokenUsage:
    """Tests for token usage and cost estimation."""

    def test_reset_token_usage_zeroes_counters(self):
        import core.llm.clients as llm_clients
        # Set some usage
        llm_clients._usage_input_tokens = 1000
        llm_clients._usage_output_tokens = 500
        llm_clients._usage_calls = 3
        reset_token_usage()
        usage = get_token_usage()
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["api_calls"] == 0

    def test_get_token_usage_returns_dict(self):
        reset_token_usage()
        usage = get_token_usage()
        assert isinstance(usage, dict)
        assert "input_tokens" in usage
        assert "output_tokens" in usage
        assert "api_calls" in usage

    def test_get_token_usage_accumulation(self):
        import core.llm.clients as llm_clients
        reset_token_usage()
        llm_clients._usage_input_tokens = 100
        llm_clients._usage_output_tokens = 50
        llm_clients._usage_calls = 2
        usage = get_token_usage()
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["api_calls"] == 2

    def test_get_token_usage_includes_model_and_tier_breakdown(self):
        import core.llm.clients as llm_clients
        reset_token_usage()
        llm_clients._models_loaded = True
        llm_clients._fast_reasoning_model = "claude-haiku-4-5"
        llm_clients._deep_reasoning_model = "claude-opus-4-6"
        llm_clients._usage_by_model = {
            "claude-haiku-4-5": {"input": 30, "output": 12},
            "claude-opus-4-6": {"input": 70, "output": 28},
        }

        usage = get_token_usage()

        assert usage["model_usage"]["claude-haiku-4-5"]["total_tokens"] == 42
        assert usage["model_usage"]["claude-opus-4-6"]["total_tokens"] == 98
        assert usage["tier_usage"]["fast"] == {
            "input_tokens": 30,
            "output_tokens": 12,
            "total_tokens": 42,
        }
        assert usage["tier_usage"]["deep"] == {
            "input_tokens": 70,
            "output_tokens": 28,
            "total_tokens": 98,
        }

    def test_estimate_cost_zero_usage(self):
        reset_token_usage()
        cost = estimate_cost()
        assert cost == 0.0

    def test_estimate_cost_with_usage(self):
        import core.llm.clients as llm_clients
        reset_token_usage()
        llm_clients._usage_input_tokens = 1_000_000
        llm_clients._usage_output_tokens = 1_000_000
        cost = estimate_cost()
        # Should be > 0 and reasonable
        assert cost > 0
        assert isinstance(cost, float)

    def test_token_budget_snapshot_returns_consistent_pair(self):
        import core.llm.clients as llm_clients
        llm_clients.set_token_budget(1234)
        try:
            llm_clients._token_budget_used = 456
            used, total = get_token_budget_usage()
            assert used == 456
            assert total == 1234
        finally:
            llm_clients.reset_token_budget()

    def test_load_pricing_warns_once_and_uses_defaults_when_failhard_disabled(self):
        import core.llm.clients as llm_clients
        old_loaded = llm_clients._pricing_loaded
        old_error_logged = llm_clients._pricing_error_logged
        llm_clients._pricing_loaded = False
        llm_clients._pricing_error_logged = False
        try:
            with patch("config.get_config", side_effect=RuntimeError("bad pricing config")), \
                 patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
                 patch("core.llm.clients.logger.warning") as log_warning:
                llm_clients._load_pricing()
                llm_clients._load_pricing()
            assert llm_clients._pricing_loaded is True
            assert llm_clients._pricing_error_logged is True
            assert log_warning.call_count == 1
        finally:
            llm_clients._pricing_loaded = old_loaded
            llm_clients._pricing_error_logged = old_error_logged

    def test_load_pricing_raises_when_failhard_enabled(self):
        import core.llm.clients as llm_clients
        old_loaded = llm_clients._pricing_loaded
        old_error_logged = llm_clients._pricing_error_logged
        llm_clients._pricing_loaded = False
        llm_clients._pricing_error_logged = False
        try:
            with patch("config.get_config", side_effect=RuntimeError("bad pricing config")), \
                 patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
                with pytest.raises(RuntimeError, match="pricing configuration"):
                    llm_clients._load_pricing()
        finally:
            llm_clients._pricing_loaded = old_loaded
            llm_clients._pricing_error_logged = old_error_logged

    def test_load_model_config_is_thread_safe_single_init(self):
        import core.llm.clients as llm_clients
        old_models_loaded = llm_clients._models_loaded
        old_fast = llm_clients._fast_reasoning_model
        old_deep = llm_clients._deep_reasoning_model
        llm_clients._models_loaded = False
        llm_clients._fast_reasoning_model = ""
        llm_clients._deep_reasoning_model = ""
        calls = {"count": 0}
        count_lock = threading.Lock()

        def _fake_get_config():
            with count_lock:
                calls["count"] += 1
            import time
            time.sleep(0.01)
            return MagicMock(models=MagicMock(
                fast_reasoning="mock-fast-model",
                deep_reasoning="mock-deep-model",
            ))

        try:
            with patch("config.get_config", side_effect=_fake_get_config):
                with ThreadPoolExecutor(max_workers=12) as executor:
                    list(executor.map(lambda _i: llm_clients._load_model_config(), range(24)))
            assert calls["count"] == 1
            assert llm_clients._models_loaded is True
            assert llm_clients._fast_reasoning_model == "mock-fast-model"
            assert llm_clients._deep_reasoning_model == "mock-deep-model"
        finally:
            llm_clients._models_loaded = old_models_loaded
            llm_clients._fast_reasoning_model = old_fast
            llm_clients._deep_reasoning_model = old_deep

    def test_load_model_config_warns_and_uses_provider_defaults_when_config_import_fails_failopen(self):
        import core.llm.clients as llm_clients
        old_models_loaded = llm_clients._models_loaded
        old_fast = llm_clients._fast_reasoning_model
        old_deep = llm_clients._deep_reasoning_model
        llm_clients._models_loaded = False
        llm_clients._fast_reasoning_model = ""
        llm_clients._deep_reasoning_model = ""
        try:
            with patch("config.get_config", side_effect=ImportError("config unavailable")), \
                 patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
                 patch("core.llm.clients.logger.warning") as log_warning:
                llm_clients._load_model_config()

            assert llm_clients._models_loaded is True
            assert llm_clients._fast_reasoning_model == ""
            assert llm_clients._deep_reasoning_model == ""
            assert log_warning.call_count == 1
        finally:
            llm_clients._models_loaded = old_models_loaded
            llm_clients._fast_reasoning_model = old_fast
            llm_clients._deep_reasoning_model = old_deep

    def test_load_model_config_raises_when_config_import_fails_failhard(self):
        import core.llm.clients as llm_clients
        old_models_loaded = llm_clients._models_loaded
        old_fast = llm_clients._fast_reasoning_model
        old_deep = llm_clients._deep_reasoning_model
        llm_clients._models_loaded = False
        llm_clients._fast_reasoning_model = ""
        llm_clients._deep_reasoning_model = ""
        try:
            with patch("config.get_config", side_effect=ImportError("config unavailable")), \
                 patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
                with pytest.raises(RuntimeError, match="model configuration unavailable"):
                    llm_clients._load_model_config()
            assert llm_clients._models_loaded is False
        finally:
            llm_clients._models_loaded = old_models_loaded
            llm_clients._fast_reasoning_model = old_fast
            llm_clients._deep_reasoning_model = old_deep


# ---------------------------------------------------------------------------
# call_fast_reasoning / call_deep_reasoning — provider delegation
# ---------------------------------------------------------------------------

class TestCallLowReasoning:
    """Tests for call_fast_reasoning() with provider delegation."""

    def test_returns_canned_response(self, test_adapter):
        """call_fast_reasoning should delegate to TestLLMProvider."""
        result, duration = call_fast_reasoning("test prompt")
        assert result is not None
        assert len(test_adapter.llm_calls) == 1
        assert test_adapter.llm_calls[0]["model_tier"] == "fast"

    def test_raises_on_provider_error(self, test_adapter):
        """call_fast_reasoning raises RuntimeError on provider/config failure."""
        with patch("core.llm.clients.call_llm", side_effect=RuntimeError("no provider")):
            with pytest.raises(RuntimeError, match="no provider"):
                call_fast_reasoning("test prompt")


class TestCallHighReasoning:
    """Tests for call_deep_reasoning() with provider delegation."""

    def test_returns_canned_response(self, test_adapter):
        """call_deep_reasoning should delegate to TestLLMProvider."""
        result, duration = call_deep_reasoning("test prompt")
        assert result is not None
        assert len(test_adapter.llm_calls) == 1
        assert test_adapter.llm_calls[0]["model_tier"] == "deep"

    def test_raises_on_provider_error(self, test_adapter):
        """call_deep_reasoning raises RuntimeError on provider/config failure."""
        with patch("core.llm.clients.call_llm", side_effect=RuntimeError("no provider")):
            with pytest.raises(RuntimeError, match="no provider"):
                call_deep_reasoning("test prompt")

    def test_passes_max_retries_to_call_llm(self, monkeypatch):
        """Daemon extraction can disable wrapper retries for bounded signal retry."""
        import core.llm.clients as llm_clients

        captured = {}
        monkeypatch.setattr(llm_clients, "_load_model_config", lambda: None)
        monkeypatch.setattr(llm_clients, "get_prompt", lambda _key: "json-only")

        def fake_call_llm(**kwargs):
            captured.update(kwargs)
            return "{}", 0.01

        monkeypatch.setattr(llm_clients, "call_llm", fake_call_llm)

        result, duration = call_deep_reasoning(
            "test prompt",
            timeout=12.0,
            max_retries=0,
            slot_timeout=45.0,
        )

        assert result == "{}"
        assert duration == 0.01
        assert captured["model_tier"] == "deep"
        assert captured["timeout"] == 12.0
        assert captured["max_retries"] == 0
        assert captured["slot_timeout"] == 45.0


# ---------------------------------------------------------------------------
# call_llm — provider delegation and token tracking
# ---------------------------------------------------------------------------

class TestCallLlmProvider:
    """Tests for call_llm() delegating to adapter's LLM provider."""

    def test_delegates_to_provider(self, test_adapter):
        """call_llm should route through the adapter's LLM provider."""
        import core.llm.clients as llm_clients
        reset_token_usage()
        result, duration = llm_clients.call_llm("system", "user", max_tokens=100)
        assert result is not None
        assert len(test_adapter.llm_calls) == 1

    def test_tracks_token_usage(self, test_adapter):
        """call_llm should accumulate token usage from LLMResult."""
        import core.llm.clients as llm_clients
        reset_token_usage()
        llm_clients.call_llm("system", "user", max_tokens=100)
        usage = get_token_usage()
        # TestLLMProvider returns input_tokens=100, output_tokens=50
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["api_calls"] == 1

    def test_emits_usage_event_when_log_path_is_configured(self, test_adapter, tmp_path, monkeypatch):
        """call_llm should append benchmark-scoped usage events when requested."""
        import core.llm.clients as llm_clients
        usage_log = tmp_path / "logs" / "llm-usage.jsonl"
        monkeypatch.setenv("QUAID_LLM_USAGE_LOG_PATH", str(usage_log))
        monkeypatch.setenv("QUAID_LLM_USAGE_PHASE", "ingest")
        monkeypatch.setenv("QUAID_LLM_USAGE_SOURCE", "benchmark")
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

        reset_token_usage()
        llm_clients.call_llm("system", "user", max_tokens=100)

        rows = usage_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(rows) == 1
        payload = json.loads(rows[0])
        assert payload["ts"] == "2026-03-11T05:06:07+00:00"
        assert payload["phase"] == "ingest"
        assert payload["source"] == "benchmark"
        assert payload["provider"]
        assert payload["tier"] in {"fast", "deep"}
        assert payload["input_tokens"] == 100
        assert payload["output_tokens"] == 50
        assert payload["total_tokens"] == 150
        assert payload["api_calls"] == 1
        assert payload["duration_ms"] >= 0
        assert isinstance(payload["model_usage"], dict)

    def test_usage_event_logs_malformed_quaid_now_when_failhard_disabled(
        self, tmp_path, monkeypatch, caplog
    ):
        """Malformed benchmark clocks should not turn usage logging into provider failure."""
        import core.llm.clients as llm_clients
        usage_log = tmp_path / "logs" / "llm-usage.jsonl"
        monkeypatch.setenv("QUAID_LLM_USAGE_LOG_PATH", str(usage_log))
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        result = LLMResult("ok", 0.01, input_tokens=1, output_tokens=2, model="mock-model")

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), caplog.at_level("WARNING"):
            llm_clients._append_usage_event(
                result,
                tier="deep",
                provider_name="TestProvider",
                requested_model="mock-model",
            )

        assert usage_log.is_file()
        assert "Invalid QUAID_NOW='not-a-date'; using wall clock for LLM timestamp" in caplog.text

    def test_usage_event_raises_malformed_quaid_now_when_failhard_enabled(self, tmp_path, monkeypatch):
        """Malformed benchmark clocks still surface under failHard."""
        import core.llm.clients as llm_clients
        usage_log = tmp_path / "logs" / "llm-usage.jsonl"
        monkeypatch.setenv("QUAID_LLM_USAGE_LOG_PATH", str(usage_log))
        monkeypatch.setenv("QUAID_NOW", "not-a-date")
        result = LLMResult("ok", 0.01, input_tokens=1, output_tokens=2, model="mock-model")

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
                llm_clients._append_usage_event(
                    result,
                    tier="deep",
                    provider_name="TestProvider",
                    requested_model="mock-model",
                )

        assert not usage_log.exists()

    def test_usage_event_logs_write_failure(self, tmp_path, monkeypatch, caplog):
        """Usage append failures should be visible without changing call behavior."""
        import core.llm.clients as llm_clients
        usage_log = tmp_path / "logs" / "llm-usage.jsonl"
        monkeypatch.setenv("QUAID_LLM_USAGE_LOG_PATH", str(usage_log))
        result = LLMResult("ok", 0.01, input_tokens=1, output_tokens=2, model="mock-model")

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
             patch.object(Path, "open", side_effect=OSError("disk full")), \
             caplog.at_level("WARNING"):
            llm_clients._append_usage_event(
                result,
                tier="deep",
                provider_name="TestProvider",
                requested_model="mock-model",
            )

        assert "LLM usage event append failed" in caplog.text
        assert "disk full" in caplog.text

    def test_trace_event_honors_quaid_now(self, tmp_path, monkeypatch):
        """LLM trace rows should be deterministic under benchmark clock overrides."""
        import core.llm.clients as llm_clients
        workspace = tmp_path / "runs" / "quaid-trace"
        monkeypatch.setenv("BENCHMARK_LLM_TRACE", "1")
        monkeypatch.setenv("QUAID_WORKSPACE", str(workspace))
        monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

        llm_clients._append_trace({"status": "ok", "provider": "test"})

        trace_log = workspace / "logs" / "llm-call-trace.jsonl"
        payload = json.loads(trace_log.read_text(encoding="utf-8").strip())
        assert payload["ts"] == "2026-03-11T05:06:07+00:00"
        assert payload["status"] == "ok"

    def test_trace_event_logs_malformed_quaid_now_when_failhard_disabled(self, tmp_path, monkeypatch, caplog):
        """Malformed benchmark clocks should not turn tracing into provider failure."""
        import core.llm.clients as llm_clients
        workspace = tmp_path / "runs" / "quaid-trace"
        monkeypatch.setenv("BENCHMARK_LLM_TRACE", "1")
        monkeypatch.setenv("QUAID_WORKSPACE", str(workspace))
        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), caplog.at_level("WARNING"):
            llm_clients._append_trace({"status": "ok"})

        trace_log = workspace / "logs" / "llm-call-trace.jsonl"
        assert trace_log.is_file()
        assert "Invalid QUAID_NOW='not-a-date'; using wall clock for LLM timestamp" in caplog.text

    def test_trace_event_raises_malformed_quaid_now_when_failhard_enabled(self, tmp_path, monkeypatch):
        """Malformed benchmark clocks still surface under failHard."""
        import core.llm.clients as llm_clients
        workspace = tmp_path / "runs" / "quaid-trace"
        monkeypatch.setenv("BENCHMARK_LLM_TRACE", "1")
        monkeypatch.setenv("QUAID_WORKSPACE", str(workspace))
        monkeypatch.setenv("QUAID_NOW", "not-a-date")

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="Invalid QUAID_NOW"):
                llm_clients._append_trace({"status": "ok"})

        assert not (workspace / "logs" / "llm-call-trace.jsonl").exists()

    def test_trace_event_logs_write_failure(self, tmp_path, monkeypatch, caplog):
        """Trace append failures should be visible without changing call behavior."""
        import core.llm.clients as llm_clients
        workspace = tmp_path / "runs" / "quaid-trace"
        monkeypatch.setenv("BENCHMARK_LLM_TRACE", "1")
        monkeypatch.setenv("QUAID_WORKSPACE", str(workspace))

        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
             patch.object(Path, "open", side_effect=OSError("trace locked")), \
             caplog.at_level("WARNING"):
            llm_clients._append_trace({"status": "ok"})

        assert "LLM trace append failed" in caplog.text
        assert "trace locked" in caplog.text

    def test_m15_trace_failure_is_diagnostic_only(self, caplog):
        """Optional M15 tracing failures should be logged but not raised."""
        import core.llm.clients as llm_clients

        with patch("lib.m15_trace.trace_m15", side_effect=RuntimeError("trace unavailable")), \
             caplog.at_level("DEBUG"):
            llm_clients._trace_m15("llm.test")

        assert "M15 trace write failed" in caplog.text
        assert "trace unavailable" in caplog.text

    def test_llm_m15_previews_are_bounded(self, test_adapter):
        """M15 trace previews should not carry long raw prompts or responses."""
        import core.llm.clients as llm_clients

        events = []

        def _trace(event, **fields):
            events.append((event, fields))

        user_message = "secret-" + ("x" * 80)
        system_prompt = "system-" + ("y" * 80)

        with patch("lib.m15_trace.trace_m15", side_effect=_trace):
            llm_clients.call_llm(system_prompt, user_message, max_tokens=100, max_retries=0)

        entry = next(fields for event, fields in events if event == "llm.call.entry")
        call_ok = next(fields for event, fields in events if event == "llm.provider.call_ok")
        assert entry["prompt_preview"] == "secret-" + ("x" * 23)
        assert entry["system_preview"] == "system-" + ("y" * 23)
        assert len(call_ok["response_preview"]) <= 30

    def test_fast_reasoning_m15_prompt_preview_is_bounded(self, monkeypatch):
        import core.llm.clients as llm_clients

        events = []
        monkeypatch.setattr(llm_clients, "_load_model_config", lambda: None)
        monkeypatch.setattr(llm_clients, "get_prompt", lambda _key: "json-only")
        monkeypatch.setattr(llm_clients, "call_llm", lambda **_kwargs: ("{}", 0.01))

        with patch("lib.m15_trace.trace_m15", side_effect=lambda event, **fields: events.append((event, fields))):
            llm_clients.call_fast_reasoning("prompt-" + ("z" * 80), max_tokens=100)

        entry = next(fields for event, fields in events if event == "llm.call_fast_reasoning.entry")
        assert entry["prompt_preview"] == "prompt-" + ("z" * 23)

    def test_disabled_llm_returns_none_when_failhard_disabled(self, test_adapter, monkeypatch, caplog):
        """QUAID_DISABLE_LLM may degrade only when failHard is disabled."""
        import core.llm.clients as llm_clients

        monkeypatch.setenv("QUAID_DISABLE_LLM", "1")
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), caplog.at_level("WARNING"):
            result, duration = llm_clients.call_llm("system", "user")

        assert result is None
        assert duration == 0.0
        assert "QUAID_DISABLE_LLM is set" in caplog.text
        assert test_adapter.llm_calls == []

    def test_disabled_llm_raises_when_failhard_enabled(self, test_adapter, monkeypatch):
        """QUAID_DISABLE_LLM must not silently suppress LLM calls under failHard."""
        import core.llm.clients as llm_clients

        monkeypatch.setenv("QUAID_DISABLE_LLM", "1")
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="QUAID_DISABLE_LLM"):
                llm_clients.call_llm("system", "user")

        assert test_adapter.llm_calls == []

    def test_max_output_config_failure_warns_and_uses_api_fallback_when_fail_open(self, test_adapter, caplog):
        import core.llm.clients as llm_clients

        llm_clients._models_loaded = True
        llm_clients._deep_reasoning_model = "claude-opus-4-6"

        def _max_output(_tier):
            raise RuntimeError("max output config broken")

        from config import get_config
        cfg = get_config()
        with patch.object(cfg.models, "max_output", side_effect=_max_output), \
             patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
             caplog.at_level("WARNING", logger="lib.llm_clients"):
            result, duration = llm_clients.call_llm("system", "user", max_tokens=20000, max_retries=0)

        assert result is not None
        assert duration > 0
        assert test_adapter.llm_calls[0]["max_tokens"] == 16384
        assert "failed to resolve max output tokens for tier deep" in caplog.text

    def test_max_output_config_failure_raises_when_failhard_enabled(self, test_adapter, caplog):
        import core.llm.clients as llm_clients

        llm_clients._models_loaded = True
        llm_clients._deep_reasoning_model = "claude-opus-4-6"

        def _max_output(_tier):
            raise RuntimeError("max output config broken")

        from config import get_config
        cfg = get_config()
        with patch.object(cfg.models, "max_output", side_effect=_max_output), \
             patch("core.llm.clients.is_fail_hard_enabled", return_value=True), \
             caplog.at_level("WARNING", logger="lib.llm_clients"):
            with pytest.raises(RuntimeError, match="max output token config") as excinfo:
                llm_clients.call_llm("system", "user", max_tokens=20000, max_retries=0)

        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "max output config broken" in str(excinfo.value.__cause__)
        assert test_adapter.llm_calls == []
        assert "failed to resolve max output tokens for tier deep" in caplog.text

    def test_rate_limit_header_parse_failure_logs_debug(self, caplog):
        import core.llm.clients as llm_clients

        class _BadHeaders:
            def items(self):
                raise RuntimeError("headers broken")

        exc = urllib.error.HTTPError("https://example.test", 429, "rate limited", _BadHeaders(), None)

        with caplog.at_level("DEBUG", logger="lib.llm_clients"):
            assert llm_clients._rate_limit_headers(exc) == {}

        assert "Failed parsing rate-limit headers: headers broken" in caplog.text

    def test_retry_delay_logs_bad_retry_after_header(self, caplog):
        import core.llm.clients as llm_clients

        exc = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {"Retry-After": "bogus"},
            None,
        )

        with caplog.at_level("DEBUG", logger="lib.llm_clients"):
            assert llm_clients._retry_delay_for_error(exc, 2.5) == 2.5

        assert "Failed parsing Retry-After header 'bogus'" in caplog.text

    def test_retry_delay_reset_header_uses_quaid_now(self, monkeypatch):
        import core.llm.clients as llm_clients

        monkeypatch.setenv("QUAID_NOW", "2030-01-01T00:00:00Z")
        exc = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            {"anthropic-ratelimit-requests-reset": "2030-01-01T00:00:10Z"},
            None,
        )

        assert llm_clients._retry_delay_for_error(exc, 1.0) == 10.0

    def test_cost_cap_abort(self, test_adapter, monkeypatch):
        """call_llm should abort when cost cap is exceeded."""
        import core.llm.clients as llm_clients
        monkeypatch.setenv("JANITOR_COST_CAP", "0.001")
        # Simulate high usage
        llm_clients._usage_by_model = {"claude-opus-4-6": {"input": 1_000_000, "output": 1_000_000}}
        llm_clients._usage_input_tokens = 1_000_000
        llm_clients._usage_output_tokens = 1_000_000
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            result, duration = llm_clients.call_llm("system", "user")
        assert result is None
        assert duration == 0.0
        # Clean up
        reset_token_usage()

    def test_cost_cap_raises_when_failhard_enabled(self, test_adapter, monkeypatch):
        """Cost cap violations should raise when failHard is enabled."""
        import core.llm.clients as llm_clients
        monkeypatch.setenv("JANITOR_COST_CAP", "0.001")
        llm_clients._usage_by_model = {"claude-opus-4-6": {"input": 1_000_000, "output": 1_000_000}}
        llm_clients._usage_input_tokens = 1_000_000
        llm_clients._usage_output_tokens = 1_000_000
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="cost cap exceeded"):
                llm_clients.call_llm("system", "user")
        reset_token_usage()

    def test_cost_cap_counts_inflight_reservations(self, monkeypatch):
        """Concurrent calls should not all pass the same stale cost snapshot."""
        import core.llm.clients as llm_clients

        monkeypatch.setenv("JANITOR_COST_CAP", "1.5")
        entered = threading.Event()
        release = threading.Event()
        calls = []
        call_lock = threading.Lock()

        class BlockingProvider:
            def llm_call(self, _messages, _model_tier="deep", _max_tokens=4000, _timeout=600):
                with call_lock:
                    calls.append(object())
                    call_number = len(calls)
                if call_number == 1:
                    entered.set()
                    assert release.wait(timeout=2.0)
                return LLMResult(
                    text='{"ok": true}',
                    duration=0.01,
                    input_tokens=10,
                    output_tokens=10,
                    model="test-deep",
                )

        reset_token_usage()
        old_models_loaded = llm_clients._models_loaded
        old_fast_model = llm_clients._fast_reasoning_model
        old_deep_model = llm_clients._deep_reasoning_model
        llm_clients._models_loaded = True
        llm_clients._deep_reasoning_model = "claude-opus-4-6"
        provider = BlockingProvider()

        try:
            with patch("core.llm.clients.get_llm_provider", return_value=provider), \
                 patch("core.llm.clients._estimate_llm_call_cost", return_value=1.0), \
                 patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(llm_clients.call_llm, "system", "user", max_retries=0)
                    assert entered.wait(timeout=2.0)

                    result, duration = llm_clients.call_llm("system", "user", max_retries=0)
                    assert result is None
                    assert duration == 0.0
                    assert len(calls) == 1

                    release.set()
                    assert first.result(timeout=2.0)[0] == '{"ok": true}'

            assert llm_clients._usage_reserved_cost == 0.0
        finally:
            release.set()
            reset_token_usage()
            llm_clients._models_loaded = old_models_loaded
            llm_clients._fast_reasoning_model = old_fast_model
            llm_clients._deep_reasoning_model = old_deep_model

    def test_model_tier_routing(self, test_adapter):
        """Haiku model should route as 'low' tier, others as 'high'."""
        import core.llm.clients as llm_clients
        # Explicitly set the low model name so test doesn't depend on config file
        llm_clients._models_loaded = True
        llm_clients._fast_reasoning_model = "claude-haiku-4-5"
        llm_clients._deep_reasoning_model = "claude-opus-4-6"

        llm_clients.call_llm("system", "user", model="claude-haiku-4-5")
        assert test_adapter.llm_calls[0]["model_tier"] == "fast"

        test_adapter.llm_calls.clear()
        llm_clients.call_llm("system", "user", model="claude-opus-4-6")
        assert test_adapter.llm_calls[0]["model_tier"] == "deep"

    def test_provider_resolution_receives_model_tier(self, test_adapter):
        """Provider lookup should receive resolved model tier for tier-specific routing."""
        import core.llm.clients as llm_clients

        llm_clients._models_loaded = True
        llm_clients._fast_reasoning_model = "claude-haiku-4-5"
        llm_clients._deep_reasoning_model = "claude-opus-4-6"

        with patch("core.llm.clients.get_llm_provider", wraps=llm_clients.get_llm_provider) as mock_get:
            llm_clients.call_llm("system", "user", model="claude-haiku-4-5")
            assert mock_get.call_args.kwargs.get("model_tier") == "fast"

    def test_retries_on_provider_error(self, test_adapter):
        """call_llm should retry on transient provider errors."""
        import core.llm.clients as llm_clients
        from lib.providers import TestLLMProvider

        call_count = [0]
        original_llm_call = test_adapter._llm.llm_call

        def flaky_llm_call(messages, model_tier="deep", max_tokens=4000, timeout=120):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise ConnectionError("transient failure")
            return original_llm_call(messages, model_tier, max_tokens, timeout)

        test_adapter._llm.llm_call = flaky_llm_call
        result, duration = llm_clients.call_llm("system", "user", max_retries=3)
        assert result is not None
        assert call_count[0] == 3  # 2 failures + 1 success

    def test_retries_on_incomplete_read_transport_error(self, test_adapter):
        """Incomplete HTTP body reads should be retried as transient transport errors."""
        import core.llm.clients as llm_clients

        call_count = [0]
        original_llm_call = test_adapter._llm.llm_call

        def flaky_llm_call(messages, model_tier="deep", max_tokens=4000, timeout=120):
            call_count[0] += 1
            if call_count[0] == 1:
                raise http.client.IncompleteRead(b'{"partial": true}', 42)
            return original_llm_call(messages, model_tier, max_tokens, timeout)

        test_adapter._llm.llm_call = flaky_llm_call
        result, _duration = llm_clients.call_llm("system", "user", max_retries=2)
        assert result is not None
        assert call_count[0] == 2

    def test_raises_on_persistent_error_when_failhard_enabled(self, test_adapter):
        """Persistent provider failures should raise when failHard is enabled."""
        import core.llm.clients as llm_clients

        def always_fail(*_args, **_kwargs):
            raise ConnectionError("persistent failure")

        test_adapter._llm.llm_call = always_fail
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                llm_clients.call_llm("system", "user", max_retries=0)

    def test_returns_none_on_persistent_error_when_failhard_disabled(self, test_adapter):
        """Persistent provider outages raise ProviderUnavailableError when failHard is disabled.

        Callers (e.g. daemon) catch ProviderUnavailableError to implement retry/fallback logic.
        """
        import core.llm.clients as llm_clients
        from lib.llm_clients import ProviderUnavailableError

        def always_fail(*_args, **_kwargs):
            raise ConnectionError("persistent failure")

        test_adapter._llm.llm_call = always_fail
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            with pytest.raises(ProviderUnavailableError):
                llm_clients.call_llm("system", "user", max_retries=0)

    def test_notifies_agent_on_persistent_provider_outage(self, test_adapter):
        import core.llm.clients as llm_clients
        from lib.llm_clients import ProviderUnavailableError

        def always_fail(*_args, **_kwargs):
            raise ConnectionError("persistent failure")

        test_adapter._llm.llm_call = always_fail
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False), \
             patch("lib.llm_clients.notify_agent") as mock_notify:
            with pytest.raises(ProviderUnavailableError):
                llm_clients.call_llm("system", "user", max_retries=0)

        mock_notify.assert_called_once()
        assert "could not reach its" in mock_notify.call_args.args[0]

    def test_no_response_raises_when_failhard_enabled(self, test_adapter):
        """Provider null responses must fail hard when failHard is enabled."""
        import core.llm.clients as llm_clients

        def no_response(*_args, **_kwargs):
            return LLMResult(text=None, duration=0.01, model="null-model")

        test_adapter._llm.llm_call = no_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                llm_clients.call_llm("system", "user", max_retries=0)

    def test_no_response_error_includes_provider_context(self, test_adapter):
        import core.llm.clients as llm_clients

        def no_response(*_args, **_kwargs):
            return LLMResult(text=None, duration=0.01, model="null-model")

        test_adapter._llm.llm_call = no_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError) as exc:
                llm_clients.call_llm("system", "user", max_retries=0, timeout=12.0)
        msg = str(exc.value)
        assert "provider=" in msg
        assert "error_type=" in msg
        assert "null-model" in msg

    def test_no_response_degrades_when_failhard_disabled(self, test_adapter):
        """Provider null responses should degrade only when failHard is disabled."""
        import core.llm.clients as llm_clients

        def no_response(*_args, **_kwargs):
            return LLMResult(text=None, duration=0.01, model="null-model")

        test_adapter._llm.llm_call = no_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            result, _duration = llm_clients.call_llm("system", "user", max_retries=0)
        assert result is None

    def test_truncated_response_raises_when_failhard_enabled(self, test_adapter):
        """Provider-side max_tokens truncation must not be treated as valid output."""
        import core.llm.clients as llm_clients

        def truncated_response(*_args, **_kwargs):
            return LLMResult(
                text='{"queries":["partial"',
                duration=0.01,
                model="haiku-test",
                truncated=True,
            )

        test_adapter._llm.llm_call = truncated_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="truncated.*failHard is enabled"):
                llm_clients.call_llm("system", "user", max_retries=0, model_tier="fast")

    def test_truncated_response_can_degrade_when_failhard_disabled(self, test_adapter):
        """Non-failHard callers keep the old degraded path, but still get a warning."""
        import core.llm.clients as llm_clients

        def truncated_response(*_args, **_kwargs):
            return LLMResult(
                text="partial text",
                duration=0.01,
                model="haiku-test",
                truncated=True,
            )

        test_adapter._llm.llm_call = truncated_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            result, _duration = llm_clients.call_llm("system", "user", max_retries=0, model_tier="fast")
        assert result == "partial text"

    def test_config_error_raises_when_failhard_disabled(self, test_adapter):
        import core.llm.clients as llm_clients
        from lib.llm_clients import ProviderConfigError

        def config_error(*_args, **_kwargs):
            raise RuntimeError(
                "Quaid fast LLM call failed: HTTP 400 from gateway "
                "(model=openai/invalid-model-xyzzy). Check fastReasoning/deepReasoning in config.json."
            )

        test_adapter._llm.llm_call = config_error
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            with pytest.raises(ProviderConfigError, match="could not access its fast language model provider"):
                llm_clients.call_llm("system", "user", max_retries=0, model_tier="fast")

    def test_successful_call_logs_pending_notice_cleanup_failure(self, test_adapter, caplog):
        """Stale provider notice cleanup failures should be visible."""
        import core.llm.clients as llm_clients

        with patch(
            "lib.agent_notice.clear_pending_notices_by_source",
            side_effect=RuntimeError("notice store locked"),
        ), caplog.at_level("WARNING"):
            result, duration = llm_clients.call_llm("system", "user", max_retries=0)

        assert result is not None
        assert duration > 0
        assert "Failed clearing provider pending notices" in caplog.text
        assert "notice store locked" in caplog.text

    def test_config_error_notifies_agent_before_failhard_raise(self, test_adapter):
        import core.llm.clients as llm_clients
        from lib.llm_clients import ProviderConfigError

        def config_error(*_args, **_kwargs):
            raise RuntimeError(
                "Quaid fast LLM call failed: HTTP 400 from gateway "
                "(model=openai/invalid-model-xyzzy). Check fastReasoning/deepReasoning in config.json."
            )

        test_adapter._llm.llm_call = config_error
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True), \
             patch("lib.llm_clients.notify_agent") as mock_notify:
            with pytest.raises(ProviderConfigError, match="failHard is enabled"):
                llm_clients.call_llm("system", "user", max_retries=0, model_tier="fast")

        mock_notify.assert_called_once()
        assert "invalid-model-xyzzy" in mock_notify.call_args.args[0]
        assert mock_notify.call_args.kwargs["severity"] == "error"
        assert mock_notify.call_args.kwargs["source"] == "provider"

    def test_uses_remaining_deadline_for_slot_and_provider_timeout(self):
        """Per-attempt timeout should use remaining deadline, not full timeout each retry."""
        import core.llm.clients as llm_clients

        captured_slot_timeouts = []
        captured_call_timeouts = []

        @contextmanager
        def _slot(timeout_seconds=None, pool_kind=None):
            captured_slot_timeouts.append(timeout_seconds)
            yield

        provider = MagicMock()

        def _llm_call(_messages, _tier, _max_tokens, timeout):
            captured_call_timeouts.append(timeout)
            return LLMResult(text='{"ok":true}', duration=0.01, model="test")

        provider.llm_call.side_effect = _llm_call

        with patch("core.llm.clients.get_llm_provider", return_value=provider), \
             patch("core.llm.clients.acquire_llm_slot", side_effect=_slot), \
             patch("core.llm.clients.time.time", side_effect=[100.0, 100.2, 100.2, 100.3]):
            llm_clients.call_llm("system", "user", timeout=1.0, max_retries=0)

        assert captured_slot_timeouts[0] == pytest.approx(0.8, rel=1e-3)
        assert captured_call_timeouts[0] == pytest.approx(0.8, rel=1e-3)

    def test_slot_wait_time_is_deducted_from_provider_timeout(self):
        """Provider timeout should be recomputed after slot acquisition wait."""
        import core.llm.clients as llm_clients

        captured_slot_timeouts = []
        captured_call_timeouts = []

        @contextmanager
        def _slot(timeout_seconds=None, pool_kind=None):
            captured_slot_timeouts.append(timeout_seconds)
            yield

        provider = MagicMock()

        def _llm_call(_messages, _tier, _max_tokens, timeout):
            captured_call_timeouts.append(timeout)
            return LLMResult(text='{"ok":true}', duration=0.01, model="test")

        provider.llm_call.side_effect = _llm_call

        with patch("core.llm.clients.get_llm_provider", return_value=provider), \
             patch("core.llm.clients.acquire_llm_slot", side_effect=_slot), \
             patch("core.llm.clients.time.time", side_effect=[100.0, 100.2, 100.5, 100.6]):
            llm_clients.call_llm("system", "user", timeout=1.0, max_retries=0)

        assert captured_slot_timeouts[0] == pytest.approx(0.8, rel=1e-3)
        assert captured_call_timeouts[0] == pytest.approx(0.5, rel=1e-3)

    def test_explicit_slot_timeout_preserves_provider_timeout(self):
        """Daemon callers can queue for a slot without spending provider call budget."""
        import core.llm.clients as llm_clients

        captured_slot_timeouts = []
        captured_call_timeouts = []

        @contextmanager
        def _slot(timeout_seconds=None, pool_kind=None):
            captured_slot_timeouts.append(timeout_seconds)
            yield

        provider = MagicMock()

        def _llm_call(_messages, _tier, _max_tokens, timeout):
            captured_call_timeouts.append(timeout)
            return LLMResult(text='{"ok":true}', duration=0.01, model="test")

        provider.llm_call.side_effect = _llm_call

        with patch("core.llm.clients.get_llm_provider", return_value=provider), \
             patch("core.llm.clients.acquire_llm_slot", side_effect=_slot), \
             patch("core.llm.clients.time.time", side_effect=[100.0, 100.2, 100.5]):
            llm_clients.call_llm(
                "system",
                "user",
                timeout=1.0,
                slot_timeout=30.0,
                max_retries=0,
            )

        assert captured_slot_timeouts[0] == pytest.approx(30.0)
        assert captured_call_timeouts[0] == pytest.approx(1.0)

    def test_explicit_slot_timeout_must_be_positive(self, test_adapter):
        import core.llm.clients as llm_clients

        with pytest.raises(ValueError, match="slot_timeout must be positive"):
            llm_clients.call_llm("system", "user", slot_timeout=0, max_retries=0)
