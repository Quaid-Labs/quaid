from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from core.runtime.plugins import (
    PluginRegistry,
    discover_plugin_manifests,
    initialize_plugin_runtime,
    reset_plugin_runtime,
    validate_manifest_dict,
)


def test_validate_manifest_happy_path():
    manifest = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "openclaw.adapter",
            "plugin_type": "adapter",
            "module": "adaptors.openclaw.adapter",
            "entrypoint": "register",
            "capabilities": {"events": ["session.new"]},
        }
    )
    assert manifest.plugin_id == "openclaw.adapter"
    assert manifest.plugin_type == "adapter"
    assert manifest.module == "adaptors.openclaw.adapter"


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


def test_registry_rejects_plugin_id_conflict():
    registry = PluginRegistry(api_version=1)
    first = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "quaid.memorydb",
            "plugin_type": "datastore",
            "module": "datastore.memorydb",
            "capabilities": {
                "supports_multi_user": True,
                "supports_policy_metadata": True,
                "supports_redaction": True,
            },
        }
    )
    second = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "quaid.memorydb",
            "plugin_type": "datastore",
            "module": "datastore.memorydb.v2",
            "capabilities": {
                "supports_multi_user": True,
                "supports_policy_metadata": True,
                "supports_redaction": True,
            },
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
        }
    )
    adapter_b = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "adapter.b",
            "plugin_type": "adapter",
            "module": "adaptors.b",
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


def test_validate_manifest_requires_datastore_capabilities():
    with pytest.raises(ValueError, match="missing required capabilities"):
        validate_manifest_dict(
            {
                "plugin_api_version": 1,
                "plugin_id": "quaid.memorydb",
                "plugin_type": "datastore",
                "module": "datastore.memorydb",
                "capabilities": {"supports_multi_user": True},
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
                }
            )
        )

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(_register, range(100)))

    assert len(registry.list("adapter")) == 100
