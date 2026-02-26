"""Tests for llm_clients.py — JSON parsing, token usage, provider delegation."""

import os
import sys
import json
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

    def test_invalid_json_emits_parse_diagnostics(self, capsys):
        assert parse_json_response("{not valid json}") is None
        captured = capsys.readouterr()
        assert "parse_json_response failed" in captured.err

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

    def test_validate_llm_output_warns_on_unknown_keys(self, capsys):
        parsed = [{"foo": "bar"}]
        results = validate_llm_output(parsed, ReviewDecision)
        captured = capsys.readouterr()
        assert results == []
        assert "dropping unknown keys" in captured.err

    def test_validate_llm_output_does_not_log_raw_values(self, capsys):
        parsed = [{"foo": "my-super-secret-token"}]
        results = validate_llm_output(parsed, ReviewDecision)
        captured = capsys.readouterr()
        assert results == []
        assert "my-super-secret-token" not in captured.err


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
        """Persistent provider failures should degrade to None when failHard is disabled."""
        import core.llm.clients as llm_clients

        def always_fail(*_args, **_kwargs):
            raise ConnectionError("persistent failure")

        test_adapter._llm.llm_call = always_fail
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            result, _duration = llm_clients.call_llm("system", "user", max_retries=0)
        assert result is None

    def test_no_response_raises_when_failhard_enabled(self, test_adapter):
        """Provider null responses must fail hard when failHard is enabled."""
        import core.llm.clients as llm_clients

        def no_response(*_args, **_kwargs):
            return LLMResult(text=None, duration=0.01, model="null-model")

        test_adapter._llm.llm_call = no_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                llm_clients.call_llm("system", "user", max_retries=0)

    def test_no_response_degrades_when_failhard_disabled(self, test_adapter):
        """Provider null responses should degrade only when failHard is disabled."""
        import core.llm.clients as llm_clients

        def no_response(*_args, **_kwargs):
            return LLMResult(text=None, duration=0.01, model="null-model")

        test_adapter._llm.llm_call = no_response
        with patch("core.llm.clients.is_fail_hard_enabled", return_value=False):
            result, _duration = llm_clients.call_llm("system", "user", max_retries=0)
        assert result is None

    def test_uses_remaining_deadline_for_slot_and_provider_timeout(self):
        """Per-attempt timeout should use remaining deadline, not full timeout each retry."""
        import core.llm.clients as llm_clients

        captured_slot_timeouts = []
        captured_call_timeouts = []

        @contextmanager
        def _slot(timeout_seconds=None):
            captured_slot_timeouts.append(timeout_seconds)
            yield

        provider = MagicMock()

        def _llm_call(_messages, _tier, _max_tokens, timeout):
            captured_call_timeouts.append(timeout)
            return LLMResult(text='{"ok":true}', duration=0.01, model="test")

        provider.llm_call.side_effect = _llm_call

        with patch("core.llm.clients.get_llm_provider", return_value=provider), \
             patch("core.llm.clients.acquire_llm_slot", side_effect=_slot), \
             patch("core.llm.clients.time.time", side_effect=[100.0, 100.2]):
            llm_clients.call_llm("system", "user", timeout=1.0, max_retries=0)

        assert captured_slot_timeouts[0] == pytest.approx(0.8, rel=1e-3)
        assert captured_call_timeouts[0] == pytest.approx(0.8, rel=1e-3)
