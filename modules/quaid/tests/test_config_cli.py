from pathlib import Path
import json

import config_cli


def test_parse_literal_handles_bool_number_json_and_string():
    assert config_cli.parse_literal("true") is True
    assert config_cli.parse_literal("false") is False
    assert config_cli.parse_literal("12") == 12
    assert config_cli.parse_literal("3.5") == 3.5
    assert config_cli.parse_literal('{"k":1}') == {"k": 1}
    assert config_cli.parse_literal("keep-as-string") == "keep-as-string"
    assert config_cli.parse_literal("null") is None
    assert config_cli.parse_literal("gpt-5.1") == "gpt-5.1"


def test_interactive_edit_updates_failhard_and_parallel_settings(monkeypatch, tmp_path):
    path = tmp_path / "config" / "memory.json"
    data = {
        "retrieval": {"failHard": True},
        "core": {"parallel": {"enabled": False, "llmWorkers": 4, "lifecyclePrepassWorkers": 3}},
    }
    answers = iter(
        [
            "7", "false",  # failHard -> false
            "8", "true",   # parallel enabled -> true
            "9", "6",      # llmWorkers -> 6
            "13",          # save and exit
        ]
    )

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    saved = {}

    def _capture_save(save_path, save_data):
        saved["path"] = save_path
        saved["data"] = save_data

    monkeypatch.setattr(config_cli, "_save_config", _capture_save)
    changed = config_cli.interactive_edit(path, data)

    assert changed is True
    assert saved["path"] == path
    assert saved["data"]["retrieval"]["failHard"] is False
    assert saved["data"]["core"]["parallel"]["enabled"] is True
    assert saved["data"]["core"]["parallel"]["llmWorkers"] == 6
    assert saved["data"]["core"]["parallel"]["lifecyclePrepassWorkers"] == 3


def test_set_builds_nested_paths():
    data = {}
    config_cli._set(data, "core.parallel.enabled", True)
    config_cli._set(data, "retrieval.failHard", False)

    assert data["core"]["parallel"]["enabled"] is True
    assert data["retrieval"]["failHard"] is False


def test_set_supports_plugin_config_dotted_plugin_id_as_atomic_key():
    data = {"plugins": {"config": {"memorydb.core": {"domains": {"personal": "Personal"}}}}}

    config_cli._set(data, ["plugins", "config", "memorydb.core"], {"domains": {"technical": "Technical"}})

    assert "memorydb.core" in data["plugins"]["config"]
    assert "memorydb" not in data["plugins"]["config"]
    assert data["plugins"]["config"]["memorydb.core"]["domains"]["technical"] == "Technical"


def test_get_supports_explicit_segments_for_dotted_plugin_id():
    data = {"plugins": {"config": {"memorydb.core": {"enabled": True}}}}
    out = config_cli._get(data, ["plugins", "config", "memorydb.core"], {})
    assert out == {"enabled": True}


def test_interactive_edit_writes_embedding_and_timeout_to_canonical_keys(monkeypatch, tmp_path):
    path = tmp_path / "config" / "memory.json"
    data = {
        "ollama": {"embeddingModel": "nomic-embed-text"},
        "capture": {"inactivity_timeout_minutes": 120},
    }
    answers = iter(
        [
            "4", "qwen3-embedding:8b",
            "6", "60",
            "13",
        ]
    )

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    saved = {}

    def _capture_save(save_path, save_data):
        saved["path"] = save_path
        saved["data"] = save_data

    monkeypatch.setattr(config_cli, "_save_config", _capture_save)
    changed = config_cli.interactive_edit(path, data)

    assert changed is True
    assert saved["path"] == path
    assert saved["data"]["ollama"]["embeddingModel"] == "qwen3-embedding:8b"
    assert saved["data"]["capture"]["inactivity_timeout_minutes"] == 60


def test_interactive_edit_updates_existing_snake_case_model_keys(monkeypatch, tmp_path):
    path = tmp_path / "config" / "memory.json"
    data = {"models": {"llm_provider": "anthropic"}}
    answers = iter(["1", "openai-compatible", "13"])

    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(config_cli, "_run_config_callbacks_after_save", lambda: None)
    saved = {}

    def _capture_save(save_path, save_data):
        saved["path"] = save_path
        saved["data"] = save_data

    monkeypatch.setattr(config_cli, "_save_config", _capture_save)
    changed = config_cli.interactive_edit(path, data)

    assert changed is True
    assert saved["path"] == path
    assert saved["data"]["models"]["llm_provider"] == "openai-compatible"
    assert "llmProvider" not in saved["data"]["models"]


def test_plugin_schema_edit_keeps_prior_values_on_invalid_field(monkeypatch, tmp_path):
    staged = {"plugins": {"config": {"memorydb.core": {"good": 1}}}}
    schema = {
        "fields": [
            {"key": "good", "type": "integer"},
            {"key": "bad", "type": "integer"},
        ]
    }
    answers = iter(["2", "not-an-int"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    config_cli._edit_plugin_config_schema(staged, tmp_path / "memory.json", "memorydb.core", schema)

    assert staged["plugins"]["config"]["memorydb.core"]["good"] == 2
    assert "bad" not in staged["plugins"]["config"]["memorydb.core"]


def test_main_set_returns_nonzero_when_callback_reload_fails(monkeypatch, tmp_path):
    path = tmp_path / "config" / "memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(config_cli, "_config_path", lambda: path)
    monkeypatch.setattr(config_cli, "_run_config_callbacks_after_save", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("sys.argv", ["config_cli.py", "set", "models.llm_provider", "anthropic"])

    assert config_cli.main() == 1
