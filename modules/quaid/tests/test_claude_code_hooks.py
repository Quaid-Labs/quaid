import pytest


class _FailingAdapter:
    def get_instance_name(self):
        raise RuntimeError("provision failed")


def test_auto_provision_raises_when_failhard_enabled(monkeypatch, capsys):
    from adaptors.claude_code import adapter as adapter_mod
    from adaptors.claude_code import hooks

    monkeypatch.setattr(adapter_mod, "ClaudeCodeAdapter", _FailingAdapter)
    monkeypatch.setattr(hooks, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="provision failed"):
        hooks._auto_provision_if_needed()

    assert "Auto-provision failed: provision failed" in capsys.readouterr().err


def test_auto_provision_warns_and_continues_when_failhard_disabled(monkeypatch, capsys):
    from adaptors.claude_code import adapter as adapter_mod
    from adaptors.claude_code import hooks

    monkeypatch.setattr(adapter_mod, "ClaudeCodeAdapter", _FailingAdapter)
    monkeypatch.setattr(hooks, "is_fail_hard_enabled", lambda: False)

    hooks._auto_provision_if_needed()

    assert "Auto-provision failed: provision failed" in capsys.readouterr().err
