import sys
from types import SimpleNamespace
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


def test_fail_policy_logs_when_config_load_fails(caplog):
    from lib.fail_policy import is_fail_hard_enabled

    fake_config_mod = SimpleNamespace()
    fake_config_mod.get_config = MagicMock(side_effect=RuntimeError("config broken"))

    with patch.dict(sys.modules, {"config": fake_config_mod}):
        with caplog.at_level("WARNING"):
            assert is_fail_hard_enabled() is True

    assert any("defaulting to enabled" in rec.message for rec in caplog.records)
