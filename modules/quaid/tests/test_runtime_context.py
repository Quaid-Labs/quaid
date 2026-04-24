from unittest.mock import MagicMock, patch

import pytest


def test_get_bootstrap_markdown_globs_returns_empty_when_fail_hard_disabled():
    from lib import runtime_context

    fake_adapter = MagicMock()
    fake_adapter.get_bootstrap_markdown_globs.side_effect = RuntimeError("adapter unavailable")

    with patch.object(runtime_context, "get_adapter", return_value=fake_adapter), \
         patch.object(runtime_context, "is_fail_hard_enabled", return_value=False):
        assert runtime_context.get_bootstrap_markdown_globs() == []


def test_get_bootstrap_markdown_globs_raises_when_fail_hard_enabled():
    from lib import runtime_context

    fake_adapter = MagicMock()
    fake_adapter.get_bootstrap_markdown_globs.side_effect = RuntimeError("adapter unavailable")

    with patch.object(runtime_context, "get_adapter", return_value=fake_adapter), \
         patch.object(runtime_context, "is_fail_hard_enabled", return_value=True):
        with pytest.raises(RuntimeError, match="bootstrap markdown globs"):
            runtime_context.get_bootstrap_markdown_globs()


def test_get_llm_provider_notifies_and_reraises():
    from lib import runtime_context

    fake_adapter = MagicMock()
    fake_adapter.get_llm_provider.side_effect = RuntimeError("unknown provider")

    with patch.object(runtime_context, "get_adapter", return_value=fake_adapter), \
         patch.object(runtime_context, "_queue_deferred_notice") as mock_queue:
        with pytest.raises(RuntimeError, match="unknown provider"):
            runtime_context.get_llm_provider(model_tier="deep")

    mock_queue.assert_called_once()
    assert "deep language model provider" in mock_queue.call_args.args[0]


def test_get_llm_provider_preserves_original_error_when_notify_fails(caplog):
    from lib import runtime_context

    caplog.set_level("WARNING")
    fake_adapter = MagicMock()
    fake_adapter.get_llm_provider.side_effect = RuntimeError("unknown provider")

    with patch.object(runtime_context, "get_adapter", return_value=fake_adapter), \
         patch.object(runtime_context, "_queue_deferred_notice", side_effect=RuntimeError("queue unavailable")):
        with pytest.raises(RuntimeError, match="unknown provider"):
            runtime_context.get_llm_provider(model_tier="deep")

    assert "Failed queuing provider access error as deferred notice" in caplog.text


def test_fail_policy_logs_when_config_load_fails(caplog, tmp_path, monkeypatch):
    from lib.fail_policy import is_fail_hard_enabled

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-main")
    cfg = tmp_path / "instances" / "codex-main" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{not-json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert is_fail_hard_enabled() is True

    assert any("defaulting to enabled" in rec.message for rec in caplog.records)


def test_get_deferred_notice_status_passes_through_options():
    from lib import runtime_context

    with patch.object(
        runtime_context,
        "_get_deferred_notice_status",
        return_value={"pending_count": 1, "items": [{"kind": "janitor"}]},
    ) as mock_status:
        payload = runtime_context.get_deferred_notice_status(limit=7, include_items=True)

    mock_status.assert_called_once_with(limit=7, include_items=True)
    assert payload["pending_count"] == 1


def test_runtime_context_uses_env_homes_without_adapter(monkeypatch, tmp_path):
    from lib import runtime_context

    hidden = tmp_path / ".quaid"
    visible = tmp_path / "quaid"
    monkeypatch.setenv("QUAID_HOME", str(hidden))
    monkeypatch.delenv("QUAID_VISIBLE_HOME", raising=False)
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)

    with patch.object(runtime_context, "get_adapter", side_effect=AssertionError("adapter should not be used")):
        assert runtime_context.get_quaid_home() == hidden.resolve()
        assert runtime_context.get_visible_quaid_home() == visible.resolve()


def test_runtime_context_uses_env_instance_roots_without_adapter(monkeypatch, tmp_path):
    from lib import runtime_context

    hidden = tmp_path / ".quaid"
    visible = tmp_path / "quaid"
    monkeypatch.setenv("QUAID_HOME", str(hidden))
    monkeypatch.delenv("QUAID_VISIBLE_HOME", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "alpha")

    with patch.object(runtime_context, "get_adapter", side_effect=AssertionError("adapter should not be used")):
        assert runtime_context.get_workspace_dir() == (hidden / "instances" / "alpha").resolve()
        assert runtime_context.get_visible_workspace_dir() == (visible / "instances" / "alpha").resolve()
