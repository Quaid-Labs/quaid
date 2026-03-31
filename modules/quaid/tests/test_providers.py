"""Tests for lib/providers.py — provider ABCs and concrete implementations."""

import json
import os
import sys
import io
import queue
import urllib.error
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
    OpenAICompatibleLLMProvider,
    TestLLMProvider,
    OllamaEmbeddingsProvider,
    MockEmbeddingsProvider,
)
from adaptors.openclaw.providers import GatewayLLMProvider
from adaptors.codex.providers import CodexLLMProvider, _CodexAppServerManager


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

    def test_llm_call_uses_bearer_for_oauth_token(self):
        """OAuth tokens should use Authorization bearer, not x-api-key."""
        p = AnthropicLLMProvider(api_key="sk-ant-oat01-test-oauth-token")
        response_data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model": "claude-haiku-4-5",
            "stop_reason": "end_turn",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                model_tier="fast", max_tokens=100,
            )
            req = mock_open.call_args[0][0]
            assert req.get_header("Authorization") == "Bearer sk-ant-oat01-test-oauth-token"
            assert req.get_header("X-api-key") is None
            assert req.get_header("Accept") == "application/json"
            assert req.get_header("User-agent") == "claude-cli/2.1.2 (external, cli)"
            assert req.get_header("X-app") == "cli"
            assert "claude-code-20250219" in (req.get_header("Anthropic-beta") or "")
            assert "oauth-2025-04-20" in (req.get_header("Anthropic-beta") or "")
            body = json.loads(mock_open.call_args[0][0].data.decode())
            assert body["system"][0]["text"] == "You are Claude Code, Anthropic's official CLI for Claude."
            assert body["system"][1]["text"] == "sys"

    def test_llm_call_raises_on_error(self):
        """API errors should propagate."""
        p = AnthropicLLMProvider(api_key="sk-test")
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                p.llm_call([{"role": "user", "content": "hi"}])

    def test_llm_call_raises_on_non_object_json(self):
        p = AnthropicLLMProvider(api_key="sk-test-key")
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(["bad", "shape"]).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="non-object JSON"):
                p.llm_call([{"role": "user", "content": "hi"}])

    def test_llm_call_retries_on_retryable_http_error(self, monkeypatch):
        p = AnthropicLLMProvider(api_key="sk-test-key")
        monkeypatch.setenv("ANTHROPIC_RETRY_ATTEMPTS", "2")
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_S", "0")
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_CAP_S", "0")

        first_err = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=529,
            msg="overloaded",
            hdrs={},
            fp=io.BytesIO(b'{"type":"error","error":{"type":"overloaded_error"}}'),
        )
        response_data = {
            "content": [{"type": "text", "text": "Recovered"}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
            "model": "claude-haiku-4-5-20251001",
            "stop_reason": "end_turn",
        }
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps(response_data).encode()
        ok_resp.__enter__ = lambda s: s
        ok_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", side_effect=[first_err, ok_resp]) as mock_open:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")
        assert result.text == "Recovered"
        assert mock_open.call_count == 2

    def test_llm_call_retries_on_retryable_http_error_by_default(self, monkeypatch):
        p = AnthropicLLMProvider(api_key="sk-test-key")
        monkeypatch.delenv("ANTHROPIC_RETRY_ATTEMPTS", raising=False)
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_S", "0")
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_CAP_S", "0")

        first_err = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=529,
            msg="overloaded",
            hdrs={},
            fp=io.BytesIO(b'{"type":"error","error":{"type":"overloaded_error"}}'),
        )
        response_data = {
            "content": [{"type": "text", "text": "Recovered"}],
            "usage": {"input_tokens": 8, "output_tokens": 3},
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
        }
        ok_resp = MagicMock()
        ok_resp.read.return_value = json.dumps(response_data).encode()
        ok_resp.__enter__ = lambda s: s
        ok_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", side_effect=[first_err, ok_resp]) as mock_open:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
        assert result.text == "Recovered"
        assert mock_open.call_count == 2

    def test_llm_call_does_not_retry_on_non_retryable_http_error(self, monkeypatch):
        p = AnthropicLLMProvider(api_key="sk-test-key")
        monkeypatch.setenv("ANTHROPIC_RETRY_ATTEMPTS", "3")
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_S", "0")
        monkeypatch.setenv("ANTHROPIC_RETRY_BACKOFF_CAP_S", "0")

        bad_req = urllib.error.HTTPError(
            url="https://api.anthropic.com/v1/messages",
            code=400,
            msg="bad_request",
            hdrs={},
            fp=io.BytesIO(b'{"type":"error","error":{"type":"invalid_request_error"}}'),
        )
        with patch("lib.providers.urllib.request.urlopen", side_effect=bad_req) as mock_open:
            with pytest.raises(RuntimeError, match="HTTPError code=400"):
                p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")
        assert mock_open.call_count == 1


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

    def test_llm_call_mock_subprocess(self, monkeypatch):
        """Mock subprocess to verify claude CLI invocation."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
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
            turns_idx = cmd.index("--max-turns")
            assert cmd[turns_idx + 1] == "2"
            assert "hi" not in cmd
            assert mock_run.call_args.kwargs["input"] == "hi"

    def test_llm_call_raises_on_cli_failure(self, monkeypatch):
        """Failed claude CLI should raise instead of returning a soft-null response."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="Claude Code failed"):
                p.llm_call(
                    [{"role": "user", "content": "hi"}],
                )

    def test_llm_call_failure_includes_tail_of_long_stderr(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = ("prefix-" * 80) + "CRITICAL_END_MARKER"

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError) as exc:
                p.llm_call([{"role": "user", "content": "hi"}])

        msg = str(exc.value)
        assert "CRITICAL_END_MARKER" in msg
        assert " ... " in msg

    def test_llm_call_raises_on_timeout(self, monkeypatch):
        """Timeout should propagate."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        import subprocess
        p = ClaudeCodeLLMProvider()

        with patch("lib.providers.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120)):
            with pytest.raises(subprocess.TimeoutExpired):
                p.llm_call([{"role": "user", "content": "hi"}])

    def test_llm_call_timeout_cap_can_reduce_effective_timeout(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_TIMEOUT_CAP_S", "2")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], timeout=30)
        assert mock_run.call_args.kwargs["timeout"] == 2.0

    def test_llm_call_deep_timeout_has_default_floor(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.delenv("CLAUDE_CODE_DEEP_TIMEOUT_S", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_S", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_CAP_S", raising=False)
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep", timeout=120)
        assert mock_run.call_args.kwargs["timeout"] == 600.0

    def test_llm_call_deep_timeout_env_override_above_floor(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_TIMEOUT_S", "450")
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_S", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_CAP_S", raising=False)
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep", timeout=120)
        assert mock_run.call_args.kwargs["timeout"] == 450.0

    def test_llm_call_timeout_multiplier_scales_effective_timeout(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.delenv("CLAUDE_CODE_DEEP_TIMEOUT_S", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_S", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_TIMEOUT_CAP_S", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_TIMEOUT_MULTIPLIER", "2")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep", timeout=120)
        assert mock_run.call_args.kwargs["timeout"] == 1200.0

    def test_llm_call_raises_on_non_object_json(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(["bad", "shape"])
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="non-object JSON"):
                p.llm_call([{"role": "user", "content": "hi"}])

    def test_llm_call_non_json_preserves_decode_cause(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not-json"
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="non-JSON output") as excinfo:
                p.llm_call([{"role": "user", "content": "hi"}])
        assert excinfo.value.__cause__ is not None

    def test_llm_call_raises_when_result_missing(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="empty result"):
                p.llm_call([{"role": "user", "content": "hi"}])

    def test_llm_call_missing_result_includes_payload_detail(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"modelUsage": {}, "meta": "x"})
        mock_result.stderr = "stderr-tail"

        with patch("lib.providers.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError) as excinfo:
                p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
        msg = str(excinfo.value)
        assert ("empty result" in msg) or ("missing result" in msg)
        assert "stdout=" in msg
        assert "stderr=stderr-tail" in msg

    def test_llm_call_retries_missing_result_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "0")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRIES", "2")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps({"modelUsage": {}})
        bad.stderr = ""

        good = MagicMock()
        good.returncode = 0
        good.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        good.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, good]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}])

        assert result.text == "ok"
        assert mock_run.call_count == 2

    def test_llm_call_missing_result_success_payload_falls_back_to_plain_text(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "modelUsage": {}}
        )
        bad.stderr = ""

        plain = MagicMock()
        plain.returncode = 0
        plain.stdout = "{\"ok\":true}\n"
        plain.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, plain]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "{\"ok\":true}"
        assert mock_run.call_count == 2
        fallback_cmd = mock_run.call_args_list[1].args[0]
        assert "--output-format" not in fallback_cmd

    def test_llm_call_empty_result_success_payload_falls_back_to_plain_text(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "", "modelUsage": {}}
        )
        bad.stderr = ""

        plain = MagicMock()
        plain.returncode = 0
        plain.stdout = "plain fallback text\n"
        plain.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, plain]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "plain fallback text"
        assert mock_run.call_count == 2

    def test_llm_call_deep_command_retry_recovers_after_malformed_streak(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", "1")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "", "modelUsage": {}}
        )
        bad.stderr = ""

        good = MagicMock()
        good.returncode = 0
        good.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        good.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, MagicMock(returncode=0, stdout="", stderr=""), good]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "ok"
        assert mock_run.call_count == 3

    def test_llm_call_deep_command_retry_switches_to_text_mode_and_recovers(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", "1")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "", "modelUsage": {}}
        )
        bad.stderr = ""

        text_ok = MagicMock()
        text_ok.returncode = 0
        text_ok.stdout = "```json\n{\"facts\": []}\n```"
        text_ok.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, MagicMock(returncode=0, stdout="", stderr=""), text_ok]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "```json\n{\"facts\": []}\n```"
        retry_cmd = mock_run.call_args_list[-1].args[0]
        assert "--output-format" not in retry_cmd

    def test_llm_call_deep_command_retry_recovers_after_empty_text_mode_output(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", "2")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps(
            {"type": "result", "subtype": "success", "is_error": False, "result": "", "modelUsage": {}}
        )
        bad.stderr = ""

        empty_text = MagicMock()
        empty_text.returncode = 0
        empty_text.stdout = ""
        empty_text.stderr = ""

        text_ok = MagicMock()
        text_ok.returncode = 0
        text_ok.stdout = "plain fallback text"
        text_ok.stderr = ""

        with patch(
            "lib.providers.subprocess.run",
            side_effect=[bad, empty_text, text_ok],
        ) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "plain fallback text"
        assert mock_run.call_count == 3
        second_cmd = mock_run.call_args_list[1].args[0]
        third_cmd = mock_run.call_args_list[2].args[0]
        assert "--output-format" not in second_cmd
        assert "--output-format" not in third_cmd

    def test_llm_call_deep_command_retry_recovers_after_retryable_error_payload(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", "1")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        retryable = MagicMock()
        retryable.returncode = 1
        retryable.stdout = ""
        retryable.stderr = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "message": "transient cli failure",
            }
        )

        good = MagicMock()
        good.returncode = 0
        good.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        good.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[retryable, good]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

        assert result.text == "ok"
        assert mock_run.call_count == 2

    def test_llm_call_deep_uses_stronger_default_malformed_retries(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.delenv("CLAUDE_CODE_MALFORMED_RETRIES", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = json.dumps({"modelUsage": {}})
        bad.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, bad, bad, bad, bad]) as mock_run:
            with pytest.raises(RuntimeError, match="(empty|missing) result"):
                p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
        assert mock_run.call_count == 5

    def test_llm_call_deep_uses_stronger_default_command_retries(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.delenv("CLAUDE_CODE_COMMAND_RETRIES", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DEEP_COMMAND_RETRIES", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRIES", "0")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MALFORMED_RETRY_DELAY_S", "0")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = ""
        bad.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad] * 5) as mock_run:
            with pytest.raises(RuntimeError, match="non-JSON output"):
                p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
        assert mock_run.call_count == 5

    def test_llm_call_fast_keeps_single_turn_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")
        cmd = mock_run.call_args[0][0]
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "1"

    def test_llm_call_deep_max_turns_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_DEEP_MAX_TURNS", "3")
        p = ClaudeCodeLLMProvider()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
        cmd = mock_run.call_args[0][0]
        turns_idx = cmd.index("--max-turns")
        assert cmd[turns_idx + 1] == "3"

    def test_llm_call_retries_non_json_then_succeeds(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRY_DELAY_S", "0")
        monkeypatch.setenv("CLAUDE_CODE_MALFORMED_RETRIES", "2")
        p = ClaudeCodeLLMProvider()

        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = "not-json"
        bad.stderr = ""

        good = MagicMock()
        good.returncode = 0
        good.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        good.stderr = ""

        with patch("lib.providers.subprocess.run", side_effect=[bad, good]) as mock_run:
            result = p.llm_call([{"role": "user", "content": "hi"}])

        assert result.text == "ok"
        assert mock_run.call_count == 2

    def test_oauth_env_file_fallback_used_when_failhard_disabled(self, tmp_path, monkeypatch):
        p = ClaudeCodeLLMProvider()
        env_file = tmp_path / ".env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.is_fail_hard_enabled", return_value=False), \
             patch("lib.adapter.get_adapter", return_value=MagicMock(quaid_home=lambda: tmp_path)), \
             patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
            passed_env = mock_run.call_args.kwargs["env"]
            assert passed_env.get("CLAUDE_CODE_OAUTH_TOKEN") == "token-from-file"

    def test_oauth_env_file_fallback_logs_structured_warnings(self, tmp_path, monkeypatch):
        p = ClaudeCodeLLMProvider()
        env_file = tmp_path / ".env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.is_fail_hard_enabled", return_value=False), \
             patch("lib.adapter.get_adapter", return_value=MagicMock(quaid_home=lambda: tmp_path)), \
             patch("lib.providers.logger.warning") as log_warning, \
             patch("lib.providers.subprocess.run", return_value=mock_result):
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
            warning_messages = [str(call.args[0]) for call in log_warning.call_args_list if call.args]
            assert any("attempting adapter .env fallback" in msg for msg in warning_messages)
            assert any("Loaded CLAUDE_CODE_OAUTH_TOKEN from adapter env fallback" in msg for msg in warning_messages)

    def test_oauth_env_file_read_error_logs_structured_error(self, tmp_path, monkeypatch):
        p = ClaudeCodeLLMProvider()
        env_file = tmp_path / ".env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.is_fail_hard_enabled", return_value=False), \
             patch("lib.adapter.get_adapter", return_value=MagicMock(quaid_home=lambda: tmp_path)), \
             patch("builtins.open", side_effect=OSError("boom")), \
             patch("lib.providers.logger.error") as log_error, \
             patch("lib.providers.subprocess.run", return_value=mock_result):
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
            error_messages = [str(call.args[0]) for call in log_error.call_args_list if call.args]
            assert any("Failed reading adapter env fallback path=" in msg for msg in error_messages)

    def test_oauth_env_file_fallback_blocked_when_failhard_enabled(self, tmp_path, monkeypatch):
        p = ClaudeCodeLLMProvider()
        env_file = tmp_path / ".env"
        env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=token-from-file\n")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"result": "ok", "modelUsage": {}})
        mock_result.stderr = ""

        with patch("lib.providers.is_fail_hard_enabled", return_value=True), \
             patch("lib.adapter.get_adapter", return_value=MagicMock(quaid_home=lambda: tmp_path)), \
             patch("lib.providers.subprocess.run", return_value=mock_result) as mock_run:
            with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN is required"):
                p.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
            mock_run.assert_not_called()


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
# OpenAICompatibleLLMProvider
# ---------------------------------------------------------------------------

class TestOpenAICompatibleLLMProvider:
    def test_init_defaults(self):
        p = OpenAICompatibleLLMProvider()
        assert p._base_url == "http://localhost:8000"
        assert p._api_key == ""
        assert p._deep_reasoning_effort == "high"
        assert p._fast_reasoning_effort == "none"

    def test_llm_call_uses_responses_api_for_openai_gpt5_models(self):
        p = OpenAICompatibleLLMProvider(
            base_url="https://api.openai.com",
            api_key="sk-openai-test",
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
            deep_reasoning_effort="high",
            fast_reasoning_effort="none",
        )
        response_data = {
            "output_text": "Hello!",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 5,
                "input_tokens_details": {"cached_tokens": 2},
            },
            "model": "gpt-5.4-mini",
            "incomplete": False,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                model_tier="fast",
                max_tokens=100,
            )

        assert result.text == "Hello!"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.cache_read_tokens == 2
        assert result.model == "gpt-5.4-mini"

        req = mock_open.call_args[0][0]
        assert req.full_url == "https://api.openai.com/v1/responses"
        assert req.get_header("Authorization") == "Bearer sk-openai-test"
        sent_body = json.loads(req.data.decode())
        assert sent_body["model"] == "gpt-5.4-mini"
        assert sent_body["instructions"] == "sys"
        assert sent_body["input"] == "hi"
        assert sent_body["reasoning"] == {"effort": "none"}
        assert sent_body["max_output_tokens"] == 100

    def test_llm_call_uses_deep_effort_for_deep_tier(self):
        p = OpenAICompatibleLLMProvider(
            base_url="https://api.openai.com",
            api_key="sk-openai-test",
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
            deep_reasoning_effort="high",
            fast_reasoning_effort="none",
        )
        response_data = {
            "output_text": "Deep reply",
            "usage": {"input_tokens": 11, "output_tokens": 6},
            "model": "gpt-5.4",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.llm_call(
                [{"role": "user", "content": "solve it"}],
                model_tier="deep",
                max_tokens=128,
            )

        assert result.text == "Deep reply"
        sent_body = json.loads(mock_open.call_args[0][0].data.decode())
        assert sent_body["model"] == "gpt-5.4"
        assert sent_body["reasoning"] == {"effort": "high"}

    def test_llm_call_uses_chat_completions_for_non_openai_endpoint(self):
        p = OpenAICompatibleLLMProvider(
            base_url="http://localhost:8000",
            api_key="local-key",
            deep_model="qwen3",
            fast_model="qwen3-mini",
        )
        response_data = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast", max_tokens=55)

        assert result.text == "ok"
        req = mock_open.call_args[0][0]
        assert req.full_url == "http://localhost:8000/v1/chat/completions"
        sent_body = json.loads(req.data.decode())
        assert sent_body["model"] == "qwen3-mini"
        assert sent_body["messages"] == [{"role": "user", "content": "hi"}]
        assert sent_body["max_tokens"] == 55

    def test_llm_call_extracts_text_from_responses_output_chunks(self):
        p = OpenAICompatibleLLMProvider(
            base_url="https://api.openai.com",
            api_key="sk-openai-test",
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
        )
        response_data = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "hello"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "world"}]},
            ],
            "usage": {"input_tokens": 2, "output_tokens": 2},
            "model": "gpt-5.4-mini",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")

        assert result.text == "hello\nworld"

    def test_llm_call_marks_incomplete_responses_as_truncated(self):
        p = OpenAICompatibleLLMProvider(
            base_url="https://api.openai.com",
            api_key="sk-openai-test",
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
        )
        response_data = {
            "output_text": "partial",
            "usage": {"input_tokens": 2, "output_tokens": 1},
            "model": "gpt-5.4-mini",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")

        assert result.truncated is True


class TestCodexLLMProvider:
    class _FakeManager:
        def __init__(self):
            self.calls = []

        def run_turn(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "text": "PONG",
                "duration": 1.25,
                "model": kwargs["model"],
                "usage": {
                    "inputTokens": 11,
                    "outputTokens": 4,
                    "cachedInputTokens": 3,
                },
            }

    def test_fast_lane_uses_fast_model_none_and_fast_service_tier(self):
        manager = self._FakeManager()
        provider = CodexLLMProvider(
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
            deep_reasoning_effort="high",
            fast_reasoning_effort="none",
            manager=manager,
        )
        result = provider.llm_call(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            model_tier="fast",
        )
        assert result.text == "PONG"
        assert result.model == "gpt-5.4-mini"
        assert result.input_tokens == 11
        assert result.cache_read_tokens == 3
        call = manager.calls[0]
        assert call["model"] == "gpt-5.4-mini"
        assert call["effort"] == "none"
        assert call["service_tier"] == "fast"
        assert "System Instructions:\nsys" in call["prompt"]
        assert "User Request:\nhi" in call["prompt"]

    def test_deep_lane_uses_deep_model_high_and_flex_service_tier(self):
        manager = self._FakeManager()
        provider = CodexLLMProvider(
            deep_model="gpt-5.4",
            fast_model="gpt-5.4-mini",
            deep_reasoning_effort="high",
            fast_reasoning_effort="none",
            manager=manager,
        )
        result = provider.llm_call(
            [{"role": "user", "content": "analyze this"}],
            model_tier="deep",
        )
        assert result.model == "gpt-5.4"
        call = manager.calls[0]
        assert call["model"] == "gpt-5.4"
        assert call["effort"] == "high"
        assert call["service_tier"] == "flex"

    def test_prompt_preserves_assistant_history(self):
        manager = self._FakeManager()
        provider = CodexLLMProvider(manager=manager)
        provider.llm_call(
            [
                {"role": "user", "content": "Question one"},
                {"role": "assistant", "content": "Prior answer"},
                {"role": "user", "content": "Follow up"},
            ],
            model_tier="deep",
        )
        prompt = manager.calls[0]["prompt"]
        assert "User Request:\nQuestion one" in prompt
        assert "Assistant:\nPrior answer" in prompt
        assert "User Request:\nFollow up" in prompt


class TestCodexAppServerManager:
    def test_run_turn_ignores_broadcast_notifications_without_ids(self):
        manager = _CodexAppServerManager(binary="/tmp/fake-codex")
        notifications: "queue.Queue[dict]" = queue.Queue()
        notifications.put(
            {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage", "text": "broadcast noise"}},
            }
        )
        notifications.put(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "item": {"type": "agentMessage", "text": "real answer"},
                },
            }
        )
        notifications.put(
            {
                "method": "turn/completed",
                "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
            }
        )

        def fake_request(method, params=None, timeout=30.0):
            if method == "thread/start":
                return {"thread": {"id": "thread-1", "path": "/tmp/thread-1.jsonl"}}
            if method == "turn/start":
                return {"turn": {"id": "turn-1"}}
            raise AssertionError(f"unexpected request: {method}")

        manager.request = fake_request
        manager.register_listener = lambda: notifications
        manager.unregister_listener = lambda listener: None

        result = manager.run_turn(
            prompt="hi",
            model="gpt-5.4-mini",
            effort="none",
            service_tier="fast",
            timeout=1.0,
            cwd="/tmp",
        )

        assert result["text"] == "real answer"


# ---------------------------------------------------------------------------
# OllamaEmbeddingsProvider
# ---------------------------------------------------------------------------

class TestOllamaEmbeddingsProvider:
    def test_init_defaults(self):
        p = OllamaEmbeddingsProvider()
        assert p._url == "http://localhost:11434"
        assert p._model == "nomic-embed-text"
        assert p._dim == 768

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
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")), \
             patch("lib.providers.is_fail_hard_enabled", return_value=False):
            result = p.embed("test text")
            assert result is None

    def test_embed_raises_on_error_when_failhard_enabled(self):
        p = OllamaEmbeddingsProvider()
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")), \
             patch("lib.providers.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                p.embed("test text")

    def test_embed_redacts_sensitive_url_in_error_logs(self):
        p = OllamaEmbeddingsProvider(url="http://user:secret@localhost:11434?token=abc")
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")), \
             patch("lib.providers.is_fail_hard_enabled", return_value=False), \
             patch("lib.providers.logger.error") as log_error:
            result = p.embed("test text")

        assert result is None
        rendered = " ".join(str(a) for a in log_error.call_args.args)
        assert "secret" not in rendered
        assert "token=abc" not in rendered
        assert "localhost:11434" in rendered

    def test_embed_returns_none_on_empty_response(self):
        p = OllamaEmbeddingsProvider()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"embeddings": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.embed("test text")
            assert result is None

    def test_embed_retries_once_on_transient_error(self):
        p = OllamaEmbeddingsProvider()
        embedding = [0.2] * 4096
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"embeddings": [embedding]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "lib.providers.urllib.request.urlopen",
            side_effect=[ConnectionError("refused"), mock_resp],
        ) as urlopen, patch("lib.providers.time.sleep") as sleep:
            result = p.embed("test text")

        assert result == embedding
        assert urlopen.call_count == 2
        sleep.assert_called_once()

    def test_embed_many_mock_http(self):
        p = OllamaEmbeddingsProvider()
        embeddings = [[0.1] * 4, [0.2] * 4]
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"embeddings": embeddings}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("lib.providers.urllib.request.urlopen", return_value=mock_resp):
            result = p.embed_many(["first", "second"])

        assert result == embeddings

    def test_embed_many_splits_large_requests_into_multiple_batches(self, monkeypatch):
        p = OllamaEmbeddingsProvider()
        monkeypatch.setenv("OLLAMA_EMBED_BATCH_SIZE", "2")

        seen_batches = []

        def _fake_urlopen(req, timeout):
            payload = json.loads(req.data.decode("utf-8"))
            batch = list(payload["input"])
            seen_batches.append(batch)
            embeddings = [[float(len(text))] for text in batch]
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"embeddings": embeddings}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("lib.providers.urllib.request.urlopen", side_effect=_fake_urlopen):
            result = p.embed_many(["a", "bb", "ccc", "dddd", "eeeee"])

        assert seen_batches == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
        assert result == [[1.0], [2.0], [3.0], [4.0], [5.0]]

    def test_embed_many_recursively_splits_timeout_batches(self, monkeypatch):
        p = OllamaEmbeddingsProvider()
        monkeypatch.setenv("OLLAMA_EMBED_BATCH_SIZE", "4")

        seen_batches = []

        def _fake_urlopen(req, timeout):
            payload = json.loads(req.data.decode("utf-8"))
            batch = list(payload["input"])
            seen_batches.append(batch)
            if len(batch) > 2:
                raise TimeoutError("timed out")
            embeddings = [[float(len(text))] for text in batch]
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"embeddings": embeddings}).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        with patch("lib.providers.urllib.request.urlopen", side_effect=_fake_urlopen), \
             patch("lib.providers.time.sleep"):
            result = p.embed_many(["a", "bb", "ccc", "dddd", "eeeee"])

        assert seen_batches == [
            ["a", "bb", "ccc", "dddd"],
            ["a", "bb", "ccc", "dddd"],
            ["a", "bb"],
            ["ccc", "dddd"],
            ["eeeee"],
        ]
        assert result == [[1.0], [2.0], [3.0], [4.0], [5.0]]

    def test_embed_many_returns_empty_list_on_error(self):
        p = OllamaEmbeddingsProvider()
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")), \
             patch("lib.providers.is_fail_hard_enabled", return_value=False):
            result = p.embed_many(["first", "second"])

        assert result == []

    def test_embed_many_raises_on_error_when_failhard_enabled(self):
        p = OllamaEmbeddingsProvider()
        with patch("lib.providers.urllib.request.urlopen", side_effect=ConnectionError("refused")), \
             patch("lib.providers.is_fail_hard_enabled", return_value=True):
            with pytest.raises(RuntimeError, match="failHard is enabled"):
                p.embed_many(["first", "second"])


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

    def test_init_defaults(self, monkeypatch):
        monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
        p = GatewayLLMProvider()
        assert p._port == 18789
        assert isinstance(p._token, str)
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

    def test_llm_call_503_preserves_http_error_cause(self):
        import urllib.request
        import urllib.error

        p = GatewayLLMProvider(port=18789)
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"missing credentials"}'),
        )
        with patch.object(urllib.request, "urlopen", side_effect=http_err):
            with pytest.raises(RuntimeError, match="HTTP 503") as excinfo:
                p.llm_call([{"role": "user", "content": "test"}], timeout=1)
        assert excinfo.value.__cause__ is http_err

    def test_llm_call_retries_once_on_transient_http_502(self):
        import urllib.request
        import urllib.error

        p = GatewayLLMProvider(port=18789)
        first_err = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=502,
            msg="Bad Gateway",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"upstream temporary failure"}'),
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "text": "ok",
            "model": "m",
            "input_tokens": 1,
            "output_tokens": 1,
            "truncated": False,
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("adaptors.openclaw.providers.time.sleep", return_value=None), \
             patch.object(urllib.request, "urlopen", side_effect=[first_err, mock_resp]) as mock_open:
            result = p.llm_call([{"role": "user", "content": "test"}], timeout=1)
        assert result.text == "ok"
        assert mock_open.call_count == 2

    def test_llm_call_http_error_with_non_object_json_body_falls_back_to_status_message(self):
        import urllib.request
        import urllib.error

        p = GatewayLLMProvider(port=18789)
        http_err = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'["not-an-object"]'),
        )
        with patch.object(urllib.request, "urlopen", side_effect=http_err):
            with pytest.raises(urllib.error.HTTPError):
                p.llm_call([{"role": "user", "content": "test"}], timeout=1)

    def test_llm_call_falls_back_to_openresponses_on_405(self):
        import urllib.request
        import urllib.error

        p = GatewayLLMProvider(port=18789, token="test-token")
        first_err = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=405,
            msg="Method Not Allowed",
            hdrs=None,
            fp=io.BytesIO(b"Method Not Allowed"),
        )
        fallback_resp = MagicMock()
        fallback_resp.read.return_value = json.dumps({
            "output_text": "fallback ok",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 12, "output_tokens": 7},
            "incomplete": False,
        }).encode()
        fallback_resp.__enter__ = MagicMock(return_value=fallback_resp)
        fallback_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", side_effect=[first_err, fallback_resp]) as mock_open:
            result = p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hello"}],
                model_tier="fast",
                timeout=1,
            )

        assert result.text == "fallback ok"
        assert result.input_tokens == 12
        assert result.output_tokens == 7
        assert mock_open.call_count == 2
        fallback_req = mock_open.call_args_list[1][0][0]
        assert "/v1/responses" in fallback_req.full_url
        sent_body = json.loads(fallback_req.data)
        assert sent_body["model"] == "openclaw"
        assert sent_body["input"] == "hello"
        assert sent_body["instructions"] == "sys"
        assert fallback_req.get_header("X-openclaw-model") == "anthropic/claude-haiku-4-5"

    def test_openresponses_fallback_uses_workspace_model_from_memory_config(self, tmp_path, monkeypatch):
        import urllib.request

        workspace = tmp_path / "ws"
        cfg_dir = workspace / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "memory.json").write_text(
            json.dumps({"models": {"fastReasoning": "qwen2.5-coder:7b", "deepReasoning": "claude-opus-4-6"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(workspace))

        p = GatewayLLMProvider(port=18789, token="test-token")
        fallback_resp = MagicMock()
        fallback_resp.read.return_value = json.dumps({
            "output_text": "ok",
            "model": "qwen2.5-coder:7b",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        fallback_resp.__enter__ = MagicMock(return_value=fallback_resp)
        fallback_resp.__exit__ = MagicMock(return_value=False)

        with patch.object(urllib.request, "urlopen", return_value=fallback_resp) as mock_open:
            result = p._llm_call_openresponses(
                system_prompt="sys",
                user_message="hello",
                model_tier="fast",
                max_tokens=32,
                timeout=1,
                start_time=0.0,
            )
        assert result.text == "ok"
        req_obj = mock_open.call_args[0][0]
        sent_body = json.loads(req_obj.data)
        assert sent_body["model"] == "openclaw"
        assert req_obj.get_header("X-openclaw-model") == "anthropic/qwen2.5-coder:7b"

    def test_gateway_provider_resolves_token_from_env_when_missing_explicit_token(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "env-gateway-token")
        p = GatewayLLMProvider(port=18789, token="")
        assert p._token == "env-gateway-token"

    def test_gateway_provider_resolves_token_from_openclaw_config(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        cfg_dir = home / ".openclaw"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "openclaw.json").write_text(
            json.dumps({"gateway": {"auth": {"mode": "token", "token": "cfg-token"}}}),
            encoding="utf-8",
        )
        monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(home))
        p = GatewayLLMProvider(port=18789, token="")
        assert p._token == "cfg-token"

    def test_openresponses_fallback_sends_x_openclaw_model_header(self, monkeypatch, tmp_path):
        """Fallback /v1/responses path must send x-openclaw-model for per-request model
        routing (v2026.3.24+).

        Without this header the gateway silently routes every call to its configured
        primary model (e.g. claude-sonnet-4-5), completely ignoring the fast/deep tier
        config in memory.json.  This test guards against that regression by verifying
        the outbound request contains the header with the correct anthropic/<model> value.
        """
        import urllib.request as _ureq
        import urllib.error

        quaid_home = tmp_path / "quaid"
        cfg_dir = quaid_home / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "memory.json").write_text(
            json.dumps({
                "models": {
                    "fastReasoning": "claude-haiku-4-5",
                    "deepReasoning": "claude-haiku-4-5",
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(quaid_home))
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)

        p = GatewayLLMProvider(port=18789, token="test-token")

        # Force the primary /plugins/quaid/llm path to 404 so the fallback fires.
        not_found = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"not found"}'),
        )
        mock_resp_data = json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "model": "openclaw",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = mock_resp_data
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured_requests = []

        def fake_urlopen(req, timeout=None):
            captured_requests.append(req)
            if "/plugins/quaid/llm" in req.full_url:
                raise not_found
            return mock_resp

        with patch.object(_ureq, "urlopen", side_effect=fake_urlopen):
            p.llm_call(
                [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                model_tier="fast",
            )

        openresponses_reqs = [r for r in captured_requests if "/v1/responses" in r.full_url]
        assert openresponses_reqs, "Expected fallback call to /v1/responses"
        req = openresponses_reqs[0]

        header = req.get_header("X-openclaw-model")
        assert header is not None, (
            "x-openclaw-model header missing from /v1/responses fallback. "
            "Without it the gateway ignores tier config and uses its primary model."
        )
        assert header.startswith("anthropic/"), (
            f"Expected header format 'anthropic/<model>', got: {header!r}"
        )
        assert "haiku" in header.lower(), (
            f"fast tier with claude-haiku-4-5 config should produce a haiku header, got: {header!r}"
        )

    def test_openresponses_fallback_x_openclaw_model_deep_tier(self, monkeypatch, tmp_path):
        """Deep tier must also send x-openclaw-model with the deepReasoning model."""
        import urllib.request as _ureq
        import urllib.error

        quaid_home = tmp_path / "quaid"
        cfg_dir = quaid_home / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "memory.json").write_text(
            json.dumps({
                "models": {
                    "fastReasoning": "claude-haiku-4-5",
                    "deepReasoning": "claude-haiku-4-5",
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(quaid_home))
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)

        p = GatewayLLMProvider(port=18789, token="tok")

        not_found = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=404, msg="Not Found", hdrs=None,
            fp=io.BytesIO(b'{"error":"not found"}'),
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "model": "openclaw",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            if "/plugins/quaid/llm" in req.full_url:
                raise not_found
            return mock_resp

        with patch.object(_ureq, "urlopen", side_effect=fake_urlopen):
            p.llm_call(
                [{"role": "user", "content": "hi"}],
                model_tier="deep",
            )

        reqs = [r for r in captured if "/v1/responses" in r.full_url]
        assert reqs
        header = reqs[0].get_header("X-openclaw-model")
        assert header is not None
        assert header.startswith("anthropic/")
        # deep tier also uses haiku per project policy
        assert "haiku" in header.lower()

    def test_openresponses_fallback_model_with_provider_prefix_not_doubled(
        self, monkeypatch, tmp_path
    ):
        """If config already stores model as 'anthropic/claude-haiku-4-5', the header
        should NOT double the prefix to 'anthropic/anthropic/claude-haiku-4-5'."""
        import urllib.request as _ureq
        import urllib.error

        quaid_home = tmp_path / "quaid"
        cfg_dir = quaid_home / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "memory.json").write_text(
            json.dumps({
                "models": {
                    "fastReasoning": "anthropic/claude-haiku-4-5",
                }
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("QUAID_HOME", str(quaid_home))
        monkeypatch.delenv("QUAID_INSTANCE", raising=False)

        p = GatewayLLMProvider(port=18789, token="tok")
        not_found = urllib.error.HTTPError(
            url="http://127.0.0.1:18789/plugins/quaid/llm",
            code=404, msg="Not Found", hdrs=None,
            fp=io.BytesIO(b'{"error":"not found"}'),
        )
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "model": "openclaw",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            if "/plugins/quaid/llm" in req.full_url:
                raise not_found
            return mock_resp

        with patch.object(_ureq, "urlopen", side_effect=fake_urlopen):
            p.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")

        reqs = [r for r in captured if "/v1/responses" in r.full_url]
        header = reqs[0].get_header("X-openclaw-model")
        assert header == "anthropic/claude-haiku-4-5", (
            f"Provider prefix must not be doubled, got: {header!r}"
        )


class TestABCEnforcement:
    def test_llm_provider_cannot_instantiate(self):
        with pytest.raises(TypeError):
            LLMProvider()

    def test_embeddings_provider_cannot_instantiate(self):
        with pytest.raises(TypeError):
            EmbeddingsProvider()
