import json
from pathlib import Path

import pytest

from core.runtime.plugins import (
    PluginRegistry,
    discover_plugin_manifests,
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
        }
    )
    second = validate_manifest_dict(
        {
            "plugin_api_version": 1,
            "plugin_id": "quaid.memorydb",
            "plugin_type": "datastore",
            "module": "datastore.memorydb.v2",
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

