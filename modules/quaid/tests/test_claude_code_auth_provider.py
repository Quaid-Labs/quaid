from __future__ import annotations

import io
import json
import logging
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from adaptors.claude_code.providers import ClaudeCodeOAuthLLMProvider, _queue_auth_refresh_notice, _read_token_file


def test_http_401_queues_auth_refresh_notice(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-5",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=401,
        msg="unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"type":"error","error":{"type":"authentication_error"}}'),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        return_value=True,
    ) as queued:
        with patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=False):
            with patch.object(provider, "_get_api_key_provider", return_value=None):
                try:
                    provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
                except RuntimeError:
                    pass

    assert queued.call_count == 1
    notice = queued.call_args.args[0]
    assert "claude setup-token" in notice
    assert "quaid auth refresh <token>" in notice
    assert "credentials.json" not in notice


def test_http_401_model_error_does_not_queue_auth_refresh_notice(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="invalid-model-m6-probe",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=401,
        msg="unauthorized",
        hdrs={},
        fp=io.BytesIO(
            b'{"type":"error","error":{"type":"not_found_error",'
            b'"message":"model invalid-model-m6-probe does not exist"}}'
        ),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        return_value=True,
    ) as queued, patch(
        "adaptors.claude_code.providers.notify_agent",
        return_value=True,
    ) as notify, patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="invalid-model-m6-probe"):
            provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

    assert queued.call_count == 0
    assert notify.call_count == 1
    assert "invalid-model-m6-probe" in notify.call_args.args[0]
    assert notify.call_args.kwargs["source"] == "provider"


def test_http_401_ambiguous_model_marker_without_model_keeps_auth_refresh(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="invalid-model-m6-probe",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=401,
        msg="unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"type":"error","error":{"type":"model_not_found"}}'),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        return_value=True,
    ) as queued, patch(
        "adaptors.claude_code.providers.notify_agent",
        return_value=True,
    ) as notify, patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=False), patch.object(
        provider,
        "_get_api_key_provider",
        return_value=None,
    ):
        with pytest.raises(RuntimeError, match="All LLM auth methods failed"):
            provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

    assert queued.call_count == 1
    assert notify.call_count == 0


def test_http_400_model_error_is_provider_config_error(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-6",
        fast_model="invalid-model-m6-probe",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=400,
        msg="bad request",
        hdrs={},
        fp=io.BytesIO(
            b'{"type":"error","error":{"type":"invalid_request_error",'
            b'"message":"unknown model invalid-model-m6-probe"}}'
        ),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        return_value=True,
    ) as queued, patch(
        "adaptors.claude_code.providers.notify_agent",
        return_value=True,
    ) as notify, patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="invalid-model-m6-probe"):
            provider.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")

    assert queued.call_count == 0
    assert notify.call_count == 1
    assert "invalid-model-m6-probe" in notify.call_args.args[0]


def test_auth_refresh_notice_queue_failure_warns_when_fail_open(caplog) -> None:
    with patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        side_effect=RuntimeError("queue unavailable"),
    ), patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=False):
        with caplog.at_level(logging.WARNING, logger="adaptors.claude_code.providers"):
            _queue_auth_refresh_notice()

    assert "failed queuing auth refresh notice" in caplog.text
    assert "queue unavailable" in caplog.text


def test_auth_refresh_notice_queue_failure_raises_when_failhard() -> None:
    with patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        side_effect=RuntimeError("queue unavailable"),
    ), patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="queue unavailable"):
            _queue_auth_refresh_notice()


def test_failhard_error_guides_token_refresh(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-5",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=401,
        msg="unauthorized",
        hdrs={},
        fp=io.BytesIO(b'{"type":"error","error":{"type":"authentication_error"}}'),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.queue_deferred_notice",
        return_value=True,
    ):
        with patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True):
            try:
                provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")
            except RuntimeError as exc:
                msg = str(exc)
            else:
                raise AssertionError("Expected RuntimeError")

    assert "quaid auth refresh" in msg
    assert "credentials.json" not in msg


def test_api_key_fallback_warning_uses_auth_refresh(monkeypatch, capsys) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-5",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    with patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=False), patch.object(
        provider, "_try_oauth_call", side_effect=Exception("missing token")
    ), patch.object(
        provider,
        "_get_api_key_provider",
        return_value=type(
            "_ApiProviderStub",
            (),
            {"llm_call": staticmethod(lambda *args, **kwargs: type("R", (), {"text": "ok"})())},
        )(),
    ):
        provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

    err = capsys.readouterr().err
    assert "quaid auth refresh <token>" in err
    assert "config set-auth" not in err


def test_oauth_token_in_anthropic_api_key_env_uses_bearer_path(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-5",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-oat01-standalone-oauth")

    with patch.object(provider, "_api_call", return_value=type("R", (), {"text": "ok"})()) as api_call, patch.object(
        provider,
        "_get_api_key_provider",
        side_effect=AssertionError("api key fallback should not be used for oauth token env"),
    ):
        result = provider.llm_call([{"role": "user", "content": "hi"}], model_tier="deep")

    assert result.text == "ok"
    assert api_call.call_count == 1
    assert api_call.call_args.args[0] == "sk-ant-oat01-standalone-oauth"


def test_read_token_file_raises_adapter_failure_when_failhard_enabled() -> None:
    with patch("lib.adapter.get_adapter", side_effect=RuntimeError("adapter unavailable")), patch(
        "adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True
    ):
        with pytest.raises(RuntimeError, match="adapter unavailable"):
            _read_token_file()


def test_http_404_notifies_agent_before_raise(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-5",
        fast_model="claude-haiku-4-5",
    )
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test-token")

    http_err = urllib.error.HTTPError(
        url="https://api.anthropic.com/v1/messages",
        code=404,
        msg="not found",
        hdrs={},
        fp=io.BytesIO(b'{"type":"error","error":{"type":"not_found_error"}}'),
    )

    with patch.object(provider, "_api_call", side_effect=http_err), patch(
        "adaptors.claude_code.providers.notify_agent",
        return_value=True,
    ) as notify:
        with patch("adaptors.claude_code.providers.is_fail_hard_enabled", return_value=True):
            try:
                provider.llm_call([{"role": "user", "content": "hi"}], model_tier="fast")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError("Expected HTTPError")

    assert notify.call_count == 1
    msg = notify.call_args.args[0]
    assert "HTTP 404" in msg
    assert "Check fastReasoning/deepReasoning in config.json" in msg
    assert notify.call_args.kwargs["severity"] == "error"
    assert notify.call_args.kwargs["source"] == "provider"
    assert notify.call_args.kwargs["dedupe_key"] == "cc-http-error:fast:404"


def test_api_call_includes_cc_identity_first_for_oauth_sonnet(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
    )
    token = "sk-ant-oat01-test-oauth-token"
    response_data = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("adaptors.claude_code.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        provider._api_call(
            token=token,
            model="claude-sonnet-4-6",
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            max_tokens=64,
            timeout=30,
        )

        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == f"Bearer {token}"
        body = json.loads(req.data.decode())
        assert body["model"] == "claude-sonnet-4-6"
        assert body["system"][0]["text"] == "You are Claude Code, Anthropic's official CLI for Claude."
        assert body["system"][1]["text"] == "sys"


def test_api_call_preserves_multi_turn_messages(monkeypatch) -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
    )
    response_data = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    messages = [
        {"role": "system", "content": "system one"},
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "system", "content": "system two"},
        {"role": "user", "content": "second user"},
    ]

    with patch("adaptors.claude_code.providers.urllib.request.urlopen", return_value=mock_resp) as mock_open:
        provider._api_call(
            token="sk-ant-oat01-test-oauth-token",
            model="claude-sonnet-4-6",
            messages=messages,
            max_tokens=64,
            timeout=30,
        )

    req = mock_open.call_args[0][0]
    body = json.loads(req.data.decode())
    assert [block["text"] for block in body["system"][1:]] == ["system one", "system two"]
    assert body["messages"] == [
        {"role": "user", "content": "first user"},
        {"role": "assistant", "content": "first assistant"},
        {"role": "user", "content": "second user"},
    ]


def test_get_profiles_marks_sentinel_models_unavailable() -> None:
    provider = ClaudeCodeOAuthLLMProvider(deep_model="default", fast_model=None)

    profiles = provider.get_profiles()

    assert profiles["deep"] == {"model": "default", "available": False}
    assert profiles["fast"] == {"model": None, "available": False}


def test_get_profiles_marks_configured_models_available() -> None:
    provider = ClaudeCodeOAuthLLMProvider(
        deep_model="claude-sonnet-4-6",
        fast_model="claude-haiku-4-5",
    )

    profiles = provider.get_profiles()

    assert profiles["deep"] == {"model": "claude-sonnet-4-6", "available": True}
    assert profiles["fast"] == {"model": "claude-haiku-4-5", "available": True}
