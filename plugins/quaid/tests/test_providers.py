"""Tests for lib/providers.py — provider ABCs and concrete implementations."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from lib.providers import (
    LLMResult,
    LLMProvider,
    EmbeddingsProvider,
    AnthropicLLMProvider,
    ClaudeCodeLLMProvider,
    TestLLMProvider,
    OllamaEmbeddingsProvider,
    MockEmbeddingsProvider,
    MODEL_TIERS,
)
from adapters.openclaw.providers import GatewayLLMProvider


# ---------------------------------------------------------------------------
# LLMResult dataclass
# ---------------------------------------------------------------------------

class TestLLMResult:
    def test_defaults(self):
        r = LLMResult(text="hello", duration=1.0)
        assert r.text == "hello"
        assert r.duration == 1.0
        assert r.input_tokens == 0
        assert r.output_tokens == 0
        assert r.model == ""
        assert r.truncated is False
        assert r.model_usage == {}

    def test_all_fields(self):
        r = LLMResult(
            text="ok", duration=2.5, input_tokens=100, output_tokens=50,
            cache_read_tokens=80, cache_creation_tokens=20,
            model="test", truncated=True,
            model_usage={"test": {"input": 100, "output": 50}},
        )
        assert r.cache_read_tokens == 80
        assert r.truncated is True

    def test_none_text(self):
        r = LLMResult(text=None, duration=0.5)
        assert r.text is None


# ---------------------------------------------------------------------------
# MODEL_TIERS
# ---------------------------------------------------------------------------

class TestModelTiers:
    def test_anthropic_tiers(self):
        assert "anthropic" in MODEL_TIERS
        assert "claude-opus-4-6" in MODEL_TIERS["anthropic"]["deep"]
        assert "claude-haiku-4-5" in MODEL_TIERS["anthropic"]["fast"]

    def test_openai_tiers(self):
        assert "openai" in MODEL_TIERS
        assert "gpt-4o" in MODEL_TIERS["openai"]["deep"]
        assert "gpt-4o-mini" in MODEL_TIERS["openai"]["fast"]


# ---------------------------------------------------------------------------
# AnthropicLLMProvider
# ---------------------------------------------------------------------------

class TestAnthropicLLMProvider:
    def test_init_defaults(self):
        p = AnthropicLLMProvider()
        assert p._base_url == AnthropicLLMProvider.ANTHROPIC_API_URL
        assert p._api_key == ""
        assert p._deep_model == "claude-opus-4-6"
        assert p._fast_model == "claude-haiku-4-5"

    def test_init_custom(self):
        p = AnthropicLLMProvider(api_key="sk-test", deep_model="opus", fast_model="haiku",
                                  base_url="https://custom.api/v1/messages")
        assert p._base_url == "https://custom.api/v1/messages"
        assert p._api_key == "sk-test"
        assert p._deep_model == "opus"
        assert p._fast_model == "haiku"

    def test_resolve_model_low(self):
        p = AnthropicLLMProvider(fast_model="claude-haiku-4-5")
        assert p._resolve_model("fast") == "claude-haiku-4-5"

    def test_resolve_model_high(self):
        p = AnthropicLLMProvider(deep_model="claude-opus-4-6")
        assert p._resolve_model("deep") == "claude-opus-4-6"

    def test_resolve_model_defaults_to_high(self):
        p = AnthropicLLMProvider(deep_model="opus")
        assert p._resolve_model("deep") == "opus"

    def test_get_profiles_with_key(self):
        p = AnthropicLLMProvider(api_key="sk-test", deep_model="opus", fast_model="haiku")
        profiles = p.get_profiles()
        assert profiles["deep"]["model"] == "opus"
        assert profiles["fast"]["model"] == "haiku"
        assert profiles["deep"]["available"] is True

    def test_get_profiles_without_key(self):
        p = AnthropicLLMProvider()
        profiles = p.get_profiles()
        assert profiles["deep"]["available"] is False

    def test_llm_call_mock_http(self):
        """Mock the HTTP call to verify request format."""
        p = AnthropicLLMProvider(api_key="sk-test-key")
        response_data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model": "claude-opus-4-6",
            "stop_reason": "end_turn",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                model_tier="deep", max_tokens=100,
            )
            assert result.text == "Hello!"
            assert result.input_tokens == 10
            assert result.output_tokens == 5
            assert result.model == "claude-opus-4-6"
            # Verify the request was made with x-api-key header
            req = mock_open.call_args[0][0]
            assert req.get_header("X-api-key") == "sk-test-key"

    def test_llm_call_raises_on_error(self):
        """API errors should propagate."""
        p = AnthropicLLMProvider(api_key="sk-test")
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                p.llm_call([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# ClaudeCodeLLMProvider
# ---------------------------------------------------------------------------

class TestClaudeCodeLLMProvider:
    def test_resolve_alias_high(self):
        p = ClaudeCodeLLMProvider(deep_model="claude-opus-4-6")
        assert p._resolve_alias("deep") == "opus"

    def test_resolve_alias_low(self):
        p = ClaudeCodeLLMProvider(fast_model="claude-haiku-4-5")
        assert p._resolve_alias("fast") == "haiku"

    def test_resolve_alias_unknown(self):
        p = ClaudeCodeLLMProvider(deep_model="grok-3")
        assert p._resolve_alias("deep") == "grok-3"

    def test_get_profiles(self):
        p = ClaudeCodeLLMProvider()
        profiles = p.get_profiles()
        assert profiles["deep"]["model"] == "claude-opus-4-6"
        assert profiles["fast"]["model"] == "claude-haiku-4-5"

    def test_llm_call_mock_subprocess(self):
        """Mock subprocess to verify claude CLI invocation."""
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "result": "Hello!",
            "modelUsage": {
                "claude-opus-4-6": {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheReadInputTokens": 0,
                    "cacheCreationInputTokens": 0,
                }
            }
        })
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            result = p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                model_tier="deep",
            )
            assert result.text == "Hello!"
            assert result.input_tokens == 100
            assert result.output_tokens == 50
            # Verify claude CLI was called
            cmd = mock_run.call_args[0][0]
            assert cmd[0] == "claude"
            assert "-p" in cmd
            assert "--model" in cmd
            idx = cmd.index("--model")
            assert cmd[idx + 1] == "opus"

    def test_llm_call_returns_none_text_on_failure(self):
        """Failed claude CLI returns LLMResult with text=None."""
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            result = p.llm_call(
                [{"role": "user", "content": "hi"}],
            )
            assert result.text is None

    def test_llm_call_raises_on_timeout(self):
        """Timeout should propagate."""
        import subprocess
        p = ClaudeCodeLLMProvider()

        with patch("lib.providers.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            with pytest.raises(subprocess.TimeoutExpired):
                p.llm_call([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# TestLLMProvider
# ---------------------------------------------------------------------------

class TestTestLLMProvider:
    def test_default_response(self):
        p = TestLLMProvider()
        result = p.llm_call([{"role": "user", "content": "hi"}])
        assert result.text == '{"action": "KEEP", "reasoning": "test"}'
        assert result.model == "test-deep"

    def test_custom_responses(self):
        p = TestLLMProvider(responses={"fast": "low-response", "deep": "high-response"})
        result_low = p.llm_call([], model_tier="fast")
        assert result_low.text == "low-response"
        result_high = p.llm_call([], model_tier="deep")
        assert result_high.text == "high-response"

    def test_records_calls(self):
        p = TestLLMProvider()
        p.llm_call([{"role": "user", "content": "a"}], model_tier="fast", max_tokens=200)
        p.llm_call([{"role": "user", "content": "b"}], model_tier="deep", max_tokens=4000)
        assert len(p.calls) == 2
        assert p.calls[0]["model_tier"] == "fast"
        assert p.calls[0]["max_tokens"] == 200
        assert p.calls[1]["model_tier"] == "deep"

    def test_get_profiles(self):
        p = TestLLMProvider()
        profiles = p.get_profiles()
        assert profiles["deep"]["available"] is True
        assert profiles["fast"]["available"] is True


# ---------------------------------------------------------------------------
# OllamaEmbeddingsProvider
# ---------------------------------------------------------------------------

class TestOllamaEmbeddingsProvider:
    def test_init_defaults(self):
        p = OllamaEmbeddingsProvider()
        assert p._url == "http://localhost:11434"
        assert p._model == "qwen3-embedding:8b"
        assert p._dim == 4096

    def test_init_custom(self):
        p = OllamaEmbeddingsProvider(url="http://gpu:11434", model="nomic", dim=768)
        assert p._url == "http://gpu:11434"
        assert p.model_name == "nomic"
        assert p.dimension() == 768

    def test_embed_mock_http(self):
        p = OllamaEmbeddingsProvider()
        embedding = [0.1] * 4096
        response_data = {"embeddings": [embedding]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.embed("test text")
            assert result == embedding

    def test_embed_returns_none_on_error(self):
        p = OllamaEmbeddingsProvider()
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")):
            result = p.embed("test text")
            assert result is None

    def test_embed_returns_none_on_empty_response(self):
        p = OllamaEmbeddingsProvider()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"embeddings": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.embed("test text")
            assert result is None


# ---------------------------------------------------------------------------
# MockEmbeddingsProvider
# ---------------------------------------------------------------------------

class TestMockEmbeddingsProvider:
    def test_dimension(self):
        p = MockEmbeddingsProvider()
        assert p.dimension() == 128

    def test_model_name(self):
        p = MockEmbeddingsProvider()
        assert p.model_name == "mock-md5"

    def test_embed_returns_correct_dim(self):
        p = MockEmbeddingsProvider()
        vec = p.embed("hello world")
        assert len(vec) == 128

    def test_embed_is_deterministic(self):
        p = MockEmbeddingsProvider()
        v1 = p.embed("test text")
        v2 = p.embed("test text")
        assert v1 == v2

    def test_embed_different_texts_differ(self):
        p = MockEmbeddingsProvider()
        v1 = p.embed("hello")
        v2 = p.embed("goodbye")
        assert v1 != v2

    def test_embed_is_normalized(self):
        p = MockEmbeddingsProvider()
        vec = p.embed("test")
        magnitude = sum(x * x for x in vec) ** 0.5
        assert abs(magnitude - 1.0) < 0.001


# ---------------------------------------------------------------------------
# ABC enforcement
# ---------------------------------------------------------------------------

@pytest.mark.adapter_openclaw
class TestGatewayLLMProvider:
    """Tests for GatewayLLMProvider — routes LLM calls through gateway HTTP."""

    def test_init_defaults(self):
        p = GatewayLLMProvider()
        assert p._port == 18789
        assert p._token == ""
        profiles = p.get_profiles()
        assert profiles["deep"]["model"] == "configured-via-gateway"
        assert profiles["fast"]["model"] == "configured-via-gateway"
        assert profiles["deep"]["available"] is True

    def test_init_custom(self):
        p = GatewayLLMProvider(port=9999, token="test-token")
        assert p._port == 9999
        assert p._token == "test-token"

    def test_llm_call_success(self):
        """Mock the HTTP call to the gateway endpoint."""
        import urllib.request
        p = GatewayLLMProvider(port=18789, token="test-token")

        mock_response = json.dumps({
            "text": "test response",
            "model": "claude-haiku-4-5",
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 80,
            "cache_creation_tokens": 10,
            "truncated": False,
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_response
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=mock_resp) as mock_open:
            result = p.llm_call(
                [{"role": "system", "content": "Be helpful"},
                 {"role": "user", "content": "Hello"}],
                model_tier="fast",
            )

        assert result.text == "test response"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.cache_read_tokens == 80
        assert result.model == "claude-haiku-4-5"
        assert result.truncated is False

        # Verify the request was sent correctly
        call_args = mock_open.call_args
        req_obj = call_args[0][0]
        assert "127.0.0.1:18789" in req_obj.full_url
        assert "/plugins/quaid/llm" in req_obj.full_url
        sent_body = json.loads(req_obj.data)
        assert sent_body["system_prompt"] == "Be helpful"
        assert sent_body["user_message"] == "Hello"
        assert sent_body["model_tier"] == "fast"

    def test_llm_call_connection_error(self):
        """Raises on connection error (gateway not running)."""
        p = GatewayLLMProvider(port=59999)  # unlikely to be running
        with pytest.raises(Exception):
            p.llm_call([{"role": "user", "content": "test"}], timeout=1)


class TestABCEnforcement:
    def test_llm_provider_cannot_instantiate(self):
        with pytest.raises(TypeError):
            LLMProvider()

    def test_embeddings_provider_cannot_instantiate(self):
        with pytest.raises(TypeError):
            EmbeddingsProvider()
