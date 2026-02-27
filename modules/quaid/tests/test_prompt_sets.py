"""Tests for prompt set registry and config selection."""

import json
import os
import sys
from unittest.mock import patch

import pytest

# Ensure plugin root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from prompt_sets import get_prompt, list_prompt_sets, register_prompt_set, reset_registry


@pytest.fixture(autouse=True)
def _reset_prompt_registry():
    reset_registry()
    yield
    reset_registry()


def test_default_prompt_set_is_registered():
    assert "default" in list_prompt_sets()


def test_default_json_only_prompt():
    text = get_prompt("llm.json_only")
    assert text.startswith("Respond with JSON only")


def test_default_extraction_prompt_contains_expected_sections():
    text = get_prompt("ingest.extraction.system")
    assert "memory extraction system" in text.lower()
    assert '"facts"' in text


def test_custom_prompt_set_can_override_selected_keys():
    register_prompt_set(
        "developer",
        {"llm.json_only": "Respond with JSON only. developer-style."},
        source="tests",
    )
    text = get_prompt("llm.json_only", prompt_set="developer")
    assert "developer-style" in text

    # Non-overridden keys fall back to default set.
    extraction_text = get_prompt("ingest.extraction.system", prompt_set="developer")
    assert "memory extraction system" in extraction_text.lower()


def test_config_fails_fast_for_unknown_prompt_set(tmp_path):
    import config

    old_config = config._config
    config._config = None
    try:
        config_file = tmp_path / "memory.json"
        config_file.write_text(json.dumps({"promptSet": "unknown-set"}))
        with patch.object(config, "_config_paths", lambda: [config_file]):
            with pytest.raises(RuntimeError, match="Unknown prompt_set"):
                config.load_config()
    finally:
        config._config = old_config


def test_register_prompt_set_rejects_invalid_set_id():
    with pytest.raises(ValueError, match="prompt set id must match"):
        register_prompt_set("invalid space", {"llm.json_only": "ok"}, source="tests")
