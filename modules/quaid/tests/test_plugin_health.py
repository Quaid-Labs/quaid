import json
from types import SimpleNamespace

from core.runtime import plugin_health


def _plugins_cfg(enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        api_version=1,
        paths=["modules/quaid", "plugins"],
        allowlist=["quaid.openclaw"],
        strict=False,
        config={},
        slots=SimpleNamespace(
            adapter="quaid.openclaw",
            ingest=["quaid.extract"],
            datastores=["quaid.memorydb"],
        ),
    )


def test_collect_plugin_health_disabled(monkeypatch):
    monkeypatch.setattr("config.get_config", lambda: SimpleNamespace(plugins=_plugins_cfg(enabled=False)))
    assert plugin_health.collect_plugin_health() == {"enabled": False, "plugins": {}}


def test_collect_plugin_health_enabled_smoke(monkeypatch):
    fake_cfg = SimpleNamespace(plugins=_plugins_cfg(enabled=True))
    monkeypatch.setattr("config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: object())

    def _collect(**kwargs):
        if kwargs.get("surface") == "health":
            return ([], ["warn"], [("quaid.memorydb", {"ok": True})])
        if kwargs.get("surface") == "dashboard":
            return ([], [], [("quaid.memorydb", {"widgets": 1})])
        raise AssertionError(f"unexpected surface: {kwargs.get('surface')}")

    monkeypatch.setattr(
        "core.runtime.plugins.run_plugin_contract_surface_collect",
        _collect,
    )

    payload = plugin_health.collect_plugin_health()
    assert payload["enabled"] is True
    assert payload["warnings"] == ["warn"]
    assert payload["plugins"] == {"quaid.memorydb": {"ok": True}}
    assert payload["dashboard"] == {"quaid.memorydb": {"widgets": 1}}


def test_collect_plugin_health_includes_initialize_diagnostics(monkeypatch, tmp_path):
    fake_cfg = SimpleNamespace(plugins=_plugins_cfg(enabled=True))
    monkeypatch.setattr("config.get_config", lambda: fake_cfg)
    monkeypatch.setattr("lib.runtime_context.get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr("core.runtime.plugins.get_runtime_registry", lambda: None)
    monkeypatch.setattr(
        "core.runtime.plugins.initialize_plugin_runtime",
        lambda **kwargs: (object(), ["init-error"], ["init-warn"]),
    )
    monkeypatch.setattr(
        "core.runtime.plugins.run_plugin_contract_surface_collect",
        lambda **kwargs: (
            (["health-error"], ["health-warn"], [])
            if kwargs.get("surface") == "health"
            else (["dash-error"], ["dash-warn"], [])
        ),
    )

    payload = plugin_health.collect_plugin_health()
    assert payload["errors"] == ["init-error", "health-error", "dash-error"]
    assert payload["warnings"] == ["init-warn", "health-warn", "dash-warn"]


def test_plugin_health_main_outputs_json(monkeypatch, capsys):
    monkeypatch.setattr(plugin_health, "collect_plugin_health", lambda: {"enabled": True, "plugins": {"x": {"ok": True}}})
    assert plugin_health.main() == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["enabled"] is True
    assert "plugins" in parsed
