from pathlib import Path

import config_cli


def test_parse_literal_handles_bool_number_json_and_string():
    assert config_cli.parse_literal("true") is True
    assert config_cli.parse_literal("false") is False
    assert config_cli.parse_literal("12") == 12
    assert config_cli.parse_literal("3.5") == 3.5
    assert config_cli.parse_literal('{"k":1}') == {"k": 1}
    assert config_cli.parse_literal("keep-as-string") == "keep-as-string"


def test_interactive_edit_updates_failhard_and_parallel_settings(monkeypatch, tmp_path):
    path = tmp_path / "config" / "memory.json"
    data = {
        "retrieval": {"failHard": True},
        "janitor": {"parallel": {"enabled": False, "llmWorkers": 4, "lifecyclePrepassWorkers": 3}},
    }
    answers = iter(
        [
            "7", "false",  # failHard -> false
            "8", "true",   # parallel enabled -> true
            "9", "6",      # llmWorkers -> 6
            "10", "5",     # prepass workers -> 5
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
    assert saved["data"]["janitor"]["parallel"]["enabled"] is True
    assert saved["data"]["janitor"]["parallel"]["llmWorkers"] == 6
    assert saved["data"]["janitor"]["parallel"]["lifecyclePrepassWorkers"] == 5


def test_set_builds_nested_paths():
    data = {}
    config_cli._set(data, "janitor.parallel.enabled", True)
    config_cli._set(data, "retrieval.failHard", False)

    assert data["janitor"]["parallel"]["enabled"] is True
    assert data["retrieval"]["failHard"] is False
