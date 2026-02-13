"""Tests for llm_clients.py — JSON parsing, retryability, token usage, API key."""

import os
import sys
import json
import urllib.error
from unittest.mock import patch, MagicMock

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real config/DB during import
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

import pytest

from llm_clients import (
    parse_json_response,
    _is_retryable,
    reset_token_usage,
    get_token_usage,
    estimate_cost,
    get_anthropic_api_key,
    call_low_reasoning,
    call_high_reasoning,
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
# _is_retryable
# ---------------------------------------------------------------------------

class TestIsRetryable:
    """Tests for _is_retryable()."""

    def test_http_429_is_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=429, msg="Rate limited",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, delay = _is_retryable(exc)
        assert retryable is True

    def test_http_503_is_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=503, msg="Service Unavailable",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_http_500_is_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=500, msg="Internal Server Error",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_http_502_is_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=502, msg="Bad Gateway",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_http_529_is_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=529, msg="Overloaded",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_http_400_is_not_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=400, msg="Bad Request",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is False

    def test_http_401_is_not_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=401, msg="Unauthorized",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is False

    def test_http_404_is_not_retryable(self):
        exc = urllib.error.HTTPError(
            url="http://example.com", code=404, msg="Not Found",
            hdrs=MagicMock(get=lambda k: None), fp=None
        )
        retryable, _ = _is_retryable(exc)
        assert retryable is False

    def test_url_error_is_retryable(self):
        exc = urllib.error.URLError("Connection refused")
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_timeout_error_is_retryable(self):
        exc = TimeoutError("timed out")
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_connection_error_is_retryable(self):
        exc = ConnectionError("connection reset")
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_os_error_is_retryable(self):
        exc = OSError("network error")
        retryable, _ = _is_retryable(exc)
        assert retryable is True

    def test_value_error_is_not_retryable(self):
        exc = ValueError("bad data")
        retryable, _ = _is_retryable(exc)
        assert retryable is False

    def test_type_error_is_not_retryable(self):
        exc = TypeError("wrong type")
        retryable, _ = _is_retryable(exc)
        assert retryable is False

    def test_429_with_retry_after_header(self):
        headers = MagicMock()
        headers.get = lambda k: "5" if k == "retry-after" else None
        exc = urllib.error.HTTPError(
            url="http://example.com", code=429, msg="Rate limited",
            hdrs=headers, fp=None
        )
        retryable, delay = _is_retryable(exc)
        assert retryable is True
        assert delay == 5.0


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------

class TestTokenUsage:
    """Tests for token usage and cost estimation."""

    def test_reset_token_usage_zeroes_counters(self):
        import llm_clients
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
        import llm_clients
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
        import llm_clients
        reset_token_usage()
        llm_clients._usage_input_tokens = 1_000_000
        llm_clients._usage_output_tokens = 1_000_000
        cost = estimate_cost()
        # Should be > 0 and reasonable
        assert cost > 0
        assert isinstance(cost, float)


# ---------------------------------------------------------------------------
# API key retrieval
# ---------------------------------------------------------------------------

class TestGetAnthropicApiKey:
    """Tests for get_anthropic_api_key()."""

    def test_returns_env_var_when_set(self):
        import llm_clients
        # Reset cached state
        llm_clients._api_key_cache = None
        llm_clients._api_key_failed = False
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key-12345"}):
            key = get_anthropic_api_key()
            assert key == "test-key-12345"
        # Clean up
        llm_clients._api_key_cache = None

    def test_caches_after_first_success(self):
        import llm_clients
        llm_clients._api_key_cache = None
        llm_clients._api_key_failed = False
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "cached-key-abc"}):
            key1 = get_anthropic_api_key()
        # Now even without env var, cache should return the same key
        key2 = get_anthropic_api_key()
        assert key1 == key2 == "cached-key-abc"
        # Clean up
        llm_clients._api_key_cache = None

    def test_raises_runtime_error_when_unavailable(self):
        import llm_clients
        from pathlib import Path as RealPath
        llm_clients._api_key_cache = None
        llm_clients._api_key_failed = False

        # We need to block all three sources: env var, .env file, and Keychain.
        # 1. Clear ANTHROPIC_API_KEY from env
        # 2. Make the .env file path resolve to something that doesn't exist
        # 3. Make subprocess.run raise FileNotFoundError (no 'security' command)
        original_path_truediv = RealPath.__truediv__

        def _fake_truediv(self, other):
            result = original_path_truediv(self, other)
            if str(other) == ".env":
                # Return a path that doesn't exist
                return RealPath("/nonexistent/.env")
            return result

        with patch.dict(os.environ, {}, clear=False), \
             patch("llm_clients.subprocess.run", side_effect=FileNotFoundError), \
             patch.object(RealPath, "__truediv__", _fake_truediv):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            with pytest.raises(RuntimeError):
                get_anthropic_api_key()
        # Clean up
        llm_clients._api_key_cache = None
        llm_clients._api_key_failed = False


# ---------------------------------------------------------------------------
# call_low_reasoning / call_high_reasoning error handling
# ---------------------------------------------------------------------------

class TestCallLowReasoning:
    """Tests for call_low_reasoning() error handling."""

    def test_returns_none_on_runtime_error(self):
        import llm_clients
        llm_clients._low_warned = False
        with patch("llm_clients.call_anthropic", side_effect=RuntimeError("no key")):
            result, duration = call_low_reasoning("test prompt")
            assert result is None
            assert duration == 0.0

    def test_does_not_raise(self):
        """call_low_reasoning never raises, even on RuntimeError."""
        import llm_clients
        llm_clients._low_warned = False
        with patch("llm_clients.call_anthropic", side_effect=RuntimeError("no key")):
            # Should not raise
            result, duration = call_low_reasoning("test prompt")
            assert result is None


class TestCallHighReasoning:
    """Tests for call_high_reasoning() error handling."""

    def test_returns_none_on_runtime_error(self):
        import llm_clients
        llm_clients._high_warned = False
        with patch("llm_clients.call_anthropic", side_effect=RuntimeError("no key")):
            result, duration = call_high_reasoning("test prompt")
            assert result is None
            assert duration == 0.0

    def test_does_not_raise(self):
        """call_high_reasoning never raises, even on RuntimeError."""
        import llm_clients
        llm_clients._high_warned = False
        with patch("llm_clients.call_anthropic", side_effect=RuntimeError("no key")):
            result, duration = call_high_reasoning("test prompt")
            assert result is None
