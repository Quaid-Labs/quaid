"""Tests for llm_clients.py — JSON parsing, token usage, provider delegation."""

import os
import sys
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real config/DB during import
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

import pytest

from core.llm.clients import (
    parse_json_response,
    reset_token_usage,
    get_token_usage,
    estimate_cost,
    call_fast_reasoning,
    call_deep_reasoning,
)


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
        result, duration = llm_clients.call_llm("system", "user")
        assert result is None
        assert duration == 0.0
        # Clean up
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
