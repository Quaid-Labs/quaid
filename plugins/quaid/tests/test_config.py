"""Tests for config.py — camelCase conversion, dataclasses, config loading."""

import os
import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env to avoid touching real DB
os.environ.setdefault("MEMORY_DB_PATH", ":memory:")

import pytest

from config import (
    _camel_to_snake,
    _load_nested,
    ModelConfig,
    MemoryConfig,
    load_config,
    get_config,
    reload_config,
    resolve_model,
    _config_paths,
)


# ---------------------------------------------------------------------------
# _camel_to_snake
# ---------------------------------------------------------------------------

class TestCamelToSnake:
    """Tests for _camel_to_snake() conversion."""

    def test_basic_camel_case(self):
        assert _camel_to_snake("batchBudgetPercent") == "batch_budget_percent"

    def test_fast_reasoning(self):
        assert _camel_to_snake("fastReasoning") == "fast_reasoning"

    def test_already_snake(self):
        assert _camel_to_snake("already_snake") == "already_snake"

    def test_all_caps(self):
        assert _camel_to_snake("ABC") == "a_b_c"

    def test_single_word(self):
        assert _camel_to_snake("model") == "model"

    def test_leading_lowercase(self):
        assert _camel_to_snake("maxTokens") == "max_tokens"

    def test_consecutive_caps(self):
        assert _camel_to_snake("HTMLParser") == "h_t_m_l_parser"

    def test_empty_string(self):
        assert _camel_to_snake("") == ""

    def test_single_char(self):
        assert _camel_to_snake("a") == "a"

    def test_numbers_unchanged(self):
        assert _camel_to_snake("model4Name") == "model4_name"


# ---------------------------------------------------------------------------
# _load_nested
# ---------------------------------------------------------------------------

class TestLoadNested:
    """Tests for _load_nested() recursive key conversion."""

    def test_simple_dict(self):
        result = _load_nested({"camelCase": "value"})
        assert result == {"camel_case": "value"}

    def test_nested_dict(self):
        result = _load_nested({"outer": {"innerKey": "value"}})
        assert result == {"outer": {"inner_key": "value"}}

    def test_preserves_lists(self):
        result = _load_nested({"myList": [1, 2, 3]})
        assert result == {"my_list": [1, 2, 3]}

    def test_empty_dict(self):
        assert _load_nested({}) == {}


# ---------------------------------------------------------------------------
# ModelConfig
# ---------------------------------------------------------------------------

class TestModelConfig:
    """Tests for ModelConfig dataclass."""

    def test_context_window_high(self):
        cfg = ModelConfig(deep_reasoning_context=200000)
        assert cfg.context_window("deep") == 200000

    def test_context_window_low(self):
        cfg = ModelConfig(fast_reasoning_context=150000)
        assert cfg.context_window("fast") == 150000

    def test_context_window_unknown_tier_defaults_to_low(self):
        cfg = ModelConfig(fast_reasoning_context=100000)
        assert cfg.context_window("unknown") == 100000

    def test_default_values(self):
        cfg = ModelConfig()
        assert cfg.fast_reasoning == "default"
        assert cfg.deep_reasoning == "default"
        assert cfg.batch_budget_percent == 0.50

    def test_custom_values(self):
        cfg = ModelConfig(
            fast_reasoning="custom-model",
            deep_reasoning_context=500000,
            batch_budget_percent=0.30,
        )
        assert cfg.fast_reasoning == "custom-model"
        assert cfg.deep_reasoning_context == 500000
        assert cfg.batch_budget_percent == 0.30


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------

class TestResolveModel:
    """Tests for resolve_model()."""

    def test_returns_input_unchanged(self):
        assert resolve_model("claude-opus-4-6") == "claude-opus-4-6"

    def test_returns_full_id_unchanged(self):
        assert resolve_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"

    def test_returns_arbitrary_string(self):
        assert resolve_model("my-custom-model") == "my-custom-model"


# ---------------------------------------------------------------------------
# Config loading and caching
# ---------------------------------------------------------------------------

class TestConfigLoading:
    """Tests for config loading and caching."""

    def test_default_config_when_no_file(self):
        """When CONFIG_PATHS all point to nonexistent files, defaults are used."""
        import config
        old_config = config._config
        config._config = None
        try:
            with patch.object(config, "_config_paths", lambda: [Path("/nonexistent/path.json")]):
                cfg = load_config()
                assert isinstance(cfg, MemoryConfig)
                # Verify defaults
                assert cfg.decay.threshold_days == 30
                assert cfg.retrieval.default_limit == 5
        finally:
            config._config = old_config

    def test_get_config_caching(self):
        """get_config() returns the same object on repeated calls."""
        import config
        old_config = config._config
        config._config = None
        try:
            with patch.object(config, "_config_paths", lambda: [Path("/nonexistent/path.json")]):
                cfg1 = get_config()
                cfg2 = get_config()
                assert cfg1 is cfg2
        finally:
            config._config = old_config

    def test_reload_config_clears_cache(self):
        """reload_config() returns a fresh object."""
        import config
        old_config = config._config
        config._config = None
        try:
            with patch.object(config, "_config_paths", lambda: [Path("/nonexistent/path.json")]):
                cfg1 = get_config()
                cfg2 = reload_config()
                # After reload, should be a different object
                assert cfg1 is not cfg2
        finally:
            config._config = old_config

    def test_load_from_json_file(self, tmp_path):
        """Config can be loaded from a JSON file."""
        import config
        old_config = config._config
        config._config = None
        try:
            config_data = {
                "decay": {"thresholdDays": 60},
                "retrieval": {"defaultLimit": 10}
            }
            config_file = tmp_path / "memory.json"
            config_file.write_text(json.dumps(config_data))

            with patch.object(config, "_config_paths", lambda: [config_file]):
                cfg = load_config()
                assert cfg.adapter.type == "standalone"
                assert cfg.decay.threshold_days == 60
                assert cfg.retrieval.default_limit == 10
        finally:
            config._config = old_config

    def test_loads_adapter_type_from_config(self, tmp_path):
        import config
        old_config = config._config
        config._config = None
        try:
            config_file = tmp_path / "memory.json"
            config_file.write_text(json.dumps({"adapter": {"type": "openclaw"}}))
            with patch.object(config, "_config_paths", lambda: [config_file]):
                cfg = load_config()
                assert cfg.adapter.type == "openclaw"
        finally:
            config._config = old_config

    def test_invalid_json_uses_defaults(self, tmp_path):
        """Invalid JSON in config file falls back to defaults."""
        import config
        old_config = config._config
        config._config = None
        try:
            config_file = tmp_path / "memory.json"
            config_file.write_text("not valid json {{{")

            with patch.object(config, "_config_paths", lambda: [config_file]):
                cfg = load_config()
                assert isinstance(cfg, MemoryConfig)
                # Defaults should be used
                assert cfg.decay.threshold_days == 30
        finally:
            config._config = old_config
