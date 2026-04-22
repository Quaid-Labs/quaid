"""Unit tests for lib/fail_policy.py — is_fail_hard_enabled().

is_fail_hard_enabled() reads config.retrieval.fail_hard and defaults to True
when config is unavailable (fail-safe default).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _write_config(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "codex-main")
    path = tmp_path / "instances" / "codex-main" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestIsFailHardEnabledConfigPresent:
    def test_returns_true_when_fail_hard_true(self, tmp_path, monkeypatch):
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": {"failHard": True}})
        assert is_fail_hard_enabled() is True

    def test_returns_false_when_fail_hard_false(self, tmp_path, monkeypatch):
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": {"failHard": False}})
        assert is_fail_hard_enabled() is False

    def test_coerces_truthy_int_to_true(self, tmp_path, monkeypatch):
        """bool() coercion: non-zero int is truthy."""
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": {"failHard": 1}})
        assert is_fail_hard_enabled() is True

    def test_coerces_zero_to_false(self, tmp_path, monkeypatch):
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": {"failHard": 0}})
        assert is_fail_hard_enabled() is False


class TestIsFailHardEnabledConfigAbsent:
    def test_defaults_true_when_config_file_is_malformed(self, tmp_path, monkeypatch):
        """If config cannot be parsed, fail-hard defaults to True."""
        from lib.fail_policy import is_fail_hard_enabled
        path = _write_config(tmp_path, monkeypatch, {})
        path.write_text("{not-json", encoding="utf-8")
        assert is_fail_hard_enabled() is True

    def test_defaults_true_when_retrieval_is_none(self, tmp_path, monkeypatch):
        """Config with retrieval=None → default True."""
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": None})
        assert is_fail_hard_enabled() is True

    def test_defaults_true_when_retrieval_has_no_fail_hard_attr(self, tmp_path, monkeypatch):
        """Retrieval object missing fail_hard value → default True."""
        from lib.fail_policy import is_fail_hard_enabled
        _write_config(tmp_path, monkeypatch, {"retrieval": {}})
        assert is_fail_hard_enabled() is True
