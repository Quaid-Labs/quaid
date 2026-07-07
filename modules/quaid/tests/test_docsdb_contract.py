import builtins
import io
import logging
import os
import sys
from contextlib import redirect_stdout
from types import SimpleNamespace

import pytest

from lib.adapter import TestAdapter, reset_adapter, set_adapter

from core.plugins.docsdb_contract import DocsDbPluginContract
from core.runtime.plugins import PluginHookContext, PluginManifest
from datastore.docsdb.system_context import build_system_context_metadata


def _ctx(workspace_root: str) -> PluginHookContext:
    manifest = PluginManifest(
        plugin_api_version=1,
        plugin_id="docsdb.core",
        plugin_type="datastore",
        module="core.plugins.docsdb_contract",
        display_name="DocsDB",
    )
    return PluginHookContext(
        plugin=manifest,
        config=object(),
        plugin_config={},
        workspace_root=workspace_root,
    )


def _raise(exc: Exception):
    raise exc


def test_docsdb_contract_on_init_ensures_project_workspace_dirs(tmp_path, monkeypatch):
    visible_root = tmp_path / "visible"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_root))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    contract = DocsDbPluginContract()
    contract.on_init(_ctx(str(tmp_path)))

    assert (visible_root / "projects").is_dir()
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "scratch").exists()


def test_docsdb_contract_misc_workspace_failure_raises_under_failhard(tmp_path, monkeypatch):
    from core.plugins import docsdb_contract
    import lib.instance as instance_mod

    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        instance_mod,
        "instance_misc_dir",
        lambda: _raise(RuntimeError("misc directory unavailable")),
    )

    with pytest.raises(RuntimeError, match="misc directory unavailable"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))


def test_docsdb_contract_misc_workspace_failure_warns_when_fail_open(tmp_path, monkeypatch, caplog):
    from core.plugins import docsdb_contract
    import lib.instance as instance_mod

    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        instance_mod,
        "instance_misc_dir",
        lambda: _raise(RuntimeError("misc directory unavailable")),
    )

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert "docsdb misc workspace directory initialization failed" in caplog.text
    assert "misc directory unavailable" in caplog.text


def test_docsdb_contract_skips_misc_init_without_instance(tmp_path, monkeypatch, caplog):
    from core.plugins import docsdb_contract
    import lib.instance as instance_mod

    visible_root = tmp_path / "visible"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_root))
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        instance_mod,
        "instance_misc_dir",
        lambda: _raise(RuntimeError("instance context required")),
    )

    with caplog.at_level(logging.DEBUG, logger="core.plugins.docsdb_contract"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert (visible_root / "projects").is_dir()
    assert "docsdb misc init skipped: no QUAID_INSTANCE context" in caplog.text


def test_docsdb_contract_misc_docs_registry_failure_raises_under_failhard(tmp_path, monkeypatch):
    from core.plugins import docsdb_contract
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")

    class BrokenRegistry:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("docs registry unavailable")

    monkeypatch.setattr(registry_mod, "DocsRegistry", BrokenRegistry)

    with pytest.raises(RuntimeError, match="docs registry unavailable"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))


def test_docsdb_contract_misc_docs_registry_failure_warns_when_fail_open(
    tmp_path,
    monkeypatch,
    caplog,
):
    from core.plugins import docsdb_contract
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")

    class BrokenRegistry:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("docs registry unavailable")

    monkeypatch.setattr(registry_mod, "DocsRegistry", BrokenRegistry)

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert "docsdb misc project registry initialization failed" in caplog.text
    assert "docs registry unavailable" in caplog.text


def test_docsdb_contract_misc_project_registration_skips_config_reload(tmp_path, monkeypatch):
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    calls = []
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")

    class FakeRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_project(self, **kwargs):
            calls.append(kwargs)
            return None

    monkeypatch.setattr(registry_mod, "DocsRegistry", FakeRegistry)

    DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert calls
    assert calls[0]["name"] == "misc--pytest"
    assert calls[0]["reload_config"] is False
    assert calls[0]["quiet"] is True


def test_docsdb_contract_misc_project_create_value_error_logs_debug(tmp_path, monkeypatch, caplog):
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod
    import lib.project_registry as project_registry_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest")
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(project_registry_mod, "register", lambda **_kwargs: None)

    class FakeRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_project_definition(self, _name):
            return None

        def create_project(self, *_args, **_kwargs):
            raise ValueError("already exists")

    monkeypatch.setattr(registry_mod, "DocsRegistry", FakeRegistry)

    with caplog.at_level(logging.DEBUG, logger="core.plugins.docsdb_contract"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert "docsdb misc project create raised ValueError; assuming already registered" in caplog.text
    assert "already exists" in caplog.text


def test_docsdb_contract_misc_project_registration_is_stdout_silent(tmp_path, monkeypatch):
    import lib.config as config_mod
    import lib.instance as instance_mod

    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")
    monkeypatch.setattr(
        instance_mod,
        "instance_misc_dir",
        lambda: tmp_path / "visible" / "projects" / "misc--pytest",
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert stdout.getvalue() == ""


def test_docsdb_contract_misc_global_registry_failure_raises_under_failhard(tmp_path, monkeypatch):
    from core.plugins import docsdb_contract
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod
    import lib.project_registry as project_registry_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")

    class FakeRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_project(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(registry_mod, "DocsRegistry", FakeRegistry)
    monkeypatch.setattr(
        project_registry_mod,
        "register",
        lambda **_kwargs: _raise(RuntimeError("global registry unavailable")),
    )

    with pytest.raises(RuntimeError, match="global registry unavailable"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))


def test_docsdb_contract_misc_global_registry_failure_warns_when_fail_open(
    tmp_path,
    monkeypatch,
    caplog,
):
    from core.plugins import docsdb_contract
    from datastore.docsdb import registry as registry_mod
    import lib.config as config_mod
    import lib.instance as instance_mod
    import lib.project_registry as project_registry_mod

    misc_dir = tmp_path / "visible" / "projects" / "misc--pytest"
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path / "visible"))
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(instance_mod, "instance_misc_dir", lambda: misc_dir)
    monkeypatch.setattr(config_mod, "get_docs_db_path", lambda: tmp_path / "docs.db")

    class FakeRegistry:
        def __init__(self, *_args, **_kwargs):
            pass

        def create_project(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(registry_mod, "DocsRegistry", FakeRegistry)
    monkeypatch.setattr(
        project_registry_mod,
        "register",
        lambda **_kwargs: _raise(RuntimeError("global registry unavailable")),
    )

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        DocsDbPluginContract().on_init(_ctx(str(tmp_path)))

    assert "docsdb misc project global registry update failed" in caplog.text
    assert "global registry unavailable" in caplog.text


def test_docsdb_contract_on_config_ensures_project_workspace_dirs(tmp_path, monkeypatch):
    visible_root = tmp_path / "visible"
    monkeypatch.setenv("QUAID_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(visible_root))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    contract = DocsDbPluginContract()
    contract.on_config(_ctx(str(tmp_path)))

    assert (visible_root / "projects").is_dir()
    assert not (tmp_path / "projects").exists()
    assert not (tmp_path / "temp").exists()
    assert not (tmp_path / "scratch").exists()


def test_docsdb_contract_get_system_context_metadata(monkeypatch, tmp_path):
    contract = DocsDbPluginContract()
    monkeypatch.setattr(
        "core.plugins.docsdb_contract.build_docsdb_system_context_metadata",
        lambda: {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]},
    )

    payload = contract.get_system_context_metadata(_ctx(str(tmp_path)))

    assert payload == {"entries": [{"key": "ok", "label": "ok", "value": "delegated"}]}


def test_docsdb_contract_fail_hard_wrapper_fails_closed_on_import_error(monkeypatch):
    from core.plugins import docsdb_contract

    original_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "lib.fail_policy":
            raise ImportError("missing fail policy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    assert docsdb_contract._fail_hard_enabled() is True


def test_docsdb_contract_fail_hard_wrapper_propagates_policy_runtime_errors(monkeypatch):
    from core.plugins import docsdb_contract

    monkeypatch.setitem(
        sys.modules,
        "lib.fail_policy",
        SimpleNamespace(is_fail_hard_enabled=lambda: (_ for _ in ()).throw(RuntimeError("policy bug"))),
    )

    with pytest.raises(RuntimeError, match="policy bug"):
        docsdb_contract._fail_hard_enabled()


def test_supervisor_parent_pid_logs_before_fail_hard_raise(monkeypatch, caplog):
    from core.plugins import docsdb_contract

    monkeypatch.setenv("QUAID_SUPERVISOR_PID", "not-a-pid")
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        with pytest.raises(RuntimeError, match="invalid supervisor parent pid"):
            docsdb_contract._supervisor_parent_pid()

    assert "Ignoring invalid supervisor parent pid" in caplog.text


def test_project_docs_monitor_disabled_logs_before_fail_hard_raise(monkeypatch, caplog):
    from core.plugins import docsdb_contract

    monkeypatch.setenv("QUAID_SUPERVISOR_DISABLE", "1")
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        with pytest.raises(RuntimeError, match="project-docs supervisor is disabled"):
            docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

    assert "project-docs supervisor is disabled" in caplog.text


def test_project_docs_monitor_maintenance_logs_fail_soft(monkeypatch, caplog):
    from core.plugins import docsdb_contract

    result = SimpleNamespace(metrics={}, logs=[], errors=[], data={})
    ctx = SimpleNamespace(dry_run=False, options={})
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        docsdb_contract,
        "_queue_project_docs_monitor_requests",
        lambda **_kwargs: _raise(ValueError("queue failed")),
    )

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        out = docsdb_contract.run_project_docs_monitor_maintenance(ctx, lambda: result)

    assert out is result
    assert result.errors == ["Project-docs monitor maintenance failed: queue failed"]
    assert "Project-docs monitor maintenance failed: queue failed" in caplog.text


def test_project_docs_monitor_maintenance_logs_before_fail_hard_raise(monkeypatch, caplog):
    from core.plugins import docsdb_contract

    ctx = SimpleNamespace(dry_run=False, options={})
    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        docsdb_contract,
        "_queue_project_docs_monitor_requests",
        lambda **_kwargs: _raise(ValueError("queue failed")),
    )

    with caplog.at_level(logging.WARNING, logger="core.plugins.docsdb_contract"):
        with pytest.raises(RuntimeError, match="Project-docs monitor maintenance failed") as excinfo:
            docsdb_contract.run_project_docs_monitor_maintenance(
                ctx,
                lambda: SimpleNamespace(metrics={}, logs=[], errors=[], data={}),
            )

    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "Project-docs monitor maintenance failed: queue failed" in caplog.text


def test_project_docs_monitor_uses_supervisor_parent_without_reensure(monkeypatch):
    from core.plugins import docsdb_contract

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setattr("core.project_registry.list_projects", lambda: {"demo": {}})
    monkeypatch.setattr(
        "core.project_docs.request_update",
        lambda name, *, reason, requested_by: {"project": name, "request_id": "req-1"},
    )

    def _unexpected_ensure():
        raise AssertionError("supervisor-owned janitor worker must not re-bootstrap supervisor")

    monkeypatch.setattr("core.project_docs.ensure_supervisor_alive", _unexpected_ensure)

    result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

    assert result["requested"] == 1
    assert result["supervisor_pid"] == os.getpid()
    assert result["errors"] == []


def test_project_docs_monitor_skips_ambiguous_host_wide_projects(monkeypatch, caplog):
    from core.plugins import docsdb_contract

    requested: list[str] = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setattr(
        "core.project_registry.list_projects",
        lambda: {
            "shared": {"instances": ["openclaw-main", "codex-cdx-livetest"]},
            "solo": {"instances": ["openclaw-main"]},
        },
    )
    monkeypatch.setattr(
        "core.project_docs.request_update",
        lambda name, *, reason, requested_by: requested.append(name) or {"project": name, "request_id": f"req-{name}"},
    )

    with caplog.at_level(logging.INFO, logger="core.plugins.docsdb_contract"):
        result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

    assert requested == ["solo"]
    assert result["requested"] == 1
    assert result["projects"] == [{"name": "solo", "request_id": "req-solo"}]
    assert result["errors"] == []
    assert result["skipped_projects"] == [
        {
            "name": "shared",
            "reason": (
                "cannot queue host-wide project-docs monitor request without a resolvable "
                "QUAID_INSTANCE (valid_linked_instances=2: codex-cdx-livetest, openclaw-main)"
            ),
        }
    ]
    assert "Skipping project-docs monitor request for shared" in caplog.text


def test_project_docs_monitor_uses_ambient_instance_for_multi_linked_projects(monkeypatch):
    from core.plugins import docsdb_contract

    requested: list[str] = []

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_INSTANCE", "openclaw-main")
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setattr(
        "core.project_registry.list_projects",
        lambda: {"shared": {"instances": ["openclaw-main", "codex-cdx-livetest"]}},
    )
    monkeypatch.setattr(
        "core.project_docs.request_update",
        lambda name, *, reason, requested_by: requested.append(name) or {"project": name, "request_id": "req-shared"},
    )

    result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

    assert requested == ["shared"]
    assert result["requested"] == 1
    assert result["skipped_projects"] == []


def test_project_docs_monitor_materializes_queued_projects(tmp_path, monkeypatch):
    from core.plugins import docsdb_contract
    from core.project_registry import project_exists_raw
    from datastore.docsdb import project_log_queue

    monkeypatch.delenv("QUAID_SUPERVISOR_DISABLE", raising=False)
    monkeypatch.setenv("QUAID_SUPERVISOR_PID", str(os.getpid()))
    monkeypatch.setenv("QUAID_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_VISIBLE_HOME", str(tmp_path))
    monkeypatch.setenv("QUAID_INSTANCE", "pytest-runner")
    monkeypatch.setenv("MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    adapter = TestAdapter(tmp_path)
    set_adapter(adapter)
    try:
        project_log_queue.enqueue_project_logs(
            {"recipe-app": ["Queued project docs entry from transcript"]},
            trigger="Compaction",
        )
        monkeypatch.setattr("core.project_registry._sync_docs_registry_project", lambda *args, **kwargs: None)
        requests: list[str] = []
        monkeypatch.setattr(
            "core.project_docs.request_update",
            lambda name, *, reason, requested_by: requests.append(name) or {"project": name, "request_id": "req-1"},
        )

        result = docsdb_contract._queue_project_docs_monitor_requests(reason="janitor", requested_by="pytest")

        assert result["requested"] == 1
        assert requests == ["recipe-app"]
        assert project_exists_raw("recipe-app") is True
    finally:
        reset_adapter()


def test_project_docs_maintenance_event_auto_register_failure_raises_under_failhard(monkeypatch):
    from core.plugins import docsdb_contract

    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        docsdb_contract,
        "_auto_register_project_docs_via_docsdb",
        lambda _project=None: _raise(RuntimeError("register failed")),
    )

    with pytest.raises(RuntimeError, match="project docs auto-register listener failed") as excinfo:
        docsdb_contract.handle_project_docs_maintenance_event(
            {
                "payload": {
                    "source": "project-docs-supervisor",
                    "tick_kind": "auto_register_and_stale_index",
                    "observed_at": "2026-03-11T00:00:00Z",
                    "requested_operations": {"auto_register": True},
                }
            }
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "register failed" in str(excinfo.value.__cause__)


def test_project_docs_maintenance_event_stale_index_failure_raises_under_failhard(monkeypatch):
    from core.plugins import docsdb_contract

    monkeypatch.setattr(docsdb_contract, "_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        docsdb_contract,
        "_auto_register_project_docs_via_docsdb",
        lambda _project=None: (0, []),
    )
    monkeypatch.setattr(
        docsdb_contract,
        "_index_one_stale_registered_doc_via_docsdb",
        lambda _project=None: _raise(RuntimeError("index failed")),
    )

    with pytest.raises(RuntimeError, match="project docs stale-index listener failed") as excinfo:
        docsdb_contract.handle_project_docs_maintenance_event(
            {
                "payload": {
                    "source": "project-docs-supervisor",
                    "tick_kind": "auto_register_and_stale_index",
                    "observed_at": "2026-03-11T00:00:00Z",
                    "requested_operations": {"stale_index": True},
                }
            }
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert "index failed" in str(excinfo.value.__cause__)


def test_build_docsdb_system_context_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr("datastore.docsdb.system_context.current_instance_id", lambda: "openclaw-main")
    monkeypatch.setattr(
        "datastore.docsdb.system_context.list_projects",
        lambda: {
            "quaid": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "quaid"),
                "instances": ["openclaw-main", "claude-code-main"],
            },
            "other": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "other"),
                "instances": ["claude-code-main"],
            },
            "misc--openclaw-main": {
                "canonical_path": str(tmp_path / "shared" / "projects" / "misc--openclaw-main"),
                "instances": ["openclaw-main"],
            },
        },
    )

    payload = build_system_context_metadata()

    assert payload == {
        "entries": [
            {
                "key": "linked_projects",
                "label": "linked projects",
                "value": (
                    f"quaid ({tmp_path / 'shared' / 'projects' / 'quaid'}); "
                    f"misc--openclaw-main ({tmp_path / 'shared' / 'projects' / 'misc--openclaw-main'})"
                ),
                "note": (
                    "Preinject does not cover project or docs detail. "
                    "MANDATORY ORDER: For project document questions, run docs recall before filesystem grep/cat "
                    "(for example: quaid recall \"<query>\" '{\"stores\":[\"docs\"],\"project\":\"<project-name>\"}'). "
                    "Use filesystem reads only when docs recall returns no relevant hits, weak hits, or only index/catalog rows; "
                    "for read-only one-fact lookups, read the catalog-listed file directly without linking."
                ),
                "order": 30,
            }
        ]
    }


def test_docsdb_system_context_current_instance_logs_adapter_failure(monkeypatch, caplog):
    import lib.adapter as adapter_mod
    from datastore.docsdb.system_context import current_instance_id

    monkeypatch.delenv("QUAID_INSTANCE", raising=False)
    monkeypatch.setattr(adapter_mod, "get_adapter", lambda: _raise(RuntimeError("adapter unavailable")))

    with caplog.at_level(logging.DEBUG, logger="datastore.docsdb.system_context"):
        assert current_instance_id() == ""

    assert "Failed resolving docsdb system-context adapter instance id" in caplog.text
    assert "adapter unavailable" in caplog.text
