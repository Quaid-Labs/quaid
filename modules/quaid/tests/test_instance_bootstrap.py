"""Regression tests for sparse instance bootstrap config writes."""

import json

import pytest


@pytest.fixture(autouse=True)
def _reset_adapter_state():
    from lib import adapter as adapter_mod

    adapter_mod.reset_adapter()
    yield
    adapter_mod.reset_adapter()


def test_auto_provision_writes_lean_instance_config(tmp_path, monkeypatch):
    from lib import adapter as adapter_mod

    instance_id = "codex-bootstrap-lean"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", instance_id)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("CODEX_PROJECT_DIR", raising=False)
    monkeypatch.setenv("QUAID_ADAPTER_TYPE", "codex")

    adapter_mod.reset_adapter()
    adapter_mod._auto_provision_from_env_if_needed()

    config_path = tmp_path / "instances" / instance_id / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(config.keys()) == {"instance", "adapter"}
    assert config["instance"]["id"] == instance_id
    assert config["adapter"]["type"] == "codex"

    banned_sections = {
        "capture",
        "models",
        "retrieval",
        "systems",
        "janitor",
        "logging",
        "docs",
        "projects",
        "users",
        "database",
        "rag",
        "decay",
        "notifications",
    }
    assert banned_sections.isdisjoint(config.keys())


def test_get_adapter_reads_type_from_lean_instance_config(tmp_path, monkeypatch):
    from adaptors.codex.adapter import CodexAdapter
    from lib import adapter as adapter_mod

    instance_id = "codex-bootstrap-lean"
    config_path = tmp_path / "instances" / instance_id / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"instance": {"id": instance_id}, "adapter": {"type": "codex"}}),
        encoding="utf-8",
    )

    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", instance_id)

    adapter_mod.reset_adapter()
    monkeypatch.setattr(adapter_mod, "_ensure_instance_projects_bootstrapped", lambda _adapter: None)
    adapter = adapter_mod.get_adapter()
    assert isinstance(adapter, CodexAdapter)
