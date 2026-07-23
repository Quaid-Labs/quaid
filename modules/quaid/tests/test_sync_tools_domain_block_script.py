from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest


def _load_script_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "sync-tools-domain-block.py"
    spec = importlib.util.spec_from_file_location("sync_tools_domain_block_script", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sync_tools_domain_block_requires_instance(tmp_path, monkeypatch, capsys):
    module = _load_script_module()

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(sys, "argv", ["sync-tools-domain-block.py", "--workspace", str(tmp_path)])

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 2
    assert "--instance or QUAID_INSTANCE is required" in capsys.readouterr().err


def test_sync_tools_domain_block_sets_instance_before_loading_db(tmp_path, monkeypatch):
    module = _load_script_module()
    workspace = tmp_path / "workspace"
    tools_dir = workspace / "projects" / "quaid"
    tools_dir.mkdir(parents=True)
    (tools_dir / "TOOLS.md").write_text("# Tools\n", encoding="utf-8")
    db_path = workspace / "instances" / "codex-demo" / "data" / "memory.db"
    calls: dict[str, object] = {}

    def fake_get_db_path():
        calls["instance_at_db_path"] = os.environ.get("QUAID_INSTANCE")
        return db_path

    def fake_load_active_domains(path, *, bootstrap_if_empty):
        calls["load_args"] = (path, bootstrap_if_empty)
        return {"technical": "Code and architecture"}

    def fake_sync_tools_domain_block(*, domains, workspace):
        calls["sync_args"] = (domains, workspace)
        return False

    monkeypatch.setitem(sys.modules, "lib.config", types.SimpleNamespace(get_db_path=fake_get_db_path))
    monkeypatch.setitem(
        sys.modules,
        "datastore.memorydb.domain_registry",
        types.SimpleNamespace(load_active_domains=fake_load_active_domains),
    )
    monkeypatch.setitem(
        sys.modules,
        "lib.tools_domain_sync",
        types.SimpleNamespace(sync_tools_domain_block=fake_sync_tools_domain_block),
    )
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-tools-domain-block.py", "--workspace", str(workspace), "--instance", "codex-demo"],
    )

    assert module.main() == 0
    assert calls["instance_at_db_path"] == "codex-demo"
    assert calls["load_args"] == (db_path, True)
    assert calls["sync_args"] == ({"technical": "Code and architecture"}, workspace.resolve())


def test_sync_tools_domain_block_accepts_instance_from_env(tmp_path, monkeypatch):
    module = _load_script_module()
    workspace = tmp_path / "workspace"
    tools_dir = workspace / "projects" / "quaid"
    tools_dir.mkdir(parents=True)
    (tools_dir / "TOOLS.md").write_text("# Tools\n", encoding="utf-8")
    db_path = workspace / "instances" / "codex-env" / "data" / "memory.db"
    calls: dict[str, object] = {}

    def fake_get_db_path():
        calls["instance_at_db_path"] = os.environ.get("QUAID_INSTANCE")
        return db_path

    def fake_load_active_domains(path, *, bootstrap_if_empty):
        calls["load_args"] = (path, bootstrap_if_empty)
        return {"technical": "Code and architecture"}

    def fake_sync_tools_domain_block(*, domains, workspace):
        calls["sync_args"] = (domains, workspace)
        return False

    monkeypatch.setitem(sys.modules, "lib.config", types.SimpleNamespace(get_db_path=fake_get_db_path))
    monkeypatch.setitem(
        sys.modules,
        "datastore.memorydb.domain_registry",
        types.SimpleNamespace(load_active_domains=fake_load_active_domains),
    )
    monkeypatch.setitem(
        sys.modules,
        "lib.tools_domain_sync",
        types.SimpleNamespace(sync_tools_domain_block=fake_sync_tools_domain_block),
    )
    monkeypatch.setenv("QUAID_INSTANCE", "codex-env")
    monkeypatch.setattr(sys, "argv", ["sync-tools-domain-block.py", "--workspace", str(workspace)])

    assert module.main() == 0
    assert calls["instance_at_db_path"] == "codex-env"
    assert calls["load_args"] == (db_path, True)
    assert calls["sync_args"] == ({"technical": "Code and architecture"}, workspace.resolve())
