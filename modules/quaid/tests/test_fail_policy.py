"""Unit tests for lib/fail_policy.py — is_fail_hard_enabled().

is_fail_hard_enabled() reads config.retrieval.fail_hard and defaults to True
when config is unavailable (fail-safe default).
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_config(fail_hard):
    """Build a minimal config object with retrieval.fail_hard set."""
    retrieval = SimpleNamespace(fail_hard=fail_hard)
    return SimpleNamespace(retrieval=retrieval)


class TestIsFailHardEnabledConfigPresent:
    def test_returns_true_when_fail_hard_true(self):
        from lib.fail_policy import is_fail_hard_enabled
        cfg = _make_config(True)
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is True

    def test_returns_false_when_fail_hard_false(self):
        from lib.fail_policy import is_fail_hard_enabled
        cfg = _make_config(False)
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is False

    def test_coerces_truthy_int_to_true(self):
        """bool() coercion: non-zero int is truthy."""
        from lib.fail_policy import is_fail_hard_enabled
        cfg = _make_config(1)
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is True

    def test_coerces_zero_to_false(self):
        from lib.fail_policy import is_fail_hard_enabled
        cfg = _make_config(0)
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is False


class TestIsFailHardEnabledConfigAbsent:
    def test_defaults_true_when_get_config_raises(self):
        """If get_config() raises any exception, fail-hard defaults to True."""
        from lib.fail_policy import is_fail_hard_enabled
        with patch("config.get_config", side_effect=RuntimeError("no config")):
            assert is_fail_hard_enabled() is True

    def test_defaults_true_when_retrieval_is_none(self):
        """Config object with retrieval=None → default True."""
        from lib.fail_policy import is_fail_hard_enabled
        cfg = SimpleNamespace(retrieval=None)
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is True

    def test_defaults_true_when_retrieval_has_no_fail_hard_attr(self):
        """Retrieval object missing fail_hard attribute → default True."""
        from lib.fail_policy import is_fail_hard_enabled
        cfg = SimpleNamespace(retrieval=SimpleNamespace())  # no fail_hard
        with patch("config.get_config", return_value=cfg):
            assert is_fail_hard_enabled() is True

    def test_defaults_true_on_import_error(self):
        """If the config module can't be imported, fail-hard defaults to True."""
        import builtins
        real_import = builtins.__import__

        def _block_config(name, *args, **kwargs):
            if name == "config":
                raise ImportError("no module named config")
            return real_import(name, *args, **kwargs)

        from lib.fail_policy import is_fail_hard_enabled
        with patch("builtins.__import__", side_effect=_block_config):
            assert is_fail_hard_enabled() is True
