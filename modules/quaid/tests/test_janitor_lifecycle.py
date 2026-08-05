import importlib
import json
import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from core.lifecycle.janitor_lifecycle import (
    LifecycleRegistry,
    RoutineContext,
    RoutineResult,
    _register_module_routines,
    build_default_registry,
)
from lib.runtime_context import get_runtime_root


class _FakeRag:
    def __init__(self) -> None:
        self.calls = []
        self.index_calls = []

    def reindex_all(self, path: str, force: bool = False):
        self.calls.append((path, force))
        return {"total_files": 2, "indexed_files": 1, "skipped_files": 1, "total_chunks": 3}

    def needs_reindex_many(self, paths):
        return {str(p): True for p in paths}

    def index_document(self, path: str):
        self.index_calls.append(path)
        return 3


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
                lifecycle_prepass_timeout_retries=1,
            )
        ),
    )


@pytest.fixture(autouse=True)
def _disable_platform_scheduler_for_lifecycle_unit_tests(monkeypatch):
    from core.llm import scheduler as scheduler_mod

    scheduler_mod.reset_global_llm_scheduler(wait=False)
    scheduler_mod.reset_platform_scheduler_client()
    monkeypatch.setattr(scheduler_mod, "get_platform_scheduler_client_for_current_instance", lambda: None)
    yield
    scheduler_mod.reset_global_llm_scheduler(wait=False)
    scheduler_mod.reset_platform_scheduler_client()


def test_rag_lifecycle_runs_and_returns_metrics(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "projects" / "demo").mkdir(parents=True, exist_ok=True)

    fake_rag = _FakeRag()
    monkeypatch.setattr("datastore.docsdb.rag.DocsRAG", lambda: fake_rag)

    class _Registry:
        def __init__(self, *args, **kwargs):
            pass

        def get_all_project_definitions(self):
            return {
                "demo": SimpleNamespace(auto_index=True, home_dir="projects/demo"),
                "off": SimpleNamespace(auto_index=False, home_dir="projects/off"),
            }

        def auto_discover(self, _project_name):
            return ["a.md", "b.md"]

        def sync_external_files(self, _project_name):
            return None

        def list_docs(self):
            return [
                {"file_path": str(tmp_path / "docs" / "a.md")},
                {"file_path": str(tmp_path / "projects" / "demo" / "b.md")},
            ]

    docs_registry_mod = ModuleType("docs_registry")
    docs_registry_mod.DocsRegistry = _Registry
    monkeypatch.setitem(sys.modules, "docs_registry", docs_registry_mod)
    monkeypatch.setattr("datastore.docsdb.registry.DocsRegistry", _Registry)

    (tmp_path / "docs" / "a.md").write_text("# a\n")
    (tmp_path / "projects" / "demo" / "b.md").write_text("# b\n")

    ctx = RoutineContext(cfg=_make_cfg(projects_enabled=True), dry_run=False, workspace=tmp_path)
    handlers = {}

    class _Registry:
        def register(self, name, handler):
            handlers[name] = handler

    from datastore.docsdb.rag import register_lifecycle_routines

    register_lifecycle_routines(_Registry(), RoutineResult)
    result = handlers["rag"](ctx)

    assert result.errors == []
    assert result.metrics["project_files_discovered"] == 2
    assert result.metrics["rag_files_indexed"] == 3  # docs + project dir + registry pass
    assert result.metrics["rag_chunks_created"] == 9
    assert any("Reindexing" in line for line in result.logs)


def test_rag_lifecycle_handles_missing_routine():
    registry = build_default_registry()
    result = registry.run("missing", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=Path(".")))
    assert result.errors
    assert "No lifecycle routine registered" in result.errors[0]


def test_workspace_lifecycle_disabled_until_user_invoked_redesign(tmp_path):
    registry = build_default_registry()
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))

    assert result.errors
    assert "No lifecycle routine registered: workspace" in result.errors[0]


def test_snippets_and_journal_lifecycle_run(monkeypatch, tmp_path):
    calls = {"journal": []}

    monkeypatch.setattr("datastore.insightdb.soul_snippets.run_soul_snippets_review", lambda dry_run, **kwargs: {
        "folded": 4,
        "rewritten": 2,
        "discarded": 1,
    })

    def _run_journal_distillation(*, dry_run, force_distill, **kwargs):
        calls["journal"].append((dry_run, force_distill))
        return {"additions": 3, "edits": 1, "recovered_edits": 2, "total_entries": 9}

    monkeypatch.setattr("datastore.insightdb.soul_snippets.run_journal_distillation", _run_journal_distillation)

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
    assert journal_result.metrics["journal_recovered_edits"] == 2
    assert journal_result.metrics["journal_entries_distilled"] == 9
    assert calls["journal"] == [(True, True)]


def test_snippets_lifecycle_does_not_hold_files_lock_during_review(monkeypatch, tmp_path):
    from lib.resource_locks import ResourceLockRegistry

    calls = []

    def _run_snippets_review(*, dry_run, **kwargs):
        calls.append(dry_run)
        return {"folded": 1, "rewritten": 0, "discarded": 0}

    monkeypatch.setattr("datastore.insightdb.soul_snippets.run_soul_snippets_review", _run_snippets_review)

    cfg = _make_cfg(False)
    cfg.core.parallel.lock_wait_seconds = 1
    registry = build_default_registry()
    lock_registry = ResourceLockRegistry(get_runtime_root(tmp_path.resolve()) / "locks" / "janitor")

    with lock_registry.acquire_many(["files:global"], timeout_seconds=1):
        result = registry.run("snippets", RoutineContext(cfg=cfg, dry_run=False, workspace=tmp_path))

    assert calls == [False]
    assert result.errors == []
    assert result.metrics["snippets_folded"] == 1


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

    handlers = {}

    class _Registry:
        def register(self, name, handler):
            handlers[name] = handler

    from datastore.docsdb.updater import register_lifecycle_routines

    register_lifecycle_routines(_Registry(), RoutineResult)

    staleness_result = handlers["docs_staleness"](
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow)
    )
    assert staleness_result.errors == []
    assert staleness_result.metrics["docs_updated"] == 1
    assert [c[0] for c in calls["updated"]] == ["README.md"]

    cleanup_result = handlers["docs_cleanup"](
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, allow_doc_apply=_allow)
    )
    assert cleanup_result.errors == []
    assert cleanup_result.metrics["docs_cleaned"] == 1
    assert [c[0] for c in calls["cleaned"]] == ["README.md"]
    assert ("README.md", "staleness update") in allow_calls
    assert ("projects/x/NOTES.md", "cleanup") in allow_calls


def test_docs_lifecycle_staleness_raises_when_fail_hard(monkeypatch, tmp_path):
    from datastore.docsdb import updater
    from datastore.docsdb.updater import register_lifecycle_routines

    monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

    def fail_staleness():
        raise RuntimeError("stale failed")

    monkeypatch.setattr(updater, "check_staleness", fail_staleness)

    handlers = {}

    class _Registry:
        def register(self, name, handler):
            handlers[name] = handler

    register_lifecycle_routines(_Registry(), RoutineResult)

    with pytest.raises(RuntimeError, match="stale failed"):
        handlers["docs_staleness"](
            RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path)
        )


def test_docs_lifecycle_cleanup_raises_when_fail_hard(monkeypatch, tmp_path):
    from datastore.docsdb import updater
    from datastore.docsdb.updater import register_lifecycle_routines

    monkeypatch.setattr(updater, "is_fail_hard_enabled", lambda: True)

    def fail_cleanup():
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(updater, "check_cleanup_needed", fail_cleanup)

    handlers = {}

    class _Registry:
        def register(self, name, handler):
            handlers[name] = handler

    register_lifecycle_routines(_Registry(), RoutineResult)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        handlers["docs_cleanup"](
            RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path)
        )


def test_docsdb_monitor_lifecycle_queues_async_project_docs(monkeypatch, tmp_path):
    from core.plugins import docsdb_contract

    calls = []

    def _fake_queue(*, reason, requested_by):
        calls.append((reason, requested_by))
        return {
            "requested": 2,
            "projects": [
                {"name": "alpha", "request_id": "r1"},
                {"name": "beta", "request_id": "r2"},
            ],
            "errors": [],
            "supervisor_pid": 4321,
            "skipped": False,
        }

    monkeypatch.setattr(docsdb_contract, "_queue_project_docs_monitor_requests", _fake_queue)

    registry = build_default_registry()
    result = registry.run(
        "project_docs_monitor",
        RoutineContext(
            cfg=_make_cfg(projects_enabled=True),
            dry_run=False,
            workspace=tmp_path,
            options={"reason": "nightly", "requested_by": "pytest"},
        ),
    )

    assert result.errors == []
    assert calls == [("nightly", "pytest")]
    assert result.metrics["project_docs_update_requests"] == 2
    assert result.metrics["project_docs_update_request_errors"] == 0
    assert result.data["supervisor_pid"] == 4321
    assert any("Queued project-docs monitor refresh requests: 2" in line for line in result.logs)


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


def test_datastore_cleanup_retries_locked_database(monkeypatch, tmp_path):
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
            """
        )
        sleeps = []
        raised = {"locked": False}

        class _LockedOnceConn:
            def __enter__(self):
                conn.__enter__()
                return self

            def __exit__(self, exc_type, exc, tb):
                return conn.__exit__(exc_type, exc, tb)

            def execute(self, sql):
                if not raised["locked"] and str(sql).startswith("DELETE FROM"):
                    raised["locked"] = True
                    raise sqlite3.OperationalError("database is locked")
                return conn.execute(sql)

        class _Graph:
            calls = 0

            def _get_conn(self):
                self.calls += 1
                return _LockedOnceConn()

        graph = _Graph()
        monkeypatch.setattr("datastore.memorydb.memory_graph.time.sleep", lambda delay: sleeps.append(delay))

        result = build_default_registry().run(
            "datastore_cleanup",
            RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, graph=graph),
        )

        assert result.errors == []
        assert graph.calls == 2
        assert sleeps == [0.1]
        assert result.data["cleanup"]["recall_log"] == 1
        assert any("database busy" in line for line in result.logs)
    finally:
        conn.close()


def test_datastore_cleanup_reports_exhausted_locked_database(monkeypatch, tmp_path):
    from datastore.memorydb import memory_graph

    class _Graph:
        calls = 0

        def _get_conn(self):
            self.calls += 1
            raise sqlite3.OperationalError("database is locked")

    graph = _Graph()
    sleeps = []
    monkeypatch.setattr("datastore.memorydb.memory_graph.time.sleep", lambda delay: sleeps.append(delay))

    result = build_default_registry().run(
        "datastore_cleanup",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, graph=graph),
    )

    expected_delays = list(memory_graph._DATASTORE_BUSY_RETRY_DELAYS_SECONDS)
    assert graph.calls == len(expected_delays) + 1
    assert sleeps == expected_delays
    assert result.errors == ["Cleanup error: database is locked"]


def test_datastore_cleanup_does_not_retry_non_lock_sqlite_error(monkeypatch, tmp_path):
    class _Graph:
        calls = 0

        def _get_conn(self):
            self.calls += 1
            raise sqlite3.OperationalError("no such table: recall_log")

    graph = _Graph()
    sleeps = []
    monkeypatch.setattr("datastore.memorydb.memory_graph.time.sleep", lambda delay: sleeps.append(delay))

    result = build_default_registry().run(
        "datastore_cleanup",
        RoutineContext(cfg=_make_cfg(False), dry_run=False, workspace=tmp_path, graph=graph),
    )

    assert graph.calls == 1
    assert sleeps == []
    assert result.errors == ["Cleanup error: no such table: recall_log"]


def test_memory_graph_init_retries_locked_database(monkeypatch, tmp_path):
    from datastore.memorydb import memory_graph

    calls = []
    sleeps = []

    def flaky_init(self):
        calls.append(self.db_path)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory_graph.MemoryGraph, "_init_db", flaky_init)
    monkeypatch.setattr(memory_graph.time, "sleep", lambda delay: sleeps.append(delay))

    graph = memory_graph.MemoryGraph(db_path=tmp_path / "memory.db")

    assert graph.db_path == tmp_path / "memory.db"
    assert len(calls) == 3
    assert sleeps == list(memory_graph._DATASTORE_BUSY_RETRY_DELAYS_SECONDS[:2])


def test_memory_graph_init_raises_non_lock_sqlite_error_without_retry(monkeypatch, tmp_path):
    from datastore.memorydb import memory_graph

    calls = []
    sleeps = []

    def broken_init(self):
        calls.append(self.db_path)
        raise sqlite3.OperationalError("no such table: nodes")

    monkeypatch.setattr(memory_graph.MemoryGraph, "_init_db", broken_init)
    monkeypatch.setattr(memory_graph.time, "sleep", lambda delay: sleeps.append(delay))

    with pytest.raises(sqlite3.OperationalError, match="no such table: nodes"):
        memory_graph.MemoryGraph(db_path=tmp_path / "memory.db")

    assert calls == [tmp_path / "memory.db"]
    assert sleeps == []


def test_memory_graph_init_exhausts_locked_database_retry(monkeypatch, tmp_path):
    from datastore.memorydb import memory_graph

    calls = []
    sleeps = []

    def locked_init(self):
        calls.append(self.db_path)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory_graph.MemoryGraph, "_init_db", locked_init)
    monkeypatch.setattr(memory_graph.time, "sleep", lambda delay: sleeps.append(delay))

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        memory_graph.MemoryGraph(db_path=tmp_path / "memory.db")

    expected_delays = list(memory_graph._DATASTORE_BUSY_RETRY_DELAYS_SECONDS)
    assert calls == [tmp_path / "memory.db"] * (len(expected_delays) + 1)
    assert sleeps == expected_delays


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


def test_lifecycle_parallel_telemetry_honors_quaid_now(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", True)
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    registry._append_parallel_telemetry(tmp_path, {"event": "probe"})

    telemetry_path = tmp_path / "logs" / "janitor" / "lifecycle-parallel-telemetry.jsonl"
    rows = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
    assert rows == [{"ts": "2026-03-11T05:06:07+00:00", "event": "probe"}]


def test_lifecycle_parallel_telemetry_rejects_malformed_quaid_now(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", True)
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    with pytest.raises(ValueError, match="Invalid QUAID_NOW"):
        registry._append_parallel_telemetry(tmp_path, {"event": "probe"})

    assert not (tmp_path / "logs").exists()


def test_lifecycle_parallel_telemetry_logs_write_failure(monkeypatch, tmp_path, caplog):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", True)
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        registry._append_parallel_telemetry(tmp_path, {"event": "probe"})

    assert "Failed to append lifecycle parallel telemetry: disk full" in caplog.text


def test_lifecycle_parallel_telemetry_write_failure_raises_when_fail_hard(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", True)
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(RuntimeError, match="Failed to append lifecycle parallel telemetry") as excinfo:
        registry._append_parallel_telemetry(tmp_path, {"event": "probe"})

    assert str(excinfo.value.__cause__) == "disk full"


def test_lifecycle_parallel_telemetry_disabled_ignores_quaid_now(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", False)
    monkeypatch.setenv("QUAID_NOW", "not-a-date")

    registry._append_parallel_telemetry(tmp_path, {"event": "probe"})

    assert not (tmp_path / "logs").exists()


def test_lifecycle_run_many_preserves_explicit_zero_prepass_timeout(tmp_path):
    registry = LifecycleRegistry()

    def _slow(_ctx):
        time.sleep(0.02)
        return RoutineResult(metrics={"finished": 1})

    registry.register("slow", _slow)
    cfg = _make_cfg(False, lifecycle_timeout_seconds=0)

    result = registry.run_many(
        [("slow", RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path))],
        max_workers=1,
    )

    assert "timed out" in result["slow"].errors[0]
    assert "0.00s" in result["slow"].errors[0]


def test_lifecycle_run_many_run_id_honors_quaid_now(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    registry.register("noop", lambda _ctx: RoutineResult(metrics={"ok": 1}))
    monkeypatch.setattr(lifecycle_mod, "_LIFECYCLE_PARALLEL_TELEMETRY_ENABLED", True)
    monkeypatch.setenv("QUAID_NOW", "2026-03-11T05:06:07Z")

    result = registry.run_many(
        [("noop", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))],
        max_workers=1,
    )

    assert result["noop"].metrics == {"ok": 1}
    telemetry_path = tmp_path / "logs" / "janitor" / "lifecycle-parallel-telemetry.jsonl"
    rows = [json.loads(line) for line in telemetry_path.read_text().splitlines()]
    run_ids = {row["run_id"] for row in rows if "run_id" in row}
    assert len(run_ids) == 1
    assert next(iter(run_ids)).startswith("1773205567000-")


def test_lifecycle_registry_run_many_raises_config_failure_when_fail_hard(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    registry.register("noop", lambda _ctx: RoutineResult(metrics={"ok": 1}))
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        lifecycle_mod,
        "get_parallel_config",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("bad parallel config")),
    )

    with pytest.raises(RuntimeError, match="bad parallel config"):
        registry.run_many(
            [("noop", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))],
            max_workers=1,
        )


def test_lifecycle_registry_run_many_warns_on_timeout_config_failure_when_fail_open(
    monkeypatch, tmp_path, caplog
):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    registry.register("noop", lambda _ctx: RoutineResult(metrics={"ok": 1}))
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: False)
    calls = {"count": 0}

    def _get_parallel_config(_cfg):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("bad timeout config")
        return cfg.core.parallel

    monkeypatch.setattr(lifecycle_mod, "get_parallel_config", _get_parallel_config)

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        result = registry.run_many(
            [("noop", RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path))],
            max_workers=1,
        )

    assert result["noop"].metrics == {"ok": 1}
    assert "Failed to resolve lifecycle prepass timeout from config" in caplog.text
    assert "bad timeout config" in caplog.text


def test_lifecycle_registry_run_many_logs_future_failure_when_fail_open(monkeypatch, tmp_path, caplog):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    registry.register("boom", lambda _ctx: (_ for _ in ()).throw(RuntimeError("routine crashed")))
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: False)

    with caplog.at_level(logging.ERROR, logger="core.lifecycle.janitor_lifecycle"):
        result = registry.run_many(
            [("boom", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))],
            max_workers=1,
        )

    assert "Parallel lifecycle run failed for boom" in caplog.text
    assert "routine crashed" in result["boom"].errors[0]


def test_lifecycle_registry_run_many_raises_future_failure_when_fail_hard(monkeypatch, tmp_path):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    registry.register("boom", lambda _ctx: (_ for _ in ()).throw(RuntimeError("routine crashed")))
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: True)

    with pytest.raises(RuntimeError, match="Parallel lifecycle run failed for boom") as exc:
        registry.run_many(
            [("boom", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))],
            max_workers=1,
        )

    assert "routine crashed" in str(exc.value.__cause__)


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


def test_lifecycle_registry_run_many_preserves_done_futures_after_as_completed_timeout(monkeypatch, tmp_path):
    from core.lifecycle import janitor_lifecycle as jl

    registry = build_default_registry()

    def _fast(_ctx):
        return SimpleNamespace(metrics={"fast": 1}, logs=[], errors=[], data={})

    def _slow(_ctx):
        time.sleep(0.15)
        return SimpleNamespace(metrics={"slow": 1}, logs=[], errors=[], data={})

    registry.register("fast", _fast)
    registry.register("slow", _slow)

    def _always_timeout(_pending, timeout=None):
        raise TimeoutError()

    monkeypatch.setattr(jl, "as_completed", _always_timeout)

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
    cfg = _make_cfg(False, lifecycle_timeout_seconds=0.05)
    cfg.core.parallel.lifecycle_prepass_timeout_retries = 0

    with pytest.raises(TimeoutError, match="timed out"):
        registry.run(
            "slow_map",
            RoutineContext(
                cfg=cfg,
                dry_run=True,
                workspace=tmp_path,
            ),
        )


def test_lifecycle_registry_parallel_map_cancels_pending_on_worker_error(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    ctx = RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)
    started: list[int] = []

    def _worker(item: int):
        started.append(item)
        if item == 0:
            raise RuntimeError("boom")
        return item

    with pytest.raises(RuntimeError, match="boom"):
        registry._core_parallel_map(ctx, [0, 1, 2], _worker, max_workers=1)
    assert started == [0]


def test_lifecycle_registry_parallel_map_passes_scheduler_controls(monkeypatch, tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    cfg.core.parallel.llm_workers = 8
    cfg.core.parallel.lifecycle_prepass_timeout_seconds = 42
    cfg.core.parallel.lifecycle_prepass_timeout_retries = 3
    ctx = RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path)

    called = {}

    class _FakeScheduler:
        def run_map(self, **kwargs):
            called.update(kwargs)
            return [1, 2, 3]

    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.get_global_llm_scheduler", lambda: _FakeScheduler())
    out = registry._core_parallel_map(ctx, [1, 2, 3], lambda x: x, max_workers=2)

    assert out == [1, 2, 3]
    assert called["configured_workers"] == 8
    assert called["requested_workers"] == 2
    assert called["timeout_seconds"] == 42
    assert called["timeout_retries"] == 3
    assert called["workload_key"] == "lifecycle_prepass:default"


def test_lifecycle_registry_parallel_map_uses_routine_scoped_workload_key(monkeypatch, tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    ctx = RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path, options={"_lifecycle_routine": "snippets"})
    called = {}

    class _FakeScheduler:
        def run_map(self, **kwargs):
            called.update(kwargs)
            return [1]

    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.get_global_llm_scheduler", lambda: _FakeScheduler())
    out = registry._core_parallel_map(ctx, [1], lambda x: x, max_workers=1)
    assert out == [1]
    assert called["workload_key"] == "lifecycle_prepass:snippets"


def test_parallel_map_timeout_retries_honors_explicit_zero(tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    cfg.core.parallel.lifecycle_prepass_timeout_retries = 0
    ctx = RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path)
    assert registry._parallel_map_timeout_retries(ctx) == 0


def test_parallel_map_timeout_retries_env_override(monkeypatch, tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    cfg.core.parallel.lifecycle_prepass_timeout_retries = 5
    ctx = RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path)
    monkeypatch.setenv("QUAID_CORE_PARALLEL_MAP_TIMEOUT_RETRIES", "0")
    assert registry._parallel_map_timeout_retries(ctx) == 0


def test_parallel_map_timeout_config_failures_warn_when_fail_open(monkeypatch, tmp_path, caplog):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    registry = LifecycleRegistry()
    ctx = RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path)
    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        lifecycle_mod,
        "get_parallel_config",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("parallel config unavailable")),
    )

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        assert registry._parallel_map_timeout_seconds(ctx, default_seconds=12.5) == 12.5
        assert registry._parallel_map_timeout_retries(ctx, default_retries=3) == 3

    assert "Failed to read lifecycle parallel timeout config" in caplog.text
    assert "Failed to read lifecycle parallel timeout retry config" in caplog.text
    assert "parallel config unavailable" in caplog.text


def test_lock_config_preserves_explicit_zero_timeout(tmp_path):
    registry = LifecycleRegistry()
    cfg = _make_cfg()
    cfg.core.parallel.lock_wait_seconds = 0
    ctx = RoutineContext(cfg=cfg, dry_run=False, workspace=tmp_path)

    lock_cfg = registry._lock_config(ctx)

    assert lock_cfg["timeout_seconds"] == 1


def test_lifecycle_registry_uses_prepass_workers_from_config(monkeypatch, tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    cfg.core.parallel.llm_workers = 9
    cfg.core.parallel.lifecycle_prepass_workers = 2
    ctx = RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path)

    called = {}

    class _FakeScheduler:
        def run_map(self, **kwargs):
            called.update(kwargs)
            return [1, 2]

    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.get_global_llm_scheduler", lambda: _FakeScheduler())
    out = registry._core_parallel_map(ctx, [1, 2], lambda x: x, max_workers=None)
    assert out == [1, 2]
    assert called["configured_workers"] == 2


def test_lifecycle_registry_preserves_explicit_zero_prepass_workers(monkeypatch, tmp_path):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    cfg = _make_cfg(False)
    cfg.core.parallel.llm_workers = 9
    cfg.core.parallel.lifecycle_prepass_workers = 0
    ctx = RoutineContext(cfg=cfg, dry_run=True, workspace=tmp_path)

    called = {}

    class _FakeScheduler:
        def run_map(self, **kwargs):
            called.update(kwargs)
            return [1]

    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.get_global_llm_scheduler", lambda: _FakeScheduler())
    out = registry._core_parallel_map(ctx, [1], lambda x: x, max_workers=None)
    assert out == [1]
    assert called["configured_workers"] == 1


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


def test_register_module_routines_replaces_prior_failure_stub(monkeypatch, tmp_path):
    module_name = "adaptors.fake.lifecycle"
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: False)

    calls = {"count": 0}

    def _import_module(_name: str):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ImportError("simulated import failure")
        mod = ModuleType("fake_lifecycle")

        def _registrar(scoped, _result_type):
            scoped.register(
                "workspace",
                lambda _ctx: SimpleNamespace(metrics={"ok": 1}, logs=[], errors=[], data={}),
            )

        mod.register_lifecycle_routines = _registrar
        return mod

    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.importlib.import_module", _import_module)

    _register_module_routines(registry, module_name, ["workspace"])
    first = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))
    assert first.errors and "Lifecycle module load failed" in first.errors[0]

    _register_module_routines(registry, module_name, ["workspace"])
    second = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))
    assert second.errors == []
    assert second.metrics["ok"] == 1


def test_register_module_routines_logs_import_failure_when_fail_open(monkeypatch, caplog, tmp_path):
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "core.lifecycle.janitor_lifecycle.importlib.import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("simulated import failure")),
    )

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        _register_module_routines(registry, "adaptors.fake.lifecycle", ["workspace"])

    assert "Lifecycle module load failed: adaptors.fake.lifecycle: simulated import failure" in caplog.text
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))
    assert result.errors and "Lifecycle module load failed" in result.errors[0]


def test_register_module_routines_logs_missing_registrar_when_fail_open(monkeypatch, caplog, tmp_path):
    module_name = "adaptors.fake.lifecycle"
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "core.lifecycle.janitor_lifecycle.importlib.import_module",
        lambda _name: ModuleType(module_name),
    )

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        _register_module_routines(registry, module_name, ["workspace"])

    assert "Lifecycle module missing register_lifecycle_routines: adaptors.fake.lifecycle" in caplog.text
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))
    assert result.errors and "Lifecycle module missing register_lifecycle_routines" in result.errors[0]


def test_register_module_routines_logs_registration_failure_when_fail_open(monkeypatch, caplog, tmp_path):
    module_name = "adaptors.fake.lifecycle"
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: False)

    mod = ModuleType(module_name)

    def _registrar(_scoped, _result_type):
        raise RuntimeError("registration failed")

    mod.register_lifecycle_routines = _registrar
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.importlib.import_module", lambda _name: mod)

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        _register_module_routines(registry, module_name, ["workspace"])

    assert "Lifecycle registration failed: adaptors.fake.lifecycle: registration failed" in caplog.text
    result = registry.run("workspace", RoutineContext(cfg=_make_cfg(False), dry_run=True, workspace=tmp_path))
    assert result.errors and "Lifecycle registration failed" in result.errors[0]


def test_register_module_routines_raises_import_failure_when_fail_hard(monkeypatch):
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        "core.lifecycle.janitor_lifecycle.importlib.import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("simulated import failure")),
    )

    with pytest.raises(ImportError, match="simulated import failure"):
        _register_module_routines(registry, "adaptors.fake.lifecycle", ["workspace"])

    assert not registry.has("workspace")


def test_register_module_routines_raises_registration_failure_when_fail_hard(monkeypatch):
    module_name = "adaptors.fake.lifecycle"
    registry = LifecycleRegistry()
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.is_fail_hard_enabled", lambda: True)

    mod = ModuleType(module_name)

    def _registrar(_scoped, _result_type):
        raise RuntimeError("registration failed")

    mod.register_lifecycle_routines = _registrar
    monkeypatch.setattr("core.lifecycle.janitor_lifecycle.importlib.import_module", lambda _name: mod)

    with pytest.raises(RuntimeError, match="registration failed"):
        _register_module_routines(registry, module_name, ["workspace"])

    assert not registry.has("workspace")


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


def test_janitor_lifecycle_registry_lazy_init_is_single_build(monkeypatch):
    import core.lifecycle.janitor as janitor

    registry = object()
    calls: list[int] = []
    errors: list[BaseException] = []
    results: list[object] = []
    start = threading.Barrier(8)

    def _build_once():
        time.sleep(0.02)
        calls.append(1)
        return registry

    def _worker():
        try:
            start.wait()
            results.append(janitor._lifecycle_registry())
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(janitor, "_ensure_runtime_state", lambda: None)
    monkeypatch.setattr(janitor, "build_default_registry", _build_once)
    monkeypatch.setattr(janitor, "_LIFECYCLE_REGISTRY", None)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert errors == []
    assert len(results) == 8
    assert calls == [1]
    assert all(result is registry for result in results)


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


def test_lifecycle_registry_shutdown_is_noop():
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    registry = LifecycleRegistry()
    # LLM scheduler is process-global; lifecycle shutdown intentionally does nothing.
    registry.shutdown(wait=False)


def test_lifecycle_registry_uses_max_lock_registries_env(monkeypatch):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    monkeypatch.setenv("QUAID_MAX_LOCK_REGISTRIES", "3")
    registry = LifecycleRegistry()

    assert registry._max_lock_registries == 3


def test_lifecycle_registry_clamps_nonpositive_max_lock_registries(monkeypatch):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    monkeypatch.setenv("QUAID_MAX_LOCK_REGISTRIES", "0")
    registry = LifecycleRegistry()

    assert registry._max_lock_registries == 1


def test_lifecycle_registry_defaults_malformed_max_lock_registries(monkeypatch, caplog):
    from core.lifecycle.janitor_lifecycle import LifecycleRegistry

    monkeypatch.setenv("QUAID_MAX_LOCK_REGISTRIES", "bad")
    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        registry = LifecycleRegistry()

    assert registry._max_lock_registries == 64
    assert "Invalid QUAID_MAX_LOCK_REGISTRIES='bad'; using default 64" in caplog.text


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
    assert str((get_runtime_root(tmp_path / "b") / "locks" / "janitor").resolve()) in keys
    assert str((get_runtime_root(tmp_path / "c") / "locks" / "janitor").resolve()) in keys


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


def test_resolve_adapter_maintenance_module_raises_config_failure_when_fail_hard(monkeypatch):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: True)
    monkeypatch.setattr(
        "config.get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("bad plugin config")),
    )

    with pytest.raises(RuntimeError, match="bad plugin config"):
        lifecycle_mod._resolve_adapter_maintenance_module()


def test_resolve_adapter_maintenance_module_warns_on_fail_open(monkeypatch, caplog):
    import core.lifecycle.janitor_lifecycle as lifecycle_mod

    class _BadPath:
        def __init__(self, *_args):
            pass

        def resolve(self):
            return self

        @property
        def parents(self):
            return [self, self, self]

        def __truediv__(self, _other):
            raise RuntimeError("bad adaptors path")

    monkeypatch.setattr(lifecycle_mod, "is_fail_hard_enabled", lambda: False)
    monkeypatch.setattr(
        "config.get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("bad plugin config")),
    )
    monkeypatch.setattr(lifecycle_mod, "Path", _BadPath)

    with caplog.at_level(logging.WARNING, logger="core.lifecycle.janitor_lifecycle"):
        resolved = lifecycle_mod._resolve_adapter_maintenance_module(default_module="fallback.module")

    assert resolved == "fallback.module"
    assert "Failed to resolve adapter maintenance module from plugin config" in caplog.text
    assert "Failed to discover adapter maintenance module from local tree" in caplog.text
    assert "bad plugin config" in caplog.text
    assert "bad adaptors path" in caplog.text


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
