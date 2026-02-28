import sys
import sqlite3
import time
import importlib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.lifecycle.janitor_lifecycle import RoutineContext, build_default_registry


class _FakeRag:
    def __init__(self) -> None:
        self.calls = []

    def reindex_all(self, path: str, force: bool = False):
        self.calls.append((path, force))
        return {"total_files": 2, "indexed_files": 1, "skipped_files": 1, "total_chunks": 3}


def _make_cfg(projects_enabled: bool = True, lifecycle_timeout_seconds: float = 300.0):
    return SimpleNamespace(
        projects=SimpleNamespace(
            enabled=projects_enabled,
            definitions={
                "demo": SimpleNamespace(auto_index=True, home_dir="projects/demo"),
                "off": SimpleNamespace(auto_index=False, home_dir="projects/off"),
            },
        ),
        rag=SimpleNamespace(docs_dir="docs"),
        database=SimpleNamespace(path="data/memory.db"),
        core=SimpleNamespace(
            parallel=SimpleNamespace(
                enabled=True,
                lock_enforcement_enabled=True,
                lock_wait_seconds=5,
                lock_require_registration=True,
                lifecycle_prepass_timeout_seconds=lifecycle_timeout_seconds,
            )
        ),
    )


def test_rag_lifecycle_runs_and_returns_metrics(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "demo").mkdir(parents=True, exist_ok=True)

    fake_rag = _FakeRag()
    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", lambda: fake_rag)

    docs_registry_mod = ModuleType("docs_registry")

    class _Registry:
        def auto_discover(self, _project_name):
            return ["a.md", "b.md"]

        def sync_external_files(self, _project_name):
            return None

    docs_registry_mod.DocsRegistry = _Registry
    monkeypatch.setitem(sys.modules, "docs_registry", docs_registry_mod)

    project_updater_mod = ModuleType("project_updater")
    project_updater_mod.process_all_events = lambda: {"processed": 2}
    monkeypatch.setitem(sys.modules, "project_updater", project_updater_mod)

    registry = build_default_registry()
    ctx = RoutineContext(cfg=_make_cfg(projects_enabled=True), dry_run=False, workspace=tmp_path)
    result = registry.run("rag", ctx)

    assert result.errors == []
    assert result.metrics["project_events_processed"] == 2
    assert result.metrics["project_files_discovered"] == 2
    assert result.metrics["rag_files_indexed"] == 2  # docs + project dir
    assert result.metrics["rag_chunks_created"] == 6
    assert any("Reindexing" in line for line in result.logs)


def test_rag_lifecycle_handles_missing_routine():
    registry = build_default_registry()
    result = registry.run("missing", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=Path(".")))
    assert result.errors
    assert "No lifecycle routine registered" in result.errors[0]


def test_workspace_lifecycle_returns_phase_and_metrics(monkeypatch, tmp_path):
    monkeypatch.setattr("core.lifecycle.workspace_audit.run_workspace_check", lambda dry_run: {
        "phase": "apply",
        "moved_to_docs": 3,
        "moved_to_memory": 1,
        "trimmed": 2,
        "bloat_warnings": 1,
        "project_detected": 1,
        "bloat_stats": {
            "big.md": {"over_limit": True, "lines": 250, "maxLines": 200},
            "ok.md": {"over_limit": False, "lines": 10, "maxLines": 200},
        },
    })

    registry = build_default_registry()
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))

    assert result.errors == []
    assert result.data["workspace_phase"] == "apply"
    assert result.data["bloated_files"] == ["big.md"]
    assert result.metrics["workspace_moved_to_docs"] == 3
    assert result.metrics["workspace_project_detected"] == 1
    assert any("Would apply review decisions" in line for line in result.logs)


def test_snippets_and_journal_lifecycle_run(monkeypatch, tmp_path):
    calls = {"journal": []}

    monkeypatch.setattr("datastore.notedb.soul_snippets.run_soul_snippets_review", lambda dry_run, **kwargs: {
        "folded": 4,
        "rewritten": 2,
        "discarded": 1,
    })

    def _run_journal_distillation(*, dry_run, force_distill, **kwargs):
        calls["journal"].append((dry_run, force_distill))
        return {"additions": 3, "edits": 1, "total_entries": 9}

    monkeypatch.setattr("datastore.notedb.soul_snippets.run_journal_distillation", _run_journal_distillation)

    registry = build_default_registry()

    snippets_result = registry.run("snippets", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert snippets_result.errors == []
    assert snippets_result.metrics["snippets_folded"] == 4
    assert snippets_result.metrics["snippets_rewritten"] == 2
    assert snippets_result.metrics["snippets_discarded"] == 1

    journal_result = registry.run(
        "journal",
        RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path, force_distill=True),
    )
    assert journal_result.errors == []
    assert journal_result.metrics["journal_additions"] == 3
    assert journal_result.metrics["journal_edits"] == 1
    assert journal_result.metrics["journal_entries_distilled"] == 9
    assert calls["journal"] == [(True, True)]


def test_docs_lifecycle_staleness_and_cleanup(monkeypatch, tmp_path):
    calls = {"updated": [], "cleaned": []}

    monkeypatch.setattr(
        "datastore.docsdb.updater.get_doc_purposes",
        lambda: {"README.md": "summary", "projects/x/NOTES.md": "notes"},
    )
    monkeypatch.setattr("datastore.docsdb.updater.check_staleness", lambda: {
        "README.md": SimpleNamespace(gap_hours=2.5, stale_sources=["src/a.ts"]),
        "projects/x/NOTES.md": SimpleNamespace(gap_hours=1.0, stale_sources=["src/b.ts"]),
    })
    monkeypatch.setattr("datastore.docsdb.updater.update_doc_from_diffs", lambda doc_path, purpose, stale_sources, dry_run: (
        calls["updated"].append((doc_path, purpose, tuple(stale_sources), dry_run)) or True
    ))
    monkeypatch.setattr("datastore.docsdb.updater.check_cleanup_needed", lambda: {
        "README.md": SimpleNamespace(reason="updates", updates_since_cleanup=5, growth_ratio=1.0),
        "projects/x/NOTES.md": SimpleNamespace(reason="growth", updates_since_cleanup=1, growth_ratio=2.2),
    })
    monkeypatch.setattr("datastore.docsdb.updater.cleanup_doc", lambda doc_path, purpose, dry_run: (
        calls["cleaned"].append((doc_path, purpose, dry_run)) or True
    ))

    allow_calls = []

    def _allow(doc_path, action):
        allow_calls.append((doc_path, action))
        return doc_path.endswith("README.md")

    registry = build_default_registry()

    staleness_result = registry.run(
        "docs_staleness",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow),
    )
    assert staleness_result.errors == []
    assert staleness_result.metrics["docs_updated"] == 1
    assert [c[0] for c in calls["updated"]] == ["README.md"]

    cleanup_result = registry.run(
        "docs_cleanup",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow),
    )
    assert cleanup_result.errors == []
    assert cleanup_result.metrics["docs_cleaned"] == 1
    assert [c[0] for c in calls["cleaned"]] == ["README.md"]
    assert ("README.md", "staleness update") in allow_calls
    assert ("projects/x/NOTES.md", "cleanup") in allow_calls


def test_datastore_cleanup_lifecycle_runs_with_graph_override(tmp_path):
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE recall_log (created_at TEXT);
            CREATE TABLE dedup_log (review_status TEXT, created_at TEXT);
            CREATE TABLE health_snapshots (created_at TEXT);
            CREATE TABLE embedding_cache (created_at TEXT);
            CREATE TABLE janitor_metadata (key TEXT, value TEXT, updated_at TEXT);
            CREATE TABLE janitor_runs (completed_at TEXT);
            INSERT INTO recall_log VALUES ('2000-01-01');
            INSERT INTO dedup_log VALUES ('done', '2000-01-01');
            INSERT INTO health_snapshots VALUES ('2000-01-01');
            INSERT INTO embedding_cache VALUES ('2000-01-01');
            INSERT INTO janitor_metadata VALUES ('update_check', '{}', '2000-01-01');
            INSERT INTO janitor_runs VALUES ('2000-01-01');
            """
        )

        class _Graph:
            def _get_conn(self):
                return conn

        registry = build_default_registry()
        result = registry.run(
            "datastore_cleanup",
            RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, graph=_Graph()),
        )
        assert result.errors == []
        assert result.data["cleanup"]["recall_log"] == 1
        assert result.data["cleanup"]["janitor_metadata"] == 1
        assert result.data["cleanup"]["janitor_runs"] == 1
    finally:
        conn.close()


def test_lifecycle_registry_run_many_executes_in_parallel_shape(tmp_path):
    registry = build_default_registry()

    def _ok_a(_ctx):
        return SimpleNamespace(metrics={"a": 1}, logs=[], errors=[], data={})

    def _ok_b(_ctx):
        return SimpleNamespace(metrics={"b": 1}, logs=[], errors=[], data={})

    registry.register("a", _ok_a)
    registry.register("b", _ok_b)

    out = registry.run_many(
        [
            ("a", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
            ("b", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
        ],
        max_workers=2,
    )
    assert set(out.keys()) == {"a", "b"}
    assert out["a"].metrics["a"] == 1
    assert out["b"].metrics["b"] == 1


def test_lifecycle_registry_run_many_times_out_pending_tasks(tmp_path):
    registry = build_default_registry()

    def _fast(_ctx):
        return SimpleNamespace(metrics={"fast": 1}, logs=[], errors=[], data={})

    def _slow(_ctx):
        time.sleep(0.2)
        return SimpleNamespace(metrics={"slow": 1}, logs=[], errors=[], data={})

    registry.register("fast", _fast)
    registry.register("slow", _slow)

    out = registry.run_many(
        [
            ("fast", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
            ("slow", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)),
        ],
        max_workers=2,
        overall_timeout_seconds=0.05,
    )
    assert out["fast"].metrics.get("fast") == 1
    assert out["slow"].errors
    assert "timed out" in out["slow"].errors[0]


def test_lifecycle_registry_parallel_map_times_out(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()

    def _slow_map(ctx):
        assert ctx.parallel_map is not None
        ctx.parallel_map([1, 2], lambda _item: time.sleep(0.2), max_workers=2)
        return SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={})

    registry.register("slow_map", _slow_map)

    with pytest.raises(TimeoutError, match="timed out"):
        registry.run(
            "slow_map",
            RoutineContext(
                cfg=_make_cfg(False, lifecycle_timeout_seconds=0.05),
                dry_run=True,
                workspace=tmp_path,
            ),
        )


def test_lifecycle_registry_requires_write_registration_when_enabled(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry.register("writer", lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}))

    result = registry.run("writer", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert result.errors
    assert "missing write resource registration" in result.errors[0]


def test_lifecycle_registry_allows_registered_write_locks(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry.register(
        "writer",
        lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}),
        write_resources=["files:global", "db:memory"],
    )

    result = registry.run("writer", RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path))
    assert result.errors == []
    assert result.metrics["ok"] == 1


def test_lifecycle_registry_allows_idempotent_reregister_same_owner(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()

    def _writer(_ctx):
        return SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={})

    registry.register("writer", _writer, owner="memorydb", write_resources=["files:global"])
    registry.register("writer", _writer, owner="memorydb")

    result = registry.run(
        "writer",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path),
    )
    assert result.errors == []
    assert result.metrics["ok"] == 1


def test_lifecycle_registry_rejects_conflicting_reregister():
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry.register("writer", lambda _ctx: SimpleNamespace(metrics={}, logs=[], errors=[], data={}), owner="memorydb")
    with pytest.raises(ValueError, match="already registered"):
        registry.register("writer", lambda _ctx: SimpleNamespace(metrics={}, logs=[], errors=[], data={}), owner="other")


def test_lifecycle_registry_register_and_has_use_registry_guard():
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    class _CountingLock:
        def __init__(self) -> None:
            self.calls = 0

        def __enter__(self):
            self.calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    registry = LifecycleRegistry()
    counter = _CountingLock()
    registry._registry_guard = counter  # Intentional white-box check for thread-safety guard coverage.
    registry.register("writer", lambda _ctx: SimpleNamespace(metrics={}, logs=[], errors=[], data={}), owner="memorydb")
    assert registry.has("writer") is True
    assert counter.calls >= 2


def test_lifecycle_registry_skips_lock_enforcement_when_disabled(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    cfg = _make_cfg(False)
    cfg.core.parallel.lock_enforcement_enabled = False
    registry = LifecycleRegistry()
    registry.register("writer", lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}))

    result = registry.run("writer", RoutineContext(cfg=cfg, dry_run=False, workspace=tmp_path))
    assert result.errors == []
    assert result.metrics["ok"] == 1


def test_lifecycle_registry_resolves_write_resources_to_absolute_paths(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    cfg = _make_cfg(False)
    cfg.database.path = "state/memory.db"
    registry = LifecycleRegistry()
    registry.register(
        "writer",
        lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}),
        write_resources=["db:memory", "core_markdown", "files:global", "file:docs/AGENTS.md"],
    )
    ctx = RoutineContext(cfg=cfg, dry_run=False, workspace=tmp_path)
    resolved = registry._resolved_write_resources("writer", ctx)  # Intentional private call for normalization coverage.

    assert "files:global" in resolved
    assert f"db:{(tmp_path / 'state' / 'memory.db').resolve()}" in resolved
    assert f"file:{(tmp_path / 'docs' / 'AGENTS.md').resolve()}" in resolved


def test_lifecycle_registry_shutdown_releases_llm_executor():
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    ex = registry._ensure_llm_executor(2)
    assert ex is not None
    assert registry._llm_executor is not None

    registry.shutdown(wait=False)
    assert registry._llm_executor is None


def test_lifecycle_registry_caps_workspace_lock_registry_cache(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    registry._max_lock_registries = 2  # White-box cap to force eviction behavior.

    reg1 = registry._lock_registry_for_workspace(tmp_path / "a")
    reg2 = registry._lock_registry_for_workspace(tmp_path / "b")
    reg3 = registry._lock_registry_for_workspace(tmp_path / "c")

    assert reg1 is not None and reg2 is not None and reg3 is not None
    assert len(registry._lock_registries) == 2
    keys = set(registry._lock_registries.keys())
    assert str((tmp_path / "b" / ".quaid" / "runtime" / "locks" / "janitor").resolve()) in keys
    assert str((tmp_path / "c" / ".quaid" / "runtime" / "locks" / "janitor").resolve()) in keys


def test_lifecycle_env_modules_reject_unapproved_prefix(monkeypatch):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    seen: list[str] = []
    real_import_module = importlib.import_module

    def _spy_import(module_name: str, *args, **kwargs):
        seen.append(module_name)
        return real_import_module(module_name, *args, **kwargs)

    monkeypatch.setattr(lifecycle_mod.importlib, "import_module", _spy_import)
    monkeypatch.setenv("QUAID_LIFECYCLE_MODULES", "evil.module")
    build_default_registry()

    assert "evil.module" not in seen


def test_resolve_adapter_maintenance_module_from_active_manifest(monkeypatch):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    fake_cfg = SimpleNamespace(
        plugins=SimpleNamespace(
            slots=SimpleNamespace(adapter="custom.adapter"),
            paths=["plugins"],
            allowlist=[],
        )
    )
    fake_manifest = SimpleNamespace(plugin_id="custom.adapter", module="adaptors.custom.adapter")

    monkeypatch.setattr("config.get_config", lambda: fake_cfg)
    monkeypatch.setattr(
        "core.runtime.plugins.discover_plugin_manifests",
        lambda **_kwargs: ([fake_manifest], []),
    )

    resolved = lifecycle_mod._resolve_adapter_maintenance_module()
    assert resolved == "adaptors.custom.maintenance"


def test_lifecycle_env_module_can_register_write_resources(monkeypatch, tmp_path):
    module_name = "core.testext"
    mod = ModuleType(module_name)

    def _register(registry, result_factory):
        def _routine(_ctx):
            return result_factory(metrics={"ok": 1})

        registry.register(
            "testext",
            _routine,
            write_resources=["db:memory", "files:global"],
        )

    mod.register_lifecycle_routines = _register
    monkeypatch.setitem(sys.modules, module_name, mod)
    monkeypatch.setenv("QUAID_LIFECYCLE_MODULES", module_name)

    registry = build_default_registry()
    assert registry.has("testext")
    assert registry._write_resources.get("testext") == ["db:memory", "files:global"]

    result = registry.run("testext", RoutineContext(cfg=_make_cfg(), dry_run=True, workspace=tmp_path))
    assert result.errors == []
    assert result.metrics.get("ok") == 1
