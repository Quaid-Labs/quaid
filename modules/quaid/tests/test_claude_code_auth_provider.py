from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from adaptors.claude_code.providers import ClaudeCodeOAuthLLMProvider


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
