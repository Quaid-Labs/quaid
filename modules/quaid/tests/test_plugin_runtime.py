from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.runtime.plugins import (
    PluginRegistry,
    collect_declared_exports,
    discover_plugin_manifests,
    get_runtime_errors,
    get_runtime_warnings,
    initialize_plugin_runtime,
    reset_plugin_runtime,
    run_plugin_contract_surface_collect,
    run_plugin_contract_surface,
    validate_manifest_dict,
)


def _contract_caps(display_name: str) -> dict:
    return {
        "display_name": display_name,
        "contract": {
            "init": {"mode": "hook"},
            "config": {"mode": "hook"},
            "status": {"mode": "hook"},
            "dashboard": {"mode": "tbd"},
            "maintenance": {"mode": "hook"},
            "tool_runtime": {"mode": "hook"},
            "health": {"mode": "hook"},
            "tools": {"mode": "declared", "exports": []},
            "api": {"mode": "declared", "exports": []},
            "events": {"mode": "declared", "exports": []},
            "ingest_triggers": {"mode": "declared", "exports": []},
            "auth_requirements": {"mode": "declared", "exports": []},
            "migrations": {"mode": "declared", "exports": []},
            "notifications": {"mode": "declared", "exports": []},
        },
    }


def _datastore_caps(display_name: str) -> dict:
    caps = _contract_caps(display_name)
    caps.update(
        {
            "supports_multi_user": True,
            "supports_policy_metadata": True,
            "supports_redaction": True,
        }
    )
    return caps


def test_validate_manifest_happy_path():
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "openclaw.adapter",
            "plugin_type": "adapter",
            "module": "adaptors.openclaw.adapter",
            "entrypoint": "register",
            "capabilities": _contract_caps("OpenClaw Adapter"),
        }
    )
    assert manifest.plugin_id == "openclaw.adapter"
    assert manifest.plugin_type == "adapter"
    assert manifest.module == "adaptors.openclaw.adapter"
    assert manifest.display_name == "OpenClaw Adapter"


def test_validate_manifest_rejects_invalid_type():
    with pytest.raises(ValueError):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "bad.type",
                "plugin_type": "unknown",
                "module": "x.y",
            }
        )


def test_validate_manifest_rejects_non_boolean_enabled():
    with pytest.raises(ValueError, match="enabled must be a boolean"):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.bad-enabled",
                "plugin_type": "adapter",
                "module": "adaptors.bad_enabled",
                "enabled": None,
                "capabilities": _contract_caps("Bad Enabled"),
            }
        )


def test_validate_manifest_preserves_zero_priority():
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.zero-priority",
            "plugin_type": "adapter",
            "module": "adaptors.zero",
            "priority": 0,
            "capabilities": _contract_caps("Zero Priority Adapter"),
        }
    )
    assert manifest.priority == 0


def test_validate_manifest_rejects_invalid_executable_mode():
    payload = {
        "plugin_api_version": 1,
        "plugin_id": "openclaw.adapter",
        "plugin_type": "adapter",
        "module": "adaptors.openclaw.adapter",
        "capabilities": _contract_caps("OpenClaw Adapter"),
    }
    payload["capabilities"]["contract"]["init"]["mode"] = "potato"
    with pytest.raises(ValueError, match="contract.init.mode"):
        validate_manifest_dict(payload)


def test_validate_manifest_rejects_unapproved_module_namespace():
    with pytest.raises(ValueError, match="Invalid manifest module"):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "bad.module",
                "plugin_type": "adapter",
                "module": "os",
                "capabilities": _contract_caps("Bad Module"),
            }
        )


def test_registry_rejects_plugin_id_conflict():
    registry = PluginRegistry(api_version=1)
    first = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "quaid.memorydb",
            "plugin_type": "datastore",
            "module": "datastore.memorydb",
            "capabilities": _datastore_caps("MemoryDB"),
        }
    )
    second = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "quaid.memorydb",
            "plugin_type": "datastore",
            "module": "datastore.memorydb.v2",
            "capabilities": _datastore_caps("MemoryDB v2"),
        }
    )
    registry.register(first)
    with pytest.raises(ValueError):
        registry.register(second)


def test_registry_singleton_slot_conflict():
    registry = PluginRegistry(api_version=1)
    adapter_a = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.a",
            "plugin_type": "adapter",
            "module": "adaptors.a",
            "capabilities": _contract_caps("Adapter A"),
        }
    )
    adapter_b = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.b",
            "plugin_type": "adapter",
            "module": "adaptors.b",
            "capabilities": _contract_caps("Adapter B"),
        }
    )
    registry.register(adapter_a)
    registry.register(adapter_b)
    registry.activate_singleton("adapter", "adapter.a")
    with pytest.raises(ValueError):
        registry.activate_singleton("adapter", "adapter.b")


def test_discover_plugin_manifests_with_allowlist(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    plugin_a = plugins_dir / "adapter-a"
    plugin_b = plugins_dir / "adapter-b"
    plugin_a.mkdir(parents=True)
    plugin_b.mkdir(parents=True)
    (plugin_a / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.a",
                "plugin_type": "adapter",
                "module": "adaptors.a",
                "capabilities": _contract_caps("Adapter A"),
            }
        ),
        encoding="utf-8",
    )
    (plugin_b / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.b",
                "plugin_type": "adapter",
                "module": "adaptors.b",
                "capabilities": _contract_caps("Adapter B"),
            }
        ),
        encoding="utf-8",
    )
    manifests, errors = discover_plugin_manifests(
        paths=[str(plugins_dir)],
        allowlist=["adapter.b"],
        strict=True,
    )
    assert not errors
    assert [m.plugin_id for m in manifests] == ["adapter.b"]


def test_discover_plugin_manifests_non_strict_collects_all_errors(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    good_dir = plugins_dir / "good"
    bad_json_dir = plugins_dir / "bad-json"
    bad_manifest_dir = plugins_dir / "bad-manifest"
    good_dir.mkdir(parents=True)
    bad_json_dir.mkdir(parents=True)
    bad_manifest_dir.mkdir(parents=True)
    (good_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.good",
                "plugin_type": "adapter",
                "module": "adaptors.good",
                "capabilities": _contract_caps("Good Adapter"),
            }
        ),
        encoding="utf-8",
    )
    (bad_json_dir / "plugin.json").write_text("{broken", encoding="utf-8")
    (bad_manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "bad.type",
                "plugin_type": "nope",
                "module": "adaptors.bad",
            }
        ),
        encoding="utf-8",
    )
    manifests, errors = discover_plugin_manifests(
        paths=[str(plugins_dir)],
        strict=False,
    )
    assert [m.plugin_id for m in manifests] == ["adapter.good"]
    assert len(errors) == 2
    assert any("bad-json" in err for err in errors)
    assert any("bad-manifest" in err for err in errors)


def test_discover_plugin_manifests_strict_aggregates_errors(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    bad_json_dir = plugins_dir / "bad-json"
    bad_manifest_dir = plugins_dir / "bad-manifest"
    bad_json_dir.mkdir(parents=True)
    bad_manifest_dir.mkdir(parents=True)
    (bad_json_dir / "plugin.json").write_text("{broken", encoding="utf-8")
    (bad_manifest_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "bad.type",
                "plugin_type": "nope",
                "module": "adaptors.bad",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Plugin manifest discovery failed"):
        discover_plugin_manifests(paths=[str(plugins_dir)], strict=True)


def test_validate_manifest_requires_datastore_capabilities():
    with pytest.raises(ValueError, match="missing required capabilities"):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "quaid.memorydb",
                "plugin_type": "datastore",
                "module": "datastore.memorydb",
                "capabilities": {
                    **_contract_caps("MemoryDB"),
                    "supports_multi_user": True,
                },
            }
        )


def test_initialize_plugin_runtime_strict_rejects_slot_type_mismatch(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    adapter_dir = plugins_dir / "adapter-a"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.a",
                "plugin_type": "adapter",
                "module": "adaptors.a",
                "capabilities": _contract_caps("Adapter A"),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected type 'datastore'"):
        initialize_plugin_runtime(
            api_version=1,
            paths=[str(plugins_dir)],
            strict=True,
            slots={"datastores": ["adapter.a"]},
            workspace_root=str(tmp_path),
        )


def test_initialize_plugin_runtime_non_strict_collects_slot_errors(tmp_path: Path):
    reset_plugin_runtime()
    plugins_dir = tmp_path / "plugins"
    adapter_dir = plugins_dir / "adapter-a"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.a",
                "plugin_type": "adapter",
                "module": "adaptors.a",
                "capabilities": _contract_caps("Adapter A"),
            }
        ),
        encoding="utf-8",
    )
    registry, errors, warnings = initialize_plugin_runtime(
        api_version=1,
        paths=[str(plugins_dir)],
        strict=False,
        slots={"datastores": ["adapter.a"], "ingest": ["missing.ingest"]},
        workspace_root=str(tmp_path),
    )
    assert not warnings
    assert registry.get("adapter.a") is not None
    assert any("expected type 'datastore'" in msg for msg in errors)
    assert any("unknown plugin_id 'missing.ingest'" in msg for msg in errors)


def test_initialize_plugin_runtime_strict_rejects_duplicate_declared_exports(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    p1 = plugins_dir / "ingest-a"
    p2 = plugins_dir / "ingest-b"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    for plugin_id, folder in (("ingest.a", p1), ("ingest.b", p2)):
        payload = {
            "plugin_api_version": 1,
            "plugin_id": plugin_id,
            "plugin_type": "ingest",
            "module": f"ingest.{plugin_id.replace('.', '_')}",
            "capabilities": _contract_caps(plugin_id),
        }
        payload["capabilities"]["contract"]["tools"]["exports"] = ["shared_tool"]
        (folder / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate declared tools export 'shared_tool'"):
        initialize_plugin_runtime(
            api_version=1,
            paths=[str(plugins_dir)],
            strict=True,
            slots={"ingest": ["ingest.a", "ingest.b"]},
            workspace_root=str(tmp_path),
        )


def test_initialize_plugin_runtime_non_strict_collects_duplicate_declared_exports(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    p1 = plugins_dir / "ingest-a"
    p2 = plugins_dir / "ingest-b"
    p1.mkdir(parents=True)
    p2.mkdir(parents=True)
    for plugin_id, folder in (("ingest.a", p1), ("ingest.b", p2)):
        payload = {
            "plugin_api_version": 1,
            "plugin_id": plugin_id,
            "plugin_type": "ingest",
            "module": f"ingest.{plugin_id.replace('.', '_')}",
            "capabilities": _contract_caps(plugin_id),
        }
        payload["capabilities"]["contract"]["tools"]["exports"] = ["shared_tool"]
        (folder / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    _registry, errors, _warnings = initialize_plugin_runtime(
        api_version=1,
        paths=[str(plugins_dir)],
        strict=False,
        slots={"ingest": ["ingest.a", "ingest.b"]},
        workspace_root=str(tmp_path),
    )
    assert any("Duplicate declared tools export 'shared_tool'" in msg for msg in errors)


def test_initialize_plugin_runtime_strict_rejects_unknown_dependency(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    ingest_dir = plugins_dir / "ingest"
    ingest_dir.mkdir(parents=True)
    payload = {
        "plugin_api_version": 1,
        "plugin_id": "ingest.dep",
        "plugin_type": "ingest",
        "module": "ingest.dep_mod",
        "dependencies": ["datastore.missing"],
        "capabilities": _contract_caps("Ingest Dep"),
    }
    (ingest_dir / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="depends on unknown plugin_id 'datastore.missing'"):
        initialize_plugin_runtime(
            api_version=1,
            paths=[str(plugins_dir)],
            strict=True,
            slots={"ingest": ["ingest.dep"]},
            workspace_root=str(tmp_path),
        )


def test_initialize_plugin_runtime_non_strict_collects_inactive_dependency(tmp_path: Path):
    plugins_dir = tmp_path / "plugins"
    ingest_dir = plugins_dir / "ingest"
    ds_dir = plugins_dir / "datastore"
    ingest_dir.mkdir(parents=True)
    ds_dir.mkdir(parents=True)
    ingest_payload = {
        "plugin_api_version": 1,
        "plugin_id": "ingest.dep",
        "plugin_type": "ingest",
        "module": "ingest.dep_mod",
        "dependencies": ["datastore.core"],
        "capabilities": _contract_caps("Ingest Dep"),
    }
    ds_payload = {
        "plugin_api_version": 1,
        "plugin_id": "datastore.core",
        "plugin_type": "datastore",
        "module": "datastore.core_mod",
        "capabilities": _datastore_caps("Datastore Core"),
    }
    (ingest_dir / "plugin.json").write_text(json.dumps(ingest_payload), encoding="utf-8")
    (ds_dir / "plugin.json").write_text(json.dumps(ds_payload), encoding="utf-8")
    _registry, errors, _warnings = initialize_plugin_runtime(
        api_version=1,
        paths=[str(plugins_dir)],
        strict=False,
        slots={"ingest": ["ingest.dep"]},
        workspace_root=str(tmp_path),
    )
    assert any("depends on inactive plugin_id 'datastore.core'" in msg for msg in errors)


def test_runtime_diagnostics_accessors_reflect_latest_initialize(tmp_path: Path):
    reset_plugin_runtime()
    plugins_dir = tmp_path / "plugins"
    adapter_dir = plugins_dir / "adapter-a"
    adapter_dir.mkdir(parents=True)
    (adapter_dir / "plugin.json").write_text(
        json.dumps(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.a",
                "plugin_type": "adapter",
                "module": "adaptors.a",
                "capabilities": _contract_caps("Adapter A"),
            }
        ),
        encoding="utf-8",
    )
    initialize_plugin_runtime(
        api_version=1,
        paths=[str(plugins_dir)],
        strict=False,
        slots={"datastores": ["adapter.a"]},
        workspace_root=str(tmp_path),
    )
    assert any("expected type 'datastore'" in msg for msg in get_runtime_errors())
    assert get_runtime_warnings() == []
    reset_plugin_runtime()
    assert get_runtime_errors() == []
    assert get_runtime_warnings() == []


def test_registry_register_is_thread_safe():
    registry = PluginRegistry(api_version=1)

    def _register(i: int) -> None:
        registry.register(
            validate_manifest_dict(
                {
                    "plugin_api_version": 1,
                    "plugin_id": f"adapter.{i}",
                    "plugin_type": "adapter",
                    "module": f"adaptors.a{i}",
                    "capabilities": _contract_caps(f"Adapter {i}"),
                }
            )
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(_register, range(100)))

    assert len(registry.list("adapter")) == 100


def test_run_plugin_contract_surface_executes_config_hook(tmp_path: Path):
    pkg = tmp_path / "hookpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "from core.runtime.plugins import PluginHookContext\n"
        "CALLED = []\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_config(self, ctx: PluginHookContext) -> None:\n"
        "        CALLED.append((ctx.plugin.plugin_id, sorted((ctx.plugin_config or {}).keys())))\n"
        "    def on_status(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_dashboard(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_maintenance(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"ready\": True}\n"
        "    def on_health(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_config(ctx):\n"
        "    _CONTRACT.on_config(ctx)\n",
        encoding="utf-8",
    )

    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.hook",
            "plugin_type": "adapter",
            "module": "hookpkg.impl",
            "capabilities": {
                "display_name": "Hook Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook", "handler": "on_config"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)

    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns = run_plugin_contract_surface(
            registry=registry,
            slots={"adapter": "adapter.hook"},
            surface="config",
            config={},
            plugin_config={"adapter.hook": {"a": 1, "b": 2}},
            workspace_root=str(tmp_path),
            strict=True,
        )
        assert errs == []
        assert warns == []
        mod = __import__("hookpkg.impl", fromlist=["CALLED"])
        assert mod.CALLED == [("adapter.hook", ["a", "b"])]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_collect_orders_by_manifest_priority(tmp_path: Path):
    pkg = tmp_path / "prioritypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx): return None\n"
        "    def on_config(self, ctx): return {\"plugin\": ctx.plugin.plugin_id}\n"
        "    def on_status(self, ctx): return {}\n"
        "    def on_dashboard(self, ctx): return {}\n"
        "    def on_maintenance(self, ctx): return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx): return {\"ready\": True}\n"
        "    def on_health(self, ctx): return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_config(ctx):\n"
        "    return _CONTRACT.on_config(ctx)\n",
        encoding="utf-8",
    )

    ingest_manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "core.ingest_high",
            "plugin_type": "ingest",
            "module": "prioritypkg.impl",
            "priority": 99,
            "capabilities": {
                **_contract_caps("Ingest High"),
                "contract": {
                    **_contract_caps("Ingest High")["contract"],
                    "config": {"mode": "hook", "handler": "on_config"},
                },
            },
        }
    )
    datastore_manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "memorydb.low",
            "plugin_type": "datastore",
            "module": "prioritypkg.impl",
            "priority": 1,
            "capabilities": {
                **_datastore_caps("Datastore Low"),
                "contract": {
                    **_datastore_caps("Datastore Low")["contract"],
                    "config": {"mode": "hook", "handler": "on_config"},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(ingest_manifest)
    registry.register(datastore_manifest)

    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns, results = run_plugin_contract_surface_collect(
            registry=registry,
            slots={
                "ingest": ["core.ingest_high"],
                "datastores": ["memorydb.low"],
            },
            surface="config",
            config={},
            plugin_config={},
            workspace_root=str(tmp_path),
            strict=True,
        )
        assert errs == []
        assert warns == []
        assert [plugin_id for plugin_id, _ in results] == ["memorydb.low", "core.ingest_high"]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_collect_strict_stops_after_first_hook_failure(tmp_path: Path):
    pkg = tmp_path / "failfastpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "CALLS = []\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx): return None\n"
        "    def on_config(self, ctx):\n"
        "        CALLS.append(ctx.plugin.plugin_id)\n"
        "        if ctx.plugin.plugin_id == 'ingest.fail':\n"
        "            raise RuntimeError('boom')\n"
        "        return {'ok': True}\n"
        "    def on_status(self, ctx): return {}\n"
        "    def on_dashboard(self, ctx): return {}\n"
        "    def on_maintenance(self, ctx): return {'handled': False}\n"
        "    def on_tool_runtime(self, ctx): return {'ready': True}\n"
        "    def on_health(self, ctx): return {'healthy': True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_config(ctx):\n"
        "    return _CONTRACT.on_config(ctx)\n",
        encoding="utf-8",
    )

    fail_manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "ingest.fail",
            "plugin_type": "ingest",
            "module": "failfastpkg.impl",
            "priority": 1,
            "capabilities": {
                **_contract_caps("Fail Ingest"),
                "contract": {
                    **_contract_caps("Fail Ingest")["contract"],
                    "config": {"mode": "hook", "handler": "on_config"},
                },
            },
        }
    )
    later_manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "ingest.later",
            "plugin_type": "ingest",
            "module": "failfastpkg.impl",
            "priority": 2,
            "capabilities": {
                **_contract_caps("Later Ingest"),
                "contract": {
                    **_contract_caps("Later Ingest")["contract"],
                    "config": {"mode": "hook", "handler": "on_config"},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(fail_manifest)
    registry.register(later_manifest)

    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns, results = run_plugin_contract_surface_collect(
            registry=registry,
            slots={"ingest": ["ingest.fail", "ingest.later"]},
            surface="config",
            config={},
            plugin_config={},
            workspace_root=str(tmp_path),
            strict=True,
        )
        assert len(errs) == 1
        assert "ingest.fail" in errs[0]
        assert warns == []
        assert results == []
        mod = __import__("failfastpkg.impl", fromlist=["CALLS"])
        assert mod.CALLS == ["ingest.fail"]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_rejects_missing_base_contract(tmp_path: Path):
    pkg = tmp_path / "badpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "def on_config(ctx):\n"
        "    return None\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.bad",
            "plugin_type": "adapter",
            "module": "badpkg.impl",
            "capabilities": {
                "display_name": "Bad Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook", "handler": "on_config"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns = run_plugin_contract_surface(
            registry=registry,
            slots={"adapter": "adapter.bad"},
            surface="config",
            config={},
            plugin_config={"adapter.bad": {}},
            workspace_root=str(tmp_path),
            strict=True,
        )
        assert warns == []
        assert len(errs) == 1
        assert "_CONTRACT implementing PluginContractBase" in errs[0]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_accepts_cross_module_handler_contract(tmp_path: Path):
    pkg = tmp_path / "crosspkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "def noop():\n"
        "    return None\n",
        encoding="utf-8",
    )
    (pkg / "handlers.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx): return None\n"
        "    def on_config(self, ctx): return {\"ok\": True}\n"
        "    def on_status(self, ctx): return {}\n"
        "    def on_dashboard(self, ctx): return {}\n"
        "    def on_maintenance(self, ctx): return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx): return {\"ready\": True}\n"
        "    def on_health(self, ctx): return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_config(ctx):\n"
        "    return _CONTRACT.on_config(ctx)\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.cross",
            "plugin_type": "adapter",
            "module": "crosspkg.impl",
            "capabilities": {
                "display_name": "Cross Module Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook", "handler": "crosspkg.handlers:on_config"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns = run_plugin_contract_surface(
            registry=registry,
            slots={"adapter": "adapter.cross"},
            surface="config",
            config={},
            plugin_config={"adapter.cross": {}},
            workspace_root=str(tmp_path),
            strict=True,
        )
        assert errs == []
        assert warns == []
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_active_slot_plugins_have_executable_init_and_config_hooks(tmp_path: Path):
    plugin_root = Path(__file__).resolve().parents[1]
    allow = ["memorydb.core", "core.extract", "openclaw.adapter"]
    manifests, errors = discover_plugin_manifests(
        paths=[str(plugin_root)],
        allowlist=allow,
        strict=True,
        workspace_root=plugin_root,
    )
    assert errors == []
    got_ids = sorted([m.plugin_id for m in manifests])
    assert got_ids == sorted(allow)

    registry = PluginRegistry(api_version=1)
    for m in manifests:
        registry.register(m)

    db_path = tmp_path / "memory.db"
    cfg = SimpleNamespace(
        database=SimpleNamespace(path=str(db_path)),
        retrieval=SimpleNamespace(domains={"technical": "code"}),
        adapter=SimpleNamespace(type="openclaw"),
    )
    slots = {
        "adapter": "openclaw.adapter",
        "ingest": ["core.extract"],
        "datastores": ["memorydb.core"],
    }
    plugin_config = {
        "memorydb.core": {},
        "core.extract": {"enabled": True},
        "openclaw.adapter": {},
    }
    init_errs, init_warns = run_plugin_contract_surface(
        registry=registry,
        slots=slots,
        surface="init",
        config=cfg,
        plugin_config=plugin_config,
        workspace_root=str(tmp_path),
        strict=True,
    )
    cfg_errs, cfg_warns = run_plugin_contract_surface(
        registry=registry,
        slots=slots,
        surface="config",
        config=cfg,
        plugin_config=plugin_config,
        workspace_root=str(tmp_path),
        strict=True,
    )
    assert init_errs == []
    assert init_warns == []
    assert cfg_errs == []
    assert cfg_warns == []


def test_run_plugin_contract_surface_collect_maintenance_returns_results(tmp_path: Path):
    pkg = tmp_path / "maintpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "from core.runtime.plugins import PluginHookContext\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_config(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_status(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_dashboard(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_maintenance(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"handled\": True, \"metrics\": {\"x\": int((ctx.payload or {}).get(\"max_items\", 0))}}\n"
        "    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"ready\": True}\n"
        "    def on_health(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_maintenance(ctx):\n"
        "    return _CONTRACT.on_maintenance(ctx)\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "datastore.maint",
            "plugin_type": "datastore",
            "module": "maintpkg.impl",
            "capabilities": {
                **_datastore_caps("Maint DS"),
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook", "handler": "on_maintenance"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns, results = run_plugin_contract_surface_collect(
            registry=registry,
            slots={"datastores": ["datastore.maint"]},
            surface="maintenance",
            config={},
            plugin_config={},
            workspace_root=str(tmp_path),
            strict=True,
            payload={"max_items": 7},
        )
        assert errs == []
        assert warns == []
        assert results == [("datastore.maint", {"handled": True, "metrics": {"x": 7}})]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_collect_health_returns_results(tmp_path: Path):
    pkg = tmp_path / "healthpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "from core.runtime.plugins import PluginHookContext\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_config(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_status(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_dashboard(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_maintenance(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"ready\": True}\n"
        "    def on_health(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"healthy\": True, \"detail\": \"ok\"}\n"
        "_CONTRACT = _Contract()\n"
        "def on_health(ctx):\n"
        "    return _CONTRACT.on_health(ctx)\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.health",
            "plugin_type": "adapter",
            "module": "healthpkg.impl",
            "capabilities": {
                "display_name": "Health Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook", "handler": "on_health"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns, results = run_plugin_contract_surface_collect(
            registry=registry,
            slots={"adapter": "adapter.health"},
            surface="health",
            config={},
            plugin_config={},
            workspace_root=str(tmp_path),
            strict=True,
            payload={},
        )
        assert errs == []
        assert warns == []
        assert results == [("adapter.health", {"healthy": True, "detail": "ok"})]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_collect_non_strict_emits_warnings(tmp_path: Path):
    pkg = tmp_path / "hookwarn"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "from core.runtime.plugins import PluginHookContext\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_config(self, ctx: PluginHookContext) -> None:\n"
        "        return None\n"
        "    def on_status(self, ctx: PluginHookContext) -> dict:\n"
        "        raise RuntimeError('status boom')\n"
        "    def on_dashboard(self, ctx: PluginHookContext) -> dict:\n"
        "        return {}\n"
        "    def on_maintenance(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"ready\": True}\n"
        "    def on_health(self, ctx: PluginHookContext) -> dict:\n"
        "        return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n"
        "def on_status(ctx):\n"
        "    return _CONTRACT.on_status(ctx)\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.warn",
            "plugin_type": "adapter",
            "module": "hookwarn.impl",
            "capabilities": {
                "display_name": "Warn Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook", "handler": "on_status"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)

    import sys
    sys.path.insert(0, str(tmp_path))
    try:
        errs, warns, results = run_plugin_contract_surface_collect(
            registry=registry,
            slots={"adapter": "adapter.warn"},
            surface="status",
            config={},
            strict=False,
        )
        assert results == []
        assert len(errs) == 1
        assert len(warns) == 1
        assert "status hook failed" in errs[0]
        assert errs[0] == warns[0]
    finally:
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_run_plugin_contract_surface_collect_missing_handler_strict_errors():
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.missing_handler",
            "plugin_type": "adapter",
            "module": "adaptors.missing",
            "capabilities": {
                "display_name": "Missing Handler Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    errs, warns, results = run_plugin_contract_surface_collect(
        registry=registry,
        slots={"adapter": "adapter.missing_handler"},
        surface="status",
        config={},
        strict=True,
    )
    assert results == []
    assert len(errs) == 1
    assert "hook missing handler declaration" in errs[0]
    assert warns == []


def test_run_plugin_contract_surface_collect_missing_handler_non_strict_warns():
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.missing_handler",
            "plugin_type": "adapter",
            "module": "adaptors.missing",
            "capabilities": {
                "display_name": "Missing Handler Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": []},
                    "api": {"mode": "declared", "exports": []},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry = PluginRegistry(api_version=1)
    registry.register(manifest)
    errs, warns, results = run_plugin_contract_surface_collect(
        registry=registry,
        slots={"adapter": "adapter.missing_handler"},
        surface="status",
        config={},
        strict=False,
    )
    assert results == []
    assert errs == []
    assert len(warns) == 1
    assert "hook missing handler declaration" in warns[0]


def test_validate_contract_instance_caches_repeated_validation(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "cachehook"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl.py").write_text(
        "from core.contracts.plugin_contract import PluginContractBase\n"
        "from core.runtime.plugins import PluginHookContext\n"
        "class _Contract(PluginContractBase):\n"
        "    def on_init(self, ctx: PluginHookContext): return None\n"
        "    def on_config(self, ctx: PluginHookContext): return None\n"
        "    def on_status(self, ctx: PluginHookContext): return {}\n"
        "    def on_dashboard(self, ctx: PluginHookContext): return {}\n"
        "    def on_maintenance(self, ctx: PluginHookContext): return {\"handled\": False}\n"
        "    def on_tool_runtime(self, ctx: PluginHookContext): return {\"ready\": True}\n"
        "    def on_health(self, ctx: PluginHookContext): return {\"healthy\": True}\n"
        "_CONTRACT = _Contract()\n",
        encoding="utf-8",
    )
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.cache",
            "plugin_type": "adapter",
            "module": "cachehook.impl",
            "capabilities": _contract_caps("Cache Adapter"),
        }
    )
    import core.runtime.plugins as plugin_runtime
    import sys
    sys.path.insert(0, str(tmp_path))
    real_import = plugin_runtime.importlib.import_module
    calls = {"module": 0}

    def _counting_import(name: str, package=None):
        if name == "cachehook.impl":
            calls["module"] += 1
        return real_import(name, package)

    monkeypatch.setattr(plugin_runtime.importlib, "import_module", _counting_import)
    plugin_runtime.reset_plugin_runtime()
    try:
        plugin_runtime._validate_contract_instance(manifest)
        plugin_runtime._validate_contract_instance(manifest)
        assert calls["module"] == 1
    finally:
        plugin_runtime.reset_plugin_runtime()
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_validate_manifest_rejects_invalid_declared_exports():
    with pytest.raises(ValueError, match="tools.exports must contain non-empty strings"):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "adapter.invalidexports",
                "plugin_type": "adapter",
                "module": "adaptors.bad",
                "capabilities": {
                    "display_name": "Bad Exports",
                    "contract": {
                        "init": {"mode": "hook"},
                        "config": {"mode": "hook"},
                        "status": {"mode": "hook"},
                        "dashboard": {"mode": "hook"},
                        "maintenance": {"mode": "hook"},
                        "tool_runtime": {"mode": "hook"},
                        "health": {"mode": "hook"},
                        "tools": {"mode": "declared", "exports": ["ok", ""]},
                        "api": {"mode": "declared", "exports": ["x"]},
                        "events": {"mode": "declared", "exports": []},
                        "ingest_triggers": {"mode": "declared", "exports": []},
                        "auth_requirements": {"mode": "declared", "exports": []},
                        "migrations": {"mode": "declared", "exports": []},
                        "notifications": {"mode": "declared", "exports": []},
                    },
                },
            }
        )


def test_collect_declared_exports_for_active_plugins():
    registry = PluginRegistry(api_version=1)
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.exports",
            "plugin_type": "adapter",
            "module": "adaptors.exports",
            "capabilities": {
                "display_name": "Exports Adapter",
                "contract": {
                    "init": {"mode": "hook"},
                    "config": {"mode": "hook"},
                    "status": {"mode": "hook"},
                    "dashboard": {"mode": "hook"},
                    "maintenance": {"mode": "hook"},
                    "tool_runtime": {"mode": "hook"},
                    "health": {"mode": "hook"},
                    "tools": {"mode": "declared", "exports": ["memory_recall", "memory_store"]},
                    "api": {"mode": "declared", "exports": ["openclaw_adapter_entry"]},
                    "events": {"mode": "declared", "exports": []},
                    "ingest_triggers": {"mode": "declared", "exports": []},
                    "auth_requirements": {"mode": "declared", "exports": []},
                    "migrations": {"mode": "declared", "exports": []},
                    "notifications": {"mode": "declared", "exports": []},
                },
            },
        }
    )
    registry.register(manifest)
    tools = collect_declared_exports(
        registry=registry,
        slots={"adapter": "adapter.exports"},
        surface="tools",
    )
    apis = collect_declared_exports(
        registry=registry,
        slots={"adapter": "adapter.exports"},
        surface="api",
    )
    assert tools == {"adapter.exports": ["memory_recall", "memory_store"]}
    assert apis == {"adapter.exports": ["openclaw_adapter_entry"]}
