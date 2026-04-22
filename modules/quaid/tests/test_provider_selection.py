"""Tests for provider resolution logic — how adapters and config combine to select providers."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from lib.adapter import (
    StandaloneAdapter,
    TestAdapter,
    set_adapter,
    reset_adapter,
    get_adapter,
)
from lib.embeddings import (
    _embedding_parallel_workers,
    get_embeddings,
    get_embeddings_provider,
    set_embeddings_provider,
    reset_embeddings_provider,
)
from lib.providers import (
    AnthropicLLMProvider,
    TestLLMProvider,
    OllamaEmbeddingsProvider,
    MockEmbeddingsProvider,
)
from adaptors.openclaw.adapter import OpenClawAdapter
from adaptors.openclaw.providers import GatewayLLMProvider, OpenClawGatewayLLMProvider


# ---------------------------------------------------------------------------
# LLM Provider Selection
# ---------------------------------------------------------------------------

class TestLLMProviderSelection:
    """Test that adapters produce the correct LLM provider."""

    @pytest.mark.adapter_openclaw
    def test_openclaw_produces_gateway(self, monkeypatch, tmp_path):
        """OpenClawAdapter returns the gateway provider when configured explicitly."""
        monkeypatch.setenv("OPENCLAW_WORKSPACE", str(tmp_path))
        adapter = OpenClawAdapter()
        set_adapter(adapter)
        cfg = SimpleNamespace(models=SimpleNamespace(
            llm_provider="openclaw-gateway",
            deep_reasoning="claude-sonnet-4-5",
            fast_reasoning="claude-haiku-4-5",
            fast_reasoning_effort="none",
            deep_reasoning_effort="high",
            fast_reasoning_provider="",
            deep_reasoning_provider="",
        ))
        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider()
        assert isinstance(llm, OpenClawGatewayLLMProvider)

    def test_standalone_produces_anthropic(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        adapter = StandaloneAdapter(home=tmp_path)
        set_adapter(adapter)
        cfg = SimpleNamespace(models=SimpleNamespace(llm_provider="anthropic"))
        with patch("config.get_config", return_value=cfg):
            llm = adapter.get_llm_provider()
        assert isinstance(llm, AnthropicLLMProvider)

    def test_test_adapter_produces_test_provider(self, tmp_path):
        adapter = TestAdapter(tmp_path)
        set_adapter(adapter)
        llm = adapter.get_llm_provider()
        assert isinstance(llm, TestLLMProvider)

    def test_test_adapter_canned_responses_work(self, tmp_path):
        adapter = TestAdapter(tmp_path, responses={"fast": "custom"})
        set_adapter(adapter)
        llm = adapter.get_llm_provider()
        result = llm.llm_call([], model_tier="fast")
        assert result.text == "custom"


# ---------------------------------------------------------------------------
# Embeddings Provider Selection
# ---------------------------------------------------------------------------

class TestEmbeddingsProviderSelection:
    """Test embeddings provider resolution chain."""

    def test_mock_env_overrides(self, monkeypatch, tmp_path):
        """MOCK_EMBEDDINGS=1 should always produce MockEmbeddingsProvider."""
        monkeypatch.setenv("MOCK_EMBEDDINGS", "1")
        reset_embeddings_provider()
        adapter = StandaloneAdapter(home=tmp_path)
        set_adapter(adapter)
        provider = get_embeddings_provider()
        assert isinstance(provider, MockEmbeddingsProvider)

    def test_adapter_embeddings_used_when_provided(self, monkeypatch, tmp_path):
        """If adapter provides embeddings, use that."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        adapter = StandaloneAdapter(home=tmp_path)
        custom_embed = MockEmbeddingsProvider()
        adapter.get_embeddings_provider = lambda: custom_embed
        set_adapter(adapter)
        with patch("lib.config.get_embeddings_provider_id", return_value="adapter"):
            provider = get_embeddings_provider()
        assert provider is custom_embed

    def test_default_fallback_to_ollama(self, monkeypatch, tmp_path):
        """Without MOCK_EMBEDDINGS or adapter embeddings, falls back to Ollama."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        monkeypatch.setenv("QUAID_HOME", str(tmp_path))
        monkeypatch.setenv("QUAID_INSTANCE", "standalone-test")
        reset_embeddings_provider()
        adapter = StandaloneAdapter(home=tmp_path)
        set_adapter(adapter)
        # Make config dir so config loads without error
        adapter.config_dir().mkdir(parents=True, exist_ok=True)
        provider = get_embeddings_provider()
        assert isinstance(provider, OllamaEmbeddingsProvider)

    @pytest.mark.parametrize("provider_id", ["ollama", "local-ollama", "standalone-ollama", ""])
    def test_configured_ollama_skips_adapter_resolution(self, monkeypatch, provider_id):
        """Default Ollama embeddings must not pay adapter/plugin discovery cost."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", return_value=provider_id), \
             patch("config.get_config", side_effect=AssertionError("full config should not load")), \
             patch("lib.adapter.get_adapter", side_effect=AssertionError("adapter should not load")):
            provider = get_embeddings_provider()
        assert isinstance(provider, OllamaEmbeddingsProvider)

    def test_set_embeddings_provider(self, tmp_path):
        """set_embeddings_provider overrides resolution."""
        reset_embeddings_provider()
        custom = MockEmbeddingsProvider()
        set_embeddings_provider(custom)
        assert get_embeddings_provider() is custom

    def test_reset_embeddings_provider(self, monkeypatch, tmp_path):
        """reset_embeddings_provider clears the cached provider."""
        monkeypatch.setenv("MOCK_EMBEDDINGS", "1")
        reset_embeddings_provider()
        p1 = get_embeddings_provider()
        reset_embeddings_provider()
        p2 = get_embeddings_provider()
        assert p1 is not p2  # New instance after reset

    def test_provider_singleton(self, monkeypatch, tmp_path):
        """Repeated calls return the same instance."""
        monkeypatch.setenv("MOCK_EMBEDDINGS", "1")
        reset_embeddings_provider()
        p1 = get_embeddings_provider()
        p2 = get_embeddings_provider()
        assert p1 is p2

    def test_adapter_embed_error_falls_back_when_failhard_disabled(self, monkeypatch, tmp_path):
        """When failHard=false, adapter embedding resolution errors degrade to Ollama."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", return_value="adapter"), \
             patch("lib.embeddings.is_fail_hard_enabled", return_value=False), \
             patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter unavailable")), \
             patch("lib.embeddings.notify_agent") as mock_notify:
            provider = get_embeddings_provider()
        assert isinstance(provider, OllamaEmbeddingsProvider)
        mock_notify.assert_called_once()
        assert "embeddings provider" in mock_notify.call_args.args[0]

    def test_adapter_embed_error_with_broken_ollama_config_falls_back_when_failhard_disabled(self, monkeypatch):
        """Compound adapter+Ollama config failure still soft-degrades when failHard=false."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", return_value="adapter"), \
             patch("lib.embeddings.is_fail_hard_enabled", return_value=False), \
             patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter unavailable")), \
             patch(
                 "lib.embeddings._build_ollama_embeddings_provider",
                 side_effect=RuntimeError("ollama config broken"),
             ), \
             patch("lib.embeddings.notify_agent") as mock_notify:
            provider = get_embeddings_provider()

        assert isinstance(provider, OllamaEmbeddingsProvider)
        assert mock_notify.call_count == 2

    def test_adapter_embed_error_raises_when_failhard_enabled(self, monkeypatch):
        """When failHard=true, adapter embedding resolution errors raise."""
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", return_value="adapter"), \
             patch("lib.embeddings.is_fail_hard_enabled", return_value=True), \
             patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter unavailable")), \
             patch("lib.embeddings.notify_agent") as mock_notify:
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                get_embeddings_provider()
        mock_notify.assert_called_once()

    def test_embedding_provider_config_error_raises_when_failhard_enabled(self, monkeypatch):
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", side_effect=RuntimeError("config broken")), \
             patch("lib.embeddings.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="configured embeddings provider"):
                get_embeddings_provider()

    def test_embedding_provider_config_error_defaults_to_ollama_when_failhard_disabled(self, monkeypatch):
        monkeypatch.delenv("MOCK_EMBEDDINGS", raising=False)
        reset_embeddings_provider()
        with patch("lib.config.get_embeddings_provider_id", side_effect=RuntimeError("config broken")), \
             patch("lib.embeddings.is_fail_hard_enabled", return_value=False):
            provider = get_embeddings_provider()
        assert isinstance(provider, OllamaEmbeddingsProvider)

    def test_get_embeddings_prefers_embed_many_and_fans_out_duplicates(self, tmp_path):
        class _BatchProvider(MockEmbeddingsProvider):
            def __init__(self):
                self.calls = []

            def embed(self, text):
                raise AssertionError("embed() should not be called when embed_many succeeds")

            def embed_many(self, texts):
                self.calls.append(list(texts))
                return [[float(idx)] for idx, _ in enumerate(texts, start=1)]

        provider = _BatchProvider()
        reset_embeddings_provider()
        set_embeddings_provider(provider)

        try:
            out = get_embeddings(["alpha", "beta", "alpha"])
        finally:
            reset_embeddings_provider()

        assert provider.calls == [["alpha", "beta"]]
        assert out == [[1.0], [2.0], [1.0]]

    def test_embedding_workers_use_separate_parallel_setting(self, monkeypatch):
        class _Cfg:
            core = type(
                "Core",
                (),
                {
                    "parallel": type(
                        "Parallel",
                        (),
                        {
                            "enabled": True,
                            "llm_workers": 9,
                            "embedding_workers": 3,
                            "task_workers": {},
                        },
                    )()
                },
            )()

        monkeypatch.setattr("config.get_config", lambda: _Cfg())
        assert _embedding_parallel_workers() == 3


# ---------------------------------------------------------------------------
# Integration: reset_adapter clears embeddings
# ---------------------------------------------------------------------------

class TestResetClearsEmbeddings:
    def test_reset_adapter_clears_embeddings_provider(self, monkeypatch, tmp_path):
        """reset_adapter() should clear the embeddings provider cache."""
        monkeypatch.setenv("MOCK_EMBEDDINGS", "1")
        reset_embeddings_provider()
        p1 = get_embeddings_provider()
        reset_adapter()  # This calls reset_embeddings_provider internally
        p2 = get_embeddings_provider()
        assert p1 is not p2
